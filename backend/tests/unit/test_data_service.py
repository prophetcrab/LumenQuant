from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.modules.data.service import DataService


def make_csv(rows: list[tuple]) -> str:
    header = "open_time,open,high,low,close,volume\n"
    return header + "\n".join(
        f"{int(t)},{o},{h},{low},{c},{v}" for t, o, h, low, c, v in rows
    )


def test_import_dedupes_and_reports( database):
    csv_text = make_csv(
        [
            (0, 100, 110, 95, 105, 10),
            (900_000, 105, 115, 100, 110, 12),
            (900_000, 105, 115, 100, 110, 12),  # duplicate
            (1_800_000, 110, 112, 108, 109, 8),
        ]
    )
    with database.session() as session:
        report = DataService(session).import_csv("BTCUSDT", timeframe="15m", content=csv_text)
        assert report.imported == 3
        assert report.skipped == 1
        assert DataService(session).quality("BTCUSDT").bars == 3


def test_invalid_rows_are_skipped(database):
    csv_text = make_csv(
        [
            (0, 100, 110, 95, 105, 10),
            (900_000, 100, 90, 95, 105, 10),  # high < close
            (1_800_000, 0, 1, 0.5, 1, 1),  # zero price
            (2_700_000, 100, 110, 95, 105, 10),
        ]
    )
    with database.session() as session:
        report = DataService(session).import_csv("ETHUSDT", timeframe="15m", content=csv_text)
        assert report.imported == 2
        assert report.skipped == 2
        assert len(report.errors) == 2


def test_replace_and_export_roundtrip(database):
    first = make_csv([(0, 1, 2, 0.5, 1.5, 1)])
    second = make_csv([(0, 10, 12, 9, 11, 3), (900_000, 11, 13, 10, 12, 4)])
    with database.session() as session:
        service = DataService(session)
        service.import_csv("SOLUSDT", timeframe="15m", content=first)
        report = service.import_csv("SOLUSDT", timeframe="15m", content=second, replace=True)
        assert report.imported == 2
        exported = service.export_csv("SOLUSDT", timeframe="15m")
        lines = exported.strip().splitlines()
        assert lines[0] == "open_time,open,high,low,close,volume"
        assert len(lines) == 3
        assert lines[1].startswith("0,10.0,12.0")


def test_timeframe_and_content_validation(database):
    with database.session() as session:
        service = DataService(session)
        with pytest.raises(ValidationError):
            service.import_csv("BTCUSDT", timeframe="1h", content=make_csv([(0, 1, 2, 0.5, 1.5, 1)]))
        with pytest.raises(ValidationError):
            service.import_csv("BTCUSDT", timeframe="15m", content="")
        with pytest.raises(ValidationError):
            service.import_csv("BTCUSDT", timeframe="15m", content="a,b\n1,2\n")


def test_gap_detection(database):
    csv_text = make_csv(
        [
            (0, 100, 110, 95, 105, 10),
            (3_600_000, 105, 115, 100, 110, 12),  # 3 missing 15m bars
        ]
    )
    with database.session() as session:
        DataService(session).import_csv("BNBUSDT", timeframe="15m", content=csv_text)
        report = DataService(session).quality("BNBUSDT")
        assert report.gaps == 3
        assert report.non_monotonic == 0
        assert report.invalid_ohlc == 0


def _cn_daily(session, symbol: str, days: list[tuple[int, int, int, int, int, float]]) -> None:
    from app.core.config import Settings  # noqa: F401
    from app.modules.cn.calendar import CN_TZ
    from app.modules.market.models import Candle, Symbol
    from datetime import datetime

    if session.query(Symbol).filter_by(code=symbol).first() is None:
        session.add(
            Symbol(
                code=symbol, name=symbol, asset_class="stock", market="cn_stock",
                exchange="SH", board="main", base=symbol.split(".")[0], quote="CNY",
                tick_size=0.01, lot_size=100.0, initial_price=100.0,
                prev_close=100.0, limit_pct=0.10,
            )
        )
        session.flush()
    for day_open, o, h, low, c, v in days:
        session.add(
            Candle(
                symbol=symbol, timeframe="1d", open_time=day_open,
                open=o, high=h, low=low, close=c, volume=v,
            )
        )
    session.flush()


def _day_ms(year: int, month: int, day: int) -> int:
    from datetime import datetime

    from app.modules.cn.calendar import CN_TZ

    return int(datetime(year, month, day, tzinfo=CN_TZ).timestamp() * 1000)


def test_daily_gaps_are_not_reported_outside_the_calendar_years(database):
    """2023 history must not look broken just because the holiday table starts in 2024.

    Importing real 2023 data reported four phantom gaps (the Mid-Autumn + National
    Day run), because `trading_days` treats uncovered lunar holidays as weekdays.
    """
    with database.session() as session:
        # 2023-09-28 then 2023-10-09: the exchange was closed in between.
        _cn_daily(
            session,
            "600519.SH",
            [
                (_day_ms(2023, 9, 27), 100, 101, 99, 100.5, 10),
                (_day_ms(2023, 9, 28), 100.5, 101, 99, 100.8, 10),
                (_day_ms(2023, 10, 9), 101, 102, 100, 101.5, 10),
                (_day_ms(2023, 10, 10), 101.5, 102, 100, 101.8, 10),
            ],
        )
        session.commit()
        report = DataService(session).quality("600519.SH", timeframe="1d")
    assert report.bars == 4
    assert report.gaps == 0


def test_daily_gaps_are_still_reported_inside_the_calendar_years(database):
    """Restricting to covered years must not hide a genuine missing session."""
    with database.session() as session:
        # 2026-10-21 (Wed) is removed from an otherwise continuous week.
        _cn_daily(
            session,
            "000001.SZ",
            [
                (_day_ms(2026, 10, 19), 10, 11, 9, 10.5, 10),
                (_day_ms(2026, 10, 20), 10.5, 11, 10, 10.8, 10),
                # 10-21 missing
                (_day_ms(2026, 10, 22), 10.8, 11, 10, 11.0, 10),
            ],
        )
        session.commit()
        report = DataService(session).quality("000001.SZ", timeframe="1d")
    assert report.gaps == 1
