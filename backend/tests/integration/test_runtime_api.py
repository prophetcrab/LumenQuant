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
        cn_symbols_enabled=False,
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
    assert instance["params"]["fast"] == 10
    assert instance["params"]["slow"] == 30

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
    assert updated.json()["params"]["fast"] == 5
    assert updated.json()["params"]["slow"] == 20

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


def test_manual_tick_runs_the_protective_sweep(runtime_client, monkeypatch):
    """「立即评估」must run the same protective pass as the scheduler.

    It used to evaluate signals only, so a stop that the background scheduler
    would have fired never triggered during manual testing — the UI disagreed
    with production in exactly the situation users check it.
    """
    from app.modules.runtime.service import RuntimeService

    calls: list[bool] = []
    original = RuntimeService.protect_positions

    def spy(self):
        calls.append(True)
        return {"probe": "swept"}

    monkeypatch.setattr(RuntimeService, "protect_positions", spy)
    tick = runtime_client.post("/api/runtime/tick")
    assert tick.status_code == 200
    assert calls, "manual tick skipped the protective sweep"
    assert tick.json()["evaluated"]["probe"] == "swept"
    monkeypatch.setattr(RuntimeService, "protect_positions", original)


def test_update_accepts_stops_and_rejects_invalid_sizing(runtime_client):
    account = runtime_client.post(
        "/api/accounts", json={"name": "Auto Edit", "initial_balance": 100_000}
    ).json()
    instance = runtime_client.post(
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
    ).json()

    updated = runtime_client.patch(
        f"/api/runtime/instances/{instance['id']}",
        json={"stop_loss_pct": 5, "take_profit_pct": 10, "trailing_stop_pct": 3},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["stop_loss_pct"] == pytest.approx(0.05)
    assert body["take_profit_pct"] == pytest.approx(0.10)
    assert body["trailing_stop_pct"] == pytest.approx(0.03)

    bad = runtime_client.patch(
        f"/api/runtime/instances/{instance['id']}", json={"position_pct": 3}
    )
    assert bad.status_code == 422


def test_one_bad_instance_does_not_break_the_whole_tick(runtime_client):
    """A malformed running instance must not abort every other instance.

    The background scheduler isolates each instance; the manual endpoint let the
    exception escape, so a single stale row turned the entire pass into a 422.
    """
    from sqlalchemy import select

    from app.core.db import Database
    from app.modules.runtime.models import StrategyInstance

    account = runtime_client.post(
        "/api/accounts", json={"name": "Tick Isolation", "initial_balance": 100_000}
    ).json()
    good = runtime_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "params": {},
            "position_pct": 0.1,
            "max_order_notional": 5000,
            "start": True,
        },
    ).json()

    # Corrupt a second instance's stored params directly, bypassing validation.
    bad = runtime_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "params": {},
            "position_pct": 0.1,
            "max_order_notional": 5000,
            "start": True,
        },
    ).json()

    settings = runtime_client.app.state.settings
    db = Database(settings.database_url)
    with db.session() as session:
        row = session.scalar(
            select(StrategyInstance).where(StrategyInstance.id == bad["id"])
        )
        row.params_json = '{"fast": 100, "slow": 5}'  # fast >= slow
        session.commit()
    db.dispose()

    response = runtime_client.post("/api/runtime/tick")
    assert response.status_code == 200, response.text
    evaluated = response.json()["evaluated"]
    assert evaluated.get(bad["id"]) == "error"
    assert good["id"] in evaluated


def test_entry_sizes_against_available_cash_not_book_balance(runtime_client):
    """A resting order freezes part of the balance; sizing must respect that.

    `_place_entry` used `account.cash_balance`, which ignores cash reserved by
    resting buy orders. With 30k frozen on a 100k account, a 50% instance still
    tried to spend 50k — more than was actually available.
    """
    import math

    account = runtime_client.post(
        "/api/accounts", json={"name": "Sizing Basis", "initial_balance": 100_000}
    ).json()
    price = runtime_client.get("/api/market/snapshot", params={"symbol": "BTCUSDT"}).json()["price"]
    quantity = math.floor((60_000 / price) / 0.0001) * 0.0001
    resting = runtime_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "limit",
            "quantity": quantity,
            "limit_price": round(price * 0.5, 2),
        },
    )
    assert resting.status_code == 201

    overview = runtime_client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    ).json()
    available = overview["available_cash"]
    assert available < overview["cash"]  # something really is frozen

    instance = runtime_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "buy_and_hold",
            "symbol": "ETHUSDT",
            "timeframe": "1h",
            "params": {},
            "position_pct": 0.5,
            "max_order_notional": 100_000,
            "start": True,
        },
    ).json()
    runtime_client.post("/api/runtime/tick")

    positions = runtime_client.get(
        "/api/trading/positions", params={"account_id": account["id"]}
    ).json()
    eth = [item for item in positions if item["symbol"] == "ETHUSDT"]
    assert eth, f"instance did not open a position: {instance['id']}"
    # Within one lot step of half the *available* cash, and clearly below half the
    # book balance.
    assert eth[0]["market_value"] < overview["cash"] * 0.5
    assert eth[0]["market_value"] == pytest.approx(available * 0.5, rel=0.05)
