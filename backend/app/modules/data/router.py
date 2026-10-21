from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.errors import ValidationError
from app.modules.data.service import DataService
from app.modules.market.service import BASE_TIMEFRAME


router = APIRouter(prefix="/data", tags=["data"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@router.get("/coverage")
def coverage(request: Request, session: Session = Depends(get_session)):
    cache = getattr(request.app.state, "stats_cache", None)
    if cache is None:
        return DataService(session).coverage()
    # Full-table aggregate over every symbol; 30s is fresh enough for a coverage
    # view and keeps dashboards from paying the scan on every poll.
    return cache.get_or_load("coverage", lambda: DataService(session).coverage())


@router.get("/quality")
def quality(
    request: Request,
    symbol: str,
    timeframe: str = BASE_TIMEFRAME,
    session: Session = Depends(get_session),
):
    cache = getattr(request.app.state, "stats_cache", None)
    loader = lambda: asdict(DataService(session).quality(symbol, timeframe=timeframe))
    if cache is None:
        return loader()
    return cache.get_or_load(f"quality:{symbol.upper()}:{timeframe}", loader)


@router.get("/export", response_class=PlainTextResponse)
def export_csv(
    symbol: str,
    timeframe: str = BASE_TIMEFRAME,
    limit: int = Query(default=5000, ge=1, le=100_000),
    session: Session = Depends(get_session),
):
    content = DataService(session).export_csv(symbol, timeframe=timeframe, limit=limit)
    return PlainTextResponse(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{symbol.upper()}_{timeframe}.csv"'
        },
    )


@router.post("/import", status_code=201)
async def import_csv(
    request: Request,
    symbol: str = Form(...),
    timeframe: str = Form(default=BASE_TIMEFRAME),
    replace: bool = Form(default=False),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError("file is larger than 20 MB")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("CSV must be UTF-8 encoded") from exc
    report = DataService(session).import_csv(
        symbol, timeframe=timeframe, content=content, replace=replace
    )
    # Coverage and quality are cached for 30s; without this the dashboard keeps
    # showing the pre-import numbers right after a successful upload.
    cache = getattr(request.app.state, "stats_cache", None)
    if cache is not None:
        cache.invalidate("coverage")
        cache.invalidate(f"quality:{symbol.upper()}:")
    return asdict(report)
