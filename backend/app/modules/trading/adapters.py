"""Adapters binding trading ports to concrete modules."""

from __future__ import annotations

from app.modules.accounts.service import AccountService
from app.modules.market.service import MarketService
from app.modules.market.service_types import SymbolView


class AccountCashAdapter:
    def __init__(self, accounts: AccountService) -> None:
        self.accounts = accounts

    def cash(self, account_id: str) -> float:
        return self.accounts.cash(account_id)

    def post_cash(
        self, account_id: str, *, amount: float, kind: str, ref: str = "", note: str = ""
    ) -> float:
        return self.accounts.post_cash(account_id, amount=amount, kind=kind, ref=ref, note=note)

    def ensure_active(self, account_id: str) -> None:
        account = self.accounts.require(account_id)
        if account.status != "active":
            from app.core.errors import ConflictError

            raise ConflictError("account is not active")


class MarketPriceAdapter:
    def __init__(self, market: MarketService) -> None:
        self.market = market

    def price(self, symbol: str) -> float:
        return self.market.last_price(symbol)

    def symbol(self, code: str) -> SymbolView:
        return self.market.require_symbol(code)
