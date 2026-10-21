"""A-share sale proceeds settle T+1, matching the share-side T+1 rule."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'cash.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=60,
        market_seed_bars=200,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
        risk_max_orders_per_minute=1000,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def _account(client: TestClient) -> dict:
    return client.post(
        "/api/accounts",
        json={"name": "结算账户", "initial_balance": 200_000, "market": "cn_stock"},
    ).json()


def test_sale_proceeds_are_held_until_the_next_trading_day(client):
    """Selling credits the ledger but must not free the cash for new purchases.

    A-share cash settles T+1 just like shares. Before this, proceeds were
    immediately spendable, so a sell-then-buy sequence in one session used money
    the broker would not have released.
    """
    account = _account(client)
    code = client.get("/api/cn/symbols").json()[0]["code"]
    client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": code,
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
    )
    client.post("/api/trading/settle")  # next day: shares become sellable
    sell = client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": code,
            "side": "sell",
            "order_type": "market",
            "quantity": 100,
        },
    )
    assert sell.status_code == 201
    body = sell.json()
    # Net proceeds: notional minus commission and stamp duty. Those are charged
    # at fill time, so the hold is the amount that actually reaches the account.
    proceeds = body["filled_quantity"] * body["avg_fill_price"] - body["fee"]

    overview = client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    ).json()
    held = round(overview["cash"] - overview["available_cash"], 2)
    assert held == pytest.approx(round(proceeds, 2), abs=0.05)


def test_settlement_releases_held_cash(client):
    account = _account(client)
    code = client.get("/api/cn/symbols").json()[0]["code"]
    client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": code,
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
    )
    client.post("/api/trading/settle")
    client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": code,
            "side": "sell",
            "order_type": "market",
            "quantity": 100,
        },
    )
    before = client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    ).json()
    assert before["available_cash"] < before["cash"]

    client.post("/api/trading/settle")
    after = client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    ).json()
    assert after["available_cash"] == pytest.approx(after["cash"], abs=0.01)


def test_crypto_has_no_cash_hold(client):
    """Crypto settles instantly on both sides; only A-shares hold proceeds."""
    account = client.post(
        "/api/accounts",
        json={"name": "加密结算", "initial_balance": 100_000, "market": "crypto"},
    ).json()
    client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.05,
        },
    )
    client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "sell",
            "order_type": "market",
            "quantity": 0.05,
        },
    )
    overview = client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    ).json()
    assert overview["available_cash"] == pytest.approx(overview["cash"], abs=0.01)
