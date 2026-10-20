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
