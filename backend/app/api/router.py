from __future__ import annotations

from fastapi import APIRouter

from app.modules.accounts.router import router as accounts_router


api_router = APIRouter()
api_router.include_router(accounts_router)


@api_router.get("/meta")
def meta():
    return {
        "modules": ["accounts"],
        "capabilities": {"paper_trading": True, "live_trading": False, "backtest": False},
    }
