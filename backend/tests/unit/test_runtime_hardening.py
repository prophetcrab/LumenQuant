"""Scheduler-loop robustness and cost.

Three properties the runner needs but did not assert:

* a bar whose evaluation crashed is retried, not consumed for good;
* a repeated quiet tick does not read and re-aggregate the whole series;
* one broken instance cannot leave the instances after it unstopped.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.alerts.service import AlertService
from app.modules.market.service_types import CandlePoint, SymbolView
from app.modules.runtime.service import RuntimeService
from app.modules.strategies.base import Signal, Strategy, StrategyParams, StrategyState
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.service import TradingService

BAR_MS = 3_600_000


class _BuyOnce(Strategy):
    key = "test_buy_once"
    name = "测试买入"
    category = "benchmark"
    description = "test only"

    class Params(StrategyParams):
        pass

    def prepare(self, candles, params):
        return StrategyState(candles=candles, params=params)

    def warmup(self, params):
        return 1

    def evaluate(self, state, index):
        return Signal("buy", "test entry")


BUY = _BuyOnce()


class _CrashOnPrepare(_BuyOnce):
    key = "test_crash_prepare"
    name = "测试崩溃"
    fail = True

    def prepare(self, candles, params):
        if self.fail:
            raise RuntimeError("indicator exploded")
        return super().prepare(candles, params)


CRASHER = _CrashOnPrepare()


class Market:
    """Fake market whose clock advances independently of the bar series.

    `reads` counts candle reads so a test can prove the clock gate avoided one.
    """

    def __init__(self) -> None:
        self.now = 1_700_000_000_000
        self.series = [
            CandlePoint(
                time=self.now + index * BAR_MS,
                open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0,
            )
            for index in range(120)
        ]
        self.reads = 0
        self.value = 100.0
        self.price_failures: dict[str, BaseException] = {}
        self.price_reads: list[str] = []

    def last_price(self, symbol: str) -> float:
        self.price_reads.append(symbol)
        failure = self.price_failures.get(symbol)
        if failure is not None:
            raise failure
        return self.value

    def price(self, symbol: str) -> float:
        return self.last_price(symbol)

    def symbol(self, code: str) -> SymbolView:
        return self.require_symbol(code)

    def supported_timeframes(self, market: str = "crypto"):
        return ("15m", "30m", "1h", "4h", "1d") if market != "cn_stock" else ("15m", "30m", "1h", "1d")

    def require_symbol(self, code: str) -> SymbolView:
        return SymbolView(
            code=code, name=code, asset_class="test", base=code, quote="USDT",
            tick_size=0.01, lot_size=1.0, initial_price=100.0, enabled=True,
            market="crypto",
        )

    def candles(self, symbol, *, timeframe="1h", limit=600, closed_only=False):
        self.reads += 1
        return list(self.series)

    # The two methods the runner uses for its clock gate.
    def now_ms(self, _symbol: str) -> int:
        return self.now

    def next_close_after(self, _symbol: str, _timeframe: str, open_time: int) -> int:
        return open_time + 2 * BAR_MS


def _stack(tmp_path, monkeypatch):
    import app.modules.runtime.service as runtime_module

    registry = {"test_buy_once": BUY, "test_crash_prepare": CRASHER}
    monkeypatch.setattr(runtime_module, "get", lambda key: registry[key])
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'hardening.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    market = Market()
    accounts = AccountService(session)
    account = accounts.create(name="Auto", initial_balance=10_000)
    session.commit()
    trading = TradingService(
        session, cash=AccountCashAdapter(accounts), prices=market, fee_bps=0, slippage_bps=0
    )
    service = RuntimeService(
        session,
        accounts_service=accounts,
        market=market,
        trading=trading,
        alerts=AlertService(session),
    )
    return db, session, service, account, market


def _create(service, account_id, key, symbol="BTCUSDT", **overrides):
    return service.create(
        account_id=account_id,
        strategy_key=key,
        symbol=symbol,
        timeframe="1h",
        params={},
        position_pct=0.5,
        max_order_notional=5_000,
        start=True,
        **overrides,
    )


def test_a_crash_does_not_consume_the_bar(tmp_path, monkeypatch):
    """The cursor must not advance for a bar the strategy failed to evaluate.

    Mirrors the scheduler: it catches the exception, records `last_error` and
    *commits*. That commit is what made an early cursor write permanent, so the
    bar was skipped for good and the signal never existed.
    """
    db, session, service, account, _ = _stack(tmp_path, monkeypatch)
    view = _create(service, account.id, "test_crash_prepare")

    # Exactly what StrategyRuntimeEngine.tick does around one instance.
    row = service._require(view.id)
    try:
        service.evaluate_instance(row)
    except Exception as exc:
        row.last_error = str(exc)[:400]
        session.commit()

    assert service._require(view.id).last_bar_time == 0, "the failed bar was consumed"

    CRASHER.fail = False
    try:
        assert service.evaluate_instance(service._require(view.id)) == "executed"
        assert service._require(view.id).last_bar_time > 0
        assert len(service.trading.positions(account.id)) == 1
    finally:
        CRASHER.fail = True
        db.dispose()


def test_a_quiet_tick_skips_the_series_read(tmp_path, monkeypatch):
    """A bar that cannot have closed yet must not trigger a full candle read."""
    db, session, service, account, market = _stack(tmp_path, monkeypatch)
    view = _create(service, account.id, "test_buy_once")
    row = service._require(view.id)
    assert service.evaluate_instance(row) == "executed"
    first_bar = service._require(row.id).last_bar_time
    # Sit halfway through the next bar's interval: it has not closed yet.
    market.now = first_bar + BAR_MS // 2
    before = market.reads

    # The next bar is still forming, so nothing new can exist.
    for _ in range(5):
        assert service.evaluate_instance(service._require(row.id)) == "no_new_bar"
    assert market.reads == before, "the clock gate should have skipped the read"

    # Once the clock passes the next close, the read happens again.
    market.now = first_bar + 2 * BAR_MS
    service.evaluate_instance(service._require(row.id))
    assert market.reads == before + 1
    db.dispose()


def test_clock_gate_failure_falls_back_to_reading(tmp_path, monkeypatch):
    """A provider that cannot answer the gate must not suppress the signal."""
    db, session, service, account, market = _stack(tmp_path, monkeypatch)
    view = _create(service, account.id, "test_buy_once")
    row = service._require(view.id)
    service.evaluate_instance(row)
    before = market.reads

    def broken(*_args, **_kwargs):
        raise RuntimeError("clock offline")

    market.next_close_after = broken
    # With a broken clock the gate reports "maybe", so the series is read and
    # the unchanged last bar correctly resolves to no_new_bar.
    assert service.evaluate_instance(service._require(row.id)) == "no_new_bar"
    assert market.reads == before + 1
    db.dispose()


def test_one_broken_instance_does_not_block_the_others_stops(tmp_path, monkeypatch):
    """A failure inside one stop check must not leave later instances unstopped."""
    db, session, service, account, market = _stack(tmp_path, monkeypatch)
    first = _create(service, account.id, "test_buy_once", stop_loss_pct=5)
    second = _create(
        service, account.id, "test_buy_once", symbol="ETHUSDT", stop_loss_pct=5
    )
    for row in service.running_instances():
        service.evaluate_instance(row)
    assert len(service.trading.positions(account.id)) == 2

    market.value = 50.0
    # The first instance's position lookup fails in a way the inner handlers do
    # not cover (a corrupt row, a DB error). The second instance's stop must
    # still run instead of the whole sweep aborting.
    original = service._position_row

    def flaky(account_id, symbol):
        if symbol.upper() == "BTCUSDT":
            raise RuntimeError("position row unreadable")
        return original(account_id, symbol)

    service._position_row = flaky
    outcomes = service.protect_positions()

    assert outcomes.get(first.id) == "protect_failed"
    assert outcomes.get(second.id) == "stop_loss"
    assert {p.symbol for p in service.trading.positions(account.id)} == {"BTCUSDT"}
    # The failure is visible, not silent.
    logs = service.logs(first.id)
    assert any(log.event == "protect_failed" for log in logs)
    db.dispose()


def test_shared_symbol_reads_price_once_per_sweep(tmp_path, monkeypatch):
    """Two instances on one symbol must not each query the feed."""
    db, session, service, account, market = _stack(tmp_path, monkeypatch)
    _create(service, account.id, "test_buy_once", stop_loss_pct=5)
    _create(service, account.id, "test_buy_once", stop_loss_pct=5)
    for row in service.running_instances():
        service.evaluate_instance(row)

    market.price_reads = []
    service.protect_positions()
    assert market.price_reads.count("BTCUSDT") == 1
    db.dispose()


def test_a_failed_stop_check_does_not_roll_back_the_other_instances(tmp_path, monkeypatch):
    """A failure in one instance must not undo what the sweep already wrote.

    The sweep shares one transaction (and the collaborators commit on their own
    anyway), so a failure late in the loop must leave earlier instances' peak
    updates in place instead of rebuilding them from scratch next tick.
    """
    db, session, service, account, market = _stack(tmp_path, monkeypatch)
    # First instance carries a trailing stop so the sweep records a new peak.
    first = _create(service, account.id, "test_buy_once", trailing_stop_pct=50)
    second = _create(
        service, account.id, "test_buy_once", symbol="ETHUSDT", stop_loss_pct=5
    )
    for row in service.running_instances():
        service.evaluate_instance(row)

    # Price rises: the first instance's peak must ratchet up during the sweep.
    market.value = 120.0
    original = service._position_row

    def flaky(account_id, symbol):
        if symbol.upper() == "ETHUSDT":
            raise RuntimeError("position row unreadable")
        return original(account_id, symbol)

    service._position_row = flaky
    outcomes = service.protect_positions()

    assert first.id not in outcomes  # nothing triggered, peak only
    assert outcomes.get(second.id) == "protect_failed"
    # The other instance's work survived the failure.
    assert service._require(first.id).peak_price == pytest.approx(120.0)
    db.dispose()
