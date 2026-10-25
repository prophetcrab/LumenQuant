from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def market_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'market_api.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=260,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_symbol_catalog(market_client):
    response = market_client.get("/api/market/symbols")
    assert response.status_code == 200
    symbols = response.json()
    codes = {s["code"] for s in symbols}
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT"} <= codes
    crypto = [s for s in symbols if s["market"] == "crypto"]
    assert len(crypto) == 6


def test_candles_contract(market_client):
    response = market_client.get(
        "/api/market/candles", params={"symbol": "BTCUSDT", "timeframe": "1h", "limit": 50}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timeframe"] == "1h"
    candles = body["candles"]
    assert len(candles) == 50
    for bar in candles:
        assert bar["high"] >= max(bar["open"], bar["close"])
        assert bar["low"] <= min(bar["open"], bar["close"])
        assert bar["volume"] >= 0


def test_snapshot_and_clock(market_client):
    snapshot = market_client.get("/api/market/snapshot", params={"symbol": "ETHUSDT"})
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["symbol"] == "ETHUSDT"
    assert body["price"] > 0

    clock = market_client.get("/api/market/clock").json()
    assert clock["simulated_now"] > 0
    assert clock["speed"] == 60.0


def test_indicators_alignment(market_client):
    response = market_client.get(
        "/api/market/indicators",
        params={
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "limit": 120,
            "specs": "sma:20,macd:12:26:9,bollinger:20:2",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["times"]) == 120
    for values in body["series"].values():
        assert len(values) == 120
    assert body["series"]["SMA(20)"][0] is None
    assert body["series"]["SMA(20)"][19] is not None


def test_snapshot_without_candles_reports_not_found(tmp_path):
    """A symbol with no price history is a 404, not an internal error.

    The crypto snapshot path raised `NameError: name 'symbol' is not defined`
    instead of the intended NotFoundError, so a freshly added symbol with no
    bars answered 500 — an error page where the UI expected "no data yet".
    """
    from app.modules.market.models import Symbol

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'snapshot.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        # Register the symbol directly: the create endpoint is not the subject here.
        session = db.session_factory()
        session.add(
            Symbol(
                code="NODATAUSDT", name="No Data", market="crypto",
                asset_class="crypto", base="NODATA", quote="USDT",
                initial_price=1.0,
            )
        )
        session.commit()
        session.close()

        response = client.get("/api/market/snapshot", params={"symbol": "NODATAUSDT"})
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "not_found"
    db.dispose()


def test_validation_errors(market_client):
    bad_frame = market_client.get(
        "/api/market/candles", params={"symbol": "BTCUSDT", "timeframe": "2m"}
    )
    assert bad_frame.status_code == 422
    assert bad_frame.json()["error"]["code"] == "validation_error"

    bad_indicator = market_client.get(
        "/api/market/indicators", params={"specs": "wizard:3"}
    )
    assert bad_indicator.status_code == 422

    missing = market_client.get("/api/market/snapshot", params={"symbol": "NOPE"})
    assert missing.status_code == 404
