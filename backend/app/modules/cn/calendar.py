"""A-share trading calendar and session clock.

China Standard Time is a fixed UTC+8 offset (no DST), so a plain timezone is
used instead of depending on an IANA tz database being present on Windows.

Holidays for 2024-2025 follow the published exchange calendars. The 2026 list
is provisional and can be replaced at runtime with LQ_CN_EXTRA_HOLIDAYS; a real
deployment should load the exchange calendar each December.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
DAY_MS = 86_400_000

MORNING = (dtime(9, 30), dtime(11, 30))
AFTERNOON = (dtime(13, 0), dtime(15, 0))

_HOLIDAYS: set[str] = {
    # 2024
    "2024-01-01",
    "2024-02-09", "2024-02-10", "2024-02-11", "2024-02-12", "2024-02-13",
    "2024-02-14", "2024-02-15", "2024-02-16", "2024-02-17",
    "2024-04-04", "2024-04-05", "2024-04-06",
    "2024-05-01", "2024-05-02", "2024-05-03", "2024-05-04", "2024-05-05",
    "2024-06-10",
    "2024-09-15", "2024-09-16", "2024-09-17",
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04",
    "2024-10-05", "2024-10-06", "2024-10-07",
    # 2025
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
    "2025-05-31", "2025-06-01", "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
    "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026 (provisional; override with LQ_CN_EXTRA_HOLIDAYS)
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-02-19", "2026-02-20", "2026-02-21",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}


def add_holidays(values: list[str]) -> None:
    """Allow deployments to extend the calendar (weekday closures, typhoons)."""
    for value in values:
        value = value.strip()
        if value:
            _HOLIDAYS.add(value)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in _HOLIDAYS


def trading_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def prev_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(370):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    raise ValueError("no trading day found within a year")


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(370):
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    raise ValueError("no trading day found within a year")


def day_key(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, CN_TZ).date()


def to_ms(day: date, clock: dtime) -> int:
    return int(datetime.combine(day, clock, tzinfo=CN_TZ).timestamp() * 1000)


def sessions(day: date) -> list[tuple[int, int]]:
    if not is_trading_day(day):
        return []
    return [(to_ms(day, MORNING[0]), to_ms(day, MORNING[1])), (to_ms(day, AFTERNOON[0]), to_ms(day, AFTERNOON[1]))]


def session_bounds(ts_ms: int) -> tuple[int, int] | None:
    """Return the (open, close) of the session containing ts_ms, or None."""
    day = day_key(ts_ms)
    for start, end in sessions(day):
        if start <= ts_ms < end:
            return start, end
    return None


def is_session_open(ts_ms: int | None = None) -> bool:
    ts_ms = int(time.time() * 1000) if ts_ms is None else ts_ms
    return session_bounds(ts_ms) is not None


def next_open_ms(ts_ms: int) -> int:
    """First session open strictly after ts_ms."""
    day = day_key(ts_ms)
    for start, _end in sessions(day):
        if start > ts_ms:
            return start
    cursor = next_trading_day(day)
    for _ in range(370):
        day_sessions = sessions(cursor)
        if day_sessions:
            return day_sessions[0][0]
        cursor = next_trading_day(cursor)
    raise ValueError("no session found within a year")


def prev_close_ms(ts_ms: int) -> int:
    """Last session close at or before ts_ms (used for daily settlement)."""
    day = day_key(ts_ms)
    for _ in range(370):
        day_sessions = sessions(day)
        for _start, end in reversed(day_sessions):
            if end <= ts_ms:
                return end
        day = prev_trading_day(day)
    raise ValueError("no session found within a year")


@dataclass(frozen=True)
class SessionState:
    open: bool
    day: str
    session: str
    session_open_ms: int
    session_close_ms: int
    next_open_ms: int

    def as_dict(self) -> dict:
        return {
            "open": self.open,
            "day": self.day,
            "session": self.session,
            "session_open": self.session_open_ms,
            "session_close": self.session_close_ms,
            "next_open": self.next_open_ms,
        }


def session_state(ts_ms: int | None = None) -> SessionState:
    ts_ms = int(time.time() * 1000) if ts_ms is None else int(ts_ms)
    day = day_key(ts_ms)
    bounds = session_bounds(ts_ms)
    if bounds is not None:
        label = "morning" if bounds[0] == to_ms(day, MORNING[0]) else "afternoon"
        return SessionState(
            open=True,
            day=day.isoformat(),
            session=label,
            session_open_ms=bounds[0],
            session_close_ms=bounds[1],
            # While open this must be the *next session's* open, not this
            # session's close — callers schedule the next evaluation with it.
            next_open_ms=next_open_ms(bounds[1]),
        )
    return SessionState(
        open=False,
        day=day.isoformat(),
        session="closed",
        session_open_ms=0,
        session_close_ms=0,
        next_open_ms=next_open_ms(ts_ms),
    )


class CnSessionClock:
    """Simulated clock that only advances while the market is open.

    Outside sessions it jumps forward: lunch -> 13:00, after close -> next
    trading day 09:30. This keeps the demo moving at 60x without waiting
    overnight, while bars are still produced only at real session timestamps.
    """

    def __init__(self, speed: float = 60.0, *, start_ms: int | None = None) -> None:
        self.speed = max(0.001, float(speed))
        self._last_real = time.monotonic()
        self._cursor = start_ms if start_ms is not None else self._initial_cursor()

    def _initial_cursor(self) -> int:
        now = int(time.time() * 1000)
        day = day_key(now)
        # Start at the open of today when today trades, otherwise the next open.
        if is_trading_day(day):
            return sessions(day)[0][0]
        return sessions(next_trading_day(day))[0][0]

    def _advance(self, cursor: int, remaining_ms: int) -> int:
        guard = 0
        while remaining_ms > 0 and guard < 5000:
            guard += 1
            bounds = session_bounds(cursor)
            if bounds is None:
                cursor = next_open_ms(cursor)
                continue
            room = bounds[1] - cursor
            step = min(room, remaining_ms)
            cursor += step
            remaining_ms -= step
            if cursor >= bounds[1]:
                cursor = next_open_ms(cursor)
        # Sub-second leftovers are dropped at a session boundary; that is
        # intentional, otherwise we would create timestamps outside sessions.
        if session_bounds(cursor) is None:
            cursor = next_open_ms(cursor)
        return cursor

    def now_ms(self) -> int:
        real = time.monotonic()
        delta = int((real - self._last_real) * 1000 * self.speed)
        self._last_real = real
        if delta > 0:
            self._cursor = self._advance(self._cursor, delta)
        return self._cursor

    def ensure_at_least(self, ts_ms: int) -> None:
        if ts_ms >= self._cursor:
            self._cursor = next_open_ms(ts_ms) if session_bounds(ts_ms) is None else ts_ms

    def info(self) -> dict:
        state = session_state(self._cursor)
        return {
            "simulated_now": self._cursor,
            "speed": self.speed,
            "timeframe": "15m",
            "market": "cn_stock",
            **state.as_dict(),
        }
