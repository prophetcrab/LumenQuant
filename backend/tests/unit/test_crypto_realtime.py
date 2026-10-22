"""Crypto realtime feed: kline parsing, accumulation and snapshot precedence."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.db import Database
from app.modules.market.crypto_quotes import CryptoQuoteEngine
from app.modules.market.models import Symbol
from app.modules.market.service_types import CandlePoint
from app.modules.market.sources.binance_public import BinancePublicSource, CryptoTicker
from app.modules.market.service import MarketService

# 2026-10-21 10:00:00 UTC, deliberately on a 15m boundary.
NOW_MS = 1792576800000
BUCKET_MS = 900_000


def ticker_row(symbol: str, price: float, ts_ms: int) -> dict:
    return {
        "symbol": symbol,
        "lastPrice": f"{price:.8f}",
        "openPrice": "80000.00000000",
        "highPrice": f"{price + 500:.8f}",
        "lowPrice": "79500.00000000",
        "volume": "1234.5",
        "prevClosePrice": "80100.00000000",
        "closeTime": ts_ms,
    }


def make_source(handler) -> BinancePublicSource:
    return BinancePublicSource(transport=httpx.MockTransport(handler))


def test_klines_parse_high_before_close():
    """Binance columns are [t, open, high, low, close, volume] — not close-first."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "klines" in str(request.url):
            return httpx.Response(
                200,
                json=[[NOW_MS, "100.0", "110.0", "90.0", "105.0", "42.0", NOW_MS + 899999]],
            )
        return httpx.Response(404)

    bars = make_source(handler).klines("BTCUSDT", limit=1)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.open == pytest.approx(100.0)
    assert bar.close == pytest.approx(105.0)
    assert bar.high == pytest.approx(110.0)
    assert bar.low == pytest.approx(90.0)
    assert bar.volume == pytest.approx(42.0)


def test_tickers_batch_one_request():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "symbols" in str(request.url)
        return httpx.Response(
            200,
            json=[ticker_row("BTCUSDT", 83500.0, NOW_MS), ticker_row("ETHUSDT", 2460.0, NOW_MS)],
        )

    quotes = make_source(handler).tickers(["BTCUSDT", "ETHUSDT"])
    assert set(quotes) == {"BTCUSDT", "ETHUSDT"}
    assert quotes["BTCUSDT"].price == pytest.approx(83500.0)
    assert quotes["ETHUSDT"].prev_close == pytest.approx(80100.0)


def test_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(451, json={"msg": "unavailable"})

    source = make_source(handler)
    with pytest.raises(Exception):
        source.tickers(["BTCUSDT"])
    # 4xx (and the 451 legal block) is permanent; retrying only wastes time.
    assert calls["n"] == 1


@pytest.fixture()
def engine_setup(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'crypto.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        crypto_data_source="binance",
        crypto_quote_poll_seconds=5.0,
    )
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
                initial_price=80000.0,
            )
        )
    state = {"now": NOW_MS + 60_000}
    source = type("FakeSource", (), {"name": "fake", "tickers": lambda self, codes: self.payload})()
    source.payload = {}
    engine = CryptoQuoteEngine(db, settings, source=source, now_ms=lambda: state["now"])
    yield state, source, engine, db
    db.dispose()


def test_live_bar_accumulates_prices_without_inventing_volume(engine_setup):
    state, source, engine, _db = engine_setup
    from app.modules.market.sources.binance_public import CryptoTicker

    source.payload = {
        "BTCUSDT": CryptoTicker(
            symbol="BTCUSDT",
            price=83500.0,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=1234.5,
            prev_close=80100.0,
            timestamp_ms=state["now"],
        )
    }
    engine.poll_once()
    bar = engine.live_candle("BTCUSDT")
    assert bar.time == NOW_MS
    assert bar.open == pytest.approx(83500.0)
    # A rolling 24h ticker cannot yield a candle volume; reporting 0 is honest.
    assert bar.volume == 0.0

    state["now"] += 60_000
    source.payload = {
        "BTCUSDT": CryptoTicker(
            symbol="BTCUSDT",
            price=83100.0,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=1300.0,
            prev_close=80100.0,
            timestamp_ms=state["now"],
        )
    }
    engine.poll_once()
    bar = engine.live_candle("BTCUSDT")
    assert bar.high == pytest.approx(83500.0)
    assert bar.low == pytest.approx(83100.0)
    assert bar.close == pytest.approx(83100.0)


