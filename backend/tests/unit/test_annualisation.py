"""Annualised metrics need enough history to mean anything.

Compounding a fortnight of returns up to a year is arithmetic, not a forecast:
1500 15m bars is 15.6 days, and +187% over that span extrapolates to ~5e13 % a
year. Every ratio built on the annualised return (Sharpe, Sortino, Calmar, alpha)
inherits the same factor, so the page reported a "年化收益" of 52 trillion percent.

The unannualised total return is exact and stays reported; the annualised fields
are withheld until the sample is long enough for them to describe something.
"""

from __future__ import annotations

from app.modules.backtest.benchmark import compare
from app.modules.backtest.metrics import (
    MIN_YEARS_FOR_ANNUALISED,
    compute_metrics,
    periods_per_year,
)


def _curve(initial: float, growth: float, bars: int) -> list[float]:
    """A monotonically compounding curve with the given total growth."""
    return [initial * (growth ** (i / (bars - 1))) for i in range(bars)]


def test_a_fortnight_cannot_be_annualised():
    """The exact case from the bug report: 1500 15m bars, +187%."""
    bars = 1500
    equity = _curve(100_000.0, 2.8714309, bars)
    metrics = compute_metrics(
        initial=100_000.0,
        equity=equity,
        trades=[],
        timeframe="15m",
        bars_in_market=200,
        market="crypto",
    )

    ppy = periods_per_year("15m", "crypto")
    assert metrics["sample_days"] == round(bars / ppy * 365, 2)
    assert metrics["sample_days"] < 20
    assert metrics["annualised_available"] is False

    # The annualised family is withheld...
    for key in ("cagr_pct", "volatility_pct", "sharpe", "sortino", "calmar"):
        assert metrics[key] is None, f"{key} must not be extrapolated from 16 days"
    # ...while everything that does not depend on a year stays exact.
    assert metrics["total_return_pct"] == round((2.8714309 - 1) * 100, 4)
    assert metrics["max_drawdown_pct"] == 0.0
    assert metrics["bars"] == bars


def test_the_old_formula_really_did_explode():
    """Guard the premise: without the threshold this is what was shown."""
    bars = 1500
    ppy = periods_per_year("15m", "crypto")
    years = bars / ppy
    growth = 2.8714309
    cagr = growth ** (1 / years) - 1
    assert cagr * 100 > 1e12, "the reported figure was 5e13 %, not a rounding issue"


def test_a_long_sample_still_annualises():
    """Two years of hourly bars must keep producing real annualised metrics."""
    bars = 8760 * 2
    equity = _curve(100_000.0, 1.5, bars)
    metrics = compute_metrics(
        initial=100_000.0,
        equity=equity,
        trades=[],
        timeframe="1h",
        bars_in_market=bars,
        market="crypto",
    )
    assert metrics["sample_days"] >= 365
    assert metrics["annualised_available"] is True
    assert metrics["cagr_pct"] is not None
    assert metrics["sharpe"] is not None
    assert metrics["volatility_pct"] is not None
    # 2 years, +50% total -> ~22% a year.
    assert 15 < metrics["cagr_pct"] < 30


def test_the_threshold_is_the_documented_value():
    bars_just_under = int(MIN_YEARS_FOR_ANNUALISED * periods_per_year("1d", "crypto")) - 1
    bars_just_over = int(MIN_YEARS_FOR_ANNUALISED * periods_per_year("1d", "crypto")) + 2
    under = compute_metrics(
        initial=1000.0,
        equity=_curve(1000.0, 1.2, bars_just_under),
        trades=[],
        timeframe="1d",
        bars_in_market=0,
        market="crypto",
    )
    over = compute_metrics(
        initial=1000.0,
        equity=_curve(1000.0, 1.2, bars_just_over),
        trades=[],
        timeframe="1d",
        bars_in_market=0,
        market="crypto",
    )
    assert under["annualised_available"] is False
    assert over["annualised_available"] is True


def test_a_share_sample_days_are_trading_days():
    """A-shares trade 242 days a year, so their sample length must say so."""
    bars = 968  # one year of 1h A-share bars
    metrics = compute_metrics(
        initial=100_000.0,
        equity=_curve(100_000.0, 1.1, bars),
        trades=[],
        timeframe="1h",
        bars_in_market=0,
        market="cn_stock",
    )
    assert metrics["sample_days"] == 242.0
    assert metrics["annualised_available"] is True


