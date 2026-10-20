"""Session gating must block A-share orders outside trading hours."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.cn.calendar import is_session_open


@pytest.fixture()
def gated_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'gated.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=60,
        market_seed_bars=200,
        market_tick_seconds=3600,
        runtime_enabled=False,
        # Enforce the session gate for paper accounts too.
        cn_session_enforced=True,
        cn_live_session_gate=True,
        live_trading_enabled=True,
        ths_mock_enabled=True,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_paper_order_blocked_outside_session(gated_client):
    account = gated_client.post(
        "/api/accounts",
        json={"name": "门控账户", "kind": "paper", "market": "cn_stock", "initial_balance": 200_000},
    ).json()
    symbols = gated_client.get("/api/cn/symbols").json()
    target = next(
        s for s in symbols if s["limit_down"] * 1.02 < s["price"] < s["limit_up"] * 0.98
    )
    response = gated_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": target["code"],
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
    )
    if is_session_open():
        assert response.status_code in {201, 409}  # open: allowed (or risk-limited)
    else:
        assert response.status_code == 409
        assert "非交易时段" in response.json()["error"]["message"]