def test_finished_bar_is_persisted(engine_setup):
    from sqlalchemy import select

    from app.modules.market.models import Candle

    state, source, engine, db = engine_setup
    source.payload = {
        "BTCUSDT": CryptoTicker(
            symbol="BTCUSDT",
            price=83500.0,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=1.0,
            prev_close=80100.0,
            timestamp_ms=state["now"],
        )
    }
    engine.poll_once()
    state["now"] += 16 * 60_000
    source.payload = {
        "BTCUSDT": CryptoTicker(
            symbol="BTCUSDT",
            price=83600.0,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=2.0,
            prev_close=80100.0,
            timestamp_ms=state["now"],
        )
    }
    engine.poll_once()

    with db.session() as session:
        bars = session.scalars(
            select(Candle).where(Candle.symbol == "BTCUSDT", Candle.timeframe == "15m")
        ).all()
    assert len(bars) == 1
    assert bars[0].open_time == NOW_MS
    assert bars[0].close == pytest.approx(83500.0)


def test_closed_bar_uses_the_authoritative_kline_volume(engine_setup):
    """Polling cannot measure candle volume; the kline endpoint supplies it.

    Without this the bar is stored with volume 0, which silently zeroes every
    volume-based strategy and the data quality report.
    """
    from sqlalchemy import select

    from app.modules.market.models import Candle

    state, source, engine, db = engine_setup
    source.klines = lambda code, interval="15m", limit=3: [
        CandlePoint(time=NOW_MS, open=83300.0, high=83600.0, low=83200.0, close=83400.0, volume=321.5),
        CandlePoint(time=NOW_MS + BUCKET_MS, open=83400.0, high=83500.0, low=83350.0, close=83450.0, volume=88.0),
    ]

    def ticker(price: float, ts_ms: int) -> CryptoTicker:
        return CryptoTicker(
            symbol="BTCUSDT",
            price=price,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=1.0,
            prev_close=80100.0,
            timestamp_ms=ts_ms,
        )

    source.payload = {"BTCUSDT": ticker(83420.0, state["now"])}
    engine.poll_once()

    state["now"] += 16 * 60_000
    source.payload["BTCUSDT"] = ticker(83460.0, state["now"])
    engine.poll_once()

    with db.session() as session:
        bar = session.scalar(
            select(Candle).where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == "15m",
                Candle.open_time == NOW_MS,
            )
        )
    assert bar is not None
    # Exchange values win over the accumulator's partial, volume-less bar.
    assert bar.open == pytest.approx(83300.0)
    assert bar.high == pytest.approx(83600.0)
    assert bar.low == pytest.approx(83200.0)
    assert bar.close == pytest.approx(83400.0)
    assert bar.volume == pytest.approx(321.5)


def test_stale_ticker_is_not_served_as_live(engine_setup):
    from app.modules.market.sources.binance_public import CryptoTicker

    state, source, engine, _db = engine_setup
    source.payload = {
        "BTCUSDT": CryptoTicker(
            symbol="BTCUSDT",
            price=83500.0,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=1.0,
            prev_close=80100.0,
            timestamp_ms=state["now"],
        )
    }
    engine.poll_once()
    state["now"] += 10 * 60_000
    assert engine.live_quote("BTCUSDT") is None
    assert engine.rows()[0]["fresh"] is False


