"""Import A-share daily history for a broad cross-section.

Research on a handful of names cannot support a conclusion: nine symbols over
eight years is ~13,500 observations, which resolves an IC of only ~0.026 — the
same magnitude as the factor being measured. This script widens the panel so the
distribution of results, not one lucky symbol, is what gets read.

Usage:
    python -m scripts.import_as_share_panel --limit 500
    python -m scripts.import_as_share_panel --limit 500 --since 2019-01-01

What it does per symbol:
  * registers the name (with its real name and ST flag — registering without
    them left every ST stock on a 10%/20% band instead of 5%);
  * fetches forward-adjusted daily bars (a split otherwise appears as a −67%
    crash and corrupts every indicator for weeks);
  * writes them in one transaction.

It deliberately does NOT sync 15m bars: intraday history is capped at ~50
trading days upstream, far too short for a daily-frequency study, and fetching it
would triple the runtime for data this study cannot use.
"""

from __future__ import annotations

import argparse
import sys
import time
from sqlalchemy import delete, select

sys.path.insert(0, ".")

from app.core.config import Settings  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.modules.cn.adjust import forward_adjusted  # noqa: E402
from app.modules.cn.rules import limit_pct  # noqa: E402
from app.modules.cn.sources.base import SourceUnavailable  # noqa: E402
from app.modules.cn.sources.tencent import TencentSource  # noqa: E402
from app.modules.market.models import Candle, Symbol  # noqa: E402
from app.modules.screener.sources.sina import SinaMarketSource  # noqa: E402

def pick_universe(limit: int, *, min_amount_yi: float) -> list[dict]:
    """Whole-market rows, filtered to what a daily study can actually use.

    Microcaps are excluded on purpose: they dominate equal-weighted A-share
    backtests and are effectively untradeable at size, which is the main
    criticism in Li et al. (2023) of the anomaly literature.
    """
    source = SinaMarketSource(timeout=30)
    rows = source.snapshot()
    out: list[dict] = []
    for raw in rows:
        digits = str(raw.get("code") or "").strip()
        ticker = str(raw.get("symbol") or "").strip().lower()
        if len(digits) != 6 or not ticker:
            continue
        price = raw.get("trade")
        amount = raw.get("amount")
        try:
            price = float(price)
            amount_yi = float(amount) / 1e8
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue  # suspended
        name = str(raw.get("name") or "").strip()
        if not name or name.startswith("*"):
            continue
        if amount_yi < min_amount_yi:
            continue
        suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(ticker[:2], "SH")
        board = _board_of(f"{digits}.{suffix}")
        is_st = "ST" in name.upper()
        out.append(
            {
                "code": f"{digits}.{suffix}",
                "name": name,
                "board": board,
                "is_st": is_st,
                "amount_yi": amount_yi,
            }
        )
    # Rank by liquidity: the most tradable names first, so a small --limit still
    # yields a usable panel.
    out.sort(key=lambda row: row["amount_yi"], reverse=True)
    return out[:limit]


def _board_of(code: str) -> str:
    digits = code.split(".")[0]
    if code.endswith(".BJ") or digits.startswith(("4", "8", "9")):
        return "bse"
    if digits.startswith("688"):
        return "star"
    if digits.startswith(("300", "301")):
        return "gem"
    return "main"


def ensure_symbol(db: Database, meta: dict) -> None:
    with db.session() as session:
        row = session.scalar(select(Symbol).where(Symbol.code == meta["code"]))
        pct = limit_pct(meta["board"], meta["is_st"])
        if row is None:
            session.add(
                Symbol(
                    code=meta["code"],
                    name=meta["name"],
                    asset_class="stock",
                    market="cn_stock",
                    exchange=meta["code"].split(".")[-1],
                    board=meta["board"],
                    base=meta["code"].split(".")[0],
                    quote="CNY",
                    tick_size=0.01,
                    lot_size=100.0,
                    initial_price=1.0,
                    prev_close=0.0,
                    limit_pct=pct,
                    is_st=meta["is_st"],
                    base_timeframe="15m",
                )
            )
        else:
            # Keep the registry in step with the screen: the name, the ST flag
            # and the derived band all decide how the symbol trades.
            row.name = meta["name"]
            row.is_st = meta["is_st"]
            row.board = meta["board"]
            row.limit_pct = pct
        session.commit()


