"""Live-order correctness under partial fills, missing prices and missing ids.

These cover the failure modes that leave a real position at the broker with a
wrong or missing local record — the cases where the platform looks healthy while
the books disagree with the exchange.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.alerts.service import AlertService
from app.modules.brokers.base import BrokerAccount, BrokerOrderResult
from app.modules.brokers.binance import BinanceBroker
from app.modules.brokers.reconciler import LiveReconciler
from app.modules.market.service_types import SymbolView
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.models import CashHold, Fill, Order, Position
from app.modules.trading.service import TradingService


class Prices:
    def price(self, _symbol):
        return 100.0

    def symbol(self, code):
        return SymbolView(
            code=code, name=code, asset_class="crypto", base=code, quote="USDT",
            tick_size=0.01, lot_size=0.001, initial_price=100.0, enabled=True,
            market="crypto",
        )


class RestingBroker:
    """Accepts orders and reports broker state only when told to."""

    name = "binance"

    def __init__(self) -> None:
        self.filled = 0.0
        self.fill_price = 0.0
        self.quote_quantity: float | None = None
        self.status = "open"

    def verify_credentials(self):
        return BrokerAccount(can_trade=True, balances={"USDT": 1_000_000.0})

    def place_order(self, request):
        return BrokerOrderResult("B-1", "open", 0.0, 0.0, "NEW")

    def cancel_order(self, symbol, broker_order_id):
        return None

    def open_orders(self, symbol):
        return []

    def order_status(self, broker_order_id, *, symbol=""):
        return BrokerOrderResult(
            broker_order_id,
            self.status,
            self.filled,
            self.fill_price,
            "FILLED" if self.status == "filled" else "NEW",
            quote_quantity=self.quote_quantity,
        )

    def balances(self):
        return {"USDT": 99_900.0}


def _settings(tmp_path, name: str = "impl.db", **overrides) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / name).as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        live_trading_enabled=True,
        fee_bps=0.0,
        cn_commission_bps=0.0,
        cn_min_commission=0.0,
        cn_stamp_duty_bps=0.0,
        cn_transfer_fee_bps=0.0,
        **overrides,
    )


def _stack(tmp_path, broker=None, **overrides):
    settings = _settings(tmp_path, **overrides)
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="Live", kind="live", market="crypto",
        initial_balance=100_000, broker="binance",
    )
    trading = TradingService(
        session, cash=AccountCashAdapter(accounts), prices=Prices(),
        fee_bps=0.0, slippage_bps=0.0, broker=broker,
    )
    return db, session, accounts, account, trading


# --------------------------------------------------------------- exchange info
def test_exchange_info_parameters_go_in_the_query_string():
    """A GET body is ignored by the exchange, so the filter was never applied.

    `/exchangeInfo?symbol=..` sent as a body returned the whole catalog, and
    callers taking the first entry applied a different pair's lot step.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"symbols": []})

    broker = BinanceBroker(
        api_key="k", api_secret="s",
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )
    broker._symbol_rules("BTCUSDT")
    assert "symbol=BTCUSDT" in seen["url"]
    assert seen["body"] == ""
    broker.close()


