"""Crypto live trading: submit, rest, reconcile, cancel.

Binance answers most limit orders with `NEW` — the order rests at the exchange
and the platform learns about the fill later. Only the A-share gateway used to be
polled, so a crypto limit order stayed `open` for ever: its cash stayed reserved,
the position never appeared, and cancelling was the only way out.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.db import Database
from app.modules.accounts.service import AccountService
from app.modules.brokers.base import BrokerAccount, BrokerOrderResult
from app.modules.brokers.binance import BinanceBroker
from app.modules.brokers.reconciler import LiveReconciler
from app.modules.market.service_types import SymbolView
from app.modules.trading.adapters import AccountCashAdapter
from app.modules.trading.models import Order, Position
from app.modules.trading.service import TradingService
from sqlalchemy import select


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
    """Accepts limit orders and reports the fill only when told to."""

    name = "binance"

    def __init__(self) -> None:
        self.filled = 0.0
        self.fill_price = 0.0
        self.status = "open"
        self.polls: list[str] = []

    def verify_credentials(self):
        return BrokerAccount(can_trade=True, balances={"USDT": 1_000_000.0})

    def place_order(self, request):
        return BrokerOrderResult("B-1", "open", 0.0, 0.0, "NEW")

    def cancel_order(self, symbol, broker_order_id):
        return None

    def open_orders(self, symbol):
        return []

    def order_status(self, broker_order_id, *, symbol=""):
        self.polls.append(symbol or "<none>")
        return BrokerOrderResult(
            broker_order_id,
            self.status,
            self.filled,
            self.fill_price,
            "FILLED" if self.status == "filled" else "NEW",
        )

    def balances(self):
        return {"USDT": 99_900.0}


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'crypto_live.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        live_trading_enabled=True,
        fee_bps=0.0,
        **overrides,
    )


def _stack(tmp_path, broker=None, **overrides):
    settings = _settings(tmp_path, **overrides)
    db = Database(settings.database_url)
    db.create_schema()
    session = db.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="Crypto Live", kind="live", market="crypto",
        initial_balance=100_000, broker="binance",
    )
    trading = TradingService(
        session, cash=AccountCashAdapter(accounts), prices=Prices(),
        fee_bps=0.0, slippage_bps=0.0, broker=broker,
    )
    return db, session, accounts, account, trading


def test_a_resting_crypto_order_is_reconciled_to_filled(tmp_path):
    """The whole point: a live crypto order must reach `filled` without cancel."""
    broker = RestingBroker()
    db, session, accounts, account, trading = _stack(tmp_path, broker)
    order = trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    assert order.status == "open"
    # The cash is committed while the order rests.
    assert trading.reserved_cash(account.id) == pytest.approx(95.0)

    reconciler = LiveReconciler(db, _settings(tmp_path), broker=broker)
    assert reconciler.tick()["filled"] == 0

    broker.filled, broker.fill_price, broker.status = 1.0, 99.0, "filled"
    assert reconciler.tick()["filled"] == 1

    session.expire_all()
    stored = session.scalar(select(Order))
    assert stored.status == "filled"
    assert stored.filled_quantity == pytest.approx(1.0)
    assert stored.avg_fill_price == pytest.approx(99.0)
    position = session.scalar(select(Position))
    assert position.quantity == pytest.approx(1.0)
    # Crypto is instantly sellable — no T+1 in this market.
    assert position.available_quantity == pytest.approx(1.0)
    # And the reservation is gone now that the order is final.
    assert trading.reserved_cash(account.id) == pytest.approx(0.0)

    # Re-polling must not book the fill twice.
    assert reconciler.tick()["filled"] == 0
    session.expire_all()
    assert session.scalar(select(Position)).quantity == pytest.approx(1.0)
    session.close()
    db.dispose()


def test_the_reconciler_passes_the_symbol_to_the_crypto_client(tmp_path):
    """Binance scopes `order_status` by symbol; the reconciler must supply it."""
    broker = RestingBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    LiveReconciler(db, _settings(tmp_path), broker=broker).tick()
    assert broker.polls == ["BTCUSDT"]
    session.close()
    db.dispose()


def test_a_partial_fill_books_only_the_delta(tmp_path):
    broker = RestingBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=2, limit_price=95,
    )
    reconciler = LiveReconciler(db, _settings(tmp_path), broker=broker)

    broker.filled, broker.fill_price, broker.status = 0.5, 95.0, "partially_filled"
    assert reconciler.tick()["filled"] == 1
    session.expire_all()
    assert session.scalar(select(Position)).quantity == pytest.approx(0.5)
    assert session.scalar(select(Order)).status == "partially_filled"

    broker.filled, broker.fill_price, broker.status = 2.0, 96.0, "filled"
    assert reconciler.tick()["filled"] == 1
    session.expire_all()
    assert session.scalar(select(Position)).quantity == pytest.approx(2.0)
    assert session.scalar(select(Order)).status == "filled"
    session.close()
    db.dispose()


def test_a_rejected_crypto_order_is_final(tmp_path):
    broker = RestingBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )
    broker.status = "rejected"
    assert LiveReconciler(db, _settings(tmp_path), broker=broker).tick()["rejected"] == 1
    session.expire_all()
    stored = session.scalar(select(Order))
    assert stored.status == "rejected"
    assert stored.reject_reason
    session.close()
    db.dispose()


def test_repeated_poll_failures_raise_one_alert(tmp_path):
    """A dead broker channel must not look like quiet bookkeeping."""
    from app.modules.alerts.service import AlertService

    broker = RestingBroker()
    db, session, _accounts, account, trading = _stack(tmp_path, broker)
    trading.place_order(
        account_id=account.id, symbol="BTCUSDT", side="buy",
        order_type="limit", quantity=1, limit_price=95,
    )

    def boom(_id, *, symbol=""):
        raise httpx.ConnectError("exchange unreachable")

    broker.order_status = boom
    reconciler = LiveReconciler(db, _settings(tmp_path), broker=broker)
    for _ in range(5):
        assert reconciler.tick()["polled"] == 1

    alerts = AlertService(session).list(category="gateway_failure")
    matching = [a for a in alerts if a.ref_id == "binance"]
    # Fires at the third consecutive failure and then stays quiet (the alert
    # service also dedupes within its own window).
    assert 1 <= len(matching) <= 2
    assert "成交回报中断" in matching[0].title
    session.close()
    db.dispose()


def test_binance_quantity_is_floored_to_the_symbol_step():
    """An off-step quantity is rejected by the exchange (-1111), not rounded up."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.00001",
                                    "minQty": "0.00001",
                                },
                                {"filterType": "NOTIONAL", "minNotional": "5"},
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={})

    broker = BinanceBroker(
        api_key="k", api_secret="s",
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )
    # 0.123456789 -> floor to 5 decimals, never up to 0.12346.
    assert broker.normalize_quantity("BTCUSDT", 0.123456789) == pytest.approx(0.12345)
    assert broker._format_quantity("BTCUSDT", 0.12345) == "0.12345"
    broker.close()


