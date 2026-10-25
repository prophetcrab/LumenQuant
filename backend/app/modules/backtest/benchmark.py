"""Benchmark comparison: is the strategy actually beating the market, or just beta?

A strategy that returns +20% in a market that returned +25% destroyed value. Raw
return comparison misses this. This module computes, from the same price series:

* **beta** — how much of the strategy's moves are explained by the benchmark;
* **alpha** — return not explained by beta (annualised);
* **information ratio** — excess return per unit of tracking error;
* **up/down capture** — behaviour in rising vs falling markets;
* **excess win rate** — how often the strategy beat the benchmark day-to-day.

The benchmark is buy-and-hold on the same symbol, which is the only fair
comparison when no index series is available.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.money import safe_div


@dataclass(frozen=True)
class BenchmarkComparison:
    benchmark: str
    periods: int = 0
    strategy_return_pct: float = 0.0
    benchmark_return_pct: float = 0.0
    excess_return_pct: float = 0.0
    # Annualised figures are None when the sample is too short to annualise.
    alpha_pct: float | None = 0.0
    beta: float = 0.0
    correlation: float = 0.0
    tracking_error_pct: float | None = 0.0
    information_ratio: float | None = 0.0
    up_capture_pct: float = 0.0
    down_capture_pct: float = 0.0
    excess_win_rate_pct: float = 0.0
    strategy_vol_pct: float | None = 0.0
    benchmark_vol_pct: float | None = 0.0
    verdict: str = ""

    def as_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "periods": self.periods,
            "strategy_return_pct": round(self.strategy_return_pct, 4),
            "benchmark_return_pct": round(self.benchmark_return_pct, 4),
            "excess_return_pct": round(self.excess_return_pct, 4),
            "alpha_pct": None
            if self.alpha_pct is None
            else round(self.alpha_pct, 4),
            "beta": round(self.beta, 4),
            "correlation": round(self.correlation, 4),
            "tracking_error_pct": None
            if self.tracking_error_pct is None
            else round(self.tracking_error_pct, 4),
            "information_ratio": None
            if self.information_ratio is None
            else round(self.information_ratio, 4),
            "up_capture_pct": round(self.up_capture_pct, 4),
            "down_capture_pct": round(self.down_capture_pct, 4),
            "excess_win_rate_pct": round(self.excess_win_rate_pct, 4),
            "strategy_vol_pct": None
            if self.strategy_vol_pct is None
            else round(self.strategy_vol_pct, 4),
            "benchmark_vol_pct": None
            if self.benchmark_vol_pct is None
            else round(self.benchmark_vol_pct, 4),
            "verdict": self.verdict,
        }


def _returns(values: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        out.append((values[i] - prev) / prev if prev else 0.0)
    return out


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _covariance(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    mean_a = _mean(a[:n])
    mean_b = _mean(b[:n])
    return sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / (n - 1)


def compare(
    *,
    strategy_equity: list[float],
    prices: list[float],
    periods_per_year: float,
    benchmark_name: str = "买入持有",
    annualise: bool = True,
) -> BenchmarkComparison:
    """Compare an equity curve against buy-and-hold on the same prices.

    Both series must be aligned bar-by-bar. The equity curve includes cash, so
    its returns are already the strategy's realised exposure — no adjustment needed.

    `annualise=False` withholds the annualised fields (alpha, information ratio,
    tracking error, volatility) while still reporting the raw returns. Two weeks
    of 15m bars scaled to a year produce alpha in the thousands of percent, which
    reads as a result rather than as the artefact it is.
    """
    if len(strategy_equity) < 3 or len(prices) < 3:
        return BenchmarkComparison(benchmark=benchmark_name, verdict="样本不足")
    n = min(len(strategy_equity), len(prices))
    equity = strategy_equity[:n]
    price_series = prices[:n]

    strat_returns = _returns(equity)
    bench_returns = _returns(price_series)
    if not strat_returns or not bench_returns:
        return BenchmarkComparison(benchmark=benchmark_name, verdict="样本不足")

    strategy_total = safe_div(equity[-1] - equity[0], equity[0])
    benchmark_total = safe_div(price_series[-1] - price_series[0], price_series[0])
    excess = strategy_total - benchmark_total

    var_bench = _stdev(bench_returns) ** 2
    beta = safe_div(_covariance(strat_returns, bench_returns), var_bench) if var_bench else 0.0
    mean_strat = _mean(strat_returns)
    mean_bench = _mean(bench_returns)
    # Jensen's alpha, annualised: excess mean return minus beta times benchmark mean.
    alpha_per_bar = mean_strat - beta * mean_bench
    alpha = alpha_per_bar * periods_per_year

    strat_vol = _stdev(strat_returns)
    bench_vol = _stdev(bench_returns)
    std_strat = strat_vol
    std_bench = bench_vol
    correlation = (
        safe_div(_covariance(strat_returns, bench_returns), std_strat * std_bench)
        if std_strat and std_bench
        else 0.0
    )

    diffs = [s - b for s, b in zip(strat_returns, bench_returns)]
    tracking_error = _stdev(diffs)
    information_ratio = (
        safe_div(_mean(diffs), tracking_error) * math.sqrt(periods_per_year)
        if tracking_error
        else 0.0
    )

    # Capture ratios: how much of the benchmark's up/down moves the strategy took.
    up_bench = [b for b in bench_returns if b > 0]
    down_bench = [b for b in bench_returns if b < 0]
    up_indices = [i for i, b in enumerate(bench_returns) if b > 0]
    down_indices = [i for i, b in enumerate(bench_returns) if b < 0]
    up_strategy = _mean([strat_returns[i] for i in up_indices]) if up_indices else 0.0
    down_strategy = _mean([strat_returns[i] for i in down_indices]) if down_indices else 0.0
    up_capture = safe_div(up_strategy, _mean(up_bench)) * 100 if up_bench and _mean(up_bench) else 0.0
    down_capture = (
        safe_div(down_strategy, _mean(down_bench)) * 100 if down_bench and _mean(down_bench) else 0.0
    )

    excess_wins = sum(1 for d in diffs if d > 0)
    excess_win_rate = safe_div(excess_wins, len(diffs)) * 100

    def annual(value: float) -> float | None:
        """Withhold the figure when the sample cannot support annualising.

        `None` rather than 0: zero is a real reading (no excess return, no
        volatility), and showing it would be a different kind of lie.
        """
        return value if annualise else None

    return BenchmarkComparison(
        benchmark=benchmark_name,
        periods=len(diffs),
        strategy_return_pct=strategy_total * 100,
        benchmark_return_pct=benchmark_total * 100,
        excess_return_pct=excess * 100,
        alpha_pct=annual(alpha * 100),
        beta=beta,
        correlation=correlation,
        tracking_error_pct=annual(tracking_error * math.sqrt(periods_per_year) * 100),
        information_ratio=annual(information_ratio),
        up_capture_pct=up_capture,
        down_capture_pct=down_capture,
        excess_win_rate_pct=excess_win_rate,
        strategy_vol_pct=annual(strat_vol * math.sqrt(periods_per_year) * 100),
        benchmark_vol_pct=annual(bench_vol * math.sqrt(periods_per_year) * 100),
        # Both arguments are fractions at this point; the verdict thresholds are
        # expressed in percent, so scale before classifying.
        verdict=_verdict(excess * 100, alpha * 100, beta, information_ratio),
    )


def _verdict(excess: float, alpha: float, beta: float, information_ratio: float) -> str:
    """Plain-language summary so the numbers are not read in isolation.

    Excess return is the primary signal: a strategy that returned less than simply
    holding the asset lost money relative to the alternative, regardless of what
    alpha says (alpha can be positive while the strategy underperforms because
    beta scaling distorts the comparison).
    """
    # Material underperformance: report it plainly, even if alpha looks positive.
    if excess <= -5:
        if alpha > 0:
            return (
                f"跑输基准 {abs(excess):.1f}%：alpha为正但仓位暴露不足，"
                "持有期间错过大部分上涨"
            )
        return "跑输基准且alpha为负，策略未创造价值"
    if excess >= 5:
        if abs(beta) < 0.2:
            # Near-zero beta means the strategy was essentially never in the
            # market. Beating a falling benchmark that way is not skill, and the
            # old wording ("超额主要来自更高暴露") said the exact opposite.
            return (
                "几乎全程空仓：超额来自回避下跌，不是选股或择时能力；"
                "请先确认策略是否真的产生过交易"
            )
        if alpha > 0 and information_ratio > 0.3:
            return "跑赢基准且alpha为正，超额收益相对稳定"
        if alpha <= 0:
            return "收益高于基准但alpha为负：超额主要来自更高暴露，不是选股或择时能力"
        return "跑赢基准，但信息比率偏低，超额收益不稳定"
    # Within ±5% is indistinguishable from noise on typical samples.
    if abs(beta) < 0.2:
        return "仓位暴露很低，收益接近现金，与基准不可比"
    return "与基准差异不显著（±5% 以内），需更长样本判断"


def equity_option_points(equity: list[float], prices: list[float], times: list[int]) -> list[dict]:
    """Rebase both series to 100 for a comparable overlay chart."""
    if not equity or not prices:
        return []
    eq0 = equity[0] or 1.0
    px0 = prices[0] or 1.0
    n = min(len(equity), len(prices), len(times))
    return [
        {
            "time": times[i],
            "strategy": round(equity[i] / eq0 * 100, 4),
            "benchmark": round(prices[i] / px0 * 100, 4),
        }
        for i in range(n)
    ]
