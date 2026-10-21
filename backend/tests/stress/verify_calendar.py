"""Check the hard-coded A-share holiday table against real exchange data.

The holiday list in `app/modules/cn/calendar.py` is maintained by hand and will
drift. Real daily bars are the exchange's own answer, so any disagreement is
either a bug in our table or in the data source — both worth knowing.

Both directions are reported:

* calendar says trading, no bar exists  → we are missing a holiday
* calendar says closed, a bar exists    → we invented a holiday

Usage:
  python tests/stress/verify_calendar.py [--codes 600519.SH,600036.SH] [--bars 500]

Exit code 1 when any disagreement is found. A handful of days missing at the
very end can be normal (today's bar may not be published until after the close),
so the last day is excluded from the report.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.modules.cn.calendar import day_key, is_trading_day  # noqa: E402
from app.modules.cn.engine import CnMarketEngine  # noqa: E402
from app.modules.cn.sources.tencent import TencentSource  # noqa: E402
from app.modules.cn.sync import CnDataSyncService  # noqa: E402
from app.modules.market.models import Candle  # noqa: E402

DEFAULT_CODES = "600519.SH,600036.SH,000001.SZ"


def fetch_days(code: str, bars: int) -> set[date]:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'cal.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_data_source="tencent",
        cn_sync_on_start=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    CnMarketEngine(db, settings).ensure_symbols()
    try:
        with db.session() as session:
            CnDataSyncService(session, TencentSource()).sync_symbols(
                [code], daily_limit=bars, intraday_limit=0
            )
            rows = session.scalars(
                select(Candle).where(
                    Candle.symbol == code, Candle.timeframe == "1d"
                )
            ).all()
            return {day_key(row.open_time) for row in rows}
    finally:
        db.dispose()


def compare(code: str, days: set[date], *, skip_last: bool = True) -> dict:
    if not days:
        return {"code": code, "bars": 0, "missing": [], "phantom": [], "error": "no data"}
    start, end = min(days), max(days)
    missing: list[date] = []
    phantom: list[date] = []
    cursor = start
    last = end - timedelta(days=1) if skip_last else end
    while cursor <= last:
        if cursor.weekday() < 5:
            expected = is_trading_day(cursor)
            has_bar = cursor in days
            if expected and not has_bar:
                missing.append(cursor)
            elif not expected and has_bar:
                phantom.append(cursor)
        elif cursor in days:
            phantom.append(cursor)  # a bar on a Saturday/Sunday is always wrong
        cursor += timedelta(days=1)
    return {
        "code": code,
        "bars": len(days),
        "range": f"{start.isoformat()}..{end.isoformat()}",
        "missing": [d.isoformat() for d in missing],
        "phantom": [d.isoformat() for d in phantom],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default=DEFAULT_CODES)
    parser.add_argument("--bars", type=int, default=500)
    args = parser.parse_args()

    failures = 0
    for code in [item.strip().upper() for item in args.codes.split(",") if item.strip()]:
        days = fetch_days(code, args.bars)
        result = compare(code, days)
        print(result)
        if result.get("missing") or result.get("phantom") or result.get("error"):
            failures += 1

    if failures:
        print(
            f"\n{failures} symbol(s) disagree with the calendar. "
            "Add missing dates to LQ_CN_EXTRA_HOLIDAYS or edit _HOLIDAYS in "
            "app/modules/cn/calendar.py."
        )
        return 1
    print("\ncalendar matches real exchange data for every checked symbol")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
