"""Tencent data source parsing and sync tests (no network).

Real endpoints are exercised by hand during development; these tests pin the
parsing contract with recorded-shaped payloads so a silent column swap, encoding
mistake or timestamp drift fails immediately.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.errors import AppError
from app.modules.cn.sources.base import SourceUnavailable, join_code, split_code
from app.modules.cn.sources.tencent import TencentSource


def quote_payload() -> bytes:
    fields = [""] * 88
    fields[0], fields[1], fields[2] = "1", "贵州茅台", "600519"
    fields[3], fields[4], fields[5], fields[6] = "1331.88", "1324.97", "1321.01", "34136"
    fields[30], fields[33], fields[34] = "20261020150000", "1335.00", "1316.14"
    return ('v_sh600519="' + "~".join(fields) + '";').encode("gbk")


def make_source(handler) -> TencentSource:
    return TencentSource(transport=httpx.MockTransport(handler))


def quote_handler(request: httpx.Request) -> httpx.Response:
    # Field layout mirrors the live endpoint: 3=price 4=prev_close 5=open
    # 6=volume 30=timestamp 33=high 34=low. Built positionally so a shift in the
    # fixture can never silently test the wrong column.
    return httpx.Response(200, content=quote_payload())


def daily_handler(request: httpx.Request) -> httpx.Response:
    payload = {
        "code": 0,
        "msg": "",
        "data": {
            "sh600519": {
                "qfqday": [
                    # [date, open, close, high, low, volume] — close BEFORE high/low
                    ["2026-10-16", "1277.27", "1277.96", "1285.53", "1270.36", "16571.00"],
                    ["2026-10-20", "1321.01", "1331.88", "1335.00", "1316.14", "34136.00"],
                ],
                "qt": {},
            }
        },
    }
    return httpx.Response(200, json=payload)


def intraday_handler(request: httpx.Request) -> httpx.Response:
    payload = {
        "code": 0,
        "msg": "",
        "data": {
            "sh600519": {
                "m15": [
                    ["202610201445", "1322.00", "1331.00", "1331.00", "1322.00", "4652.00"],
                    ["202610201500", "1330.97", "1331.88", "1332.98", "1327.63", "5993.00"],
                ],
                "prec": "1324.97",
            }
        },
    }
    return httpx.Response(200, json=payload)


def dispatch(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "qt.gtimg.cn" in url:
        return quote_handler(request)
    if "fqkline" in url:
        return daily_handler(request)
    if "mkline" in url:
        return intraday_handler(request)
    return httpx.Response(404)


def test_code_helpers():
    assert split_code("600519.SH") == ("sh", "600519")
    assert split_code("000001.SZ") == ("sz", "000001")
    assert split_code("430047.BJ") == ("bj", "430047")
    assert join_code("sh", "600519") == "600519.SH"
    assert join_code("sz", "000001") == "000001.SZ"


def test_quote_parses_gbk_and_fields():
    source = make_source(dispatch)
    quote = source.quote("600519.SH")
    assert quote.name == "贵州茅台"
    assert quote.price == pytest.approx(1331.88)
    assert quote.prev_close == pytest.approx(1324.97)
    assert quote.open == pytest.approx(1321.01)
    assert quote.high == pytest.approx(1335.00)
    assert quote.low == pytest.approx(1316.14)
    assert quote.volume == pytest.approx(34136)


def test_daily_bars_respect_close_before_high_ordering():
    source = make_source(dispatch)
    batch = source.daily_bars("600519.SH", limit=2)
    assert batch.timeframe == "1d"
    assert len(batch.bars) == 2
    last = batch.bars[-1]
    # If the parser swapped high/low this would fail loudly.
    assert last.open == pytest.approx(1321.01)
    assert last.close == pytest.approx(1331.88)
    assert last.high == pytest.approx(1335.00)
    assert last.low == pytest.approx(1316.14)
    assert last.high >= max(last.open, last.close)
    assert last.low <= min(last.open, last.close)


def test_intraday_bars_are_session_timestamps():
    source = make_source(dispatch)
    batch = source.intraday_bars("600519.SH", limit=2)
    assert batch.timeframe == "15m"
    assert len(batch.bars) == 2
    last = batch.bars[-1]
    assert last.close == pytest.approx(1331.88)
    # 15:00 Beijing == 07:00 UTC
    from datetime import datetime, timezone

    local = datetime.fromtimestamp(last.time / 1000, timezone.utc)
    assert (local.hour, local.minute) == (7, 0)


def test_health_reports_failure_without_raising():
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    source = TencentSource(transport=httpx.MockTransport(broken), retries=1, backoff_seconds=0)
    health = source.health()
    assert health.available is False
    assert "不可达" in health.detail


def test_upstream_error_raises_source_unavailable():
    source = TencentSource(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
        retries=1,
        backoff_seconds=0,
    )
    with pytest.raises(SourceUnavailable):
        source.quote("600519.SH")


def test_unparseable_payload_is_rejected():
    source = make_source(lambda request: httpx.Response(200, content=b"garbage"))
    with pytest.raises(SourceUnavailable):
        source.quote("600519.SH")


def test_transient_failure_is_retried_then_succeeds():
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, content=quote_payload())

    source = TencentSource(
        transport=httpx.MockTransport(flaky), retries=3, backoff_seconds=0
    )
    quote = source.quote("600519.SH")
    assert quote.price == pytest.approx(1331.88)
    assert calls["n"] == 3


def test_retries_are_bounded():
    calls = {"n": 0}

    def always_down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down", request=request)

    source = TencentSource(
        transport=httpx.MockTransport(always_down), retries=2, backoff_seconds=0
    )
    with pytest.raises(SourceUnavailable):
        source.quote("600519.SH")
    assert calls["n"] == 2


def test_client_error_is_not_retried():
    calls = {"n": 0}

    def not_found(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="gone")

    source = TencentSource(
        transport=httpx.MockTransport(not_found), retries=3, backoff_seconds=0
    )
    with pytest.raises(SourceUnavailable):
        source.quote("600519.SH")
    assert calls["n"] == 1  # permanent errors must fail fast
