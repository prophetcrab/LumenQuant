from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def runtime_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'runtime.db').as_posix()}",
        seed_market=True,
        market_seed_bars=400,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_instance_lifecycle(runtime_client):
    account = runtime_client.post(
        "/api/accounts", json={"name": "Auto Desk", "initial_balance": 100_000}
    ).json()
    created = runtime_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "params": {"fast": 10, "slow": 30},
            "position_pct": 0.2,
            "max_order_notional": 20_000,
            "start": False,
        },
    )
    assert created.status_code == 201, created.text
    instance = created.json()
    assert instance["running"] is False
    assert instance["strategy_name"] == "双均线交叉"
    assert instance["params"] == {"fast": 10, "slow": 30}

    started = runtime_client.post(
        f"/api/runtime/instances/{instance['id']}/toggle", json={"running": True}
    )
    assert started.status_code == 200
    assert started.json()["running"] is True

    tick = runtime_client.post("/api/runtime/tick")
    assert tick.status_code == 200
    assert instance["id"] in tick.json()["evaluated"]

    logs = runtime_client.get(f"/api/runtime/instances/{instance['id']}/logs").json()
    assert any(log["event"] == "started" for log in logs)

    updated = runtime_client.patch(
        f"/api/runtime/instances/{instance['id']}",
        json={"params": {"fast": 5, "slow": 20}, "position_pct": 0.1},
    )
    assert updated.status_code == 200
    assert updated.json()["params"] == {"fast": 5, "slow": 20}

    blocked = runtime_client.delete(f"/api/runtime/instances/{instance['id']}")
    assert blocked.status_code == 409

    runtime_client.post(
        f"/api/runtime/instances/{instance['id']}/toggle", json={"running": False}
    )
    deleted = runtime_client.delete(f"/api/runtime/instances/{instance['id']}")
    assert deleted.status_code == 204


def test_runtime_validation(runtime_client):
    account = runtime_client.post(
        "/api/accounts", json={"name": "Auto Validation", "initial_balance": 50_000}
    ).json()
    bad_strategy = runtime_client.post(
        "/api/runtime/instances",
        json={"account_id": account["id"], "strategy_key": "grail", "symbol": "BTCUSDT"},
    )
    assert bad_strategy.status_code == 404

    bad_params = runtime_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "sma_cross",
            "symbol": "BTCUSDT",
            "params": {"fast": 80, "slow": 10},
        },
    )
    assert bad_params.status_code == 422

    bad_account = runtime_client.post(
        "/api/runtime/instances",
        json={"account_id": "nope", "strategy_key": "sma_cross", "symbol": "BTCUSDT"},
    )
    assert bad_account.status_code == 404
