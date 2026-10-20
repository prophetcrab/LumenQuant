from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import AppError
from app.core.events import EventBus


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db = database or Database(settings.database_url)
    bus = EventBus()
    market_engine = None
    if settings.seed_market:
        from app.modules.market.engine import MarketEngine

        market_engine = MarketEngine(db, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db.create_schema()
        if market_engine is not None:
            await market_engine.start()
        try:
            yield
        finally:
            if market_engine is not None:
                await market_engine.stop()
            db.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.bus = bus
    app.state.market_engine = market_engine

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health", tags=["system"])
    def health():
        try:
            with db.session() as session:
                session.execute(text("SELECT 1"))
            database_status = "ok"
        except Exception:
            database_status = "unavailable"
        return {
            "status": "ok" if database_status == "ok" else "degraded",
            "version": settings.app_version,
            "database": database_status,
            "environment": settings.environment,
        }

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
