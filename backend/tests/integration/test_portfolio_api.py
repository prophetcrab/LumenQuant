from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def portfolio_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'portfolio.db').as_posix()}",
        seed_market=True,
        market_seed_bars=300,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_overview_without_account_returns_404(portfolio_client):
    response = portfolio_client.get("/api/portfolio/overview")
    assert response.status_code == 404


def test_overview_and_history(portfolio_client):
    account = portfolio_client.post(
        "/api/accounts", json={"name": "Book", "initial_balance": 20_000}
    ).json()
    portfolio_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.1,
        },
    )
    overview = portfolio_client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    )
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["equity"] == pytest.approx(body["cash"] + body["positions_value"])
    assert body["position_count"] == 1
    assert body["allocations"][0]["symbol"] == "BTCUSDT"
    assert 0 < body["exposure_pct"] < 100

    history = portfolio_client.get(
        "/api/portfolio/history", params={"account_id": account["id"]}
    ).json()
    assert len(history) >= 1
    assert history[-1]["equity"] == pytest.approx(body["equity"])
