"""Full A-share acceptance: rules, data, trading, backtest, live gateway."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def app_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'cn_e2e.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=250,
        market_seed_bars=300,
        market_tick_seconds=3600,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,  # tests run outside Beijing hours
        live_trading_enabled=True,
        ths_mock_enabled=True,
        risk_max_orders_per_minute=2000,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_a_share_acceptance(app_client):
    meta = app_client.get("/api/meta").json()
    assert "cn_stock" in meta["capabilities"]["markets"]
    assert meta["capabilities"]["cn_rules"]["t_plus_one"] is True

    rules = app_client.get("/api/cn/rules").json()
    assert rules["lot_size"] == 100
    assert rules["boards"]["main"]["limit_pct"] == 0.10
    assert rules["boards"]["gem"]["limit_pct"] == 0.20
    assert rules["fees"]["stamp_duty_bps"] == 5.0

    symbols = app_client.get("/api/cn/symbols").json()
    assert len(symbols) >= 8
    for symbol in symbols:
        assert symbol["market"] == "cn_stock"
        assert symbol["limit_down"] < symbol["limit_up"]
        # Every seeded board is represented.
    boards = {s["board"] for s in symbols}
    assert {"main", "gem", "star"} <= boards

    account = app_client.post(
        "/api/accounts",
        json={"name": "A股验收", "kind": "paper", "market": "cn_stock", "initial_balance": 500_000},
    ).json()
    assert account["market"] == "cn_stock" and account["base_currency"] == "CNY"

    target = next(
        s for s in symbols if s["limit_down"] * 1.02 < s["price"] < s["limit_up"] * 0.98
    )

    # Odd lots rejected for buys.
    odd = app_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"], "symbol": target["code"], "side": "buy",
            "order_type": "market", "quantity": 150,
        },
    )
    assert odd.status_code == 422

    # Buy 100 shares; T+1 holds them.
    buy = app_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"], "symbol": target["code"], "side": "buy",
            "order_type": "market", "quantity": 100,
        },
    )
    assert buy.status_code == 201, buy.text
    assert buy.json()["status"] == "filled"

    position = app_client.get(
        "/api/trading/positions", params={"account_id": account["id"]}
    ).json()[0]
    assert position["quantity"] == 100
    assert position["available_quantity"] == 0

    blocked = app_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"], "symbol": target["code"], "side": "sell",
            "order_type": "market", "quantity": 100,
        },
    )
    assert blocked.status_code == 409

    # Settlement unlocks; sell goes through with stamp duty.
    app_client.post("/api/trading/settle")
    sell = app_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"], "symbol": target["code"], "side": "sell",
            "order_type": "market", "quantity": 100,
        },
    ).json()
    fills = app_client.get("/api/trading/fills", params={"account_id": account["id"]}).json()
    buy_fill = next(f for f in fills if f["side"] == "buy")
    sell_fill = next(f for f in fills if f["side"] == "sell")
    assert sell_fill["fee"] > buy_fill["fee"]
    assert sell["status"] == "filled"

    # Portfolio stays consistent.
    overview = app_client.get(
        "/api/portfolio/overview", params={"account_id": account["id"]}
    ).json()
    assert overview["equity"] == pytest.approx(
        overview["cash"] + overview["positions_value"], abs=0.01
    )

    # Backtest enforces A-share rules from the symbol, not from the request.
    run = app_client.post(
        "/api/backtests",
        json={
            "strategy": "sma_cross", "symbol": target["code"], "timeframe": "1d",
            "bars": 200, "params": {"fast": 5, "slow": 20},
            "config": {"t_plus_one": False, "lot_size": 1, "limit_pct": 0},
        },
    )
    assert run.status_code == 201, run.text
    body = run.json()
    assert body["config"]["t_plus_one"] is True
    assert body["config"]["lot_size"] == 100
    assert body["config"]["limit_pct"] > 0
    assert body["metrics"]["periods_per_year"] == 242.0
    for trade in body["trades"]:
        assert trade["quantity"] % 100 == 0

    # THS gateway channel is visible and reachable.
    status = app_client.get("/api/brokers/status").json()
    assert status["channels"]["ths"]["ready"] is True
    assert app_client.get("/api/brokers/ths/health").json()["ok"] is True

    # Live A-share order routes through the gateway and records its id.
    live_account = app_client.post(
        "/api/accounts",
        json={"name": "A股实盘验收", "kind": "live", "market": "cn_stock", "initial_balance": 500_000},
    ).json()
    live_order = app_client.post(
        "/api/trading/orders",
        json={
            "account_id": live_account["id"], "symbol": target["code"], "side": "buy",
            "order_type": "limit", "quantity": 100, "limit_price": round(target["limit_down"], 2),
        },
        headers={"X-Live-Confirm": "true"},
    )
    assert live_order.status_code == 201, live_order.text
    assert live_order.json()["broker_order_id"].startswith("MOCK-")

    reconcile = app_client.get(
        "/api/brokers/ths/reconcile", params={"account_id": live_account["id"]}
    ).json()
    assert reconcile["gateway"] == "ths-mock"

    # Market data quality: native daily bars, no gaps in the seeded range.
    quality = app_client.get("/api/data/quality", params={"symbol": target["code"]}).json()
    assert quality["invalid_ohlc"] == 0
    assert quality["duplicate_times"] == 0
