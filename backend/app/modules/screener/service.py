"""A-share screening: normalise a full-market snapshot and apply filters.

The whole market is fetched once and cached briefly; filters then run in memory.
That shape matters because a person tweaks a slider and expects the result to move
immediately — re-fetching ~5,400 rows per keystroke would be both slow and rude to
the upstream.

Technical indicators are computed only for the rows actually returned, and only
when the caller asks: the database holds candles for a handful of symbols, so
computing them for everything would be mostly empty work.
"""

from __future__ import annotations

import logging
import time

from app.modules.screener.schemas import BOARDS, ScreenFilter
from app.modules.screener.sources.sina import SinaMarketSource, SinaUnavailable

logger = logging.getLogger(__name__)

# Sina reports 成交额 in 元 and 市值 in 万元.
YUAN_PER_YI = 1_0000_0000.0
WAN_PER_YI = 1_0000.0


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def board_of(code: str) -> str:
    """Derive the board from the exchange prefix — the feed does not label it."""
    digits = code.split(".")[0]
    if code.endswith(".BJ") or digits.startswith(("4", "8", "9")):
        return "bse"
    if digits.startswith("688"):
        return "star"
    if digits.startswith(("300", "301")):
        return "gem"
    return "main"


def _normalise(row: dict) -> dict | None:
    """One source row -> one candidate, or None when it cannot be screened.

    Suspended names come back with `trade` parked at 0 and no turnover; they would
    match a "price below X" filter for the wrong reason, so they are dropped
    instead of being ranked.
    """
    raw_symbol = str(row.get("symbol") or "").strip().lower()
    digits = str(row.get("code") or "").strip()
    if len(digits) != 6 or not raw_symbol:
        return None
    price = _num(row.get("trade"))
    if price is None or price <= 0:
        return None
    name = str(row.get("name") or "").strip()
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(raw_symbol[:2], "SH")
    code = f"{digits}.{suffix}"
    market_cap_yi = (_num(row.get("mktcap")) or 0.0) / WAN_PER_YI
    float_cap_yi = (_num(row.get("nmc")) or 0.0) / WAN_PER_YI
    return {
        "code": code,
        "name": name,
        "board": board_of(code),
        "is_st": "ST" in name.upper() or name.startswith("*"),
        "price": price,
        "change_pct": _num(row.get("changepercent")) or 0.0,
        "amount_yi": (_num(row.get("amount")) or 0.0) / YUAN_PER_YI,
        "turnover_pct": _num(row.get("turnoverratio")) or 0.0,
        # PE of exactly 0 means "not applicable" (loss-making), not "free".
        "pe": (lambda p: p if p and p > 0 else None)(_num(row.get("per"))),
        "pb": (lambda p: p if p and p > 0 else None)(_num(row.get("pb"))),
        "market_cap_yi": round(market_cap_yi, 2),
        "float_cap_yi": round(float_cap_yi, 2),
    }


def _passes(candidate: dict, f: ScreenFilter, boards: set[str]) -> bool:
    if boards and candidate["board"] not in boards:
        return False
    if f.exclude_st and candidate["is_st"]:
        return False

    def within(value: float | None, low: float | None, high: float | None) -> bool:
        if low is not None:
            if value is None or value < low:
                return False
        if high is not None:
            if value is None or value > high:
                return False
        return True

    # PE / PB filters must exclude the names whose value is unknown: a "PE below
    # 20" screen silently including loss-makers would be exactly backwards.
    if (f.pe_min is not None or f.pe_max is not None) and candidate["pe"] is None:
        return False
    if (f.pb_min is not None or f.pb_max is not None) and candidate["pb"] is None:
        return False

    return (
        within(candidate["price"], f.price_min, f.price_max)
        and within(candidate["change_pct"], f.change_pct_min, f.change_pct_max)
        and within(candidate["amount_yi"], f.amount_min, None)
        and within(candidate["turnover_pct"], f.turnover_min, f.turnover_max)
        and within(candidate["pe"], f.pe_min, f.pe_max)
        and within(candidate["pb"], f.pb_min, f.pb_max)
        and within(candidate["market_cap_yi"], f.market_cap_min, f.market_cap_max)
    )


