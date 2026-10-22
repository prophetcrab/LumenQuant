"""Realtime quote polling: batch parsing, live-bar accounting, persistence.

The tests drive a deterministic clock and a fake source, so no network is
required and every timestamp is exact.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.modules.cn.calendar import CN_TZ, to_ms
from app.modules.cn.quotes import CnQuoteEngine
from app.modules.cn.sources.base import Quote
from app.modules.cn.sources.tencent import TencentSource
from app.modules.market.models import Candle, Symbol

# 2026-10-21 is a Wednesday and not a holiday in the tabulated calendar.
TRADING_DAY = datetime(2026, 10, 21, tzinfo=CN_TZ).date()


def at(hour: int, minute: int, second: int = 0) -> int:
    return to_ms(TRADING_DAY, datetime.min.time().replace(hour=hour, minute=minute, second=second))


class FakeSource:
    name = "fake"

    def __init__(self) -> None:
        self.payload: dict[str, Quote] = {}
        self.calls = 0
        self.error: Exception | None = None
        # 15m bars the intraday endpoint would return; empty means "unavailable".
        self.intraday: dict[str, list] = {}

    def quotes(self, codes):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {code: self.payload[code] for code in codes if code in self.payload}

    def intraday_bars(self, code, *, limit=320):
        from app.modules.cn.sources.base import BarBatch

        bars = self.intraday.get(code.upper(), [])
        return BarBatch(code=code.upper(), timeframe="15m", bars=list(bars))


def quote(code: str, price: float, volume: float, ts_ms: int, prev_close: float = 10.0) -> Quote:
    return Quote(
        code=code,
        name=code,
        price=price,
        prev_close=prev_close,
        open=10.0,
        high=max(price, 10.0),
        low=min(price, 10.0),
        volume=volume,
        timestamp_ms=ts_ms,
    )


@pytest.fixture()
def clock():
    state = {"now": at(10, 5)}

    def now_ms() -> int:
        return state["now"]

    return state, now_ms


@pytest.fixture()
def setup(tmp_path, clock):
    state, now_ms = clock
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'quotes.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        cn_data_source="tencent",
        cn_quote_poll_seconds=5.0,
    )
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
                initial_price=10.0,
                prev_close=10.0,
                limit_pct=0.10,
                base_timeframe="15m",
            )
        )
    source = FakeSource()
    engine = CnQuoteEngine(db, settings, source=source, now_ms=now_ms)
    yield state, source, engine, db
    db.dispose()


def test_first_poll_builds_a_live_bar(setup):
    state, source, engine, _db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])

    assert engine.poll_once() == 1
    bar = engine.live_candle("600519.SH")
    assert bar is not None
    assert bar.time == at(10, 0)
    # Open is the first price actually observed, not the day's open.
    assert bar.open == pytest.approx(10.0)
    assert bar.close == pytest.approx(10.0)


def test_same_bucket_accumulates_high_low_and_volume_delta(setup):
    state, source, engine, _db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])
    engine.poll_once()

    state["now"] = at(10, 10)
    source.payload["600519.SH"] = quote("600519.SH", 10.3, 160.0, state["now"])
    engine.poll_once()

    bar = engine.live_candle("600519.SH")
    assert bar.time == at(10, 0)
    assert bar.high == pytest.approx(10.3)
    assert bar.low == pytest.approx(10.0)
    # Volume is the daily cumulative figure, so the bar gets the delta only.
    assert bar.volume == pytest.approx(60.0)


def test_new_bucket_closes_and_persists_the_previous_bar(setup):
    state, source, engine, db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])
    engine.poll_once()

    state["now"] = at(10, 20)
    source.payload["600519.SH"] = quote("600519.SH", 10.3, 200.0, state["now"])
    engine.poll_once()

    with db.session() as session:
        stored = session.scalars(
            select(Candle).where(
                Candle.symbol == "600519.SH", Candle.timeframe == "15m"
            )
        ).all()
    assert len(stored) == 1
    assert stored[0].open_time == at(10, 0)
    assert stored[0].open == pytest.approx(10.0)
    # The 10:00 bar closed on its last observed price (10.0); 10.3 only arrived
    # once the next bucket had already started.
    assert stored[0].close == pytest.approx(10.0)
    live = engine.live_candle("600519.SH")
    assert live.time == at(10, 15)
    assert live.open == pytest.approx(10.3)


def test_daily_bar_is_written_straight_from_the_quote(setup):
    """The quote *is* today's daily bar, including the exchange's volume total.

    Deriving it by summing accumulated 15m bars loses every trade from before the
    process started — and after a restart it loses them twice.
    """
    state, source, engine, db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 5_000.0, state["now"])
    engine.poll_once()
    state["now"] = at(10, 20)
    source.payload["600519.SH"] = quote("600519.SH", 10.3, 5_120.0, state["now"])
    engine.poll_once()

    with db.session() as session:
        daily = session.scalar(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        )
    assert daily is not None
    assert daily.open == pytest.approx(10.0)
    assert daily.close == pytest.approx(10.3)
    assert daily.high == pytest.approx(10.3)
    assert daily.low == pytest.approx(10.0)
    # Cumulative day volume, not a sum of the bars this process happened to see.
    assert daily.volume == pytest.approx(5_120.0)


def test_daily_row_tracks_the_quote_instead_of_accumulating(setup):
    state, source, engine, db = setup
    day_open = int(datetime.combine(TRADING_DAY, datetime.min.time(), tzinfo=CN_TZ).timestamp() * 1000)
    with db.session() as session:
        session.add(
            Candle(
                symbol="600519.SH",
                timeframe="1d",
                open_time=day_open,
                open=9.8,
                high=10.1,
                low=9.7,
                close=10.0,
                volume=5_000.0,
            )
        )

    source.payload["600519.SH"] = quote("600519.SH", 10.0, 5_000.0, state["now"])
    engine.poll_once()
    state["now"] = at(10, 20)
    source.payload["600519.SH"] = quote("600519.SH", 10.3, 5_120.0, state["now"])
    engine.poll_once()

    with db.session() as session:
        daily = session.scalar(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        )
    # Assign, never add: the exchange total already includes everything.
    assert daily.volume == pytest.approx(5_120.0)
    assert daily.close == pytest.approx(10.3)
    assert daily.high == pytest.approx(10.3)

    state["now"] = at(10, 35)
    source.payload["600519.SH"] = quote("600519.SH", 10.2, 5_160.0, state["now"])
    engine.poll_once()
    with db.session() as session:
        daily = session.scalar(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        )
    assert daily.volume == pytest.approx(5_160.0)
    assert daily.close == pytest.approx(10.2)


def test_no_live_bar_outside_session(setup):
    state, source, engine, _db = setup
    state["now"] = at(16, 0)
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])
    engine.poll_once()

    assert engine.live_candle("600519.SH") is None
    # The quote itself is still available: the last price does not vanish at 15:00.
    assert engine.live_quote("600519.SH").price == pytest.approx(10.0)


def test_stale_quote_is_reported_instead_of_served_as_live(setup):
    state, source, engine, _db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])
    engine.poll_once()
    # 5s poll → 30s in-session tolerance; jump past it without polling again.
    state["now"] = at(10, 6, 30)

    assert engine.live_quote("600519.SH") is None
    rows = engine.snapshot_rows()
    assert rows[0]["fresh"] is False
    assert rows[0]["age_ms"] == 90_000


def test_prev_close_is_persisted_only_when_it_changes(setup):
    _state, source, engine, db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, at(10, 5), prev_close=10.0)
    engine.poll_once()
    with db.session() as session:
        assert session.scalar(select(Symbol.prev_close).where(Symbol.code == "600519.SH")) == pytest.approx(10.0)

    source.payload["600519.SH"] = quote("600519.SH", 11.0, 120.0, at(10, 6), prev_close=10.5)
    engine.poll_once()
    with db.session() as session:
        assert session.scalar(select(Symbol.prev_close).where(Symbol.code == "600519.SH")) == pytest.approx(10.5)


def test_limit_lock_is_detected_from_the_quote(setup):
    _state, source, engine, _db = setup
    source.payload["600519.SH"] = quote("600519.SH", 11.0, 100.0, at(10, 5), prev_close=10.0)
    engine.poll_once()
    assert engine.is_locked("600519.SH") == "up"

    source.payload["600519.SH"] = quote("600519.SH", 10.4, 100.0, at(10, 6), prev_close=10.0)
    engine.poll_once()
    assert engine.is_locked("600519.SH") == ""


def test_batch_quotes_parse_every_symbol_in_one_request():
    def handler(request: httpx.Request) -> httpx.Response:
        text = (
            'v_sh600519="1~贵州茅台~600519~1309.69~1331.88~1331.88~33514~1~1~'
            + "~".join([""] * 21)
            + '~20261021161453~-22.19~-1.67~1331.98~1302.36";'
            "\n"
            'v_sz000001="51~平安银行~000001~11.41~11.44~11.44~813541~1~1~'
            + "~".join([""] * 21)
            + '~20261021161500~-0.03~-0.26~11.50~11.38";'
        )
        return httpx.Response(200, content=text.encode("gbk"))

    source = TencentSource(transport=httpx.MockTransport(handler))
    result = source.quotes(["600519.SH", "000001.SZ", "600519.SH"])

    assert set(result) == {"600519.SH", "000001.SZ"}
    assert result["600519.SH"].name == "贵州茅台"
    assert result["600519.SH"].price == pytest.approx(1309.69)
    assert result["000001.SZ"].prev_close == pytest.approx(11.44)
    assert result["000001.SZ"].timestamp_ms == int(
        datetime(2026, 10, 21, 16, 15, 0, tzinfo=CN_TZ).timestamp() * 1000
    )


def test_batch_quotes_skip_missing_symbols_without_failing():
    def handler(request: httpx.Request) -> httpx.Response:
        # Only one of the two requested symbols comes back.
        text = 'v_sh600519="1~贵州茅台~600519~1309.69~1331.88~1331.88~33514~1~1~'
        text += "~".join([""] * 21)
        text += '~20261021161453~-22.19~-1.67~1331.98~1302.36";'
        return httpx.Response(200, content=text.encode("gbk"))

    source = TencentSource(transport=httpx.MockTransport(handler))
    result = source.quotes(["600519.SH", "000001.SZ"])
    assert list(result) == ["600519.SH"]


def test_closing_auction_price_replaces_the_last_in_session_write(setup):
    """After 15:00 the quote is the closing print; the daily bar must take it.

    Writing the daily bar only during a session froze it at the last poll before
    the close, so the closing auction — which often moves the price — never
    reached the stored bar.
    """
    state, source, engine, db = setup
    state["now"] = at(14, 59)
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 5_000.0, state["now"])
    engine.poll_once()

    # 15:00:00 closing auction prints a different price and a larger day volume.
    state["now"] = at(15, 1)
    source.payload["600519.SH"] = quote("600519.SH", 10.25, 5_800.0, at(15, 0))
    engine.poll_once()

    with db.session() as session:
        daily = session.scalar(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        )
    assert daily.close == pytest.approx(10.25)
    assert daily.volume == pytest.approx(5_800.0)


def test_pre_open_quote_does_not_create_a_phantom_daily_bar(setup):
    """Before 09:30 the quote still describes yesterday; today has not traded."""
    state, source, engine, db = setup
    state["now"] = at(9, 10)
    # Stamped yesterday 15:00 — the last session's close.
    yesterday_close = at(10, 5) - 86_400_000
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 5_000.0, yesterday_close)
    engine.poll_once()

    with db.session() as session:
        today_bar = session.scalar(
            select(Candle).where(
                Candle.symbol == "600519.SH",
                Candle.timeframe == "1d",
                Candle.open_time == at(0, 0),
            )
        )
    assert today_bar is None


def test_feed_outage_raises_one_alert_and_recovery_clears_it(setup):
    """A dead feed must be visible; a persistent outage must not spam.

    Nothing else notices: every consumer silently falls back to the last stored
    bar, so the UI keeps showing prices that look real while the feed is gone.
    """
    from app.modules.alerts.models import Alert

    state, source, engine, db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])
    engine.poll_once()
    engine._check_health()

    # Three consecutive failures put the feed in the "broken" state.
    source.error = RuntimeError("connection reset")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            engine.poll_once()
    engine._check_health()

    with db.session() as session:
        open_alerts = session.scalars(
            select(Alert).where(Alert.category == "data_feed", Alert.acknowledged.is_(False))
        ).all()
    assert len(open_alerts) == 1
    assert "中断" in open_alerts[0].title
    assert open_alerts[0].level == "critical"

    # Still broken: a second check must not add another row.
    engine._check_health()
    with db.session() as session:
        count = len(
            session.scalars(select(Alert).where(Alert.category == "data_feed")).all()
        )
    assert count == 1

    # Recovery acknowledges the alert instead of leaving a permanent red banner.
    source.error = None
    state["now"] = at(10, 20)
    source.payload["600519.SH"] = quote("600519.SH", 10.1, 150.0, state["now"])
    engine.poll_once()
    engine._check_health()
    with db.session() as session:
        remaining = session.scalars(
            select(Alert).where(Alert.category == "data_feed", Alert.acknowledged.is_(False))
        ).all()
    assert remaining == []



def test_outage_before_any_success_reports_a_sane_message(setup):
    """`last_poll` is 0 before the first success; the gap must not be the epoch."""
    from app.modules.alerts.models import Alert

    _state, source, engine, db = setup
    source.error = RuntimeError("dns failure")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            engine.poll_once()
    engine._check_health()

    with db.session() as session:
        alert = session.scalar(select(Alert).where(Alert.category == "data_feed"))
    assert alert is not None
    assert "启动后从未成功获取报价" in alert.message
    assert "1792" not in alert.message


def test_poll_outage_is_backfilled_from_the_intraday_endpoint(setup):
    """A gap must be filled with real bars, not left as one fabricated bar.

    The accumulator only saw the samples at each edge of the outage: its "bar"
    spans the whole gap with a high/low that never saw the middle. Persisting that
    while leaving the missing buckets absent corrupts the series twice over.
    """
    from app.modules.market.service_types import CandlePoint

    state, source, engine, db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])
    engine.poll_once()

    # Polling dies for 30 minutes, then resumes.
    state["now"] = at(10, 31)
    source.payload["600519.SH"] = quote("600519.SH", 10.4, 900.0, state["now"])
    # Upstream still has the real 15m candles for the missed window.
    source.intraday["600519.SH"] = [
        CandlePoint(time=at(10, 0), open=10.0, high=10.2, low=9.9, close=10.15, volume=300.0),
        CandlePoint(time=at(10, 15), open=10.15, high=10.45, low=10.1, close=10.4, volume=500.0),
    ]
    engine.poll_once()

    with db.session() as session:
        bars = session.scalars(
            select(Candle)
            .where(Candle.symbol == "600519.SH", Candle.timeframe == "15m")
            .order_by(Candle.open_time.asc())
        ).all()
    assert len(bars) == 2
    assert [bar.open_time for bar in bars] == [at(10, 0), at(10, 15)]
    # Exchange values, not the single-sample shape.
    assert bars[0].high == pytest.approx(10.2)
    assert bars[0].low == pytest.approx(9.9)
    assert bars[0].volume == pytest.approx(300.0)
    assert bars[1].volume == pytest.approx(500.0)


def test_a_share_gap_bar_is_dropped_when_the_endpoint_is_unavailable(setup):
    """Without klines the edge-sampled bar must not be stored as if it were real."""
    state, source, engine, db = setup
    source.payload["600519.SH"] = quote("600519.SH", 10.0, 100.0, state["now"])
    engine.poll_once()

    state["now"] = at(10, 31)
    source.payload["600519.SH"] = quote("600519.SH", 10.4, 900.0, state["now"])
    source.intraday["600519.SH"] = []  # endpoint unavailable
    engine.poll_once()

    with db.session() as session:
        bars = session.scalars(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "15m")
        ).all()
    # Nothing fabricated: the gap stays a gap until a real fetch fills it.
    assert bars == []


def test_opening_auction_quote_does_not_create_a_daily_bar(setup):
    """09:15–09:30 has a price but no cumulative volume or O/H/L.

    A daily row written then would be an artefact the first real bar has to
    overwrite. It happened to be blocked by the `volume > 0` filter, but that was
    luck: a source that reports a stale volume would have slipped through.
    """
    state, source, engine, db = setup
    pre_open = at(9, 20)
    source.payload["600519.SH"] = quote("600519.SH", 10.5, 0.0, pre_open)
    engine.poll_once()

    with db.session() as session:
        daily = session.scalars(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        ).all()
    assert daily == []

    # A quote stamped 09:30 exactly is a real session print and must be stored.
    state["now"] = at(9, 40)
    source.payload["600519.SH"] = quote("600519.SH", 10.6, 500.0, at(9, 35))
    engine.poll_once()
    with db.session() as session:
        daily = session.scalars(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        ).all()
    assert len(daily) == 1


def test_closing_auction_print_at_1500_is_accepted(setup):
    """15:00:00 is the closing print; rejecting it would freeze the last price."""
    state, source, engine, db = setup
    state["now"] = at(15, 2)
    source.payload["600519.SH"] = quote("600519.SH", 10.9, 9_000.0, at(15, 0))
    engine.poll_once()

    with db.session() as session:
        daily = session.scalars(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        ).all()
    assert len(daily) == 1
    assert daily[0].close == pytest.approx(10.9)
    assert daily[0].volume == pytest.approx(9_000.0)
