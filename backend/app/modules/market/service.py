"""Read-side market service: symbols, candles, snapshots, indicators."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.modules.market.aggregate import aggregate
from app.modules.market.feed import MarketClock, timeframe_ms
from app.modules.market.indicators import compute
from app.modules.market.models import Candle, Symbol
from app.modules.market.service_types import CandlePoint, Snapshot, SymbolView

BASE_TIMEFRAME = "15m"
SUPPORTED_TIMEFRAMES = ("15m", "30m", "1h", "4h", "1d")


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
        )

    def list_symbols(self, *, enabled_only: bool = True) -> list[SymbolView]:
        stmt = select(Symbol).order_by(Symbol.code.asc())
        if enabled_only:
            stmt = stmt.where(Symbol.enabled.is_(True))
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
    ) -> SymbolView:
        code = code.upper().strip()
        if not code or not code.isalnum():
            raise ValidationError("symbol code must be alphanumeric")
        if initial_price <= 0:
            raise ValidationError("initial price must be positive")
        if self.session.scalar(select(Symbol.id).where(Symbol.code == code)) is not None:
            raise ValidationError(f"symbol {code} already exists")
        row = Symbol(
            code=code,
            name=name or code,
            base=base or code.removesuffix(quote),
            quote=quote.upper(),
            initial_price=float(initial_price),
            tick_size=tick_size,
            lot_size=lot_size,
        )
        self.session.add(row)
        self.session.commit()
        return self.view(row)

    def set_enabled(self, code: str, enabled: bool) -> SymbolView:
        row = self._symbol(code)
        row.enabled = enabled
        self.session.commit()
        return self.view(row)

    # ------------------------------------------------------------- candles
    def base_candles(self, symbol: str, *, limit: int, end_ms: int | None = None) -> list[CandlePoint]:
        code = symbol.upper()
        stmt = (
            select(Candle)
            .where(Candle.symbol == code, Candle.timeframe == BASE_TIMEFRAME)
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

    def candles(
        self, symbol: str, *, timeframe: str = BASE_TIMEFRAME, limit: int = 300
    ) -> list[CandlePoint]:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValidationError(f"timeframe must be one of {', '.join(SUPPORTED_TIMEFRAMES)}")
        limit = max(1, min(int(limit), 5000))
        ratio = timeframe_ms(timeframe) // timeframe_ms(BASE_TIMEFRAME)
        needed = min(limit * ratio + ratio, 20_000)
        points = self.base_candles(symbol, limit=needed)
        if self.live is not None and timeframe == BASE_TIMEFRAME:
            current = self.live.live_candle(symbol.upper())
            if current is not None and (not points or current.time > points[-1].time):
                points.append(current)
        elif self.live is not None:
            current = self.live.live_candle(symbol.upper())
            if current is not None and (not points or current.time > points[-1].time):
                points.append(current)
        if timeframe != BASE_TIMEFRAME:
            points = aggregate(points, timeframe)
        return points[-limit:]

    def snapshot(self, symbol: str) -> Snapshot:
        points = self.base_candles(symbol, limit=97)
        current = self.live.live_candle(symbol.upper()) if self.live else None
        if current is not None:
            points.append(current)
        if not points:
            raise NotFoundError(f"no candles for {symbol}")
        window = points[-96:]
        first = window[0]
        last = window[-1]
        change = (last.close / first.open - 1.0) * 100.0 if first.open else 0.0
        return Snapshot(
            symbol=symbol.upper(),
            price=last.close,
            change_24h_pct=round(change, 4),
            high_24h=max(p.high for p in window),
            low_24h=min(p.low for p in window),
            volume_24h=round(sum(p.volume for p in window), 4),
            time=last.time,
        )

    def snapshots(self) -> list[Snapshot]:
        out: list[Snapshot] = []
        for view in self.list_symbols():
            try:
                out.append(self.snapshot(view.code))
            except NotFoundError:
                continue
        return out

    def last_price(self, symbol: str) -> float:
        if self.live is not None:
            current = self.live.live_candle(symbol.upper())
            if current is not None:
                return current.close
        points = self.base_candles(symbol, limit=1)
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
        code = symbol.upper()
        self._symbol(code)
        result = self.session.execute(
            delete(Candle).where(Candle.symbol == code, Candle.timeframe == BASE_TIMEFRAME)
        )
        self.session.commit()
        return int(result.rowcount or 0)
