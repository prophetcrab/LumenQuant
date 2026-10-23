from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.modules.cn.calendar import (
    day_key,
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


def test_custom_holiday_can_be_added(monkeypatch):
    # add_holidays() mutates a module-level set. Without isolating it here the
    # date stays a holiday for every later test in the same process, which made
    # unrelated session/quote tests fail depending on file order.
    from app.modules.cn import calendar as calendar_module

    monkeypatch.setattr(
        calendar_module, "_HOLIDAYS", set(calendar_module._HOLIDAYS)
    )
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


def test_prev_close_rolls_to_the_previous_trading_day_close(tmp_path):
    """`prev_close` is the limit band's reference and must advance each session.

    The engine only synced it once at seed time, so after the seed date every
    session's ±10% band was measured against a price from days ago — a stock could
    look "near limit-up" (or never reachable) for reasons unrelated to today.
    """
    from app.core.db import Database
    from app.core.config import Settings
    from app.modules.cn.engine import CnMarketEngine
    from app.modules.market.models import Candle, Symbol
    from app.modules.cn.calendar import CN_TZ
    from datetime import date, datetime
    from sqlalchemy import select

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'roll.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_seed_days=5,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    engine = CnMarketEngine(db, settings)
    engine.seed()

    with db.session() as session:
        row = session.scalar(select(Symbol).where(Symbol.code == "600519.SH"))
        previous = session.scalar(
            select(Candle)
            .where(Candle.symbol == row.code, Candle.timeframe == "1d")
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
        # Target the day AFTER the newest seeded bar: that is exactly the rollover
        # this method serves. Hard-coding a date made the test depend on the wall
        # clock — it passed on 2026-10-22 and failed the next day, when the seed
        # itself already included that date.
        previous.close = 1500.0
        target = day_key(previous.open_time) + timedelta(days=1)
        row.prev_close = 1631.6
        session.commit()

        engine._roll_prev_close(session, [row], target)

        assert row.prev_close == pytest.approx(1500.0)
        assert engine._prev_close[row.code] == pytest.approx(1500.0)
    db.dispose()


def test_prev_close_roll_ignores_todays_partial_bar(tmp_path):
    """Today's daily row is still being written; using it would move the band.

    The daily bar for the current session is updated in place as intraday bars
    close, so "the latest daily close" is a moving target. The roll must take the
    last close strictly *before* today.
    """
    from app.core.db import Database
    from app.core.config import Settings
    from app.modules.cn.engine import CnMarketEngine
    from app.modules.market.models import Candle, Symbol
    from app.modules.cn.calendar import CN_TZ
    from datetime import date, datetime
    from sqlalchemy import select

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'roll2.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_seed_days=5,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    engine = CnMarketEngine(db, settings)
    engine.seed()

    with db.session() as session:
        row = session.scalar(select(Symbol).where(Symbol.code == "600519.SH"))
        newest = session.scalar(
            select(Candle)
            .where(Candle.symbol == row.code, Candle.timeframe == "1d")
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
        target = day_key(newest.open_time) + timedelta(days=1)
        day_start = int(
            datetime.combine(target, datetime.min.time(), tzinfo=CN_TZ).timestamp() * 1000
        )
        # A partial bar for the target day, priced very differently.
        session.add(
            Candle(
                symbol=row.code,
                timeframe="1d",
                open_time=day_start,
                open=1900.0, high=1900.0, low=1900.0, close=1900.0, volume=1.0,
            )
        )
        session.commit()
        engine._roll_prev_close(session, [row], target)
        # 1900 must NOT become the reference; yesterday's real close does.
        assert row.prev_close != pytest.approx(1900.0)
    db.dispose()
