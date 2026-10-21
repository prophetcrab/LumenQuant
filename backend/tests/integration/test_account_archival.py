"""Archiving an account must stop its trading and keep its history readable."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'arch.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=400,
        market_tick_seconds=3600,
        runtime_enabled=False,
        risk_max_orders_per_minute=1000,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def _setup(client: TestClient) -> tuple[dict, dict]:
    account = client.post(
        "/api/accounts", json={"name": "归档账户", "initial_balance": 200_000, "market": "crypto"}
    ).json()
    client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.1,
        },
    )
    instance = client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "buy_and_hold",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "params": {},
            "position_pct": 0.1,
            "max_order_notional": 20_000,
            "start": True,
        },
    ).json()
    return account, instance


def test_archive_stops_running_instances(client):
    """Regression: an archived account's instance kept trading.

    `set_running` checked account status, but nothing re-checked it while the
    scheduler ran, so archiving an account left its instances placing orders.
    """
    account, instance = _setup(client)
    response = client.patch(f"/api/accounts/{account['id']}", json={"status": "archived"})
    assert response.status_code == 200
    assert response.json()["status"] == "archived"

    instances = client.get("/api/runtime/instances", params={"account_id": account["id"]}).json()
    assert instances[0]["running"] is False
    # The manual tick has nothing left to evaluate.
    assert client.post("/api/runtime/tick").json()["evaluated"] == {}


def test_archived_account_history_stays_readable(client):
    account, _instance = _setup(client)
    client.patch(f"/api/accounts/{account['id']}", json={"status": "archived"})

    assert client.get("/api/trading/positions", params={"account_id": account["id"]}).status_code == 200
    assert client.get("/api/trading/orders", params={"account_id": account["id"]}).status_code == 200
    assert client.get("/api/portfolio/overview", params={"account_id": account["id"]}).status_code == 200
    assert client.get(f"/api/accounts/{account['id']}/ledger").status_code == 200


def test_archived_account_rejects_new_activity(client):
    account, _instance = _setup(client)
    client.patch(f"/api/accounts/{account['id']}", json={"status": "archived"})

    order = client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.01,
        },
    )
    assert order.status_code == 409
    assert client.post(f"/api/accounts/{account['id']}/deposit", json={"amount": 1000}).status_code == 409


def test_execution_time_guard_blocks_seeded_running_instance(client):
    """A running instance on an archived account must not trade, even if seeded.

    Simulates a row that predates the archive-stops-instances fix (or one edited
    directly in the database): the scheduler must still refuse to act on it.
    """
    from sqlalchemy import select

    from app.modules.runtime.models import StrategyInstance

    account, instance = _setup(client)
    client.patch(f"/api/accounts/{account['id']}", json={"status": "archived"})

    # Force the flag back on, bypassing the API, then tick.
    settings = client.app.state.settings
    db = Database(settings.database_url)
    with db.session() as session:
        row = session.scalar(
            select(StrategyInstance).where(StrategyInstance.id == instance["id"])
        )
        row.running = True
        session.commit()
    db.dispose()

    tick = client.post("/api/runtime/tick").json()
    assert tick["evaluated"].get(instance["id"]) == "account_archived"
    orders = client.get("/api/trading/orders", params={"account_id": account["id"]}).json()
    # Only the original position-opening order exists.
    assert len(orders) == 1
