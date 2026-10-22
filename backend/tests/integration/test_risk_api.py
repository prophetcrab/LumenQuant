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
        cn_symbols_enabled=False,
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


def test_risk_overview_aggregates_everything(risk_client):
    account = risk_client.post(
        "/api/accounts", json={"name": "风控中心", "initial_balance": 300_000}
    ).json()
    risk_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.5,
        },
    )
    response = risk_client.get(
        "/api/risk/overview", params={"account_id": account["id"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["account"]["name"] == "风控中心"
    assert body["account"]["base_currency"] == "USDT"
    assert body["state"]["breaker_active"] is False
    assert body["limits"]["max_daily_loss_pct"] == 0.05
    assert body["exposure"]["position_count"] == 1
    assert body["exposure"]["exposure_pct"] > 0
    assert body["exposure"]["largest_position_pct"] > 0
    assert len(body["positions"]) == 1
    assert body["positions"][0]["symbol"] == "BTCUSDT"
    assert isinstance(body["instances"], list)
    assert isinstance(body["warnings"], list)


def test_risk_overview_warns_about_unprotected_instances(risk_client):
    account = risk_client.post(
        "/api/accounts", json={"name": "无保护账户", "initial_balance": 200_000}
    ).json()
    created = risk_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "params": {},
            "position_pct": 0.2,
            "max_order_notional": 20_000,
            "start": True,
        },
    )
    assert created.status_code == 201, created.text

    body = risk_client.get(
        "/api/risk/overview", params={"account_id": account["id"]}
    ).json()
    instance = body["instances"][0]
    assert instance["protected"] is False
    assert any("未设置任何止损" in warning for warning in body["warnings"])

    # Now set a stop and confirm the warning clears.
    patched = risk_client.patch(
        f"/api/runtime/instances/{instance['id']}", json={"stop_loss_pct": 3}
    )
    assert patched.status_code == 200
    assert patched.json()["stop_loss_pct"] == pytest.approx(0.03)

    body = risk_client.get(
        "/api/risk/overview", params={"account_id": account["id"]}
    ).json()
    assert body["instances"][0]["protected"] is True
    assert not body["warnings"]


def test_quote_outage_does_not_trip_the_breakers(risk_client, monkeypatch):
    """A missing price must not be read as a total loss.

    The risk overview values positions through the same path that falls back to
    cost when no quote is available. If that fallback were zero, one failed quote
    would show equity collapsing and trip the daily-loss breaker, blocking all
    trading for a transient network error.
    """
    account = risk_client.post(
        "/api/accounts",
        json={"name": "熔断误判", "initial_balance": 200000, "market": "crypto"},
    ).json()
    placed = risk_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 1,
        },
    )
    assert placed.status_code == 201, placed.text

    healthy = risk_client.get(
        "/api/risk/overview", params={"account_id": account["id"]}
    ).json()
    assert healthy["exposure"]["equity"] > 0

    # Break the live-candle path the way a dead feed does.
    engine = risk_client.app.state.live_market
    monkeypatch.setattr(
        engine, "live_candle", lambda symbol: (_ for _ in ()).throw(RuntimeError("down"))
    )

    degraded = risk_client.get(
        "/api/risk/overview", params={"account_id": account["id"]}
    ).json()
    # Equity may differ slightly (last stored bar vs live price) but must stay in
    # the same order of magnitude, and no breaker may trip.
    assert degraded["exposure"]["equity"] > healthy["exposure"]["equity"] * 0.5
    assert degraded["state"]["breaker_active"] is False
    assert degraded["positions"][0]["market_price"] > 0
