from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def backtest_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'backtest_api.db').as_posix()}",
        seed_market=True,
        market_seed_bars=3000,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_run_and_fetch_backtest(backtest_client):
    response = backtest_client.post(
        "/api/backtests",
        json={
            "strategy": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "bars": 500,
            "params": {"fast": 10, "slow": 30},
            "config": {"initial_capital": 50000, "position_pct": 0.9},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["strategy"] == "sma_cross"
    assert body["metrics"]["initial_capital"] == 50000
    assert body["metrics"]["bars"] == 500
    assert len(body["equity"]) == 500
    assert body["equity"][0]["time"] < body["equity"][-1]["time"]
    assert body["duration_ms"] >= 0
    for trade in body["trades"]:
        assert trade["exit_time"] >= trade["entry_time"]

    run_id = body["id"]
    fetched = backtest_client.get(f"/api/backtests/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == run_id

    listed = backtest_client.get("/api/backtests")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert "equity" not in listed.json()[0]

    deleted = backtest_client.delete(f"/api/backtests/{run_id}")
    assert deleted.status_code == 204
    assert backtest_client.get(f"/api/backtests/{run_id}").status_code == 404


def test_all_strategies_backtest(backtest_client):
    catalog = backtest_client.get("/api/strategies").json()
    for meta in catalog:
        response = backtest_client.post(
            "/api/backtests",
            json={
                "strategy": meta["key"],
                "symbol": "ETHUSDT",
                "timeframe": "1h",
                "bars": 600,
            },
        )
        assert response.status_code == 201, f"{meta['key']}: {response.text}"
        metrics = response.json()["metrics"]
        assert metrics["bars"] == 600
        assert metrics["max_drawdown_pct"] >= 0


def test_backtest_validation(backtest_client):
    missing = backtest_client.post(
        "/api/backtests", json={"strategy": "grail", "bars": 100}
    )
    assert missing.status_code == 404

    bad_params = backtest_client.post(
        "/api/backtests",
        json={"strategy": "sma_cross", "bars": 300, "params": {"fast": 60, "slow": 10}},
    )
    assert bad_params.status_code == 422

    too_few = backtest_client.post(
        "/api/backtests", json={"strategy": "sma_cross", "bars": 40}
    )
    assert too_few.status_code == 422

    unknown_strategy = backtest_client.post(
        "/api/backtests", json={"strategy": "wizard", "bars": 300}
    )
    assert unknown_strategy.status_code == 404
