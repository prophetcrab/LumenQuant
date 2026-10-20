"""Assistant orchestration.

Two modes share one tool layer:
- `local` (default): deterministic intent matching, no network, always available.
- `llm`: an OpenAI-compatible chat model may choose tools and phrase the answer;
  if the model is unavailable the service degrades to local mode.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import ValidationError
from app.modules.assistant.tools import ToolBox

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssistantReply:
    answer: str
    mode: str
    tool_calls: list[dict]


INTENTS: list[tuple[re.Pattern, str, dict]] = [
    (re.compile(r"(持仓|仓位|position)", re.I), "list_positions", {}),
    (re.compile(r"(订单|委托|order)", re.I), "recent_orders", {}),
    (
        re.compile(r"(行情|价格|什么价|多少钱|报价|涨跌|price|quote|how much)", re.I),
        "market_snapshot",
        {},
    ),
    (re.compile(r"(权益|总览|overview|账户情况|收益)", re.I), "account_overview", {}),
    (re.compile(r"(策略列表|有哪些策略|strategy list)", re.I), "list_strategies", {}),
    (re.compile(r"(自动运行|实例|runtime)", re.I), "list_runtime_instances", {}),
]

SYMBOL_RE = re.compile(r"\b(BTC|ETH|SOL|BNB|XRP|ADA)(USDT)?\b", re.I)
TIMEFRAME_RE = re.compile(r"\b(15m|30m|1h|4h|1d)\b", re.I)
STRATEGY_ALIASES = {
    "双均线": "sma_cross",
    "均线": "sma_cross",
    "ema": "ema_trend",
    "rsi": "rsi_reversion",
    "布林": "bollinger_reversion",
    "macd": "macd_momentum",
    "唐奇安": "donchian_breakout",
    "海龟": "donchian_breakout",
    "atr": "atr_channel",
    "随机": "stochastic_reversal",
    "买入持有": "buy_and_hold",
}


class AssistantService:
    def __init__(self, toolbox: ToolBox, settings: Settings) -> None:
        self.toolbox = toolbox
        self.settings = settings

    def reply(self, message: str, *, account_id: str | None = None) -> AssistantReply:
        message = (message or "").strip()
        if not message:
            raise ValidationError("message must not be empty")
        if len(message) > 2000:
            raise ValidationError("message is too long")
        if self.settings.llm_base_url and self.settings.llm_model:
            try:
                return self._llm_reply(message, account_id)
            except Exception:
                logger.exception("LLM assistant failed, falling back to local mode")
        return self._local_reply(message, account_id)

    # ---------------------------------------------------------------- local
    def _local_reply(self, message: str, account_id: str | None) -> AssistantReply:
        calls: list[dict] = []
        symbol_match = SYMBOL_RE.search(message)
        symbol = f"{symbol_match.group(1).upper()}USDT" if symbol_match else None
        timeframe_match = TIMEFRAME_RE.search(message)
        timeframe = timeframe_match.group(1) if timeframe_match else "1h"

        if re.search(r"(回测|backtest)", message, re.I):
            key = self._match_strategy(message)
            if key is None:
                return AssistantReply(
                    answer="要回测哪个策略？可以说“双均线”“RSI”“MACD”“布林”或“唐奇安”。",
                    mode="local",
                    tool_calls=[],
                )
            result = self.toolbox.call(
                "run_backtest",
                {
                    "strategy_key": key,
                    "symbol": symbol or "BTCUSDT",
                    "timeframe": timeframe,
                    "bars": 800,
                },
            )
            calls.append({"tool": "run_backtest", "arguments": {"strategy": key}, "result": result})
            verdict = "跑赢" if result["total_return_pct"] > result["buy_hold_pct"] else "跑输"
            return AssistantReply(
                answer=(
                    f"{result['strategy']} 在 {result['symbol']} {result['timeframe']} 上回测 {result['total_return_pct']:.2f}%，"
                    f"同期买入持有 {result['buy_hold_pct']:.2f}%（{verdict}基准）。"
                    f"最大回撤 {result['max_drawdown_pct']:.2f}%，Sharpe {result['sharpe']:.2f}，"
                    f"{result['trade_count']} 笔交易，胜率 {result['win_rate_pct']:.1f}%。"
                ),
                mode="local",
                tool_calls=calls,
            )

        if re.search(r"(信号|signal)", message, re.I):
            key = self._match_strategy(message) or "sma_cross"
            result = self.toolbox.call(
                "strategy_signals",
                {"strategy_key": key, "symbol": symbol or "BTCUSDT", "timeframe": timeframe},
            )
            calls.append({"tool": "strategy_signals", "arguments": {"strategy": key}, "result": result})
            action_map = {"buy": "买入", "sell": "卖出", "hold": "观望"}
            action = action_map.get(result["latest_action"], result["latest_action"])
            return AssistantReply(
                answer=(
                    f"{result['strategy']} 在 {result['symbol']} {result['timeframe']} 最新收盘 K 线的结论是"
                    f"「{action}」：{result['latest_reason'] or '无触发条件'}。"
                    f"近期共有 {len(result['recent_signals'])} 个历史信号。"
                ),
                mode="local",
                tool_calls=calls,
            )

        for pattern, tool, _args in INTENTS:
            if not pattern.search(message):
                continue
            arguments: dict[str, Any] = {}
            if tool in {"list_positions", "recent_orders", "account_overview", "list_runtime_instances"}:
                arguments["account_id"] = account_id
            if tool == "market_snapshot" and symbol:
                arguments["symbol"] = symbol
            result = self.toolbox.call(tool, arguments)
            calls.append({"tool": tool, "arguments": arguments, "result": result})
            return AssistantReply(
                answer=self._render(tool, result),
                mode="local",
                tool_calls=calls,
            )

        return AssistantReply(
            answer=(
                "我可以帮你查看账户总览、持仓、订单、行情快照、策略列表、策略信号、自动运行实例，"
                "也能直接跑一次回测。例如：“看看我的持仓”“BTC 现在什么价”“双均线在 ETH 1h 的信号”"
                "“回测 MACD 最近 800 根”。"
            ),
            mode="local",
            tool_calls=[],
        )

    def _match_strategy(self, message: str) -> str | None:
        lowered = message.lower()
        for alias, key in STRATEGY_ALIASES.items():
            if alias in lowered:
                return key
        return None

    def _render(self, tool: str, result: Any) -> str:
        if tool == "account_overview" and isinstance(result, dict):
            return (
                f"账户「{result['account']}」权益 {result['equity']:,.2f}，现金 {result['cash']:,.2f}，"
                f"持仓市值 {result['positions_value']:,.2f}（敞口 {result['exposure_pct']:.1f}%）。"
                f"累计收益 {result['total_return_pct']:.2f}%，浮动盈亏 {result['unrealized_pnl']:,.2f}，"
                f"已实现盈亏 {result['realized_pnl']:,.2f}。"
            )
        if tool == "list_positions":
            if not result:
                return "当前没有持仓。"
            lines = [
                f"{p['symbol']}：{p['quantity']} @ 成本 {p['avg_price']:.4f}，现价 {p['market_price']:.4f}，"
                f"浮盈 {p['unrealized_pnl']:+,.2f}（{p['unrealized_pct']:+.2f}%）"
                for p in result
            ]
            return "当前持仓：\n" + "\n".join(lines)
        if tool == "recent_orders":
            if not result:
                return "最近没有订单。"
            lines = [
                f"{o['created_at'][:19]} {o['symbol']} {o['side']} {o['quantity']} @ "
                f"{o['avg_fill_price'] or 0:.4f} · {o['status']} · {o['source']}"
                for o in result[:5]
            ]
            return "最近订单：\n" + "\n".join(lines)
        if tool == "market_snapshot":
            return "行情：" + "；".join(
                f"{s['symbol']} {s['price']:.4f}（{s['change_24h_pct']:+.2f}%）" for s in result[:6]
            )
        if tool == "list_strategies":
            return "内置策略：" + "、".join(f"{s['name']}({s['key']})" for s in result)
        if tool == "list_runtime_instances":
            if not result:
                return "还没有自动运行实例。"
            return "自动运行实例：\n" + "\n".join(
                f"{i['strategy']} · {i['symbol']} {i['timeframe']} · "
                f"{'运行中' if i['running'] else '已停止'} · 执行 {i['executions']} 次"
                + (f" · 错误：{i['last_error']}" if i["last_error"] else "")
                for i in result
            )
        return json.dumps(result, ensure_ascii=False)[:1200]

    # ------------------------------------------------------------------ llm
    def _llm_reply(self, message: str, account_id: str | None) -> AssistantReply:
        tools = [tool.as_dict() for tool in self.toolbox.tools]
        system = (
            "你是 LumenQuant 的交易研究助手。只能通过提供的只读工具获取事实，"
            "不得编造数字，不得给出投资建议或承诺收益。回答使用简体中文，简洁、可核对。"
            f"当前账户 ID：{account_id or '未指定'}。"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"} if self.settings.llm_api_key else {}
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "tools": [{"type": "function", "function": tool} for tool in tools],
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        calls: list[dict] = []
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            for _ in range(4):
                response = client.post(
                    f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                choice = response.json()["choices"][0]["message"]
                tool_calls = choice.get("tool_calls") or []
                if not tool_calls:
                    return AssistantReply(
                        answer=choice.get("content") or "（模型未返回内容）",
                        mode="llm",
                        tool_calls=calls,
                    )
                messages.append(choice)
                for call in tool_calls:
                    name = call["function"]["name"]
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                    if "account_id" in self.toolbox._tools[name].parameters and not arguments.get("account_id"):
                        arguments["account_id"] = account_id
                    try:
                        result = self.toolbox.call(name, arguments)
                    except Exception as exc:
                        result = {"error": str(exc)}
                    calls.append({"tool": name, "arguments": arguments, "result": result})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False)[:8000],
                        }
                    )
                payload = {**payload, "messages": messages}
        return AssistantReply(
            answer="工具调用次数超限，请把问题拆小一点。",
            mode="llm",
            tool_calls=calls,
        )
