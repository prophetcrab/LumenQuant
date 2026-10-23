"""A-share screening endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_market, settings_of
from app.modules.market.service import MarketService
from app.modules.screener.schemas import AddSymbolRequest, ScreenFilter
from app.modules.screener.service import ScreenerService, preset_filters
from app.modules.screener.sources.sina import SinaMarketSource, SinaUnavailable

router = APIRouter(prefix="/screener", tags=["screener"])


def _service(request: Request, session: Session) -> ScreenerService:
    settings = settings_of(request)
    source = SinaMarketSource(timeout=settings.request_timeout_seconds)
    return ScreenerService(
        source,
        market=build_market(request, session),
        cache=getattr(request.app.state, "screener_cache", None),
    )


@router.get("/source")
def source_status(request: Request):
    settings = settings_of(request)
    available, detail, latency = SinaMarketSource(
        timeout=settings.request_timeout_seconds
    ).health()
    return {
        "source": "sina-market",
        "available": available,
        "detail": detail,
        "latency_ms": latency,
    }


@router.get("/presets")
def presets():
    return [
        {
            "key": item["key"],
            "name": item["name"],
            "description": item["description"],
            "filter": item["filter"].model_dump(exclude_none=True),
        }
        for item in preset_filters()
    ]


@router.post("/screen")
def screen(
    payload: ScreenFilter,
    request: Request,
    session: Session = Depends(get_session),
    force: bool = False,
):
    """Filter the whole A-share market.

    `force=true` bypasses the one-minute snapshot cache; use it when the user
    explicitly asks for a refresh rather than on every keystroke.
    """
    try:
        return _service(request, session).screen(payload, force=force)
    except SinaUnavailable as exc:
        # 502: the upstream refused us, which is not the caller's fault.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/presets/{key}")
def run_preset(
    key: str,
    request: Request,
    session: Session = Depends(get_session),
    force: bool = False,
):
    selected = {item["key"]: item for item in preset_filters()}.get(key)
    if selected is None:
        raise HTTPException(status_code=404, detail=f"unknown preset '{key}'")
    try:
        return _service(request, session).screen(selected["filter"], force=force)
    except SinaUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/add-symbol", status_code=201)
def add_symbol(
    payload: AddSymbolRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """Import a screened name into the local catalog.

    A screening hit is only useful if it can then be charted, backtested and traded.
    The local catalog is what every other module reads, so this is the bridge — it
    deliberately does not fetch bars (that is `/api/cn/data/sync`'s job).
    """
    market: MarketService = build_market(request, session)
    code = payload.code.strip().upper()
    if not code.endswith((".SH", ".SZ", ".BJ")):
        raise HTTPException(
            status_code=422, detail="A股代码需带交易所后缀，例如 600519.SH"
        )
    existing = [view for view in market.list_symbols(market="cn_stock") if view.code == code]
    if existing:
        return {"created": False, "symbol": existing[0].code, "detail": "已在标的列表中"}
    # The price is unknown until the first quote/sync; `prev_close` is what the
    # limit band needs, so leave both at 0 and let the sync fill them.
    view = market.add_symbol(
        code=code,
        name=payload.name or code.split(".")[0],
        initial_price=1.0,
        market="cn_stock",
        board=_board_of(code),
        prev_close=0.0,
    )
    return {"created": True, "symbol": view.code, "detail": "已加入标的列表，请同步历史数据"}


def _board_of(code: str) -> str:
    digits = code.split(".")[0]
    if code.endswith(".BJ") or digits.startswith(("4", "8", "9")):
        return "bse"
    if digits.startswith("688"):
        return "star"
    if digits.startswith(("300", "301")):
        return "gem"
    return "main"
