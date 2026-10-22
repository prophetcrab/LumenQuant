"""Realtime A-share quotes through the API surface.

Uses a fake source injected into the app state, so the assertions are about the
HTTP contract and snapshot precedence rather than the upstream endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.cn.sources.base import Quote
from app.modules.market.models import Symbol


class StubSource:
    """Deterministic replacement for TencentSource; no network involved."""

    name = "stub"
    payload: dict[str, Quote] = {}

    def quotes(self, codes):
        return {code: self.payload[code] for code in codes if code in self.payload}

    def daily_bars(self, code, *, limit=800):
        raise AssertionError("integration test must not fetch history")

    def intraday_bars(self, code, *, limit=320):
        raise AssertionError("integration test must not fetch history")

    def quote(self, code):
        return self.payload[code]

    def health(self):
        raise AssertionError("not used in realtime mode")


@pytest.fixture()
def rt_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'rt.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_data_source="tencent",
        cn_sync_on_start=False,
        cn_realtime_enabled=True,
        runtime_enabled=False,
        live_trading_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    with db.session() as session:
        session.add(
            Symbol(
                code="600519.SH",
                name="贵州茅台",
                asset_class="stock",
                market="cn_stock",
                exchange="SH",
                board="main",
                base="600519",
                quote="CNY",
                tick_size=0.01,
                lot_size=100.0,
                initial_price=1000.0,
                prev_close=1000.0,
                limit_pct=0.10,
                base_timeframe="15m",
            )
        )
    app = create_app(settings, db)
    engine = app.state.cn_quote_engine
    assert engine is not None, "real-data mode must install the realtime engine"
    source = StubSource()
    engine.source = source
    StubSource.payload = {}
    with TestClient(app) as client:
        yield client, engine, source
    db.dispose()


def test_real_data_mode_replaces_the_synthetic_engine(rt_client):
    _client, engine, _source = rt_client
    # If the synthetic engine were still installed, T+1 duties would read a 60x
    # simulated clock and bar timestamps would drift from wall time.
    assert engine.__class__.__name__ == "CnQuoteEngine"


def test_quotes_endpoint_reports_freshness(rt_client):
    client, _engine, source = rt_client
    source.payload["600519.SH"] = Quote(
        code="600519.SH",
        name="贵州茅台",
        price=1030.0,
        prev_close=1000.0,
        open=1010.0,
        high=1040.0,
        low=1005.0,
        volume=12345.0,
        timestamp_ms=0,
    )
    poll = client.post("/api/cn/data/poll")
    assert poll.status_code == 200
    assert poll.json()["updated"] == 1

    payload = client.get("/api/cn/data/quotes").json()
    assert payload["health"]["mode"] == "realtime-poll"
    row = payload["quotes"][0]
    assert row["code"] == "600519.SH"
    assert row["price"] == pytest.approx(1030.0)
    assert "age_ms" in row and "fresh" in row


def test_snapshot_prefers_the_exchange_quote(rt_client):
    client, _engine, source = rt_client
    source.payload["600519.SH"] = Quote(
        code="600519.SH",
        name="贵州茅台",
        price=1030.0,
        prev_close=1000.0,
        open=1010.0,
        high=1040.0,
        low=1005.0,
        volume=12345.0,
        timestamp_ms=0,
    )
    client.post("/api/cn/data/poll")

    snapshot = client.get("/api/market/snapshot", params={"symbol": "600519.SH"}).json()
    assert snapshot["price"] == pytest.approx(1030.0)
    assert snapshot["prev_close"] == pytest.approx(1000.0)
    assert snapshot["high_24h"] == pytest.approx(1040.0)
    assert snapshot["low_24h"] == pytest.approx(1005.0)
    assert snapshot["change_24h_pct"] == pytest.approx(3.0)
    assert snapshot["limit_up"] == pytest.approx(1100.0)
    assert snapshot["limit_down"] == pytest.approx(900.0)


def test_quote_poll_updates_prev_close_for_the_next_limit_band(rt_client):
    client, _engine, source = rt_client
    source.payload["600519.SH"] = Quote(
        code="600519.SH",
        name="贵州茅台",
        price=1050.0,
        prev_close=1000.0,
        open=1010.0,
        high=1050.0,
        low=1005.0,
        volume=1.0,
        timestamp_ms=0,
    )
    client.post("/api/cn/data/poll")

    # After an ex-dividend adjustment the exchange publishes a new prev_close;
    # the stored row (and therefore the limit band) must follow it.
    source.payload["600519.SH"] = Quote(
        code="600519.SH",
        name="贵州茅台",
        price=1050.0,
        prev_close=900.0,
        open=1010.0,
        high=1050.0,
        low=1005.0,
        volume=2.0,
        timestamp_ms=0,
    )
    client.post("/api/cn/data/poll")

    symbols = client.get("/api/cn/symbols").json()
    row = next(item for item in symbols if item["code"] == "600519.SH")
    assert row["prev_close"] == pytest.approx(900.0)
    assert row["limit_up"] == pytest.approx(990.0)
    assert row["limit_down"] == pytest.approx(810.0)


def test_poll_and_quotes_fail_clearly_in_synthetic_mode(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'syn.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
        cn_data_source="synthetic",
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        assert app.state.cn_quote_engine is None
        response = client.get("/api/cn/data/quotes")
        assert response.status_code == 502
        assert "LQ_CN_DATA_SOURCE" in response.json()["error"]["message"]
        source = client.get("/api/cn/data/source").json()
        assert source["mode"] == "synthetic"
    db.dispose()
