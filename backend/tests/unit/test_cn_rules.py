from __future__ import annotations

from datetime import date

import pytest

from app.modules.cn.calendar import (
    add_holidays,
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    session_bounds,
    session_state,
    sessions,
    trading_days,
    to_ms,
    MORNING,
)
from app.modules.cn.fees import CnFeeSchedule
from app.modules.cn.rules import (
    price_band,
    round_tick,
    validate_limit_price,
    validate_quantity,
)


def test_weekend_and_holiday_are_closed():
    assert is_trading_day(date(2026, 10, 20)) is True  # Tuesday
    assert is_trading_day(date(2026, 10, 17)) is False  # Saturday
    assert is_trading_day(date(2026, 10, 1)) is False  # National Day


def test_trading_day_navigation_skips_weekends():
    assert prev_trading_day(date(2026, 10, 19)) == date(2026, 10, 16)  # Mon -> Fri
    assert next_trading_day(date(2026, 10, 16)) == date(2026, 10, 19)  # Fri -> Mon


def test_sessions_cover_morning_and_afternoon():
    day = date(2026, 10, 20)
    starts = sessions(day)
    assert len(starts) == 2
    assert starts[0][1] - starts[0][0] == 2 * 3600 * 1000
    assert starts[1][1] - starts[1][0] == 2 * 3600 * 1000


def test_session_bounds_and_state():
    day = date(2026, 10, 20)
    open_ms = to_ms(day, MORNING[0])
    assert session_bounds(open_ms + 1000) is not None
    assert session_bounds(open_ms - 1000) is None  # before open
    lunch = open_ms + 2 * 3600 * 1000 + 60_000
    assert session_bounds(lunch) is None  # lunch break
    state = session_state(lunch)
    assert state.open is False
    assert state.next_open_ms > lunch


def test_custom_holiday_can_be_added():
    add_holidays(["2026-10-21"])
    assert is_trading_day(date(2026, 10, 21)) is False


def test_price_band_by_board():
    main = price_band(10.00, "main")
    assert main.up == pytest.approx(11.00)
    assert main.down == pytest.approx(9.00)
    gem = price_band(10.00, "gem")
    assert gem.up == pytest.approx(12.00)
    star = price_band(10.00, "star")
    assert star.up == pytest.approx(12.00)
    bse = price_band(10.00, "bse")
    assert bse.up == pytest.approx(13.00)
    st = price_band(10.00, "main", is_st=True)
    assert st.up == pytest.approx(10.50)


def test_price_band_rounds_to_tick():
    band = price_band(10.03, "main")
    assert band.up == pytest.approx(11.03)
    assert round_tick(10.034, 0.01) == pytest.approx(10.03)


def test_limit_price_validation():
    band = price_band(10.00, "main")
    validate_limit_price(10.50, band)
    with pytest.raises(Exception):
        validate_limit_price(12.00, band)
    with pytest.raises(Exception):
        validate_limit_price(8.00, band)


def test_quantity_lot_rules():
    validate_quantity("buy", 300, position_quantity=0, available_quantity=0)
    with pytest.raises(Exception):
        validate_quantity("buy", 150, position_quantity=0, available_quantity=0)
    # Odd-lot sell is only allowed as a full exit.
    validate_quantity("sell", 150, position_quantity=150, available_quantity=150)
    with pytest.raises(Exception):
        validate_quantity("sell", 150, position_quantity=450, available_quantity=450)
    with pytest.raises(Exception):
        validate_quantity("sell", 300, position_quantity=300, available_quantity=100)


def test_cn_fee_asymmetry():
    fees = CnFeeSchedule()
    buy = fees.breakdown("buy", 10_000)
    sell = fees.breakdown("sell", 10_000)
    assert buy["stamp_duty"] == 0.0
    assert sell["stamp_duty"] > 0
    assert sell["total"] > buy["total"]
    # Minimum commission dominates small tickets.
    small = fees.breakdown("buy", 1_000)
    assert small["commission"] == pytest.approx(5.0)


def test_cn_fee_zero_notional():
    fees = CnFeeSchedule()
    assert fees.total("buy", 0) == 0.0


def test_cn_daily_import_allowed_and_other_timeframes_rejected(database):
    from app.modules.data.service import DataService
    from app.modules.market.service import MarketService

    with database.session() as session:
        market = MarketService(session)
        market.add_symbol(
            code="600000.SH", name="浦发银行", initial_price=10.0,
            market="cn_stock", board="main", exchange="SH",
        )
        service = DataService(session)
        csv_text = "open_time,open,high,low,close,volume\n0,10,10.2,9.9,10.1,1000000\n86400000,10.1,10.3,10.0,10.2,1200000\n"
        report = service.import_csv("600000.SH", timeframe="1d", content=csv_text)
        assert report.imported == 2
        with pytest.raises(Exception):
            service.import_csv("600000.SH", timeframe="1h", content=csv_text)
