"""Concurrent API load smoke test.

Usage:
  python tests/stress/load_api.py --base http://127.0.0.1:8811 --requests 500 --concurrency 25
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str) -> tuple[int, float, str]:
    async with sem:
        start = time.perf_counter()
        try:
            response = await client.get(url)
            elapsed = (time.perf_counter() - start) * 1000
            return response.status_code, elapsed, ""
        except Exception as exc:  # network failures count as errors
            elapsed = (time.perf_counter() - start) * 1000
            return 0, elapsed, repr(exc)


async def run(base: str, requests: int, concurrency: int, path: str) -> dict:
    path = "/" + path.lstrip("/")  # Git Bash may rewrite leading slashes
    sem = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
        results = await asyncio.gather(*(one(client, sem, path) for _ in range(requests)))
    latencies = sorted(r[1] for r in results)
    errors = [r[2] for r in results if r[0] < 200 or r[0] >= 300]
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    return {
        "path": path,
        "requests": requests,
        "concurrency": concurrency,
        "errors": len(errors),
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(latencies[-1], 2),
        "sample_error": errors[0] if errors else "",
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8811")
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=25)
    parser.add_argument("--path", default="/health")
    args = parser.parse_args()
    result = await run(args.base, args.requests, args.concurrency, args.path)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