def test_symbol_rules_match_the_requested_symbol_not_the_first_entry():
    """A server that ignores the filter must not leak another pair's step."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "ETHBTC",
                        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00100000"}],
                    },
                    {
                        "symbol": "BTCUSDT",
                        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000"}],
                    },
                ]
            },
        )

    broker = BinanceBroker(
        api_key="k", api_secret="s",
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )
    assert broker.normalize_quantity("BTCUSDT", 0.123456789) == pytest.approx(0.12345)
    broker.close()


# ------------------------------------------------------------ acknowledgements
def test_a_broker_ack_without_an_order_id_is_refused(tmp_path):
    """Without an id the order can be neither polled nor safely filled.

    `match_open_orders` would otherwise treat the row as a paper order and fill
    it against our own feed, inventing a trade the broker never made.
    """

    class NoIdBroker(RestingBroker):
        def place_order(self, request):
            return BrokerOrderResult("", "open", 0.0, 0.0, "NEW")

    broker = NoIdBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    from app.core.errors import AppError

    with pytest.raises(AppError):
        trading.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy",
            order_type="market", quantity=1,
        )
    stored = session.scalar(select(Order))
    assert stored.status == "rejected"
    assert "order id" in stored.reject_reason
    assert session.scalar(select(Position)) is None
    session.close()
    db.dispose()


def test_match_open_orders_never_fills_a_live_order(tmp_path):
    """The local matcher must not touch anything the broker owns."""
    broker = RestingBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    order = trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="market", quantity=1,
    )
    # Worst case: a live row that lost its broker id.
    row = session.get(Order, order.id)
    row.broker_order_id = ""
    row.status = "open"
    session.commit()

    assert trading.match_open_orders() == 0
    session.expire_all()
    assert session.get(Order, order.id).filled_quantity == pytest.approx(0.0)
    assert session.scalar(select(Position)) is None
    session.close()
    db.dispose()


# ------------------------------------------------------------------ reconcile
def test_a_cancel_without_a_fill_price_is_left_pollable(tmp_path):
    """A cancelled order reporting quantity but no price must not be closed.

    Gateways commonly answer a cancel with the cumulative quantity and no price.
    Writing `cancelled` then dropped that fill for good, because the order leaves
    the polled set — while the broker really did trade it.
    """
    broker = RestingBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    reconciler = LiveReconciler(db, _settings(tmp_path), broker=broker)

    broker.filled, broker.fill_price, broker.status = 0.4, 0.0, "cancelled"
    reconciler.tick()

    session.expire_all()
    stored = session.scalar(select(Order))
    assert stored.status == "open", "a fill with no price must keep the order open"
    assert "未提供成交价" in stored.reject_reason
    assert session.scalar(select(Position)) is None

    # Once a price arrives, the fill is booked and the order can close.
    broker.fill_price = 95.0
    reconciler.tick()
    session.expire_all()
    stored = session.scalar(select(Order))
    assert stored.filled_quantity == pytest.approx(0.4)
    assert stored.status == "cancelled"
    assert session.scalar(select(Position)).quantity == pytest.approx(0.4)
    session.close()
    db.dispose()


def test_partial_fills_are_priced_at_their_own_increment(tmp_path):
    """A cumulative average misprices each later slice.

    first:  0.5 filled, 50 quote   -> avg 100
    second: 1.0 filled, 110 quote  -> avg 110

    Booking the second slice at 110 spends 105 in total when the exchange
    actually took 110; the 5 never appears in the ledger again.
    """
    broker = RestingBroker()
    db, session, accounts, account, trading = _stack(tmp_path, broker)
    trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=120,
    )
    start_cash = accounts.cash(account.id)
    reconciler = LiveReconciler(db, _settings(tmp_path), broker=broker)

    broker.filled, broker.fill_price, broker.status = 0.5, 100.0, "partially_filled"
    broker.quote_quantity = 50.0
    reconciler.tick()

    broker.filled, broker.fill_price, broker.status = 1.0, 110.0, "filled"
    broker.quote_quantity = 110.0
    reconciler.tick()

    assert accounts.cash(account.id) == pytest.approx(start_cash - 110.0)
    session.expire_all()
    fills = session.scalars(select(Fill).order_by(Fill.created_at)).all()
    assert [round(f.price, 4) for f in fills] == [100.0, 120.0]
    # The order's own average stays cumulative: the next delta depends on it.
    assert session.scalar(select(Order)).avg_fill_price == pytest.approx(110.0)
    session.close()
    db.dispose()


def test_a_share_live_sell_proceeds_are_held_until_the_next_session(tmp_path):
    """The reconciler path must apply T+1 too.

    Live A-share orders almost always fill asynchronously, so this is the normal
    path — without the hold, sale proceeds became spendable the same session.
    """
    settings = _settings(tmp_path, name="cn_live.db")
    settings = Settings(
        database_url=settings.database_url,
        seed_market=False, cn_symbols_enabled=False, runtime_enabled=False,
        live_trading_enabled=True,
        cn_commission_bps=0.0, cn_min_commission=0.0,
        cn_stamp_duty_bps=0.0, cn_transfer_fee_bps=0.0,
    )
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="CN Live", kind="live", market="cn_stock",
        initial_balance=100_000, broker="ths",
    )
    session.add(
        Position(
            id="p-cn", account_id=account.id, symbol="600036.SH",
            quantity=1000, available_quantity=1000, avg_price=10.0, realized_pnl=0.0,
        )
    )
    session.add(
        Order(
            id="o-cn", account_id=account.id, symbol="600036.SH", side="sell",
            order_type="market", quantity=1000, status="open",
            source="live:manual-close", broker_order_id="B-CN",
        )
    )
    session.commit()

    class CnGateway(RestingBroker):
        name = "ths"

        def order_status(self, broker_order_id, *, symbol=""):
            return BrokerOrderResult(broker_order_id, "filled", 1000.0, 11.0, "FILLED")

    LiveReconciler(db, settings, gateway=CnGateway()).tick()

    session.expire_all()
    holds = session.scalars(select(CashHold)).all()
    assert len(holds) == 1
    assert holds[0].amount == pytest.approx(11_000.0)
    session.close()
    db.dispose()


def test_an_orphaned_order_is_reported_once(tmp_path, monkeypatch):
    """A channel that disappears must not leave orders silently unpolled."""
    broker = RestingBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )

    reconciler = LiveReconciler(db, _settings(tmp_path))
    # Neither channel configured any more. Patch the properties on the *instance*
    # class via monkeypatch so the change is undone for later tests: assigning to
    # the class directly leaked into every subsequent reconciler.
    monkeypatch.setattr(
        type(reconciler), "gateway", property(lambda self: None)
    )
    monkeypatch.setattr(
        type(reconciler), "broker", property(lambda self: None)
    )
    for _ in range(4):
        reconciler.tick()

    alerts = [
        a for a in AlertService(session).list(category="gateway_failure")
        if a.ref_id == "binance"
    ]
    assert len(alerts) == 1
    assert "通道已不可用" in alerts[0].title
    session.close()
    db.dispose()
