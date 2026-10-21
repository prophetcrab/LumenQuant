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
        cn_symbols_enabled=False,
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


def test_risk_intent_answers_with_the_same_snapshot_as_the_risk_centre(assistant_client):
    account = assistant_client.post(
        "/api/accounts", json={"name": "Risk Book", "initial_balance": 200_000}
    ).json()

    reply = assistant_client.post(
        "/api/assistant/chat",
        json={"message": "我现在能开仓吗？风控状态怎么样", "account_id": account["id"]},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["tool_calls"][0]["tool"] == "risk_overview"
    assert "交易闸门正常" in body["answer"]
    assert "单笔上限" in body["answer"]

    # The assistant must report the same exposure the risk centre renders.
    overview = assistant_client.get(
        "/api/risk/overview", params={"account_id": account["id"]}
    ).json()
    tool_result = body["tool_calls"][0]["result"]
    assert tool_result["exposure"]["equity"] == pytest.approx(
        overview["exposure"]["equity"], abs=0.01
    )
    assert tool_result["limits"] == overview["limits"]


def test_risk_intent_reports_unprotected_instances(assistant_client):
    account = assistant_client.post(
        "/api/accounts", json={"name": "Risk Gap", "initial_balance": 100_000}
    ).json()
    assistant_client.post(
        "/api/runtime/instances",
        json={
            "account_id": account["id"],
            "strategy_key": "sma_cross",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "params": {},
            "position_pct": 0.1,
            "max_order_notional": 5000,
            "start": True,
        },
    )
    reply = assistant_client.post(
        "/api/assistant/chat",
        json={"message": "有哪些实例没有止损", "account_id": account["id"]},
    )
    body = reply.json()
    assert body["tool_calls"][0]["tool"] == "risk_overview"
    tool_result = body["tool_calls"][0]["result"]
    assert tool_result["warnings"], "unprotected running instance must be surfaced"
    assert "止损" in body["answer"]
