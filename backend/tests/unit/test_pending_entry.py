"""Entry-path stability when the gateway fills asynchronously.

A live buy stays `open` until the exchange fills it, so the position reads 0 for
several bars. Evaluating each new bar against "position <= 0" produced one entry
per bar, all working at once.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.brokers.base import BrokerAccount, BrokerOrderResult
from app.modules.market.service_types import CandlePoint, SymbolView
from app.modules.runtime.service import RuntimeService
from app.modules.strategies.base import Signal
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.models import Order
from app.modules.trading.service import TradingService
from sqlalchemy import select


class PendingGateway:
    name = "pending-mock"

    def __init__(self) -> None:
        self.placed = 0

    def verify_credentials(self) -> BrokerAccount:
        return BrokerAccount(can_trade=True, balances={"USDT": 1_000_000.0})

    def place_order(self, request) -> BrokerOrderResult:
        self.placed += 1
        return BrokerOrderResult(
            broker_order_id=f"P{self.placed}",
            status="open",
            filled_quantity=0.0,
            avg_price=0.0,
            raw_status="accepted",
        )

    def cancel_order(self, symbol: str, broker_order_id: str) -> None:
        return None

    def order_status(self, broker_order_id: str) -> BrokerOrderResult:
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status="open",
            filled_quantity=0.0,
            avg_price=0.0,
            raw_status="accepted",
        )

    def open_orders(self, symbol: str) -> list[dict]:
        return []

    def positions(self) -> list:
        return []

    def trades_since(self, since_ms: int) -> list[dict]:
        return []


class Market:
    def __init__(self) -> None:
        self.now = 1_700_000_000_000

    def last_price(self, symbol: str) -> float:
        return 100.0

    def supported_timeframes(self, market: str = "crypto"):
        return ("15m", "30m", "1h", "4h", "1d")

    def require_symbol(self, code: str) -> SymbolView:
        return SymbolView(
            code=code, name=code, asset_class="crypto", base=code, quote="USDT",
            tick_size=0.01, lot_size=0.01, initial_price=100.0, enabled=True,
            market="crypto",
        )

    def candles(self, symbol, *, timeframe="1h", limit=600, closed_only=False):
        return [
            CandlePoint(
                time=self.now + index * 3_600_000,
                open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0,
            )
            for index in range(200)
        ]


class Params:
    def model_dump(self) -> dict:
        return {}


class AlwaysBuy:
    key = "always_buy"
    name = "always buy"
    category = "test"

    def validate_params(self, params) -> Params:
        return Params()

    def warmup(self, params) -> int:
        return 1

    def prepare(self, candles, params):
        return None

    def evaluate(self, state, index) -> Signal:
        return Signal(action="buy", reason="test", strength=1.0)


def _stack(tmp_path, monkeypatch):
    import app.modules.runtime.service as runtime_module

    monkeypatch.setattr(runtime_module, "get", lambda key: AlwaysBuy())

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'entry.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        live_trading_enabled=True,
        live_auto_trading_enabled=True,
    )
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    market = Market()
    gateway = PendingGateway()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(name="live", initial_balance=100_000, kind="live", market="crypto")
    session.commit()
    trading = TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=MarketPriceAdapter(market),
        fee_bps=0,
        slippage_bps=0,
        broker=gateway,
    )
    service = RuntimeService(
        session,
        accounts_service=accounts,
        market=market,
        trading=trading,
        live_auto_trading_enabled=True,
    )
    view = service.create(
        account_id=account.id, strategy_key="always_buy", symbol="BTCUSDT",
        timeframe="1h", params={}, position_pct=0.5, max_order_notional=50_000, start=True,
    )
    return db, session, service, view, market, gateway


def test_a_pending_entry_is_not_duplicated_on_the_next_bar(tmp_path, monkeypatch):
    """One configured position must not become two working buys.

    The gateway keeps the order open, so `position` stays 0 and every new bar
    passed the "no position yet" check. Measured before the fix: 500 shares and
    then 250 shares both in flight — double the intended exposure if both filled.
    """
    db, session, service, view, market, gateway = _stack(tmp_path, monkeypatch)

    assert service.evaluate_instance(service._require(view.id)) == "executed"
    for _ in range(3):
        market.now += 3_600_000
        service.evaluate_instance(service._require(view.id))

    assert gateway.placed == 1, "a pending entry must not be duplicated"
    orders = session.scalars(select(Order).where(Order.side == "buy")).all()
    assert len(orders) == 1
    assert orders[0].status in {"new", "open", "partially_filled"}
    session.close()
    db.dispose()


def test_a_filled_entry_is_not_reopened_while_the_position_exists(tmp_path, monkeypatch):
    """Once a position exists the entry guard is the position itself."""
    from app.modules.market.service_types import CandlePoint  # noqa: F401
    from app.modules.trading.models import Position

    db, session, service, view, market, gateway = _stack(tmp_path, monkeypatch)
    service.evaluate_instance(service._require(view.id))

    # Simulate the gateway filling the order.
    row = service._require(view.id)
    from app.modules.trading.models import Order as OrderRow

    order = session.scalar(select(OrderRow).where(OrderRow.side == "buy"))
    order.status = "filled"
    order.filled_quantity = order.quantity
    session.add(
        Position(
            account_id=row.account_id, symbol="BTCUSDT",
            quantity=order.quantity, available_quantity=order.quantity, avg_price=100.0,
        )
    )
    session.commit()

    market.now += 3_600_000
    service.evaluate_instance(service._require(view.id))
    assert gateway.placed == 1
    session.close()
    db.dispose()
