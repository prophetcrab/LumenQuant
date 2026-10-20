from __future__ import annotations

from app.core.config import Settings
from app.core.db import Database
from app.modules.market.aggregate import aggregate
from app.modules.market.engine import DEFAULT_SYMBOLS, MarketEngine
from app.modules.market.feed import SyntheticFeed
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import MarketService
from app.modules.market.service_types import CandlePoint
from sqlalchemy import func, select


def make_engine(tmp_path, **overrides):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'market.db').as_posix()}",
        seed_market=False,
        runtime_enabled=False,
        market_seed_bars=300,
        **overrides,
    )
    db = Database(settings.database_url)
    db.create_schema()
    engine = MarketEngine(db, settings)
    return settings, db, engine


def test_seed_creates_catalog_and_history(tmp_path):
    _, db, engine = make_engine(tmp_path)
    engine.seed()
    with db.session() as session:
        assert session.scalar(select(func.count(Symbol.id))) == len(DEFAULT_SYMBOLS)
        count = session.scalar(
            select(func.count(Candle.id)).where(Candle.symbol == "BTCUSDT")
        )
        assert count == 300
    db.dispose()


def test_live_candle_created_on_tick(tmp_path):
    _, db, engine = make_engine(tmp_path)
    engine.seed()
    engine.tick()
    live = engine.live_candle("BTCUSDT")
    assert live is not None
    assert live.close > 0
    assert live.high >= live.close >= live.low
    db.dispose()


def test_service_reads_and_aggregates(tmp_path):
    _, db, engine = make_engine(tmp_path)
    engine.seed()
    engine.tick()
    with db.session() as session:
        service = MarketService(session, clock=engine.clock, live=engine)
        candles = service.candles("BTCUSDT", timeframe="15m", limit=64)
        assert len(candles) == 64
        assert all(candles[i].time < candles[i + 1].time for i in range(len(candles) - 1))
        hourly = service.candles("BTCUSDT", timeframe="1h", limit=10)
        assert len(hourly) == 10
        assert hourly[-1].time % 3_600_000 == 0
        snapshot = service.snapshot("BTCUSDT")
        assert snapshot.price > 0
        assert snapshot.high_24h >= snapshot.low_24h
    db.dispose()


def test_synthetic_feed_is_deterministic():
    feed = SyntheticFeed()
    a = feed.generate("BTCUSDT", 1_700_000_000_000, 100.0, 900_000)
    b = feed.generate("BTCUSDT", 1_700_000_000_000, 100.0, 900_000)
    c = feed.generate("ETHUSDT", 1_700_000_000_000, 100.0, 900_000)
    assert a == b
    assert a != c


def test_aggregation_ohlcv():
    points = [
        CandlePoint(0, 10, 12, 9, 11, 5),
        CandlePoint(900_000, 11, 15, 10, 14, 6),
        CandlePoint(1_800_000, 14, 14.5, 8, 9, 7),
        CandlePoint(2_700_000, 9, 10, 8.5, 9.5, 8),
    ]
    result = aggregate(points, "1h")
    assert len(result) == 1
    bar = result[0]
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (10, 15, 8, 9.5, 26)
