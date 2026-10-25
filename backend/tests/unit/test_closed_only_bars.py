"""`closed_only` must exclude a bar whose interval has not finished.

Real-time feeds update today's daily bar as trades arrive. That row is not a
finished bar: its close still moves, so any caller that reads it produces results
that change with the wall clock. Backtests and strategy signals must not.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.config import Settings
from app.core.db import Database
from app.modules.cn.calendar import CN_TZ, to_ms
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import MarketService

TRADING_DAY = datetime(2026, 10, 21, tzinfo=CN_TZ).date()


def ms(hour: int, minute: int = 0) -> int:
    return to_ms(TRADING_DAY, datetime.min.time().replace(hour=hour, minute=minute))


class FixedClock:
    def __init__(self, now_ms: int) -> None:
        self._now = now_ms

    def now_ms(self) -> int:
        return self._now

    def set(self, value: int) -> None:
        self._now = value


class FakeLive:
    """Live provider that only supplies a clock, no in-progress candle."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock

    def live_candle(self, symbol: str):
        return None


def make_settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'closed.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
    )


@pytest.fixture()
def cn_db(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.database_url)
    db.create_schema()
    with db.session() as session:
        session.add(
            Symbol(
                code="600519.SH",
                name="贵州茅台",
                asset_class="stock",
                market="cn_stock",
                exchange="SH",
                board="main",
                base="600519",
                quote="CNY",
                tick_size=0.01,
                lot_size=100.0,
                initial_price=1000.0,
                prev_close=1000.0,
                limit_pct=0.10,
            )
        )
        # Yesterday's final bar and today's still-forming bar.
        session.add(
            Candle(
                symbol="600519.SH",
                timeframe="1d",
                open_time=ms(0) - 86_400_000,
                open=990.0,
                high=1010.0,
                low=985.0,
                close=1005.0,
                volume=5000.0,
            )
        )
        session.add(
            Candle(
                symbol="600519.SH",
                timeframe="1d",
                open_time=ms(0),
                open=1005.0,
                high=1015.0,
                low=1000.0,
                close=1012.0,
                volume=1200.0,
            )
        )
    yield db
    db.dispose()


def test_today_daily_bar_is_excluded_while_the_session_is_open(cn_db):
    clock = FixedClock(ms(11, 0))
    with cn_db.session() as session:
        market = MarketService(session, live=FakeLive(clock))
        closed = market.candles("600519.SH", timeframe="1d", limit=10, closed_only=True)
        everything = market.candles(
            "600519.SH", timeframe="1d", limit=10, closed_only=False
        )
    assert len(everything) == 2
    assert len(closed) == 1
    # Only yesterday's finished bar survives.
    assert closed[-1].close == pytest.approx(1005.0)


def test_the_same_bar_becomes_available_after_the_close(cn_db):
    clock = FixedClock(ms(15, 1))
    with cn_db.session() as session:
        market = MarketService(session, live=FakeLive(clock))
        closed = market.candles("600519.SH", timeframe="1d", limit=10, closed_only=True)
    assert len(closed) == 2
    assert closed[-1].close == pytest.approx(1012.0)


def test_crypto_15m_bar_is_excluded_until_its_bucket_ends(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.database_url)
    db.create_schema()
    bucket = 1_792_576_800_000  # 2026-10-21 10:00:00 UTC
    with db.session() as session:
        session.add(
            Symbol(
                code="BTCUSDT",
                name="Bitcoin / USDT",
                market="crypto",
                base="BTC",
                quote="USDT",
                initial_price=80_000.0,
            )
        )
        session.add(
            Candle(
                symbol="BTCUSDT",
                timeframe="15m",
                open_time=bucket,
                open=80_000.0,
                high=80_500.0,
                low=79_900.0,
                close=80_200.0,
                volume=10.0,
            )
        )

    clock = FixedClock(bucket + 60_000)  # one minute into the bucket
    with db.session() as session:
        market = MarketService(session, live=FakeLive(clock))
        assert market.candles("BTCUSDT", timeframe="15m", limit=5, closed_only=True) == []

    clock.set(bucket + 15 * 60_000)
    with db.session() as session:
        market = MarketService(session, live=FakeLive(clock))
        closed = market.candles("BTCUSDT", timeframe="15m", limit=5, closed_only=True)
    assert len(closed) == 1
    db.dispose()


