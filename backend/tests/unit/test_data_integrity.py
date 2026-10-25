"""Data integrity fixes that had to precede any A-share strategy research.

Three defects made the earlier numbers untrustworthy:

* the backtester hard-coded a 10% limit band, so 创业板/科创板 moves were wrongly
  blocked;
* daily bars were unadjusted, so a split looked like a -67% crash and poisoned
  every indicator for weeks;
* a backtest could only look at "the newest N bars", so no study could hold out
  a later period.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.db import Database
from app.modules.backtest.engine import BacktestConfig, BacktestEngine
from app.modules.cn.fees import CnFeeSchedule
from app.modules.cn.sources.tencent import TencentSource
from app.modules.market.models import Candle, Symbol
from app.modules.market.service import MarketService


# --------------------------------------------------------- price-limit bands
@pytest.mark.parametrize(
    "pct,label",
    [(0.10, "main"), (0.20, "gem/star"), (0.30, "bse"), (0.05, "ST")],
)
def test_backtest_band_uses_the_symbols_own_limit(pct, label):
    """The engine hard-coded ("main", False), so every 20% board used a 10% band.

    A legitimate 15% move on 创业板 was then read as "limit-up, no counterparty"
    and the order was silently blocked — measured on real data as six blocked
    orders for 300017.SZ.
    """
    cfg = BacktestConfig(market="cn_stock", limit_pct=pct, tick_size=0.01)
    band = BacktestEngine(cfg, cn_fees=CnFeeSchedule())._band(100.0)
    assert band is not None, label
    assert band.up == pytest.approx(100.0 * (1 + pct))
    assert band.down == pytest.approx(100.0 * (1 - pct))
    assert band.pct == pytest.approx(pct)


def test_a_move_inside_the_20_percent_band_is_not_treated_as_limit_up():
    """Sanity: 15% up on a 20% board must remain tradable."""
    cfg = BacktestConfig(market="cn_stock", limit_pct=0.20, tick_size=0.01)
    band = BacktestEngine(cfg, cn_fees=CnFeeSchedule())._band(100.0)
    assert band is not None
    assert 115.0 < band.up  # 115 is inside the band


def test_no_band_for_crypto_or_zero_limit():
    assert BacktestEngine(BacktestConfig(market="crypto"))._band(100.0) is None
    assert (
        BacktestEngine(
            BacktestConfig(market="cn_stock", limit_pct=0.0)
        )._band(100.0)
        is None
    )


# ------------------------------------------------------------- ex-rights gaps
def test_adjusted_series_removes_the_split_jump():
    """002594.SZ split on 2025-07-29: raw -66.9%, adjusted ~+0.4%.

    Raw and adjusted must agree on every other day, otherwise the adjustment is
    doing something other than rebasing history.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        ticker = "sz002594"
        rows = [
            ["2025-07-28", "337.00", "337.00", "338.00", "336.00", "1000"],
            ["2025-07-29", "111.42", "111.42", "112.00", "110.00", "1000"],
            ["2025-07-30", "108.70", "108.70", "109.00", "108.00", "1000"],
        ]
        if "fqkline" in url:
            # Same days, rebased so the latest price matches reality.
            rows = [
                ["2025-07-28", "110.65", "110.65", "110.98", "110.30", "1000"],
                ["2025-07-29", "111.06", "111.06", "111.64", "109.63", "1000"],
                ["2025-07-30", "108.34", "108.34", "108.64", "107.64", "1000"],
            ]
            return httpx.Response(200, json={"data": {ticker: {"qfqday": rows}}})
        return httpx.Response(200, json={"data": {ticker: {"day": rows}}})

    src = TencentSource(timeout=5, transport=httpx.MockTransport(handler))
    raw = src.daily_history("002594.SZ", start="2025-07-01", end="2025-08-01")
    adj = src.adjusted_history("002594.SZ", start="2025-07-01", end="2025-08-01")

    raw_jump = raw.bars[1].close / raw.bars[0].close - 1
    adj_jump = adj.bars[1].close / adj.bars[0].close - 1
    assert raw_jump < -0.60, "the mock's raw series must show the split"
    assert abs(adj_jump) < 0.05, "the adjusted series must not"

    # Every non-split day must agree to within rounding of a 2-decimal price.
    raw_r2 = raw.bars[2].close / raw.bars[1].close
    adj_r2 = adj.bars[2].close / adj.bars[1].close
    assert raw_r2 == pytest.approx(adj_r2, rel=1e-3)


