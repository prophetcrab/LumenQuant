"""A-share end-to-end: market data, rules, trading, backtest and THS live path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def cn_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'cn.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=120,
        market_seed_bars=300,
        market_tick_seconds=3600,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,  # rehearsal: tests run outside market hours
        live_trading_enabled=True,
        ths_mock_enabled=True,
        risk_max_orders_per_minute=1000,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_cn_rules_endpoint(cn_client):
    rules = cn_client.get("/api/cn/rules").json()
    assert rules["lot_size"] == 100
    assert rules["t_plus_one"] is True
    assert rules["boards"]["main"]["limit_pct"] == 0.10
    assert rules["boards"]["gem"]["limit_pct"] == 0.20
    assert rules["st_limit_pct"] == 0.05
    assert rules["fees"]["stamp_duty_bps"] == 5.0
    assert rules["symbols"] >= 8


def test_cn_symbols_have_limits_and_daily_bars(cn_client):
    symbols = cn_client.get("/api/cn/symbols").json()
    assert len(symbols) >= 8
    sample = symbols[0]
    assert sample["market"] == "cn_stock"
    assert sample["quote"] == "CNY"
    assert sample["price"] > 0
    assert sample["limit_up"] > sample["price"] > sample["limit_down"]

    daily = cn_client.get(
        "/api/market/candles",
        params={"symbol": sample["code"], "timeframe": "1d", "limit": 120},
    ).json()
    assert len(daily["candles"]) == 120
    for bar in daily["candles"]:
        assert bar["high"] >= max(bar["open"], bar["close"])
        assert bar["low"] <= min(bar["open"], bar["close"])


def test_cn_session_endpoint(cn_client):
    payload = cn_client.get("/api/cn/session").json()
    assert payload["market"] == "cn_stock"
    assert "open" in payload
    calendar = cn_client.get("/api/cn/calendar", params={"days": 5}).json()
    assert len(calendar["upcoming"]) == 5


def test_cn_order_lifecycle(cn_client):
    account = cn_client.post(
        "/api/accounts",
        json={
            "name": "A股模拟",
            "kind": "paper",
            "market": "cn_stock",
            "initial_balance": 500_000,
        },
    ).json()
    assert account["market"] == "cn_stock"
    assert account["base_currency"] == "CNY"

    symbols = cn_client.get("/api/cn/symbols").json()
    # Pick a symbol that is not at its limit and has room to trade.
    target = next(
        s for s in symbols if s["limit_down"] * 1.02 < s["price"] < s["limit_up"] * 0.98
    )
    price = target["price"]
    quantity = 100

    buy = cn_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": target["code"],
            "side": "buy",
            "order_type": "market",
            "quantity": quantity,
        },
    )
    assert buy.status_code == 201, buy.text
    order = buy.json()
    assert order["status"] in {"filled", "open"}

    positions = cn_client.get(
        "/api/trading/positions", params={"account_id": account["id"]}
    ).json()
    if order["status"] == "filled":
        assert positions[0]["quantity"] == quantity
        assert positions[0]["available_quantity"] == 0  # T+1

        blocked = cn_client.post(
            "/api/trading/orders",
            json={
                "account_id": account["id"],
                "symbol": target["code"],
                "side": "sell",
                "order_type": "market",
                "quantity": quantity,
            },
        )
        assert blocked.status_code == 409
        assert "可用" in blocked.json()["error"]["message"] or "T+1" in blocked.json()["error"]["message"]

        assert cn_client.post("/api/trading/settle").status_code == 200
        positions = cn_client.get(
            "/api/trading/positions", params={"account_id": account["id"]}
        ).json()
        assert positions[0]["available_quantity"] == quantity

    odd_lot = cn_client.post(
        "/api/trading/orders",
        json={
            "account_id": account["id"],
            "symbol": target["code"],
            "side": "buy",
            "order_type": "market",
            "quantity": 150,
        },
    )
    assert odd_lot.status_code == 422


def test_cn_backtest_uses_native_rules(cn_client):
    symbols = cn_client.get("/api/cn/symbols").json()
    code = symbols[0]["code"]
    run = cn_client.post(
        "/api/backtests",
        json={
            "strategy": "sma_cross",
            "symbol": code,
            "timeframe": "1d",
            "bars": 100,
            "params": {"fast": 5, "slow": 20},
        },
    )
    assert run.status_code == 201, run.text
    body = run.json()
    config = body["config"]
    assert config["market"] == "cn_stock"
    assert config["t_plus_one"] is True
    assert config["lot_size"] == 100
    assert config["limit_pct"] > 0
    assert body["metrics"]["periods_per_year"] == 242.0
    for trade in body["trades"]:
        assert trade["quantity"] % 100 == 0


def test_ths_gateway_status_and_reconcile(cn_client):
    status = cn_client.get("/api/brokers/status").json()
    assert status["channels"]["ths"]["market"] == "cn_stock"
    assert status["channels"]["ths"]["ready"] is True

    health = cn_client.get("/api/brokers/ths/health")
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True

    account = cn_client.post(
        "/api/accounts",
        json={
            "name": "A股实盘",
            "kind": "live",
            "market": "cn_stock",
            "initial_balance": 1_000_000,
        },
    ).json()
    reconcile = cn_client.get(
        "/api/brokers/ths/reconcile", params={"account_id": account["id"]}
    )
    assert reconcile.status_code == 200, reconcile.text
    payload = reconcile.json()
    assert payload["gateway"] == "ths-mock"
    assert "positions" in payload


def test_live_cn_order_requires_confirm_and_routes_to_gateway(cn_client):
    account = cn_client.post(
        "/api/accounts",
        json={
            "name": "A股实盘下单",
            "kind": "live",
            "market": "cn_stock",
            "initial_balance": 1_000_000,
        },
    ).json()
    symbols = cn_client.get("/api/cn/symbols").json()
    target = next(
        s for s in symbols if s["limit_down"] * 1.02 < s["price"] < s["limit_up"] * 0.98
    )
    payload = {
        "account_id": account["id"],
        "symbol": target["code"],
        "side": "buy",
        "order_type": "market",
        "quantity": 100,
    }
    missing = cn_client.post("/api/trading/orders", json=payload)
    assert missing.status_code == 403
    assert "X-Live-Confirm" in missing.json()["error"]["message"]

    placed = cn_client.post(
        "/api/trading/orders", json=payload, headers={"X-Live-Confirm": "true"}
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert order["source"].startswith("live:")
    assert order["broker_order_id"]


def test_market_mismatch_between_account_and_symbol(cn_client):
    crypto_account = cn_client.post(
        "/api/accounts",
        json={"name": "币账户", "kind": "paper", "market": "crypto", "initial_balance": 10_000},
    ).json()
    symbols = cn_client.get("/api/cn/symbols").json()
    response = cn_client.post(
        "/api/trading/orders",
        json={
            "account_id": crypto_account["id"],
            "symbol": symbols[0]["code"],
            "side": "buy",
            "order_type": "market",
            "quantity": 100,
        },
    )
    assert response.status_code == 422
    assert "市场" in response.json()["error"]["message"]


def test_runtime_instance_rejects_market_mismatch(cn_client):
    crypto_account = cn_client.post(
        "/api/accounts",
        json={"name": "币策略账户", "kind": "paper", "market": "crypto", "initial_balance": 50_000},
    ).json()
    symbols = cn_client.get("/api/cn/symbols").json()
    response = cn_client.post(
        "/api/runtime/instances",
        json={
            "account_id": crypto_account["id"],
            "strategy_key": "sma_cross",
            "symbol": symbols[0]["code"],
            "timeframe": "1d",
            "params": {},
            "position_pct": 0.2,
            "max_order_notional": 10_000,
            "start": True,
        },
    )
    assert response.status_code == 422
    assert "市场" in response.json()["error"]["message"]


def test_runtime_instance_accepts_cn_symbol_on_cn_account(cn_client):
    account = cn_client.post(
        "/api/accounts",
        json={"name": "A股策略账户", "kind": "paper", "market": "cn_stock", "initial_balance": 200_000},
    ).json()
    symbols = cn_client.get("/api/cn/symbols").json()
    created = cn_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "sma_cross",
            "symbol": symbols[0]["code"],
            "timeframe": "1d",
            "params": {"fast": 5, "slow": 20},
            "position_pct": 0.2,
            "max_order_notional": 20_000,
            "start": True,
        },
    )
    assert created.status_code == 201, created.text
    instance = created.json()
    assert instance["symbol"] == symbols[0]["code"]
    # One evaluation pass must not error.
    tick = cn_client.post("/api/runtime/tick").json()
    assert tick["evaluated"][instance["id"]] in {"hold", "executed", "no_new_bar", "insufficient_data"}
