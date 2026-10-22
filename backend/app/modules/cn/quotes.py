"""Realtime A-share quotes.

The sync service imports *history*; this engine keeps the *current* price fresh
between syncs. It polls the batch quote endpoint on a timer, keeps an in-memory
quote per symbol, and accumulates a live 15m bar from successive polls.

Why not reuse the synthetic CnMarketEngine when real data is enabled? Because it
would keep writing synthetic bars into the same table after a real sync, mixing
two price universes again — the exact defect that made change% and limit bands
meaningless before. In real-data mode this engine is the *only* CN live provider;
the synthetic engine is reduced to registering the symbol catalog.

Quotes are never written to the database: the database keeps closed bars, and a
restart immediately re-polls. Persisting every tick would grow the file by tens
of thousands of rows a day for data that is superseded seconds later.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select

from app.core.config import Settings
from app.core.db import Database
from app.modules.cn.calendar import (
    CN_TZ,
    day_key,
    is_session_open,
    session_bounds,
    session_state,
)
from app.modules.cn.rules import price_band
from app.modules.market.feed import align_down
from app.modules.market.models import Candle, Symbol
from app.modules.market.service_types import CandlePoint

logger = logging.getLogger(__name__)

CN_STEP_MS = 900_000


@dataclass(frozen=True)
class LiveQuote:
    code: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: float
    timestamp_ms: int
    fetched_at_ms: int
    source: str = "tencent"


class RealtimeSessionClock:
    """Wall-clock session state exposed where a market engine's clock is expected.

    `CnMarketEngine.clock` is a simulated 60x clock. In real-data mode there is no
    simulated engine, but callers still ask the provider for `clock.now_ms()` to
    decide sessions and T+1 duties, so this returns true Beijing time.
    """

    def __init__(self, engine: "CnQuoteEngine") -> None:
        self._engine = engine

    def now_ms(self) -> int:
        return self._engine.now_ms()

    def info(self) -> dict:
        payload = session_state(self.now_ms()).as_dict()
        payload.update(
            {
                "source": "tencent-realtime",
                "simulated": False,
                "quote_poll_seconds": self._engine.poll_seconds,
            }
        )
        return payload


class CnQuoteEngine:
    name = "tencent-realtime"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        source=None,
        now_ms=None,
    ) -> None:
        self.db = database
        self.settings = settings
        self.source = source or _default_source(settings)
        self.poll_seconds = max(2.0, float(settings.cn_quote_poll_seconds))
        # A quote older than four polls is treated as missing: during a session a
        # dead poller must not keep serving a price as if it were live. Outside a
        # session prices do not move, so the tolerance is much wider.
        self.session_max_age_ms = int(max(30.0, self.poll_seconds * 4) * 1000)
        self.closed_max_age_ms = int(max(600.0, self.poll_seconds * 8) * 1000)
        self._quotes: dict[str, LiveQuote] = {}
        self._live: dict[str, CandlePoint] = {}
        self._cum_volume: dict[str, float] = {}
        self._locked: dict[str, str] = {}
        self._lock = threading.Lock()
        self._last_poll_ms = 0
        self._last_latency_ms = 0.0
        self._last_error = ""
        self._failures = 0
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.clock = RealtimeSessionClock(self)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        # Fetch once before the loop so the first request after boot shows a real
        # price instead of waiting a full poll interval (15s of stale display).
        try:
            await asyncio.to_thread(self.poll_once)
        except Exception:
            logger.warning("initial A-share quote poll failed; will retry", exc_info=True)
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="cn-quote-engine")
        logger.info(
            "A-share realtime quote engine started (poll=%ss, symbols=%s)",
            self.poll_seconds,
            len(self._quotes),
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
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._next_delay()
                )
                continue
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception as exc:
                # Expected during a network hiccup; a traceback every 15s only
                # makes the log unreadable. The reason is kept in `last_error`
                # and surfaced through /cn/data/source.
                logger.warning("A-share quote poll failed: %s", str(exc)[:160])

    def _next_delay(self) -> float:
        """Session-paced polling with exponential backoff while failing."""
        with self._lock:
            failures = self._failures
        if failures <= 0:
            return (
                self.poll_seconds
                if is_session_open(self.now_ms())
                else max(60.0, self.poll_seconds * 4)
            )
        return min(300.0, self.poll_seconds * (2**failures))

    # ----------------------------------------------------------------- poll
    def now_ms(self) -> int:
        return self._now_ms()

    def poll_once(self) -> int:
        meta = self._symbol_meta()
        if not meta:
            return 0
        started = time.perf_counter()
        try:
            fetched = self.source.quotes(list(meta))
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:200]
                self._failures += 1
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        now = self.now_ms()
        # Close finished bars *before* ingesting: `_ingest` overwrites the live
        # slot with the new bucket, so closing afterwards would silently drop the
        # last bar of every 15-minute window.
        closed = self._close_finished(now)
        if closed:
            self._persist_closed(closed)
        updated = self._ingest(fetched, meta, now)
        self._persist_prev_close(fetched, meta)
        self._sync_today_daily(fetched, now)
        with self._lock:
            self._last_poll_ms = now
            self._last_latency_ms = latency_ms
            self._last_error = ""
            self._failures = 0
        return updated

    def _symbol_meta(self) -> dict[str, tuple[str, bool, float, float]]:
        with self.db.session() as session:
            rows = session.scalars(
                select(Symbol).where(
                    Symbol.market == "cn_stock", Symbol.enabled.is_(True)
                )
            ).all()
            return {
                row.code: (
                    row.board or "main",
                    bool(row.is_st),
                    float(row.tick_size or 0.01),
                    float(row.prev_close or 0.0),
                )
                for row in rows
            }

    def _ingest(
        self,
        fetched: dict,
        meta: dict[str, tuple[str, bool, float, float]],
        now: int,
    ) -> int:
        bounds = session_bounds(now)
        with self._lock:
            for code, quote in fetched.items():
                details = meta.get(code)
                if details is None or quote.price <= 0:
                    continue
                board, is_st, tick, _stored_prev = details
                self._quotes[code] = LiveQuote(
                    code=code,
                    price=float(quote.price),
                    prev_close=float(quote.prev_close),
                    open=float(quote.open),
                    high=float(quote.high),
                    low=float(quote.low),
                    volume=float(quote.volume),
                    timestamp_ms=int(quote.timestamp_ms),
                    fetched_at_ms=now,
                    source=getattr(self.source, "name", "tencent"),
                )
                self._update_live_bar(code, quote, bounds)
                prev_close = quote.prev_close if quote.prev_close > 0 else _stored_prev
                self._update_lock_state(code, quote.price, prev_close, board, is_st, tick)
            return len(fetched)

    def _update_live_bar(self, code: str, quote, bounds) -> None:
        """Accumulate the current 15m bar from polled prices.

        The source only exposes day-level OHLC and the day's *cumulative* volume,
        so the bar is built from successive prices and volume deltas. `open` is
        the price first seen in the bucket, which is the best available
        approximation when the process starts mid-bar.
        """
        cumulative = float(quote.volume)
        previous_cum = self._cum_volume.get(code)
        delta = (
            max(0.0, cumulative - previous_cum)
            if previous_cum is not None and cumulative >= previous_cum
            else 0.0
        )
        self._cum_volume[code] = cumulative
        if bounds is None:
            # Outside a session a "live" bar would be a phantom: the last closed
            # bar already lives in the database and `_close_finished` flushes it.
            return
        start_ms, end_ms = bounds
        bucket = min(
            max(align_down(quote.timestamp_ms, CN_STEP_MS), start_ms),
            end_ms - CN_STEP_MS,
        )
        bar = self._live.get(code)
        if bar is None or bar.time != bucket:
            # With no previous bar the intra-bar path is unknown. Using the day's
            # open here would fabricate a wick from 09:30 to now; the first price
            # we actually saw is the honest choice.
            open_price = bar.close if bar is not None and bar.time < bucket else quote.price
            self._live[code] = CandlePoint(
                time=bucket,
                open=open_price,
                high=max(open_price, quote.price),
                low=min(open_price, quote.price),
                close=quote.price,
                volume=delta,
            )
            return
        self._live[code] = CandlePoint(
            time=bar.time,
            open=bar.open,
            high=max(bar.high, quote.price),
            low=min(bar.low, quote.price),
            close=quote.price,
            volume=bar.volume + delta,
        )

    def _close_finished(self, now: int) -> list[dict]:
        """Move completed live bars out of memory.

        During a session a bar is complete once a newer bucket has started.
        Outside a session (lunch break, after the close) every live bar is
        complete: no further trade can arrive in it.
        """
        current_bucket = align_down(now, CN_STEP_MS)
        open_now = is_session_open(now)
        closed: list[dict] = []
        with self._lock:
            for code, bar in list(self._live.items()):
                if open_now and bar.time >= current_bucket:
                    continue
                closed.append(
                    {
                        "symbol": code,
                        "timeframe": "15m",
                        "open_time": bar.time,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": round(max(bar.volume, 0.0), 2),
                    }
                )
                self._live.pop(code, None)
        return closed

    def _persist_closed(self, rows: list[dict]) -> None:
        """Write completed bars and fold them into the daily series.

        Without this the database only advances on a manual sync, so after a
        restart the app would show a realtime price next to yesterday's history,
        and every backtest would stop at the last sync date. Bars already present
        are skipped, and their volume is not re-added to the daily bar.
        """
        if not rows:
            return
        with self.db.session() as session:
            existing = {
                (symbol, ts)
                for symbol, ts in session.execute(
                    select(Candle.symbol, Candle.open_time).where(
                        Candle.symbol.in_({row["symbol"] for row in rows}),
                        Candle.timeframe == "15m",
                        Candle.open_time.in_({row["open_time"] for row in rows}),
                    )
                ).all()
            }
            fresh = [
                row for row in rows if (row["symbol"], row["open_time"]) not in existing
            ]
            if not fresh:
                return
            session.execute(insert(Candle), fresh)
            self._roll_daily(session, fresh)
        logger.debug("persisted %s closed A-share bars from realtime quotes", len(fresh))

    def _roll_daily(self, session, fresh: list[dict]) -> None:
        grouped: dict[tuple[str, int], list[dict]] = {}
        for row in fresh:
            day_open = int(
                datetime.combine(
                    day_key(row["open_time"]), datetime.min.time(), tzinfo=CN_TZ
                ).timestamp()
                * 1000
            )
            grouped.setdefault((row["symbol"], day_open), []).append(row)
        for (symbol, day_open), bars in grouped.items():
            bars.sort(key=lambda item: item["open_time"])
            existing = session.scalar(
                select(Candle).where(
                    Candle.symbol == symbol,
                    Candle.timeframe == "1d",
                    Candle.open_time == day_open,
                )
            )
            high = max(bar["high"] for bar in bars)
            low = min(bar["low"] for bar in bars)
            last_close = bars[-1]["close"]
            if existing is None:
                session.add(
                    Candle(
                        symbol=symbol,
                        timeframe="1d",
                        open_time=day_open,
                        open=bars[0]["open"],
                        high=high,
                        low=low,
                        close=last_close,
                        # Volume is not summed from 15m bars: a process started
                        # mid-session never saw the earlier trades. The quote
                        # carries the exchange's authoritative day total and
                        # `_sync_today_daily` writes it.
                        volume=0.0,
                    )
                )
                continue
            existing.high = max(existing.high, high)
            existing.low = min(existing.low, low)
            existing.close = last_close

    def _sync_today_daily(self, fetched: dict, now: int) -> None:
        """Write today's daily bar from the quote itself.

        The quote gives open/high/low/last and the day's cumulative volume, which
        *is* the daily bar. Deriving it from accumulated 15m bars instead loses
        every trade that happened before the process started, and after a restart
        it loses them twice.

        Only written during a session: outside it the stored row is already final,
        and re-writing `high`/`low` from a quote taken after the close could
        silently narrow a range that the exchange already published.
        """
        if not is_session_open(now):
            return
        day_open = int(
            datetime.combine(day_key(now), datetime.min.time(), tzinfo=CN_TZ).timestamp()
            * 1000
        )
        rows = [
            (code, quote)
            for code, quote in fetched.items()
            if quote.price > 0 and quote.volume > 0
        ]
        if not rows:
            return
        with self.db.session() as session:
            for code, quote in rows:
                existing = session.scalar(
                    select(Candle).where(
                        Candle.symbol == code,
                        Candle.timeframe == "1d",
                        Candle.open_time == day_open,
                    )
                )
                if existing is None:
                    session.add(
                        Candle(
                            symbol=code,
                            timeframe="1d",
                            open_time=day_open,
                            open=quote.open or quote.price,
                            high=max(quote.high, quote.price),
                            low=min(quote.low or quote.price, quote.price),
                            close=quote.price,
                            volume=round(float(quote.volume), 2),
                        )
                    )
                    continue
                # The exchange's day figures are authoritative and monotone within
                # a session, so assign rather than max/min: a previously stored
                # synthetic or half-imported range must not widen the real one.
                existing.open = existing.open or quote.open or quote.price
                existing.high = max(quote.high, quote.price)
                existing.low = min(quote.low or quote.price, quote.price)
                existing.close = quote.price
                existing.volume = round(float(quote.volume), 2)

    def _update_lock_state(
        self, code: str, price: float, prev_close: float, board: str, is_st: bool, tick: float
    ) -> None:
        band = price_band(prev_close, board, is_st, tick)
        if band is None:
            self._locked.pop(code, None)
            return
        if price >= band.up - tick / 2:
            self._locked[code] = "up"
        elif price <= band.down + tick / 2:
            self._locked[code] = "down"
        else:
            self._locked.pop(code, None)

    def _persist_prev_close(self, fetched: dict, meta: dict[str, tuple[str, bool, float, float]]) -> None:
        """Keep the stored reference price in step with the exchange.

        `prev_close` drives limit bands and change%; after a dividend or a new
        session it changes without any bar being written. Only real differences
        are committed so a 15s poll does not rewrite every row forever.
        """
        changed: list[tuple[str, float]] = []
        for code, quote in fetched.items():
            details = meta.get(code)
            if details is None or quote.prev_close <= 0:
                continue
            if abs(quote.prev_close - details[3]) > 1e-6:
                changed.append((code, float(quote.prev_close)))
        if not changed:
            return
        with self.db.session() as session:
            for code, prev_close in changed:
                row = session.scalar(select(Symbol).where(Symbol.code == code))
                if row is not None:
                    row.prev_close = prev_close
        logger.info("updated prev_close for %s symbols from realtime quotes", len(changed))

    # ---------------------------------------------------------------- reads
    def _max_age_ms(self) -> int:
        return self.session_max_age_ms if is_session_open(self.now_ms()) else self.closed_max_age_ms

    def live_quote(self, symbol: str) -> LiveQuote | None:
        code = symbol.upper()
        with self._lock:
            quote = self._quotes.get(code)
        if quote is None:
            return None
        if self.now_ms() - quote.fetched_at_ms > self._max_age_ms():
            return None
        return quote

    def live_candle(self, symbol: str) -> CandlePoint | None:
        code = symbol.upper()
        if not is_session_open(self.now_ms()):
            return None
        if self.live_quote(code) is None:
            return None
        with self._lock:
            return self._live.get(code)

    def is_locked(self, symbol: str) -> str:
        with self._lock:
            return self._locked.get(symbol.upper(), "")

    def reset_live(self) -> None:
        """Drop live bars and volume baselines after the history was replaced.

        Called by the sync endpoint: its purge deletes the bars these objects were
        derived from, and a retained volume baseline would make the next delta
        negative or double-count.
        """
        with self._lock:
            self._live.clear()
            self._cum_volume.clear()

    def prev_close(self, symbol: str) -> float:
        quote = self.live_quote(symbol)
        return quote.prev_close if quote is not None else 0.0

    def clock_info(self) -> dict:
        """Same shape as the synthetic engine's clock payload.

        /api/market/clock reads `cn.clock_info()` unconditionally, so a realtime
        replacement that omitted it would 500 the whole clock endpoint.
        """
        return self.clock.info()

    def snapshot_rows(self) -> list[dict]:
        now = self.now_ms()
        max_age = self._max_age_ms()
        with self._lock:
            quotes = list(self._quotes.values())
        rows = []
        for quote in sorted(quotes, key=lambda item: item.code):
            age = max(0, now - quote.fetched_at_ms)
            rows.append(
                {
                    "code": quote.code,
                    "price": quote.price,
                    "prev_close": quote.prev_close,
                    "open": quote.open,
                    "high": quote.high,
                    "low": quote.low,
                    "volume": quote.volume,
                    "timestamp": quote.timestamp_ms,
                    "fetched_at": quote.fetched_at_ms,
                    "age_ms": age,
                    "fresh": age <= max_age,
                    "source": quote.source,
                }
            )
        return rows

    def health(self) -> dict:
        now = self.now_ms()
        with self._lock:
            ages = [now - quote.fetched_at_ms for quote in self._quotes.values()]
            return {
                "source": getattr(self.source, "name", "tencent"),
                "mode": "realtime-poll",
                "poll_seconds": self.poll_seconds,
                "quotes": len(self._quotes),
                "last_poll": self._last_poll_ms,
                "last_poll_age_ms": (now - self._last_poll_ms) if self._last_poll_ms else -1,
                "worst_age_ms": max(ages) if ages else -1,
                "latency_ms": round(self._last_latency_ms, 1),
                "last_error": self._last_error,
                "session_open": is_session_open(now),
            }


def _default_source(settings: Settings):
    from app.modules.cn.sources.tencent import TencentSource

    return TencentSource(timeout=settings.request_timeout_seconds)
