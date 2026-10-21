"""End-to-end acceptance against real exchange data.

Runs the whole product path on a throwaway database: sync → data quality →
account → backtest → manual trade with T+1 → automated instance → risk view →
equity identity → assistant → archive. Each step asserts on the same invariants
the UI relies on, so a regression anywhere in the chain fails here.

Requires network access to the Tencent quote endpoints.

Usage:
  python tests/stress/real_data_e2e.py [--code 600036.SH] [--daily 300]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="600036.SH")
    parser.add_argument("--daily", type=int, default=300)
    parser.add_argument("--intraday", type=int, default=160)
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'e2e.db').as_posix()}",
        seed_market=False,
        cn_symbols_enabled=True,
        cn_data_source="tencent",
        cn_sync_on_start=False,
        runtime_enabled=False,
        cn_session_enforced=False,
        cn_live_session_gate=False,
        risk_max_orders_per_minute=1000,
    )
    db = Database(settings.database_url)
    db.create_schema()
    app = create_app(settings, db)

    results: list[bool] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")

    with TestClient(app) as client:
        sync = client.post(
            "/api/cn/data/sync",
            params={
                "codes": args.code,
                "daily_limit": args.daily,
                "intraday_limit": args.intraday,
            },
        )
        rows = sync.json()["symbols"]
        check(
            "真实数据同步",
            bool(sync.json()["ok"]) and all(row["daily_bars"] > 200 for row in rows),
            f"daily={[r['daily_bars'] for r in rows]}",
        )

        quality = client.get(
            "/api/data/quality", params={"symbol": args.code, "timeframe": "1d"}
        ).json()
        check(
            "日线质量无缺口",
            quality["gaps"] == 0
            and quality["invalid_ohlc"] == 0
            and quality["duplicate_times"] == 0,
            f"bars={quality['bars']} gaps={quality['gaps']}",
        )

        account = client.post(
            "/api/accounts",
            json={"name": "E2E", "initial_balance": 400_000, "market": "cn_stock"},
        ).json()
        check("创建 A 股账户", account["base_currency"] == "CNY")

        run = client.post(
            "/api/backtests",
            json={
                "strategy": "sma_cross",
                "symbol": args.code,
                "timeframe": "1d",
                "bars": args.daily,
                "config": {"initial_capital": 400_000},
            },
        ).json()
        check(
            "真实数据回测",
            run["metrics"]["trade_count"] > 0 and run["currency"] == "CNY",
            f"trades={run['metrics']['trade_count']} ret={run['metrics']['total_return_pct']:.2f}%",
        )

        buy = client.post(
            "/api/trading/orders",
            json={
                "account_id": account["id"],
                "symbol": args.code,
                "side": "buy",
                "order_type": "market",
                "quantity": 100,
            },
        )
        positions = client.get(
            "/api/trading/positions", params={"account_id": account["id"]}
        ).json()
        sell = client.post(
            "/api/trading/orders",
            json={
                "account_id": account["id"],
                "symbol": args.code,
                "side": "sell",
                "order_type": "market",
                "quantity": 100,
            },
        )
        check(
            "真实价下单 + T+1 拦截",
            buy.status_code == 201
            and positions
            and positions[0]["available_quantity"] == 0
            and sell.status_code == 409,
            f"fill={buy.json()['avg_fill_price']}",
        )

        instance = client.post(
            "/api/runtime/instances",
            json={
                "account_id": account["id"],
                "strategy_key": "buy_and_hold",
                "symbol": args.code,
                "timeframe": "1d",
                "params": {},
                "position_pct": 0.2,
                "max_order_notional": 60_000,
                "stop_loss_pct": 5,
                "start": True,
            },
        ).json()
        tick = client.post("/api/runtime/tick").json()["evaluated"]
        check(
            "自动实例执行",
            tick.get(instance["id"]) in {"executed", "no_new_bar", "insufficient_data"},
            f"tick={tick.get(instance['id'])}",
        )

        risk = client.get("/api/risk/overview", params={"account_id": account["id"]}).json()
        protected = [item for item in risk["instances"] if item["protected"]]
        check(
            "风控可见止损保护",
            len(protected) == 1 and not risk["warnings"],
            f"protected={len(protected)}",
        )

        overview = client.get(
            "/api/portfolio/overview", params={"account_id": account["id"]}
        ).json()
        market_value = sum(
            p["market_value"]
            for p in client.get(
                "/api/trading/positions", params={"account_id": account["id"]}
            ).json()
        )
        check(
            "权益 = 现金 + 持仓市值",
            abs(overview["cash"] + market_value - overview["equity"]) < 0.01,
            f"{overview['cash']:.2f}+{market_value:.2f}={overview['equity']:.2f}",
        )

        answer = client.post(
            "/api/assistant/chat",
            json={"message": "双均线信号", "account_id": account["id"]},
        ).json()["answer"]
        check("助手不跨市场", "USDT" not in answer, answer[:60])

        client.patch(f"/api/accounts/{account['id']}", json={"status": "archived"})
        remaining = client.get(
            "/api/runtime/instances", params={"account_id": account["id"]}
        ).json()
        check("归档停止实例", all(not item["running"] for item in remaining))

    db.dispose()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
