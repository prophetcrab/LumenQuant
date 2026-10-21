"""Data writes must invalidate every cache that holds a derived copy.

Three caches sit in front of the same rows: coverage/quality (30s), candles (2s)
and snapshots (1.5s). Import and delete only cleared the first, so for up to two
seconds `/api/market/candles` kept serving the previous series — long enough for a
user to import a CSV and immediately run a backtest on the old data.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.cn.calendar import CN_TZ, trading_days

CN_TZ_LOCAL = timezone(timedelta(hours=8))


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'cache.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_data_source="synthetic",
        cn_sync_on_start=False,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def _csv(price: float, days: int = 120) -> str:
    rows = ["open_time,open,high,low,close,volume"]
    for day in trading_days(date(2024, 1, 2), date(2026, 12, 31))[:days]:
        ts = int(datetime.combine(day, datetime.min.time(), tzinfo=CN_TZ).timestamp() * 1000)
        rows.append(f"{ts},{price},{price + 1},{price - 1},{price},1000")
    return "\n".join(rows)


def _import(client: TestClient, price: float) -> None:
    response = client.post(
        "/api/data/import",
        data={"symbol": "600036.SH", "timeframe": "1d", "replace": "true"},
        files={"file": ("bars.csv", _csv(price), "text/csv")},
    )
    assert response.status_code == 201, response.text


def test_import_invalidates_the_candle_cache(client):
    _import(client, 40.0)
    before = client.get(
        "/api/market/candles", params={"symbol": "600036.SH", "timeframe": "1d", "limit": 5}
    ).json()["candles"]
    assert before[0]["close"] == 40.0

    # Same cache key: without invalidation this returns the 40.x series.
    _import(client, 80.0)
    after = client.get(
        "/api/market/candles", params={"symbol": "600036.SH", "timeframe": "1d", "limit": 5}
    ).json()["candles"]
    assert after[0]["close"] == 80.0


def test_import_invalidates_the_snapshot_cache(client):
    _import(client, 40.0)
    first = client.get("/api/market/snapshot", params={"symbol": "600036.SH"}).json()
    assert first["price"] == pytest.approx(40.0, rel=1e-6)

    _import(client, 90.0)
    second = client.get("/api/market/snapshot", params={"symbol": "600036.SH"}).json()
    assert second["price"] == pytest.approx(90.0, rel=1e-6)


def test_delete_invalidates_the_candle_cache(client):
    _import(client, 40.0)
    client.get(
        "/api/market/candles", params={"symbol": "600036.SH", "timeframe": "1d", "limit": 5}
    )
    removed = client.request(
        "DELETE", "/api/data/series", params={"symbol": "600036.SH", "timeframe": "1d"}
    ).json()
    assert removed["deleted"] == 120

    after = client.get(
        "/api/market/candles", params={"symbol": "600036.SH", "timeframe": "1d", "limit": 5}
    ).json()["candles"]
    # Daily aggregation of an empty store yields nothing rather than stale bars.
    assert after == [] or all(bar["close"] != 40.0 for bar in after)
