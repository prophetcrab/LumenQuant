from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_market
from app.core.errors import ValidationError
from app.modules.market.schemas import SymbolCreate, SymbolUpdate
from app.modules.market.service import SUPPORTED_TIMEFRAMES, MarketService
from app.modules.market.service_types import CandlePoint


router = APIRouter(prefix="/market", tags=["market"])

_SPEC_KEYS = {
    "sma": ("period",),
    "ema": ("period",),
    "rsi": ("period",),
    "atr": ("period",),
    "adx": ("period",),
    "stochastic": ("period", "smooth"),
    "macd": ("fast", "slow", "signal"),
    "bollinger": ("period", "deviations"),
}
_DEFAULTS = {
    "sma": {"period": 20.0},
    "ema": {"period": 20.0},
    "rsi": {"period": 14.0},
    "atr": {"period": 14.0},
    "adx": {"period": 14.0},
    "stochastic": {"period": 14.0, "smooth": 3.0},
    "macd": {"fast": 12.0, "slow": 26.0, "signal": 9.0},
    "bollinger": {"period": 20.0, "deviations": 2.0},
}


def _service(request: Request, session: Session) -> MarketService:
    return build_market(request, session)


def parse_specs(raw: str) -> list[tuple[str, dict[str, float]]]:
    specs: list[tuple[str, dict[str, float]]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        name = parts[0].lower()
        if name not in _SPEC_KEYS:
            raise ValidationError(f"unknown indicator '{name}'")
        keys = _SPEC_KEYS[name]
        if len(parts) - 1 > len(keys):
            raise ValidationError(f"too many parameters for {name}")
        params = dict(_DEFAULTS[name])
        for key, value in zip(keys, parts[1:]):
            try:
                params[key] = float(value)
            except ValueError as exc:
                raise ValidationError(f"invalid parameter '{value}' for {name}") from exc
        specs.append((name, params))
    if not specs:
        raise ValidationError("at least one indicator spec is required, e.g. sma:20,macd")
    return specs


def _candle_dict(point: CandlePoint) -> dict:
    return asdict(point)


@router.get("/symbols")
def list_symbols(
    request: Request,
    market: str | None = None,
    session: Session = Depends(get_session),
):
    return _service(request, session).list_symbols(market=market)


@router.post("/symbols", status_code=201)
def create_symbol(payload: SymbolCreate, request: Request, session: Session = Depends(get_session)):
    return _service(request, session).add_symbol(
        code=payload.code,
        name=payload.name,
        initial_price=payload.initial_price,
        base=payload.base,
        quote=payload.quote,
        tick_size=payload.tick_size,
        lot_size=payload.lot_size,
    )


@router.patch("/symbols/{code}")
def update_symbol(
    code: str,
    payload: SymbolUpdate,
    request: Request,
    session: Session = Depends(get_session),
):
    return _service(request, session).set_enabled(code, payload.enabled)


@router.get("/candles")
def candles(
    request: Request,
    symbol: str = Query(default="BTCUSDT"),
    timeframe: str = Query(default="15m"),
    limit: int = Query(default=300, ge=1, le=5000),
    session: Session = Depends(get_session),
):
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValidationError(f"timeframe must be one of {', '.join(SUPPORTED_TIMEFRAMES)}")
    cache = getattr(request.app.state, "candle_cache", None)
    if cache is None:
        points = _service(request, session).candles(symbol, timeframe=timeframe, limit=limit)
    else:
        key = f"{symbol.upper()}:{timeframe}:{limit}"
        points = cache.get_or_load(
            key,
            lambda: _service(request, session).candles(
                symbol, timeframe=timeframe, limit=limit
            ),
        )
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "candles": [_candle_dict(p) for p in points],
    }


@router.get("/indicators")
def indicators(
    request: Request,
    symbol: str = Query(default="BTCUSDT"),
    timeframe: str = Query(default="15m"),
    limit: int = Query(default=300, ge=20, le=5000),
    specs: str = Query(default="sma:20,ema:50,rsi:14"),
    session: Session = Depends(get_session),
):
    parsed = parse_specs(specs)
    points, series = _service(request, session).indicators(
        symbol, timeframe=timeframe, limit=limit, specs=parsed
    )
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "times": [p.time for p in points],
        "series": series,
    }


@router.get("/snapshot")
def snapshot(
    request: Request,
    symbol: str = Query(default="BTCUSDT"),
    session: Session = Depends(get_session),
):
    return asdict(_service(request, session).snapshot(symbol))


@router.get("/snapshots")
def snapshots(
    request: Request,
    market: str | None = None,
    session: Session = Depends(get_session),
):
    cache = getattr(request.app.state, "market_cache", None)
    if cache is None:
        return [asdict(s) for s in _service(request, session).snapshots(market=market)]
    # The dashboard polls this every few seconds; a 1.5s cache collapses
    # duplicate work from multiple tabs without making prices feel stale.
    return cache.get_or_load(
        f"snapshots:{market or 'all'}",
        lambda: [asdict(s) for s in _service(request, session).snapshots(market=market)],
    )


@router.get("/clock")
def clock(request: Request):
    crypto = getattr(request.app.state, "market_engine", None)
    cn = getattr(request.app.state, "cn_market_engine", None)
    payload = {"simulated_now": 0, "speed": 1.0, "timeframe": "15m", "tick_seconds": 0}
    if crypto is not None:
        payload = {**payload, **crypto.clock_info()}
    if cn is not None:
        payload["cn"] = cn.clock_info()
    return payload


@router.get("/symbols/{code}")
def get_symbol(code: str, request: Request, session: Session = Depends(get_session)):
    return _service(request, session).require_symbol(code)
