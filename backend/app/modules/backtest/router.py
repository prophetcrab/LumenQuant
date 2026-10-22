from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_backtests
from app.core.errors import ValidationError
from app.modules.backtest.optimiser import OptimiserService, parse_space
from app.modules.backtest.schemas import (
    BacktestRequest,
    MultiBacktestRequest,
    OptimiseRequest,
    PortfolioRequest,
    WalkForwardRequest,
)
from app.modules.backtest.multi import MultiSymbolBacktester
from app.modules.cn.fees import CnFeeSchedule
from app.modules.backtest.portfolio import PortfolioAnalyser
from app.modules.backtest.walkforward import WalkForwardService
from app.modules.backtest.service import BacktestService


router = APIRouter(prefix="/backtests", tags=["backtest"])


def build_service(request: Request, session: Session) -> BacktestService:
    return build_backtests(request, session)


@router.post("", status_code=201)
def run_backtest(
    payload: BacktestRequest, request: Request, session: Session = Depends(get_session)
):
    return build_service(request, session).run(
        strategy_key=payload.strategy,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        bars=payload.bars,
        params=payload.params,
        config=payload.config,
    )


@router.get("")
def list_backtests(
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
    session: Session = Depends(get_session),
):
    return build_service(request, session).list(limit=limit)


@router.post("/optimise")
def optimise(
    payload: OptimiseRequest, request: Request, session: Session = Depends(get_session)
):
    """Grid search with in/out-of-sample validation.

    Returns the best parameter sets plus overfitting warnings; it does not persist
    a backtest run, so repeated searches do not pollute the history list.
    """
    from app.api.wiring import build_market
    from app.modules.backtest.engine import BacktestConfig
    from app.modules.market.service import SUPPORTED_TIMEFRAMES
    from app.modules.strategies.registry import get

    settings = request.app.state.settings
    if payload.timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValidationError(f"unsupported timeframe {payload.timeframe}")

    strategy = get(payload.strategy)
    market = build_market(request, session, live=False)
    symbol_view = market.require_symbol(payload.symbol)
    candles = market.candles(payload.symbol, timeframe=payload.timeframe, limit=payload.bars)
    if not candles:
        raise ValidationError(f"{payload.symbol} 没有可用的 K 线数据")

    raw_config = dict(payload.config or {})
    raw_config.setdefault("fee_bps", settings.fee_bps)
    raw_config.setdefault("slippage_bps", settings.slippage_bps)
    raw_config["market"] = symbol_view.market
    raw_config["tick_size"] = symbol_view.tick_size
    if symbol_view.market == "cn_stock":
        raw_config["limit_pct"] = symbol_view.limit_pct
        raw_config["lot_size"] = 100.0
        raw_config["t_plus_one"] = True
    cfg = BacktestConfig.from_dict(raw_config)

    service = OptimiserService(
        cn_fees=CnFeeSchedule(
            commission_bps=settings.cn_commission_bps,
            min_commission=settings.cn_min_commission,
            stamp_duty_bps=settings.cn_stamp_duty_bps,
            transfer_fee_bps=settings.cn_transfer_fee_bps,
        )
    )
    report = service.optimise(
        strategy,
        candles,
        space=parse_space(payload.space),
        base_params=payload.base_params,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        objective=payload.objective,
        config=cfg,
        top_n=payload.top_n,
        # Synthetic data produces unrealistically smooth trends; the optimiser
        # must say so instead of letting the numbers speak for themselves.
        data_source=(
            "synthetic"
            if symbol_view.market == "crypto" or settings.cn_data_source == "synthetic"
            else "real"
        ),
    )
    return report.as_dict()


