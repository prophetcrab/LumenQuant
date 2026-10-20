from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def system_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'system.db').as_posix()}",
        seed_market=True,
        market_seed_bars=200,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_request_id_header(system_client):
    response = system_client.get("/api/market/symbols")
    assert response.headers["X-Request-ID"]

    custom = system_client.get("/api/market/symbols", headers={"X-Request-ID": "trace-123"})
    assert custom.headers["X-Request-ID"] == "trace-123"


def test_metrics_accumulate(system_client):
    system_client.get("/api/market/symbols")
    system_client.get("/api/market/symbols")
    system_client.get("/api/accounts/unknown-id")
    metrics = system_client.get("/api/system/metrics").json()
    assert metrics["requests"]["requests"] >= 3
    assert metrics["requests"]["status_codes"].get("404", 0) >= 1
    assert metrics["engines"]["market"] is True
    assert "candles" in metrics["caches"]


def test_audit_trail_records_account_creation(system_client):
    system_client.post("/api/accounts", json={"name": "Audit Me", "initial_balance": 1000})
    rows = system_client.get("/api/system/audit", params={"action": "account.created"}).json()
    assert len(rows) >= 1
    assert rows[0]["detail"]["name"] == "Audit Me"

    summary = system_client.get("/api/system/audit/summary").json()
    assert any(item["action"] == "account.created" and item["count"] >= 1 for item in summary)
