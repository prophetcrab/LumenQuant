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
        cn_symbols_enabled=False,
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


def test_optimise_endpoint(backtest_client):
    response = backtest_client.post(
        "/api/backtests/optimise",
        json={
            "strategy": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "bars": 800,
            "space": {"fast": [5, 10], "slow": [20, 30]},
            "objective": "risk_adjusted",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tested"] == 4
    assert body["valid"] >= 1
    assert body["best"] is not None
    assert "params" in body["best"]
    assert body["in_sample_bars"] + body["out_of_sample_bars"] == body["bars"]
    assert isinstance(body["warnings"], list)
    assert body["distribution"]["best_return_pct"] >= body["distribution"]["worst_return_pct"]


def test_optimise_validation(backtest_client):
    empty_space = backtest_client.post(
        "/api/backtests/optimise",
        json={"strategy": "sma_cross", "bars": 800, "space": {}},
    )
    assert empty_space.status_code == 422

    bad_objective = backtest_client.post(
        "/api/backtests/optimise",
        json={
            "strategy": "sma_cross",
            "bars": 800,
            "space": {"fast": [5]},
            "objective": "magic",
        },
    )
    assert bad_objective.status_code == 422

    too_few_bars = backtest_client.post(
        "/api/backtests/optimise",
        json={"strategy": "sma_cross", "bars": 100, "space": {"fast": [5]}},
    )
    assert too_few_bars.status_code == 422

    unknown = backtest_client.post(
        "/api/backtests/optimise",
        json={"strategy": "grail", "bars": 800, "space": {"fast": [5]}},
    )
    assert unknown.status_code == 404


def test_optimise_does_not_pollute_backtest_history(backtest_client):
    before = backtest_client.get("/api/backtests").json()
    backtest_client.post(
        "/api/backtests/optimise",
        json={
            "strategy": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "bars": 600,
            "space": {"fast": [5, 10], "slow": [20]},
        },
    )
    after = backtest_client.get("/api/backtests").json()
    assert len(after) == len(before)


def test_walk_forward_endpoint(backtest_client):
    response = backtest_client.post(
        "/api/backtests/walk-forward",
        json={
            "strategy": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "bars": 1200,
            "space": {"fast": [5, 10], "slow": [20, 30]},
            "objective": "risk_adjusted",
            "windows": 3,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["windows"]
    assert body["aggregate"]["windows_completed"] == len(body["windows"])
    for window in body["windows"]:
        assert window["params"]
        assert window["train_end"] == window["test_start"]
    assert isinstance(body["warnings"], list)
    assert body["data_source"] in {"synthetic", "real"}


def test_walk_forward_validation(backtest_client):
    too_few = backtest_client.post(
        "/api/backtests/walk-forward",
        json={"strategy": "sma_cross", "bars": 300, "space": {"fast": [5]}, "windows": 3},
    )
    assert too_few.status_code == 422

    bad_windows = backtest_client.post(
        "/api/backtests/walk-forward",
        json={"strategy": "sma_cross", "bars": 1200, "space": {"fast": [5]}, "windows": 1},
    )
    assert bad_windows.status_code == 422

    empty = backtest_client.post(
        "/api/backtests/walk-forward",
        json={"strategy": "sma_cross", "bars": 1200, "space": {}, "windows": 3},
    )
    assert empty.status_code == 422
