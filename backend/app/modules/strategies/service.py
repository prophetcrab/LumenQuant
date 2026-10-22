"""Read service for strategy catalog and signal previews."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ValidationError
from app.modules.market.service import MarketService
from app.modules.strategies.base import Signal, Strategy
from app.modules.strategies.registry import catalog, get


@dataclass(frozen=True)
class SignalPoint:
    time: int
    price: float
    action: str
    reason: str


class StrategyService:
    def __init__(self, market: MarketService) -> None:
        self.market = market

    def catalog(self) -> list[dict]:
        return catalog()

    def detail(self, key: str) -> dict:
        return get(key).meta()

    def signals(
        self,
        key: str,
        *,
        symbol: str,
        timeframe: str,
        limit: int,
        params: dict | None = None,
    ) -> dict:
        strategy: Strategy = get(key)
        try:
            parsed = strategy.validate_params(params)
        except Exception as exc:
            raise ValidationError(f"invalid strategy parameters: {exc}") from exc
        candles = self.market.candles(
            symbol, timeframe=timeframe, limit=limit, closed_only=True
        )
        required = strategy.warmup(parsed) + 2
        if len(candles) < required:
            # Mirror the backtest message so the same failure reads the same way
            # in both places; the regime filter is the usual culprit.
            params_dump = parsed.model_dump()
            hint = ""
            if params_dump.get("require_regime"):
                hint = (
                    f"；该策略启用了牛熊过滤（{params_dump.get('regime_period')} 日均线），"
                    "可增大 limit 或设置 require_regime=false"
                )
            raise ValidationError(
                f"数据不足：仅有 {len(candles)} 根 K 线，需要至少 {required} 根{hint}"
            )
        state = strategy.prepare(candles, parsed)
        points: list[SignalPoint] = []
        # Simulate the execution layer's position state so the preview shows the
        # signals that would actually become orders. Strategies emit a bare "buy"
        # on every bar they want to be long (buy_and_hold does this deliberately
        # so the runtime, which only sees the latest bar, always finds one);
        # rendering those raw produced 799 "buy" markers on 800 bars while the
        # backtest made a single trade. Suppressing an action that the current
        # holding already satisfies is exactly what the trading layer does.
        in_position = False
        suppressed = 0
        for index in range(strategy.warmup(parsed), len(candles)):
            signal: Signal = strategy.evaluate(state, index)
            if not signal.is_actionable:
                continue
            if (signal.action == "buy" and in_position) or (
                signal.action == "sell" and not in_position
            ):
                suppressed += 1
                continue
            in_position = signal.action == "buy"
            points.append(
                SignalPoint(
                    time=candles[index].time,
                    price=candles[index].close,
                    action=signal.action,
                    reason=signal.reason,
                )
            )
        overlays = {name: values for name, values in state.series.items()}
        return {
            "strategy": key,
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "params": parsed.model_dump(),
            "signals": [
                {"time": p.time, "price": p.price, "action": p.action, "reason": p.reason}
                for p in points
            ],
            # How many raw signals were dropped because they repeated an action
            # already in effect. Surfaced so "the strategy fired 800 times but the
            # chart shows one marker" is explainable rather than suspicious.
            "suppressed_signals": suppressed,
            "overlays": overlays,
        }
