"""Portfolio-level analysis across several stored backtest runs.

A single backtest answers "does this strategy work on this symbol?". The next
question a user asks is "do these three sleeves actually diversify, or am I
running the same bet three times?" No individual report can answer that, so it
is computed here from the stored equity curves — no re-run, and the numbers
match exactly what the user already saw for each run.

Everything is derived from equity, so the analysis is limited to runs that
share a market and timeframe: aligning a 24/7 crypto hourly curve with an
A-share session curve would invent bars that never existed.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.modules.backtest.engine import IMPLAUSIBLE_BAR_RETURN
from app.modules.backtest.metrics import compute_metrics, periods_per_year
from app.modules.backtest.models import BacktestRun

MIN_COMPONENTS = 2
MAX_COMPONENTS = 8
MIN_ALIGNED_BARS = 50
# Above this, two sleeves are effectively the same risk; the payload says so
# instead of leaving the user to eyeball a heat-map cell.
HIGH_CORRELATION = 0.85
# A correlation shift this large means the anomalous bars were driving the
# number, not the co-movement. Below it the two estimates agree closely enough
# that reporting both would just add noise.
MATERIAL_CORRELATION_SHIFT = 0.15


@dataclass(frozen=True)
class Sleeve:
    run: BacktestRun
    weight: float


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _correlation(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_std = _std(left)
    right_std = _std(right)
    if left_std == 0 or right_std == 0:
        # A flat sleeve carries no information about co-movement; reporting 0
        # is honest, whereas dividing would produce inf/nan in the JSON.
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    covariance = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right)
    ) / (len(left) - 1)
    return max(-1.0, min(1.0, covariance / (left_std * right_std)))


def _returns(equity: list[float]) -> list[float]:
    return [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]


def _indexed_returns(equity: list[float]) -> list[tuple[int, float]]:
    """Returns paired with the index of the bar they land on.

    `_returns` drops entries whose base is non-positive, which shifts indices and
    makes it impossible to say *which* bar an anomaly came from. This variant keeps
    the bar index so a warning can name the exact timestamp.
    """
    out: list[tuple[int, float]] = []
    for index in range(1, len(equity)):
        previous = equity[index - 1]
        if previous <= 0:
            continue
        out.append((index, (equity[index] - previous) / previous))
    return out


def _implausible_bars(equity: list[float]) -> list[tuple[int, float]]:
    """Every bar whose equity move cannot come from price alone.

    Same threshold the single-run and multi-symbol engines use, so a curve that
    triggers the warning there cannot slip through here.
    """
    return [
        (index, change * 100)
        for index, change in _indexed_returns(equity)
        if abs(change) >= IMPLAUSIBLE_BAR_RETURN
    ]


class PortfolioAnalyser:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ input
    def _load(self, run_ids: list[str], weights: list[float] | None) -> list[Sleeve]:
        if len(run_ids) < MIN_COMPONENTS:
            raise ValidationError(f"组合分析至少需要 {MIN_COMPONENTS} 个回测")
        if len(run_ids) > MAX_COMPONENTS:
            raise ValidationError(f"组合分析最多支持 {MAX_COMPONENTS} 个回测")
        if len(set(run_ids)) != len(run_ids):
            raise ValidationError("同一个回测不能重复加入组合")

        rows: list[BacktestRun] = []
        for run_id in run_ids:
            row = self.session.get(BacktestRun, run_id)
            if row is None:
                raise NotFoundError(f"回测 {run_id} 不存在")
            rows.append(row)

        timeframes = {row.timeframe for row in rows}
        if len(timeframes) > 1:
            raise ValidationError(
                "组合内回测的周期必须一致，当前混用："
                + "、".join(sorted(timeframes))
                + "。不同周期的 K 线时间轴无法对齐。"
            )
        markets = {self._market(row) for row in rows}
        if len(markets) > 1:
            raise ValidationError(
                "组合内回测的市场必须一致，当前混用："
                + "、".join(sorted(markets))
                + "。跨市场（加密/A股）的交易时段不同，权益曲线无法逐根对齐。"
            )

        if weights is None:
            normalised = [1.0 / len(rows)] * len(rows)
        else:
            if len(weights) != len(rows):
                raise ValidationError("权重数量必须与回测数量一致")
            if any(not math.isfinite(w) or w <= 0 for w in weights):
                raise ValidationError("权重必须为正数")
            total = sum(weights)
            normalised = [w / total for w in weights]

        return [Sleeve(run=row, weight=w) for row, w in zip(rows, normalised)]

    @staticmethod
    def _market(row: BacktestRun) -> str:
        try:
            return json.loads(row.config_json).get("market", "crypto")
        except (ValueError, AttributeError):
            return "crypto"

    @staticmethod
    def _equity(row: BacktestRun) -> dict[int, float]:
        try:
            points = json.loads(row.equity_json)
        except ValueError as exc:
            raise ValidationError(f"回测 {row.id[:8]} 的权益曲线已损坏") from exc
        return {int(p["time"]): float(p["equity"]) for p in points if p.get("time")}

    # --------------------------------------------------------------- analysis
    def analyse(
        self,
        *,
        run_ids: list[str],
        weights: list[float] | None = None,
        initial_capital: float = 100_000.0,
    ) -> dict:
        if initial_capital <= 0:
            raise ValidationError("组合初始资金必须为正数")
        sleeves = self._load(run_ids, weights)
        curves = [self._equity(sleeve.run) for sleeve in sleeves]

        common = sorted(set.intersection(*(set(curve) for curve in curves)))
        if len(common) < MIN_ALIGNED_BARS:
            raise ValidationError(
                f"回测区间重叠不足：只有 {len(common)} 根共同 K 线，"
                f"至少需要 {MIN_ALIGNED_BARS} 根。请选择时间范围相近的回测。"
            )

        # Each sleeve is funded with its share of the portfolio capital, so the
        # combined curve is literally the sum of the sleeves — no rescaling
        # afterwards that would distort fee and position sizing.
        sleeve_payload = []
        sleeve_returns: list[list[float]] = []
        sleeve_equities: list[list[float]] = []
        sleeve_spans: list[list[tuple[int, float]]] = []
        anomalies: list[tuple[int, int, float]] = []  # (sleeve index, bar, pct)
        for sleeve_index, (sleeve, curve) in enumerate(zip(sleeves, curves)):
            allocation = initial_capital * sleeve.weight
            anchor = curve[common[0]]
            if anchor <= 0:
                raise ValidationError(
                    f"回测 {sleeve.run.id[:8]} 在重叠区间的起始权益为 0，无法归一化"
                )
            scale = allocation / anchor
            equity = [curve[time] * scale for time in common]
            sleeve_equities.append(equity)
            spans = _indexed_returns(equity)
            sleeve_spans.append(spans)
            returns = [value for _index, value in spans]
            sleeve_returns.append(returns)
            for bar_index, change_pct in _implausible_bars(equity):
                # Only the aligned curve matters: a jump outside the common window
                # cannot distort a correlation computed on it.
                anomalies.append((sleeve_index, bar_index, change_pct))
            ppy = periods_per_year(sleeve.run.timeframe, self._market(sleeve.run))
            volatility = _std(returns) * math.sqrt(ppy) * 100
            peak = equity[0]
            drawdown = 0.0
            for value in equity:
                peak = max(peak, value)
                if peak > 0:
                    drawdown = max(drawdown, (peak - value) / peak)
            total_trades = len(json.loads(sleeve.run.trades_json or "[]"))
            sleeve_payload.append(
                {
                    "run_id": sleeve.run.id,
                    "strategy": sleeve.run.strategy,
                    "symbol": sleeve.run.symbol,
                    "timeframe": sleeve.run.timeframe,
                    "params": json.loads(sleeve.run.params_json or "{}"),
                    "weight": round(sleeve.weight, 6),
                    "allocation": round(allocation, 2),
                    "bars_total": sleeve.run.bars,
                    "bars_aligned": len(common),
                    "return_pct": round((equity[-1] / allocation - 1.0) * 100, 4),
                    "volatility_pct": round(volatility, 4),
                    "max_drawdown_pct": round(drawdown * 100, 4),
                    "trade_count": total_trades,
                }
            )

        combined = [
            sum(sleeve[i] for sleeve in sleeve_equities) for i in range(len(common))
        ]
        market = self._market(sleeves[0].run)
        timeframe = sleeves[0].run.timeframe
        # Exposure is the weighted average of the sleeves'; compute_metrics only
        # needs it to derive the exposure percentage over the aligned window.
        weighted_exposure = sum(
            sleeve.weight
            * float(json.loads(sleeve.run.metrics_json).get("exposure_pct", 0.0))
            for sleeve in sleeves
        )
        metrics = compute_metrics(
            initial=initial_capital,
            equity=combined,
            trades=[],
            timeframe=timeframe,
            bars_in_market=int(round(weighted_exposure / 100 * len(common))),
            market=market,
        )
        # Trade-level statistics are meaningless when each sleeve has its own
        # bet list; leaving zeros there would read as "0% win rate".
        for key in (
            "trade_count",
            "win_rate_pct",
            "profit_factor",
            "avg_trade_pct",
            "best_trade_pct",
            "worst_trade_pct",
            "avg_hold_bars",
        ):
            metrics[key] = None
        metrics["exposure_pct"] = round(weighted_exposure, 4)

        # Two runs of the same strategy on the same symbol are legal (e.g. two
        # parameter sets) but would produce identical labels — indistinguishable
        # in the matrix. Disambiguate only when the bare label repeats.
        bare = [f"{item['strategy']} · {item['symbol']}" for item in sleeve_payload]
        seen: dict[str, int] = {}
        for label in bare:
            seen[label] = seen.get(label, 0) + 1
        labels = [
            f"{label} #{item['run_id'][:4]}" if seen[label] > 1 else label
            for label, item in zip(bare, sleeve_payload)
        ]
        matrix = [
            [round(_correlation(left, right), 4) for right in sleeve_returns]
            for left in sleeve_returns
        ]

        # A jump that only one sleeve contains makes that sleeve look
        # uncorrelated (or spuriously correlated) with everything else, because a
        # single ±95% return dominates the covariance. Recompute the matrix with
        # every bar touched by an anomaly removed from *all* sleeves, so pairing
        # stays aligned, and report it only when the estimates actually differ.
        robust = self._robust_correlation(sleeve_spans, anomalies)
        correlation_payload: dict = {"labels": labels, "matrix": matrix}
        if robust is not None:
            correlation_payload["robust"] = robust["payload"]

        combined_returns = _returns(combined)
        ppy = periods_per_year(timeframe, market)
        combined_vol = _std(combined_returns) * math.sqrt(ppy) * 100
        weighted_vol = sum(
            item["weight"] * item["volatility_pct"] for item in sleeve_payload
        )

        warnings = self._warnings(sleeve_payload, matrix, labels)
        warnings.extend(self._anomaly_warnings(anomalies, labels, common, sleeve_payload))
        if robust is not None:
            warnings.extend(robust["warnings"])
        return {
            "timeframe": timeframe,
            "market": market,
            "currency": "CNY" if market == "cn_stock" else "USDT",
            "initial_capital": round(initial_capital, 2),
            "start_time": common[0],
            "end_time": common[-1],
            "aligned_bars": len(common),
            "metrics": metrics,
            "equity": [
                {"time": time, "equity": round(value, 2)}
                for time, value in zip(common, combined)
            ],
            "components": sleeve_payload,
            "correlation": correlation_payload,
            "diversification": {
                "weighted_volatility_pct": round(weighted_vol, 4),
                "combined_volatility_pct": round(combined_vol, 4),
                "ratio": round(combined_vol / weighted_vol, 4) if weighted_vol else 0.0,
                "benefit_pct": round((1 - combined_vol / weighted_vol) * 100, 4)
                if weighted_vol
                else 0.0,
            },
            "warnings": warnings,
        }

    @staticmethod
    def _anomaly_warnings(
        anomalies: list[tuple[int, int, float]],
        labels: list[str],
        common: list[int],
        components: list[dict],
    ) -> list[str]:
        """Report each sleeve whose curve contains an impossible move.

        Deliberately loud: once a curve has a bar that cannot come from price
        alone, every statistic derived from it — correlation, volatility,
        drawdown, the diversification benefit — is suspect, and the portfolio
        numbers will not match what the user remembers from the single runs.
        """
        if not anomalies:
            return []
        from datetime import datetime, timezone

        by_sleeve: dict[int, list[tuple[int, float]]] = {}
        for sleeve_index, bar_index, change_pct in anomalies:
            by_sleeve.setdefault(sleeve_index, []).append((bar_index, change_pct))

        out: list[str] = []
        for sleeve_index, hits in sorted(by_sleeve.items()):
            worst_bar, worst_pct = max(hits, key=lambda item: abs(item[1]))
            if 0 <= worst_bar < len(common):
                when = datetime.fromtimestamp(
                    common[worst_bar] / 1000, timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
            else:
                when = "—"
            out.append(
                f"{labels[sleeve_index]} 的权益曲线有 {len(hits)} 根异常跳变"
                f"（最大 {worst_pct:+.1f}%，位于 {when}）；"
                f"相关性、波动率与分散收益都建立在这条曲线上，结论不可直接采信"
            )
        return out

    @staticmethod
    def _robust_correlation(
        spans: list[list[tuple[int, float]]],
        anomalies: list[tuple[int, int, float]],
    ) -> dict | None:
        """Correlation recomputed with every anomalous bar removed.

        All sleeves lose the same bar indices, so the return series stay aligned
        pairwise. The cleaned matrix is always offered when anomalies exist — the
        primary matrix is known to be suspect, so the user needs something to
        compare it against. Only the *shift* warning is gated on materiality, to
        avoid claiming distortion when the estimates happen to agree anyway.
        """
        if not anomalies:
            return None
        # `bar_index` is the index in the aligned equity array, i.e. the bar the
        # return lands on. Return series are indexed from 1, so drop index-1.
        drop = {bar_index - 1 for _sleeve, bar_index, _pct in anomalies}
        cleaned = [
            [value for position, (_index, value) in enumerate(span) if position not in drop]
            for span in spans
        ]
        if any(len(series) < MIN_ALIGNED_BARS - 1 for series in cleaned):
            # Removing the bad bars leaves too little data; a correlation on a
            # handful of points would be less trustworthy than the warning.
            return None

        robust_matrix = [
            [round(_correlation(left, right), 4) for right in cleaned] for left in cleaned
        ]
        original_matrix = [
            [
                round(_correlation([v for _i, v in left], [v for _i, v in right]), 4)
                for right in spans
            ]
            for left in spans
        ]

        shifted: list[str] = []
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                delta = robust_matrix[i][j] - original_matrix[i][j]
                if abs(delta) >= MATERIAL_CORRELATION_SHIFT:
                    shifted.append(
                        f"剔除异常 bar 后，第 {i + 1} 与第 {j + 1} 条曲线的相关性从 "
                        f"{original_matrix[i][j]:.2f} 变为 {robust_matrix[i][j]:.2f}"
                        f"（变化 {delta:+.2f}）"
                    )
        warnings = [
            f"已剔除 {len(drop)} 根异常 bar 重算相关性（剩 {len(cleaned[0])} 个样本），"
            "可对比两者判断原矩阵被扭曲的程度"
        ]
        if shifted:
            warnings.append("相关性被异常跳变显著扭曲：" + "；".join(shifted))
        return {
            "payload": {
                "matrix": robust_matrix,
                "dropped_bars": len(drop),
                "returns_per_sleeve": len(cleaned[0]),
            },
            "warnings": warnings,
        }

    @staticmethod
    def _warnings(
        components: list[dict], matrix: list[list[float]], labels: list[str]
    ) -> list[str]:
        warnings: list[str] = []
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                if matrix[i][j] >= HIGH_CORRELATION:
                    warnings.append(
                        f"{labels[i]} 与 {labels[j]} 相关性 {matrix[i][j]:.2f}，"
                        f"两者几乎是同一个风险源，分散效果有限"
                    )
        for item in components:
            if item["bars_aligned"] < item["bars_total"] * 0.6:
                warnings.append(
                    f"{item['strategy']} · {item['symbol']} 只有 "
                    f"{item['bars_aligned']}/{item['bars_total']} 根落在公共区间，"
                    f"其单次回测表现不能直接与组合结果对比"
                )
        if len({item["symbol"] for item in components}) == 1:
            warnings.append("组合内所有标的相同，分散只来自策略差异，不含标的风险分散")
        if len({item["strategy"] for item in components}) == 1:
            warnings.append("组合内所有策略相同，分散只来自标的风险，不含策略差异")
        return warnings
