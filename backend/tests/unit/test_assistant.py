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
