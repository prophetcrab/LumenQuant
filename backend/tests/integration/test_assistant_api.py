from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app


@pytest.fixture()
def assistant_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'assistant_api.db').as_posix()}",
        seed_market=True,
        market_seed_bars=400,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield client
    db.dispose()


def test_status_and_tools(assistant_client):
    status = assistant_client.get("/api/assistant/status").json()
    assert status["mode"] == "local"

    tools = assistant_client.get("/api/assistant/tools").json()
    names = {tool["name"] for tool in tools}
    assert {"account_overview", "list_positions", "run_backtest"} <= names
    for tool in tools:
        assert tool["description"]


def test_chat_without_account(assistant_client):
    reply = assistant_client.post("/api/assistant/chat", json={"message": "BTC 现在什么价"})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["mode"] == "local"
    assert "BTCUSDT" in body["answer"]


def test_chat_with_account_and_tool_invocation(assistant_client):
    account = assistant_client.post(
        "/api/accounts", json={"name": "AI Book", "initial_balance": 50_000}
    ).json()
    reply = assistant_client.post(
        "/api/assistant/chat",
        json={"message": "账户总览", "account_id": account["id"]},
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["tool_calls"]
    assert body["tool_calls"][0]["tool"] == "account_overview"

    direct = assistant_client.post(
        "/api/assistant/tools/list_positions",
        json={"tool": "list_positions", "arguments": {"account_id": account["id"]}},
    )
    assert direct.status_code == 200
    assert direct.json()["result"] == []


def test_chat_validation(assistant_client):
    empty = assistant_client.post("/api/assistant/chat", json={"message": ""})
    assert empty.status_code == 422

    unknown = assistant_client.post(
        "/api/assistant/tools/nope", json={"tool": "nope", "arguments": {}}
    )
    assert unknown.status_code == 404
