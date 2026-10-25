"""Binance public market data.

Read-only endpoints, no API key:

* tickers  https://data-api.binance.vision/api/v3/ticker/24hr?symbols=[...]
* klines   https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=N

`api.binance.com` is unreachable from some networks while the dedicated market
data host is not; the base URL is therefore configurable and defaults to the
data host. Live *orders* still go through `modules/brokers/binance.py` with the
signed trading API — this module never touches an account.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import httpx

from app.modules.market.service_types import CandlePoint

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://data-api.binance.vision"

# Kline rows are positional: [openTime, open, high, low, close, volume, closeTime, ...]
# — unlike the Tencent daily feed, high/low come *before* close here.
KLINES_PATH = "/api/v3/klines"
TICKERS_PATH = "/api/v3/ticker/24hr"


class CryptoSourceUnavailable(RuntimeError):
    """Upstream could not serve the request; callers degrade to local data."""


@dataclass(frozen=True)
class CryptoTicker:
    symbol: str
    price: float
    open: float
    high: float
    low: float
    volume: float
    prev_close: float
    timestamp_ms: int


class BinancePublicSource:
    name = "binance-public"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        retries: int = 3,
        backoff_seconds: float = 0.4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self.retries = max(1, retries)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def _get(self, path: str, params: dict) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                    response = client.get(f"{self.base_url}{path}", params=params)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    return response
                # 418 is an explicit IP ban; retrying extends it.
                if response.status_code in (418, 451) or 400 <= response.status_code < 500:
                    raise CryptoSourceUnavailable(
                        f"Binance 行情返回 {response.status_code}"
                    )
                last_error = CryptoSourceUnavailable(
                    f"Binance 行情返回 {response.status_code}"
                )
            if attempt + 1 < self.retries:
                delay = self.backoff_seconds * (2**attempt)
                delay *= 0.75 + random.random() * 0.5
                time.sleep(delay)
        raise CryptoSourceUnavailable(f"Binance 行情不可达：{last_error}") from last_error

    def tickers(self, symbols: list[str]) -> dict[str, CryptoTicker]:
        wanted = [symbol.upper() for symbol in symbols]
        if not wanted:
            return {}
        response = self._get(
            TICKERS_PATH,
            {"symbols": "[" + ",".join(f'"{symbol}"' for symbol in wanted) + "]"},
        )
        try:
            rows = response.json()
        except ValueError as exc:
            raise CryptoSourceUnavailable("Binance ticker 返回非 JSON") from exc
        if not isinstance(rows, list):
            raise CryptoSourceUnavailable("Binance ticker 返回结构异常")

        out: dict[str, CryptoTicker] = {}
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in wanted:
                continue
            price = _num(row.get("lastPrice"))
            if price <= 0:
                continue
            out[symbol] = CryptoTicker(
                symbol=symbol,
                price=price,
                open=_num(row.get("openPrice")),
                high=_num(row.get("highPrice")),
                low=_num(row.get("lowPrice")),
                volume=_num(row.get("volume")),
                prev_close=_num(row.get("prevClosePrice")),
                timestamp_ms=int(row.get("closeTime") or time.time() * 1000),
            )
        return out

    def klines(
        self,
        symbol: str,
        *,
        interval: str = "15m",
        limit: int = 500,
        start_ms: int | None = None,
    ) -> list[CandlePoint]:
        """Fetch up to `limit` bars, paging past the endpoint's 1000-bar cap.

        Binance returns at most 1000 rows per call, and the old implementation
        simply clamped the request to 1000 — so asking for a long history
        silently produced only the most recent 1000 bars (a month of 15m data
        instead of the two years a backtest might want).

        Two paging directions, because they answer different questions:

        * `start_ms is None` — "the newest N bars". Walk *backwards* with
          `endTime`, which is what a chart or a live strategy needs.
        * `start_ms` given — "N bars from this point forward", used by importers
          that want a bounded historical window.
        """
        wanted = max(1, int(limit))
        out: list[CandlePoint] = []
        # Forward paging (`start_ms` given) advances `startTime`; backward paging
        # walks `endTime` down. Mixing the two re-fetches the same newest page and
        # yields duplicate timestamps, so each direction keeps its own cursor.
        cursor = start_ms
        end_ms: int | None = None
        while len(out) < wanted:
            batch_limit = min(1000, wanted - len(out))
            params: dict = {
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": batch_limit,
            }
            if start_ms is not None:
                params["startTime"] = int(cursor)  # type: ignore[arg-type]
            elif end_ms is not None:
                params["endTime"] = int(end_ms)
            response = self._get(KLINES_PATH, params)
            try:
                rows = response.json()
            except ValueError as exc:
                raise CryptoSourceUnavailable("Binance kline 返回非 JSON") from exc
            if not isinstance(rows, list):
                raise CryptoSourceUnavailable("Binance kline 返回结构异常")
            if not rows:
                break

            page: list[CandlePoint] = []
            for row in rows:
                if len(row) < 6:
                    continue
                # [openTime, open, high, low, close, volume, closeTime, ...]
                page.append(
                    CandlePoint(
                        time=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
            if not page:
                break

            if start_ms is None:
                # Backwards: prepend this older page and continue before it.
                out = page + out
                end_ms = page[0].time - 1
            else:
                out.extend(page)
                cursor = page[-1].time + 1
            if len(rows) < batch_limit:
                break  # reached the listing date (or the end of history)

        out.sort(key=lambda bar: bar.time)
        return out[:wanted] if start_ms is not None else out[-wanted:]

    def health(self) -> tuple[bool, str, float]:
        started = time.perf_counter()
        try:
            self.tickers(["BTCUSDT"])
        except Exception as exc:
            return False, str(exc)[:200], round((time.perf_counter() - started) * 1000, 1)
        return True, "ok", round((time.perf_counter() - started) * 1000, 1)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
