"""Run one strategy across several symbols and report the combined result.

A single-symbol backtest answers "does this work on this instrument?". The next
question is "does it work on the basket I actually hold?" — which needs the same
capital split, the same period, and an honest statement about what the combined
curve looked like.

Each symbol is backtested independently with its own slice of capital. They are
not traded as one book (no cross-symbol position sizing, no shared cash), so the
combined curve is the sum of the sleeves. That is the right model for a
long-only strategy: sizing one symbol against another's P&L would require a
portfolio optimiser, which is a different product.

Alignment: only timestamps present in **every** sleeve are summed. A-shares share
a trading calendar so this is nearly free; crypto symbols do too. If one symbol
was suspended or delisted mid-window, its sleeve simply stops contributing and
the report says how much was dropped.
"""

from __future__ import annotations

import math

from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.modules.backtest.benchmark import compare, equity_option_points
from app.modules.backtest.engine import BacktestConfig, BacktestEngine
from app.modules.backtest.metrics import compute_metrics
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service import MarketService, SUPPORTED_TIMEFRAMES
from app.modules.strategies.registry import get

MIN_SYMBOLS = 2
MAX_SYMBOLS = 12
MIN_ALIGNED_BARS = 50


class MultiSymbolBacktester:
    def __init__(
        self,
        market: MarketService,
        *,
        fee_bps: float = 10.0,
        slippage_bps: float = 3.0,
        cn_fees: CnFeeSchedule | None = None,
    ) -> None:
        self.market = market
        self.default_fee_bps = fee_bps
        self.default_slippage_bps = slippage_bps
        self.cn_fees = cn_fees or CnFeeSchedule()

    def run(
        self,
        *,
        strategy_key: str,
        symbols: list[str],
        timeframe: str,
        bars: int,
        params: dict | None = None,
        config: dict | None = None,
        weights: list[float] | None = None,
        rebalance_bars: int = 0,
    ) -> dict:
        """Run the basket, optionally restoring target weights every N bars.

        `rebalance_bars=0` means buy-and-hold the sleeves: each one compounds on
        its own allocation. A positive value snaps every sleeve back to its target
        share on that cadence, which is what a real rebalanced portfolio does and
        produces materially different risk/return.
        """
        if rebalance_bars < 0:
            raise ValidationError("rebalance_bars 必须为非负数")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValidationError(f"unsupported timeframe {timeframe}")
        if bars < 50:
            raise ValidationError("at least 50 bars are required")
        if bars > 20_000:
            raise ValidationError("bars must be <= 20000")

        codes = self._normalise(symbols)
        if len(codes) < MIN_SYMBOLS:
            raise ValidationError(f"多标的回测至少需要 {MIN_SYMBOLS} 个标的")
        if len(codes) > MAX_SYMBOLS:
            raise ValidationError(f"多标的回测最多支持 {MAX_SYMBOLS} 个标的")

        strategy = get(strategy_key)
        try:
            parsed = strategy.validate_params(params)
        except Exception as exc:
            raise ValidationError(f"invalid strategy parameters: {exc}") from exc

        views = [self.market.require_symbol(code) for code in codes]
        # Mixed markets cannot be compared: A-share and crypto sessions do not
        # share timestamps, so the intersection would silently collapse.
        markets = {view.market for view in views}
        if len(markets) > 1:
            raise ValidationError(
                "多标的回测要求标的属于同一市场，当前混用："
                + "、".join(sorted(markets))
            )
        market = next(iter(markets))
        allowed = self.market.supported_timeframes(market)
        if timeframe not in allowed:
            raise ValidationError(
                f"{market} 支持 {', '.join(allowed)}，不支持 {timeframe}"
            )

        shares = self._weights(len(codes), weights)
        base_capital = float((config or {}).get("initial_capital", 100_000.0))
        if base_capital <= 0:
            raise ValidationError("初始资金必须为正数")

        sleeves = []
        for view, share in zip(views, shares):
            allocation = round(base_capital * share, 2)
            if allocation <= 0:
                raise ValidationError("权重必须为正数")
            raw = dict(config or {})
            raw["initial_capital"] = allocation
            raw.setdefault("fee_bps", self.default_fee_bps)
            raw.setdefault("slippage_bps", self.default_slippage_bps)
            # Symbol rules always override caller-supplied config.
            raw["market"] = view.market
            raw["tick_size"] = view.tick_size
            if view.market == "cn_stock":
                raw["limit_pct"] = view.limit_pct
                raw["lot_size"] = 100.0
                raw["t_plus_one"] = True
            cfg = BacktestConfig.from_dict(raw)

            candles = self.market.candles(view.code, timeframe=timeframe, limit=bars)
            required = strategy.warmup(parsed) + 3
            if len(candles) < required:
                raise ValidationError(
                    f"{view.code} 数据不足：仅有 {len(candles)} 根 K 线，"
                    f"需要至少 {required} 根"
                )
            try:
                result = BacktestEngine(cfg, cn_fees=self.cn_fees).run(
                    strategy, parsed, candles, timeframe
                )
            except ValueError as exc:
                raise ValidationError(f"{view.code}: {exc}") from exc

            sleeves.append(
                {
                    "symbol": view.code,
                    "name": view.name,
                    "share": share,
                    "allocation": allocation,
                    "result": result,
                    "candles": candles,
                    "blocked": result.blocked,
                }
            )

        return self._assemble(
            strategy_key=strategy_key,
            timeframe=timeframe,
            market=market,
            base_capital=base_capital,
            sleeves=sleeves,
            params=parsed.model_dump(),
            rebalance_bars=rebalance_bars,
        )

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _combine(
        *,
        curves: list[dict[int, float]],
        common: list[int],
        shares: list[float],
        rebalance_bars: int,
    ) -> list[float]:
        """Sum the sleeves, optionally rebalancing to target shares periodically.

        Without rebalancing the sleeves simply compound: a sleeve that doubles has
        twice the influence by the end. Rebalancing resets each sleeve to
        `share × total`, which requires turning a per-sleeve curve into a
        per-sleeve *return stream* and applying it to a reallocated base.

        The reallocation assumes fractional shares — the alternative (respecting
        lot sizes on every rebalance) would need order-level simulation, which is
        a different engine. This is a weight study, not an execution model, and the
        response says so.
        """
        if rebalance_bars <= 0:
            return [round(sum(curve[time] for curve in curves), 2) for time in common]

        # Convert each curve into bar-over-bar returns so it can be rescaled.
        returns: list[list[float]] = [[1.0] for _ in curves]
        for index in range(1, len(common)):
            prev, cur = common[index - 1], common[index]
            for slot, curve in enumerate(curves):
                base = curve[prev]
                returns[slot].append(curve[cur] / base if base else 1.0)

        total = sum(curve[common[0]] for curve in curves)
        values = [total * share for share in shares]
        out = [round(total, 2)]
        for index in range(1, len(common)):
            for slot in range(len(curves)):
                values[slot] *= returns[slot][index]
            if index % rebalance_bars == 0:
                total = sum(values)
                values = [total * share for share in shares]
            out.append(round(sum(values), 2))
        return out

    @staticmethod
    def _count_rebalances(bars: int, rebalance_bars: int) -> int:
        if rebalance_bars <= 0:
            return 0
        return max(0, (bars - 1) // rebalance_bars)

    @staticmethod
    def _normalise(symbols: list[str]) -> list[str]:
        codes = [code.strip().upper() for code in symbols if code and code.strip()]
        if len(set(codes)) != len(codes):
            raise ValidationError("同一个标的不能重复加入")
        return codes

    @staticmethod
    def _weights(count: int, weights: list[float] | None) -> list[float]:
        if weights is None:
            return [1.0 / count] * count
        if len(weights) != count:
            raise ValidationError("权重数量必须与标的数量一致")
        if any(not math.isfinite(w) or w <= 0 for w in weights):
            raise ValidationError("权重必须为正数")
        total = sum(weights)
        return [w / total for w in weights]

    def _assemble(
        self,
        *,
        strategy_key: str,
        timeframe: str,
        market: str,
        base_capital: float,
        sleeves: list[dict],
        params: dict,
        rebalance_bars: int = 0,
    ) -> dict:
        curves = [
            {int(point["time"]): float(point["equity"]) for point in sleeve["result"].equity_curve}
            for sleeve in sleeves
        ]
        common = sorted(set.intersection(*(set(curve) for curve in curves)))
        if len(common) < MIN_ALIGNED_BARS:
            raise ValidationError(
                f"标的重叠区间不足：只有 {len(common)} 根共同 K 线，"
                f"至少需要 {MIN_ALIGNED_BARS} 根"
            )

        combined = self._combine(
            curves=curves,
            common=common,
            shares=[sleeve["share"] for sleeve in sleeves],
            rebalance_bars=rebalance_bars,
        )
        rebalances = self._count_rebalances(len(common), rebalance_bars)

        total_trades = sum(len(sleeve["result"].trades) for sleeve in sleeves)
        bars_in_market = sum(sleeve["result"].bars_in_market for sleeve in sleeves)
        # Exposure is a weighted average across sleeves: summing bars would exceed
        # the number of aligned bars when there are several symbols.
        weighted_exposure_bars = int(round(bars_in_market / len(sleeves)))

        metrics = compute_metrics(
            initial=base_capital,
            equity=combined,
            trades=[],
            timeframe=timeframe,
            bars_in_market=weighted_exposure_bars,
            market=market,
        )
        # Trade-level statistics are per sleeve; a combined number would mix
        # different instruments. Expose the total instead.
        for key in (
            "win_rate_pct",
            "profit_factor",
            "avg_trade_pct",
            "best_trade_pct",
            "worst_trade_pct",
            "avg_hold_bars",
            "calmar",
        ):
            metrics[key] = None
        metrics["trade_count"] = total_trades

        # Benchmark: equal-weight buy-and-hold of the same basket, in currency so
        # it can be compared with the strategy curve without rebasing.
        benchmarks = [
            _buy_and_hold(sleeve["candles"], common, sleeve["allocation"])
            for sleeve in sleeves
        ]
        benchmark_curve = [
            round(sum(points[i] for points in benchmarks), 2)
            for i in range(len(common))
        ]
        comparison = compare(
            strategy_equity=combined,
            prices=benchmark_curve,
            periods_per_year=float(metrics["periods_per_year"]),
            benchmark_name="等权买入持有",
        )
        buy_hold_pct = (
            round((benchmark_curve[-1] / benchmark_curve[0] - 1.0) * 100, 4)
            if benchmark_curve and benchmark_curve[0]
            else 0.0
        )
        metrics["buy_hold_pct"] = buy_hold_pct
        metrics["benchmark"] = comparison.as_dict()

        components = []
        warnings: list[str] = []
        for sleeve, curve in zip(sleeves, curves):
            aligned = [curve[time] for time in common]
            allocation = sleeve["allocation"]
            components.append(
                {
                    "symbol": sleeve["symbol"],
                    "name": sleeve["name"],
                    "share": round(sleeve["share"], 6),
                    "allocation": allocation,
                    "bars_total": len(sleeve["candles"]),
                    "bars_aligned": len(common),
                    "return_pct": round((aligned[-1] / aligned[0] - 1.0) * 100, 4)
                    if aligned[0]
                    else 0.0,
                    "trades": len(sleeve["result"].trades),
                    "blocked": sleeve["blocked"],
                }
            )
            if len(sleeve["candles"]) > len(common) * 1.5:
                warnings.append(
                    f"{sleeve['symbol']} 只有 {len(common)}/{len(sleeve['candles'])} 根"
                    "落在共同区间，其单独表现不能直接与组合结果对比"
                )
            if not sleeve["result"].trades:
                warnings.append(
                    f"{sleeve['symbol']} 在该区间没有产生交易"
                    f"（{_blocked_hint(sleeve['blocked'])}）"
                )

        if not any(sleeve["result"].trades for sleeve in sleeves):
            warnings.insert(
                0,
                "所有标的都没有产生可执行交易，请检查策略参数、K 线数量或资金设置",
            )
        if len({item["return_pct"] for item in components}) == 1:
            warnings.append("所有标的表现完全一致，可能使用了高度相关的数据")

        return {
            "strategy": strategy_key,
            "timeframe": timeframe,
            "market": market,
            "currency": "CNY" if market == "cn_stock" else "USDT",
            "params": params,
            "initial_capital": round(base_capital, 2),
            "aligned_bars": len(common),
            "rebalance_bars": rebalance_bars,
            "rebalance_count": rebalances,
            "start_time": common[0],
            "end_time": common[-1],
            "metrics": metrics,
            "equity": [
                {"time": time, "equity": equity}
                for time, equity in zip(common, combined)
            ],
            "comparison": equity_option_points(
                combined, benchmark_curve, common
            ),
            "components": components,
            "warnings": warnings,
        }


def _buy_and_hold(candles, common: list[int], allocation: float) -> list[float]:
    """Value of holding `allocation` in the symbol across the aligned window."""
    by_time = {bar.time: bar.close for bar in candles}
    first = by_time[common[0]]
    if first <= 0:
        return [allocation for _ in common]
    return [allocation * (by_time[time] / first) for time in common]


def _blocked_hint(blocked: dict) -> str:
    parts = []
    if blocked.get("limit_up"):
        parts.append(f"涨停未成交 {blocked['limit_up']} 次")
    if blocked.get("limit_down"):
        parts.append(f"跌停未成交 {blocked['limit_down']} 次")
    if blocked.get("t_plus_one"):
        parts.append(f"T+1 拦截 {blocked['t_plus_one']} 次")
    if blocked.get("insufficient_capital"):
        parts.append("资金不足一手")
    return "、".join(parts) if parts else "策略未触发"
