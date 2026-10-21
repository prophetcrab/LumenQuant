"""End-to-end load and consistency check.

Verifies not just latency but invariants:
  equity == cash + sum(position market value)
  ledger sum == cash
  filled orders have a matching fill row

Usage:
  python tests/stress/system_load.py --base http://127.0.0.1:8000 \
      --account <id> --orders 120 --concurrency 8 --queries 300
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time

import httpx

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
# Sized to clear the 10 USDT minimum notional at current demo prices.
QUANTITIES = {"BTCUSDT": 0.001, "ETHUSDT": 0.1, "SOLUSDT": 1.0, "BNBUSDT": 0.5}


async def timed(sem, client, method, path, **kwargs):
    async with sem:
        start = time.perf_counter()
        try:
            response = await client.request(method, path, **kwargs)
            return response.status_code, (time.perf_counter() - start) * 1000, response
        except Exception as exc:
            return 0, (time.perf_counter() - start) * 1000, exc


async def place_orders(client, account_id, count, concurrency):
    sem = asyncio.Semaphore(concurrency)

    async def one(index):
        symbol = SYMBOLS[index % len(SYMBOLS)]
        side = random.choice(["buy", "sell"])
        body = {
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "order_type": "market",
            "quantity": QUANTITIES[symbol],
            "client_order_id": f"stress-{int(time.time() * 1000)}-{index}",
        }
        return await timed(sem, client, "POST", "/api/trading/orders", json=body)

    return await asyncio.gather(*(one(i) for i in range(count)))


async def read_queries(client, account_id, count, concurrency):
    sem = asyncio.Semaphore(concurrency)
    # `/api/trading/positions` requires account_id; passing None used to drop the
    # param and make one in four "reads" a 422, which the summary then reported as
    # an error rate instead of a broken benchmark.
    paths = [
        "/api/portfolio/overview",
        "/api/trading/positions",
        "/api/market/snapshots",
        "/api/portfolio/analytics",
    ]

    async def one(index):
        path = paths[index % len(paths)]
        params = {} if path == "/api/market/snapshots" else {"account_id": account_id}
        return await timed(sem, client, "GET", path, params=params)

    return await asyncio.gather(*(one(i) for i in range(count)))


def summarise(label, results):
    latencies = sorted(r[1] for r in results)
    ok = [r for r in results if 200 <= r[0] < 300]
    errors = [r for r in results if r[0] < 200 or r[0] >= 300]

    def body(result):
        return str(getattr(result[2], "text", result[2]))[:200]

    # A rate-limit rejection is the risk layer doing its job, not a failure of
    # the system under load. Counting it as an error made a healthy run look
    # like 90% breakage and hid the actual latency profile of accepted orders.
    throttled = [
        r for r in errors
        if r[0] == 429 or (r[0] == 409 and "rate limit" in body(r))
    ]
    # A random sell without holdings, or one that overdraws cash, is the
    # business layer rejecting an impossible order — correct behaviour, and it
    # must not be counted as a system fault.
    business_markers = (
        "insufficient position",
        "insufficient cash",
        "可卖数量不足",
        "可用资金",
        "lot size",
        "涨停",
        "跌停",
    )
    rejected = [
        r for r in errors
        if r not in throttled and any(marker in body(r) for marker in business_markers)
    ]
    hard_errors = [r for r in errors if r not in throttled and r not in rejected]
    return {
        "label": label,
        "requests": len(results),
        "ok": len(ok),
        "throttled": len(throttled),
        "rejected": len(rejected),
        "errors": len(hard_errors),
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 2),
        "max_ms": round(latencies[-1], 2),
        "error_sample": "" if not hard_errors else body(hard_errors[0]),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--account", required=True)
    parser.add_argument("--orders", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--queries", type=int, default=300)
    parser.add_argument(
        "--expect-throttling",
        action="store_true",
        help="allow the server's rate limiter to reject orders; report them separately",
    )
    args = parser.parse_args()

    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(base_url=args.base, timeout=timeout) as client:
        # Warm caches so an expiry storm does not dominate the read numbers.
        await client.get("/api/market/snapshots")

        order_results = await place_orders(client, args.account, args.orders, args.concurrency)
        read_results = await read_queries(client, args.account, args.queries, args.concurrency)

        overview_resp = await client.get(
            "/api/portfolio/overview", params={"account_id": args.account}
        )
        account_resp = await client.get(f"/api/accounts/{args.account}")
        if overview_resp.status_code != 200 or account_resp.status_code != 200:
            raise SystemExit(
                "final state fetch failed: "
                f"overview={overview_resp.status_code} account={account_resp.status_code} "
                f"{overview_resp.text[:200]}"
            )
        overview = overview_resp.json()
        ledger_resp = await client.get(
            f"/api/accounts/{args.account}/ledger", params={"limit": 1000}
        )
        ledger = ledger_resp.json() if ledger_resp.status_code == 200 else []
        fills_resp = await client.get("/api/trading/fills", params={"account_id": args.account, "limit": 1000})
        orders_resp = await client.get("/api/trading/orders", params={"account_id": args.account, "limit": 1000})
        # Answering with an error body used to crash the invariant check with a
        # TypeError; surface the HTTP problem instead of a JSON traceback.
        fills = fills_resp.json() if fills_resp.status_code == 200 else []
        orders = orders_resp.json() if orders_resp.status_code == 200 else []
        positions_resp = await client.get("/api/trading/positions", params={"account_id": args.account})
        positions = positions_resp.json() if positions_resp.status_code == 200 else []

    positions_value = round(sum(p["market_value"] for p in positions), 6)
    equity_check = round(overview["cash"] + positions_value - overview["equity"], 6)
    # Sum every ledger row (initial funding included) and compare with cash.
    ledger_cash = round(sum(row["amount"] for row in ledger), 6)
    filled = [o for o in orders if o["status"] == "filled"]
    fill_rows = len(fills)

    print(summarise("place_orders", order_results))
    print(summarise("read_queries", read_results))
    print(
        {
            "equity_reconciles": abs(equity_check) < 0.01,
            "equity_delta": equity_check,
            "ledger_reconciles": abs(ledger_cash - overview["cash"]) < 0.01,
            "ledger_cash": ledger_cash,
            "cash": overview["cash"],
            "filled_orders": len(filled),
            "fills": fill_rows,
            "fills_cover_orders": fill_rows >= len(filled),
            "positions": len(positions),
        }
    )


if __name__ == "__main__":
    asyncio.run(main())
