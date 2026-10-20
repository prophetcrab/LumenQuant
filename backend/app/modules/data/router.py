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
def coverage(session: Session = Depends(get_session)):
    return DataService(session).coverage()


@router.get("/quality")
def quality(
    symbol: str,
    timeframe: str = BASE_TIMEFRAME,
    session: Session = Depends(get_session),
):
    return asdict(DataService(session).quality(symbol, timeframe=timeframe))


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
    return asdict(report)