def import_daily(
    db: Database, source: TencentSource, meta: dict, *, since: str
) -> tuple[int, str]:
    try:
        # `changed_history` (the exchange's adjusted series) is rate-limited far
        # harder than the raw endpoint — measured 2026-10: every request returned
        # HTTP 501 while raw kept working. So fetch raw and derive the divisor
        # locally; `cn.adjust` reproduces BYD's real 3.0246 split factor from the
        # gap alone.
        batch = source.daily_history(meta["code"], start=since)
    except SourceUnavailable as exc:
        return 0, str(exc)[:120]
    except Exception as exc:  # one bad symbol must not abort the batch
        return 0, f"{type(exc).__name__}: {exc}"[:120]
    if not batch.bars:
        return 0, "无数据"
    adjusted = forward_adjusted(batch.bars)
    if adjusted.events:
        print(
            f"    {meta['code']} 检测到 {len(adjusted.events)} 个除权事件",
            flush=True,
        )

    rows = [
        {
            "symbol": meta["code"],
            "timeframe": "1d",
            "open_time": bar.time,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in adjusted.bars
    ]
    with db.session() as session:
        session.execute(
            delete(Candle).where(
                Candle.symbol == meta["code"], Candle.timeframe == "1d"
            )
        )
        for start in range(0, len(rows), 2000):
            session.bulk_insert_mappings(Candle, rows[start : start + 2000])
        # prev_close drives the daily limit band; anchor it to the newest close.
        symbol = session.scalar(select(Symbol).where(Symbol.code == meta["code"]))
        if symbol is not None:
            symbol.prev_close = adjusted.bars[-1].close
            symbol.limit_pct = limit_pct(symbol.board, symbol.is_st)
        session.commit()
    return len(rows), ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Import an A-share daily panel")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--since", default="2019-01-01")
    parser.add_argument(
        "--min-amount-yi", type=float, default=0.5,
        help="最低日成交额（亿元）；过低会让微盘股主导结果",
    )
    args = parser.parse_args()

    settings = Settings()
    db = Database(settings.database_url)
    source = TencentSource(timeout=30)

    universe = pick_universe(args.limit, min_amount_yi=args.min_amount_yi)
    print(f"候选池 {len(universe)} 只（日成交额 ≥ {args.min_amount_yi} 亿，按流动性排序）")

    started = time.time()
    ok = failed = 0
    total_bars = 0
    failures: list[str] = []
    for index, meta in enumerate(universe, start=1):
        ensure_symbol(db, meta)
        count, error = import_daily(db, source, meta, since=args.since)
        if count:
            ok += 1
            total_bars += count
        else:
            failed += 1
            if len(failures) < 10:
                failures.append(f"{meta['code']} {meta['name']}: {error}")
        time.sleep(0.35)  # the host throttles bursts with HTTP 501
        if index % 25 == 0 or index == len(universe):
            elapsed = time.time() - started
            rate = index / elapsed if elapsed else 0
            eta = (len(universe) - index) / rate if rate else 0
            print(
                f"  {index:>4}/{len(universe)}  ok={ok} fail={failed}  "
                f"{total_bars:>7} 根  {elapsed:>6.0f}s  ETA {eta:>5.0f}s",
                flush=True,
            )

    print(f"\n完成：{ok} 只导入成功，{failed} 只失败，共 {total_bars:,} 根")
    for line in failures:
        print(f"  失败: {line}")
    db.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
