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
