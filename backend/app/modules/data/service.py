"""Bulk OHLCV import/export and data quality checks.

CSV is the lingua franca for research data. Import is strict about ordering and
OHLC consistency: silently accepting bad bars would poison every downstream
backtest.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import ValidationError
from app.modules.market.feed import timeframe_ms
from app.modules.market.models import Candle
from app.modules.market.service import BASE_TIMEFRAME


@dataclass(frozen=True)
class ImportReport:
    symbol: str
    timeframe: str
    imported: int
    skipped: int
    errors: list[str]


@dataclass(frozen=True)
class QualityReport:
    symbol: str
    timeframe: str
    bars: int
    first_time: int
    last_time: int
    duplicate_times: int
    non_monotonic: int
    invalid_ohlc: int
    gaps: int
    expected_step: int


class DataService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def export_csv(self, symbol: str, *, timeframe: str, limit: int = 5000) -> str:
        rows = self.session.scalars(
            select(Candle)
            .where(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe)
            .order_by(Candle.open_time.asc())
            .limit(max(1, min(limit, 100_000)))
        ).all()
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["open_time", "open", "high", "low", "close", "volume"])
        for row in rows:
            writer.writerow([row.open_time, row.open, row.high, row.low, row.close, row.volume])
        return buffer.getvalue()

    def import_csv(
        self,
        symbol: str,
        *,
        timeframe: str,
        content: str,
        replace: bool = False,
    ) -> ImportReport:
        symbol = symbol.upper().strip()
        if not symbol:
            raise ValidationError("symbol is required")
        if timeframe != BASE_TIMEFRAME:
            raise ValidationError(
                f"only base timeframe {BASE_TIMEFRAME} can be imported; "
                "other timeframes are aggregated on read"
            )
        if not content.strip():
            raise ValidationError("CSV content is empty")

        reader = csv.DictReader(io.StringIO(content))
        required = {"open_time", "open", "high", "low", "close"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise ValidationError(
                f"CSV header must contain {', '.join(sorted(required))} (volume optional)"
            )

        step = timeframe_ms(timeframe)
        prepared: list[dict] = []
        errors: list[str] = []
        skipped = 0
        for line_no, row in enumerate(reader, start=2):
            try:
                open_time = int(float(row["open_time"]))
                o = float(row["open"])
                h = float(row["high"])
                low = float(row["low"])
                c = float(row["close"])
                v = float(row.get("volume") or 0.0)
            except (TypeError, ValueError, KeyError):
                skipped += 1
                if len(errors) < 5:
                    errors.append(f"line {line_no}: unparseable values")
                continue
            if open_time % step != 0:
                open_time = open_time - (open_time % step)
            if min(o, h, low, c) <= 0 or h < max(o, c) or low > min(o, c):
                skipped += 1
                if len(errors) < 5:
                    errors.append(f"line {line_no}: invalid OHLC")
                continue
            prepared.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "open_time": open_time,
                    "open": o,
                    "high": h,
                    "low": low,
                    "close": c,
                    "volume": max(v, 0.0),
                }
            )

        if not prepared:
            raise ValidationError("no valid rows found in CSV")

        if replace:
            self.session.execute(
                delete(Candle).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
            )
        existing = set(
            self.session.scalars(
                select(Candle.open_time).where(
                    Candle.symbol == symbol, Candle.timeframe == timeframe
                )
            ).all()
        )
        fresh: list[dict] = []
        for row in prepared:
            if row["open_time"] in existing:
                skipped += 1
                continue
            existing.add(row["open_time"])
            fresh.append(row)

        for start in range(0, len(fresh), 2000):
            self.session.execute(insert(Candle), fresh[start : start + 2000])
        record_audit(
            self.session,
            action="data.imported",
            entity="symbol",
            entity_id=symbol,
            detail=f'{{"timeframe":"{timeframe}","rows":{len(fresh)},"replace":{str(replace).lower()}}}',
        )
        self.session.commit()
        return ImportReport(
            symbol=symbol,
            timeframe=timeframe,
            imported=len(fresh),
            skipped=skipped,
            errors=errors,
        )

    def quality(self, symbol: str, *, timeframe: str = BASE_TIMEFRAME) -> QualityReport:
        rows = self.session.scalars(
            select(Candle)
            .where(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe)
            .order_by(Candle.open_time.asc())
        ).all()
        step = timeframe_ms(timeframe)
        times = [row.open_time for row in rows]
        duplicates = len(times) - len(set(times))
        non_monotonic = sum(1 for i in range(1, len(times)) if times[i] <= times[i - 1])
        invalid = sum(
            1
            for row in rows
            if min(row.open, row.high, row.low, row.close) <= 0
            or row.high < max(row.open, row.close)
            or row.low > min(row.open, row.close)
        )
        gaps = 0
        for i in range(1, len(times)):
            delta = times[i] - times[i - 1]
            if delta > step:
                gaps += int(delta // step) - 1
        return QualityReport(
            symbol=symbol.upper(),
            timeframe=timeframe,
            bars=len(rows),
            first_time=times[0] if times else 0,
            last_time=times[-1] if times else 0,
            duplicate_times=duplicates,
            non_monotonic=non_monotonic,
            invalid_ohlc=invalid,
            gaps=gaps,
            expected_step=step,
        )

    def coverage(self) -> list[dict]:
        rows = self.session.execute(
            select(
                Candle.symbol,
                Candle.timeframe,
                func.count(Candle.id),
                func.min(Candle.open_time),
                func.max(Candle.open_time),
            ).group_by(Candle.symbol, Candle.timeframe)
        ).all()
        return [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "bars": bars,
                "first_time": first,
                "last_time": last,
            }
            for symbol, timeframe, bars, first, last in rows
        ]
