"""Automated live trading needs its own opt-in, separate from manual live orders.

The manual order endpoint requires `X-Live-Confirm: true` plus
`LQ_LIVE_TRADING_ENABLED`. A running strategy instance places orders through the
service layer, so neither check applied to it: a live account plus a started
instance was enough to send real orders with no human in the loop, and turning the
switch back off did not stop an instance that already existed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app

BASE = dict(
    seed_market=True,
    cn_symbols_enabled=True,
    cn_seed_days=60,
    market_seed_bars=300,
    market_tick_seconds=3600,
    runtime_enabled=False,
    cn_session_enforced=False,
    cn_live_session_gate=False,
    live_trading_enabled=True,
    ths_mock_enabled=True,
    risk_max_orders_per_minute=1000,
)


def _app(tmp_path, name: str, **overrides):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / name).as_posix()}", **{**BASE, **overrides}
    )
    db = Database(settings.database_url)
    db.create_schema()
    return settings, db, create_app(settings, db)


def _live_account(client: TestClient) -> dict:
    return client.post(
        "/api/accounts",
        json={
            "name": "Live",
            "kind": "live",
            "market": "cn_stock",
            "initial_balance": 500_000,
        },
    ).json()


def _instance_body(account_id: str) -> dict:
    return {
        "account_id": account_id,
        "strategy_key": "buy_and_hold",
        "symbol": "600036.SH",
        "timeframe": "1d",
        "params": {},
        "position_pct": 0.1,
        "max_order_notional": 50_000,
        "start": True,
    }


def test_manual_live_order_still_requires_the_confirm_header(tmp_path):
    _settings, db, app = _app(tmp_path, "manual.db")
    with TestClient(app) as client:
        account = _live_account(client)
        blocked = client.post(
            "/api/trading/orders",
            json={
                "account_id": account["id"],
                "symbol": "600036.SH",
                "side": "buy",
                "order_type": "market",
                "quantity": 100,
            },
        )
        assert blocked.status_code == 403
        assert "X-Live-Confirm" in blocked.json()["error"]["message"]
    db.dispose()


def test_live_instance_cannot_be_created_without_automation_opt_in(tmp_path):
    _settings, db, app = _app(tmp_path, "create.db")
    with TestClient(app) as client:
        account = _live_account(client)
        response = client.post("/api/runtime/instances", json=_instance_body(account["id"]))
        assert response.status_code == 403
        assert "LQ_LIVE_AUTO_TRADING_ENABLED" in response.json()["error"]["message"]
        assert client.get("/api/trading/orders", params={"account_id": account["id"]}).json() == []
    db.dispose()


def test_live_instance_is_allowed_when_automation_is_enabled(tmp_path):
    _settings, db, app = _app(
        tmp_path, "enabled.db", live_auto_trading_enabled=True
    )
    with TestClient(app) as client:
        account = _live_account(client)
        created = client.post("/api/runtime/instances", json=_instance_body(account["id"]))
        assert created.status_code == 201, created.text
    db.dispose()


def test_disabling_automation_stops_an_existing_live_instance(tmp_path):
    """The switch must take effect at execution time, not only at creation."""
    settings, db, app = _app(
        tmp_path, "existing.db", live_auto_trading_enabled=True
    )
    with TestClient(app) as client:
        account = _live_account(client)
        instance = client.post(
            "/api/runtime/instances", json=_instance_body(account["id"])
        ).json()

    # Same database, automation now disabled — the situation after an operator
    # flips the flag and restarts, expecting the bot to be off.
    settings_off = Settings(
        database_url=settings.database_url,
        **{**BASE, "live_auto_trading_enabled": False},
    )
    db_off = Database(settings_off.database_url)
    app_off = create_app(settings_off, db_off)
    with TestClient(app_off) as client:
        for _ in range(3):
            tick = client.post("/api/runtime/tick")
            assert tick.status_code == 200
            assert tick.json()["evaluated"].get(instance["id"]) == "live_auto_disabled"
        assert client.get("/api/trading/orders", params={"account_id": account["id"]}).json() == []
        logs = client.get(f"/api/runtime/instances/{instance['id']}/logs").json()
        # Logged once, not once per tick.
        assert len([item for item in logs if item["event"] == "live_auto_disabled"]) == 1
    db_off.dispose()
    db.dispose()


def test_paper_instances_unaffected_by_the_live_switch(tmp_path):
    _settings, db, app = _app(tmp_path, "paper.db")
    with TestClient(app) as client:
        account = client.post(
            "/api/accounts",
            json={"name": "Paper", "kind": "paper", "market": "cn_stock", "initial_balance": 300_000},
        ).json()
        created = client.post("/api/runtime/instances", json=_instance_body(account["id"]))
        assert created.status_code == 201, created.text
    db.dispose()
