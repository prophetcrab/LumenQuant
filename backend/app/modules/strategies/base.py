"""Strategy plugin contract.

A strategy is a pure signal generator: `prepare` precomputes indicator series
once, `evaluate` inspects one bar. It never places orders itself, so the same
class runs in backtests and in live/paper runtime without modification.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.modules.market.service_types import CandlePoint

Action = Literal["buy", "sell", "hold"]


@dataclass(frozen=True)
class Signal:
    action: Action
    reason: str = ""
    strength: float = 1.0
    price_hint: float | None = None

    @property
    def is_actionable(self) -> bool:
        return self.action != "hold"


@dataclass
class StrategyState:
    candles: list[CandlePoint]
    params: BaseModel
    series: dict[str, list[float | None]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Strategy(ABC):
    key: str = ""
    name: str = ""
    category: str = "trend"
    description: str = ""
    tags: tuple[str, ...] = ()
    Params: type[BaseModel] = StrategyParams

    @abstractmethod
    def prepare(self, candles: list[CandlePoint], params: BaseModel) -> StrategyState:
        ...

    @abstractmethod
    def evaluate(self, state: StrategyState, index: int) -> Signal:
        ...

    def warmup(self, params: BaseModel) -> int:
        return 2

    def validate_params(self, raw: dict[str, Any] | None) -> BaseModel:
        return self.Params.model_validate(raw or {})

    def meta(self) -> dict[str, Any]:
        schema = self.Params.model_json_schema()
        defaults = self.Params().model_dump()
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "tags": list(self.tags),
            "params_schema": schema,
            "defaults": defaults,
        }


def hold(reason: str = "") -> Signal:
    return Signal(action="hold", reason=reason)


def crossed_above(
    series: list[float | None], reference: list[float | None], index: int
) -> bool:
    if index < 1:
        return False
    a0, a1 = series[index - 1], series[index]
    b0, b1 = reference[index - 1], reference[index]
    if a0 is None or a1 is None or b0 is None or b1 is None:
        return False
    return a0 <= b0 and a1 > b1


def crossed_below(
    series: list[float | None], reference: list[float | None], index: int
) -> bool:
    if index < 1:
        return False
    a0, a1 = series[index - 1], series[index]
    b0, b1 = reference[index - 1], reference[index]
    if a0 is None or a1 is None or b0 is None or b1 is None:
        return False
    return a0 >= b0 and a1 < b1


def crossed_above_level(series: list[float | None], level: float, index: int) -> bool:
    if index < 1:
        return False
    a0, a1 = series[index - 1], series[index]
    if a0 is None or a1 is None:
        return False
    return a0 <= level < a1


def crossed_below_level(series: list[float | None], level: float, index: int) -> bool:
    if index < 1:
        return False
    a0, a1 = series[index - 1], series[index]
    if a0 is None or a1 is None:
        return False
    return a0 >= level > a1
