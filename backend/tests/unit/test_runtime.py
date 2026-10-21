from __future__ import annotations

import pytest

from app.modules.accounts.service import AccountService
from app.modules.market.service_types import CandlePoint, SymbolView
from app.modules.runtime.service import RuntimeService
from app.modules.strategies.base import Signal, Strategy, StrategyParams, StrategyState
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.service import TradingService


class _BuyNow(Strategy):
    key = "test_buy_now"
    name = "测试立即买入"
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


class _SellNow(_BuyNow):
    key = "test_sell_now"
    name = "测试立即卖出"

    def evaluate(self, state, index):
        return Signal("sell", "test exit")


class Prices:
    current = 100.0

    def price(self, _symbol):
        return self.current

    def symbol(self, code):
        return SymbolView(
            code=code, name=code, asset_class="test", base=code, quote="USDT",
            tick_size=0.01, lot_size=0.01, initial_price=self.current, enabled=True,
        )


class Market:
    def __init__(self) -> None:
        self.time = 1_700_000_000_000
        self.closed_only_seen: bool | None = None
        self.current = 100.0

    def last_price(self, _symbol: str) -> float:
        return self.current

    def supported_timeframes(self, market: str = "crypto"):
        # Mirrors the real service so instance creation exercises the same guard.
        return ("15m", "30m", "1h", "4h", "1d") if market != "cn_stock" else ("15m", "30m", "1h", "1d")

    def require_symbol(self, code):
        return SymbolView(
            code=code, name=code, asset_class="test", base=code, quote="USDT",
            tick_size=0.01, lot_size=1.0, initial_price=100.0, enabled=True,
        )

    def candles(self, symbol, *, timeframe="1h", limit=600, closed_only=False):
        self.closed_only_seen = closed_only
        return [
            CandlePoint(
                time=self.time + i * 3_600_000,
                open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0,
            )
            for i in range(120)
        ]


STUBS = {"test_buy_now": _BuyNow(), "test_sell_now": _SellNow()}


@pytest.fixture()
def env(database, monkeypatch):
    # Keep the production registry untouched; inject stub strategies instead.
    monkeypatch.setattr("app.modules.runtime.service.get", lambda key: STUBS[key])
    prices = Prices()
    market = Market()
    with database.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="Auto", initial_balance=10_000)
        trading = TradingService(
            session, cash=AccountCashAdapter(accounts), prices=prices, fee_bps=0, slippage_bps=0
        )
        service = RuntimeService(
            session, accounts_service=accounts, market=market, trading=trading
        )
        yield service, accounts, trading, account, market


def make_instance(service, account_id, key):
    return service.create(
        account_id=account_id,
        strategy_key=key,
        symbol="BTCUSDT",
        timeframe="1h",
        params={},
        position_pct=0.5,
        max_order_notional=5_000,
        start=True,
    )


def test_entry_executes_once_per_bar(env):
    service, accounts, trading, account, _ = env
    view = make_instance(service, account.id, "test_buy_now")
    row = service._require(view.id)
    assert service.evaluate_instance(row) == "executed"
    assert service.market.closed_only_seen is True
    positions = trading.positions(account.id)
    assert len(positions) == 1
    assert positions[0].quantity == pytest.approx(50.0)
    assert accounts.cash(account.id) == pytest.approx(5_000)

    # Same bar again must not re-enter.
    assert service.evaluate_instance(row) == "no_new_bar"
    assert len(trading.positions(account.id)) == 1


def test_sell_signal_closes_position(env):
    service, _, trading, account, _ = env
    make_instance(service, account.id, "test_buy_now")
    row = service.running_instances()[0]
    service.evaluate_instance(row)

    row.strategy_key = "test_sell_now"
    row.last_bar_time = 0
    service.session.flush()
    assert service.evaluate_instance(row) == "executed"
    assert trading.positions(account.id) == []


def test_logs_and_validation(env):
    service, _, _, account, _ = env
    view = make_instance(service, account.id, "test_buy_now")
    logs = service.logs(view.id)
    assert any(log.event == "created" for log in logs)

    with pytest.raises(Exception):
        service.create(
            account_id=account.id, strategy_key="nope", symbol="BTCUSDT",
            timeframe="1h", params={}, position_pct=0.2, max_order_notional=1000,
        )
    with pytest.raises(Exception):
        service.create(
            account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
            timeframe="1h", params={}, position_pct=2.0, max_order_notional=1000,
        )


