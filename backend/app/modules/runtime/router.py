from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.wiring import build_accounts, build_market, build_trading
from app.modules.alerts.service import AlertService
from app.modules.runtime.schemas import InstanceCreate, InstanceUpdate, RunningToggle
from app.modules.runtime.service import RuntimeService


router = APIRouter(prefix="/runtime", tags=["runtime"])


def build_service(request: Request, session: Session) -> RuntimeService:
    accounts = build_accounts(request, session)
    market = build_market(request, session)
    trading = build_trading(request, session, accounts=accounts, market=market, with_brokers=True)
    settings = request.app.state.settings
    return RuntimeService(
        session,
        accounts_service=accounts,
        market=market,
        trading=trading,
        live_auto_trading_enabled=settings.live_auto_trading_enabled,
        alerts=AlertService(session),
    )


@router.get("/instances")
def list_instances(
    request: Request,
    account_id: str | None = None,
    session: Session = Depends(get_session),
):
    return build_service(request, session).list(account_id=account_id)


@router.post("/instances", status_code=201)
def create_instance(
    payload: InstanceCreate, request: Request, session: Session = Depends(get_session)
):
    return build_service(request, session).create(
        account_id=payload.account_id,
        strategy_key=payload.strategy_key,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        params=payload.params,
        position_pct=payload.position_pct,
        max_order_notional=payload.max_order_notional,
        stop_loss_pct=payload.stop_loss_pct,
        take_profit_pct=payload.take_profit_pct,
        trailing_stop_pct=payload.trailing_stop_pct,
        start=payload.start,
    )


@router.patch("/instances/{instance_id}")
def update_instance(
    instance_id: str,
    payload: InstanceUpdate,
    request: Request,
    session: Session = Depends(get_session),
):
    return build_service(request, session).update(
        instance_id,
        params=payload.params,
        position_pct=payload.position_pct,
        max_order_notional=payload.max_order_notional,
        stop_loss_pct=payload.stop_loss_pct,
        take_profit_pct=payload.take_profit_pct,
        trailing_stop_pct=payload.trailing_stop_pct,
    )


@router.post("/instances/{instance_id}/toggle")
def toggle_instance(
    instance_id: str,
    payload: RunningToggle,
    request: Request,
    session: Session = Depends(get_session),
):
    return build_service(request, session).set_running(instance_id, payload.running)


@router.delete("/instances/{instance_id}", status_code=204)
def delete_instance(instance_id: str, request: Request, session: Session = Depends(get_session)):
    build_service(request, session).delete(instance_id)


@router.get("/instances/{instance_id}/logs")
def instance_logs(
    instance_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    session: Session = Depends(get_session),
):
    return build_service(request, session).logs(instance_id, limit=limit)


@router.post("/tick")
def tick(request: Request, session: Session = Depends(get_session)):
    """Manual evaluation pass, used by the UI refresh and by tests.

    Mirrors the scheduler's order exactly — protective exits first, then
    signals — so a manual pass can be used to verify a stop actually fires.
    Skipping the sweep here made "立即评估" quietly disagree with production.
    """
    service = build_service(request, session)
    results = {}
    # Same one-read set as the scheduler, so the manual pass cannot disagree with
    # production about which instances were running when it started.
    running = service.running_instances()
    try:
        results.update(service.protect_positions(running))
    except Exception:  # a broken stop check must not block signal evaluation
        pass
    for row in running:
        # One malformed instance (bad stored params, a timeframe its market no
        # longer supports) must not abort the pass and starve every other
        # instance. The background scheduler already isolates per instance; the
        # manual endpoint used to let the exception escape as a 422 for the whole
        # request.
        try:
            results[row.id] = service.evaluate_instance(row)
        except Exception as exc:
            row.last_error = str(exc)[:400]
            session.commit()
            results[row.id] = "error"
    return {"evaluated": results}
