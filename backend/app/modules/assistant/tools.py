"""Read-only tools exposed to the assistant.

Tools are the only way the assistant touches system state. They are all reads,
so a hallucinated tool call cannot move money. Write actions stay manual.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.money import safe_div
from app.modules.accounts.service import AccountService
from app.modules.backtest.service import BacktestService
from app.modules.market.service import MarketService
from app.modules.portfolio.service import PortfolioService
from app.modules.strategies.registry import catalog, get
from app.modules.trading.service import TradingService


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, str]
    handler: Callable[..., Any]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolBox:
    def __init__(
        self,
        session: Session,
        *,
        accounts: AccountService,
        market: MarketService,
        trading: TradingService,
        portfolio: PortfolioService,
        backtests: BacktestService,
    ) -> None:
        self.session = session
        self.accounts = accounts
        self.market = market
        self.trading = trading
        self.portfolio = portfolio
        self.backtests = backtests
        self._tools: dict[str, Tool] = {}
        self._register()

    def _register(self) -> None:
        self._add(
            Tool(
                "list_accounts",
                "列出全部账户及其现金余额与状态。",
                {},
                self._list_accounts,
            )
        )
        self._add(
            Tool(
                "account_overview",
                "获取账户权益、现金、持仓市值、收益与敞口。account_id 可省略（默认第一个活跃账户）。",
                {"account_id": "string?"},
                self._overview,
            )
        )
        self._add(
            Tool(
                "list_positions",
                "列出账户持仓：数量、均价、现价、市值、浮盈。",
                {"account_id": "string?"},
                self._positions,
            )
        )
        self._add(
            Tool(
                "recent_orders",
                "列出最近订单与成交状态。",
                {"account_id": "string?", "limit": "int?"},
                self._orders,
            )
        )
        self._add(
            Tool(
                "market_snapshot",
                "获取一个或全部标的的最新价与 24h 涨跌。",
                {"symbol": "string?"},
                self._snapshots,
            )
        )
        self._add(
            Tool(
                "list_strategies",
                "列出可用策略及其参数默认值。",
                {},
                self._strategies,
            )
        )
        self._add(
            Tool(
                "strategy_signals",
                "对指定策略计算近期信号（最近 600 根 K 线，仅已收盘）。",
                {"strategy_key": "string", "symbol": "string?", "timeframe": "string?"},
                self._signals,
            )
        )
        self._add(
            Tool(
                "run_backtest",
                "运行一次回测并返回核心绩效；不会保存到历史之外。",
                {
                    "strategy_key": "string",
                    "symbol": "string?",
                    "timeframe": "string?",
                    "bars": "int?",
                },
                self._backtest,
            )
        )
        self._add(
            Tool(
                "list_runtime_instances",
                "列出自动运行实例、状态与最近错误。",
                {"account_id": "string?"},
                self._runtime,
            )
        )

    def _add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._tools:
            raise KeyError(f"unknown tool '{name}'")
        return self._tools[name].handler(**arguments)

    # ---------------------------------------------------------------- tools
    def _list_accounts(self) -> list[dict]:
        return [
            {
                "id": a.id,
                "name": a.name,
                "kind": a.kind,
                "status": a.status,
                "cash": a.cash_balance,
                "initial_balance": a.initial_balance,
            }
            for a in self.accounts.list()
        ]

    def _overview(self, account_id: str | None = None) -> dict:
        view = self.portfolio.overview(account_id, record=False)
        return {
            "account": view.account_name,
            "equity": view.equity,
            "cash": view.cash,
            "positions_value": view.positions_value,
            "unrealized_pnl": view.unrealized_pnl,
            "realized_pnl": view.realized_pnl,
            "total_return_pct": view.total_return_pct,
            "exposure_pct": view.exposure_pct,
            "positions": len(view.allocations),
            "top_allocations": [
                {"symbol": a.symbol, "weight_pct": a.weight_pct}
                for a in view.allocations[:5]
            ],
        }

    def _positions(self, account_id: str | None = None) -> list[dict]:
        resolved = account_id or self._default_account()
        return [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_price": p.avg_price,
                "market_price": p.market_price,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "unrealized_pct": round(
                    safe_div(p.unrealized_pnl, p.avg_price * p.quantity) * 100, 4
                ),
            }
            for p in self.trading.positions(resolved)
        ]

    def _orders(self, account_id: str | None = None, limit: int = 10) -> list[dict]:
        resolved = account_id or self._default_account()
        return [
            {
                "symbol": o.symbol,
                "side": o.side,
                "type": o.order_type,
                "quantity": o.quantity,
                "avg_fill_price": o.avg_fill_price,
                "status": o.status,
                "source": o.source,
                "created_at": o.created_at,
            }
            for o in self.trading.list_orders(resolved, limit=min(int(limit), 50))
        ]

    def _snapshots(self, symbol: str | None = None) -> list[dict]:
        if symbol:
            snap = self.market.snapshot(symbol)
            return [
                {
                    "symbol": snap.symbol,
                    "price": snap.price,
                    "change_24h_pct": snap.change_24h_pct,
                    "high_24h": snap.high_24h,
                    "low_24h": snap.low_24h,
                }
            ]
        return [
            {
                "symbol": s.symbol,
                "price": s.price,
                "change_24h_pct": s.change_24h_pct,
            }
            for s in self.market.snapshots()
        ]

    def _strategies(self) -> list[dict]:
        return [
            {
                "key": meta["key"],
                "name": meta["name"],
                "category": meta["category"],
                "defaults": meta["defaults"],
            }
            for meta in catalog()
        ]

    def _signals(
        self, strategy_key: str, symbol: str = "BTCUSDT", timeframe: str = "1h"
    ) -> dict:
        strategy = get(strategy_key)
        params = strategy.validate_params({})
        candles = self.market.candles(symbol, timeframe=timeframe, limit=600, closed_only=True)
        if len(candles) < strategy.warmup(params) + 3:
            return {"error": "not enough candles"}
        state = strategy.prepare(candles, params)
        signals = []
        for index in range(strategy.warmup(params), len(candles)):
            signal = strategy.evaluate(state, index)
            if signal.is_actionable:
                signals.append(
                    {
                        "time": candles[index].time,
                        "price": candles[index].close,
                        "action": signal.action,
                        "reason": signal.reason,
                    }
                )
        latest = strategy.evaluate(state, len(candles) - 1)
        return {
            "strategy": strategy_key,
            "symbol": symbol,
            "timeframe": timeframe,
            "latest_bar_time": candles[-1].time,
            "latest_action": latest.action,
            "latest_reason": latest.reason,
            "recent_signals": signals[-8:],
        }

    def _backtest(
        self,
        strategy_key: str,
        symbol: str = "BTCUSDT",
        timeframe: str = "1h",
        bars: int = 800,
    ) -> dict:
        result = self.backtests.run(
            strategy_key=strategy_key,
            symbol=symbol,
            timeframe=timeframe,
            bars=min(max(int(bars), 100), 5000),
        )
        metrics = result["metrics"]
        return {
            "run_id": result["id"],
            "strategy": strategy_key,
            "symbol": symbol,
            "timeframe": timeframe,
            "total_return_pct": metrics["total_return_pct"],
            "buy_hold_pct": metrics["buy_hold_pct"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "sharpe": metrics["sharpe"],
            "trade_count": metrics["trade_count"],
            "win_rate_pct": metrics["win_rate_pct"],
        }

    def _runtime(self, account_id: str | None = None) -> list[dict]:
        from app.modules.runtime.service import RuntimeService

        service = RuntimeService(
            self.session,
            accounts_service=self.accounts,
            market=self.market,
            trading=self.trading,
        )
        return [
            {
                "id": i.id,
                "strategy": i.strategy_name,
                "symbol": i.symbol,
                "timeframe": i.timeframe,
                "running": i.running,
                "executions": i.executions,
                "last_error": i.last_error,
            }
            for i in service.list(account_id=account_id)
        ]

    def _default_account(self) -> str:
        accounts = self.accounts.list(include_archived=False)
        if not accounts:
            raise ValueError("no active account")
        return accounts[0].id
