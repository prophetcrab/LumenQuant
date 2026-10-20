"""Request contracts must reject unknown fields.

Pydantic ignores undeclared fields by default, which let `market` be dropped from
symbol creation and silently produced crypto symbols for A-share codes. Every
request body now forbids extras so a client/server mismatch fails loudly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def strict_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'strict.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/accounts", {"name": "x", "not_a_field": 1}),
        ("/api/market/symbols", {"code": "TESTUSDT", "initial_price": 1, "unknown": True}),
        ("/api/backtests", {"strategy": "sma_cross", "bars": 100, "typo": 1}),
        (
            "/api/trading/orders",
            {
                "account_id": "x",
                "symbol": "BTCUSDT",
                "side": "buy",
                "order_type": "market",
                "quantity": 1,
                "slippage": 5,
            },
        ),
        ("/api/assistant/chat", {"message": "hi", "temperature": 0.9}),
    ],
)
def test_unknown_fields_are_rejected(strict_client, path, payload):
    response = strict_client.post(path, json=payload)
    assert response.status_code == 422, response.text


def test_valid_requests_still_pass(strict_client):
    account = strict_client.post(
        "/api/accounts", json={"name": "契约账户", "initial_balance": 1000}
    )
    assert account.status_code == 201, account.text
    symbol = strict_client.post(
        "/api/market/symbols",
        json={"code": "NEWUSDT", "name": "New", "initial_price": 2.5},
    )
    assert symbol.status_code == 201, symbol.text
