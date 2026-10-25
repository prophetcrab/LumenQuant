"""Evaluate a strategy across the whole A-share panel, and report the distribution.

Single-symbol results cannot support a conclusion. Eight years of daily bars on
one name gives 20-50 trades, and a win rate over 20 trades has a standard error
wide enough to contain any claim. What matters is the *distribution* across
hundreds of names: a strategy worth keeping beats buy-and-hold on most of them and
improves drawdown on most of them, not on the one that was cherry-picked.

Usage:
    python -m scripts.evaluate_panel --strategy cn_hybrid --limit 400
    python -m scripts.evaluate_panel --strategy cn_hybrid --since 2022-01-01
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timezone

from sqlalchemy import select

sys.path.insert(0, ".")

from app.core.config import Settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.modules.backtest.service import BacktestService  # noqa: E402
from app.modules.cn.fees import CnFeeSchedule  # noqa: E402
from app.modules.market.models import Symbol  # noqa: E402
from app.modules.market.service import MarketService  # noqa: E402
from app.modules.strategies.registry import get  # noqa: E402


def _to_ms(value: str | None) -> int | None:
    if not value:
        return None
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description="Panel evaluation")
    parser.add_argument("--strategy", default="cn_hybrid")
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--since", default="")
    parser.add_argument("--until", default="")
    parser.add_argument("--params", default="{}", help="JSON params for the strategy")
    parser.add_argument("--min-bars", type=int, default=500)
    args = parser.parse_args()

    import json

    params = json.loads(args.params)
    settings = Settings()
    db = Database(settings.database_url)
    end_ms = _to_ms(args.until)

    with db.session() as session:
        market = MarketService(session)
        service = BacktestService(
            session, market,
            fee_bps=settings.fee_bps,
            slippage_bps=settings.slippage_bps,
            cn_fee_schedule=CnFeeSchedule(),
        )
        codes = list(
            session.scalars(
                select(Symbol.code).where(
                    Symbol.market == "cn_stock", Symbol.enabled.is_(True)
                )
            ).all()
        )[: args.limit]

        strategy = get(args.strategy)
        parsed = strategy.validate_params(params)
        warmup = strategy.warmup(parsed)

        rows: list[dict] = []
        skipped: list[str] = []
        for index, code in enumerate(codes, start=1):
            try:
                candles = market.candles(
                    code, timeframe="1d", limit=20_000, closed_only=True, end_ms=end_ms
                )
            except Exception:
                skipped.append(f"{code}: 无法读取")
                continue
            if len(candles) < max(args.min_bars, warmup + 50):
                skipped.append(f"{code}: 仅 {len(candles)} 根")
                continue
            try:
                run = service.run(
                    strategy_key=args.strategy, symbol=code, timeframe="1d",
                    bars=min(len(candles), 20_000), params=params,
                    config={"initial_capital": 1_000_000, "position_pct": 0.95},
                    end_ms=end_ms,
                )
                bench = service.run(
                    strategy_key="buy_and_hold", symbol=code, timeframe="1d",
                    bars=min(len(candles), 20_000), params={},
                    config={"initial_capital": 1_000_000, "position_pct": 0.95},
                    end_ms=end_ms,
                )
            except Exception as exc:
                skipped.append(f"{code}: {type(exc).__name__}")
                continue
            m, b = run["metrics"], bench["metrics"]
            rows.append(
                {
                    "code": code,
                    "ret": m["total_return_pct"],
                    "bh": b["total_return_pct"],
                    "dd": m["max_drawdown_pct"],
                    "bh_dd": b["max_drawdown_pct"],
                    "calmar": m["calmar"],
                    "sharpe": m["sharpe"],
                    "trades": m["trade_count"],
                    "exposure": m["exposure_pct"],
                }
            )
            if index % 50 == 0:
                print(f"  {index}/{len(codes)} 已评估 {len(rows)}", flush=True)

    if not rows:
        print("没有可用样本")
        db.dispose()
        return 1

    rets = [r["ret"] for r in rows]
    excess = [r["ret"] - r["bh"] for r in rows]
    dd_gain = [r["bh_dd"] - r["dd"] for r in rows]
    calmars = [r["calmar"] for r in rows if r["calmar"] is not None]
    sharpes = [r["sharpe"] for r in rows if r["sharpe"] is not None]
    trades = [r["trades"] for r in rows]
    exposures = [r["exposure"] for r in rows]

    def pct_beat(values: list[float]) -> float:
        return 100.0 * sum(1 for v in values if v > 0) / len(values)

    print()
    print(f"=== {args.strategy} 在 {len(rows)} 只标的上的分布 ===")
    if args.since or args.until:
        print(f"窗口: {args.since or '最早'} ~ {args.until or '最新'}")
    print()
    print(f"{'指标':<22}{'中位':>10}{'均值':>10}{'25%':>10}{'75%':>10}{'最差':>10}{'最好':>10}")
    for label, values in (
        ("策略收益 %", rets),
        ("买入持有 %", [r["bh"] for r in rows]),
        ("超额 %", excess),
        ("回撤改善 pp", dd_gain),
        ("策略回撤 %", [r["dd"] for r in rows]),
        ("Calmar", calmars),
        ("Sharpe", sharpes),
        ("交易笔数", trades),
        ("市场暴露 %", exposures),
    ):
        if not values:
            continue
        q = statistics.quantiles(values, n=4) if len(values) > 3 else [min(values), 0, max(values)]
        print(
            f"{label:<22}{statistics.median(values):>10.1f}{statistics.mean(values):>10.1f}"
            f"{q[0]:>10.1f}{q[2]:>10.1f}{min(values):>10.1f}{max(values):>10.1f}"
        )
    print()
    print(f"跑赢买入持有: {pct_beat(excess):.1f}%  ({sum(1 for v in excess if v>0)}/{len(rows)})")
    print(f"回撤改善:     {pct_beat(dd_gain):.1f}%  ({sum(1 for v in dd_gain if v>0)}/{len(rows)})")
    print(f"收益为正:     {pct_beat(rets):.1f}%")
    if skipped:
        print(f"\n跳过 {len(skipped)} 只（前 5）：{skipped[:5]}")
    db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
