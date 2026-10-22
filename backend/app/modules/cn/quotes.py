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
from datetime import datetime, time as dtime

from sqlalchemy import func, insert, select

from app.core.config import Settings
from app.core.db import Database
from app.modules.cn.calendar import (
    CN_TZ,
    day_key,
    is_session_open,
    is_trading_day,
    session_bounds,
    session_state,
)
from app.modules.cn.rules import price_band
from app.modules.market.feed import align_down
from app.modules.market.models import Candle, Symbol
from app.modules.market.service_types import CandlePoint

logger = logging.getLogger(__name__)

CN_STEP_MS = 900_000
# A bar closes when the *next* bucket starts, so a continuously observed bar
# always spans exactly one bucket. Anything larger means at least one bucket went
# unseen: the bar then holds only the samples before and after a gap, and storing
# it would record a high/low that never saw the middle of the period.
MAX_TRUSTED_GAP_BUCKETS = 1


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


def _has_traded(quote_ts_ms: int) -> bool:
    """Whether a quote stamped at this time can describe a real session bar.

    Continuous trading runs 09:30–11:30 and 13:00–15:00. The opening auction
    (09:15–09:30) produces a price but no cumulative volume and no O/H/L, so a
    daily bar built from it is an artefact. The closing auction's print is
    stamped at 15:00:00 and *is* valid, hence the inclusive end.
    """
    local = datetime.fromtimestamp(quote_ts_ms / 1000, CN_TZ)
    moment = local.time()
    return (
        dtime(9, 30) <= moment <= dtime(11, 30)
        or dtime(13, 0) <= moment <= dtime(15, 0)
    )


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
        # Health guard state: which condition is currently reported, so a
        # persistent outage raises one alert instead of one per poll, and a
        # recovery is announced exactly once.
        self._outage_reported = False
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.clock = RealtimeSessionClock(self)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        # A deployment with no stored history is the normal case for a fresh
        # install, and the engine alone would only ever produce bars from *now*
        # onward: the chart opens empty and every backtest fails with
        # "数据不足". The crypto engine imports history at startup; A-shares must
        # do the same instead of depending on the user remembering
        # LQ_CN_SYNC_ON_START.
        try:
            await asyncio.to_thread(self._import_missing_history)
        except Exception:
            logger.warning("A-share startup history import failed", exc_info=True)
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

    def _import_missing_history(self) -> int:
        """Fill symbols that have no stored history yet.

        Only symbols with zero daily bars are imported: re-importing everything on
        every boot would re-download years of data for rows that are already there.
        `/cn/data/sync` remains the explicit way to force a refresh.
        """
        from app.modules.cn.sync import CnDataSyncService

        with self.db.session() as session:
            rows = session.scalars(
                select(Symbol).where(Symbol.market == "cn_stock", Symbol.enabled.is_(True))
            ).all()
            empty = [
                row.code
                for row in rows
                if session.scalar(
                    select(func.count(Candle.id)).where(
                        Candle.symbol == row.code, Candle.timeframe == "1d"
                    )
                )
                == 0
            ]
        if not empty:
            return 0
        with self.db.session() as session:
            service = CnDataSyncService(session, self.source)
            report = service.sync_symbols(
                empty,
                daily_limit=self.settings.cn_sync_daily_limit,
                intraday_limit=self.settings.cn_sync_intraday_limit,
            )
        imported = sum(item.daily_bars + item.intraday_bars for item in report.symbols)
        if imported:
            logger.info(
                "imported %s bars of A-share history for %s symbols at startup",
                imported,
                len(empty),
            )
        return imported

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
            try:
                await asyncio.to_thread(self._check_health)
            except Exception:
                logger.exception("A-share quote health check failed")

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

    def _check_health(self) -> None:
        """Raise/clear an alert when the quote feed dies or recovers.

        Nothing else notices a dead feed: every consumer silently falls back to
        the last stored bar, so the platform keeps serving prices that look real.
        An operator watching a position needs to be told the feed is gone.
        """
        from app.modules.alerts.service import AlertService

        now = self.now_ms()
        with self._lock:
            last_poll = self._last_poll_ms
            failures = self._failures
            error = self._last_error
        # Outside a session the poller is deliberately slow, so a long gap is
        # normal; only an actual failure streak should alert then.
        open_now = is_session_open(now)
        stale_after_ms = self._max_age_ms() * 2
        stale = bool(last_poll) and (now - last_poll) > stale_after_ms
        broken = failures >= 3 or (open_now and stale)
        if broken and not self._outage_reported:
            if last_poll:
                gap = f"最近报价已 {max(0, now - last_poll) // 1000}s 未更新。"
            else:
                # Without this the message subtracts from 0 and reports the whole
                # Unix epoch ("1,792,631,255s"), which reads like a bug in the
                # alert rather than a feed that never came up.
                gap = "启动后从未成功获取报价。"
            with self.db.session() as session:
                AlertService(session).raise_alert(
                    category="data_feed",
                    level="critical",
                    title="A 股实时行情中断",
                    message=(
                        f"连续失败 {failures} 次；{gap}"
                        f"原因：{error or '未知'}。交易仍在用最后一根已存 K 线估值。"
                    ),
                    ref_type="data_feed",
                    ref_id="cn",
                    commit=True,
                )
            self._outage_reported = True
            logger.error("A-share quote feed outage recorded: %s", error)
            return
        if not broken and self._outage_reported:
            with self.db.session() as session:
                alerts = AlertService(session)
                for row in alerts.list(
                    category="data_feed", unacknowledged_only=True, limit=50
                ):
                    if row.ref_id == "cn":
                        alerts.acknowledge(row.id)
                        break
            self._outage_reported = False
            logger.info("A-share quote feed recovered")
        return

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
        """Write completed bars, repairing gaps from the intraday endpoint.

        Without this the database only advances on a manual sync, so after a
        restart the app would show a realtime price next to yesterday's history,
        and every backtest would stop at the last sync date.

        A poll outage leaves two problems that look like data rather than errors:

        * the accumulated bar spans the whole gap, so its high/low come from the
          samples at the edges only;
        * every bucket inside the gap is simply absent.

        Both are fixed by fetching the real 15m series for the affected symbols
        whenever a bar closes. A bar whose span exceeds the trusted window is
        dropped rather than stored: it is not a bar, it is two samples.
        """
        if not rows:
            return
        current_bucket = align_down(self.now_ms(), CN_STEP_MS)
        by_symbol: dict[str, list[dict]] = {}
        for row in rows:
            by_symbol.setdefault(row["symbol"], []).append(row)

        for code, closing in by_symbol.items():
            oldest = min(row["open_time"] for row in closing)
            missed = max(0, (current_bucket - oldest) // CN_STEP_MS)
            # The endpoint returns the most recent N bars; one session is 16 bars,
            # so allow enough to cover a multi-day outage without over-fetching.
            limit = max(32, min(320, int(missed) + 8))
            authoritative: dict[int, dict] = {}
            try:
                batch = self.source.intraday_bars(code, limit=limit)
            except Exception as exc:
                # Daily bars and the quote path are unaffected; keep the bars we
                # did observe and let the next successful fetch repair the rest.
                logger.warning(
                    "A-share intraday fetch failed for %s; storing accumulated bars: %s",
                    code,
                    exc,
                )
                batch = None
            if batch is not None:
                for bar in batch.bars:
                    if bar.time < current_bucket:
                        authoritative[bar.time] = {
                            "symbol": code,
                            "timeframe": "15m",
                            "open_time": bar.time,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                        }

            accepted: dict[int, dict] = {}
            for row in closing:
                source_bar = authoritative.get(row["open_time"])
                if source_bar is not None:
                    # Keeps the exchange's range and volume instead of our sampled
                    # approximation; the gap fill below covers the other buckets.
                    accepted[row["open_time"]] = source_bar
                    continue
                if current_bucket - row["open_time"] > (
                    MAX_TRUSTED_GAP_BUCKETS * CN_STEP_MS
                ):
                    continue
                accepted[row["open_time"]] = row
            for bar_time, bar in authoritative.items():
                accepted.setdefault(bar_time, bar)

            if accepted:
                # When the endpoint was unreachable, `accepted` holds sampled bars.
                # They must not overwrite a row that already came from the exchange
                # — that would replace a precise range with a coarser one.
                self._write_bars(
                    code, list(accepted.values()), authoritative=bool(authoritative)
                )

    def _write_bars(
        self, code: str, rows: list[dict], *, authoritative: bool = True
    ) -> None:
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
                # Always take the authoritative values. An accumulated bar has a
                # non-zero volume too, so the old `volume <= 0` condition left every
                # sampled high/low permanently in place: measured on real data, a
                # 15m bucket stored high 42.19 while the exchange said 42.30 — the
                # 0.11 gap came from samples that missed the peak and could never be
                # corrected afterwards.
                if not authoritative:
                    # Endpoint unreachable: keep whatever exact row already exists
                    # and only fill genuinely missing volume.
                    if stored.volume > 0:
                        continue
                if (
                    stored.open != row["open"]
                    or stored.high != row["high"]
                    or stored.low != row["low"]
                    or stored.close != row["close"]
                    or stored.volume != row["volume"]
                ):
                    stored.open, stored.high = row["open"], row["high"]
                    stored.low, stored.close = row["low"], row["close"]
                    stored.volume = row["volume"]
                    repaired += 1
            if not fresh and not repaired:
                return
            if fresh:
                session.execute(insert(Candle), fresh)
                self._roll_daily(session, fresh)
        logger.debug(
            "persisted %s A-share bars for %s (%s repaired)", len(fresh), code, repaired
        )

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
        """Write the daily bar for the quote's own trading day.

        The quote gives open/high/low/last and the day's cumulative volume, which
        *is* the daily bar. Deriving it from accumulated 15m bars instead loses
        every trade that happened before the process started, and after a restart
        it loses them twice.

        The day is taken from the **quote timestamp**, not from `now`:

        * after 15:00 the quote is stamped at the close and still describes today,
          so the closing-auction print is captured instead of being frozen at the
          last in-session poll;
        * before 09:30 the quote still belongs to yesterday, so no phantom bar is
          created for a day that has not traded yet.

        A quote stamped inside today's opening auction (09:15–09:30) is also
        rejected: the session has not produced a real open yet, and the cumulative
        volume is still 0 — a row created there would be an intraday artefact that
        the first real bar then has to overwrite. The volume check below already
        caught this, but relying on it was luck, not a rule.
        """
        rows = [
            (code, quote)
            for code, quote in fetched.items()
            if quote.price > 0
            and quote.volume > 0
            and quote.timestamp_ms > 0
            and _has_traded(quote.timestamp_ms)
        ]
        if not rows:
            return
        with self.db.session() as session:
            for code, quote in rows:
                quote_day = day_key(quote.timestamp_ms)
                if not is_trading_day(quote_day):
                    continue
                day_open = int(
                    datetime.combine(quote_day, datetime.min.time(), tzinfo=CN_TZ).timestamp()
                    * 1000
                )
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
                # The exchange's day figures are authoritative, so assign rather
                # than max/min: a previously stored synthetic or half-imported
                # range must not widen the real one. `open` is assigned too — an
                # earlier `_roll_daily` row may hold the first *observed* 15m
                # price, which is not the session's opening auction.
                existing.open = quote.open or existing.open or quote.price
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
