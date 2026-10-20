from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def risk_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'risk.db').as_posix()}",
        seed_market=True,
        market_seed_bars=300,
        market_tick_seconds=3600,
        runtime_enabled=False,
        risk_max_daily_loss_pct=0.05,
        risk_max_drawdown_pct=0.20,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_limits_endpoint(risk_client):
    limits = risk_client.get("/api/risk/limits").json()
    assert limits["max_daily_loss_pct"] == 0.05
    assert limits["max_drawdown_pct"] == 0.20
    assert limits["min_order_notional"] == 10.0


def test_daily_loss_trips_and_blocks_orders(risk_client):
    account = risk_client.post(
        "/api/accounts", json={"name": "Risk Book", "initial_balance": 10_000}
    ).json()
    started = risk_client.post(
        "/api/risk/observe", params={"account_id": account["id"], "equity": 10_000}
    ).json()
    assert started["day_start_equity"] == 10_000
    assert started["breaker_active"] is False

    tripped = risk_client.post(
        "/api/risk/observe", params={"account_id": account["id"], "equity": 9_400}
    ).json()
    assert tripped["breaker_active"] is True
    assert "daily loss" in tripped["breaker_reason"]

    blocked = risk_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.001,
        },
    )
    assert blocked.status_code == 409
    assert "halted" in blocked.json()["error"]["message"]

    reset = risk_client.post("/api/risk/reset", params={"account_id": account["id"]}).json()
    assert reset["breaker_active"] is False

    allowed = risk_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.001,
        },
    )
    assert allowed.status_code == 201
