"""Ports implemented by neighbouring modules or adapters."""

from __future__ import annotations

from typing import Protocol

from app.modules.market.service_types import SymbolView


class CashPort(Protocol):
    def kind(self, account_id: str) -> str: ...

    def market(self, account_id: str) -> str: ...

    def cash(self, account_id: str) -> float: ...

    def post_cash(
        self, account_id: str, *, amount: float, kind: str, ref: str = "", note: str = ""
    ) -> float: ...

    def ensure_active(self, account_id: str) -> None: ...


class PricePort(Protocol):
    def price(self, symbol: str) -> float: ...

    def symbol(self, code: str) -> SymbolView: ...
