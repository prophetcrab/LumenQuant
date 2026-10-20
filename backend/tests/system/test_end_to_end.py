"""End-to-end acceptance: one trader's full workflow in a single session.

Covers every module in dependency order and asserts the cross-module invariants
that unit tests cannot see: cash conservation, portfolio valuation, analytics
consistency, assistant grounding, and restart durability.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def live_app(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=3000,
        market_tick_seconds=3600,
        runtime_enabled=False,
        risk_max_daily_loss_pct=0.05,
        risk_max_drawdown_pct=0.10,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    yield app, settings, db, tmp_path
    db.dispose()


def test_full_trader_workflow(live_app):
    app, settings, db, tmp_path = live_app
    with TestClient(app) as client:
        # 1) System is healthy and advertises capabilities.
        health = client.get("/health").json()
        assert health["status"] == "ok"
        meta = client.get("/api/meta").json()
        for module in ("accounts", "market", "trading", "strategies", "backtest", "runtime", "assistant"):
            assert module in meta["modules"]

        # 2) Market data is present for every symbol with a coherent snapshot.
        symbols = client.get("/api/market/symbols").json()
        assert len(symbols) >= 6
        for symbol in symbols:
            snapshot = client.get("/api/market/snapshot", params={"symbol": symbol["code"]}).json()
            assert snapshot["price"] > 0
            assert snapshot["low_24h"] <= snapshot["price"] <= snapshot["high_24h"]

        # 3) Create a funded account.
        account = client.post(
            "/api/accounts", json={"name": "验收账户", "initial_balance": 250_000}
        ).json()
        account_id = account["id"]
        assert account["cash_balance"] == 250_000

        # 4) Manual market buy, then verify cash/position/fill agreement.
        order = client.post(
            "/api/trading/orders",
            json={
                "account_id": account_id,
                "symbol": "BTCUSDT",
                "side": "buy",
                "order_type": "market",
                "quantity": 0.05,
            },
        ).json()
        assert order["status"] == "filled"

        positions = client.get("/api/trading/positions", params={"account_id": account_id}).json()
        assert positions[0]["quantity"] == 0.05
        fills = client.get("/api/trading/fills", params={"account_id": account_id}).json()
        assert len(fills) == 1

        overview = client.get("/api/portfolio/overview", params={"account_id": account_id}).json()
        assert overview["equity"] == pytest.approx(
            overview["cash"] + overview["positions_value"], abs=0.01
        )
        assert overview["position_count"] == 1
        assert abs(overview["total_return_pct"]) < 2.0  # just fees and drift

        ledger = client.get(f"/api/accounts/{account_id}/ledger").json()
        assert sum(row["amount"] for row in ledger) == pytest.approx(overview["cash"], abs=0.01)

        # 5) Strategy catalog + signal preview + backtest for every strategy.
        catalog = client.get("/api/strategies").json()
        assert len(catalog) == 9
        for strategy in catalog:
            preview = client.post(
                f"/api/strategies/{strategy['key']}/signals",
                json={"symbol": "ETHUSDT", "timeframe": "1h", "limit": 600, "params": {}},
            )
            assert preview.status_code == 200, f"{strategy['key']}: {preview.text}"
            run = client.post(
                "/api/backtests",
                json={
                    "strategy": strategy["key"],
                    "symbol": "ETHUSDT",
                    "timeframe": "1h",
                    "bars": 600,
                    "params": {},
                },
            )
            assert run.status_code == 201, f"{strategy['key']}: {run.text}"
            metrics = run.json()["metrics"]
            assert metrics["bars"] == 600
            assert -100 <= metrics["total_return_pct"] <= 100_000
            assert 0 <= metrics["max_drawdown_pct"] <= 100

        # 6) Auto-trading instance, evaluated once on a closed bar.
        instance = client.post(
            "/api/runtime/instances",
            json={
                "account_id": account_id,
                "strategy_key": "sma_cross",
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "params": {"fast": 10, "slow": 30},
                "position_pct": 0.1,
                "max_order_notional": 10_000,
                "start": True,
            },
        )
        assert instance.status_code == 201, instance.text
        instance_id = instance.json()["id"]
        tick = client.post("/api/runtime/tick").json()
        assert instance_id in tick["evaluated"]
        logs = client.get(f"/api/runtime/instances/{instance_id}/logs").json()
        assert any(log["event"] in {"created", "started"} for log in logs)

        # 7) Assistant answers with grounded data (read-only tools).
        chat = client.post(
            "/api/assistant/chat",
            json={"message": "我的持仓怎么样", "account_id": account_id},
        )
        assert chat.status_code == 200
        reply = chat.json()
        assert reply["tool_calls"]
        assert "BTCUSDT" in reply["answer"]

        # 8) Risk state and audit trail are observable.
        risk = client.get("/api/risk/state", params={"account_id": account_id}).json()
        assert risk["breaker_active"] is False
        audit = client.get("/api/system/audit", params={"limit": 200}).json()
        actions = {row["action"] for row in audit}
        assert {"account.created", "order.placed", "runtime.created"} <= actions

        # 9) Data quality endpoint agrees with the seeded history.
        quality = client.get("/api/data/quality", params={"symbol": "BTCUSDT"}).json()
        assert quality["bars"] > 500
        assert quality["duplicate_times"] == 0
        assert quality["invalid_ohlc"] == 0

        # 10) Metrics endpoint saw the traffic.
        metrics = client.get("/api/system/metrics").json()
        assert metrics["requests"]["requests"] > 20
        assert metrics["requests"]["server_errors"] == 0

    # 11) Restart durability: a fresh app over the same DB keeps the data.
    db2 = Database(settings.database_url)
    app2 = create_app(settings, db2)
    with TestClient(app2) as client2:
        accounts = client2.get("/api/accounts").json()
        assert any(item["id"] == account_id for item in accounts)
        stored_positions = client2.get(
            "/api/trading/positions", params={"account_id": account_id}
        ).json()
        assert stored_positions[0]["quantity"] == 0.05
        backtests = client2.get("/api/backtests").json()
        assert len(backtests) == 9
    db2.dispose()


def test_risk_breaker_blocks_then_recovers(live_app):
    app, _, _, _ = live_app
    with TestClient(app) as client:
        account = client.post(
            "/api/accounts", json={"name": "熔断账户", "initial_balance": 50_000}
        ).json()
        client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 50_000})
        client.post("/api/risk/observe", params={"account_id": account["id"], "equity": 40_000})
        state = client.get("/api/risk/state", params={"account_id": account["id"]}).json()
        assert state["breaker_active"] is True

        blocked = client.post(
            "/api/trading/orders",
            json={
                "account_id": account["id"],
                "symbol": "BTCUSDT",
                "side": "buy",
                "order_type": "market",
                "quantity": 0.01,
            },
        )
        assert blocked.status_code == 409

        client.post("/api/risk/reset", params={"account_id": account["id"]})
        allowed = client.post(
            "/api/trading/orders",
            json={
                "account_id": account["id"],
                "symbol": "BTCUSDT",
                "side": "buy",
                "order_type": "market",
                "quantity": 0.01,
            },
        )
        assert allowed.status_code == 201