def test_binance_order_status_requires_the_symbol():
    """Refusing to guess beats a lookup that is guaranteed to fail."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    broker = BinanceBroker(
        api_key="k", api_secret="s",
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )
    from app.core.errors import AppError

    with pytest.raises(AppError):
        broker.order_status("123")
    broker.close()


def test_binance_order_status_parses_a_resting_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/order"
        return httpx.Response(
            200,
            json={
                "orderId": 4242,
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.5",
                "cummulativeQuoteQty": "4750.0",
            },
        )

    broker = BinanceBroker(
        api_key="k", api_secret="s",
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )
    result = broker.order_status("4242", symbol="BTCUSDT")
    assert result.status == "partially_filled"
    assert result.filled_quantity == pytest.approx(0.5)
    assert result.avg_price == pytest.approx(9500.0)
    broker.close()


def test_binance_no_longer_opens_a_client_per_request():
    """The reconciler polls continuously; a TLS handshake per poll is waste."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={"orderId": 1, "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"},
        )

    broker = BinanceBroker(
        api_key="k", api_secret="s",
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )
    first = broker._client()
    broker.order_status("1", symbol="BTCUSDT")
    broker.order_status("1", symbol="BTCUSDT")
    assert broker._client() is first
    assert len(calls) == 2
    broker.close()
    assert broker._http is None