def test_snapshot_uses_the_24h_ticker(engine_setup):
    from app.modules.market.sources.binance_public import CryptoTicker

    state, source, engine, db = engine_setup
    source.payload = {
        "BTCUSDT": CryptoTicker(
            symbol="BTCUSDT",
            price=83500.0,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=1234.5,
            prev_close=80100.0,
            timestamp_ms=state["now"],
        )
    }
    engine.poll_once()

    with db.session() as session:
        market = MarketService(session, clock=engine.clock, live=engine)
        snap = market.snapshot("BTCUSDT")
    # The ticker's own 24h window is the definition of the field, so change% is
    # (last / open - 1) and the extremes come straight from the exchange.
    assert snap.price == pytest.approx(83500.0)
    assert snap.change_24h_pct == pytest.approx(4.375, abs=1e-3)
    assert snap.high_24h == pytest.approx(84000.0)
    assert snap.volume_24h == pytest.approx(1234.5)


def test_crypto_feed_outage_raises_one_alert_and_recovery_clears_it(engine_setup):
    """Crypto trades 24/7, so a silent feed is always a defect worth alerting."""
    from sqlalchemy import select

    from app.modules.alerts.models import Alert
    from app.modules.market.sources.binance_public import CryptoTicker

    state, source, engine, db = engine_setup
    source.payload = {
        "BTCUSDT": CryptoTicker(
            symbol="BTCUSDT",
            price=83500.0,
            open=80000.0,
            high=84000.0,
            low=79000.0,
            volume=1.0,
            prev_close=80100.0,
            timestamp_ms=state["now"],
        )
    }
    engine.poll_once()
    engine._check_health()

    def failing(codes):
        raise RuntimeError("connection reset")

    source.tickers = failing
    for _ in range(3):
        with pytest.raises(RuntimeError):
            engine.poll_once()
    engine._check_health()

    with db.session() as session:
        open_alerts = session.scalars(
            select(Alert).where(
                Alert.category == "data_feed", Alert.acknowledged.is_(False)
            )
        ).all()
    assert len(open_alerts) == 1
    assert open_alerts[0].ref_id == "crypto"
    assert "中断" in open_alerts[0].title

    # Repeated checks while still broken must not add rows.
    engine._check_health()
    with db.session() as session:
        assert len(session.scalars(select(Alert).where(Alert.category == "data_feed")).all()) == 1

    # Recovery acknowledges it.
    source.tickers = lambda codes: source.payload
    state["now"] += 60_000
    source.payload["BTCUSDT"] = CryptoTicker(
        symbol="BTCUSDT",
        price=83600.0,
        open=80000.0,
        high=84000.0,
        low=79000.0,
        volume=2.0,
        prev_close=80100.0,
        timestamp_ms=state["now"],
    )
    engine.poll_once()
    engine._check_health()
    with db.session() as session:
        assert (
            session.scalars(
                select(Alert).where(
                    Alert.category == "data_feed", Alert.acknowledged.is_(False)
                )
            ).all()
            == []
        )