def test_delete_requires_stop(env):
    service, _, _, account, _ = env
    view = make_instance(service, account.id, "test_buy_now")
    with pytest.raises(Exception):
        service.delete(view.id)
    service.set_running(view.id, False)
    service.delete(view.id)
    assert service.list(account_id=account.id) == []


def test_sizing_explains_notional_cap_not_cash(env):
    """A notional cap below one board lot must not be reported as missing cash."""
    service, accounts, trading, account, _ = env
    accounts.deposit(account.id, 900_000)  # plenty of cash
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=1.0,
        max_order_notional=50, start=True,  # one lot (100 x 100) = 10,000
    )
    row = service._require(view.id)
    assert service.evaluate_instance(row) == "executed"
    logs = service.logs(view.id)
    skipped = [log for log in logs if log.event == "skipped"]
    assert skipped, "expected a skip log"
    message = skipped[0].message
    assert "max_order_notional" in message or "单笔上限" in message
    assert "可用资金" not in message.split("；")[0] or "超过单笔上限" in message
    assert trading.positions(account.id) == []


def test_protective_exit_closes_position(env):
    """The runner must honour stop rules — the backtest always did."""
    service, accounts, trading, account, market = env
    accounts.deposit(account.id, 900_000)
    view = service.create(
        account_id=account.id,
        strategy_key="test_buy_now",
        symbol="BTCUSDT",
        timeframe="1h",
        params={},
        position_pct=0.5,
        max_order_notional=50_000,
        stop_loss_pct=10,       # percent form, normalised to 0.10
        start=True,
    )
    row = service._require(view.id)
    assert service.evaluate_instance(row) == "executed"
    positions = trading.positions(account.id)
    assert positions and positions[0].quantity > 0
    entry = positions[0].avg_price
    assert row.stop_loss_pct == pytest.approx(0.10)

    # Nudge the fake market below the stop and run the protective sweep.
    market.current = entry * 0.85
    outcomes = service.protect_positions()
    assert outcomes.get(row.id) == "stop_loss"

    assert trading.positions(account.id) == []
    logs = service.logs(view.id)
    assert any(log.event == "stop_loss" for log in logs)
    assert service._require(view.id).peak_price == 0.0


def test_protective_sweep_ignores_instances_without_rules(env):
    service, accounts, _, account, market = env
    accounts.deposit(account.id, 900_000)
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.5, max_order_notional=50_000, start=True,
    )
    row = service._require(view.id)
    service.evaluate_instance(row)
    market.current = row_last_price(service, row) * 0.5
    outcomes = service.protect_positions()
    assert outcomes == {}


def row_last_price(service, row) -> float:
    return service.market.last_price(row.symbol)


def test_trailing_stop_ratchets_peak(env):
    service, accounts, trading, account, market = env
    accounts.deposit(account.id, 900_000)
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.5, max_order_notional=50_000,
        trailing_stop_pct=10, start=True,
    )
    row = service._require(view.id)
    service.evaluate_instance(row)
    entry = trading.positions(account.id)[0].avg_price

    # Price advances 20%: peak ratchets up, position stays open.
    market.current = entry * 1.20
    outcomes = service.protect_positions()
    assert outcomes == {}
    assert row.peak_price == pytest.approx(entry * 1.20, rel=1e-6)

    # A 10% pullback from the peak (108 when entry=100) must exit.
    market.current = entry * 1.06
    outcomes = service.protect_positions()
    assert outcomes.get(row.id) == "trailing_stop"
    assert trading.positions(account.id) == []


def test_price_failure_is_reported_not_silent(env):
    """A broken price feed must surface, not silently skip protection."""
    service, accounts, _, account, market = env
    accounts.deposit(account.id, 900_000)
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.5, max_order_notional=50_000,
        stop_loss_pct=5, start=True,
    )
    service.evaluate_instance(service._require(view.id))

    def broken(_symbol):
        raise RuntimeError("feed offline")

    market.last_price = broken
    outcomes = service.protect_positions()
    assert outcomes.get(view.id) == "price_unavailable"
    logs = service.logs(view.id)
    assert any(log.event == "price_unavailable" for log in logs)
    assert "feed offline" in service._require(view.id).last_error


