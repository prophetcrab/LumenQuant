"""Backtest orchestration: validate inputs, run, persist, expose results."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.timeutil import iso
from app.modules.backtest.benchmark import compare, equity_option_points
from app.modules.backtest.engine import BacktestConfig, BacktestEngine
from app.modules.backtest.models import BacktestRun
from app.modules.cn.fees import CnFeeSchedule
from app.modules.market.service import MarketService, SUPPORTED_TIMEFRAMES
from app.modules.strategies.registry import get


class BacktestService:
    def __init__(
        self,
        session: Session,
        market: MarketService,
        *,
        fee_bps: float = 10.0,
        slippage_bps: float = 3.0,
        cn_fee_schedule: CnFeeSchedule | None = None,
    ) -> None:
        self.session = session
        self.market = market
        self.default_fee_bps = fee_bps
        self.default_slippage_bps = slippage_bps
        self.cn_fees = cn_fee_schedule or CnFeeSchedule()

    def run(
        self,
        *,
        strategy_key: str,
        symbol: str,
        timeframe: str,
        bars: int,
        params: dict | None = None,
        config: dict | None = None,
    ) -> dict:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValidationError(f"unsupported timeframe {timeframe}")
        if bars < 50:
            raise ValidationError("at least 50 bars are required")
        if bars > 20_000:
            raise ValidationError("bars must be <= 20000")
        strategy = get(strategy_key)
        try:
            parsed = strategy.validate_params(params)
        except Exception as exc:
            raise ValidationError(f"invalid strategy parameters: {exc}") from exc
        symbol_view = self.market.require_symbol(symbol)
        raw_config = dict(config or {})
        raw_config.setdefault("fee_bps", self.default_fee_bps)
        raw_config.setdefault("slippage_bps", self.default_slippage_bps)
        # Symbol rules always win over caller-supplied overrides, otherwise a
        # client could "backtest" A-shares without T+1 or price limits.
        raw_config["market"] = symbol_view.market
        raw_config["tick_size"] = symbol_view.tick_size
        if symbol_view.market == "cn_stock":
            raw_config["limit_pct"] = symbol_view.limit_pct
            raw_config["lot_size"] = 100.0
            raw_config["t_plus_one"] = True
        cfg = BacktestConfig.from_dict(raw_config)

        # closed_only: an in-progress daily bar keeps moving, so including it would
        # make the same backtest return different results depending on when it ran.
        candles = self.market.candles(
            symbol, timeframe=timeframe, limit=bars, closed_only=True
        )
        required = strategy.warmup(parsed) + 3
        if len(candles) < required:
            # Explain *why* so the user knows which knob to turn. Strategies with a
            # long-term regime filter need a year of data just to warm up.
            hints = []
            params_dump = parsed.model_dump()
            if params_dump.get("require_regime"):
                period = params_dump.get("regime_period")
                hints.append(
                    f"该策略启用了牛熊过滤（{period} 日均线），可增大 K 线数量、"
                    f"或设置 require_regime=false 关闭过滤"
                )
            if params_dump.get("slow"):
                hints.append(f"慢线周期 {params_dump['slow']}")
            suffix = "；".join(hints)
            raise ValidationError(
                f"数据不足：仅有 {len(candles)} 根 K 线，需要至少 {required} 根。"
                + (f"（{suffix}）" if suffix else "")
            )
        started = time.perf_counter()
        try:
            result = BacktestEngine(cfg, cn_fees=self.cn_fees).run(
                strategy, parsed, candles, timeframe
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        duration_ms = (time.perf_counter() - started) * 1000

        buy_hold = (
            (candles[-1].close / candles[0].close - 1.0) * 100 if candles[0].close else 0.0
        )
        # Risk-adjusted comparison against buy-and-hold on the same series.
        benchmark = compare(
            strategy_equity=[point["equity"] for point in result.equity_curve],
            prices=[bar.close for bar in candles],
            periods_per_year=float(result.metrics.get("periods_per_year", 252) or 252),
            benchmark_name="买入持有",
        )
        comparison_points = equity_option_points(
            [point["equity"] for point in result.equity_curve],
            [bar.close for bar in candles],
            [bar.time for bar in candles],
        )
        run = BacktestRun(
            strategy=strategy_key,
            symbol=symbol.upper(),
            timeframe=timeframe,
            params_json=json.dumps(parsed.model_dump()),
            config_json=json.dumps(asdict(cfg)),
            metrics_json=json.dumps(
                {
                    **result.metrics,
                    "buy_hold_pct": round(buy_hold, 4),
                    "benchmark": benchmark.as_dict(),
                }
            ),
            equity_json=json.dumps(result.equity_curve),
            comparison_json=json.dumps(comparison_points),
            trades_json=json.dumps([t.as_dict() for t in result.trades]),
            initial_capital=cfg.initial_capital,
            final_equity=result.metrics["final_equity"],
            bars=len(candles),
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return self._view(run, include_series=True)

    def list(self, *, limit: int = 30) -> list[dict]:
        # Basket sleeves live here too (so portfolio analysis can use them), but
        # they belong to their parent run, not to the single-symbol history.
        rows = self.session.scalars(
            select(BacktestRun)
            .where(BacktestRun.origin != "multi")
            .order_by(BacktestRun.started_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        return [self._view(row, include_series=False) for row in rows]

    def get(self, run_id: str) -> dict:
        row = self.session.get(BacktestRun, run_id)
        if row is None:
            raise NotFoundError(f"backtest {run_id} not found")
        return self._view(row, include_series=True)

    def delete(self, run_id: str) -> None:
        row = self.session.get(BacktestRun, run_id)
        if row is None:
            raise NotFoundError(f"backtest {run_id} not found")
        self.session.delete(row)
        self.session.commit()

    def _view(self, row: BacktestRun, *, include_series: bool) -> dict:
        payload = {
            "id": row.id,
            "strategy": row.strategy,
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            # Explicit currency instead of letting the UI infer it from the symbol
            # suffix. A new market (e.g. .HK) would otherwise silently be labelled
            # USDT, and A-shares were already rendering "$" in chart tooltips.
            "currency": "CNY" if row.symbol.endswith((".SH", ".SZ", ".BJ")) else "USDT",
            "params": json.loads(row.params_json),
            "config": json.loads(row.config_json),
            "metrics": json.loads(row.metrics_json),
            "initial_capital": row.initial_capital,
            "final_equity": row.final_equity,
            "bars": row.bars,
            "started_at": iso(row.started_at),
            "duration_ms": round(row.duration_ms, 2),
        }
        if include_series:
            payload["equity"] = json.loads(row.equity_json)
            payload["trades"] = json.loads(row.trades_json)
            # Older runs were stored before the comparison series existed.
            payload["comparison"] = json.loads(getattr(row, "comparison_json", "[]") or "[]")
        return payload
