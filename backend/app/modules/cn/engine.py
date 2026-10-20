"""A-share synthetic market engine.

Differences from the crypto engine that matter for realism:

* bars exist only inside 09:30-11:30 / 13:00-15:00 on trading days;
* the simulated clock jumps over closed periods instead of idling;
* prices are clamped to the daily limit band, and a limit-locked bar stops
  trading (no fills) exactly as on the exchange;
* daily bars are written alongside 15m bars and carry the next session's
  previous-close reference.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, insert, select

from app.core.config import Settings
from app.core.db import Database
from app.modules.cn.calendar import (
    CN_TZ,
    CnSessionClock,
    day_key,
    is_trading_day,
    next_trading_day,
    sessions,
    trading_days,
)
from app.modules.cn.rules import limit_pct, price_band, round_tick
from app.modules.market.feed import align_down
from app.modules.market.models import Candle, Symbol
from app.modules.market.service_types import CandlePoint

logger = logging.getLogger(__name__)

CN_BASE_TIMEFRAME = "15m"
CN_DAY_MS = 86_400_000

# (code, name, initial price, board, limit volatility multiplier)
CN_SYMBOLS: tuple[tuple[str, str, float, str, float], ...] = (
    ("600519.SH", "贵州茅台", 1680.00, "main", 0.7),
    ("601318.SH", "中国平安", 48.50, "main", 0.9),
    ("600036.SH", "招商银行", 35.20, "main", 0.8),
    ("000858.SZ", "五粮液", 142.00, "main", 0.9),
    ("000001.SZ", "平安银行", 11.30, "main", 0.9),
    ("300750.SZ", "宁德时代", 182.00, "gem", 1.3),
    ("688981.SH", "中芯国际", 52.40, "star", 1.3),
    ("002594.SZ", "比亚迪", 245.00, "main", 1.0),
)


def _seed(*parts: object) -> int:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:16], 16)


class CnFeed:
    """Deterministic daily + intraday generator with board-aware volatility."""

    def __init__(self, volatility: float = 0.014) -> None:
        self.volatility = volatility

    def daily_range(self, symbol: str, day: date, prev_close: float, board: str) -> tuple[float, float, float, float]:
        """Open/high/low/close for one session, clamped inside the limit band."""
        rng = random.Random(_seed(symbol, "d", day.isoformat()))
        regime = random.Random(_seed(symbol, "regime", day.toordinal() // 20))
        drift = regime.uniform(-0.0016, 0.0016)
        vol = self.volatility * regime.uniform(0.6, 1.7)
        ret = max(-0.15, min(0.15, drift + rng.gauss(0.0, vol)))
        open_price = prev_close * (1.0 + rng.gauss(0.0, vol * 0.35))
        close = prev_close * (1.0 + ret)
        wick = abs(rng.gauss(0.0, vol * 0.7))
        high = max(open_price, close) * (1.0 + wick)
        low = min(open_price, close) * (1.0 - wick)
        band = price_band(prev_close, board, False, 0.01)
        if band is not None:
            open_price = min(max(open_price, band.down), band.up)
            close = min(max(close, band.down), band.up)
            high = min(high, band.up)
            low = max(low, band.down)
        return (
            round_tick(open_price),
            round_tick(high),
            round_tick(low),
            round_tick(close),
        )

    def intraday_bar(
        self, symbol: str, open_time: int, prev_close: float, board: str, price: float
    ) -> tuple[float, float, float, float, float]:
        rng = random.Random(_seed(symbol, "i", open_time // 900_000))
        vol = self.volatility / math.sqrt(16) * rng.uniform(0.5, 1.6)
        close = price * (1.0 + rng.gauss(0.0, vol))
        wick = abs(rng.gauss(0.0, vol * 0.6))
        high = max(price, close) * (1.0 + wick)
        low = min(price, close) * (1.0 - wick)
        band = price_band(prev_close, board, False, 0.01)
        if band is not None:
            close = min(max(close, band.down), band.up)
            high = min(high, band.up)
            low = max(low, band.down)
        volume = abs(rng.gauss(1.0, 0.4)) * 850_000
        return (
            round_tick(price),
            round_tick(high),
            round_tick(low),
            round_tick(close),
            round(volume, 2),
        )


class CnMarketEngine:
    """Seeds and advances A-share symbols; the only writer of cn_stock bars."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        clock: CnSessionClock | None = None,
        feed: CnFeed | None = None,
    ) -> None:
        self.db = database
        self.settings = settings
        self.clock = clock or CnSessionClock(
            speed=getattr(settings, "market_clock_speed", 60.0)
        )
        self.feed = feed or CnFeed()
        self.step_ms = 900_000
        self._live: dict[str, CandlePoint] = {}
        self._locked: dict[str, str] = {}
        self._prev_close: dict[str, float] = {}
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    def ensure_symbols(self) -> None:
        with self.db.session() as session:
            existing = {row.code for row in session.scalars(select(Symbol)).all()}
            for code, name, price, board, _bias in CN_SYMBOLS:
                if code in existing:
                    continue
                session.add(
                    Symbol(
                        code=code,
                        name=name,
                        asset_class="stock",
                        market="cn_stock",
                        exchange="SH" if code.endswith(".SH") else "SZ",
                        board=board,
                        base=code.split(".")[0],
                        quote="CNY",
                        tick_size=0.01,
                        lot_size=100.0,
                        initial_price=price,
                        prev_close=price,
                        limit_pct=limit_pct(board),
                        base_timeframe=CN_BASE_TIMEFRAME,
                    )
                )
            session.flush()

    def seed(self, *, days: int | None = None) -> None:
        self.ensure_symbols()
        horizon = int(days or self.settings.cn_seed_days)
        with self.db.session() as session:
            today = day_key(self.clock.now_ms())
            start = today - timedelta(days=int(horizon * 1.55))
            calendar = trading_days(start, today)[-horizon:]
            for row in session.scalars(select(Symbol).where(Symbol.market == "cn_stock")).all():
                count = session.scalar(
                    select(func.count(Candle.id)).where(
                        Candle.symbol == row.code, Candle.timeframe == CN_BASE_TIMEFRAME
                    )
                )
                if count and count > 0:
                    last_close = session.scalar(
                        select(Candle.close)
                        .where(Candle.symbol == row.code, Candle.timeframe == "1d")
                        .order_by(Candle.open_time.desc())
                        .limit(1)
                    )
                    if last_close:
                        self._prev_close[row.code] = float(last_close)
                    continue
                self._seed_symbol(session, row, calendar)
                logger.info("seeded A-share history for %s (%s days)", row.code, len(calendar))
            self._sync_prev_close(session)
        latest = self.clock.now_ms()
        self.clock.ensure_at_least(latest)

    def _seed_symbol(self, session, row: Symbol, calendar: list[date]) -> None:
        price = row.initial_price
        intraday_rows: list[dict] = []
        daily_rows: list[dict] = []
        tick = row.tick_size or 0.01
        for day in calendar:
            # The daily generator only sets the session's closing direction; the
            # daily bar itself is derived from the intraday bars below so the two
            # timeframes can never disagree (a real defect in the first version).
            _o, _h, _low, target_close = self.feed.daily_range(
                row.code, day, price, row.board
            )
            day_sessions = sessions(day)
            if not day_sessions:
                continue
            band = price_band(price, row.board, row.is_st, tick)
            step = self.step_ms
            bars: list[tuple[int, float, float, float, float, float]] = []
            cursor = day_sessions[0][0]
            last = price
            while cursor < day_sessions[-1][1]:
                if not any(start <= cursor < end for start, end in day_sessions):
                    cursor = next(
                        (start for start, _ in day_sessions if start > cursor), cursor + step
                    )
                    continue
                o_, h_, l_, c_, vol = self.feed.intraday_bar(
                    row.code, cursor, price, row.board, last
                )
                bars.append((cursor, o_, h_, l_, c_, vol))
                last = c_
                cursor += self.step_ms

            # Bridge the intraday path onto the intended close, then clamp every
            # bar into the daily limit band so no bar can be impossible.
            if bars and bars[-1][4] > 0:
                factor = target_close / bars[-1][4]
                bars = [
                    (ts, o_ * factor, h_ * factor, l_ * factor, c_ * factor, v)
                    for ts, o_, h_, l_, c_, v in bars
                ]
            normalised: list[dict] = []
            for ts, o_, h_, l_, c_, v in bars:
                o_ = round_tick(max(0.01, o_), tick)
                c_ = round_tick(max(0.01, c_), tick)
                h_ = round_tick(max(o_, c_, h_), tick)
                l_ = round_tick(min(o_, c_, l_), tick)
                if band is not None:
                    o_ = min(max(o_, band.down), band.up)
                    c_ = min(max(c_, band.down), band.up)
                    h_ = min(max(h_, max(o_, c_)), band.up)
                    l_ = max(min(l_, min(o_, c_)), band.down)
                normalised.append(
                    {
                        "symbol": row.code,
                        "timeframe": CN_BASE_TIMEFRAME,
                        "open_time": ts,
                        "open": o_,
                        "high": h_,
                        "low": l_,
                        "close": c_,
                        "volume": round(max(v, 0.0), 2),
                    }
                )
            intraday_rows.extend(normalised)
            if normalised:
                day_open = int(
                    datetime.combine(day, datetime.min.time(), tzinfo=CN_TZ).timestamp()
                    * 1000
                )
                daily_rows.append(
                    {
                        "symbol": row.code,
                        "timeframe": "1d",
                        "open_time": day_open,
                        "open": normalised[0]["open"],
                        "high": max(bar["high"] for bar in normalised),
                        "low": min(bar["low"] for bar in normalised),
                        "close": normalised[-1]["close"],
                        "volume": round(sum(bar["volume"] for bar in normalised), 2),
                    }
                )
                price = normalised[-1]["close"]

        for start in range(0, len(intraday_rows), 4000):
            session.execute(insert(Candle), intraday_rows[start : start + 4000])
        for start in range(0, len(daily_rows), 2000):
            session.execute(insert(Candle), daily_rows[start : start + 2000])
        if daily_rows:
            self._prev_close[row.code] = daily_rows[-1]["close"]

    def _sync_prev_close(self, session) -> None:
        for row in session.scalars(select(Symbol).where(Symbol.market == "cn_stock")).all():
            last = session.scalar(
                select(Candle.close)
                .where(Candle.symbol == row.code, Candle.timeframe == "1d")
                .order_by(Candle.open_time.desc())
                .limit(1)
            )
            if last:
                row.prev_close = float(last)
                row.limit_pct = limit_pct(row.board, row.is_st)
                self._prev_close[row.code] = float(last)
        session.commit()

    async def start(self) -> None:
        if not self.settings.cn_symbols_enabled:
            return
        await asyncio.to_thread(self.seed)
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="cn-market-engine")
        logger.info("A-share market engine started (tick=%ss)", self.settings.market_tick_seconds)

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
                logger.exception("A-share tick failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=max(0.5, self.settings.market_tick_seconds),
                )
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- ticks
    def tick(self) -> None:
        """Advance one live bar per symbol; close finished bars into the DB."""
        now = self.clock.now_ms()
        current_day = day_key(now)
        if not is_trading_day(current_day):
            return
        day_sessions = sessions(current_day)
        if not day_sessions:
            return
        in_session = any(start <= now < end for start, end in day_sessions)
        if not in_session:
            return

        with self._lock:
            with self.db.session() as session:
                rows = session.scalars(
                    select(Symbol).where(
                        Symbol.market == "cn_stock", Symbol.enabled.is_(True)
                    )
                ).all()
                closed: list[dict] = []
                for row in rows:
                    if row.suspended:
                        continue
                    prev_close = self._prev_close.get(
                        row.code, row.prev_close or row.initial_price
                    )
                    bar = self._live.get(row.code)
                    bucket = align_down(now, self.step_ms)
                    if bar is None or bar.time != bucket:
                        if bar is not None and bar.time < bucket:
                            closed.append(self._bar_row(row.code, bar))
                        start_price = bar.close if bar is not None else self._last_close(session, row.code, prev_close)
                        o, h, lo, c, vol = self.feed.intraday_bar(
                            row.code, bucket, prev_close, row.board, start_price
                        )
                        self._live[row.code] = CandlePoint(bucket, o, h, lo, c, vol)
                    else:
                        # Same bucket: nudge the close, keep high/low monotone.
                        band = price_band(prev_close, row.board, row.is_st, row.tick_size or 0.01)
                        rng = random.Random(_seed(row.code, "tick", now // 1500))
                        nxt = bar.close * (1.0 + rng.gauss(0.0, 0.0004))
                        if band is not None:
                            nxt = min(max(nxt, band.down), band.up)
                        nxt = round_tick(nxt, row.tick_size or 0.01)
                        self._live[row.code] = CandlePoint(
                            bar.time,
                            bar.open,
                            max(bar.high, nxt),
                            min(bar.low, nxt),
                            nxt,
                            bar.volume + abs(rng.gauss(0.0, 1.0)) * 12_000,
                        )
                    self._update_lock_state(row, self._live.get(row.code), prev_close)
                if closed:
                    fresh = self._insert_closed(session, closed)
                    if fresh:
                        self.upsert_daily_bars(session, fresh)
                    session.commit()

    def upsert_daily_bars(self, session, closed_rows: list[dict]) -> int:
        """Maintain the native daily bar from intraday closes.

        Seeding wrote daily bars once; without this the daily series froze at the
        seed date while intraday bars kept advancing, so daily strategies traded on
        stale prices. Each closed intraday bar updates the current session's daily
        O/H/L/C/volume in place.
        """
        if not closed_rows:
            return 0
        touched: dict[tuple[str, int], list[dict]] = {}
        for row in closed_rows:
            day_open = int(
                datetime.combine(
                    day_key(row["open_time"]), datetime.min.time(), tzinfo=CN_TZ
                ).timestamp()
                * 1000
            )
            touched.setdefault((row["symbol"], day_open), []).append(row)

        updated = 0
        for (symbol, day_open), bars in touched.items():
            existing = session.scalar(
                select(Candle).where(
                    Candle.symbol == symbol,
                    Candle.timeframe == "1d",
                    Candle.open_time == day_open,
                )
            )
            bars.sort(key=lambda item: item["open_time"])
            first_open = bars[0]["open"]
            day_high = max(bar["high"] for bar in bars)
            day_low = min(bar["low"] for bar in bars)
            last_close = bars[-1]["close"]
            added_volume = sum(bar["volume"] for bar in bars)
            if existing is None:
                session.add(
                    Candle(
                        symbol=symbol,
                        timeframe="1d",
                        open_time=day_open,
                        open=first_open,
                        high=day_high,
                        low=day_low,
                        close=last_close,
                        volume=round(added_volume, 2),
                    )
                )
                updated += 1
                continue
            # Only widen the range with genuinely new bars; re-presented bars at a
            # restart boundary must not double-count volume.
            existing.high = max(existing.high, day_high)
            existing.low = min(existing.low, day_low)
            existing.close = last_close
            existing.volume = round(existing.volume + added_volume, 2)
            updated += 1
        return updated

    def _insert_closed(self, session, rows: list[dict]) -> list[dict]:
        """Insert closed bars, skipping any that already exist.

        The live bar is rebuilt from the last stored bar on restart, so a bar can
        be re-presented at the boundary. Ignoring duplicates keeps the unique
        index as a safety net instead of a crash source. Returns the rows that
        were actually written, so callers never double-count volume.
        """
        existing = {
            ts
            for ts in session.scalars(
                select(Candle.open_time).where(
                    Candle.symbol.in_({row["symbol"] for row in rows}),
                    Candle.timeframe == CN_BASE_TIMEFRAME,
                    Candle.open_time.in_({row["open_time"] for row in rows}),
                )
            ).all()
        }
        fresh = [row for row in rows if row["open_time"] not in existing]
        if fresh:
            session.execute(insert(Candle), fresh)
        return fresh

    def _bar_row(self, code: str, bar: CandlePoint) -> dict:
        return {
            "symbol": code,
            "timeframe": CN_BASE_TIMEFRAME,
            "open_time": bar.time,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }

    def _last_close(self, session, code: str, prev_close: float) -> float:
        value = session.scalar(
            select(Candle.close)
            .where(Candle.symbol == code, Candle.timeframe == CN_BASE_TIMEFRAME)
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
        return float(value) if value else prev_close

    def _update_lock_state(self, row: Symbol, bar: CandlePoint | None, prev_close: float) -> None:
        """Track limit-up/down locks; a locked book cannot be traded."""
        if bar is None:
            return
        band = price_band(prev_close, row.board, row.is_st, row.tick_size or 0.01)
        if band is None:
            self._locked.pop(row.code, None)
            return
        tick = row.tick_size or 0.01
        if bar.close >= band.up - tick / 2:
            self._locked[row.code] = "up"
        elif bar.close <= band.down + tick / 2:
            self._locked[row.code] = "down"
        else:
            self._locked.pop(row.code, None)

    # --------------------------------------------------------------- reads
    def live_candle(self, symbol: str) -> CandlePoint | None:
        with self._lock:
            return self._live.get(symbol.upper())

    def is_locked(self, symbol: str) -> str:
        with self._lock:
            return self._locked.get(symbol.upper(), "")

    def prev_close(self, symbol: str) -> float:
        with self._lock:
            return self._prev_close.get(symbol.upper(), 0.0)

    def clock_info(self) -> dict:
        return self.clock.info()

    # ------------------------------------------------------- maintenance
    def refresh_prev_close(self) -> int:
        """Called after a session closes so tomorrow's limits use today's close."""
        with self.db.session() as session:
            self._sync_prev_close(session)
        with self._lock:
            self._last_bar_day = day_key(self.clock.now_ms())
        return len(self._prev_close)

    def clear(self) -> int:
        with self.db.session() as session:
            result = session.execute(
                delete(Candle).where(
                    Candle.symbol.in_([code for code, *_ in CN_SYMBOLS])
                )
            )
            session.commit()
        self._live.clear()
        self._locked.clear()
        return int(result.rowcount or 0)
