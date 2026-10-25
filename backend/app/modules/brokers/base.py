"""Broker abstraction for live trading.

A broker is a thin transport: it never stores state and never decides risk.
The trading module validates first, then translates an `OrderIntent` into a
broker call. That keeps live and paper paths structurally identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BrokerOrderRequest:
    symbol: str
    side: str  # buy | sell
    order_type: str  # market | limit
    quantity: float
    limit_price: float | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class BrokerOrderResult:
    broker_order_id: str
    status: str
    filled_quantity: float
    avg_price: float
    raw_status: str
    # Cumulative quote amount (price x quantity) the broker reports. Brokers
    # quote a *cumulative* average price, so booking each increment at that
    # average prices the newest slice at the average of all previous ones.
    # Carrying the cumulative notional lets the reconciler difference it and get
    # the true marginal price of the increment.
    quote_quantity: float | None = None


@dataclass(frozen=True)
class BrokerAccount:
    can_trade: bool
    balances: dict[str, float]


class BrokerPort(Protocol):
    name: str

    def verify_credentials(self) -> BrokerAccount: ...

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult: ...

    def cancel_order(self, symbol: str, broker_order_id: str) -> None: ...

    def open_orders(self, symbol: str) -> list[dict]: ...

    def order_status(
        self, broker_order_id: str, *, symbol: str = ""
    ) -> BrokerOrderResult: ...
