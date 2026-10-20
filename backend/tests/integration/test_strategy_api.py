from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def strategy_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'strategy_api.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=600,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_catalog_and_detail(strategy_client):
    response = strategy_client.get("/api/strategies")
    assert response.status_code == 200
    catalog = response.json()
    assert len(catalog) == 9
    sma = next(item for item in catalog if item["key"] == "sma_cross")
    assert sma["defaults"] == {"fast": 10, "slow": 30}

    detail = strategy_client.get("/api/strategies/macd_momentum")
    assert detail.status_code == 200
    assert "params_schema" in detail.json()


def test_signal_preview(strategy_client):
    response = strategy_client.post(
        "/api/strategies/sma_cross/signals",
        json={"symbol": "BTCUSDT", "timeframe": "1h", "limit": 100, "params": {"fast": 8, "slow": 24}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["params"] == {"fast": 8, "slow": 24}
    assert len(body["overlays"]["fast"]) == len(body["overlays"]["slow"]) == 100
    for signal in body["signals"]:
        assert signal["action"] in {"buy", "sell"}
        assert signal["reason"]


def test_signal_errors(strategy_client):
    missing = strategy_client.post("/api/strategies/nope/signals", json={})
    assert missing.status_code == 404

    bad_params = strategy_client.post(
        "/api/strategies/sma_cross/signals", json={"params": {"fast": 100, "slow": 5}}
    )
    assert bad_params.status_code == 422

    too_short = strategy_client.post(
        "/api/strategies/donchian_breakout/signals",
        json={"limit": 20, "params": {"entry": 55, "exit": 20}},
    )
    assert too_short.status_code == 422