def test_benchmark_alpha_is_withheld_with_the_sample():
    """Alpha is annualised too; a short sample must not produce a huge number."""
    bars = 1500
    equity = _curve(100_000.0, 2.87, bars)
    prices = _curve(100.0, 1.91, bars)
    ppy = periods_per_year("15m", "crypto")

    short = compare(
        strategy_equity=equity,
        prices=prices,
        periods_per_year=ppy,
        annualise=False,
    )
    assert short.alpha_pct is None
    assert short.information_ratio is None
    assert short.tracking_error_pct is None
    # Raw comparison is unaffected: this is what the user should read instead.
    assert short.excess_return_pct > 0
    assert "alpha" not in short.verdict or "跑赢" in short.verdict

    long = compare(
        strategy_equity=equity,
        prices=prices,
        periods_per_year=ppy,
        annualise=True,
    )
    assert long.alpha_pct is not None
    assert long.information_ratio is not None


def test_benchmark_serialisation_carries_none_through():
    """`as_dict` must not call round() on a withheld figure."""
    bars = 400
    payload = compare(
        strategy_equity=_curve(100_000.0, 1.1, bars),
        prices=_curve(100.0, 1.05, bars),
        periods_per_year=periods_per_year("15m", "crypto"),
        annualise=False,
    ).as_dict()
    assert payload["alpha_pct"] is None
    assert payload["tracking_error_pct"] is None
    assert payload["information_ratio"] is None
    assert payload["strategy_vol_pct"] is None
    assert payload["benchmark_vol_pct"] is None
    # Non-annualised fields survive.
    assert isinstance(payload["excess_return_pct"], float)
    assert isinstance(payload["beta"], float)


def test_the_engine_explains_why_the_annualised_fields_are_empty():
    """Silently blank cells look like a bug; the warning says what happened."""
    import inspect

    from app.modules.backtest import engine

    source = inspect.getsource(engine.BacktestEngine.run)
    assert "不足以年化" in source
    assert "annualised_available" in source


def test_the_api_returns_nullable_annualised_fields():
    """End to end: the HTTP response must carry null, not a 5e13 number."""
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.core.db import Database
    from app.main import create_app
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'ann.db').as_posix()}",
        seed_market=True,
        cn_symbols_enabled=False,
        market_seed_bars=1500,
        market_tick_seconds=3600,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)
    with TestClient(app) as client:
        body = client.post(
            "/api/backtests",
            json={
                "strategy": "atr_channel",
                "symbol": "BTCUSDT",
                "timeframe": "15m",
                "bars": 1500,
                "params": {},
                "config": {"initial_capital": 100_000, "position_pct": 0.9},
            },
        ).json()
    metrics = body["metrics"]
    assert metrics["annualised_available"] is False
    assert metrics["cagr_pct"] is None
    assert metrics["sharpe"] is None
    # The warning is what turns an empty cell into an explanation.
    assert any("不足以年化" in w for w in metrics["warnings"])
    # And the plain return is still there.
    assert isinstance(metrics["total_return_pct"], float)
    db.dispose()


def test_a_near_flat_curve_does_not_produce_an_absurd_ratio():
    """Zero-variance equity means the ratio is undefined, not 1e14.

    A straight-line synthetic curve has a standard deviation of ~1e-17 (float
    residue), so dividing the mean by it produced a Sharpe around 1e14. Guarding
    only against exactly zero was not enough.
    """
    bars = 730
    smooth = _curve(100_000.0, 1.5, bars)
    metrics = compute_metrics(
        initial=100_000.0,
        equity=smooth,
        trades=[],
        timeframe="1d",
        bars_in_market=400,
        market="crypto",
    )
    assert metrics["annualised_available"] is True
    assert metrics["sharpe"] == 0.0
    assert metrics["sortino"] == 0.0


def test_a_realistically_volatile_curve_still_scores():
    """The floor must not swallow genuine risk-adjusted results."""
    import random

    random.seed(7)
    equity = [100_000.0]
    for _ in range(729):
        equity.append(equity[-1] * (1 + random.gauss(0.0006, 0.012)))
    metrics = compute_metrics(
        initial=100_000.0,
        equity=equity,
        trades=[],
        timeframe="1d",
        bars_in_market=600,
        market="crypto",
    )
    assert metrics["sharpe"] is not None
    assert 0 < metrics["sharpe"] < 100
    assert metrics["sortino"] is not None
    assert 0 < metrics["sortino"] < 100
