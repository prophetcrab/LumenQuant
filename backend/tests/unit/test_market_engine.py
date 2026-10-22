from __future__ import annotations

import pytest

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


def test_cn_snapshot_keeps_limit_band_without_any_candles(database):
    """A registered symbol with no bars must still report its price limits.

    This is the state right after switching to a real data source, before the
    first sync lands. The previous close comes from the symbol registry, so the
    band is computable — but the snapshot used to fail outright, and the list
    route filled price/limit_up/limit_down with 0.0. The UI then showed
    "涨停 0.00 / 跌停 0.00", i.e. no limits at all.
    """
    from app.modules.market.service import MarketService
    from sqlalchemy import select
    from app.modules.market.models import Candle

    with database.session() as session:
        service = MarketService(session, live=None)
        service.add_symbol(
            code="600519.SH", name="贵州茅台", initial_price=1680.0,
            market="cn_stock", board="main", prev_close=1680.0,
        )
        snap = service.snapshot("600519.SH")
        assert snap.price == pytest.approx(1680.0)
        assert snap.prev_close == pytest.approx(1680.0)
        assert snap.limit_up == pytest.approx(1848.0)
        assert snap.limit_down == pytest.approx(1512.0)
        # Nothing was fabricated into the candle table to make this work.
        assert session.scalars(
            select(Candle).where(Candle.symbol == "600519.SH")
        ).all() == []


def test_gem_board_uses_a_twenty_percent_band(database):
    """The band must follow the board, not a hard-coded 10%."""
    from app.modules.market.service import MarketService

    with database.session() as session:
        service = MarketService(session, live=None)
        service.add_symbol(
            code="300750.SZ", name="宁德时代", initial_price=200.0,
            market="cn_stock", board="gem", prev_close=200.0,
        )
        snap = service.snapshot("300750.SZ")
        assert snap.limit_up == pytest.approx(240.0)
        assert snap.limit_down == pytest.approx(160.0)
