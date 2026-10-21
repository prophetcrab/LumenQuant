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


def test_no_account_yields_guidance_not_an_error(assistant_client):
    """Asking for account data with no accounts is a valid question.

    It used to answer 404 for `account_overview` and raise an unhandled
    ValueError (HTTP 500) for `risk_overview` — the assistant looked broken on a
    fresh install, which is exactly the first thing a new user tries.
    """
    for message in ("账户总览", "我现在能开仓吗", "看看我的持仓", "有哪些实例没有止损"):
        reply = assistant_client.post("/api/assistant/chat", json={"message": message})
        assert reply.status_code == 200, (message, reply.text)
        body = reply.json()
        assert "创建一个模拟账户" in body["answer"], (message, body["answer"])
        assert body["tool_calls"][0].get("error")


def test_unknown_symbol_is_explained(assistant_client):
    reply = assistant_client.post(
        "/api/assistant/chat", json={"message": "NOPE 现在什么价"}
    )
    # No symbol regex match -> falls back to the market snapshot of everything,
    # which must still succeed rather than 500.
    assert reply.status_code == 200, reply.text


def _two_market_client(tmp_path):
    from fastapi.testclient import TestClient as _Client

    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'assistant_two.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=60,
        market_seed_bars=400,
        market_tick_seconds=3600,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    return _Client(app), db


def test_assistant_stays_inside_the_account_market(tmp_path):
    """An unspecified symbol must not default to Bitcoin for an A-share account.

    Regression: "双均线信号" with an A-share account answered about BTCUSDT — a
    symbol the account cannot trade, presented as if it were the user's market.
    """
    client, db = _two_market_client(tmp_path)
    with client as assistant_client:
        _assert_market_stays(client)
    db.dispose()


def _assert_market_stays(assistant_client):
    cn_account = assistant_client.post(
        "/api/accounts",
        json={"name": "A股助手", "initial_balance": 500_000, "market": "cn_stock"},
    ).json()
    reply = assistant_client.post(
        "/api/assistant/chat",
        json={"message": "双均线信号", "account_id": cn_account["id"]},
    ).json()
    assert "USDT" not in reply["answer"]
    assert "600036.SH" in reply["answer"]
    assert "1d" in reply["answer"]

    crypto_account = assistant_client.post(
        "/api/accounts",
        json={"name": "加密助手", "initial_balance": 100_000, "market": "crypto"},
    ).json()
    reply = assistant_client.post(
        "/api/assistant/chat",
        json={"message": "双均线信号", "account_id": crypto_account["id"]},
    ).json()
    assert "BTCUSDT" in reply["answer"]


def test_assistant_understands_a_share_codes(tmp_path):
    """ "600036" and "600036.SH" must both resolve to the Shanghai listing."""
    client, db = _two_market_client(tmp_path)
    with client as assistant_client:
        _assert_cn_codes(client)
    db.dispose()


def _assert_cn_codes(assistant_client):
    account = assistant_client.post(
        "/api/accounts",
        json={"name": "代码助手", "initial_balance": 500_000, "market": "cn_stock"},
    ).json()
    for message in ("600036 双均线信号", "600036.SH 双均线信号", "000001 双均线信号"):
        reply = assistant_client.post(
            "/api/assistant/chat", json={"message": message, "account_id": account["id"]}
        ).json()
        assert "BTCUSDT" not in reply["answer"], message
    assert "600036.SH" in assistant_client.post(
        "/api/assistant/chat",
        json={"message": "600036 双均线信号", "account_id": account["id"]},
    ).json()["answer"]
    assert "000001.SZ" in assistant_client.post(
        "/api/assistant/chat",
        json={"message": "000001 双均线信号", "account_id": account["id"]},
    ).json()["answer"]
