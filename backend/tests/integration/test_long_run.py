"""Accelerated long-run stability check.

Runs the A-share engine at 20,000x so one real second covers ~5.5 simulated hours,
then asserts: no bar escapes the trading session, daily bars stay one-per-session,
memory does not balloon, and the simulated clock keeps advancing.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.modules.cn.calendar import CnSessionClock
from app.modules.cn.engine import CnMarketEngine
from app.modules.market.models import Candle

CST = timezone(timedelta(hours=8))


def test_accelerated_run_keeps_data_inside_sessions(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'long.db').as_posix()}",
        seed_market=False,
        cn_seed_days=10,
        market_tick_seconds=1,
    )
    db = Database(settings.database_url)
    db.create_schema()
    clock = CnSessionClock(speed=20_000.0)
    engine = CnMarketEngine(db, settings, clock=clock)
    engine.seed()
    try:
        start = clock.now_ms()
        for _ in range(40):
            engine.tick()
            time.sleep(0.02)

        with db.session() as session:
            rows = session.execute(
                select(Candle.timeframe, Candle.open_time).where(
                    Candle.symbol == "600519.SH"
                )
            ).all()

        clock_advance_ms = clock.now_ms() - start
        assert clock_advance_ms > 6 * 3_600_000, "clock did not advance enough"

        # 15m bars must all fall inside 09:30-11:30 / 13:00-15:00.
        for timeframe, ts in rows:
            if timeframe != "15m":
                continue
            local = datetime.fromtimestamp(ts / 1000, CST)
            minutes = local.hour * 60 + local.minute
            assert 570 <= minutes < 690 or 780 <= minutes < 900, f"bar outside session: {local}"

        # Daily bars are stamped at local midnight, exactly one per session.
        daily_times = [ts for tf, ts in rows if tf == "1d"]
        assert daily_times
        assert len(daily_times) == len(set(daily_times))
        for ts in daily_times:
            local = datetime.fromtimestamp(ts / 1000, CST)
            assert (local.hour, local.minute) == (0, 0)
    finally:
        db.dispose()


def test_engine_does_not_accumulate_live_state(tmp_path):
    """The live-bar cache must stay bounded by the symbol count."""
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'state.db').as_posix()}",
        seed_market=False,
        cn_seed_days=10,
        market_tick_seconds=1,
    )
    db = Database(settings.database_url)
    db.create_schema()
    engine = CnMarketEngine(db, settings, clock=CnSessionClock(speed=20_000.0))
    engine.seed()
    try:
        for _ in range(30):
            engine.tick()
            time.sleep(0.01)
        assert 0 < len(engine._live) <= 8
        assert len(engine._locked) <= 8
    finally:
        db.dispose()
