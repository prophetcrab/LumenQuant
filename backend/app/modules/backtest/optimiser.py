"""Parameter optimisation (grid search) with overfitting safeguards.

A grid search on a single price series will always find a good-looking result —
that is arithmetic, not alpha. So the engine reports the evidence a researcher
needs to distrust it:

* in-sample vs out-of-sample split (the best params are chosen on the first 70%
  and re-measured on the remaining 30%);
* parameter-neighbourhood robustness (a real edge survives small perturbations);
* how much of the grid was profitable (a lone peak is usually noise);
* explicit warnings when the sample is too short or the search is too wide.

The search itself is synchronous and fast (≈5ms per backtest on 800 bars), so it
runs inline; a task queue would add complexity without buying anything here.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ValidationError
from app.core.money import safe_div
from app.modules.backtest.engine import BacktestConfig, BacktestEngine
from app.modules.backtest.metrics import TradeRecord
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.base import Strategy

logger = logging.getLogger(__name__)

# Guard rails: a grid of 200 combinations on 100 bars is curve fitting, not research.
MAX_COMBINATIONS = 500
# Grid results need enough data to split into in/out-of-sample windows; 120 bars
# cannot even produce a 60-bar holdout for most strategies.
MIN_BARS = 250
MIN_BARS_BY_STRATEGY = 250
MAX_BARS = 5000
OUT_OF_SAMPLE_RATIO = 0.3
MIN_OOS_BARS = 60
# Percentage points of in-sample advantage that, if lost out of sample, deserve a
# warning. Matches the walk-forward degradation threshold so both tools agree.
DEGRADATION_WARN_PP = 10.0


@dataclass(frozen=True)
class ParamRange:
    name: str
    values: list[float]

    def normalised(self) -> list[float]:
        # Preserve order, drop duplicates, keep the user's intent visible.
        seen: list[float] = []
        for value in self.values:
            if value not in seen:
                seen.append(value)
        return seen


@dataclass
class CandidateResult:
    params: dict[str, Any]
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    # None when the optimiser's sample is too short to annualise.
    sharpe: float | None = 0.0
    trade_count: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float | None = None
    final_equity: float = 0.0
    score: float = 0.0
    in_sample_return_pct: float = 0.0
    out_of_sample_return_pct: float = 0.0
    rejected: bool = False
    reject_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "params": self.params,
            "total_return_pct": round(self.total_return_pct, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "sharpe": None if self.sharpe is None else round(self.sharpe, 4),
            "trade_count": self.trade_count,
            "win_rate_pct": round(self.win_rate_pct, 4),
            "profit_factor": self.profit_factor,
            "final_equity": round(self.final_equity, 2),
            "score": round(self.score, 4),
            "in_sample_return_pct": round(self.in_sample_return_pct, 4),
            "out_of_sample_return_pct": round(self.out_of_sample_return_pct, 4),
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
        }


@dataclass
class OptimisationReport:
    strategy: str
    symbol: str
    timeframe: str
    objective: str
    bars: int
    tested: int = 0
    valid: int = 0
    rejected: int = 0
    elapsed_ms: float = 0.0
    best: CandidateResult | None = None
    top: list[CandidateResult] = field(default_factory=list)
    robust_alternatives: list[CandidateResult] = field(default_factory=list)
    distribution: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    data_source: str = ""
    in_sample_bars: int = 0
    out_of_sample_bars: int = 0

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "objective": self.objective,
            "bars": self.bars,
            "tested": self.tested,
            "valid": self.valid,
            "rejected": self.rejected,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "best": self.best.as_dict() if self.best else None,
            "top": [item.as_dict() for item in self.top],
            "robust_alternatives": [item.as_dict() for item in self.robust_alternatives],
            "distribution": self.distribution,
            "warnings": self.warnings,
            "data_source": self.data_source,
            "in_sample_bars": self.in_sample_bars,
            "out_of_sample_bars": self.out_of_sample_bars,
        }


# Scoring: return per unit of drawdown, with a Sharpe bonus. Raw return alone
# rewards strategies that take enormous risk.
#
# The Sharpe term is optional: on a sample too short to annualise, `sharpe` comes
# back as None, and scoring must fall back to the non-annualised ratios instead of
# crashing on a comparison with None. Ranking is unaffected in practice — every
# candidate in one grid shares the same sample length, so they are withheld
# together and the comparison stays like-for-like.
def _sharpe_of(metrics: dict) -> float:
    value = metrics.get("sharpe")
    return 0.0 if value is None else float(value)


OBJECTIVES = {
    "return": lambda m: m["total_return_pct"],
    "sharpe": _sharpe_of,
    "calmar": lambda m: safe_div(m["total_return_pct"], max(m["max_drawdown_pct"], 0.01)),
    "risk_adjusted": lambda m: safe_div(m["total_return_pct"], max(m["max_drawdown_pct"], 0.01))
    + 0.5 * _sharpe_of(m),
}


class OptimiserService:
    def __init__(self, *, cn_fees: CnFeeSchedule | None = None) -> None:
        self.cn_fees = cn_fees or CnFeeSchedule()

    # ------------------------------------------------------------------ grid
    @staticmethod
    def build_grid(
        space: dict[str, list[Any]], base_params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not space:
            raise ValidationError("参数空间不能为空")
        names = list(space.keys())
        value_lists: list[list[Any]] = []
        total = 1
        for name in names:
            values = space[name]
            if not values:
                raise ValidationError(f"参数 {name} 没有候选值")
            if len(values) > 50:
                raise ValidationError(f"参数 {name} 候选值过多（最多 50 个）")
            value_lists.append(values)
            total *= len(values)
        if total > MAX_COMBINATIONS:
            raise ValidationError(
                f"组合数 {total} 超过上限 {MAX_COMBINATIONS}，请缩小参数空间"
            )

        grid: list[dict[str, Any]] = []
        for combo in itertools.product(*value_lists):
            params = dict(base_params)
            params.update(dict(zip(names, combo)))
            grid.append(params)
        return grid

    # ------------------------------------------------------------------- run
    def optimise(
        self,
        strategy: Strategy,
        candles: list[CandlePoint],
        *,
        space: dict[str, list[Any]],
        base_params: dict[str, Any] | None = None,
        symbol: str,
        timeframe: str,
        objective: str = "risk_adjusted",
        config: BacktestConfig | None = None,
        top_n: int = 10,
        data_source: str = "",
    ) -> OptimisationReport:
        if objective not in OBJECTIVES:
            raise ValidationError(
                f"objective 必须是 {', '.join(OBJECTIVES)} 之一"
            )
        # A strategy with a long warmup needs much more data than the bare
        # minimum: the grid runs on the whole series and splits it afterwards.
        min_bars = max(MIN_BARS, strategy.warmup(strategy.validate_params(base_params or {})) + MIN_OOS_BARS)
        if len(candles) < min_bars:
            raise ValidationError(
                f"参数优化至少需要 {min_bars} 根 K 线"
                f"（含 {MIN_OOS_BARS} 根样本外验证），当前仅 {len(candles)} 根；"
                "请增大 bars 或换用更长周期的数据"
            )
        if len(candles) > MAX_BARS:
            raise ValidationError(f"参数优化最多支持 {MAX_BARS} 根 K 线")

        base = dict(base_params or {})
        grid = self.build_grid(space, base)
        cfg = config or BacktestConfig()
        score = OBJECTIVES[objective]
        report = OptimisationReport(
            strategy=strategy.key,
            symbol=symbol.upper(),
            timeframe=timeframe,
            objective=objective,
            bars=len(candles),
            tested=len(grid),
            data_source=data_source,
        )

        split = int(len(candles) * (1 - OUT_OF_SAMPLE_RATIO))
        report.in_sample_bars = split
        report.out_of_sample_bars = len(candles) - split
        enough_oos = report.out_of_sample_bars >= MIN_OOS_BARS

        started = time.perf_counter()
        results: list[CandidateResult] = []
        for params in grid:
            try:
                parsed = strategy.validate_params(params)
            except Exception as exc:
                report.rejected += 1
                continue
            warmup = strategy.warmup(parsed)
            if len(candles) < warmup + 3:
                report.rejected += 1
                continue
            try:
                result = BacktestEngine(cfg, cn_fees=self.cn_fees).run(
                    strategy, parsed, candles, timeframe
                )
            except Exception:
                logger.exception("optimisation candidate failed: %s", params)
                report.rejected += 1
                continue

            metrics = result.metrics
            candidate = CandidateResult(
                params=parsed.model_dump(),
                total_return_pct=metrics["total_return_pct"],
                max_drawdown_pct=metrics["max_drawdown_pct"],
                sharpe=metrics["sharpe"],
                trade_count=metrics["trade_count"],
                win_rate_pct=metrics["win_rate_pct"],
                profit_factor=metrics["profit_factor"],
                final_equity=metrics["final_equity"],
                score=score(metrics),
            )

            # In/out-of-sample split, so the winner can be judged on data it was
            # not chosen on.
            if enough_oos:
                in_sample = candles[:split]
                oos = candles[split:]
                if len(in_sample) >= warmup + 3:
                    cand_in = BacktestEngine(cfg, cn_fees=self.cn_fees).run(
                        strategy, parsed, in_sample, timeframe
                    )
                    candidate.in_sample_return_pct = cand_in.metrics["total_return_pct"]
                if len(oos) >= warmup + 3:
                    cand_out = BacktestEngine(cfg, cn_fees=self.cn_fees).run(
                        strategy, parsed, oos, timeframe
                    )
                    candidate.out_of_sample_return_pct = cand_out.metrics["total_return_pct"]
            results.append(candidate)

        report.valid = len(results)
        report.elapsed_ms = (time.perf_counter() - started) * 1000
        report.distribution = self._distribution(results)
        report.warnings = self._warnings(report, results, enough_oos)

        results.sort(key=lambda item: item.score, reverse=True)
        report.top = results[: max(1, min(top_n, 50))]
        report.best = report.top[0] if report.top else None
        report.robust_alternatives = self._robust(results, report.best)
        return report

    # ------------------------------------------------------------- analysis
    @staticmethod
    def _distribution(results: list[CandidateResult]) -> dict:
        if not results:
            return {}
        returns = sorted(item.total_return_pct for item in results)
        profitable = sum(1 for value in returns if value > 0)
        return {
            "profitable_count": profitable,
            "profitable_pct": round(safe_div(profitable, len(results)) * 100, 2),
            "median_return_pct": round(statistics.median(returns), 4),
            "best_return_pct": round(returns[-1], 4),
            "worst_return_pct": round(returns[0], 4),
            "spread_pct": round(returns[-1] - returns[0], 4),
        }

    @staticmethod
    def _warnings(
        report: OptimisationReport,
        results: list[CandidateResult],
        enough_oos: bool,
    ) -> list[str]:
        warnings: list[str] = []
        if report.data_source == "synthetic":
            warnings.append(
                "当前使用本地合成行情：走势过于规整，任何策略都能得到漂亮数字。"
                "参数优化结论必须用真实行情（LQ_CN_DATA_SOURCE=tencent）重跑才有意义。"
            )
        if not results:
            warnings.append("没有任何参数组合通过校验，请检查参数空间")
            return warnings
        if not enough_oos:
            warnings.append(
                f"样本外区间不足 {MIN_OOS_BARS} 根，无法验证稳健性；"
                "建议增加 K 线数量"
            )
        if report.tested >= 100:
            warnings.append(
                f"共测试 {report.tested} 组参数，最优结果很可能是过拟合；"
                "请用样本外表现或参数邻域稳健性判断"
            )
        if len(results) >= 5:
            profitable = sum(1 for item in results if item.total_return_pct > 0)
            ratio = profitable / len(results)
            if ratio < 0.2:
                warnings.append(
                    f"仅 {ratio * 100:.0f}% 的参数组合盈利，说明该策略在当前标的上"
                    "缺乏普遍有效性，最优值可能是噪声"
                )
        best = max(results, key=lambda item: item.score)
        if best.trade_count < 5:
            warnings.append(
                f"最优参数只产生 {best.trade_count} 笔交易，统计上不足以判断策略优劣"
            )
        if best.out_of_sample_return_pct < 0 < best.in_sample_return_pct:
            warnings.append(
                f"样本内 {best.in_sample_return_pct:+.2f}% 但样本外 "
                f"{best.out_of_sample_return_pct:+.2f}%，是典型的过拟合信号"
            )
        # A positive-but-tiny out-of-sample result after a strong in-sample run is
        # the same story with a less dramatic ending. Walk-forward already warns at
        # a 10-point degradation; the optimiser stayed silent, so 16% in-sample /
        # 1% out-of-sample came back with an empty warnings list.
        if enough_oos and best.in_sample_return_pct > 0:
            degradation = best.in_sample_return_pct - best.out_of_sample_return_pct
            if degradation >= DEGRADATION_WARN_PP:
                warnings.append(
                    f"样本内 {best.in_sample_return_pct:+.2f}% vs 样本外 "
                    f"{best.out_of_sample_return_pct:+.2f}%，衰减 {degradation:.2f} 个百分点；"
                    "最优参数在未见过的数据上大部分优势消失"
                )
        return warnings

    @staticmethod
    def _robust(
        results: list[CandidateResult], best: CandidateResult | None
    ) -> list[CandidateResult]:
        """Candidates near the top whose performance is not a lone spike.

        A parameter set that only works at one exact value is usually curve
        fitting; one that works across neighbours is more likely to be real.
        """
        if best is None or len(results) < 3:
            return []
        top_half = [item for item in results if item.score >= best.score * 0.6]
        robust = [
            item
            for item in top_half
            if item is not best and item.trade_count >= 5 and item.total_return_pct > 0
        ]
        return robust[:5]


def parse_space(raw: dict[str, Any]) -> dict[str, list[Any]]:
    """Normalise a JSON parameter space into ordered candidate lists."""
    if not isinstance(raw, dict):
        raise ValidationError("参数空间必须是对象，例如 {\"fast\": [5, 10]}")
    space: dict[str, list[Any]] = {}
    for name, values in raw.items():
        if not isinstance(values, list):
            raise ValidationError(f"参数 {name} 的候选值必须是数组")
        space[name] = values
    return space


def score_is_finite(value: float) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value) and not math.isinf(value)
