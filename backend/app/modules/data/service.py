"""Bulk OHLCV import/export and data quality checks.

CSV is the lingua franca for research data. Import is strict about ordering and
OHLC consistency: silently accepting bad bars would poison every downstream
backtest.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.errors import ValidationError
from app.modules.market.feed import timeframe_ms
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import BASE_TIMEFRAME, SUPPORTED_TIMEFRAMES


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
        # A symbol's own base timeframe is always importable; A-shares also accept
        # native daily bars, which is what real exchange exports provide.
        allowed = set(SUPPORTED_TIMEFRAMES)
        base_tf = BASE_TIMEFRAME
        market = "crypto"
        row_symbol = self.session.scalar(select(Symbol).where(Symbol.code == symbol))
        if row_symbol is not None:
            base_tf = row_symbol.base_timeframe or BASE_TIMEFRAME
            market = row_symbol.market
        if timeframe not in allowed:
            raise ValidationError(f"timeframe must be one of {', '.join(SUPPORTED_TIMEFRAMES)}")
        if market == "cn_stock" and timeframe not in {base_tf, "1d"}:
            raise ValidationError(
                f"A股只接受基础周期 {base_tf} 或原生日线 1d，其他周期读取时聚合"
            )
        if market != "cn_stock" and timeframe != base_tf:
            raise ValidationError(
                f"only the base timeframe {base_tf} can be imported; "
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
        symbol_row = self.session.scalar(
            select(Symbol).where(Symbol.code == symbol.upper())
        )
        market = symbol_row.market if symbol_row is not None else "crypto"
        gaps = self._count_gaps(times, step=step, timeframe=timeframe, market=market)
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

    @staticmethod
    def _count_gaps(
        times: list[int], *, step: int, timeframe: str, market: str
    ) -> int:
        """Count missing bars, respecting the market's trading calendar.

        A naive `delta > step` test calls every weekend and lunch break a gap.
        On real A-share daily data that produced 341 "gaps" for 158 bars — the
        number was really just the closed days between sessions, and it made a
        clean series look catastrophically broken.
        """
        if len(times) < 2:
            return 0
        if market != "cn_stock":
            gaps = 0
            for i in range(1, len(times)):
                delta = times[i] - times[i - 1]
                if delta > step:
                    gaps += int(delta // step) - 1
            return gaps

        from app.modules.cn.calendar import day_key, sessions, trading_days

        if timeframe == "1d":
            from app.modules.cn.calendar import CALENDAR_YEARS, day_key, trading_days

            first = day_key(times[0])
            last = day_key(times[-1])
            # The holiday table only covers CALENDAR_YEARS. Outside it the moveable
            # lunar holidays are unknown, so "expected trading days" is wrong and
            # every Spring Festival / Mid-Autumn closure looks like missing data —
            # imported 2023 history reported four phantom gaps for the Mid-Autumn +
            # National Day run. Gap counting is therefore restricted to the covered
            # window; `calendar_coverage()` tells the user the rest is unverified.
            in_window = [ts for ts in times if day_key(ts).year in CALENDAR_YEARS]
            if len(in_window) < 2:
                return 0
            window_first = day_key(in_window[0])
            window_last = day_key(in_window[-1])
            expected_days = [
                day
                for day in trading_days(window_first, window_last)
                if day.year in CALENDAR_YEARS
            ]
            return max(0, len(expected_days) - len(in_window))

        # Intraday: expected bars exist only inside a session. Count the bars each
        # trading day should contain and compare with what is stored, so the
        # overnight gap and the lunch break are not treated as missing data.
        per_day: dict[str, int] = {}
        for ts in times:
            key = day_key(ts).isoformat()
            per_day[key] = per_day.get(key, 0) + 1
        if not sessions(day_key(times[0])):
            return 0
        # The first and last days are partial by construction: the fetch window
        # may start mid-session, and "today" is still running. Counting them
        # reports bars that were never supposed to exist yet.
        ordered_days = sorted(per_day)
        missing = 0
        for day_iso in ordered_days[1:-1]:
            from datetime import date

            day = date.fromisoformat(day_iso)
            expected = max(0, sum((end - start) // step for start, end in sessions(day)))
            missing += max(0, expected - per_day[day_iso])
        return missing

    def delete_series(self, symbol: str, *, timeframe: str) -> int:
        """Drop every stored bar for one (symbol, timeframe).

        The only way to fix a bad import was to overwrite it with a good one. When
        the data is simply wrong (wrong symbol, unwanted period), there was no way
        to remove it — and a stale series keeps feeding backtests and the A-share
        daily close (prev_close) that price limits are computed from.
        """
        code = symbol.upper().strip()
        if not code:
            raise ValidationError("symbol is required")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValidationError(
                f"timeframe must be one of {', '.join(SUPPORTED_TIMEFRAMES)}"
            )
        result = self.session.execute(
            delete(Candle).where(Candle.symbol == code, Candle.timeframe == timeframe)
        )
        removed = int(result.rowcount or 0)
        if removed:
            # prev_close and limit bands are derived from the daily series; a
            # deleted series must not leave the symbol quoting against a bar that
            # no longer exists.
            row = self.session.scalar(select(Symbol).where(Symbol.code == code))
            if row is not None and timeframe == "1d":
                latest = self.session.scalar(
                    select(Candle.close)
                    .where(Candle.symbol == code, Candle.timeframe == "1d")
                    .order_by(Candle.open_time.desc())
                    .limit(1)
                )
                if latest:
                    row.prev_close = float(latest)
            record_audit(
                self.session,
                action="data.series_deleted",
                entity="symbol",
                entity_id=code,
                detail=json.dumps({"timeframe": timeframe, "bars": removed}),
            )
        self.session.commit()
        return removed

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
