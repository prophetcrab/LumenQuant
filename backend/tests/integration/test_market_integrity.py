"""Data-integrity tests for the two markets.

These guard the defects found by actually using the running system:

* the crypto engine wrote 24/7 bars for A-share symbols, producing bars at
  19:00 and a 31-bar day;
* daily bars were generated independently of intraday bars, so the two
  timeframes disagreed on open/high/low/close;
* epoch bucketing produced 30m/1h bars straddling the lunch break.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.cn.aggregate import aggregate_session_bars
from app.modules.market.service_types import CandlePoint


@pytest.fixture()
def dual_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'dual.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=30,
        market_seed_bars=200,
        market_tick_seconds=1,
        runtime_enabled=True,
        engine_interval_seconds=1,
        cn_session_enforced=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_crypto_engine_does_not_touch_cn_symbols(dual_client):
    candles = dual_client.get(
        "/api/market/candles",
        params={"symbol": "600519.SH", "timeframe": "15m", "limit": 500},
    ).json()["candles"]
    assert candles
    from datetime import datetime, timedelta, timezone

    cst = timezone(timedelta(hours=8))
    per_day: dict[str, int] = {}
    for bar in candles:
        local = datetime.fromtimestamp(bar["time"] / 1000, cst)
        per_day[local.date().isoformat()] = per_day.get(local.date().isoformat(), 0) + 1
        # No bar may exist outside 09:30-11:30 / 13:00-15:00.
        minutes = local.hour * 60 + local.minute
        assert 570 <= minutes < 690 or 780 <= minutes < 900, f"bar at {local}"
    # A full session has exactly 16 fifteen-minute bars.
    assert max(per_day.values()) <= 16


def test_daily_bar_matches_intraday_aggregate(dual_client):
    from datetime import datetime, timedelta, timezone

    cst = timezone(timedelta(hours=8))
    daily = dual_client.get(
        "/api/market/candles",
        params={"symbol": "600519.SH", "timeframe": "1d", "limit": 5},
    ).json()["candles"]
    intraday = dual_client.get(
        "/api/market/candles",
        params={"symbol": "600519.SH", "timeframe": "15m", "limit": 200},
    ).json()["candles"]

    by_day: dict[str, list[dict]] = {}
    for bar in intraday:
        key = datetime.fromtimestamp(bar["time"] / 1000, cst).date().isoformat()
        by_day.setdefault(key, []).append(bar)

    checked = 0
    for day in daily:
        key = datetime.fromtimestamp(day["time"] / 1000, cst).date().isoformat()
        bars = by_day.get(key)
        if not bars:
            continue
        assert day["open"] == pytest.approx(bars[0]["open"], abs=0.02)
        assert day["close"] == pytest.approx(bars[-1]["close"], abs=0.02)
        assert day["high"] == pytest.approx(max(b["high"] for b in bars), abs=0.02)
        assert day["low"] == pytest.approx(min(b["low"] for b in bars), abs=0.02)
        checked += 1
    assert checked >= 3


def test_1h_bars_do_not_straddle_lunch():
    def bar(hour: int, minute: int, close: float) -> CandlePoint:
        from app.modules.cn.calendar import to_ms
        from datetime import date

        return CandlePoint(
            time=to_ms(date(2026, 10, 20), __import__("datetime").time(hour, minute)),
            open=close, high=close, low=close, close=close, volume=1.0,
        )

    morning = [bar(9, 30, 10), bar(9, 45, 11), bar(10, 0, 12), bar(10, 15, 13),
               bar(10, 30, 14), bar(10, 45, 15), bar(11, 0, 16), bar(11, 15, 17)]
    afternoon = [bar(13, 0, 20), bar(13, 15, 21), bar(13, 30, 22), bar(13, 45, 23),
                 bar(14, 0, 24), bar(14, 15, 25), bar(14, 30, 26), bar(14, 45, 27)]
    out = aggregate_session_bars(morning + afternoon, "1h")
    # Two bars in the morning (4+4), two in the afternoon.
    assert len(out) == 4
    assert out[0].open == 10 and out[0].close == 13
    assert out[1].open == 14 and out[1].close == 17
    assert out[2].open == 20 and out[2].close == 23
    assert out[3].open == 24 and out[3].close == 27


def test_cn_rejects_intraday_4h(dual_client):
    response = dual_client.get(
        "/api/market/candles",
        params={"symbol": "600519.SH", "timeframe": "4h", "limit": 50},
    )
    assert response.status_code == 422
    assert "4h" in response.json()["error"]["message"]


def test_crypto_still_supports_4h(dual_client):
    response = dual_client.get(
        "/api/market/candles",
        params={"symbol": "BTCUSDT", "timeframe": "4h", "limit": 50},
    )
    assert response.status_code == 200
    candles = response.json()["candles"]
    # 200 seeded 15m bars aggregate to ~12 four-hour bars; assert the timeframe
    # is honoured rather than a specific count that depends on seed size.
    assert 0 < len(candles) <= 50
    assert all(c["time"] % (4 * 3_600_000) == 0 for c in candles)


def test_daily_bar_advances_with_intraday(dual_client):
    """The daily series must not freeze at the seed date.

    It previously only existed for seeded history, so every daily strategy traded
    on stale prices while intraday kept moving.
    """
    import time
    from datetime import datetime, timedelta, timezone

    cst = timezone(timedelta(hours=8))

    def latest_daily():
        return dual_client.get(
            "/api/market/candles",
            params={"symbol": "000001.SZ", "timeframe": "1d", "limit": 1},
        ).json()["candles"][-1]

    before = latest_daily()
    # Let the live simulator close at least one 15m bar.
    for _ in range(14):
        time.sleep(1.05)
        if latest_daily()["close"] != before["close"]:
            break
    after = latest_daily()
    intraday = dual_client.get(
        "/api/market/candles",
        params={"symbol": "000001.SZ", "timeframe": "15m", "limit": 60},
    ).json()["candles"]
    day = datetime.fromtimestamp(after["time"] / 1000, cst).date()
    same_day = [
        bar for bar in intraday
        if datetime.fromtimestamp(bar["time"] / 1000, cst).date() == day
    ]
    assert same_day, "no intraday bars for the latest daily bar"
    assert after["open"] == pytest.approx(same_day[0]["open"], abs=0.02)
    assert after["close"] == pytest.approx(same_day[-1]["close"], abs=0.02)
    assert after["high"] == pytest.approx(max(b["high"] for b in same_day), abs=0.02)
    assert after["low"] == pytest.approx(min(b["low"] for b in same_day), abs=0.02)
