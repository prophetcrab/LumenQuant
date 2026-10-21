"""Batch strategy/backtest throughput check (in-process, no HTTP).

Usage: python tests/stress/strategy_load.py [--bars 2000] [--repeat 5]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

# Allow running this file directly from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import Settings
from app.core.db import Database
from app.modules.backtest.engine import BacktestConfig, BacktestEngine
from app.modules.market.engine import MarketEngine
from app.modules.market.service import MarketService
from app.modules.strategies.registry import all_strategies


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()

    settings = Settings(
        database_url="sqlite:///D:/LumenQuant/data/lumenquant.db",
        seed_market=False,
        runtime_enabled=False,
    )
    db = Database(settings.database_url)

    class NoLive:
        def live_candle(self, symbol):  # noqa: ANN001
            return None

    timings: list[float] = []
    results = []
    with db.session() as session:
        market = MarketService(session, live=NoLive())
        for symbol in ("BTCUSDT", "ETHUSDT"):
            candles = market.candles(
                symbol, timeframe="1h", limit=args.bars, closed_only=True
            )
            for _ in range(args.repeat):
                for strategy in all_strategies():
                    params = strategy.validate_params({})
                    if len(candles) < strategy.warmup(params) + 3:
                        continue
                    start = time.perf_counter()
                    result = BacktestEngine(BacktestConfig()).run(
                        strategy, params, candles, "1h"
                    )
                    elapsed = (time.perf_counter() - start) * 1000
                    timings.append(elapsed)
                    results.append(
                        (symbol, strategy.key, round(elapsed, 1), result.metrics["total_return_pct"])
                    )
    db.dispose()

    timings.sort()
    print(
        {
            "runs": len(timings),
            "bars": args.bars,
            "p50_ms": round(statistics.median(timings), 1),
            "p95_ms": round(timings[int(len(timings) * 0.95) - 1], 1),
            "max_ms": round(timings[-1], 1),
            "total_s": round(sum(timings) / 1000, 2),
            # Slowest runs, not highest returns: this script exists to find
            # throughput outliers, and returning the same strategy three times
            # says nothing about cost.
            "slowest": sorted(results, key=lambda r: r[2], reverse=True)[:3],
        }
    )


if __name__ == "__main__":
    main()
