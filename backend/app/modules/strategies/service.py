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
        candles = self.market.candles(symbol, timeframe=timeframe, limit=limit)
        if len(candles) < strategy.warmup(parsed) + 2:
            raise ValidationError(
                f"not enough candles: need at least {strategy.warmup(parsed) + 2}, got {len(candles)}"
            )
        state = strategy.prepare(candles, parsed)
        points: list[SignalPoint] = []
        for index in range(strategy.warmup(parsed), len(candles)):
            signal: Signal = strategy.evaluate(state, index)
            if signal.is_actionable:
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
            "overlays": overlays,
        }
