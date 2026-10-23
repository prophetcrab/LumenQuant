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