@router.post("/walk-forward")
def walk_forward(
    payload: WalkForwardRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """Rolling train/optimise/test validation across several windows."""
    from app.api.wiring import build_market
    from app.modules.backtest.engine import BacktestConfig
    from app.modules.market.service import SUPPORTED_TIMEFRAMES
    from app.modules.strategies.registry import get

    settings = request.app.state.settings
    if payload.timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValidationError(f"unsupported timeframe {payload.timeframe}")

    strategy = get(payload.strategy)
    market = build_market(request, session, live=False)
    symbol_view = market.require_symbol(payload.symbol)
    candles = market.candles(payload.symbol, timeframe=payload.timeframe, limit=payload.bars)
    if not candles:
        raise ValidationError(f"{payload.symbol} 没有可用的 K 线数据")

    raw_config = dict(payload.config or {})
    raw_config.setdefault("fee_bps", settings.fee_bps)
    raw_config.setdefault("slippage_bps", settings.slippage_bps)
    raw_config["market"] = symbol_view.market
    raw_config["tick_size"] = symbol_view.tick_size
    if symbol_view.market == "cn_stock":
        raw_config["limit_pct"] = symbol_view.limit_pct
        raw_config["lot_size"] = 100.0
        raw_config["t_plus_one"] = True
    cfg = BacktestConfig.from_dict(raw_config)

    service = WalkForwardService(
        cn_fees=CnFeeSchedule(
            commission_bps=settings.cn_commission_bps,
            min_commission=settings.cn_min_commission,
            stamp_duty_bps=settings.cn_stamp_duty_bps,
            transfer_fee_bps=settings.cn_transfer_fee_bps,
        )
    )
    report = service.run(
        strategy,
        candles,
        space=parse_space(payload.space),
        base_params=payload.base_params,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        objective=payload.objective,
        config=cfg,
        windows=payload.windows,
        data_source=(
            "synthetic"
            if symbol_view.market == "crypto" or settings.cn_data_source == "synthetic"
            else "real"
        ),
    )
    return report.as_dict()


@router.post("/multi")
def multi_symbol(payload: MultiBacktestRequest, request: Request, session: Session = Depends(get_session)):
    """Run one strategy across a basket, splitting capital by weight.

    Sleeves are traded independently — this is not a cross-symbol portfolio
    optimiser, and the response says so in its warnings when the basket behaves
    suspiciously uniformly.
    """
    from app.api.wiring import build_market

    settings = request.app.state.settings
    market = build_market(request, session, live=False)
    service = MultiSymbolBacktester(
        market,
        session=session,
        fee_bps=settings.fee_bps,
        slippage_bps=settings.slippage_bps,
        cn_fees=CnFeeSchedule(
            commission_bps=settings.cn_commission_bps,
            min_commission=settings.cn_min_commission,
            stamp_duty_bps=settings.cn_stamp_duty_bps,
            transfer_fee_bps=settings.cn_transfer_fee_bps,
        ),
    )
    return service.run(
        strategy_key=payload.strategy,
        symbols=payload.symbols,
        timeframe=payload.timeframe,
        bars=payload.bars,
        params=payload.params,
        config=payload.config,
        weights=payload.weights,
        rebalance_bars=payload.rebalance_bars,
    )


@router.get("/multi")
def list_multi(
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
    session: Session = Depends(get_session),
):
    from app.api.wiring import build_market

    return MultiSymbolBacktester(
        build_market(request, session, live=False), session=session
    ).list_runs(limit=limit)


@router.get("/multi/{run_id}")
def get_multi(run_id: str, request: Request, session: Session = Depends(get_session)):
    from app.api.wiring import build_market

    return MultiSymbolBacktester(
        build_market(request, session, live=False), session=session
    ).get_run(run_id)


@router.delete("/multi/{run_id}", status_code=204)
def delete_multi(run_id: str, request: Request, session: Session = Depends(get_session)):
    from app.api.wiring import build_market

    MultiSymbolBacktester(
        build_market(request, session, live=False), session=session
    ).delete_run(run_id)


@router.post("/portfolio")
def portfolio(payload: PortfolioRequest, session: Session = Depends(get_session)):
    """Combine stored backtests into one portfolio.

    Read-only over existing runs: the analysis reuses the equity curves the user
    already saw, so re-running it can never produce slightly different numbers.
    """
    return PortfolioAnalyser(session).analyse(
        run_ids=payload.run_ids,
        weights=payload.weights,
        initial_capital=payload.initial_capital,
    )


@router.get("/{run_id}")
def get_backtest(run_id: str, request: Request, session: Session = Depends(get_session)):
    return build_service(request, session).get(run_id)


@router.delete("/{run_id}", status_code=204)
def delete_backtest(run_id: str, request: Request, session: Session = Depends(get_session)):
    build_service(request, session).delete(run_id)
