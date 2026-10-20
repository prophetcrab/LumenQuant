from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.errors import NotFoundError
from app.modules.accounts.service import AccountService
from app.modules.assistant.schemas import ChatRequest, ToolCallRequest
from app.modules.assistant.service import AssistantService
from app.modules.assistant.tools import ToolBox
from app.modules.backtest.service import BacktestService
from app.modules.market.service import MarketService
from app.modules.portfolio.service import PortfolioService
from app.modules.trading.adapters import AccountCashAdapter, MarketPriceAdapter
from app.modules.trading.service import TradingService


router = APIRouter(prefix="/assistant", tags=["assistant"])


def build_service(request: Request, session: Session) -> AssistantService:
    settings = request.app.state.settings
    engine = getattr(request.app.state, "market_engine", None)
    accounts = AccountService(session, live_trading_enabled=settings.live_trading_enabled)
    market = MarketService(
        session, clock=getattr(engine, "clock", None), live=engine if engine else None
    )
    trading = TradingService(
        session,
        cash=AccountCashAdapter(accounts),
        prices=MarketPriceAdapter(market),
        bus=request.app.state.bus,
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
    )
    portfolio = PortfolioService(session, accounts, trading)
    backtests = BacktestService(
        session, market, fee_bps=settings.fee_bps, slippage_bps=settings.slippage_bps
    )
    toolbox = ToolBox(
        session,
        accounts=accounts,
        market=market,
        trading=trading,
        portfolio=portfolio,
        backtests=backtests,
    )
    return AssistantService(toolbox, settings)


@router.get("/tools")
def list_tools(request: Request, session: Session = Depends(get_session)):
    service = build_service(request, session)
    return [tool.as_dict() for tool in service.toolbox.tools]


@router.get("/status")
def status(request: Request):
    settings = request.app.state.settings
    mode = "llm" if settings.llm_base_url and settings.llm_model else "local"
    return {"mode": mode, "model": settings.llm_model if mode == "llm" else ""}


@router.post("/chat")
def chat(payload: ChatRequest, request: Request, session: Session = Depends(get_session)):
    service = build_service(request, session)
    reply = service.reply(payload.message, account_id=payload.account_id)
    return asdict(reply)


@router.post("/tools/{name}")
def invoke_tool(
    name: str,
    payload: ToolCallRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    service = build_service(request, session)
    if name != payload.tool:
        raise NotFoundError("tool name mismatch")
    try:
        result = service.toolbox.call(name, payload.arguments)
    except KeyError as exc:
        raise NotFoundError(f"tool {name} not found") from exc
    return {"tool": name, "result": result}
