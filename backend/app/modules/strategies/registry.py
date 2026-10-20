"""In-process strategy registry.

Adding a strategy = create a class, decorate with @register, import it from the
library package. Nothing else changes (backtest, runtime, UI all read the
catalog from here).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.errors import NotFoundError
from app.modules.strategies.base import Strategy

_REGISTRY: dict[str, Strategy] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    instance = cls()
    if not instance.key:
        raise ValueError(f"{cls.__name__} must define a key")
    if instance.key in _REGISTRY:
        raise ValueError(f"duplicate strategy key {instance.key}")
    _REGISTRY[instance.key] = instance
    return cls


def load_builtin() -> None:
    if _REGISTRY:
        return
    from app.modules.strategies import library  # noqa: F401


def get(key: str) -> Strategy:
    load_builtin()
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise NotFoundError(f"strategy '{key}' not found") from exc


def all_strategies() -> Iterable[Strategy]:
    load_builtin()
    return sorted(_REGISTRY.values(), key=lambda s: (s.category, s.name))


def catalog() -> list[dict]:
    return [strategy.meta() for strategy in all_strategies()]
