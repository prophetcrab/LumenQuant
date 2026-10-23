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
    sync: bool = True,
):
    """Import a screened name into the catalog, and by default its history too.

    Registering the code alone left the symbol in a state that *looks* usable and
    is not: `initial_price=1.0` meant the list showed a 1 元 price with a 0.9–1.1
    limit band, and every backtest failed with "数据不足". The user had to discover
    on their own that a second, differently-placed action was required.

    `sync=false` keeps the old catalog-only behaviour for callers that want to
    stage many symbols and fetch them in one later pass.
    """
    settings = settings_of(request)
    market: MarketService = build_market(request, session)
    code = payload.code.strip().upper()
    if not code.endswith((".SH", ".SZ", ".BJ")):
        raise HTTPException(
            status_code=422, detail="A股代码需带交易所后缀，例如 600519.SH"
        )

    existing = [view for view in market.list_symbols(market="cn_stock") if view.code == code]
    created = not existing
    if created:
        view = market.add_symbol(
            code=code,
            name=payload.name or code.split(".")[0],
            initial_price=1.0,
            market="cn_stock",
            board=_board_of(code),
            prev_close=0.0,
        )
        name = view.name
    else:
        name = existing[0].name

    result = {
        "created": created,
        "symbol": code,
        "name": name,
        "synced": False,
        "daily_bars": 0,
        "intraday_bars": 0,
        "detail": "已在标的列表中" if not created else "已加入标的列表",
    }
    if not sync:
        result["detail"] += "，请稍后同步历史数据"
        return result

    if settings.cn_data_source == "synthetic":
        # Nothing to fetch from: the synthetic engine seeds its own catalog and
        # would not know this symbol. Say so instead of reporting a silent no-op.
        result["detail"] = "已加入标的列表（合成模式没有可同步的数据源）"
        return result

    from app.core.cache import invalidate_symbol_caches
    from app.modules.cn.sources.base import SourceUnavailable
    from app.modules.cn.sources.tencent import TencentSource
    from app.modules.cn.sync import CnDataSyncService

    try:
        service = CnDataSyncService(
            session, TencentSource(timeout=settings.request_timeout_seconds)
        )
        report = service.sync_symbols(
            [code],
            daily_limit=settings.cn_sync_daily_limit,
            intraday_limit=settings.cn_sync_intraday_limit,
        )
    except SourceUnavailable as exc:
        # The symbol stays registered: the user can retry the sync from the A-share
        # page, and discarding the row would make them repeat the whole screen.
        result["detail"] = f"已加入标的，但历史同步失败：{exc}"[:200]
        return result
    except Exception as exc:
        result["detail"] = f"已加入标的，但历史同步异常：{type(exc).__name__}: {exc}"[:200]
        return result

    item = report.symbols[0] if report.symbols else None
    if item is not None and item.error:
        result["detail"] = f"已加入标的，但历史同步失败：{item.error}"[:200]
        return result
    result["synced"] = True
    result["daily_bars"] = item.daily_bars if item else 0
    result["intraday_bars"] = item.intraday_bars if item else 0
    result["detail"] = (
        f"已加入标的并导入 {result['daily_bars']} 根日线、"
        f"{result['intraday_bars']} 根 15 分钟线"
    )
    invalidate_symbol_caches(request.app.state, code)
    return result


def _board_of(code: str) -> str:
    digits = code.split(".")[0]
    if code.endswith(".BJ") or digits.startswith(("4", "8", "9")):
        return "bse"
    if digits.startswith("688"):
        return "star"
    if digits.startswith(("300", "301")):
        return "gem"
    return "main"
