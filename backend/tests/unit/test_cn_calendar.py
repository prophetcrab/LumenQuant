"""A-share calendar: fixed dates, moveable holidays, and uncovered years."""

from __future__ import annotations

from datetime import date

from app.modules.cn.calendar import (
    CALENDAR_YEARS,
    calendar_coverage,
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    trading_days,
)


def test_weekends_are_closed():
    assert not is_trading_day(date(2026, 10, 24))  # Saturday
    assert not is_trading_day(date(2026, 10, 25))  # Sunday
    assert is_trading_day(date(2026, 10, 23))      # Friday


def test_tabulated_holidays_are_closed():
    assert not is_trading_day(date(2026, 1, 1))    # New Year
    assert not is_trading_day(date(2026, 10, 5))   # National Day week
    assert not is_trading_day(date(2025, 1, 29))   # Spring Festival


def test_uncovered_year_still_closes_fixed_holidays():
    """2027 has no holiday table yet; statutory fixed dates must still close.

    Regression: 2027-01-01 was reported as a trading day, so the session clock
    would have opened a phantom session on New Year's Day.
    """
    assert 2027 not in CALENDAR_YEARS
    assert not is_trading_day(date(2027, 1, 1))
    assert not is_trading_day(date(2027, 5, 1))
    assert not is_trading_day(date(2027, 10, 1))
    assert not is_trading_day(date(2027, 10, 2))
    assert not is_trading_day(date(2027, 10, 3))
    # An ordinary weekday is still tradable.
    assert is_trading_day(date(2027, 3, 2))


def test_coverage_reports_the_gap_instead_of_guessing():
    covered = calendar_coverage(date(2026, 10, 21))
    assert covered["covered"] is True
    assert covered["warning"] == ""
    assert 2026 in covered["covered_years"]

    uncovered = calendar_coverage(date(2027, 2, 8))
    assert uncovered["covered"] is False
    assert "2027" in uncovered["warning"]
    assert "LQ_CN_EXTRA_HOLIDAYS" in uncovered["warning"]


def test_rollover_across_year_boundary_skips_new_year():
    # 2026-12-31 is a Thursday, 2027-01-01 a Friday holiday, then a weekend.
    nxt = next_trading_day(date(2026, 12, 31))
    assert nxt == date(2027, 1, 4)
    prev = prev_trading_day(date(2027, 1, 4))
    assert prev == date(2026, 12, 31)


def test_spring_festival_gap_is_skipped_when_tabulated():
    days = trading_days(date(2026, 2, 13), date(2026, 2, 24))
    # 2026-02-15..2026-02-21 are closed for Spring Festival.
    assert date(2026, 2, 16) not in days
    assert date(2026, 2, 13) in days


def test_spring_festival_2026_runs_through_feb_23():
    """Real exchange data showed no bars from Feb 16 to Feb 23, resuming Feb 24.

    The provisional list stopped at Feb 21, so Monday Feb 23 looked tradable and
    every data-quality check reported a phantom gap there.
    """
    for day in range(16, 24):
        assert not is_trading_day(date(2026, 2, day)), day
    assert is_trading_day(date(2026, 2, 24))
    # The day before the holiday week still trades.
    assert is_trading_day(date(2026, 2, 13))
