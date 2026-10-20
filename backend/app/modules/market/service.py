"""Read-side market service: symbols, candles, snapshots, indicators.

Two markets share this service:

* crypto — 24/7 synthetic 15m bars aggregated on read;
* cn_stock — session-bound 15m bars plus native daily bars seeded by CnMarketEngine.

When a native series exists for the requested timeframe it is preferred over
aggregation, so A-share daily backtests use real daily bars instead of 16-bar
aggregates.
"""

from __future__ import annotations

from collections.abc import Sequence
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
        if timeframe != base_tf and self._native_count(symbol, timeframe) >= min(limit, 30):
            return self._read(symbol, timeframe, limit=limit)

        ratio = timeframe_ms(timeframe) // timeframe_ms(base_tf)
        needed = min(limit * ratio + ratio, 20_000)
        points = self._read(symbol, base_tf, limit=needed)
        if self.live is not None and not closed_only:
            current = self.live.live_candle(symbol)
            if current is not None and (not points or current.time > points[-1].time):
                points.append(current)
        if timeframe != base_tf:
            if row.market == "cn_stock":
                from app.modules.cn.aggregate import aggregate_session_bars

                points = aggregate_session_bars(points, timeframe)
            else:
                points = aggregate(points, timeframe)
        return points[-limit:]

    def snapshot(self, symbol: str) -> Snapshot:
        row = self._symbol(symbol)
        current = self.live.live_candle(row.code) if self.live else None
        if row.market == "cn_stock":
            return self._cn_snapshot(row, current)
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
        daily = self._read(row.code, "1d", limit=3)
        prev_close = row.prev_close or (daily[-2].close if len(daily) >= 2 else 0.0)
        intraday = self._read(row.code, row.base_timeframe or BASE_TIMEFRAME, limit=24)
        if current is not None:
            intraday.append(current)
        price = current.close if current is not None else (daily[-1].close if daily else 0.0)
        if price <= 0:
            raise NotFoundError(f"no price for {row.code}")
        high = max((p.high for p in intraday), default=daily[-1].high if daily else price)
        low = min((p.low for p in intraday), default=daily[-1].low if daily else price)
        volume = sum(p.volume for p in intraday)
        change = (price / prev_close - 1.0) * 100.0 if prev_close else 0.0
        band = price_band(prev_close, row.board, row.is_st, row.tick_size or 0.01)
        return Snapshot(
            symbol=row.code,
            price=price,
            change_24h_pct=round(change, 4),
            high_24h=high,
            low_24h=low,
            volume_24h=round(volume, 4),
            time=intraday[-1].time if intraday else 0,
            market=row.market,
            prev_close=prev_close,
            limit_up=band.up if band else 0.0,
            limit_down=band.down if band else 0.0,
        )

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
