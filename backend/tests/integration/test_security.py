from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


def build(tmp_path, **overrides):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'security.db').as_posix()}",
        seed_market=False,
        runtime_enabled=False,
        **overrides,
    )
    db = Database(settings.database_url)
    db.create_schema()
    return settings, db, TestClient(create_app(settings, db))


def test_auth_disabled_by_default(tmp_path):
    settings, db, client = build(tmp_path)
    with client:
        assert client.get("/api/accounts").status_code == 200
    db.dispose()


def test_auth_requires_bearer_token(tmp_path):
    settings, db, client = build(tmp_path, auth_enabled=True, auth_token="s3cret")
    with client:
        unauthorized = client.get("/api/accounts")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "unauthorized"

        wrong = client.get("/api/accounts", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        ok = client.get("/api/accounts", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200

        # Health stays public so process supervisors can probe it.
        assert client.get("/health").status_code == 200
    db.dispose()


def test_auth_enabled_without_token_blocks_everything(tmp_path):
    settings, db, client = build(tmp_path, auth_enabled=True, auth_token="")
    with client:
        assert client.get("/api/accounts").status_code == 401
    db.dispose()


def test_rate_limit_returns_429(tmp_path):
    settings, db, client = build(tmp_path, rate_limit_per_minute=3)
    with client:
        for _ in range(3):
            assert client.get("/api/accounts").status_code == 200
        blocked = client.get("/api/accounts")
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"]
    db.dispose()


def test_live_order_requires_confirmation_header(tmp_path):
    settings, db, client = build(tmp_path, live_trading_enabled=True)
    with client:
        account = client.post(
            "/api/accounts",
            json={"name": "Live Guard", "kind": "live", "initial_balance": 100_000},
        ).json()
        missing = client.post(
            "/api/trading/orders",
            json={
                "account_id": account["id"],
                "symbol": "BTCUSDT",
                "side": "buy",
                "order_type": "market",
                "quantity": 0.01,
            },
        )
        assert missing.status_code == 403
        assert "X-Live-Confirm" in missing.json()["error"]["message"]
    db.dispose()
