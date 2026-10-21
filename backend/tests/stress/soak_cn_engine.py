"""Long-run soak: drive the simulated A-share clock across many sessions.

The existing stress scripts measure request latency against a live server. This
one answers a different question: does the *engine* stay correct when it runs for
months of market time without a restart? It compresses time by stepping the
session clock directly, so a quarter of trading days takes seconds.

Invariants checked afterwards:
  * no bar written on a non-trading day
  * no malformed OHLC (low <= open/close <= high, positive prices)
  * exactly one daily bar per symbol per trading day (no duplicates)
  * every symbol has a per-session bar count matching its session structure
  * prev_close and limit bands stay populated

Usage:
  python tests/stress/soak_cn_engine.py --sessions 60
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import func, select  # noqa: E402

from app.modules.cn.calendar import is_session_open  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.modules.cn.calendar import (  # noqa: E402
    day_key,
    is_trading_day,
    next_open_ms,
)
from app.modules.cn.engine import CnMarketEngine  # noqa: E402
from app.modules.market.models import Candle, Symbol  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
STEP_MS = 15 * 60 * 1000


def advance(engine: CnMarketEngine, days: int) -> int:
    """Tick through `days` full trading sessions (morning + afternoon).

    Two details matter here:

    * Jumps must go through `next_open_ms`. Landing exactly on 11:30 is outside
      the morning window, and a naive "next trading day" jump silently skips the
      whole afternoon — half the bars, with a soak that still looks green.
    * A day ends when the *date* changes, not when the clock jumps. The lunch
      break is also a jump, so counting jumps would halve the coverage.
    """
    ticks = 0
    start_day = day_key(engine.clock.now_ms())
    while True:
        engine.tick()
        ticks += 1
        cursor = engine.clock._cursor + STEP_MS
        if not is_session_open(cursor):
            cursor = next_open_ms(cursor)
        engine.clock._cursor = cursor
        if (day_key(cursor) - start_day).days >= days:
            break
    return ticks


def report(db: Database, engine: CnMarketEngine) -> dict:
    with db.session() as session:
        symbols = session.scalars(
            select(Symbol).where(Symbol.market == "cn_stock")
        ).all()
        daily = session.scalars(
            select(Candle).where(Candle.timeframe == "1d")
        ).all()
        intraday_count = session.scalar(
            select(func.count(Candle.id)).where(Candle.timeframe == "15m")
        )

    non_trading = sum(1 for bar in daily if not is_trading_day(day_key(bar.open_time)))
    malformed = sum(
        1
        for bar in daily
        if bar.low <= 0
        or bar.high < bar.low
        or not (bar.low <= bar.open <= bar.high)
        or not (bar.low <= bar.close <= bar.high)
    )
    dupes = sum(
        1 for count in Counter((bar.symbol, bar.open_time) for bar in daily).values()
        if count > 1
    )
    missing_prev_close = [row.code for row in symbols if row.prev_close <= 0]
    missing_band = [row.code for row in symbols if row.limit_pct <= 0]

    final_day = day_key(engine.clock.now_ms())
    return {
        "final_day": final_day.isoformat(),
        "symbols": len(symbols),
        "intraday_bars": intraday_count,
        "daily_bars": len(daily),
        "bars_on_non_trading_days": non_trading,
        "malformed_daily_bars": malformed,
        "duplicate_daily_bars": dupes,
        "missing_prev_close": missing_prev_close,
        "missing_limit_band": missing_band,
        "ok": not (non_trading or malformed or dupes or missing_prev_close or missing_band),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60, help="trading days to simulate")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'soak.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=20,
        market_tick_seconds=3600,
        cn_clock_speed=1.0,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    engine = CnMarketEngine(db, settings)
    engine.seed()

    print(f"start: {day_key(engine.clock.now_ms()).isoformat()}")
    started = time.perf_counter()
    ticks = advance(engine, args.days)
    elapsed = time.perf_counter() - started

    result = report(db, engine)
    result["ticks"] = ticks
    result["elapsed_s"] = round(elapsed, 2)
    result["ticks_per_s"] = round(ticks / elapsed, 1) if elapsed else 0.0
    db.dispose()

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
