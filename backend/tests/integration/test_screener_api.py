"""Screener HTTP surface.

Uses an in-process stub for the upstream, so these assertions cover routing,
validation and the "add to catalog" bridge rather than the feed itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.screener import router as screener_router


def _row(symbol: str, code: str, name: str, price: float, change: float, **extra):
    payload = {
        "symbol": symbol,
        "code": code,
        "name": name,
        "trade": str(price),
        "changepercent": change,
        "amount": 5_0000_0000.0,
        "turnoverratio": 6.0,
        "per": 15.0,
        "pb": 1.5,
        "mktcap": 5_000_000.0,
        "nmc": 4_000_000.0,
    }
    payload.update(extra)
    return payload


class StubSource:
    name = "stub"
    rows = [
        _row("sh600519", "600519", "贵州茅台", 1318.49, 1.25),
        _row("sz300750", "300750", "宁德时代", 237.60, 7.80),
        _row("sh688981", "688981", "中芯国际", 100.09, -0.15),
    ]

    def snapshot(self, *, sort: str = "symbol", max_rows: int | None = None):
        return list(self.rows)

    def health(self):
        return True, "ok", 12.5


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'screener.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_data_source="synthetic",
        cn_sync_on_start=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    # Route the service factory at a stub so no test touches the network.
    monkeypatch.setattr(
        screener_router, "_service",
        lambda request, session: __import__(
            "app.modules.screener.service", fromlist=["ScreenerService"]
        ).ScreenerService(
            StubSource(), cache=getattr(request.app.state, "screener_cache", None)
        ),
    )
    with TestClient(app) as test_client:
        yield test_client
    db.dispose()


def test_presets_are_listed(client):
    body = client.get("/api/screener/presets").json()
    keys = {item["key"] for item in body}
    assert "strong_breakout" in keys
    assert "value_blue_chip" in keys
    for item in body:
        assert item["name"] and item["description"]


def test_screen_returns_candidates_with_units(client):
    response = client.post(
        "/api/screener/screen", json={"exclude_st": True, "limit": 10}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_universe"] == 3
    assert body["returned"] == 3
    item = body["candidates"][0]
    # Amount is normalised to 亿元 (5e8 元 -> 5 亿).
    assert item["amount_yi"] == pytest.approx(5.0)
    assert item["market_cap_yi"] == pytest.approx(500.0)


def test_screen_rejects_an_unknown_board(client):
    response = client.post(
        "/api/screener/screen", json={"boards": ["nasdaq"], "limit": 5}
    )
    assert response.status_code == 422


def test_screen_limit_is_applied(client):
    body = client.post("/api/screener/screen", json={"limit": 1}).json()
    assert body["returned"] == 1
    assert body["matched"] == 3


def test_unknown_preset_is_404(client):
    response = client.post("/api/screener/presets/does_not_exist")
    assert response.status_code == 404


def test_add_symbol_bridges_a_screened_name_into_the_catalog(client):
    """A hit is useless unless it can be charted and backtested afterwards."""
    # 600000.SH is not part of the seeded catalog, so this exercises the insert.
    response = client.post(
        "/api/screener/add-symbol", json={"code": "600000.SH", "name": "浦发银行"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True

    symbols = client.get("/api/cn/symbols").json()
    added = [item for item in symbols if item["code"] == "600000.SH"]
    assert len(added) == 1
    assert added[0]["board"] == "main"

    # Adding it twice reports the existing entry instead of duplicating it.
    again = client.post(
        "/api/screener/add-symbol", json={"code": "600000.SH"}
    ).json()
    assert again["created"] is False


def test_add_symbol_requires_an_exchange_suffix(client):
    response = client.post("/api/screener/add-symbol", json={"code": "600519"})
    assert response.status_code == 422
    assert "后缀" in response.json()["detail"]


def test_board_is_inferred_for_an_added_symbol(client):
    client.post("/api/screener/add-symbol", json={"code": "300750.SZ", "name": "宁德时代"})
    symbols = {item["code"]: item for item in client.get("/api/cn/symbols").json()}
    assert symbols["300750.SZ"]["board"] == "gem"


def test_add_symbol_imports_history_so_it_can_be_backtested(client, monkeypatch):
    """Registering a code alone leaves a symbol that looks usable and is not.

    `initial_price=1.0` made the list show a 1 元 price with a 0.9–1.1 limit band,
    and every backtest answered "数据不足". The import must be part of the same
    action, or the user has to discover a second, differently-placed step.
    """
    from app.modules.cn.sources.base import BarBatch
    from app.modules.market.service_types import CandlePoint

    class StubSource:
        name = "stub"

        def __init__(self, **_kwargs):
            pass

        def daily_bars(self, code, *, limit=800):
            base = 1_792_512_000_000
            return BarBatch(
                code=code,
                timeframe="1d",
                bars=[
                    CandlePoint(
                        time=base - (30 - i) * 86_400_000,
                        open=9.5, high=9.6, low=9.4, close=9.53, volume=1000.0,
                    )
                    for i in range(30)
                ],
            )

        def intraday_bars(self, code, *, limit=320):
            return BarBatch(code=code, timeframe="15m", bars=[])

        def quote(self, code):
            from app.modules.cn.sources.base import Quote

            return Quote(
                code=code, name="浦发银行", price=9.53, prev_close=9.62,
                open=9.6, high=9.7, low=9.4, volume=1000.0, timestamp_ms=1_792_512_000_000,
            )

        def health(self):
            raise AssertionError("unused")

    monkeypatch.setattr(
        "app.modules.cn.sources.tencent.TencentSource", StubSource, raising=True
    )
    # The fixture's app is configured for synthetic mode, where there is nothing
    # to fetch. The import path only exists for a real source, so flip the setting
    # on the running app rather than rebuilding it.
    client.app.state.settings.cn_data_source = "tencent"

    response = client.post(
        "/api/screener/add-symbol", json={"code": "600000.SH", "name": "浦发银行"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["synced"] is True
    # Days that are not trading days are dropped by the sync (16 of 30 calendar
    # days here), so assert the import actually happened rather than an exact count.
    assert body["daily_bars"] >= 10

    # The catalog row now carries a real reference price, so the band is sane.
    symbols = {item["code"]: item for item in client.get("/api/cn/symbols").json()}
    row = symbols["600000.SH"]
    assert row["price"] == pytest.approx(9.53)
    assert row["limit_up"] > row["price"] > row["limit_down"]


def test_add_symbol_can_still_skip_the_import(client):
    """`sync=false` keeps the staged-catalog path for batch workflows."""
    response = client.post(
        "/api/screener/add-symbol?sync=false", json={"code": "600001.SH"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["synced"] is False
    assert "同步" in body["detail"]


def test_add_symbols_registers_the_whole_batch(client):
    """One request for many candidates: 50 individual clicks is two minutes."""
    response = client.post(
        "/api/screener/add-symbols?sync=false",
        json={"codes": ["600000.SH", "600015.SH", "600016.SH", "600000.SH"], "sync": False},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["requested"] == 4
    assert body["created"] == 3          # the duplicate is collapsed
    # 600519.SH etc. from the seed are not in this list, so nothing is pre-existing.
    assert body["already_present"] == 0
    symbols = {item["code"] for item in client.get("/api/cn/symbols").json()}
    assert {"600000.SH", "600015.SH", "600016.SH"} <= symbols


def test_add_symbols_reports_bad_codes_without_failing_the_batch(client):
    """One malformed code must not discard the valid ones beside it."""
    response = client.post(
        "/api/screener/add-symbols?sync=false",
        json={"codes": ["600000.SH", "BADCODE"], "sync": False},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["created"] == 1
    assert len(body["failed"]) == 1
    assert body["failed"][0]["code"] == "BADCODE"
    assert "后缀" in body["failed"][0]["reason"]


def test_add_symbols_counts_already_present_names(client):
    """Re-adding a seeded symbol reports it separately instead of duplicating."""
    body = client.post(
        "/api/screener/add-symbols?sync=false",
        json={"codes": ["600519.SH", "600000.SH"], "sync": False},
    ).json()
    assert body["already_present"] == 1
    assert body["created"] == 1
