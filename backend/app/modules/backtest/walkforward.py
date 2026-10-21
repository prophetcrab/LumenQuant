"""Walk-forward validation.

A single 70/30 split answers one question: "does the winner of this window work in
the next one?" That is one sample. Walk-forward repeats the experiment across
rolling windows and reports the *distribution* of out-of-sample results, which is
what actually distinguishes a strategy from a lucky parameter set.

Design:

    [--- train 1 ---][- test -]
              [--- train 2 ---][- test -]
                        [--- train 3 ---][- test -]

For each window: optimise on `train`, then apply the winning parameters unchanged
to the immediately following `test` slice. The concatenated test returns form the
walk-forward equity path.

The result deliberately includes the *worst* window, not just the average: a
strategy that fails badly in one regime is not robust no matter how good the mean
looks.
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
from app.modules.backtest.optimiser import (
    MAX_COMBINATIONS,
    OptimiserService,
    parse_space,
)
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.base import Strategy

logger = logging.getLogger(__name__)

MIN_TOTAL_BARS = 400
MIN_TRAIN_BARS = 200
MIN_TEST_BARS = 40
MAX_WINDOWS = 12
MIN_WINDOWS = 2


@dataclass
class WindowResult:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    params: dict[str, Any] = field(default_factory=dict)
    train_return_pct: float = 0.0
    test_return_pct: float = 0.0
    test_drawdown_pct: float = 0.0
    test_trades: int = 0
    test_sharpe: float = 0.0
    degradation_pct: float = 0.0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "params": self.params,
            "train_return_pct": round(self.train_return_pct, 4),
            "test_return_pct": round(self.test_return_pct, 4),
            "test_drawdown_pct": round(self.test_drawdown_pct, 4),
            "test_trades": self.test_trades,
            "test_sharpe": round(self.test_sharpe, 4),
            "degradation_pct": round(self.degradation_pct, 4),
        }


@dataclass
class WalkForwardReport:
    strategy: str
    symbol: str
    timeframe: str
    objective: str
    total_bars: int
    windows: list[WindowResult] = field(default_factory=list)
    aggregate: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    data_source: str = ""

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "objective": self.objective,
            "total_bars": self.total_bars,
            "windows": [item.as_dict() for item in self.windows],
            "aggregate": self.aggregate,
            "warnings": self.warnings,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "data_source": self.data_source,
        }


class WalkForwardService:
    def __init__(self, *, cn_fees: CnFeeSchedule | None = None) -> None:
        self.cn_fees = cn_fees or CnFeeSchedule()
        self.optimiser = OptimiserService(cn_fees=self.cn_fees)

    def run(
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
        windows: int = 4,
        train_bars: int = 0,
        test_bars: int = 0,
        data_source: str = "",
    ) -> WalkForwardReport:
        if len(candles) < MIN_TOTAL_BARS:
            raise ValidationError(
                f"滚动验证至少需要 {MIN_TOTAL_BARS} 根 K 线，当前 {len(candles)} 根"
            )
        if not MIN_WINDOWS <= windows <= MAX_WINDOWS:
            raise ValidationError(
                f"窗口数必须在 {MIN_WINDOWS}–{MAX_WINDOWS} 之间"
            )
        # Validate the space once, with the same rules as a single search.
        OptimiserService.build_grid(space, base_params or {})
        combos = math.prod(len(values) for values in space.values())
        if combos > MAX_COMBINATIONS:
            raise ValidationError(f"组合数 {combos} 超过上限 {MAX_COMBINATIONS}")

        total = len(candles)
        # Test windows are evaluated with the strategy as-is, so they must be long
        # enough to clear the warmup on their own — otherwise the window is
        # silently skipped and the run looks shorter than requested.
        base_for_warmup = dict(base_params or {})
        warmup = self._max_warmup(strategy, space, base_for_warmup)
        min_test = max(MIN_TEST_BARS, warmup + 10)
        if train_bars <= 0 or test_bars <= 0:
            train_bars, test_bars = self._auto_split(total, windows, min_test=min_test)
        if train_bars < MIN_TRAIN_BARS:
            raise ValidationError(
                f"训练窗口至少 {MIN_TRAIN_BARS} 根，当前 {train_bars} 根"
            )
        if test_bars < min_test:
            raise ValidationError(
                f"测试窗口至少需要 {min_test} 根"
                f"（策略暖机 {warmup} 根 + 10 根观察），当前 {test_bars} 根；"
                "请减少窗口数或使用更短周期的数据"
            )
        needed = train_bars + test_bars * windows
        # Auto-fit the window count instead of failing: a user asking for 4 windows
        # on limited data is better served by 3 windows plus an explicit note.
        requested_windows = windows
        while needed > total and windows > MIN_WINDOWS:
            windows -= 1
            train_bars, test_bars = self._auto_split(total, windows, min_test=min_test)
            needed = train_bars + test_bars * windows
        if needed > total:
            raise ValidationError(
                f"需要 {needed} 根 K 线才能完成 {windows} 个窗口"
                f"（训练 {train_bars} + 测试 {test_bars}×{windows}），当前仅 {total} 根；"
                "请减少窗口数或缩短窗口长度"
            )

        cfg = config or BacktestConfig()
        report = WalkForwardReport(
            strategy=strategy.key,
            symbol=symbol.upper(),
            timeframe=timeframe,
            objective=objective,
            total_bars=total,
            data_source=data_source,
        )
        started = time.perf_counter()

        for index in range(windows):
            train_start = index * test_bars
            train_end = train_start + train_bars
            test_start = train_end
            test_end = min(test_start + test_bars, total)
            train_slice = candles[train_start:train_end]
            test_slice = candles[test_start:test_end]
            if len(train_slice) < max(MIN_TRAIN_BARS, warmup + 10):
                break
            if len(test_slice) < min_test:
                # A short tail window is better skipped than evaluated with a
                # half-warmed strategy; the skipped count is reported at the end.
                continue

            window = WindowResult(
                index=index + 1,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
            try:
                opt = self.optimiser.optimise(
                    strategy,
                    train_slice,
                    space=space,
                    base_params=base_params,
                    symbol=symbol,
                    timeframe=timeframe,
                    objective=objective,
                    config=cfg,
                    top_n=1,
                )
            except ValidationError:
                # Not enough data in this window for the strategy's warmup.
                continue
            if opt.best is None:
                continue

            window.params = opt.best.params
            window.train_return_pct = opt.best.total_return_pct

            try:
                parsed = strategy.validate_params(window.params)
                tested = BacktestEngine(cfg, cn_fees=self.cn_fees).run(
                    strategy, parsed, test_slice, timeframe
                )
            except Exception:
                logger.exception("walk-forward test window %s failed", index)
                continue

            window.test_return_pct = tested.metrics["total_return_pct"]
            window.test_drawdown_pct = tested.metrics["max_drawdown_pct"]
            window.test_trades = tested.metrics["trade_count"]
            window.test_sharpe = tested.metrics["sharpe"]
            window.degradation_pct = window.train_return_pct - window.test_return_pct
            report.windows.append(window)

        report.elapsed_ms = (time.perf_counter() - started) * 1000
        if not report.windows:
            raise ValidationError(
                "没有窗口能完成验证：策略暖机过长或数据不足，请增大 K 线数量"
            )
        report.aggregate = self._aggregate(report.windows)
        report.aggregate["requested_windows"] = requested_windows
        report.aggregate["skipped_windows"] = requested_windows - len(report.windows)
        # Build the structural messages, then prepend them. _warnings() used to be
        # assigned over report.warnings, which discarded every message added here —
        # so "only N windows fit" and "M windows were dropped" never reached the user.
        structural: list[str] = []
        if windows < requested_windows:
            structural.append(
                f"数据只够完成 {windows} 个窗口（原请求 {requested_windows} 个），"
                "已自动缩减；如需更多窗口请增加 K 线数量"
            )
        dropped = requested_windows - len(report.windows)
        if dropped > 0 and windows == requested_windows:
            # Windows were lost after sizing succeeded — e.g. the optimiser chose
            # parameters the test slice could not warm up. Report it instead of
            # returning a shorter report that looks complete.
            structural.append(
                f"有 {dropped} 个窗口未能完成验证（样本外区间不足以暖机），"
                f"实际只验证了 {len(report.windows)}/{requested_windows} 个窗口；"
                "结论的统计意义相应减弱"
            )
        report.warnings = structural + self._warnings(report)
        return report

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _max_warmup(strategy: Strategy, space: dict[str, list[Any]], base: dict[str, Any]) -> int:
        """Longest warmup the optimiser could select, across the whole grid.

        Sizing the test window from the *first* combination is wrong: the grid may
        contain a slow moving average whose warmup is twice as long, and when the
        optimiser picks it the test window can no longer warm up. That window was
        then dropped silently, and the report looked shorter than requested.
        """
        worst = 0
        keys = [key for key, values in space.items() if values]
        if not keys:
            try:
                return strategy.warmup(strategy.validate_params(base))
            except Exception:
                return 0
        # Cartesian product, bounded by the same cap as the search itself.
        for combo in itertools.product(*(space[key] for key in keys)):
            try:
                params = strategy.validate_params({**base, **dict(zip(keys, combo))})
                worst = max(worst, strategy.warmup(params))
            except Exception:
                continue
        return worst

    @staticmethod
    def _auto_split(total: int, windows: int, *, min_test: int = MIN_TEST_BARS) -> tuple[int, int]:
        """Choose train/test sizes that fit `windows` sliding windows in `total`.

        Layout for N windows: train + test*N rows, with each later train window
        shifted by one test window. Test windows must satisfy `min_test` so they
        can run the strategy without being skipped for warmup.
        """
        min_train = max(MIN_TRAIN_BARS, min_test)
        # Layout: [train][test] [test] [test] ...  => total = train + test*windows
        # Target roughly 3:1 train:test per window, then verify the layout fits.
        test = max(min_test, total // (windows * 5))
        train = total - test * windows
        if train < min_train:
            # Shrink the test slice so training keeps its minimum.
            test = max(min_test, (total - min_train) // windows)
            train = total - test * windows
        return max(train, min_train), test

    @staticmethod
    def _aggregate(windows: list[WindowResult]) -> dict:
        test_returns = [item.test_return_pct for item in windows]
        positive = sum(1 for value in test_returns if value > 0)
        compounded = 1.0
        for value in test_returns:
            compounded *= 1 + value / 100.0
        degradations = [item.degradation_pct for item in windows]
        return {
            "windows_completed": len(windows),
            "positive_windows": positive,
            "positive_pct": round(safe_div(positive, len(windows)) * 100, 2),
            "mean_test_return_pct": round(statistics.mean(test_returns), 4),
            "median_test_return_pct": round(statistics.median(test_returns), 4),
            "worst_test_return_pct": round(min(test_returns), 4),
            "best_test_return_pct": round(max(test_returns), 4),
            "std_test_return_pct": round(
                statistics.stdev(test_returns) if len(test_returns) > 1 else 0.0, 4
            ),
            "compounded_test_return_pct": round((compounded - 1) * 100, 4),
            "mean_degradation_pct": round(statistics.mean(degradations), 4),
            "mean_test_trades": round(
                statistics.mean([item.test_trades for item in windows]), 2
            ),
            "mean_test_drawdown_pct": round(
                statistics.mean([item.test_drawdown_pct for item in windows]), 4
            ),
        }

    def _warnings(self, report: WalkForwardReport) -> list[str]:
        warnings: list[str] = []
        agg = report.aggregate
        if report.data_source == "synthetic":
            warnings.append(
                "当前使用本地合成行情：走势过于规整，滚动验证结果不能代表真实市场。"
            )
        if agg["positive_pct"] < 50:
            warnings.append(
                f"仅 {agg['positive_pct']:.0f}% 的窗口样本外为正"
                f"（{agg['positive_windows']}/{agg['windows_completed']}），"
                "策略在当前标的上不稳定"
            )
        if agg["worst_test_return_pct"] < -10:
            warnings.append(
                f"最差窗口样本外收益 {agg['worst_test_return_pct']:.2f}%，"
                "存在显著回撤风险"
            )
        if agg["mean_degradation_pct"] > 10:
            warnings.append(
                f"平均训练→测试衰减 {agg['mean_degradation_pct']:.2f}%，"
                "训练期表现被明显高估"
            )
        if agg["mean_test_trades"] < 3:
            warnings.append(
                f"平均每窗口仅 {agg['mean_test_trades']:.1f} 笔交易，统计意义不足"
            )
        if report.windows and len({tuple(sorted(w.params.items())) for w in report.windows}) == 1:
            warnings.append(
                "所有窗口选出了同一组参数，说明参数选择稳定；"
                "但也需确认该参数不是在拟合整体趋势"
            )
        return warnings


def summarise_walk_forward(report: WalkForwardReport) -> str:
    agg = report.aggregate
    return (
        f"{report.strategy} on {report.symbol} {report.timeframe}: "
        f"{agg['windows_completed']} windows, "
        f"positive {agg['positive_pct']:.0f}%, "
        f"mean test {agg['mean_test_return_pct']:+.2f}%, "
        f"worst {agg['worst_test_return_pct']:+.2f}%"
    )
