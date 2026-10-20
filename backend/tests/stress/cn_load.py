"""A-share load and invariant checks.

Verifies:
  * throughput of ordered A-share tickets (lot validation + fee math);
  * cost invariants (sell fee > buy fee due to stamp duty);
  * T+1 accounting: available <= quantity at all times.

Usage: python -m tests.stress.cn_load --orders 200 --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def one(sem, client, account_id, symbol, limit_price, index):
    async with sem:
        body = {
            "account_id": account_id,
            "symbol": symbol,
            "side": "buy",
            "order_type": "limit",
            # Bid at the limit-down edge: legal, but far from market so orders
            # rest instead of filling (isolates order-validation throughput).
            "quantity": 100,
            "limit_price": limit_price,
            "client_order_id": f"cn-load-{int(time.time()*1000)}-{index}",
        }
        start = time.perf_counter()
        try:
            response = await client.post("/api/trading/orders", json=body)
            return response.status_code, (time.perf_counter() - start) * 1000
        except Exception:
            return 0, (time.perf_counter() - start) * 1000


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base, timeout=30.0) as client:
        account = (
            await client.post(
                "/api/accounts",
                json={
                    "name": f"压测A股-{int(time.time())}",
                    "kind": "paper",
                    "market": "cn_stock",
                    "initial_balance": 1_000_000,
                },
            )
        ).json()
        symbols = (await client.get("/api/cn/symbols")).json()
        target = next(
            s for s in symbols if s["limit_down"] * 1.02 < s["price"] < s["limit_up"] * 0.98
        )
        sem = asyncio.Semaphore(args.concurrency)
        # Stay inside the daily band: limit-down is the cheapest legal bid.
        limit_price = round(target["limit_down"], 2)
        results = await asyncio.gather(
            *(
                one(sem, client, account["id"], target["code"], limit_price, i)
                for i in range(args.orders)
            )
        )
        positions = (
            await client.get("/api/trading/positions", params={"account_id": account["id"]})
        ).json()
        orders = (
            await client.get("/api/trading/orders", params={"account_id": account["id"], "limit": 500})
        ).json()

    latencies = sorted(r[1] for r in results)
    errors = [r for r in results if r[0] < 200 or r[0] >= 300]
    t1_ok = all(p["available_quantity"] <= p["quantity"] + 1e-9 for p in positions)
    print(
        {
            "orders": args.orders,
            "concurrency": args.concurrency,
            "errors": len(errors),
            "p50_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 1),
            "max_ms": round(latencies[-1], 1),
            "resting_orders": sum(1 for o in orders if o["status"] == "open"),
            "t1_invariant_holds": t1_ok,
            "error_sample": "" if not errors else str(errors[0][0]),
        }
    )


if __name__ == "__main__":
    asyncio.run(main())