def test_outage_gap_is_backfilled_instead_of_persisting_a_fake_bar(engine_setup):
    """After a polling outage the missed candles come from klines.

    The accumulator only saw samples at the edges of the gap, so persisting it
    would store a bar whose high/low never saw the middle of the period and whose
    volume is unknown — while every bucket in between stayed missing entirely.
    """
    from sqlalchemy import select

    from app.modules.market.models import Candle

    state, source, engine, db = engine_setup
    base = NOW_MS  # a 15m boundary

    def ticker(price: float, volume: float, ts_ms: int) -> CryptoTicker:
        return CryptoTicker(
            symbol="BTCUSDT",
            price=price,
            open=price,
            high=price,
            low=price,
            volume=volume,
            prev_close=price,
            timestamp_ms=ts_ms,
        )

    # Upstream holds the candles the poller will miss.
    def klines(code, interval="15m", limit=500):
        out = []
        bucket = base + BUCKET_MS
        while bucket < (state["now"] // BUCKET_MS) * BUCKET_MS:
            out.append(
                CandlePoint(
                    time=bucket,
                    open=81000.0,
                    high=81100.0,
                    low=80900.0,
                    close=81050.0,
                    volume=42.0,
                )
            )
            bucket += BUCKET_MS
        return out

    source.klines = klines
    source.payload = {"BTCUSDT": ticker(80000.0, 10.0, base + 60_000)}
    engine.poll_once()

    # Polling dies for two hours, then resumes.
    state["now"] += 2 * 3600_000
    source.payload = {"BTCUSDT": ticker(82000.0, 5000.0, state["now"])}
    engine.poll_once()

    with db.session() as session:
        bars = session.scalars(
            select(Candle)
            .where(Candle.symbol == "BTCUSDT", Candle.timeframe == "15m")
            .order_by(Candle.open_time.asc())
        ).all()

    now_bucket = (state["now"] // BUCKET_MS) * BUCKET_MS
    # Buckets base .. now_bucket-1 exist upstream. The very first one (`base`) was
    # only sampled once before the outage, so the fake upstream stub below does not
    # serve it either — every later bucket must be present.
    expected = (now_bucket - base) // BUCKET_MS - 1
    assert len(bars) == expected, f"expected {expected} closed bars, got {len(bars)}"
    # Every stored bar carries real volume and a real range; none is fabricated.
    assert all(bar.volume > 0 for bar in bars)
    assert all(bar.high > bar.low for bar in bars)
    # The pre-outage bucket is not left with the single-sample O=H=L=C shape.
    assert bars[0].high > bars[0].low


def test_backfill_repairs_a_bar_stored_without_volume(engine_setup):
    """Bars already stored with volume 0 by a degraded poll get corrected."""
    from sqlalchemy import select

    from app.modules.market.models import Candle

    state, source, engine, db = engine_setup
    with db.session() as session:
        session.add(
            Candle(
                symbol="BTCUSDT",
                timeframe="15m",
                open_time=NOW_MS,
                open=80000.0,
                high=80000.0,
                low=80000.0,
                close=80000.0,
                volume=0.0,
            )
        )

    def klines(code, interval="15m", limit=500):
        return [
            CandlePoint(
                time=NOW_MS, open=79950.0, high=80100.0, low=79800.0, close=80050.0, volume=77.0
            )
        ]

    source.klines = klines
    # Jump three buckets so the stored bar is well behind "now": that is exactly
    # the condition under which a degraded bar is recognised and repaired.
    state["now"] += 3 * BUCKET_MS
    source.payload = {"BTCUSDT": ticker_for(80100.0, state["now"])}
    engine.poll_once()
    # The repair rides on the closing-bar sync: advance one more bucket so a bar
    # closes and the authoritative fetch runs.
    state["now"] += BUCKET_MS
    source.payload = {"BTCUSDT": ticker_for(80200.0, state["now"])}
    engine.poll_once()

    with db.session() as session:
        bar = session.scalar(
            select(Candle).where(Candle.open_time == NOW_MS, Candle.timeframe == "15m")
        )
    assert bar.volume == pytest.approx(77.0)
    assert bar.high == pytest.approx(80100.0)
    assert bar.low == pytest.approx(79800.0)


def ticker_for(price: float, ts_ms: int) -> CryptoTicker:
    return CryptoTicker(
        symbol="BTCUSDT",
        price=price,
        open=price,
        high=price,
        low=price,
        volume=1.0,
        prev_close=price,
        timestamp_ms=ts_ms,
    )


def test_future_dated_bars_are_not_treated_as_fresh(engine_setup):
    """Synthetic bars run ahead of wall time; they must not block a real import.

    `age = now - latest` goes negative for a future timestamp, and the freshness
    test `age <= max_age_ms` then returns True. On a database that had been running
    the 60x synthetic engine, switching to Binance skipped `sync_history` entirely
    and left ~23k synthetic 15m bars (spanning months ahead) beside live quotes.
    """
    from sqlalchemy import select

    from app.modules.market.models import Candle

    state, _source, engine, db = engine_setup
    future = state["now"] + 90 * 24 * 3600_000
    with db.session() as session:
        session.add(
            Candle(
                symbol="BTCUSDT",
                timeframe="15m",
                open_time=future,
                open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
            )
        )

    assert engine._history_is_fresh("BTCUSDT", 1_800_000) is False
