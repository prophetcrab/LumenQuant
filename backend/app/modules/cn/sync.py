"""Import real A-share data into the local store.

Sync is additive and safe to re-run:

* bars are upserted by (symbol, timeframe, open_time);
* daily bars are recomputed from the authoritative daily series, not aggregated
  from partial intraday data;
* `prev_close`, `limit_pct` and the trading calendar are refreshed so paper
  trading uses the real reference price.

The service never raises on upstream failure — it reports per-symbol problems so
a single delisted or mistyped code cannot break the whole run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.modules.cn.calendar import day_key
from app.modules.cn.rules import limit_pct
from app.modules.cn.sources.base import MarketDataSource, SourceUnavailable
from app.modules.market.models import Candle, Symbol

logger = logging.getLogger(__name__)


@dataclass
class SymbolSyncResult:
    code: str
    daily_bars: int = 0
    intraday_bars: int = 0
    quote_price: float = 0.0
    prev_close: float = 0.0
    error: str = ""


@dataclass
class SyncReport:
    source: str
    symbols: list[SymbolSyncResult] = field(default_factory=list)
    started_at: str = ""
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return all(not item.error for item in self.symbols) and bool(self.symbols)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "started_at": self.started_at,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "symbols": [
                {
                    "code": item.code,
                    "daily_bars": item.daily_bars,
                    "intraday_bars": item.intraday_bars,
                    "quote_price": item.quote_price,
                    "prev_close": item.prev_close,
                    "error": item.error,
                }
                for item in self.symbols
            ],
        }


class CnDataSyncService:
    def __init__(self, session: Session, source: MarketDataSource) -> None:
        self.session = session
        self.source = source

    # ------------------------------------------------------------------ sync
    def sync_symbols(
        self,
        codes: list[str] | None = None,
        *,
        daily_limit: int = 800,
        intraday_limit: int = 320,
    ) -> SyncReport:
        import time

        started = time.perf_counter()
        report = SyncReport(
            source=getattr(self.source, "name", "unknown"),
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        targets = codes or [
            row.code
            for row in self.session.scalars(
                select(Symbol).where(Symbol.market == "cn_stock", Symbol.enabled.is_(True))
            ).all()
        ]

        for code in targets:
            result = SymbolSyncResult(code=code.upper())
            try:
                self._sync_one(result, code, daily_limit=daily_limit, intraday_limit=intraday_limit)
            except SourceUnavailable as exc:
                result.error = str(exc)[:240]
                logger.warning("sync failed for %s: %s", code, exc)
            except Exception as exc:  # one bad symbol must not abort the batch
                result.error = f"{type(exc).__name__}: {exc}"[:240]
                logger.exception("unexpected sync failure for %s", code)
            report.symbols.append(result)

        report.elapsed_ms = (time.perf_counter() - started) * 1000
        imported = sum(item.daily_bars + item.intraday_bars for item in report.symbols)
        record_audit(
            self.session,
            action="cn.data_synced",
            entity="market",
            entity_id=report.source,
            detail=f'{{"symbols":{len(report.symbols)},"bars":{imported},"ok":{str(report.ok).lower()}}}',
        )
        self.session.commit()
        return report

    def _sync_one(
        self,
        result: SymbolSyncResult,
        code: str,
        *,
        daily_limit: int,
        intraday_limit: int,
    ) -> None:
        symbol = self.session.scalar(select(Symbol).where(Symbol.code == code.upper()))
        if symbol is None:
            raise SourceUnavailable(f"本地未登记标的 {code}")

        daily = self.source.daily_bars(code, limit=daily_limit)
        # Real data supersedes synthetic history. Price levels differ entirely
        # (synthetic ~1695, real ~1331 for the same code), so mixing them makes
        # change% and limit bands meaningless. A successful fetch replaces the
        # symbol's whole series rather than appending to it.
        removed = self._purge_local(symbol.code)
        if removed:
            logger.info("replaced %s local bars for %s with real data", removed, symbol.code)
        result.daily_bars = self._upsert(symbol.code, "1d", daily.bars)

        try:
            intraday = self.source.intraday_bars(code, limit=intraday_limit)
            result.intraday_bars = self._upsert(symbol.code, "15m", intraday.bars)
        except SourceUnavailable as exc:
            # Daily sync is still valuable; record the intraday gap explicitly.
            result.error = f"分钟线缺失：{exc}"[:240]

        # Refresh the reference price used for limit bands and P&L.
        # Priority: the quote's previous close (authoritative for today's band)
        # > the newest daily close > whatever was stored before.
        try:
            quote = self.source.quote(code)
            result.quote_price = quote.price
            if quote.prev_close > 0:
                symbol.prev_close = quote.prev_close
        except SourceUnavailable:
            if daily.bars:
                symbol.prev_close = daily.bars[-1].close
        if symbol.prev_close <= 0 and daily.bars:
            symbol.prev_close = daily.bars[-1].close
        symbol.limit_pct = limit_pct(symbol.board, symbol.is_st)
        result.prev_close = symbol.prev_close
        self.session.flush()

    def _purge_local(self, code: str) -> int:
        """Delete all stored bars for a symbol before importing real data.

        Synthetic and real series live at different price levels for the same
        code, and the synthetic clock can even run ahead of wall time, so neither
        time- nor price-based filtering is reliable. Replacement is the only
        unambiguous option; it runs only after the upstream fetch succeeded.
        """
        result = self.session.execute(
            delete(Candle).where(
                Candle.symbol == code,
                Candle.timeframe.in_(("1d", "15m")),
            )
        )
        self.session.flush()
        return int(result.rowcount or 0)

    def _upsert(self, code: str, timeframe: str, bars: list) -> int:
        if not bars:
            return 0
        times = [bar.time for bar in bars]
        existing = {
            ts
            for ts in self.session.scalars(
                select(Candle.open_time).where(
                    Candle.symbol == code,
                    Candle.timeframe == timeframe,
                    Candle.open_time.in_(times),
                )
            ).all()
        }
        fresh = [
            Candle(
                symbol=code,
                timeframe=timeframe,
                open_time=bar.time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in bars
            if bar.time not in existing
        ]
        if fresh:
            self.session.add_all(fresh)
            self.session.flush()
        return len(fresh)

    # -------------------------------------------------------------- maintenance
    def replace_intraday(self, code: str, bars: list) -> int:
        """Wholesale replace of a symbol's 15m series (used by full resyncs).

        Also clears 15m bars that disagree in price level with the incoming data,
        which happens when synthetic history is still present.
        """
        code = code.upper()
        if bars:
            first_real = min(bar.time for bar in bars)
            self.session.execute(
                delete(Candle).where(
                    Candle.symbol == code,
                    Candle.timeframe == "15m",
                    Candle.open_time < first_real,
                )
            )
        return self._upsert(code, "15m", bars)

    def latest_local_bar(self, code: str, timeframe: str) -> int:
        value = self.session.scalar(
            select(Candle.open_time)
            .where(Candle.symbol == code.upper(), Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
        return int(value or 0)

    def staleness_days(self, code: str) -> int:
        """Calendar days between the newest daily bar and today (CN time)."""
        latest = self.latest_local_bar(code, "1d")
        if not latest:
            return 9999
        today = day_key(int(datetime.now().timestamp() * 1000))
        return (today - day_key(latest)).days
