"""Realtime crypto engine backed by Binance public market data.

Mirrors `CnQuoteEngine`: poll tickers, accumulate the in-progress base bar, flush
finished bars to the database. Two differences matter:

* crypto trades 24/7, so there is no session gate and no休市 downshift;
* history is fetched once at startup instead of being seeded synthetically, and
  that fetch replaces the synthetic series outright — the same "one price
  universe per symbol" rule that applies to A-shares.

The daily series is imported natively so backtests get years of history without
needing 35,000 15m bars per symbol.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from sqlalchemy import delete, func, insert, select

from app.core.config import Settings
from app.core.db import Database
from app.modules.market.engine import DEFAULT_SYMBOLS
from app.modules.market.feed import MarketClock, align_down
from app.modules.market.models import Candle, Symbol
from app.modules.market.service_types import CandlePoint
from app.modules.market.sources.binance_public import (
    BinancePublicSource,
    CryptoSourceUnavailable,
)

logger = logging.getLogger(__name__)

CRYPTO_STEP_MS = 900_000
DAILY_MS = 86_400_000
# A bar closes when the *next* bucket starts, so a continuously observed bar
# always spans exactly one bucket. Anything larger means at least one bucket went
# unseen: the bar then holds only the samples before and after a gap, and storing
# it would record a fabricated high/low and volume 0.
MAX_TRUSTED_GAP_BUCKETS = 1


class CryptoQuoteEngine:
    name = "binance-realtime"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        source: BinancePublicSource | None = None,
        now_ms=None,
        clock: MarketClock | None = None,
    ) -> None:
        self.db = database
        self.settings = settings
        self.source = source or BinancePublicSource(
            base_url=settings.crypto_data_base_url,
            timeout=settings.request_timeout_seconds,
        )
        self.poll_seconds = max(1.0, float(settings.crypto_quote_poll_seconds))
        # A crypto quote is stale much faster than an A-share one: the market
        # never closes, so there is no legitimate quiet period to hide behind.
        self.max_age_ms = int(max(15.0, self.poll_seconds * 5) * 1000)
        # `clock` exists so MarketService and the snapshots endpoint can timestamp
        # responses; crypto runs on wall time, speed 1.
        self.clock = clock or MarketClock(speed=1.0)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._tickers: dict[str, object] = {}
        self._live: dict[str, CandlePoint] = {}
        self._lock = threading.Lock()
        self._last_poll_ms = 0
        self._last_latency_ms = 0.0
        self._last_error = ""
        self._failures = 0
        self._outage_reported = False
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    def ensure_symbols(self) -> None:
        with self.db.session() as session:
            existing = {row.code for row in session.scalars(select(Symbol)).all()}
            for code, name, price, volume_bias in DEFAULT_SYMBOLS:
                if code in existing:
                    continue
                session.add(
                    Symbol(
                        code=code,
                        name=name,
                        market="crypto",
                        base=code.removesuffix("USDT"),
                        quote="USDT",
                        initial_price=price,
                        volume_bias=volume_bias,
                    )
                )

    async def start(self) -> None:
        self.ensure_symbols()
        try:
            await asyncio.to_thread(self.sync_history)
        except Exception:
            logger.warning("crypto history import failed; keeping local bars", exc_info=True)
        try:
            await asyncio.to_thread(self.poll_once)
        except Exception:
            logger.warning("initial crypto quote poll failed; will retry", exc_info=True)
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="crypto-quote-engine")
        logger.info(
            "crypto realtime engine started (poll=%ss, symbols=%s)",
            self.poll_seconds,
            len(self._tickers),
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
                # A resolver hiccup is expected and self-healing; a full traceback
                # every 5 seconds buries the lines that matter.
                logger.warning("crypto quote poll failed: %s", str(exc)[:160])
            try:
                await asyncio.to_thread(self._check_health)
            except Exception:
                logger.exception("crypto quote health check failed")

    def _check_health(self) -> None:
        """Raise/clear an alert when the Binance feed dies or recovers.

        Crypto trades 24/7, so there is no "market is closed" excuse for silence:
        a gap here always means the feed is broken.
        """
        from app.modules.alerts.service import AlertService

        now = self.now_ms()
        with self._lock:
            last_poll = self._last_poll_ms
            failures = self._failures
            error = self._last_error
        stale = bool(last_poll) and (now - last_poll) > self.max_age_ms * 2
        broken = failures >= 3 or stale
        if broken and not self._outage_reported:
            if last_poll:
                gap = f"最近报价已 {max(0, now - last_poll) // 1000}s 未更新。"
            else:
                # Never polled successfully: subtracting from 0 would report the
                # whole Unix epoch as the gap, which looks like an alert bug.
                gap = "启动后从未成功获取报价。"
            with self.db.session() as session:
                AlertService(session).raise_alert(
                    category="data_feed",
                    level="critical",
                    title="加密实时行情中断",
                    message=(
                        f"连续失败 {failures} 次；{gap}"
                        f"原因：{error or '未知'}。交易仍在用最后一根已存 K 线估值。"
                    ),
                    ref_type="data_feed",
                    ref_id="crypto",
                    commit=True,
                )
            self._outage_reported = True
            logger.error("crypto quote feed outage recorded: %s", error)
            return
        if not broken and self._outage_reported:
            with self.db.session() as session:
                alerts = AlertService(session)
                for row in alerts.list(
                    category="data_feed", unacknowledged_only=True, limit=50
                ):
                    if row.ref_id == "crypto":
                        alerts.acknowledge(row.id)
                        break
            self._outage_reported = False
            logger.info("crypto quote feed recovered")

    def _next_delay(self) -> float:
        """Exponential backoff while failing, capped so recovery stays quick.

        Polling every 5s through a DNS outage both wastes time and makes the
        outage longer. Backing off to 60s keeps a broken network from being
        hammered while still recovering within a minute.
        """
        with self._lock:
            failures = self._failures
        if failures <= 0:
            return self.poll_seconds
        return min(60.0, self.poll_seconds * (2**failures))

    # -------------------------------------------------------------- history
    def sync_history(
        self, *, intraday_limit: int = 1000, daily_limit: int = 1000, force: bool = False, max_age_ms: int = 1_800_000
    ) -> int:
        """Replace each symbol's synthetic series with real Binance history.

        The purge happens only after the fetch succeeded, and it removes *every*
        stored timeframe for that symbol: a leftover synthetic 1d row would sit
        next to real 15m bars and corrupt both change% and backtest returns.

        Re-importing on every start would re-download ~2000 bars per symbol for
        data that is already present and at most one bar stale. Freshness is
        checked per symbol and `force=True` (the /sync endpoint) overrides it.
        """
        codes = [
            code
            for code, *_ in DEFAULT_SYMBOLS
        ]
        imported = 0
        for code in codes:
            if not force and self._history_is_fresh(code, max_age_ms):
                continue
            try:
                daily = self.source.klines(code, interval="1d", limit=daily_limit)
                intraday = self.source.klines(code, interval="15m", limit=intraday_limit)
            except CryptoSourceUnavailable as exc:
                logger.warning("crypto history import failed for %s: %s", code, exc)
                continue
            if not daily and not intraday:
                continue
            with self.db.session() as session:
                session.execute(delete(Candle).where(Candle.symbol == code))
                rows = [
                    {
                        "symbol": code,
                        "timeframe": "15m",
                        "open_time": bar.time,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                    for bar in intraday
                ] + [
                    {
                        "symbol": code,
                        "timeframe": "1d",
                        "open_time": bar.time,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                    for bar in daily
                ]
                for start in range(0, len(rows), 2000):
                    session.execute(insert(Candle), rows[start : start + 2000])
                imported += len(rows)
            logger.info(
                "imported %s real bars for %s (%s daily, %s intraday)",
                len(daily) + len(intraday),
                code,
                len(daily),
                len(intraday),
            )
        # The purge replaced every bar the accumulator was built against; keeping
        # it would re-emit a bar whose stored volume has just been rewritten.
        with self._lock:
            self._live.clear()
        return imported

    def _history_is_fresh(self, code: str, max_age_ms: int) -> bool:
        """True when the stored 15m series already reaches the current bucket."""
        with self.db.session() as session:
            latest = session.scalar(
                select(func.max(Candle.open_time)).where(
                    Candle.symbol == code, Candle.timeframe == "15m"
                )
            )
        if latest is None:
            return False
        age = self.now_ms() - int(latest)
        # A bar stamped in the future cannot come from the exchange: it is left over
        # from the synthetic engine, whose 60x clock runs ahead of wall time. The
        # plain `age <= max_age_ms` test evaluated *negative* for those rows and
        # reported them as "fresh", so switching to real data skipped the import
        # entirely and left ~23k synthetic bars (spanning months into the future)
        # in place next to live quotes.
        if age < 0:
            return False
        # The newest bar can legitimately be one bucket behind "now": the current
        # bucket is still open and only lands in the database after it closes.
        return age <= max_age_ms

    # ----------------------------------------------------------------- poll
    def now_ms(self) -> int:
        return self._now_ms()

    def poll_once(self) -> int:
        with self.db.session() as session:
            symbols = [
                row.code
                for row in session.scalars(
                    select(Symbol).where(
                        Symbol.market == "crypto", Symbol.enabled.is_(True)
                    )
                ).all()
            ]
        if not symbols:
            return 0
        started = time.perf_counter()
        try:
            fetched = self.source.tickers(symbols)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:200]
                self._failures += 1
            raise
        latency_ms = (time.perf_counter() - started) * 1000
        now = self.now_ms()
        closed = self._close_finished(now)
        if closed:
            self._persist_closed(closed)
        with self._lock:
            for code, ticker in fetched.items():
                self._tickers[code] = ticker
                self._accumulate(code, ticker)
            self._last_poll_ms = now
            self._last_latency_ms = latency_ms
            self._last_error = ""
            self._failures = 0
        return len(fetched)

    def _accumulate(self, code: str, ticker) -> None:
        bucket = align_down(int(ticker.timestamp_ms), CRYPTO_STEP_MS)
        bar = self._live.get(code)
        if bar is None or bar.time != bucket:
            # A newer bucket means the previous bar has been flushed already.
            # When starting mid-bar the first observed price is the honest open.
            #
            # Volume stays 0 on purpose: the 24h ticker reports a *rolling*
            # turnover, not this candle's volume, so differencing it would
            # attribute up to a day of trades to a 15-minute bar. The imported
            # kline already carries the real candle volume, and MarketService
            # adds this bar on top of it when merging.
            open_price = bar.close if bar is not None and bar.time < bucket else ticker.price
            self._live[code] = CandlePoint(
                time=bucket,
                open=open_price,
                high=max(open_price, ticker.price),
                low=min(open_price, ticker.price),
                close=ticker.price,
                volume=0.0,
            )
            return
        self._live[code] = CandlePoint(
            time=bar.time,
            open=bar.open,
            high=max(bar.high, ticker.price),
            low=min(bar.low, ticker.price),
            close=ticker.price,
            volume=bar.volume,
        )

    def _close_finished(self, now: int) -> list[dict]:
        current_bucket = align_down(now, CRYPTO_STEP_MS)
        closed: list[dict] = []
        with self._lock:
            for code, bar in list(self._live.items()):
                if bar.time >= current_bucket:
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
                        "volume": round(max(bar.volume, 0.0), 8),
                    }
                )
                self._live.pop(code, None)
        return closed

    def _persist_closed(self, rows: list[dict]) -> None:
        """Store closed bars, using klines as the source of truth.

        One fetch per symbol per closed bucket (every 15 minutes) serves three
        purposes at once:

        * the closing bar gets its real OHLCV instead of the sampled range and
          volume 0 that a ticker poll can produce (the 24h field is a rolling
          turnover, not a candle volume);
        * buckets missed by a polling outage are imported rather than left absent;
        * rows previously stored with volume 0 are repaired while the
          authoritative candle is in hand.

        A bar that was only observed at the edges of an outage is dropped: its
        high/low never saw the middle of the period, so persisting it would be a
        fabricated candle. The import replaces it with the real one.
        """
        if not rows:
            return
        current_bucket = align_down(self.now_ms(), CRYPTO_STEP_MS)
        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            by_symbol.setdefault(row["symbol"], []).append(row)

        for code, closing in by_symbol.items():
            oldest = min(row["open_time"] for row in closing)
            missed = max(0, (current_bucket - oldest) // CRYPTO_STEP_MS)
            # Enough history to cover the outage plus the bars being closed.
            limit = max(12, min(500, int(missed) + 4))
            try:
                fetched = self.source.klines(code, interval="15m", limit=limit)
            except Exception as exc:
                logger.warning(
                    "crypto kline fetch failed for %s; storing accumulated bars: %s",
                    code,
                    exc,
                )
                fetched = []

            authoritative = {
                bar.time: bar for bar in fetched if bar.time < current_bucket
            }
            accepted: dict[int, dict] = {}
            for row in closing:
                source_bar = authoritative.get(row["open_time"])
                if source_bar is not None:
                    accepted[row["open_time"]] = {
                        **row,
                        "open": source_bar.open,
                        "high": source_bar.high,
                        "low": source_bar.low,
                        "close": source_bar.close,
                        "volume": source_bar.volume,
                    }
                    continue
                if current_bucket - row["open_time"] > (
                    MAX_TRUSTED_GAP_BUCKETS * CRYPTO_STEP_MS
                ):
                    # Edge samples only: not a bar, and the import below supplies
                    # the real one if upstream has it.
                    continue
                accepted[row["open_time"]] = row

            # Gaps: every authoritative bucket that is not the in-progress one.
            for bar_time, bar in authoritative.items():
                if bar_time not in accepted:
                    accepted[bar_time] = {
                        "symbol": code,
                        "timeframe": "15m",
                        "open_time": bar.time,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }

            if not accepted:
                continue
            self._write_bars(code, list(accepted.values()))

    def _write_bars(self, code: str, rows: list[dict]) -> None:
        """Insert new bars, repair volume-less ones, and roll the daily series."""
        with self.db.session() as session:
            existing = {
                row.open_time: row
                for row in session.scalars(
                    select(Candle).where(
                        Candle.symbol == code,
                        Candle.timeframe == "15m",
                        Candle.open_time.in_({row["open_time"] for row in rows}),
                    )
                ).all()
            }
            fresh: list[dict] = []
            repaired = 0
            for row in rows:
                stored = existing.get(row["open_time"])
                if stored is None:
                    fresh.append(row)
                    continue
                if stored.volume <= 0 and row["volume"] > 0:
                    stored.open, stored.high = row["open"], row["high"]
                    stored.low, stored.close = row["low"], row["close"]
                    stored.volume = row["volume"]
                    repaired += 1
            if not fresh and not repaired:
                return
            if fresh:
                session.execute(insert(Candle), fresh)
                self._roll_daily(session, fresh)
        if fresh or repaired:
            logger.debug(
                "stored %s crypto bars for %s (%s repaired)",
                len(fresh),
                code,
                repaired,
            )

    def _roll_daily(self, session, fresh: list[dict]) -> None:
        """Extend the current UTC day's bar.

        Binance daily klines start at 00:00 UTC, so the bucket key must use UTC —
        using the local zone would create a second, misaligned daily series and
        every backtest would see two 2026-10-21 bars.
        """
        grouped: dict[tuple[str, int], list[dict]] = {}
        for row in fresh:
            day_open = row["open_time"] - (row["open_time"] % DAILY_MS)
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
            if existing is None:
                # Only reached when the import did not cover this day at all, so
                # the accumulator's own (zero) volume is the only number there is.
                session.add(
                    Candle(
                        symbol=symbol,
                        timeframe="1d",
                        open_time=day_open,
                        open=bars[0]["open"],
                        high=high,
                        low=low,
                        close=bars[-1]["close"],
                        volume=round(sum(bar["volume"] for bar in bars), 8),
                    )
                )
                continue
            existing.high = max(existing.high, high)
            existing.low = min(existing.low, low)
            existing.close = bars[-1]["close"]
            # Volume is deliberately left alone. The daily row was imported from
            # Binance with that day's turnover, and the accumulator reports 0
            # because a rolling 24h ticker cannot yield a per-candle volume.
            # Adding the two would either double-count or add nothing; leaving
            # the imported figure is the only value that stays honest.

    # ---------------------------------------------------------------- reads
    def live_quote(self, symbol: str):
        code = symbol.upper()
        with self._lock:
            ticker = self._tickers.get(code)
        if ticker is None:
            return None
        if self.now_ms() - int(ticker.timestamp_ms) > self.max_age_ms:
            # closeTime of a 24h ticker is "now"; a stale one means the poller
            # stopped, and a dead price must not be presented as live.
            return None
        return ticker

    def live_candle(self, symbol: str) -> CandlePoint | None:
        code = symbol.upper()
        if self.live_quote(code) is None:
            return None
        with self._lock:
            return self._live.get(code)

    def clock_info(self) -> dict:
        return {
            "simulated_now": self.now_ms(),
            "speed": 1.0,
            "timeframe": "15m",
            "tick_seconds": self.poll_seconds,
            "source": self.name,
            "simulated": False,
        }

    def health(self) -> dict:
        now = self.now_ms()
        with self._lock:
            ages = [now - int(t.timestamp_ms) for t in self._tickers.values()]
            return {
                "source": getattr(self.source, "name", "binance-public"),
                "mode": "realtime-poll",
                "poll_seconds": self.poll_seconds,
                "symbols": len(self._tickers),
                "last_poll_age_ms": (now - self._last_poll_ms) if self._last_poll_ms else -1,
                "worst_age_ms": max(ages) if ages else -1,
                "latency_ms": round(self._last_latency_ms, 1),
                "last_error": self._last_error,
            }

    def rows(self) -> list[dict]:
        now = self.now_ms()
        with self._lock:
            tickers = list(self._tickers.values())
        out = []
        for ticker in sorted(tickers, key=lambda item: item.symbol):
            age = max(0, now - int(ticker.timestamp_ms))
            out.append(
                {
                    "code": ticker.symbol,
                    "price": ticker.price,
                    "prev_close": ticker.prev_close,
                    "open": ticker.open,
                    "high": ticker.high,
                    "low": ticker.low,
                    "volume": ticker.volume,
                    "timestamp": ticker.timestamp_ms,
                    "age_ms": age,
                    "fresh": age <= self.max_age_ms,
                }
            )
        return out
