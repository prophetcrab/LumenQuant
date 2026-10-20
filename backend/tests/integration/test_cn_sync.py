"""Real-data sync: import semantics and API surface."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import Database
from app.main import create_app
from app.modules.cn.sources.base import SourceUnavailable
from app.modules.cn.sources.tencent import TencentSource
from app.modules.cn.sync import CnDataSyncService
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import MarketService


def quote_payload() -> bytes:
    fields = [""] * 88
    fields[0], fields[1], fields[2] = "1", "贵州茅台", "600519"
    fields[3], fields[4], fields[5], fields[6] = "1331.88", "1324.97", "1321.01", "34136"
    fields[30], fields[33], fields[34] = "20261020150000", "1335.00", "1316.14"
    return ('v_sh600519="' + "~".join(fields) + '";').encode("gbk")


def dispatch(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "qt.gtimg.cn" in url:
        return httpx.Response(200, content=quote_payload())
    if "fqkline" in url:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "sh600519": {
                        "qfqday": [
                            ["2026-10-16", "1277.27", "1277.96", "1285.53", "1270.36", "16571"],
                            ["2026-10-20", "1321.01", "1331.88", "1335.00", "1316.14", "34136"],
                        ]
                    }
                },
            },
        )
    if "mkline" in url:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "sh600519": {
                        "m15": [
                            ["202610201445", "1322.00", "1331.00", "1331.00", "1322.00", "4652"],
                            ["202610201500", "1330.97", "1331.88", "1332.98", "1327.63", "5993"],
                        ]
                    }
                },
            },
        )
    return httpx.Response(404)


@pytest.fixture()
def cn_db(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'sync.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=True,
        cn_seed_days=10,
        market_seed_bars=120,
        runtime_enabled=False,
        cn_data_source="synthetic",
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        yield db, client
    db.dispose()


def make_source() -> TencentSource:
    return TencentSource(transport=httpx.MockTransport(dispatch))


def test_sync_imports_daily_and_intraday(cn_db):
    db, _client = cn_db
    source = make_source()
    with db.session() as session:
        # Remove seeded data for the target so import is observable.
        session.query(Candle).filter(Candle.symbol == "600519.SH").delete()
        session.commit()
        report = CnDataSyncService(session, source).sync_symbols(
            ["600519.SH"], daily_limit=2, intraday_limit=2
        )
    assert report.ok is True
    item = report.symbols[0]
    assert item.daily_bars == 2
    assert item.intraday_bars == 2
    assert item.prev_close == pytest.approx(1324.97)

    with db.session() as session:
        daily = session.scalars(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
        ).all()
        intraday = session.scalars(
            select(Candle).where(Candle.symbol == "600519.SH", Candle.timeframe == "15m")
        ).all()
        symbol = session.scalar(select(Symbol).where(Symbol.code == "600519.SH"))
    assert len(daily) == 2 and len(intraday) == 2
    assert symbol.prev_close == pytest.approx(1324.97)
    assert symbol.limit_pct == pytest.approx(0.10)


def test_sync_is_idempotent(cn_db):
    """Re-running a sync must leave exactly the upstream series, not duplicates.

    Sync replaces the stored series, so the second run re-imports the same rows
    rather than appending; what matters is that the final state is identical.
    """
    db, _ = cn_db

    def snapshot() -> tuple[int, int, list[float]]:
        with db.session() as session:
            daily = session.scalars(
                select(Candle)
                .where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
                .order_by(Candle.open_time)
            ).all()
            intraday = session.scalars(
                select(Candle)
                .where(Candle.symbol == "600519.SH", Candle.timeframe == "15m")
                .order_by(Candle.open_time)
            ).all()
            return len(daily), len(intraday), [row.close for row in daily]

    with db.session() as session:
        session.query(Candle).filter(Candle.symbol == "600519.SH").delete()
        session.commit()
        service = CnDataSyncService(session, make_source())
        service.sync_symbols(["600519.SH"], daily_limit=2, intraday_limit=2)

    first = snapshot()
    with db.session() as session:
        CnDataSyncService(session, make_source()).sync_symbols(
            ["600519.SH"], daily_limit=2, intraday_limit=2
        )
    second = snapshot()

    assert first == second
    assert first[0] == 2 and first[1] == 2


def test_sync_reports_unknown_symbol(cn_db):
    db, _ = cn_db
    with db.session() as session:
        report = CnDataSyncService(session, make_source()).sync_symbols(["999999.SH"])
    assert report.ok is False
    assert "未登记" in report.symbols[0].error


def test_sync_survives_upstream_failure(cn_db):
    db, _ = cn_db

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with db.session() as session:
        service = CnDataSyncService(session, TencentSource(transport=httpx.MockTransport(broken)))
        report = service.sync_symbols(["600519.SH"])
    assert report.ok is False
    assert "不可达" in report.symbols[0].error


def test_market_service_reads_synced_bars(cn_db):
    db, _ = cn_db
    with db.session() as session:
        session.query(Candle).filter(Candle.symbol == "600519.SH").delete()
        session.commit()
        CnDataSyncService(session, make_source()).sync_symbols(
            ["600519.SH"], daily_limit=2, intraday_limit=2
        )
        market = MarketService(session, live=None)
        daily = market.candles("600519.SH", timeframe="1d", limit=2)
    assert [round(bar.close, 2) for bar in daily] == [1277.96, 1331.88]


def test_source_endpoint_reports_synthetic(cn_db):
    _, client = cn_db
    payload = client.get("/api/cn/data/source").json()
    assert payload["configured"] == "synthetic"
    assert payload["available"] is False


def test_sync_endpoint_requires_real_source(cn_db):
    _, client = cn_db
    response = client.post("/api/cn/data/sync", params={"codes": "600519.SH"})
    assert response.status_code == 502
    assert "LQ_CN_DATA_SOURCE" in response.json()["error"]["message"]


def test_staleness_endpoint(cn_db):
    _, client = cn_db
    rows = client.get("/api/cn/data/staleness").json()
    assert rows
    assert {"code", "latest_daily", "stale_days"} <= set(rows[0])


def test_sync_replaces_synthetic_history(cn_db):
    """Real data must replace synthetic history, not append to it.

    Synthetic and real series sit at different price levels for the same code;
    mixing them produces impossible change percentages and limit bands.
    """
    db, _ = cn_db
    with db.session() as session:
        symbol = session.scalar(select(Symbol).where(Symbol.code == "600519.SH"))
        symbol.initial_price = 1680.0
        symbol.prev_close = 1680.0
        session.commit()

        CnDataSyncService(session, make_source()).sync_symbols(
            ["600519.SH"], daily_limit=2, intraday_limit=2
        )
        closes = [
            round(row.close, 2)
            for row in session.scalars(
                select(Candle)
                .where(Candle.symbol == "600519.SH", Candle.timeframe == "1d")
                .order_by(Candle.open_time)
            ).all()
        ]
        symbol = session.scalar(select(Symbol).where(Symbol.code == "600519.SH"))

    # Only the two real bars survive: 1277.96 then 1331.88.
    assert closes == [1277.96, 1331.88]
    assert symbol.prev_close == pytest.approx(1324.97)