class ScreenerService:
    def __init__(self, source: SinaMarketSource, *, market=None, cache=None) -> None:
        self.source = source
        self.market = market
        self.cache = cache
        # Rows age fast during a session; the cache is short on purpose.
        self.cache_key = "screener:universe"
        self.cache_ttl_seconds = 60.0

    def universe(self, *, force: bool = False) -> tuple[list[dict], float]:
        """Normalised full-market rows plus the latency of obtaining them."""
        if self.cache is not None and not force:
            cached = self.cache.get(self.cache_key)
            if cached is not None:
                rows, latency = cached  # type: ignore[misc]
                return rows, latency
        started = time.perf_counter()
        raw = self.source.snapshot()
        rows = [item for item in (_normalise(row) for row in raw) if item is not None]
        latency = (time.perf_counter() - started) * 1000
        if not rows:
            raise SinaUnavailable("选股数据源返回空列表")
        if self.cache is not None:
            self.cache.put(self.cache_key, (rows, latency))
        return rows, latency

    def screen(self, f: ScreenFilter, *, force: bool = False) -> dict:
        boards = f.normalised_boards()
        if f.sort_by not in {
            "change_pct", "amount", "turnover", "pe", "pb", "market_cap", "price"
        }:
            raise ValueError(f"unknown sort field: {f.sort_by}")
        rows, latency = self.universe(force=force)
        matched = [row for row in rows if _passes(row, f, boards)]

        key = f.sort_by
        # Missing values collect at the end regardless of direction: a loss-maker
        # with no PE must not win a "cheapest PE" screen, and reversing the whole
        # list would otherwise float it to the top.
        present = [row for row in matched if row.get(key) is not None]
        absent = [row for row in matched if row.get(key) is None]
        present.sort(key=lambda row: float(row[key]), reverse=not f.ascending)
        matched = present + absent

        picked = matched[: f.limit]
        warnings: list[str] = []
        if f.with_technicals:
            empty = self._attach_technicals(picked)
            if empty:
                warnings.append(
                    f"{empty}/{len(picked)} 个标的没有本地 K 线，未计算技术指标；"
                    "可先在「A股」页同步这些标的的数据"
                )
        return {
            "total_universe": len(rows),
            "matched": len(matched),
            "returned": len(picked),
            "source": getattr(self.source, "name", "sina-market"),
            "latency_ms": round(latency, 1),
            "criteria": f.model_dump(exclude_none=True),
            "candidates": picked,
            "warnings": warnings,
        }

    # ------------------------------------------------------------ technicals
    def _attach_technicals(self, rows: list[dict]) -> int:
        """Add indicator context for rows that have stored candles.

        Returns how many rows were left without technicals, so the response can say
        so instead of presenting a blank field as "no signal".
        """
        if self.market is None:
            return len(rows)
        missing = 0
        for row in rows:
            try:
                candles = self.market.candles(
                    row["code"], timeframe="1d", limit=120, closed_only=True
                )
            except Exception:
                candles = []
            if len(candles) < 30:
                missing += 1
                continue
            closes = [c.close for c in candles]
            volumes = [c.volume for c in candles]
            row["technicals"] = {
                "ma5": round(sum(closes[-5:]) / 5, 3),
                "ma20": round(sum(closes[-20:]) / 20, 3),
                "ma60": round(sum(closes[-60:]) / 60, 3) if len(closes) >= 60 else None,
                "price_vs_ma20_pct": round((closes[-1] / (sum(closes[-20:]) / 20) - 1) * 100, 2),
                "rsi14": _rsi(closes, 14),
                "return_5d_pct": _pct_change(closes, 5),
                "return_20d_pct": _pct_change(closes, 20),
                "volume_ratio_5_20": round(
                    (sum(volumes[-5:]) / 5) / (sum(volumes[-20:]) / 20), 3
                )
                if sum(volumes[-20:]) > 0
                else None,
                "distance_from_20d_high_pct": round(
                    (closes[-1] / max(closes[-20:]) - 1) * 100, 2
                ),
                "bars": len(closes),
            }
        return missing


def _pct_change(closes: list[float], days: int) -> float | None:
    if len(closes) <= days:
        return None
    base = closes[-1 - days]
    return round((closes[-1] / base - 1) * 100, 2) if base else None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI over the last `period` changes."""
    if len(closes) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(len(closes) - period, len(closes)):
        change = closes[index] - closes[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    if gains + losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100 - 100 / (1 + rs), 2)


# ------------------------------------------------------------------ presets
def preset_filters() -> list[dict]:
    """Ready-made screens.

    Each one encodes a *reason* to look, not just a range: a screen that matches
    thousands of names (or none) teaches nothing. Thresholds are deliberately
    loose enough that a normal session produces tens of candidates.
    """
    return [
        {
            "key": "strong_breakout",
            "name": "强势突破",
            "description": "涨幅 3%–9.5%、换手 3%–15%、成交额 2 亿以上，非 ST",
            "filter": ScreenFilter(
                exclude_st=True,
                change_pct_min=3.0,
                change_pct_max=9.5,
                turnover_min=3.0,
                turnover_max=15.0,
                amount_min=2.0,
                sort_by="change_pct",
                limit=50,
            ),
        },
        {
            "key": "oversold_bounce",
            "name": "超跌反弹",
            "description": "跌幅 9%–3%、换手 5% 以上、成交额 1 亿以上，非 ST",
            "filter": ScreenFilter(
                exclude_st=True,
                change_pct_min=-9.0,
                change_pct_max=-3.0,
                turnover_min=5.0,
                amount_min=1.0,
                sort_by="change_pct",
                ascending=True,
                limit=50,
            ),
        },
        {
            "key": "active_volume",
            "name": "放量活跃",
            "description": "成交额 5 亿以上、换手 8% 以上，按成交额排序",
            "filter": ScreenFilter(
                exclude_st=True,
                amount_min=5.0,
                turnover_min=8.0,
                sort_by="amount",
                limit=50,
            ),
        },
        {
            "key": "value_blue_chip",
            "name": "低估值蓝筹",
            "description": "市值 300 亿以上、PE 5–20、PB 0.5–3，按市值排序",
            "filter": ScreenFilter(
                exclude_st=True,
                market_cap_min=300.0,
                pe_min=5.0,
                pe_max=20.0,
                pb_min=0.5,
                pb_max=3.0,
                sort_by="market_cap",
                limit=50,
            ),
        },
        {
            "key": "small_active",
            "name": "小盘活跃",
            "description": "市值 30–150 亿、换手 5%–25%、成交额 1 亿以上",
            "filter": ScreenFilter(
                exclude_st=True,
                market_cap_min=30.0,
                market_cap_max=150.0,
                turnover_min=5.0,
                turnover_max=25.0,
                amount_min=1.0,
                sort_by="turnover",
                limit=50,
            ),
        },
        {
            "key": "main_board_only",
            "name": "主板稳健",
            "description": "仅主板、市值 100 亿以上、涨跌幅 −2%–5%，按成交额排序",
            "filter": ScreenFilter(
                boards=["main"],
                exclude_st=True,
                market_cap_min=100.0,
                change_pct_min=-2.0,
                change_pct_max=5.0,
                sort_by="amount",
                limit=50,
            ),
        },
    ]
