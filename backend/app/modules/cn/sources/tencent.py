"""Tencent market-data source.

Endpoints (public, no key required):

* quote     https://qt.gtimg.cn/q=sh600519                 (GBK text)
* daily     https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,N,qfq
* intraday  https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m15,,N

Field order for daily bars is [date, open, close, high, low, volume]; note the
*close-before-high* ordering, which is easy to get wrong and silently swaps
columns. Intraday bars are [YYYYMMDDHHMM, open, close, high, low, volume, ...].

Quotes are GBK encoded; decoding as UTF-8 raises, so the encoding is explicit.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

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

QUOTE_URL = "https://qt.gtimg.cn/q={ticker}"
DAILY_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={ticker},day,,,{limit},qfq"
)
INTRADAY_URL = (
    "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={ticker},m15,,{limit}"
)


class TencentSource:
    name = "tencent"

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self._transport = transport

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
        try:
            with self._client() as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"腾讯行情不可达：{exc}") from exc
        if response.status_code >= 400:
            raise SourceUnavailable(f"腾讯行情返回 {response.status_code}")
        return response

    @staticmethod
    def _ticker(code: str) -> str:
        market, digits = split_code(code)
        return f"{market}{digits}"

    # ---------------------------------------------------------------- quotes
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

        def num(value: str, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        timestamp = fields[30]
        ts_ms = int(time.time() * 1000)
        if len(timestamp) == 14:
            parsed = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
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

    # ------------------------------------------------------------------ bars
    def daily_bars(self, code: str, *, limit: int = 800) -> BarBatch:
        ticker = self._ticker(code)
        url = DAILY_URL.format(ticker=ticker, limit=max(1, min(limit, 2000)))
        response = self._get(url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceUnavailable("腾讯日线返回非 JSON") from exc
        node = (payload.get("data") or {}).get(ticker) or {}
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            raise SourceUnavailable(f"{code} 无日线数据")

        bars: list[CandlePoint] = []
        for row in rows:
            if len(row) < 6:
                continue
            day = datetime.strptime(row[0], "%Y-%m-%d").replace(tzinfo=CN_TZ)
            # IMPORTANT: [date, open, close, high, low, volume]
            open_p, close_p, high_p, low_p = (
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
            )
            bars.append(
                CandlePoint(
                    time=int(day.timestamp() * 1000),
                    open=round_price(open_p),
                    high=round_price(max(high_p, open_p, close_p)),
                    low=round_price(min(low_p, open_p, close_p)),
                    close=round_price(close_p),
                    volume=float(row[5]),
                )
            )
        bars.sort(key=lambda bar: bar.time)
        return BarBatch(code=code.upper(), timeframe="1d", bars=bars)

    def intraday_bars(self, code: str, *, limit: int = 320) -> BarBatch:
        ticker = self._ticker(code)
        url = INTRADAY_URL.format(ticker=ticker, limit=max(1, min(limit, 1000)))
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
