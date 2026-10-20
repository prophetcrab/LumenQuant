from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.errors import AppError
from app.modules.accounts.service import AccountService
from app.modules.brokers.base import BrokerOrderRequest
from app.modules.brokers.reconciler import LiveReconciler
from app.modules.brokers.ths import MockThsGateway, ThsHttpGateway
from app.modules.trading.service import TradingService


def test_http_gateway_contract():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/account":
            return httpx.Response(
                200,
                json={
                    "cash": 250_000.0,
                    "positions": [
                        {"symbol": "600519.SH", "quantity": 300, "available_quantity": 200, "avg_price": 1680.0}
                    ],
                },
            )
        if request.url.path == "/api/orders" and request.method == "POST":
            return httpx.Response(
                200, json={"order_id": "G-1", "status": "accepted", "filled_quantity": 0, "avg_price": 0}
            )
        if request.url.path == "/api/orders/G-1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "order_id": "G-1",
                    "status": "filled",
                    "filled_quantity": 200,
                    "avg_price": 10.5,
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    gateway = ThsHttpGateway(
        base_url="http://gateway.test", token="tok", transport=httpx.MockTransport(handler)
    )
    account = gateway.verify_credentials()
    assert account.can_trade is True
    assert account.balances["CNY"] == 250_000.0
    assert gateway.positions()[0].available_quantity == 200

    result = gateway.place_order(
        BrokerOrderRequest(symbol="600519.SH", side="buy", order_type="limit", quantity=200, limit_price=10.5)
    )
    assert result.status == "open"
    assert result.broker_order_id == "G-1"

    filled = gateway.order_status("G-1")
    assert filled.status == "filled"
    assert filled.filled_quantity == 200
    assert filled.avg_price == 10.5
    assert any(call[0] == "POST" and call[1] == "/api/orders" for call in calls)


def test_http_gateway_maps_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "broker offline"})

    gateway = ThsHttpGateway(
        base_url="http://gateway.test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(AppError) as exc:
        gateway.verify_credentials()
    assert "同花顺网关" in str(exc.value)


def test_mock_gateway_fills_market_order():
    gateway = MockThsGateway(price_feed=lambda symbol: 12.34)
    result = gateway.place_order(
        BrokerOrderRequest(symbol="600519.SH", side="buy", order_type="market", quantity=100)
    )
    assert result.status == "filled"
    assert result.filled_quantity == 100
    position = gateway.positions()[0]
    assert position.quantity == 100
    assert position.avg_price == pytest.approx(12.34)


def test_reconciler_books_gateway_fill_once(database):
    gateway = MockThsGateway(price_feed=lambda symbol: 10.0)
    session = database.session_factory()
    settings = type(
        "S",
        (),
        {
            "live_trading_enabled": True,
            "live_reconcile_seconds": 1.0,
            "cn_commission_bps": 2.5,
            "cn_min_commission": 5.0,
            "cn_stamp_duty_bps": 5.0,
            "cn_transfer_fee_bps": 0.1,
        },
    )()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(
        name="实盘A股", kind="live", initial_balance=100_000, market="cn_stock"
    )
    # Simulate an accepted (unfilled) order row the trading service wrote.
    from app.modules.trading.models import Order, new_id

    order = Order(
        id=new_id(),
        account_id=account.id,
        symbol="600519.SH",
        side="buy",
        order_type="market",
        quantity=200,
        status="open",
        source="live:manual",
        broker_order_id="MOCK-1",
    )
    session.add(order)
    session.commit()
    gateway._orders["MOCK-1"] = {
        "order_id": "MOCK-1",
        "symbol": "600519.SH",
        "side": "buy",
        "order_type": "market",
        "quantity": 200,
        "limit_price": None,
        "filled_quantity": 0.0,
        "avg_price": 0.0,
        "status": "accepted",
    }
    gateway.fill_order("MOCK-1", 200, 10.0)

    reconciler = LiveReconciler(database, settings, gateway=gateway)
    first = reconciler.tick()
    assert first["filled"] == 1
    positions = TradingService(
        session,
        cash=type("C", (), {
            "kind": lambda self, _id: "live",
            "market": lambda self, _id: "cn_stock",
            "cash": lambda self, _id: accounts.cash(_id),
            "post_cash": lambda self, _id, amount, kind, ref="", note="": accounts.post_cash(_id, amount=amount, kind=kind, ref=ref, note=note),
            "ensure_active": lambda self, _id: None,
        })(),
        prices=type("P", (), {
            "price": lambda self, _s: 10.0,
            "symbol": lambda self, code: __import__("app.modules.market.service_types", fromlist=["SymbolView"]).SymbolView(
                code=code, name=code, asset_class="stock", base=code, quote="CNY",
                tick_size=0.01, lot_size=100, initial_price=10.0, enabled=True,
                market="cn_stock", prev_close=10.0, limit_pct=0.10,
            ),
        })(),
    ).positions(account.id)
    assert positions[0].quantity == 200
    assert positions[0].available_quantity == 0  # T+1

    # Second poll must not double-book.
    second = reconciler.tick()
    assert second["filled"] == 0
    assert TradingService(
        session,
        cash=type("C", (), {
            "kind": lambda self, _id: "live",
            "market": lambda self, _id: "cn_stock",
            "cash": lambda self, _id: accounts.cash(_id),
            "post_cash": lambda self, _id, amount, kind, ref="", note="": accounts.post_cash(_id, amount=amount, kind=kind, ref=ref, note=note),
            "ensure_active": lambda self, _id: None,
        })(),
        prices=type("P", (), {
            "price": lambda self, _s: 10.0,
            "symbol": lambda self, code: __import__("app.modules.market.service_types", fromlist=["SymbolView"]).SymbolView(
                code=code, name=code, asset_class="stock", base=code, quote="CNY",
                tick_size=0.01, lot_size=100, initial_price=10.0, enabled=True,
                market="cn_stock", prev_close=10.0, limit_pct=0.10,
            ),
        })(),
    ).positions(account.id)[0].quantity == 200
    session.close()


def test_reconciler_releases_t1(database):
    from app.modules.trading.models import Position, new_id

    session = database.session_factory()
    accounts = AccountService(session, live_trading_enabled=True)
    account = accounts.create(name="T1", kind="paper", initial_balance=10_000, market="cn_stock")
    session.add(
        Position(
            id=new_id(), account_id=account.id, symbol="600519.SH",
            quantity=300, available_quantity=0, avg_price=10.0, realized_pnl=0.0,
        )
    )
    session.commit()
    reconciler = LiveReconciler(database, type("S", (), {"live_trading_enabled": False})())
    assert reconciler.unlock_t1() == 1
    session.refresh(session.query(Position).first())
    assert session.query(Position).first().available_quantity == 300
    session.close()
