"""Tiny synchronous in-process event bus.

Domain modules publish facts (order filled, account reset) without importing
each other. Subscribers are wired only in the composition root.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DomainEvent:
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class OrderFilled(DomainEvent):
    order_id: str = ""
    account_id: str = ""
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    fee: float = 0.0


@dataclass(frozen=True)
class AccountReset(DomainEvent):
    account_id: str = ""
    initial_balance: float = 0.0


@dataclass(frozen=True)
class StrategySignalEmitted(DomainEvent):
    instance_id: str = ""
    symbol: str = ""
    action: str = ""
    reason: str = ""


Handler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Any) -> None:
        for handler in list(self._handlers.get(type(event), ())):
            try:
                handler(event)
            except Exception:  # a subscriber must never break the publisher
                logger.exception("event handler failed for %s", type(event).__name__)
