"""Tencent market-data source.

Endpoints (public, no key required):

* quote     https://qt.gtimg.cn/q=sh600519                 (GBK text)
* daily     https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=sh600519,day,,,N,
            (unadjusted — matches the quote so change% and limit bands are correct)
* intraday  https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m15,,N

Field order for daily bars is [date, open, close, high, low, volume]; note the
*close-before-high* ordering, which is easy to get wrong and silently swaps
columns. Intraday bars are [YYYYMMDDHHMM, open, close, high, low, volume, ...].

Quotes are GBK encoded; decoding as UTF-8 raises, so the encoding is explicit.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.core.money import round_price
from app.modules.cn.calendar import CN_TZ
from app.modules.cn.sources.base import (
    BarBatch,
    Quote,
    SourceHealth,
    SourceUnavailable,
    split_code,
)
from app.modules.market.service_types import CandlePoint

logger = logging.getLogger(__name__)

QUOTE_URL = "https://qt.gtimg.cn/q={ticker}"
# Unadjusted daily bars (`day`), NOT forward-adjusted (`qfqday`).
# Real-time quotes from this source are unadjusted too; mixing qfq history with an
# unadjusted quote produced impossible changes such as +192% and put every symbol
# at its limit band.
DAILY_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={ticker},day,,,{limit},"
)
# Forward-adjusted series, fetched separately. `qfqday` is the exchange's own
# adjustment, so it removes splits/dividends without any inference on our side.
# Measured on 002594.SZ: raw showed -66.9% on 2025-07-29 (a split), qfq showed
# +0.37% — the same day, with every other day's return unchanged.
ADJUSTED_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={ticker},day,{start},{end},{limit},qfq"
)
# Date-ranged unadjusted query. The endpoint accepts `start,end` and returns up
# to ~640 rows per call, which is what makes multi-decade paging possible: the
# `kline?param=...,day,,,N` form silently caps at ~2000 rows no matter what N is.
RANGED_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
    "?param={ticker},day,{start},{end},{limit},"
)
# Same paging, forward-adjusted. Kept separate because the two endpoints return
# different keys (`day` vs `qfqday`) and mixing them would mean silently
# switching price basis mid-series — the exact defect this module warns about.
RANGED_ADJUSTED_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={ticker},day,{start},{end},{limit},qfq"
)
INTRADAY_URL = (
    "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={ticker},m15,,{limit}"
)

# Measured limits of the public endpoints (2026-10):
#   intraday 15m : 800 bars (~50 trading days); above that it returns 320
#   daily        : 2000 bars per *single* request; above that the payload breaks.
#                  Paging by date range bypasses this — see `daily_history`.
MAX_INTRADAY_BARS = 800
MAX_DAILY_BARS = 2000
# Rows a date-ranged daily request returns in one call.
RANGED_PAGE_BARS = 640


class TencentSource:
    name = "tencent"

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        retries: int = 3,
        backoff_seconds: float = 0.4,
    ) -> None:
        self.timeout = timeout
        self._transport = transport
        # Public endpoints throttle aggressively under burst load; a short
        # exponential backoff turns transient resets into a slow success.
        self.retries = max(1, retries)
        self.backoff_seconds = max(0.0, backoff_seconds)

    # ------------------------------------------------------------- internals
    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LumenQuant/0.1)",
                "Referer": "https://gu.qq.com/",
            },
        )

    def _get(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._client() as client:
                    response = client.get(url)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                # 4xx is a permanent client error; retrying cannot help.
                if response.status_code < 400:
                    return response
                if 400 <= response.status_code < 500:
                    raise SourceUnavailable(f"腾讯行情返回 {response.status_code}")
                last_error = SourceUnavailable(f"腾讯行情返回 {response.status_code}")
            if attempt + 1 < self.retries:
                delay = self.backoff_seconds * (2**attempt)
                # Jitter keeps parallel syncs from retrying in lockstep.
                delay *= 0.75 + random.random() * 0.5
                logger.debug("retrying %s in %.2fs (attempt %s)", url[:60], delay, attempt + 1)
                time.sleep(delay)
        raise SourceUnavailable(f"腾讯行情不可达：{last_error}") from last_error

    @staticmethod
    def _ticker(code: str) -> str:
        market, digits = split_code(code)
        return f"{market}{digits}"

    # ---------------------------------------------------------------- quotes
    @staticmethod
    def _fields_to_quote(code: str, fields: list[str]) -> Quote:
        def num(value: str, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        timestamp = fields[30]
        ts_ms = int(time.time() * 1000)
        if len(timestamp) == 14:
            try:
                parsed = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
            except ValueError:
                pass
            else:
                ts_ms = int(parsed.timestamp() * 1000)
        return Quote(
            code=code.upper(),
            name=fields[1],
            price=num(fields[3]),
            prev_close=num(fields[4]),
            open=num(fields[5]),
            high=num(fields[33]) if len(fields) > 33 else num(fields[3]),
            low=num(fields[34]) if len(fields) > 34 else num(fields[3]),
            volume=num(fields[6]),
            timestamp_ms=ts_ms,
        )

    def quote(self, code: str) -> Quote:
        ticker = self._ticker(code)
        response = self._get(QUOTE_URL.format(ticker=ticker))
        # GBK, not UTF-8: Chinese names would otherwise mojibake or raise.
        text = response.content.decode("gbk", errors="replace")
        payload = text.split('"')
        if len(payload) < 2 or "~" not in payload[1]:
            raise SourceUnavailable(f"无法解析 {code} 的行情响应")
        fields = payload[1].split("~")
        if len(fields) < 33:
            raise SourceUnavailable(f"{code} 行情字段不足（{len(fields)}）")
        return self._fields_to_quote(code, fields)

    def quotes(self, codes: list[str]) -> dict[str, Quote]:
        """Batch quotes: one HTTP request covers many symbols.

        The endpoint accepts comma-separated tickers and answers with one
        `v_<ticker>="..."` line per symbol. Polling a whole watchlist one request
        per symbol would multiply latency and rate-limit exposure, so the realtime
        loop always uses this path. Symbols missing from the response are simply
        absent from the result; a chunk that fails entirely is skipped rather
        than discarding quotes from the other chunks.
        """
        unique: list[str] = []
        for code in codes:
            normalized = code.strip().upper()
            if normalized and normalized not in unique:
                unique.append(normalized)
        if not unique:
            return {}

        out: dict[str, Quote] = {}
        errors: list[str] = []
        chunk_size = 60
        for start in range(0, len(unique), chunk_size):
            chunk = unique[start : start + chunk_size]
            by_ticker = {self._ticker(code): code for code in chunk}
            url = QUOTE_URL.format(ticker=",".join(by_ticker))
            try:
                response = self._get(url)
            except SourceUnavailable as exc:
                errors.append(str(exc))
                continue
            text = response.content.decode("gbk", errors="replace")
            for line in text.split(";"):
                line = line.strip()
                if not line.startswith("v_") or "=" not in line:
                    continue
                ticker, _, payload = line.partition("=")
                code = by_ticker.get(ticker[2:])
                if code is None:
                    continue
                fields = payload.strip().strip('"').split("~")
                if len(fields) < 33:
                    continue
                out[code] = self._fields_to_quote(code, fields)
        if not out and errors:
            raise SourceUnavailable(f"腾讯行情不可达：{errors[0]}")
        return out

    # ------------------------------------------------------------------ bars
    def daily_bars(self, code: str, *, limit: int = 800) -> BarBatch:
        ticker = self._ticker(code)
        url = DAILY_URL.format(ticker=ticker, limit=max(1, min(limit, MAX_DAILY_BARS)))
        response = self._get(url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceUnavailable("腾讯日线返回非 JSON") from exc
        node = (payload.get("data") or {}).get(ticker) or {}
        # Prefer the unadjusted series; fall back only if the endpoint changes.
        rows = node.get("day") or node.get("qfqday") or []
        if not rows:
            raise SourceUnavailable(f"{code} 无日线数据")
        bars = self._rows_to_bars(rows)
        return BarBatch(code=code.upper(), timeframe="1d", bars=bars)

    def adjusted_daily_bars(
        self, code: str, *, limit: int = MAX_DAILY_BARS
    ) -> BarBatch:
        """Forward-adjusted daily series (前复权), paged.

        Research must never run on raw prices: a split or a large dividend shows
        up as a catastrophic one-day drop that the raw series cannot distinguish
        from a real crash. Every indicator then lies for weeks — measured on
        300750.SZ, MA20 kept "falling" for 20 sessions after a 2023 transfer.

        Forward adjustment anchors the *latest* price at its real level, so the
        most recent bar still matches the live quote; only history is rebased.
        """
        batch = self._paged_daily(code, RANGED_ADJUSTED_URL, key="qfqday")
        return BarBatch(
            code=batch.code, timeframe="1d", bars=batch.bars[-limit:]
        )

    def daily_history(
        self, code: str, *, start: str = "1990-01-01", end: str = "2050-01-01"
    ) -> BarBatch:
        """Unadjusted daily bars for a date range, paged."""
        return self._paged_daily(code, RANGED_URL, key="day", start=start, end=end)

    def adjusted_history(
        self, code: str, *, start: str = "1990-01-01", end: str = "2050-01-01"
    ) -> BarBatch:
        """Forward-adjusted daily bars for a date range, paged.

        This is the series research should use: it removes ex-rights gaps while
        still starting at whatever date the caller asks for.
        """
        return self._paged_daily(
            code, RANGED_ADJUSTED_URL, key="qfqday", start=start, end=end
        )

    def _paged_daily(
        self,
        code: str,
        template: str,
        *,
        key: str,
        start: str = "1990-01-01",
        end: str = "2050-01-01",
    ) -> BarBatch:
        """Walk a daily series backwards, past the per-request row cap.

        Measured behaviour of the endpoint (2026-10): `start` alone is *ignored* —
        a request for 2010-2012 came back as the most recent 640 rows. Only
        `end` is honoured. So paging moves `end` to the day before the earliest
        bar received, which reaches any listing date.

        Without this a "since 2001" request returned the newest 640 bars, which
        is why multi-year A-share research was impossible.
        """
        accumulated: dict[int, CandlePoint] = {}
        cursor_end = end
        ticker = self._ticker(code)
        for _ in range(80):  # 80 * 640 rows far exceeds any listing history
            url = template.format(
                ticker=ticker, start=start, end=cursor_end, limit=RANGED_PAGE_BARS
            )
            response = self._get(url)
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceUnavailable("腾讯日线返回非 JSON") from exc
            node = (payload.get("data") or {}).get(ticker) or {}
            rows = node.get(key) or node.get("day") or []
            if not rows:
                break
            page = self._rows_to_bars(rows)
            if not page:
                break
            for bar in page:
                accumulated[bar.time] = bar
            earliest = page[0]
            earliest_day = datetime.fromtimestamp(earliest.time / 1000, CN_TZ)
            if earliest_day.strftime("%Y-%m-%d") <= start:
                break  # reached the requested lower bound
            if len(rows) < RANGED_PAGE_BARS:
                break  # reached the listing date
            # Step back one calendar day and keep walking into the past.
            previous = earliest_day - timedelta(days=1)
            next_end = previous.strftime("%Y-%m-%d")
            if next_end >= cursor_end:
                break  # no backward progress; stop rather than loop
            cursor_end = next_end

        bars = [accumulated[stamp] for stamp in sorted(accumulated)]
        if not bars:
            raise SourceUnavailable(f"{code} 无日线数据")
        return BarBatch(code=code.upper(), timeframe="1d", bars=bars)

    def _rows_to_bars(self, rows: list) -> list[CandlePoint]:
        """Tencent daily row -> CandlePoint.

        Field order is [date, open, close, high, low, volume] — close comes
        *before* high, which is easy to get wrong and silently swaps columns.
        """
        bars: list[CandlePoint] = []
        for row in rows:
            if len(row) < 6:
                continue
            try:
                day = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").replace(
                    tzinfo=CN_TZ
                )
                open_p, close_p, high_p, low_p = (
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                )
                volume = float(row[5])
            except (TypeError, ValueError):
                continue
            bars.append(
                CandlePoint(
                    time=int(day.timestamp() * 1000),
                    open=round_price(open_p),
                    high=round_price(max(high_p, open_p, close_p)),
                    low=round_price(min(low_p, open_p, close_p)),
                    close=round_price(close_p),
                    volume=volume,
                )
            )
        bars.sort(key=lambda bar: bar.time)
        return bars

    def intraday_bars(self, code: str, *, limit: int = 320) -> BarBatch:
        ticker = self._ticker(code)
        # Hard upstream cap. Above ~800 the endpoint silently returns 320 rows
        # instead — measured 2026-10: 800 -> 800 bars, 900 -> 320. Clamping here
        # keeps the caller's intent ("as much as possible") explicit rather than
        # letting a larger request collapse to a smaller result.
        requested = max(1, min(limit, MAX_INTRADAY_BARS))
        url = INTRADAY_URL.format(ticker=ticker, limit=requested)
        response = self._get(url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceUnavailable("腾讯分钟线返回非 JSON") from exc
        node = (payload.get("data") or {}).get(ticker) or {}
        rows = node.get("m15") or []
        if not rows:
            raise SourceUnavailable(f"{code} 无分钟线数据")

        timezone_utc = timezone.utc
        bars: list[CandlePoint] = []
        for row in rows:
            if len(row) < 6:
                continue
            stamp = str(row[0])
            if len(stamp) < 12:
                continue
            try:
                local = datetime.strptime(stamp[:12], "%Y%m%d%H%M").replace(tzinfo=CN_TZ)
            except ValueError:
                continue
            open_p, close_p, high_p, low_p = (
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
            )
            bars.append(
                CandlePoint(
                    time=int(local.astimezone(timezone_utc).timestamp() * 1000),
                    open=round_price(open_p),
                    high=round_price(max(high_p, open_p, close_p)),
                    low=round_price(min(low_p, open_p, close_p)),
                    close=round_price(close_p),
                    volume=float(row[5]),
                )
            )
        bars.sort(key=lambda bar: bar.time)
        return BarBatch(code=code.upper(), timeframe="15m", bars=bars)

    # ---------------------------------------------------------------- health
    def health(self) -> SourceHealth:
        started = time.perf_counter()
        try:
            self.quote("600519.SH")
        except Exception as exc:  # health must never raise
            return SourceHealth(
                name=self.name,
                available=False,
                detail=str(exc)[:200],
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        return SourceHealth(
            name=self.name,
            available=True,
            detail="ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
