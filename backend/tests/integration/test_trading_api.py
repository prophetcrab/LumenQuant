from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def trading_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'trading_api.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=260,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def create_account(client, balance=100_000):
    response = client.post("/api/accounts", json={"name": f"Desk {balance}", "initial_balance": balance})
    assert response.status_code == 201, response.text
    return response.json()


def test_place_market_order_updates_positions(trading_client):
    account = create_account(trading_client)
    response = trading_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.05,
        },
    )
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["status"] == "filled"
    assert order["filled_quantity"] == 0.05

    positions = trading_client.get(
        "/api/trading/positions", params={"account_id": account["id"]}
    ).json()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTCUSDT"
    assert positions[0]["quantity"] == 0.05
    assert positions[0]["unrealized_pnl"] is not None

    account_after = trading_client.get(f"/api/accounts/{account['id']}").json()
    assert account_after["cash_balance"] < account["cash_balance"]

    fills = trading_client.get(
        "/api/trading/fills", params={"account_id": account["id"]}
    ).json()
    assert len(fills) == 1
    assert fills[0]["quantity"] == 0.05


def test_limit_order_lifecycle(trading_client):
    account = create_account(trading_client, 50_000)
    btc = trading_client.get(
        "/api/market/snapshot", params={"symbol": "BTCUSDT"}
    ).json()["price"]
    response = trading_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "limit",
            "quantity": 0.01,
            "limit_price": round(btc * 0.5, 2),
        },
    )
    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "open"

    orders = trading_client.get(
        "/api/trading/orders", params={"account_id": account["id"], "status": "open"}
    ).json()
    assert len(orders) == 1

    cancelled = trading_client.delete(
        f"/api/trading/orders/{order['id']}", params={"account_id": account["id"]}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_guards(trading_client):
    account = create_account(trading_client, 1_000)
    too_big = trading_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 1,
        },
    )
    assert too_big.status_code == 409
    assert too_big.json()["error"]["code"] == "conflict"

    short = trading_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "sell",
            "order_type": "market",
            "quantity": 1,
        },
    )
    assert short.status_code == 409

    missing = trading_client.post(
        "/api/trading/orders",
        json={
            "account_id": "nope",
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.001,
        },
    )
    assert missing.status_code == 404