def test_update_normalises_stops_and_rejects_bad_params(env):
    """Editing an instance must go through the same validation as creation."""
    from app.core.errors import ValidationError

    service, accounts, _, account, _ = env
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.2, max_order_notional=5_000,
    )
    updated = service.update(
        view.id, stop_loss_pct=5, take_profit_pct=12, trailing_stop_pct=3,
        position_pct=0.35, max_order_notional=8_000,
    )
    # Percent form (5) is normalised to a fraction (0.05), same as create().
    assert updated.stop_loss_pct == pytest.approx(0.05)
    assert updated.take_profit_pct == pytest.approx(0.12)
    assert updated.trailing_stop_pct == pytest.approx(0.03)
    assert updated.position_pct == pytest.approx(0.35)
    assert updated.max_order_notional == pytest.approx(8_000)

    with pytest.raises(ValidationError):
        service.update(view.id, position_pct=1.5)
    with pytest.raises(ValidationError):
        service.update(view.id, max_order_notional=0)
    # Invalid parameter sets must not silently stick to the row.
    assert service._require(view.id).position_pct == pytest.approx(0.35)


def test_adding_a_stop_to_a_live_instance_protects_the_open_position(env):
    """Editing protection must apply to a position that is already open."""
    service, accounts, trading, account, market = env
    accounts.deposit(account.id, 900_000)
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.5, max_order_notional=50_000,
        start=True,
    )
    service.evaluate_instance(service._require(view.id))
    position = trading.positions(account.id)[0]
    entry = position.avg_price

    # No rules yet: a 15% drop must leave the position alone.
    market.current = entry * 0.85
    assert service.protect_positions() == {}

    service.update(view.id, stop_loss_pct=10)
    outcomes = service.protect_positions()
    assert outcomes.get(view.id) == "stop_loss"
    assert trading.positions(account.id) == []


def test_repeated_warning_is_logged_once_per_window(env):
    """A persistent condition must not write one row per scheduler tick.

    The scheduler runs every few seconds. "insufficient data" or a dead price
    feed used to append a fresh row each tick — ~14k rows/day for one unresolved
    warning, growing the database for the life of the deployment.
    """
    service, accounts, _, account, market = env
    accounts.deposit(account.id, 900_000)
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.2, max_order_notional=5_000,
        start=True,
    )
    row = service._require(view.id)

    # Force the insufficient-data path: no candles at all.
    market.candles = lambda *a, **k: []
    for _ in range(5):
        assert service.evaluate_instance(row) == "insufficient_data"

    warnings = [log for log in service.logs(view.id) if log.event == "insufficient_data"]
    assert len(warnings) == 1, f"expected one row, got {len(warnings)}"


def test_warning_reappears_after_the_dedupe_window(env, monkeypatch):
    service, accounts, _, account, _market = env
    accounts.deposit(account.id, 900_000)
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.2, max_order_notional=5_000,
        start=True,
    )
    row = service._require(view.id)
    service._log(view.id, "warn", "feed_down", "no price", bar_time=1)
    service._log(view.id, "warn", "feed_down", "no price", bar_time=2)
    # autoflush is off, so a pending row is invisible to a following query.
    service.session.commit()
    assert len([l for l in service.logs(view.id) if l.event == "feed_down"]) == 1

    from datetime import timedelta

    import app.modules.runtime.service as runtime_service

    later = runtime_service.utcnow() + timedelta(
        seconds=runtime_service.LOG_DEDUPE_SECONDS + 1
    )
    monkeypatch.setattr(runtime_service, "utcnow", lambda: later)
    service._log(view.id, "warn", "feed_down", "no price", bar_time=3)
    service.session.commit()
    assert len([l for l in service.logs(view.id) if l.event == "feed_down"]) == 2


def test_state_changes_are_not_deduped(env):
    """Toggling an instance twice is user intent, not a repeated warning."""
    service, accounts, _, account, _market = env
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.2, max_order_notional=5_000,
    )
    service.set_running(view.id, True)
    service.set_running(view.id, False)
    service.set_running(view.id, True)
    started = [l for l in service.logs(view.id) if l.event == "started"]
    assert len(started) == 2


def test_log_pruning_keeps_the_newest_rows(env):
    service, accounts, _, account, _market = env
    view = service.create(
        account_id=account.id, strategy_key="test_buy_now", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.2, max_order_notional=5_000,
    )
    for index in range(30):
        service._log(view.id, "info", "note", f"row {index}")
    service.session.commit()

    removed = service.prune_logs(view.id, keep=10)
    assert removed == 21  # 30 notes + 1 "created"
    remaining = service.logs(view.id, limit=200)
    assert len(remaining) == 10
    assert remaining[0].message == "row 29"  # newest first