def test_next_close_after_answers_per_market(cn_db):
    """The scheduler's clock gate must agree with `closed_only` for both markets.

    A crypto bucket closes one timeframe after it opens. An A-share intraday bar
    closes one step after it opens, but only after a session break; a daily bar
    is final at 15:00 on the following trading day, not at midnight.
    """
    with cn_db.session() as session:
        market = MarketService(session, live=FakeLive(FixedClock(ms(11, 0))))
        # A morning 30m bar (10:00-10:30) is followed by 10:30-11:00.
        assert market.next_close_after("600519.SH", "30m", ms(10, 0)) == ms(11, 0)
        # The 11:30 bar rolls into the afternoon session: 13:00-13:30.
        assert market.next_close_after("600519.SH", "30m", ms(11, 0)) == ms(13, 30)
        # Today's daily bar is not final until the next trading day's 15:00.
        from app.modules.cn.calendar import next_trading_day

        expected = to_ms(
            next_trading_day(TRADING_DAY), datetime.min.time().replace(hour=15)
        )
        assert market.next_close_after("600519.SH", "1d", ms(0)) == expected


def test_next_close_after_for_crypto_is_two_timeframes(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.database_url)
    db.create_schema()
    with db.session() as session:
        session.add(
            Symbol(
                code="BTCUSDT",
                name="Bitcoin / USDT",
                market="crypto",
                base="BTC",
                quote="USDT",
                initial_price=80_000.0,
            )
        )
        session.commit()
        market = MarketService(session)
        start = 1_792_576_800_000
        assert market.next_close_after("BTCUSDT", "15m", start) == start + 2 * 900_000
        assert market.next_close_after("BTCUSDT", "1h", start) == start + 2 * 3_600_000
    db.dispose()


class FakeEngine:
    def __init__(self, now_ms: int) -> None:
        self.clock = FixedClock(now_ms)

    def live_candle(self, symbol: str):
        return None


def test_a_share_bar_is_judged_by_the_a_share_clock(cn_db):
    """The crypto clock must not decide whether an A-share bar has closed.

    Crypto's simulated clock runs at 60x. When both markets are enabled the
    provider's default clock is the crypto one, so using it here declared today's
    A-share daily bar finished hours before the 15:00 close — and `closed_only`
    then handed an in-progress bar to backtests and live signals.
    """
    from app.modules.cn.provider import CompositeLiveProvider

    # Crypto has already "reached" 18:00; the A-share session has not opened yet.
    crypto_now = ms(18, 0)
    cn_now = ms(6, 20)
    provider = CompositeLiveProvider(
        crypto=FakeEngine(crypto_now), cn=FakeEngine(cn_now)
    )
    provider.register("600519.SH", "cn_stock")

    with cn_db.session() as session:
        market = MarketService(session, live=provider)
        closed = market.candles("600519.SH", timeframe="1d", limit=10, closed_only=True)
    # Only yesterday's finished bar; today's is still forming on the A-share clock.
    assert len(closed) == 1
    assert closed[-1].close == pytest.approx(1005.0)

    # Once the A-share clock passes the close, the same bar becomes available.
    provider.cn.clock.set(ms(15, 1))
    with cn_db.session() as session:
        market = MarketService(session, live=provider)
        closed = market.candles("600519.SH", timeframe="1d", limit=10, closed_only=True)
    assert len(closed) == 2
    assert closed[-1].close == pytest.approx(1012.0)


# --------------------------------------------------------------- clock payload
class _FakeQuoteEngine:
    """Minimal stand-in for CnQuoteEngine: the clock only needs now_ms."""

    def __init__(self, now_ms_value: int) -> None:
        self._now = now_ms_value
        self.poll_seconds = 15.0

    def now_ms(self) -> int:
        return self._now


def test_realtime_cn_clock_reports_the_same_shape_as_the_synthetic_one():
    """One frontend card reads both engines' clock payloads.

    The realtime clock used to return only `session_state()`'s keys, so
    `simulated_now` and `speed` were missing whenever A-shares ran on real
    quotes — the footer and market cards then had no timestamp to show. The two
    engines must be interchangeable without the caller branching on which one is
    active.
    """
    from app.modules.cn.quotes import RealtimeSessionClock

    engine = _FakeQuoteEngine(ms(11, 0))
    payload = RealtimeSessionClock(engine).info()

    # Keys the synthetic engine always provides.
    for key in ("simulated_now", "speed", "timeframe", "open", "day", "session"):
        assert key in payload, f"{key} missing from the realtime clock payload"
    assert payload["simulated_now"] == ms(11, 0)
    assert payload["speed"] == 1.0
    # And it says which clock it is, so the UI can label it honestly.
    assert payload["simulated"] is False
    assert payload["source"]


def test_market_clock_speed_is_configurable():
    """It was read via getattr but never declared, so the env var was ignored.

    The synthetic clock was therefore always 60x, and charts drifted days ahead
    of wall time with no way to turn it off short of editing code.
    """
    from app.core.config import Settings

    assert Settings(market_clock_speed=1).market_clock_speed == 1.0
    assert Settings().market_clock_speed == 60.0
