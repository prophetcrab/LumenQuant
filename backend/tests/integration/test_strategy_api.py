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
    assert sma["defaults"]["fast"] == 10
    assert sma["defaults"]["slow"] == 30
    assert sma["defaults"]["require_regime"] is False

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
    assert body["params"]["fast"] == 8
    assert body["params"]["slow"] == 24
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


def test_boolean_strategy_param_is_accepted(strategy_client):
    """Boolean params must survive the HTTP round trip.

    The UI rendered them as numbers before (`Number(false) === 0`), which made the
    regime filter impossible to toggle from the browser.
    """
    enabled = strategy_client.post(
        "/api/strategies/sma_cross/signals",
        json={
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "limit": 400,
            "params": {"fast": 10, "slow": 30, "require_regime": True, "regime_period": 100},
        },
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["params"]["require_regime"] is True

    disabled = strategy_client.post(
        "/api/strategies/sma_cross/signals",
        json={
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "limit": 400,
            "params": {"fast": 10, "slow": 30, "require_regime": False, "regime_period": 100},
        },
    )
    assert disabled.status_code == 200
    assert disabled.json()["params"]["require_regime"] is False
    # The filter can only remove entries, never add them.
    assert len(enabled.json()["signals"]) <= len(disabled.json()["signals"])


def test_catalog_exposes_boolean_param_type(strategy_client):
    catalog = strategy_client.get("/api/strategies").json()
    sma = next(item for item in catalog if item["key"] == "sma_cross")
    prop = sma["params_schema"]["properties"]["require_regime"]
    assert prop["type"] == "boolean"
    assert sma["defaults"]["require_regime"] is False


def test_preview_suppresses_signals_the_position_already_satisfies(strategy_client):
    """The preview must show what would become orders, not raw strategy output.

    buy_and_hold emits "buy" on every bar from warmup (deliberate: the runtime
    only evaluates the latest bar, so it needs a standing signal). Rendering those
    raw gave 799 buy markers over 800 bars while the backtest made exactly one
    trade — the chart and the report described different systems.
    """
    body = strategy_client.post(
        "/api/strategies/buy_and_hold/signals",
        json={"symbol": "BTCUSDT", "timeframe": "1h", "limit": 200, "params": {}},
    ).json()
    assert len(body["signals"]) == 1
    assert body["signals"][0]["action"] == "buy"
    # Every bar from warmup onward produced a raw buy; exactly one survived.
    # Only ~150 of the requested 200 bars exist at this tick rate, so assert the
    # relationship instead of a fixed count.
    assert body["suppressed_signals"] > 0
    assert len(body["signals"]) == 1


def test_preview_actions_alternate_starting_with_buy(strategy_client):
    """With position state applied, a long-only strategy must alternate buy/sell."""
    body = strategy_client.post(
        "/api/strategies/sma_cross/signals",
        json={"symbol": "BTCUSDT", "timeframe": "1h", "limit": 600, "params": {}},
    ).json()
    actions = [item["action"] for item in body["signals"]]
    assert actions, "expected at least one signal"
    for index, action in enumerate(actions):
        expected = "buy" if index % 2 == 0 else "sell"
        assert action == expected, f"signal {index} was {action}: {actions}"