def test_daily_paging_walks_backwards_until_the_start_date():
    """`start` alone is ignored upstream; paging must move `end` backwards."""
    from datetime import date, timedelta

    from app.modules.cn.sources.tencent import RANGED_PAGE_BARS

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        params = url.split("param=")[1].split(",")
        end = date.fromisoformat(params[3])
        ticker = params[0]
        calls.append(params[3])
        # A full page is what tells the source there may be more history.
        rows = []
        for offset in range(RANGED_PAGE_BARS):
            day = end - timedelta(days=offset)
            rows.append([day.isoformat(), "10", "10", "10.5", "9.5", "1000"])
        return httpx.Response(200, json={"data": {ticker: {"day": rows}}})

    src = TencentSource(timeout=5, transport=httpx.MockTransport(handler))
    # Start far enough back that one 640-row page cannot reach it.
    batch = src.daily_history("600519.SH", start="2020-01-01", end="2026-10-10")
    # It must have paged at least twice, each time stepping backwards.
    assert len(calls) >= 2
    assert calls[0] > calls[1]
    stamps = [b.time for b in batch.bars]
    assert stamps == sorted(stamps)
    assert len(stamps) == len(set(stamps)), "paging must not duplicate bars"


# ------------------------------------------------------------ window slicing
def _seed(tmp_path) -> Database:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'slice.db').as_posix()}",
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
        for i in range(100):
            session.add(
                Candle(
                    symbol="600000.SH", timeframe="1d",
                    open_time=1_700_000_000_000 + i * 86_400_000,
                    open=10.0, high=10.5, low=9.5, close=10.0 + i * 0.01,
                    volume=1000.0,
                )
            )
        session.commit()
    return db


def test_candles_can_be_scoped_by_end_time(tmp_path):
    """Without `end_ms` a study can only see "the newest N bars"."""
    db = _seed(tmp_path)
    with db.session() as session:
        market = MarketService(session)
        full = market.candles("600000.SH", timeframe="1d", limit=1000)
        assert len(full) == 100
        cutoff = full[49].time
        part = market.candles("600000.SH", timeframe="1d", limit=1000, end_ms=cutoff)
        assert len(part) == 50, "end_ms must clamp the upper bound"
        assert part[-1].time == cutoff
        # And the trailing window from that instant.
        tail = market.candles("600000.SH", timeframe="1d", limit=10, end_ms=cutoff)
        assert len(tail) == 10
        assert tail[-1].time == cutoff
    db.dispose()


# --------------------------------------------------------------- ST metadata
def test_add_symbols_keeps_the_st_flag_and_name():
    """Registering lost both, so an ST stock got a 20%/10% band instead of 5%."""
    from app.modules.screener.router import _board_of  # noqa: F401
    from app.modules.screener.schemas import AddBatchRequest
    from app.modules.cn.rules import limit_pct

    payload = AddBatchRequest(
        items=[{"code": "301117.SZ", "name": "ST佳缘", "is_st": True}],
        sync=False,
    )
    items = payload.normalised_items()
    assert len(items) == 1
    assert items[0].name == "ST佳缘"
    assert items[0].is_st is True
    # An ST flag on the 创业板 must resolve to 5%, not the board's 20%.
    assert limit_pct(_board_of("301117.SZ"), True) == pytest.approx(0.05)
    assert limit_pct(_board_of("301117.SZ"), False) == pytest.approx(0.20)


def test_add_symbols_still_accepts_bare_codes():
    """The legacy string form must keep working."""
    from app.modules.screener.schemas import AddBatchRequest

    payload = AddBatchRequest(codes=["600000.SH", "600015.SH"], sync=False)
    items = payload.normalised_items()
    assert [item.code for item in items] == ["600000.SH", "600015.SH"]
    assert all(item.is_st is False for item in items)


# --------------------------------------------------------- position strength
def test_signal_strength_scales_the_position():
    """`Signal.strength` was discarded, so every entry used the full budget.

    Without it a strategy cannot express volatility targeting at all.
    """
    from app.modules.market.service_types import CandlePoint
    from app.modules.strategies.base import Signal, Strategy, StrategyParams, StrategyState

    class HalfSize(Strategy):
        key = "test_half"
        name = "half"
        category = "test"

        class Params(StrategyParams):
            pass

        def prepare(self, candles, params):
            return StrategyState(candles=candles, params=params)

        def warmup(self, params):
            return 1

        def evaluate(self, state, index):
            return Signal("buy", "half", strength=0.5)

    candles = [
        CandlePoint(
            time=1_700_000_000_000 + i * 86_400_000,
            open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0,
        )
        for i in range(10)
    ]
    cfg = BacktestConfig(initial_capital=100_000, position_pct=1.0, fee_bps=0, slippage_bps=0)
    result = BacktestEngine(cfg).run(HalfSize(), HalfSize.Params(), candles, "1d")
    # Half of 100k at 100/share = 500 shares.
    assert result.trades
    assert result.trades[0].quantity == pytest.approx(500.0, rel=1e-6)
