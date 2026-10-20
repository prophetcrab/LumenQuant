"""Composite live-candle provider.

Routes a symbol to whichever engine owns it, so routers only ever hold one
provider regardless of how many markets are enabled.
"""

from __future__ import annotations

from app.modules.market.service_types import CandlePoint


class CompositeLiveProvider:
    def __init__(self, crypto=None, cn=None) -> None:
        self.crypto = crypto
        self.cn = cn
        self._symbol_market: dict[str, str] = {}

    def register(self, symbol: str, market: str) -> None:
        self._symbol_market[symbol.upper()] = market

    def _provider(self, symbol: str):
        market = self._symbol_market.get(symbol.upper())
        if market == "cn_stock":
            return self.cn
        if market == "crypto":
            return self.crypto
        # Unknown (not yet registered): ask each engine in turn.
        return self.cn if self.cn is not None and self.cn.live_candle(symbol) else self.crypto

    def live_candle(self, symbol: str) -> CandlePoint | None:
        provider = self._provider(symbol)
        return provider.live_candle(symbol) if provider is not None else None

    def is_locked(self, symbol: str) -> str:
        if self.cn is not None and self._symbol_market.get(symbol.upper()) == "cn_stock":
            return self.cn.is_locked(symbol)
        return ""

    def prev_close(self, symbol: str) -> float:
        if self.cn is not None:
            return self.cn.prev_close(symbol)
        return 0.0
