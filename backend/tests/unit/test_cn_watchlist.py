"""A-share watchlist: merge three upstreams without inventing a single field.

The screen has to be honest about what it cannot show. Two failure modes this
guards against:

* a price that silently comes from a stale candle while looking live;
* a field group that is simply missing upstream, rendered as an empty column the
  reader assumes means "zero" or "not computed".
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.db import Database
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import MarketService
from app.modules.cn.watchlist import build_watchlist


class FakeScreener:
    """Stands in for ScreenerService: a whole-market snapshot with valuation."""

    class source:
        @staticmethod
        def snapshot():
            return [
                {
                    "symbol": "sh600000",
                    "code": "600000",
                    "trade": "10.50",
                    "settlement": "10.00",
                    "open": "10.10",
                    "high": "10.80",
                    "low": "9.90",
                    "volume": "1234500",
                    "amount": "13000000",
                    "turnoverratio": "1.25",
                    "per": "12.5",
                    "pb": "1.10",
                    "mktcap": "30000000",
                    "nmc": "20000000",
                }
            ]

    def universe(self, *, force: bool = False):
        from app.modules.screener.service import _normalise

        rows = [
            item
            for item in (_normalise(r) for r in self.source.snapshot())
            if item is not None
        ]
        return rows, 12.5


class FailingScreener:
    class source:
        @staticmethod
        def snapshot():
            raise RuntimeError("upstream down")

    def universe(self, *, force: bool = False):
        from app.modules.screener.sources.sina import SinaUnavailable

        raise SinaUnavailable("新浪接口超时")


def _db(tmp_path) -> Database:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'wl.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    with db.session() as session:
        session.add(
            Symbol(
                code="600000.SH", name="浦发银行", asset_class="stock",
                market="cn_stock", exchange="SH", board="main",
                base="600000", quote="CNY", tick_size=0.01, lot_size=100.0,
                initial_price=10.0, prev_close=10.0, limit_pct=0.10,
            )
        )
        # 40 daily bars: enough for MA20/RSI but not MA60.
        for i in range(40):
            price = 10.0 + i * 0.05
            session.add(
                Candle(
                    symbol="600000.SH", timeframe="1d",
                    open_time=1_700_000_000_000 + i * 86_400_000,
                    open=price, high=price + 0.1, low=price - 0.1,
                    close=price, volume=1000.0,
                )
            )
        session.commit()
    return db


def test_merges_price_valuation_and_technicals(tmp_path):
    db = _db(tmp_path)
    with db.session() as session:
        market = MarketService(session)
        result = build_watchlist(
            session=session, market=market, screener=FakeScreener(), limit=10
        )
    assert result["count"] == 1
    row = result["rows"][0]
    assert row["code"] == "600000.SH"
    # Price from the snapshot (no live quote supplied).
    assert row["price"] == pytest.approx(10.50)
    assert row["price_source"] == "snapshot"
    assert row["prev_close"] == pytest.approx(10.00)
    assert row["change_pct"] == pytest.approx(5.0)
    # Amplitude needs high/low, which the screener's normalised rows drop.
    assert row["amplitude_pct"] == pytest.approx(9.0)
    assert row["high"] == pytest.approx(10.80)
    assert row["low"] == pytest.approx(9.90)
    # Valuation slice.
    assert row["pe"] == pytest.approx(12.5)
    assert row["pb"] == pytest.approx(1.10)
    assert row["turnover_pct"] is not None
    # History slice.
    tech = row["technicals"]
    assert tech is not None
    assert tech["ma5"] > 0 and tech["ma20"] > 0
    assert tech["ma60"] is None, "only 40 bars stored, MA60 must not be faked"
    assert tech["bars"] == 40
    assert tech["atr_pct"] is not None
    db.dispose()


def test_live_quote_outranks_the_snapshot(tmp_path):
    db = _db(tmp_path)
    with db.session() as session:
        market = MarketService(session)
        result = build_watchlist(
            session=session,
            market=market,
            screener=FakeScreener(),
            quote_rows=[
                {
                    "code": "600000.SH", "price": 11.00, "prev_close": 10.00,
                    "open": 10.2, "high": 11.1, "low": 10.0,
                    "volume": 9_000_000, "age_ms": 1200, "fresh": True,
                }
            ],
            limit=10,
        )
    row = result["rows"][0]
    assert row["price"] == pytest.approx(11.00)
    assert row["price_source"] == "realtime"
    assert row["quote_fresh"] is True
    assert row["quote_age_ms"] == 1200
    assert row["change_pct"] == pytest.approx(10.0)
    db.dispose()


def test_unavailable_field_groups_are_declared_not_blank(tmp_path):
    """A missing column must carry a reason, never render as an empty cell."""
    db = _db(tmp_path)
    with db.session() as session:
        result = build_watchlist(
            session=session, market=MarketService(session),
            screener=FakeScreener(), limit=10,
        )
    coverage = result["coverage"]
    assert coverage["price"]["available"] is True
    assert coverage["valuation"]["available"] is True
    assert coverage["technicals"]["available"] is True
    # Not provided by any current upstream — declared with the reason.
    for key in ("industry", "fund_flow", "financials"):
        assert coverage[key]["available"] is False, key
        assert coverage[key]["detail"], f"{key} needs an explanation"
    db.dispose()


def test_a_broken_snapshot_still_returns_prices(tmp_path):
    """Valuation is best-effort; losing it must not blank the whole screen."""
    db = _db(tmp_path)
    with db.session() as session:
        market = MarketService(session)
        result = build_watchlist(
            session=session, market=market, screener=FailingScreener(), limit=10
        )
    assert result["count"] == 1
    row = result["rows"][0]
    # Falls back to the stored close, and says so.
    assert row["price"] > 0
    assert row["price_source"] in {"candle", "snapshot"}
    assert row["pe"] is None
    assert result["coverage"]["valuation"]["available"] is False
    assert any("估值/换手数据暂不可用" in w for w in result["warnings"])
    db.dispose()


def test_warns_when_symbols_have_no_local_history(tmp_path):
    db = _db(tmp_path)
    with db.session() as session:
        session.add(
            Symbol(
                code="600001.SH", name="无历史", asset_class="stock",
                market="cn_stock", exchange="SH", board="main",
                base="600001", quote="CNY", tick_size=0.01, lot_size=100.0,
                initial_price=5.0, prev_close=5.0, limit_pct=0.10,
            )
        )
        session.commit()
        result = build_watchlist(
            session=session, market=MarketService(session),
            screener=FakeScreener(), limit=10,
        )
    assert result["count"] == 2
    missing = [r for r in result["rows"] if r["technicals"] is None]
    assert len(missing) == 1
    assert any("没有本地日线" in w for w in result["warnings"])
    db.dispose()
