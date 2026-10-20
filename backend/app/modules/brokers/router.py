from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request

from app.core.errors import ConflictError, ForbiddenError
from app.modules.brokers.binance import build_broker


router = APIRouter(prefix="/brokers", tags=["brokers"])


@router.get("/status")
def broker_status(request: Request):
    settings = request.app.state.settings
    configured = bool(settings.binance_api_key and settings.binance_api_secret)
    return {
        "live_trading_enabled": settings.live_trading_enabled,
        "broker": "binance",
        "credentials_configured": configured,
        "ready": settings.live_trading_enabled and configured,
    }


@router.get("/binance/verify")
def verify_binance(request: Request):
    settings = request.app.state.settings
    if not settings.live_trading_enabled:
        raise ForbiddenError("live trading is disabled by server configuration")
    broker = build_broker(settings)
    if broker is None:
        raise ConflictError("binance credentials are not configured")
    account = broker.verify_credentials()
    return asdict(account)
