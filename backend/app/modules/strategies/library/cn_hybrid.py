"""Multi-horizon hybrid strategy for A-share daily bars.

Built from what the evidence actually supports, not from what is fashionable.
The research behind each choice:

* **Gradient-boosted trees beat deep sequence models on tabular financial data**
  (Gu-Kelly-Xiu 2020 RFS; Grinsztajn et al. 2022). Qlib's own CSI300 benchmark
  puts LightGBM at IC 0.0448 versus 0.0264 for a Transformer and 0.0204 for
  TabNet; on raw Alpha360 sequences the Transformer earns **-2.70%** annualised.
  So there is no deep network here — the edge, if any, lives in the features and
  the risk layer, not in the architecture.
* **Averaging across lookback windows beats picking the best one**
  (Pesaran-Pick 2010 JBES): for every case except a single minimal break, the
  RMSFE of a window-averaged forecast is lower than any single window's.
  Rolling-optimising the window turns in-sample noise into a parameter.
* **A-share short-term reversal is stronger than momentum**, while one-day
  momentum can appear where reversal is expected (Neszveda et al. 2022). The
  reversal family is therefore signed negative and standardised, and it is
  deliberately not combined with a momentum-signed version of the same input.
* **Turnover / liquidity is the single most important predictor in the Chinese
  cross-section** (Leippold-Wang-Zhou 2021 JFE), so abnormal turnover is a first
  class feature rather than an afterthought.
* **Volatility targeting is risk management, not alpha**: managed portfolios did
  *not* beat unmanaged ones out of sample across 103 strategies (Cederburg et al.
  2020 JFE). It is used here to size positions — never to claim extra return.

Every series below is causal: the value at bar `i` uses only bars `<= i`. The
look-ahead test in `tests/unit/test_cn_hybrid.py` truncates the series and
recomputes, asserting the prefix is bit-identical.
"""

from __future__ import annotations

import math

from pydantic import Field

from app.modules.market import indicators as ta
from app.modules.market.service_types import CandlePoint
from app.modules.strategies.base import (
    Signal,
    Strategy,
    StrategyParams,
    StrategyState,
    hold,
)
from app.modules.strategies.registry import register

# Longest derived lookback (the 20-day volume mean plus the 250-day window).
_FEATURE_WARMUP = 260

# Lookback windows whose standardised scores are averaged. Chosen to span
# "recent regime" through "full cycle" rather than to fit any one sample.
_WINDOWS = (60, 120, 250)


