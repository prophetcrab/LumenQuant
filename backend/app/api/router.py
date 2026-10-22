from __future__ import annotations

from fastapi import APIRouter

from app.modules.accounts.router import router as accounts_router
from app.modules.alerts.router import router as alerts_router
from app.modules.assistant.router import router as assistant_router
from app.modules.backtest.router import router as backtest_router
from app.modules.brokers.router import router as brokers_router
from app.modules.cn.data_router import router as cn_data_router
from app.modules.cn.router import router as cn_router
from app.modules.data.router import router as data_router
from app.modules.market.data_router import router as market_data_router
from app.modules.market.router import router as market_router
from app.modules.portfolio.router import router as portfolio_router
from app.modules.risk.router import router as risk_router
from app.modules.runtime.router import router as runtime_router
from app.modules.strategies.router import router as strategies_router
from app.modules.system.router import router as system_router
from app.modules.trading.router import router as trading_router


api_router = APIRouter()
api_router.include_router(accounts_router)
api_router.include_router(market_router)
api_router.include_router(market_data_router)
api_router.include_router(trading_router)
api_router.include_router(strategies_router)
api_router.include_router(backtest_router)
api_router.include_router(portfolio_router)
api_router.include_router(runtime_router)
api_router.include_router(alerts_router)
api_router.include_router(assistant_router)
api_router.include_router(brokers_router)
api_router.include_router(risk_router)
api_router.include_router(system_router)
api_router.include_router(data_router)
api_router.include_router(cn_router)
api_router.include_router(cn_data_router)


@api_router.get("/meta")
def meta():
    return {
        "modules": [
            "accounts",
            "market",
            "trading",
            "strategies",
            "backtest",
            "portfolio",
            "runtime",
            "assistant",
            "brokers",
            "risk",
            "system",
            "data",
            "cn",
        ],
        "capabilities": {
            "paper_trading": True,
            "live_trading": True,
            "backtest": True,
            "auto_trading": True,
            "assistant": True,
            "markets": ["crypto", "cn_stock"],
            "cn_rules": {
                "t_plus_one": True,
                "price_limits": True,
                "lot_size": 100,
                "session": "09:30-11:30, 13:00-15:00",
            },
            "live_gateways": ["binance", "ths"],
        },
    }
