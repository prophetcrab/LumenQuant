"""Read-side market service: symbols, candles, snapshots, indicators.

Two markets share this service:

* crypto — 24/7 synthetic 15m bars aggregated on read;
* cn_stock — session-bound 15m bars plus native daily bars seeded by CnMarketEngine.

When a native series exists for the requested timeframe it is preferred over
aggregation, so A-share daily backtests use real daily bars instead of 16-bar
aggregates.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import time as dtime
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.modules.cn.rules import price_band
from app.modules.market.aggregate import aggregate
from app.modules.market.feed import MarketClock, timeframe_ms
from app.modules.market.indicators import compute
from app.modules.market.models import Candle, Symbol
from app.modules.market.service_types import CandlePoint, Snapshot, SymbolView

BASE_TIMEFRAME = "15m"
SUPPORTED_TIMEFRAMES = ("15m", "30m", "1h", "4h", "1d")
# A-shares trade 4 hours a day; a 4h "bar" would be a half-session, which no
# broker or charting tool calls 4h. Offering it invites misleading comparisons.
CN_TIMEFRAMES = ("15m", "30m", "1h", "1d")
MARKETS = ("crypto", "cn_stock")


def _bar_close_ms(market: str, timeframe: str, open_time: int) -> int:
    """When a bar's interval ends, in epoch milliseconds.

    Daily bars are not simply `open + 1d`: an A-share daily bar is stamped at
    local midnight but is only final at the 15:00 close, so `open + 24h` would
    keep calling it "in progress" all evening and hide the just-closed session
    from every `closed_only` caller.
    """
    if market == "cn_stock" and timeframe == "1d":
        from app.modules.cn.calendar import CN_TZ, day_key, to_ms

        return to_ms(day_key(open_time), dtime(15, 0))
    return open_time + timeframe_ms(timeframe)


def _merge_partial_bar(stored: CandlePoint, live: CandlePoint) -> CandlePoint:
    """Combine a stored partial bar with the in-memory accumulator.

    The stored bar came from an import (real open/high/low/volume up to that
    moment); the live bar knows only the prices observed since. The open and the
    extremes must be the union, and the close must be the newest price. Volume is
    summed because the live accumulator only reports what it saw itself.
    """
    return CandlePoint(
        time=stored.time,
        open=stored.open,
        high=max(stored.high, live.high),
        low=min(stored.low, live.low),
        close=live.close,
        volume=round(stored.volume + live.volume, 8),
    )


class LiveCandleProvider(Protocol):
    def live_candle(self, symbol: str) -> CandlePoint | None: ...


class MarketService:
    def __init__(
        self,
        session: Session,
        *,
        clock: MarketClock | None = None,
        live: LiveCandleProvider | None = None,
    ) -> None:
        self.session = session
        self.clock = clock
        self.live = live

    # ------------------------------------------------------------- symbols
    def _symbol(self, code: str) -> Symbol:
        code = code.upper().strip()
        row = self.session.scalar(select(Symbol).where(Symbol.code == code))
        if row is None:
            raise NotFoundError(f"symbol {code} not found")
        return row

    def view(self, row: Symbol) -> SymbolView:
        pct = row.limit_pct
        if row.market == "cn_stock" and pct <= 0:
            from app.modules.cn.rules import limit_pct as resolve_limit

            pct = resolve_limit(row.board, row.is_st)
        return SymbolView(
            code=row.code,
            name=row.name,
            asset_class=row.asset_class,
            base=row.base,
            quote=row.quote,
            tick_size=row.tick_size,
            lot_size=row.lot_size,
            initial_price=row.initial_price,
            enabled=row.enabled,
            market=row.market,
            exchange=row.exchange,
            board=row.board,
            prev_close=row.prev_close,
            limit_pct=pct,
            is_st=row.is_st,
            suspended=row.suspended,
            base_timeframe=row.base_timeframe or BASE_TIMEFRAME,
        )

    def list_symbols(
        self, *, enabled_only: bool = True, market: str | None = None
    ) -> list[SymbolView]:
        stmt = select(Symbol).order_by(Symbol.market.asc(), Symbol.code.asc())
        if enabled_only:
            stmt = stmt.where(Symbol.enabled.is_(True))
        if market:
            if market not in MARKETS:
                raise ValidationError(f"market must be one of {', '.join(MARKETS)}")
            stmt = stmt.where(Symbol.market == market)
        return [self.view(row) for row in self.session.scalars(stmt).all()]

    def require_symbol(self, code: str) -> SymbolView:
        return self.view(self._symbol(code))

    def add_symbol(
        self,
        *,
        code: str,
        name: str,
        initial_price: float,
        base: str = "",
        quote: str = "USDT",
        tick_size: float = 0.01,
        lot_size: float = 0.0001,
        market: str = "crypto",
        exchange: str = "",
        board: str = "",
        prev_close: float = 0.0,
        limit_pct_value: float = 0.0,
        is_st: bool = False,
        base_timeframe: str = BASE_TIMEFRAME,
    ) -> SymbolView:
        code = code.upper().strip()
        if not code or not code.replace(".", "").isalnum():
            raise ValidationError("symbol code must be alphanumeric (dots allowed for exchange suffixes)")
        if initial_price <= 0:
            raise ValidationError("initial price must be positive")
        if market not in MARKETS:
            raise ValidationError(f"market must be one of {', '.join(MARKETS)}")
        if self.session.scalar(select(Symbol.id).where(Symbol.code == code)) is not None:
            raise ValidationError(f"symbol {code} already exists")
        if market == "cn_stock":
            quote = "CNY"
            lot_size = 100.0
            if not board:
                board = "main"
        row = Symbol(
            code=code,
            name=name or code,
            asset_class="stock" if market == "cn_stock" else "crypto",
            market=market,
            exchange=exchange,
            board=board,
            base=base or code.removesuffix(quote).split(".")[0],
            quote=quote.upper(),
            initial_price=float(initial_price),
            tick_size=tick_size,
            lot_size=lot_size,
            prev_close=prev_close or float(initial_price),
            limit_pct=limit_pct_value,
            is_st=is_st,
            base_timeframe=base_timeframe,
        )
        self.session.add(row)
        self.session.commit()
        return self.view(row)

    def set_enabled(self, code: str, enabled: bool) -> SymbolView:
        row = self._symbol(code)
        row.enabled = enabled
        self.session.commit()
        return self.view(row)

    def set_suspended(self, code: str, suspended: bool) -> SymbolView:
        row = self._symbol(code)
        row.suspended = suspended
        self.session.commit()
        return self.view(row)

    # ------------------------------------------------------------- candles
    def _read(
        self,
        symbol: str,
        timeframe: str,
        *,
        limit: int,
        end_ms: int | None = None,
    ) -> list[CandlePoint]:
        stmt = (
            select(Candle)
            .where(Candle.symbol == symbol.upper(), Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(limit)
        )
        if end_ms is not None:
            stmt = stmt.where(Candle.open_time <= end_ms)
        rows = list(self.session.scalars(stmt).all())
        rows.reverse()
        return [
            CandlePoint(
                time=row.open_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
            )
            for row in rows
        ]

    def base_candles(
        self, symbol: str, *, limit: int, end_ms: int | None = None
    ) -> list[CandlePoint]:
        row = self._symbol(symbol)
        timeframe = row.base_timeframe or BASE_TIMEFRAME
        return self._read(symbol, timeframe, limit=limit, end_ms=end_ms)

    def _native_count(self, symbol: str, timeframe: str) -> int:
        from sqlalchemy import func

        return int(
            self.session.scalar(
                select(func.count(Candle.id)).where(
                    Candle.symbol == symbol.upper(), Candle.timeframe == timeframe
                )
            )
            or 0
        )

    def supported_timeframes(self, market: str = "crypto") -> tuple[str, ...]:
        return CN_TIMEFRAMES if market == "cn_stock" else SUPPORTED_TIMEFRAMES

    def candles(
        self,
        symbol: str,
        *,
        timeframe: str = BASE_TIMEFRAME,
        limit: int = 300,
        closed_only: bool = False,
    ) -> list[CandlePoint]:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValidationError(f"timeframe must be one of {', '.join(SUPPORTED_TIMEFRAMES)}")
        limit = max(1, min(int(limit), 5000))
        row = self._symbol(symbol)
        base_tf = row.base_timeframe or BASE_TIMEFRAME
        symbol = row.code
        allowed = self.supported_timeframes(row.market)
        if timeframe not in allowed:
            raise ValidationError(
                f"{row.market} 支持 {', '.join(allowed)}，不支持 {timeframe}"
            )

        # Native series (e.g. A-share daily) beat aggregation when long enough.
        native = self._native_count(symbol, timeframe) if timeframe != base_tf else 0
        if native and (native >= min(limit, 30) or self._native_count(symbol, base_tf) == 0):
            # The second clause matters: a freshly imported symbol may have fewer
            # than `limit` native bars, and aggregating from a base series that
            # does not exist returns *nothing* — an empty chart where the data was
            # actually present.
            points = self._read(symbol, timeframe, limit=limit)
            if closed_only:
                points = self._drop_in_progress(row, timeframe, points)
            return points

        ratio = timeframe_ms(timeframe) // timeframe_ms(base_tf)
        needed = min(limit * ratio + ratio, 20_000)
        points = self._read(symbol, base_tf, limit=needed)
        if self.live is not None and not closed_only:
            current = self.live.live_candle(symbol)
            if current is not None:
                if not points or current.time > points[-1].time:
                    points.append(current)
                elif current.time == points[-1].time:
                    # Kline sources include the still-open candle, so an import
                    # can leave a partial bar at the live bucket's timestamp.
                    # Replacing it wholesale throws away the real open/high/low
                    # and volume that the import already had; merging keeps them
                    # and only takes the fresher price.
                    points[-1] = _merge_partial_bar(points[-1], current)
        if timeframe != base_tf:
            if row.market == "cn_stock":
                from app.modules.cn.aggregate import aggregate_session_bars

                points = aggregate_session_bars(points, timeframe)
            else:
                points = aggregate(points, timeframe)
        if closed_only:
            # The stored base bar can itself be unfinished — the realtime engine
            # only closes a bucket once the next one starts, and a daily bar stays
            # open all session. Filtering here covers every path (native series,
            # aggregation, live append) instead of only the first one.
            points = self._drop_in_progress(row, timeframe, points)
        return points[-limit:]

    def _now_ms(self) -> int:
        """Best available "now" for deciding whether a bar has closed.

        The live engine owns the clock that produced the bars, so it is the only
        consistent reference; the wall clock is the fallback for a market with no
        engine (tests, imported data).
        """
        clock = getattr(self.live, "clock", None)
        if clock is not None:
            try:
                return int(clock.now_ms())
            except Exception:
                pass
        return int(time.time() * 1000)

    def _drop_in_progress(
        self, row: Symbol, timeframe: str, points: list[CandlePoint]
    ) -> list[CandlePoint]:
        """Remove a bar whose interval has not finished yet.

        Real-time feeds keep today's daily bar up to date as prices arrive. That
        row is *unfinished*: a strategy or a backtest reading it sees a close that
        will still move, so the same request returns different results an hour
        later. `closed_only` is the contract that excludes it.
        """
        if not points:
            return points
        last = points[-1]
        if _bar_close_ms(row.market, timeframe, last.time) <= self._now_ms():
            return points
        return points[:-1]

    def snapshot(self, symbol: str) -> Snapshot:
        row = self._symbol(symbol)
        current = self.live.live_candle(row.code) if self.live else None
        if row.market == "cn_stock":
            return self._cn_snapshot(row, current)
        return self._crypto_snapshot(row, current)

    def _crypto_snapshot(self, row: Symbol, current: CandlePoint | None) -> Snapshot:
        quote = self._live_quote(row.code)
        if quote is not None and quote.price > 0:
            # Binance's 24h ticker is itself a rolling 24-hour window, which is
            # exactly what this field means — better than differencing 96 bars,
            # because it does not depend on how many bars happen to be stored.
            prev = quote.open or quote.prev_close
            change = (quote.price / prev - 1.0) * 100.0 if prev else 0.0
            return Snapshot(
                symbol=row.code,
                price=quote.price,
                change_24h_pct=round(change, 4),
                high_24h=quote.high,
                low_24h=quote.low,
                volume_24h=round(quote.volume, 4),
                time=int(quote.timestamp_ms),
                market=row.market,
                prev_close=prev,
            )
        points = self._read(row.code, row.base_timeframe or BASE_TIMEFRAME, limit=97)
        if current is not None:
            points.append(current)
        if not points:
            raise NotFoundError(f"no candles for {symbol}")
        window = points[-96:]
        first = window[0]
        last = window[-1]
        change = (last.close / first.open - 1.0) * 100.0 if first.open else 0.0
        return Snapshot(
            symbol=row.code,
            price=last.close,
            change_24h_pct=round(change, 4),
            high_24h=max(p.high for p in window),
            low_24h=min(p.low for p in window),
            volume_24h=round(sum(p.volume for p in window), 4),
            time=last.time,
            market=row.market,
        )

    def _cn_snapshot(self, row: Symbol, current: CandlePoint | None) -> Snapshot:
        quote = self._live_quote(row.code)
        daily = self._read(row.code, "1d", limit=3)
        prev_close = (
            (quote.prev_close if quote is not None else 0.0)
            or row.prev_close
            or (daily[-2].close if len(daily) >= 2 else 0.0)
        )
        intraday = self._read(row.code, row.base_timeframe or BASE_TIMEFRAME, limit=24)
        if current is not None:
            intraday.append(current)
        # A polled quote is the exchange's own last price; the accumulated 15m bar
        # is our reconstruction of it. Prefer the quote whenever it is fresh.
        price = (
            quote.price
            if quote is not None and quote.price > 0
            else (current.close if current is not None else (daily[-1].close if daily else 0.0))
        )
        if price <= 0:
            raise NotFoundError(f"no price for {row.code}")
        if quote is not None and quote.high > 0 and quote.low > 0:
            high, low = quote.high, quote.low
        else:
            high = max((p.high for p in intraday), default=daily[-1].high if daily else price)
            low = min((p.low for p in intraday), default=daily[-1].low if daily else price)
        volume = quote.volume if quote is not None else sum(p.volume for p in intraday)
        change = (price / prev_close - 1.0) * 100.0 if prev_close else 0.0
        band = price_band(prev_close, row.board, row.is_st, row.tick_size or 0.01)
        return Snapshot(
            symbol=row.code,
            price=price,
            change_24h_pct=round(change, 4),
            high_24h=high,
            low_24h=low,
            volume_24h=round(volume, 4),
            time=quote.timestamp_ms
            if quote is not None
            else (intraday[-1].time if intraday else 0),
            market=row.market,
            prev_close=prev_close,
            limit_up=band.up if band else 0.0,
            limit_down=band.down if band else 0.0,
        )

    def _live_quote(self, code: str):
        """Realtime quote from the provider, when it has one.

        The crypto engine has no quote feed, so this stays optional rather than
        becoming a required part of the provider contract.
        """
        getter = getattr(self.live, "live_quote", None)
        return getter(code) if getter is not None else None

    def snapshots(self, *, market: str | None = None) -> list[Snapshot]:
        out: list[Snapshot] = []
        for view in self.list_symbols(market=market):
            try:
                out.append(self.snapshot(view.code))
            except NotFoundError:
                continue
        return out

    def last_price(self, symbol: str) -> float:
        row = self._symbol(symbol)
        if self.live is not None:
            # An exchange quote outranks our reconstructed bar: the bar's close is
            # the last polled price, the quote is the authoritative one.
            quote = self._live_quote(row.code)
            if quote is not None and quote.price > 0:
                return quote.price
            current = self.live.live_candle(row.code)
            if current is not None:
                return current.close
        points = self._read(row.code, row.base_timeframe or BASE_TIMEFRAME, limit=1)
        if not points:
            raise NotFoundError(f"no price for {symbol}")
        return points[-1].close

    def indicators(
        self,
        symbol: str,
        *,
        timeframe: str,
        limit: int,
        specs: Sequence[tuple[str, dict[str, float]]],
    ) -> tuple[list[CandlePoint], dict[str, list[float | None]]]:
        points = self.candles(symbol, timeframe=timeframe, limit=limit)
        series: dict[str, list[float | None]] = {}
        for name, params in specs:
            try:
                result = compute(name, points, params)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            label = name.upper()
            if params:
                label += "(" + ",".join(str(int(v)) if v.is_integer() else str(v) for v in params.values()) + ")"
            for key, values in result.items():
                suffix = "" if len(result) == 1 else f".{key}"
                series[label + suffix] = values
        return points, series

    def clear_history(self, symbol: str) -> int:
        row = self._symbol(symbol)
        result = self.session.execute(delete(Candle).where(Candle.symbol == row.code))
        self.session.commit()
        return int(result.rowcount or 0)
