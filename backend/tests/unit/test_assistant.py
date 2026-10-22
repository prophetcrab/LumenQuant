from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.db import Database
from app.modules.market.engine import MarketEngine
from app.modules.market.service import MarketService
from app.modules.accounts.service import AccountService
from app.modules.assistant.service import AssistantService
from app.modules.assistant.tools import ToolBox
from app.modules.backtest.service import BacktestService
from app.modules.portfolio.service import PortfolioService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.service import TradingService


@pytest.fixture()
def assistant(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'assistant.db').as_posix()}",
        seed_market=False,
        runtime_enabled=False,
        market_seed_bars=400,
    )
    db = Database(settings.database_url)
    db.create_schema()
    engine = MarketEngine(db, settings)
    engine.seed()
    engine.tick()
    with db.session() as session:
        accounts = AccountService(session)
        account = accounts.create(name="Assistant Book", initial_balance=100_000)
        market = MarketService(session, clock=engine.clock, live=engine)
        trading = TradingService(
            session,
            cash=AccountCashAdapter(accounts),
            prices=MarketPriceAdapter(market),
            fee_bps=10,
            slippage_bps=3,
        )
        trading.place_order(
            account_id=account.id, symbol="BTCUSDT", side="buy",
            order_type="market", quantity=0.1,
        )
        toolbox = ToolBox(
            session,
            accounts=accounts,
            market=market,
            trading=trading,
            portfolio=PortfolioService(session, accounts, trading),
            backtests=BacktestService(session, market),
        )
        yield AssistantService(toolbox, settings), account, toolbox
    db.dispose()


def test_tools_are_read_only(assistant):
    _, _, toolbox = assistant
    names = {tool.name for tool in toolbox.tools}
    assert "account_overview" in names
    assert "run_backtest" in names
    # No tool may mutate money or place orders.
    assert not any(word in name for name in names for word in ("place", "cancel", "withdraw", "deposit"))


def test_overview_answer_contains_real_numbers(assistant):
    service, account, _ = assistant
    reply = service.reply("我的账户情况怎么样", account_id=account.id)
    assert reply.mode == "local"
    assert "100,000" in reply.answer or "99," in reply.answer
    assert reply.tool_calls[0]["tool"] == "account_overview"
    assert reply.tool_calls[0]["result"]["equity"] > 0


def test_positions_answer(assistant):
    service, account, _ = assistant
    reply = service.reply("看看我的持仓", account_id=account.id)
    assert "BTCUSDT" in reply.answer
    assert "浮盈" in reply.answer


def test_market_and_strategy_intents(assistant):
    service, _, _ = assistant
    quote = service.reply("BTC 现在什么价")
    assert "BTCUSDT" in quote.answer

    strategies = service.reply("有哪些策略")
    assert "双均线交叉" in strategies.answer

    signals = service.reply("RSI 在 ETH 1h 的信号")
    assert "eth" in signals.answer.lower()
    assert signals.tool_calls[0]["tool"] == "strategy_signals"


def test_backtest_intent_writes_nothing_to_orders(assistant):
    service, account, toolbox = assistant
    before = len(toolbox.trading.list_orders(account.id))
    reply = service.reply("回测 MACD 最近 800 根", account_id=account.id)
    assert "回测" in reply.answer
    assert len(toolbox.trading.list_orders(account.id)) == before


def test_empty_message_rejected(assistant):
    service, _, _ = assistant
    with pytest.raises(Exception):
        service.reply("   ")


def test_unknown_tool(assistant):
    _, _, toolbox = assistant
    with pytest.raises(KeyError):
        toolbox.call("drop_database", {})


def test_snapshot_tool_labels_market_and_currency(assistant):
    """Bare prices from two markets are not comparable, and a model will compare them.

    The tool used to return `{symbol, price, change_24h_pct}` for every market at
    once, so an A-share price in CNY and a crypto price in USDT arrived under
    identical keys with nothing marking the difference.
    """
    _, _, toolbox = assistant
    rows = toolbox.call("market_snapshot", {})
    assert rows
    for row in rows:
        assert row["market"] in {"crypto", "cn_stock"}
        assert row["currency"] == ("CNY" if row["market"] == "cn_stock" else "USDT")
        assert row["change_label"] == (
            "较昨收" if row["market"] == "cn_stock" else "近 24 小时"
        )


def test_snapshot_tool_can_be_scoped_to_one_market(assistant):
    """A model asking about one market must not receive the other's prices."""
    _, _, toolbox = assistant
    crypto = toolbox.call("market_snapshot", {"market": "crypto"})
    assert crypto
    assert all(row["market"] == "crypto" for row in crypto)
    assert all(row["currency"] == "USDT" for row in crypto)


def test_account_tools_report_the_currency(assistant):
    _, account, toolbox = assistant
    accounts = toolbox.call("list_accounts", {})
    row = next(item for item in accounts if item["id"] == account.id)
    assert row["currency"] == account.base_currency
    assert row["market"] == account.market

    overview = toolbox.call("account_overview", {"account_id": account.id})
    assert overview["currency"] == account.base_currency


def test_local_answer_labels_currency_and_separates_markets(assistant):
    """The local (no-LLM) formatter must not print two currencies as one list."""
    service, account, _ = assistant
    reply = service.reply("现在行情怎么样", account_id=account.id)
    assert reply.mode == "local"
    # Crypto fixture only, so exactly one currency group is present and labelled.
    assert "USDT" in reply.answer


def test_llm_tool_calls_get_the_account_market_symbol(assistant, monkeypatch):
    """A model that omits `symbol` must not silently answer with crypto data.

    The tool defaults were BTCUSDT, so an A-share question turned into a crypto
    backtest while the answer still looked like it described the user's market.
    `AssistantService` now fills the symbol from the account's own market.
    """
    import httpx as _httpx

    service, _crypto_account, toolbox = assistant
    # Give the A-share catalog one symbol: the fixture only seeds crypto, and an
    # A-share account with no symbols must fail loudly rather than borrow a
    # crypto price.
    toolbox.market.add_symbol(
        code="600036.SH",
        name="招商银行",
        initial_price=35.2,
        market="cn_stock",
        board="main",
        prev_close=35.0,
    )
    cn_account = toolbox.accounts.create(
        name="A股助手", initial_balance=300_000, market="cn_stock"
    )

    def handler(request: _httpx.Request) -> _httpx.Response:
        payload = __import__("json").loads(request.content)
        if not any(m.get("role") == "tool" for m in payload["messages"]):
            return _httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "strategy_signals",
                                            # Deliberately omits symbol and timeframe.
                                            "arguments": '{"strategy_key": "sma_cross"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return _httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "完成"}}]}
        )

    service.settings = service.settings.model_copy(
        update={"llm_base_url": "https://llm.test/v1", "llm_model": "test-model"}
    )
    original_client = _httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = _httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr("app.modules.assistant.service.httpx.Client", patched_client)

    reply = service.reply("给我双均线信号", account_id=cn_account.id)
    assert reply.mode == "llm"
    assert reply.tool_calls
    arguments = reply.tool_calls[0]["arguments"]
    # Resolved from the A-share catalog, not from a hard-coded crypto constant.
    assert arguments["symbol"].endswith((".SH", ".SZ", ".BJ"))
    assert arguments["timeframe"] == "1d"