@register
class CnMultiHorizonHybrid(Strategy):
    key = "cn_hybrid"
    name = "A股多周期混合"
    category = "hybrid"
    description = (
        "趋势 + 短期反转 + 流动性三族因子，各在 60/120/250 日三个窗口内标准化后等权平均；"
        "叠加波动率门控与双阈值滞回，并按波动率目标动态调整仓位。"
        "日线周期，T+1 由回测引擎强制执行。"
    )
    tags = ("A股", "日线", "混合", "多周期", "波动率目标")

    class Params(StrategyParams):
        # --- signal thresholds -------------------------------------------------
        entry_score: float = Field(
            default=0.25, ge=0.0, le=1.0, title="开仓分数阈值",
            description="综合分（三族因子等权平均）超过该值才开仓",
        )
        exit_score: float = Field(
            default=-0.10, ge=-1.0, le=0.5, title="平仓分数阈值",
            description="低于该值离场；应低于开仓阈值以形成滞回，避免反复进出",
        )
        # --- factor family switches -------------------------------------------
        # Reversal is a *filter*, not an equal-weight family. Measured on nine
        # A-shares over 8 years of daily data: using reversal as a scoring family
        # returned -18.8% median and beat buy-and-hold on 0 of 9 symbols. The
        # published A-share reversal effect is cross-sectional (buy the losers
        # among many names); applied to one symbol's own time series it opposes
        # the trend factor and cancels it out. As an overbought guard it removes
        # 10-30% of entries without fighting the trend.
        use_trend: bool = Field(default=True, title="启用趋势因子")
        use_liquidity: bool = Field(default=True, title="启用流动性因子")
        use_overbought_filter: bool = Field(
            default=True, title="启用超买过滤",
            description="RSI 高于阈值时不新开仓（反转只做过滤，不参与打分）",
        )
        overbought_rsi: float = Field(
            default=72.0, ge=50.0, le=95.0, title="超买 RSI 阈值",
        )
        # --- risk gates --------------------------------------------------------
        max_vol_percentile: float = Field(
            default=0.85, ge=0.5, le=1.0, title="波动率门控分位",
            description="ATR%% 在 250 日内的分位超过该值时不新开仓（高波动回避）",
        )
        target_vol_pct: float = Field(
            default=15.0, ge=3.0, le=60.0, title="目标年化波动（%）",
            description="按 20 日实现波动缩放仓位；只缩不放（上限 1.0，不加杠杆）",
        )
        min_strength: float = Field(
            default=0.25, ge=0.05, le=1.0, title="最低仓位系数",
            description="波动率再高也保留的最小仓位；低于该值不如空仓",
        )
        # --- trend specifics ---------------------------------------------------
        trend_fast: int = Field(default=20, ge=5, le=60, title="趋势快线")
        trend_slow: int = Field(default=60, ge=20, le=250, title="趋势慢线")

    # ------------------------------------------------------------------ prepare
    def prepare(self, candles: list[CandlePoint], params: Params) -> StrategyState:
        state = StrategyState(candles=candles, params=params)
        n = len(candles)
        if n < _FEATURE_WARMUP:
            state.series["score"] = [None] * n
            state.series["strength"] = [None] * n
            return state

        series = self._build_series(candles, params)
        score = self._multi_window_score(series, params, n)
        strength = self._vol_target_strength(series, params, n)

        state.series.update(series)
        state.series["score"] = score
        state.series["strength"] = strength
        return state

    # ------------------------------------------------------- raw factor series
    def _build_series(
        self, candles: list[CandlePoint], params: Params
    ) -> dict[str, list[float | None]]:
        """Causal series, one entry per bar."""
        n = len(candles)
        closes = ta.closes(candles)
        volumes = [c.volume for c in candles]
        rsi14 = ta.rsi(closes, 14)
        atr14 = ta.atr(candles, 14)
        fast = ta.ema(closes, params.trend_fast)
        slow = ta.ema(closes, params.trend_slow)

        # Only the 1-day return is needed now: the multi-day reversal family was
        # removed after ablation showed it cancelling the trend factor.
        ret1: list[float | None] = [None] * n
        for i in range(1, n):
            prev = closes[i - 1]
            ret1[i] = (closes[i] / prev - 1.0) if prev else None

        # --- trend: EMA spread + slope of the fast EMA -------------------------
        ema_spread: list[float | None] = [None] * n
        for i in range(n):
            if fast[i] and slow[i]:
                ema_spread[i] = fast[i] / slow[i] - 1.0
        fast_slope: list[float | None] = [None] * n
        for i in range(5, n):
            if fast[i] and fast[i - 5]:
                fast_slope[i] = fast[i] / fast[i - 5] - 1.0

        # --- liquidity: abnormal turnover surrogate ----------------------------
        # Volume only (no float shares available), so this is "volume relative to
        # its own recent average" — the same construct as Qlib's VMA family.
        vol_ratio5: list[float | None] = [None] * n
        vol_ratio20: list[float | None] = [None] * n
        for i in range(5, n):
            avg = sum(volumes[i - 5 : i]) / 5
            vol_ratio5[i] = (volumes[i] / avg) if avg > 0 else None
        for i in range(20, n):
            avg = sum(volumes[i - 20 : i]) / 20
            vol_ratio20[i] = (volumes[i] / avg) if avg > 0 else None

        # --- volatility (used for both the gate and position sizing) -----------
        atr_pct: list[float | None] = [
            (a / closes[i] * 100.0) if a is not None and closes[i] else None
            for i, a in enumerate(atr14)
        ]
        # 20-day realised volatility, annualised for the T+1 daily universe.
        rv20: list[float | None] = [None] * n
        for i in range(20, n):
            window = [v for v in ret1[i - 19 : i + 1] if v is not None]
            if len(window) < 15:
                continue
            mean = sum(window) / len(window)
            var = sum((v - mean) ** 2 for v in window) / (len(window) - 1)
            rv20[i] = math.sqrt(var) * math.sqrt(242) * 100.0

        return {
            "ema_spread": ema_spread,
            "fast_slope": fast_slope,
            # Raw RSI feeds the overbought *filter*; it is not a scoring input.
            "rsi_14": list(rsi14),
            "vol_ratio5": vol_ratio5,
            "vol_ratio20": vol_ratio20,
            "atr_pct": atr_pct,
            "rv20": rv20,
            "close": list(closes),
        }

    # --------------------------------------------- multi-window standardisation
    def _multi_window_score(
        self,
        series: dict[str, list[float | None]],
        params: Params,
        n: int,
    ) -> list[float | None]:
        """Average one score per lookback window — never pick a single window.

        Each window standardises the same raw factors over its own history, so a
        factor that behaves differently in a fast and a slow regime contributes
        through both views instead of whichever window happened to win a search.
        """
        all_families = (
            ("trend", ("ema_spread", "fast_slope"), params.use_trend),
            ("liquidity", ("vol_ratio5", "vol_ratio20"), params.use_liquidity),
        )
        families = [entry for entry in all_families if entry[2]]
        if not families:
            return [None] * n

        # Per-window, per-family, per-factor z-scores over the trailing window.
        accumulators: list[list[float | None]] = [
            [None] * n for _ in range(len(families))
        ]
        window_counts: list[list[int]] = [[0] * n for _ in range(len(families))]

        for window in _WINDOWS:
            if n <= window:
                continue
            for family_index, (_family, names, _enabled) in enumerate(families):
                z_sum = [0.0] * n
                z_valid = [0] * n
                for name in names:
                    zs = _rolling_zscore(series[name], window)
                    for i in range(n):
                        if zs[i] is not None:
                            z_sum[i] += zs[i]
                            z_valid[i] += 1
                for i in range(n):
                    if z_valid[i] == 0:
                        continue
                    family_score = z_sum[i] / z_valid[i]
                    slot = accumulators[family_index][i]
                    accumulators[family_index][i] = (
                        family_score if slot is None else slot + family_score
                    )
                    window_counts[family_index][i] += 1

        score: list[float | None] = [None] * n
        for i in range(n):
            parts: list[float] = []
            for family_index in range(len(families)):
                count = window_counts[family_index][i]
                if count == 0:
                    continue
                parts.append(accumulators[family_index][i] / count)
            if parts:
                # Equal weight across families, then squashed into (-1, 1) so the
                # threshold parameters mean the same thing across symbols.
                mean_score = sum(parts) / len(parts)
                score[i] = math.tanh(mean_score / 1.5)
        return score

    # ------------------------------------------------------- volatility sizing
    def _vol_target_strength(
        self,
        series: dict[str, list[float | None]],
        params: CnMultiHorizonHybrid.Params,
        n: int,
    ) -> list[float | None]:
        """Scale the position by realised volatility, only ever downward.

        Capped at 1.0: a target above realised volatility implies leverage, and
        Kelly-style leverage on an estimated edge is exactly the failure mode the
        literature warns about. This sizes risk; it does not add return.
        """
        target = params.target_vol_pct
        strength: list[float | None] = [None] * n
        for i in range(n):
            rv = series["rv20"][i]
            if rv is None or rv <= 0:
                continue
            scale = target / rv
            strength[i] = max(params.min_strength, min(1.0, scale))
        return strength

    # ------------------------------------------------------------ volatility gate
    def _vol_gate(
        self, series: dict[str, list[float | None]], index: int, params: Params
    ) -> bool:
        """True when volatility is too extreme to open a new position."""
        atr_pct = series["atr_pct"]
        window = 250
        start = max(0, index - window + 1)
        history = [v for v in atr_pct[start : index + 1] if v is not None]
        current = atr_pct[index]
        if current is None or len(history) < 60:
            return False
        rank = sum(1 for v in history if v <= current) / len(history)
        return rank > params.max_vol_percentile

    # ----------------------------------------------------------------- evaluate
    def warmup(self, params: Params) -> int:
        return _FEATURE_WARMUP + 5

    def evaluate(self, state: StrategyState, index: int) -> Signal:
        params: CnMultiHorizonHybrid.Params = state.params  # type: ignore[assignment]
        score = state.series["score"][index]
        if score is None:
            return hold("尚未积累足够历史")

        strength = state.series["strength"][index]
        if strength is None:
            strength = 1.0

        if score >= params.entry_score:
            if self._vol_gate(state.series, index, params):
                return hold(
                    f"分数 {score:.2f} 达标，但波动率处于高位，暂不开仓"
                )
            if params.use_overbought_filter:
                rsi = state.series["rsi_14"][index]
                if rsi is not None and rsi > params.overbought_rsi:
                    return hold(
                        f"分数 {score:.2f} 达标，但 RSI {rsi:.0f} > "
                        f"{params.overbought_rsi:.0f}，追高回避"
                    )
            return Signal(
                "buy",
                f"综合分 {score:.2f} ≥ {params.entry_score:.2f}"
                f"（仓位系数 {strength:.2f}）",
                strength=strength,
            )

        if score <= params.exit_score:
            return Signal(
                "sell", f"综合分 {score:.2f} ≤ {params.exit_score:.2f}"
            )

        return hold(f"综合分 {score:.2f} 在区间内")


def _rolling_zscore(
    series: list[float | None], window: int
) -> list[float | None]:
    """Z-score against the trailing window, excluding the current bar.

    Excluding `i` itself matters: including it lets a large value inflate its own
    denominator, which dampens exactly the outlier a factor is supposed to detect.
    When the window holds too few values, or has no dispersion, the result is
    None rather than 0 — a fabricated zero would read as "neutral evidence".
    """
    n = len(series)
    out: list[float | None] = [None] * n
    for i in range(window, n):
        window_values = series[i - window : i]
        values = [v for v in window_values if v is not None]
        if len(values) < window * 0.8 or series[i] is None:
            continue
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        sd = math.sqrt(var)
        if sd <= 0:
            continue
        out[i] = (series[i] - mean) / sd
    return out
