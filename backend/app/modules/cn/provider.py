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

    @property
    def clock(self):
        """Default clock for callers that have no symbol in hand.

        Crypto's clock is the better default because it advances continuously.
        It is *not* a substitute for `clock_for(symbol)`: the two markets run on
        different clocks, and A-share bars must be judged against the A-share one.
        """
        if self.crypto is not None:
            return getattr(self.crypto, "clock", None)
        if self.cn is not None:
            return getattr(self.cn, "clock", None)
        return None

    def clock_for(self, symbol: str):
        """The clock that produced this symbol's bars.

        Crypto's simulated clock runs at 60x while the A-share clock only advances
        inside sessions. Using the wrong one to decide "has this bar closed yet"
        makes an A-share daily bar look finished hours before the 15:00 close, so
        `closed_only` would hand an in-progress bar to backtests and live signals.
        """
        provider = self._provider(symbol)
        if provider is None:
            return self.clock
        return getattr(provider, "clock", None) or self.clock

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

    def live_quote(self, symbol: str):
        """Realtime quote when the owning provider has one (A-share only).

        Kept optional instead of declaring it on every engine: the crypto engine
        has no quote feed yet, and returning None is the honest answer there.
        """
        provider = self._provider(symbol)
        getter = getattr(provider, "live_quote", None)
        return getter(symbol) if getter is not None else None

    def is_locked(self, symbol: str) -> str:
        if self.cn is not None and self._symbol_market.get(symbol.upper()) == "cn_stock":
            return self.cn.is_locked(symbol)
        return ""

    def prev_close(self, symbol: str) -> float:
        if self.cn is not None:
            return self.cn.prev_close(symbol)
        return 0.0
