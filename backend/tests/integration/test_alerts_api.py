"""Alerts are for decisions, not forensics."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'alerts.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=60,
        market_seed_bars=400,
        market_tick_seconds=3600,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
        risk_max_orders_per_minute=1000,
        risk_max_daily_loss_pct=0.01,
        risk_max_drawdown_pct=0.05,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def test_breaker_trip_creates_a_critical_alert(client):
    account = client.post(
        "/api/accounts", json={"name": "熔断账户", "initial_balance": 100_000}
    ).json()
    # Force the daily-loss breaker: observe a baseline, then trip it.
    client.post(
        "/api/risk/observe",
        params={"account_id": account["id"], "equity": 100_000},
    )
    response = client.post(
        "/api/risk/observe",
        params={"account_id": account["id"], "equity": 90_000},
    )
    assert response.status_code == 200
    assert response.json()["breaker_active"] is True

    alerts = client.get("/api/alerts", params={"account_id": account["id"]}).json()
    breaker = [item for item in alerts if item["category"] == "breaker"]
    assert breaker, alerts
    assert breaker[0]["level"] == "critical"
    assert breaker[0]["acknowledged"] is False


def test_alert_can_be_acknowledged(client):
    account = client.post(
        "/api/accounts", json={"name": "确认账户", "initial_balance": 100_000}
    ).json()
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 100_000})
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 90_000})

    alert = client.get(
        "/api/alerts", params={"account_id": account["id"]}
    ).json()[0]
    acked = client.post(f"/api/alerts/{alert['id']}/ack").json()
    assert acked["acknowledged"] is True
    assert acked["acknowledged_at"]

    unacked = client.get(
        "/api/alerts",
        params={"account_id": account["id"], "unacknowledged_only": True},
    ).json()
    assert all(item["id"] != alert["id"] for item in unacked)


def test_acknowledge_all_and_summary(client):
    account = client.post(
        "/api/accounts", json={"name": "汇总账户", "initial_balance": 100_000}
    ).json()
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 100_000})
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 90_000})

    summary = client.get("/api/alerts/summary", params={"account_id": account["id"]}).json()
    assert summary["unacknowledged"] >= 1
    assert summary["by_level"]["critical"] >= 1

    cleared = client.post("/api/alerts/ack-all", params={"account_id": account["id"]}).json()
    assert cleared["acknowledged"] >= 1
    after = client.get("/api/alerts/summary", params={"account_id": account["id"]}).json()
    assert after["unacknowledged"] == 0


def test_repeated_breaker_trips_do_not_flood(client):
    """The scheduler re-evaluates constantly; one open alert is enough."""
    account = client.post(
        "/api/accounts", json={"name": "去重账户", "initial_balance": 100_000}
    ).json()
    client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 100_000})
    for _ in range(4):
        client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 90_000})
    alerts = client.get(
        "/api/alerts", params={"account_id": account["id"], "unacknowledged_only": True}
    ).json()
    assert len([item for item in alerts if item["category"] == "breaker"]) == 1


def test_stop_loss_creates_an_alert(client):
    account = client.post(
        "/api/accounts",
        json={"name": "止损告警", "initial_balance": 500_000, "market": "cn_stock"},
    ).json()
    code = client.get("/api/cn/symbols").json()[0]["code"]
    instance = client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "buy_and_hold",
            "symbol": code,
            "timeframe": "1d",
            "params": {},
            "position_pct": 0.2,
            "max_order_notional": 50_000,
            "stop_loss_pct": 3,
            "start": True,
        },
    ).json()
    client.post("/api/runtime/tick")

    # Mark the position entry far above the market so the stop is already breached;
    # the synthetic feed's price is not something the test can push down directly.
    from sqlalchemy import select

    from app.modules.trading.models import Position

    settings = client.app.state.settings
    db = Database(settings.database_url)
    with db.session() as session:
        position = session.scalar(
            select(Position).where(Position.account_id == account["id"])
        )
        assert position is not None, "instance did not open a position"
        position.avg_price = position.avg_price * 2
        session.commit()
    db.dispose()

    client.post("/api/runtime/tick")
    alerts = client.get("/api/alerts", params={"account_id": account["id"]}).json()
    categories = [a["category"] for a in alerts]
    # A same-day A-share stop cannot actually sell (T+1), so the operator gets an
    # `exit_failed` alert instead of a `stop_loss` one — that is the more urgent
    # message: the position is unprotected and still open. Either alert proves the
    # protective path reached the alert layer.
    assert set(categories) & {"stop_loss", "trailing_stop", "take_profit", "exit_failed"}, categories
    assert any(a["ref_id"] == instance["id"] for a in alerts)


def test_unknown_alert_is_404(client):
    assert client.post("/api/alerts/nope/ack").status_code == 404
