"""History depth: the importers must actually deliver the bars they promise.

Two separate silent truncations made long backtests impossible:

* `BinancePublicSource.klines` clamped its request to 1000, so asking for 8,000
  bars quietly returned the newest 1,000 (~10 days of 15m data).
* `TencentSource.intraday_bars` clamped to 1000, but the endpoint above ~800
  returns 320 — a *larger* request could produce a *smaller* result.

Both are contract violations, not tuning: a caller that asks for N bars needs to
know what it actually got.
"""

from __future__ import annotations

import httpx

from app.modules.cn.sources.tencent import MAX_DAILY_BARS, MAX_INTRADAY_BARS, TencentSource
from app.modules.market.sources.binance_public import BinancePublicSource

STEP_MS = 900_000  # 15m


def _kline_row(open_time: int, price: float = 100.0) -> list:
    return [
        open_time, f"{price}", f"{price + 1}", f"{price - 1}", f"{price + 0.5}",
        "10.0", open_time + STEP_MS - 1, "0", 1, "0", "0", "0",
    ]


# --------------------------------------------------------------- Binance paging
def test_binance_pages_backwards_for_the_newest_bars():
    """The no-start_ms form walks `endTime` down and must not duplicate rows."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        limit = int(params["limit"])
        end = int(params.get("endTime", 2_000 * STEP_MS))
        # Return a full page ending at `end`.
        first = end - limit * STEP_MS
        rows = [_kline_row(first + i * STEP_MS) for i in range(limit)]
        return httpx.Response(200, json=rows)

    source = BinancePublicSource(
        timeout=5, transport=httpx.MockTransport(handler)
    )
    bars = source.klines("BTCUSDT", interval="15m", limit=2500)

    assert len(bars) == 2500
    times = [b.time for b in bars]
    assert len(set(times)) == 2500, "duplicate timestamps from paging"
    assert times == sorted(times)
    # Three pages: 1000 + 1000 + 500.
    assert [int(c["limit"]) for c in calls] == [1000, 1000, 500]
    # Backward paging must use endTime, never startTime.
    assert all("endTime" in c for c in calls[1:])
    assert all("startTime" not in c for c in calls)


def test_binance_pages_forward_when_a_start_time_is_given():
    """Importers bound a window with start_ms; that form advances startTime."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        limit = int(params["limit"])
        start = int(params["startTime"])
        rows = [_kline_row(start + i * STEP_MS) for i in range(limit)]
        return httpx.Response(200, json=rows)

    source = BinancePublicSource(timeout=5, transport=httpx.MockTransport(handler))
    bars = source.klines("BTCUSDT", interval="15m", limit=2000, start_ms=1_000 * STEP_MS)

    assert len(bars) == 2000
    assert len({b.time for b in bars}) == 2000
    assert all("startTime" in c for c in calls)
    assert all("endTime" not in c for c in calls)
    # Second page must resume exactly after the first page's last bar.
    assert int(calls[1]["startTime"]) == bars[999].time + 1


def test_binance_stops_at_the_listing_date():
    """A short page means the exchange has no older data; stop, do not loop."""
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        limit = int(params["limit"])
        # Pretend only 300 bars exist.
        rows = [_kline_row(1_000 * STEP_MS + i * STEP_MS) for i in range(min(limit, 300))]
        return httpx.Response(200, json=rows)

    source = BinancePublicSource(timeout=5, transport=httpx.MockTransport(handler))
    bars = source.klines("NEWUSDT", interval="15m", limit=5000)
    assert len(bars) == 300


def test_binance_empty_page_terminates():
    source = BinancePublicSource(
        timeout=5, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    )
    assert source.klines("BTCUSDT", interval="15m", limit=500) == []


# ------------------------------------------------------------- Tencent clamping
def _tencent_m15_payload(ticker: str, count: int) -> dict:
    rows = []
    for i in range(count):
        stamp = f"2026080{1 + i // 100}{9 + (i % 8):02d}{(i % 60):02d}"[:12]
        rows.append([stamp, "10.0", "10.1", "10.2", "9.9", "1000"])
    return {"code": 0, "data": {ticker: {"m15": rows}}}


def test_tencent_intraday_never_asks_above_the_endpoint_ceiling():
    """Above the real cap the endpoint returns fewer bars, not an error.

    Measured 2026-10: `limit=800` -> 800 rows, `limit=900` -> 320 rows. Asking
    for 2000 must therefore send 800 and return what came back, instead of
    silently collapsing to 320.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        # Emulate the upstream: requests above the cap get the small default.
        requested = int(url.rsplit(",", 1)[-1])
        count = requested if requested <= MAX_INTRADAY_BARS else 320
        ticker = url.split("param=")[1].split(",")[0]
        return httpx.Response(200, json=_tencent_m15_payload(ticker, count))

    source = TencentSource(timeout=5, transport=httpx.MockTransport(handler))
    batch = source.intraday_bars("600519.SH", limit=5000)

    requested = int(seen[0].rsplit(",", 1)[-1])
    assert requested == MAX_INTRADAY_BARS, "must clamp to the real ceiling"
    assert len(batch.bars) == MAX_INTRADAY_BARS


def test_tencent_daily_clamps_to_its_own_ceiling():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        # URL shape: ...?param=sh600519,day,,,2000,
        param = url.split("param=")[1].split(",")
        ticker = param[0]
        requested = int(param[4])
        count = min(requested, MAX_DAILY_BARS)
        rows = [[f"2026-08-{1 + i % 28:02d}", "10.0", "10.0", "10.1", "9.9", "1000"]
                for i in range(count)]
        return httpx.Response(200, json={"code": 0, "data": {ticker: {"day": rows}}})

    source = TencentSource(timeout=5, transport=httpx.MockTransport(handler))
    batch = source.daily_bars("600519.SH", limit=99_999)
    assert int(seen[0].split("param=")[1].split(",")[4]) == MAX_DAILY_BARS
    assert len(batch.bars) == MAX_DAILY_BARS


# ------------------------------------------------------------------- config
def test_history_depth_is_configurable():
    """The depth has to be settable without editing code."""
    from app.core.config import Settings

    defaults = Settings()
    assert defaults.crypto_sync_intraday_limit >= 4000
    assert defaults.crypto_sync_daily_limit >= 2000
    assert defaults.crypto_sync_hourly_limit >= 20_000
    assert defaults.cn_sync_daily_limit >= 2000
    assert defaults.cn_sync_intraday_limit >= 800

    custom = Settings(crypto_sync_hourly_limit=1234, cn_sync_intraday_limit=100)
    assert custom.crypto_sync_hourly_limit == 1234
    assert custom.cn_sync_intraday_limit == 100
