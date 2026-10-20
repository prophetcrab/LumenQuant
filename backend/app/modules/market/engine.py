"""Background market simulator.

Owns the live (in-progress) candle per symbol and appends closed base candles to
the database. It is the only writer of `market_candles`, which keeps the read
API lock-free from its perspective.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import Database
from app.modules.market.feed import Bar, MarketClock, SyntheticFeed, align_down, timeframe_ms
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import BASE_TIMEFRAME
from app.modules.market.service_types import CandlePoint

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = (
    ("BTCUSDT", "Bitcoin / USDT", 65_000.0, 1.0),
    ("ETHUSDT", "Ethereum / USDT", 3_200.0, 1.2),
    ("SOLUSDT", "Solana / USDT", 152.0, 1.5),
    ("BNBUSDT", "BNB / USDT", 585.0, 0.9),
    ("XRPUSDT", "XRP / USDT", 0.62, 1.4),
    ("ADAUSDT", "Cardano / USDT", 0.46, 1.3),
)


class MarketEngine:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        clock: MarketClock | None = None,
        feed: SyntheticFeed | None = None,
    ) -> None:
        self.db = database
        self.settings = settings
        self.clock = clock or MarketClock(speed=getattr(settings, "market_clock_speed", 60.0))
        self.feed = feed or SyntheticFeed()
        self.step_ms = timeframe_ms(BASE_TIMEFRAME)
        self._live: dict[str, Bar] = {}
        self._lock = threading.Lock()
        self._rng = random.Random(7)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    def seed(self) -> None:
        with self.db.session() as session:
            self._ensure_symbols(session)
            last_times = []
            for symbol in session.scalars(
                select(Symbol).where(
                    Symbol.enabled.is_(True), Symbol.market == "crypto"
                )
            ).all():
                count = session.scalar(
                    select(func.count(Candle.id)).where(
                        Candle.symbol == symbol.code, Candle.timeframe == BASE_TIMEFRAME
                    )
                )
                if not count:
                    self._seed_history(session, symbol)
                last = session.scalar(
                    select(func.max(Candle.open_time)).where(
                        Candle.symbol == symbol.code, Candle.timeframe == BASE_TIMEFRAME
                    )
                )
                if last is not None:
                    last_times.append(int(last))
            if last_times:
                self.clock.ensure_at_least(max(last_times) + self.step_ms)

    def _ensure_symbols(self, session: Session) -> None:
        existing = {row.code for row in session.scalars(select(Symbol)).all()}
        for code, name, price, volume_bias in DEFAULT_SYMBOLS:
            if code in existing:
                continue
            session.add(
                Symbol(
                    code=code,
                    name=name,
                    base=code.removesuffix("USDT"),
                    quote="USDT",
                    initial_price=price,
                    volume_bias=volume_bias,
                )
            )
        session.flush()

    def _seed_history(self, session: Session, symbol: Symbol) -> None:
        now = self.clock.now_ms()
        count = max(200, int(self.settings.market_seed_bars))
        bars = self.feed.history(
            symbol.code,
            end_ms=now,
            count=count,
            step_ms=self.step_ms,
            initial_price=symbol.initial_price,
        )
        rows = [
            {
                "symbol": symbol.code,
                "timeframe": BASE_TIMEFRAME,
                "open_time": bar.open_time,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ]
        for start in range(0, len(rows), 2000):
            session.execute(insert(Candle), rows[start : start + 2000])
        logger.info("seeded %s candles for %s", len(rows), symbol.code)

    async def start(self) -> None:
        await asyncio.to_thread(self.seed)
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="market-engine")
        logger.info(
            "market engine started (base=%s, tick=%ss, clock=%sx)",
            BASE_TIMEFRAME,
            self.settings.market_tick_seconds,
            self.clock.speed,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logger.exception("market tick failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=max(0.5, self.settings.market_tick_seconds)
                )
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- ticks
    def tick(self) -> None:
        now = self.clock.now_ms()
        closed: list[dict] = []
        with self._lock:
            with self.db.session() as session:
                symbols = session.scalars(
                    select(Symbol).where(
                        Symbol.enabled.is_(True), Symbol.market == "crypto"
                    )
                ).all()
                for symbol in symbols:
                    bar = self._live.get(symbol.code)
                    if bar is None:
                        bar = self._bootstrap_live(session, symbol, now)
                    closed.extend(self._advance(symbol, bar, now))
                    self._live[symbol.code] = bar
                if closed:
                    session.execute(insert(Candle), closed)

    def _bootstrap_live(self, session: Session, symbol: Symbol, now: int) -> Bar:
        last = session.scalars(
            select(Candle)
            .where(Candle.symbol == symbol.code, Candle.timeframe == BASE_TIMEFRAME)
            .order_by(Candle.open_time.desc())
            .limit(1)
        ).first()
        if last is None:
            self._seed_history(session, symbol)
            return self._bootstrap_live(session, symbol, now)
        bucket = align_down(now, self.step_ms)
        if bucket <= last.open_time:
            bucket = last.open_time + self.step_ms
        return Bar(
            open_time=bucket,
            open=last.close,
            high=last.close,
            low=last.close,
            close=last.close,
            volume=0.0,
        )

    def _advance(self, symbol: Symbol, bar: Bar, now: int) -> list[dict]:
        closed: list[dict] = []
        guard = 0
        while now >= bar.open_time + self.step_ms and guard < 240:
            closed.append(
                {
                    "symbol": symbol.code,
                    "timeframe": BASE_TIMEFRAME,
                    "open_time": bar.open_time,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            )
            open_time = bar.open_time + self.step_ms
            open_price = bar.close
            bar.open_time = open_time
            bar.open = open_price
            bar.high = open_price
            bar.low = open_price
            bar.close = open_price
            bar.volume = 0.0
            guard += 1
        # Intra-bar random walk, scaled by symbol liquidity.
        move = self._rng.gauss(0.0, 0.00035) * (symbol.volume_bias or 1.0)
        bar.close = max(1e-9, bar.close * (1.0 + move))
        bar.high = max(bar.high, bar.close)
        bar.low = min(bar.low, bar.close)
        bar.volume += abs(move) * 250_000.0 + 120.0
        return closed

    # --------------------------------------------------------------- reads
    def live_candle(self, symbol: str) -> CandlePoint | None:
        with self._lock:
            bar = self._live.get(symbol.upper())
            if bar is None:
                return None
            return CandlePoint(
                time=bar.open_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )

    def clock_info(self) -> dict:
        return {
            "simulated_now": self.clock.now_ms(),
            "speed": self.clock.speed,
            "timeframe": BASE_TIMEFRAME,
            "tick_seconds": self.settings.market_tick_seconds,
        }
