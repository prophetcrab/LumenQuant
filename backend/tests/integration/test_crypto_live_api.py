"""Crypto live trading over the HTTP API.

These exercise the path an operator actually takes: create a live account, tell
the platform what the exchange holds, place an order that rests, and watch the
reconciler finish it. The broker is a MockTransport-backed Binance adapter, so
the request signing and response parsing are real code.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.brokers.binance import BinanceBroker


class ExchangeState:
    """Shared mutable state for the mocked Binance endpoints."""

    def __init__(self) -> None:
        self.filled = 0.0
        self.fill_price = 0.0
        self.status = "NEW"
        self.usdt = 100_000.0
        self.place_calls: list[dict] = []


@pytest.fixture()
def crypto_live(tmp_path):
    state = ExchangeState()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                {"filterType": "NOTIONAL", "minNotional": "5"},
                            ],
                        }
                    ]
                },
            )
        if path == "/api/v3/account":
            return httpx.Response(
                200,
                json={
                    "canTrade": True,
                    "balances": [
                        {"asset": "USDT", "free": f"{state.usdt:.8f}", "locked": "0"},
                    ],
                },
            )
        if path == "/api/v3/order" and request.method == "POST":
            # Signed requests carry their parameters in the query string.
            state.place_calls.append(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "orderId": 7001,
                    "status": "NEW",
                    "executedQty": "0.00000000",
                    "cummulativeQuoteQty": "0.00000000",
                },
            )
        if path == "/api/v3/order" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "orderId": 7001,
                    "status": state.status,
                    "executedQty": f"{state.filled:.8f}",
                    "cummulativeQuoteQty": f"{state.filled * state.fill_price:.8f}",
                },
            )
        if path == "/api/v3/order" and request.method == "DELETE":
            return httpx.Response(200, json={"orderId": 7001, "status": "CANCELED"})
        return httpx.Response(404, json={"code": -1, "msg": path})

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'clive.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=300,
        market_tick_seconds=3600,
        runtime_enabled=False,
        live_trading_enabled=True,
        live_auto_trading_enabled=True,
        binance_api_key="key",
        binance_api_secret="secret",
        fee_bps=0.0,
        risk_max_orders_per_minute=1000,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    # Swap in a transport-backed adapter after startup so no network is touched.
    broker = BinanceBroker(
        api_key="key", api_secret="secret",
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        app.state.crypto_broker = broker
        if app.state.reconcile_engine is not None:
            app.state.reconcile_engine._broker = broker
        yield client, app, state
    broker.close()
    db.dispose()


def _live_crypto_account(client: TestClient, initial: float = 1.0) -> dict:
    response = client.post(
        "/api/accounts",
        json={
            "name": "币安实盘",
            "kind": "live",
            "market": "crypto",
            "initial_balance": initial,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_channel_status_reports_both_markets(crypto_live):
    client, _app, _state = crypto_live
    body = client.get("/api/brokers/status").json()
    assert body["live_trading_enabled"] is True
    assert body["channels"]["binance"]["ready"] is True
    assert body["channels"]["ths"]["ready"] is False
    # Fill polling is what makes a live order real; it must be reported apart
    # from "credentials configured".
    assert "reconciler_running" in body


def test_alignment_preview_changes_nothing(crypto_live):
    client, _app, state = crypto_live
    account = _live_crypto_account(client)

    preview = client.post(
        "/api/brokers/binance/align-cash", params={"account_id": account["id"]}
    ).json()
    assert preview["applied"] is False
    assert preview["local_cash"] == pytest.approx(1.0)
    assert preview["broker_cash"] == pytest.approx(state.usdt)
    # Nothing moved: this endpoint must not fund an account as a side effect.
    cash = client.get(f"/api/accounts/{account['id']}").json()["cash_balance"]
    assert cash == pytest.approx(1.0)


def test_alignment_lets_a_live_account_actually_trade(crypto_live):
    """A live account funded with the documented `initial_balance=1` cannot buy.

    Sizing reads the local book, so the platform must be told what the exchange
    holds before any real order has a chance.
    """
    client, _app, state = crypto_live
    account = _live_crypto_account(client)

    # Without alignment the order is refused on cash.
    blocked = client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"], "symbol": "BTCUSDT", "side": "buy",
            "order_type": "limit", "quantity": 0.01, "limit_price": 1000.0,
        },
        headers={"X-Live-Confirm": "true"},
    )
    assert blocked.status_code == 409, blocked.text
    assert "insufficient cash" in blocked.json()["error"]["message"]

    applied = client.post(
        "/api/brokers/binance/align-cash",
        params={"account_id": account["id"], "confirm": True},
        headers={"X-Live-Confirm": "true"},
    ).json()
    assert applied["applied"] is True
    assert applied["cash_after"] == pytest.approx(state.usdt)

    placed = client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"], "symbol": "BTCUSDT", "side": "buy",
            "order_type": "limit", "quantity": 0.25, "limit_price": 1000.0,
        },
        headers={"X-Live-Confirm": "true"},
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["status"] == "open"
    # The exchange received the order it will actually fill.
    sent = state.place_calls[-1]
    assert sent["symbol"] == "BTCUSDT"
    assert sent["quantity"] == "0.25"


def test_live_order_reaches_filled_through_the_reconciler(crypto_live):
    client, app, state = crypto_live
    account = _live_crypto_account(client)
    client.post(
        "/api/brokers/binance/align-cash",
        params={"account_id": account["id"], "confirm": True},
        headers={"X-Live-Confirm": "true"},
    )
    response = client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"], "symbol": "BTCUSDT", "side": "buy",
            "order_type": "limit", "quantity": 0.5, "limit_price": 1000.0,
        },
        headers={"X-Live-Confirm": "true"},
    )
    assert response.status_code == 201, response.text
    placed = response.json()
    assert placed["status"] == "open"

    # The background loop is off under test settings, so drive the same object
    # the scheduler would use.
    from app.modules.brokers.reconciler import LiveReconciler

    reconciler = LiveReconciler(
        Database(app.state.settings.database_url),
        app.state.settings,
        broker=app.state.crypto_broker,
    )

    # Nothing fills yet.
    assert reconciler.tick()["filled"] == 0
    orders = client.get("/api/trading/orders", params={"account_id": account["id"]}).json()
    assert orders[0]["status"] == "open"

    # The exchange fills it (partially, then fully) and the reconciler catches up.
    state.filled, state.fill_price, state.status = 0.2, 990.0, "PARTIALLY_FILLED"
    assert reconciler.tick()["filled"] == 1
    orders = client.get("/api/trading/orders", params={"account_id": account["id"]}).json()
    assert orders[0]["status"] == "partially_filled"
    assert orders[0]["filled_quantity"] == pytest.approx(0.2)

    state.filled, state.fill_price, state.status = 0.5, 995.0, "FILLED"
    assert reconciler.tick()["filled"] == 1
    orders = client.get("/api/trading/orders", params={"account_id": account["id"]}).json()
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_quantity"] == pytest.approx(0.5)

    positions = client.get(
        "/api/trading/positions", params={"account_id": account["id"]}
    ).json()
    assert positions[0]["symbol"] == "BTCUSDT"
    assert positions[0]["quantity"] == pytest.approx(0.5)
    # Crypto has no T+1: the position is immediately sellable.
    assert positions[0]["available_quantity"] == pytest.approx(0.5)

    fills = client.get("/api/trading/fills", params={"account_id": account["id"]}).json()
    assert len(fills) == 2  # one per incremental fill, not one per poll
    # A filled order no longer reserves cash.
    overview = client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    ).json()
    assert overview["available_cash"] == pytest.approx(
        overview["cash"], rel=0, abs=1e-6
    )


def test_reconciling_an_unsynced_account_reports_the_difference(crypto_live):
    """The comparison must compare both sides, not just what it can see."""
    client, _app, state = crypto_live
    account = _live_crypto_account(client)
    body = client.get(
        "/api/brokers/binance/reconcile", params={"account_id": account["id"]}
    ).json()
    assert body["gateway"] == "binance"
    assert body["cash_match"] is False
    assert body["all_match"] is False
    assert body["cash"]["local_cash"] == pytest.approx(1.0)
    assert body["cash"]["exchange_cash"] == pytest.approx(state.usdt)
