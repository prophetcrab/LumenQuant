"""A-share watchlist: one row per symbol with everything obtainable per screen.

The monitoring page needs more than a price. Three upstreams each know a
different slice, and none knows all of it:

* **Tencent realtime quote** — freshest price/OHLC/volume during a session.
* **Sina market snapshot** — valuation (PE/PB/market cap), turnover, amount.
  Whole-market, refreshed on demand, cached for a minute.
* **Local candles** — anything derived from history: moving averages, RSI,
  momentum, volume ratio, ATR. No upstream call at all.

No upstream here provides industry classification, fund flow or financial
statements, so those are reported as unavailable with the reason instead of being
rendered as an empty column. A blank cell reads as "zero" or "not computed"; an
explicit "上游不提供" is the truth.

Merging order matters: a live quote beats the snapshot's end-of-day price, and the
snapshot beats a stale candle close. Each row records which one it used, so a
price that looks live can be traced back to where it came from.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.money import round_money, round_qty
from app.modules.market import indicators as ta
from app.modules.market.service import MarketService
from app.modules.screener.service import ScreenerService
from app.modules.screener.sources.sina import SinaUnavailable

# Daily bars needed for the longest-derived figure (MA60 / ATR14 needs a little
# more, 120 gives every indicator a full warmup).
_TECHNICAL_BARS = 120
_MIN_TECHNICAL_BARS = 30


def _pct_change(closes: list[float], days: int) -> float | None:
    if len(closes) <= days:
        return None
    base = closes[-days - 1]
    return round((closes[-1] / base - 1) * 100, 2) if base else None


def _technicals(market: MarketService, code: str) -> dict | None:
    """History-derived context, or None when the symbol has no local candles."""
    try:
        candles = market.candles(code, timeframe="1d", limit=_TECHNICAL_BARS, closed_only=True)
    except Exception:
        return None
    if len(candles) < _MIN_TECHNICAL_BARS:
        return None

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    rsi_series = ta.rsi(closes, 14)
    atr_series = ta.atr(candles, 14)
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    atr = atr_series[-1]
    last = closes[-1]

    return {
        "ma5": round(ma5, 3),
        "ma20": round(ma20, 3),
        "ma60": round(ma60, 3) if ma60 is not None else None,
        # Above/below MA20 is the single most-read trend fact on a watchlist.
        "price_vs_ma20_pct": round((last / ma20 - 1) * 100, 2) if ma20 else None,
        "rsi14": round(rsi_series[-1], 2) if rsi_series and rsi_series[-1] is not None else None,
        "return_5d_pct": _pct_change(closes, 5),
        "return_20d_pct": _pct_change(closes, 20),
        "volume_ratio_5_20": (
            round((sum(volumes[-5:]) / 5) / (sum(volumes[-20:]) / 20), 3)
            if sum(volumes[-20:]) > 0
            else None
        ),
        "distance_from_20d_high_pct": round((last / max(closes[-20:]) - 1) * 100, 2),
        # ATR as a share of price: the cost/risk yardstick this platform uses
        # everywhere else, so the number means the same thing across pages.
        "atr_pct": round(atr / last * 100, 2) if atr is not None and last else None,
        "bars": len(closes),
    }


def _snapshot_ohlc(screener: ScreenerService) -> dict[str, dict]:
    """Raw OHLC/volume from the same snapshot, keyed by code.

    `ScreenerService._normalise` deliberately keeps only the fields a *screen*
    filters on, so `high`/`low`/`open`/`volume` never reach its rows. A watchlist
    needs the intraday range to show 振幅, so the raw payload is read once here
    rather than widening the screener's contract for a caller it does not serve.
    """
    try:
        raw = screener.source.snapshot()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for row in raw:
        digits = str(row.get("code") or "").strip()
        ticker = str(row.get("symbol") or "").strip().lower()
        if len(digits) != 6 or not ticker:
            continue
        suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(ticker[:2], "SH")
        code = f"{digits}.{suffix}"
        out[code] = {
            "open": _float(row.get("open")),
            "high": _float(row.get("high")),
            "low": _float(row.get("low")),
            "prev_close": _float(row.get("settlement")),
            "volume": _float(row.get("volume")),
        }
    return out


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def build_watchlist(
    *,
    session: Session,
    market: MarketService,
    screener: ScreenerService,
    quote_rows: list[dict] | None = None,
    codes: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Merged watchlist rows plus an explicit coverage report.

    `quote_rows` comes from the realtime poller when it is running; without it the
    snapshot's price is used and `price_source` says so.
    """
    views = market.list_symbols(market="cn_stock")
    wanted = {c.strip().upper() for c in (codes or []) if c.strip()}
    if wanted:
        views = [v for v in views if v.code.upper() in wanted]
    views = views[: max(1, min(limit, 500))]

    # Valuation/turnover slice. A whole-market fetch is slow and can fail; the
    # watchlist must still render with prices in that case.
    snapshot: dict[str, dict] = {}
    raw_ohlc: dict[str, dict] = {}
    warnings: list[str] = []
    snapshot_latency: float | None = None
    try:
        rows, latency = screener.universe()
        snapshot_latency = latency
        snapshot = {row["code"]: row for row in rows}
        raw_ohlc = _snapshot_ohlc(screener)
    except SinaUnavailable as exc:
        warnings.append(
            f"估值/换手数据暂不可用（{exc}）；价格与历史指标仍会显示"
        )

    quotes = {row["code"]: row for row in (quote_rows or [])}
    out: list[dict] = []
    technical_count = 0

    for view in views:
        code = view.code
        snap = snapshot.get(code, {})
        quote = quotes.get(code)
        tech = _technicals(market, code)
        if tech is not None:
            technical_count += 1

        # Price priority: live quote > snapshot > last stored close.
        if quote is not None and quote.get("price"):
            price_value = float(quote["price"])
            price_source = "realtime"
            quote_age = quote.get("age_ms")
            quote_fresh = quote.get("fresh")
        elif snap.get("price"):
            price_value = float(snap["price"])
            price_source = "snapshot"
            quote_age = None
            quote_fresh = None
        else:
            price_value = float(view.prev_close or view.initial_price or 0.0)
            price_source = "candle"
            quote_age = None
            quote_fresh = None

        prev_close = (
            float(quote["prev_close"]) if quote and quote.get("prev_close")
            else float(snap.get("prev_close") or raw_ohlc.get(code, {}).get("prev_close")
                       or view.prev_close or 0.0)
        )
        change_pct = (
            round((price_value / prev_close - 1) * 100, 2) if prev_close else 0.0
        )
        # Intraday range: the live quote is fresher, the snapshot is the fallback.
        ohlc = raw_ohlc.get(code, {})
        high = (quote.get("high") if quote else None) or ohlc.get("high")
        low = (quote.get("low") if quote else None) or ohlc.get("low")
        open_price = (quote.get("open") if quote else None) or ohlc.get("open")
        volume = (quote.get("volume") if quote else None) or ohlc.get("volume")
        # Amplitude is a watchlist staple in China: (high-low)/prev_close.
        amplitude = (
            round((float(high) - float(low)) / prev_close * 100, 2)
            if high and low and prev_close
            else None
        )

        out.append(
            {
                "code": code,
                "name": view.name,
                "board": view.board,
                "is_st": view.is_st,
                "suspended": view.suspended,
                # --- price -------------------------------------------------
                "price": round_money(price_value),
                "prev_close": round_money(prev_close),
                "change_pct": change_pct,
                "open": round_money(open_price) if open_price else None,
                "high": round_money(float(high)) if high else None,
                "low": round_money(float(low)) if low else None,
                "amplitude_pct": amplitude,
                "limit_up": round_money(prev_close * (1 + view.limit_pct)) if prev_close else None,
                "limit_down": round_money(prev_close * (1 - view.limit_pct)) if prev_close else None,
                # --- volume / liquidity ------------------------------------
                "volume": round_qty(float(volume)) if volume else None,
                "amount_yi": snap.get("amount_yi"),
                "turnover_pct": snap.get("turnover_pct"),
                # --- valuation --------------------------------------------
                "pe": snap.get("pe"),
                "pb": snap.get("pb"),
                "market_cap_yi": snap.get("market_cap_yi"),
                "float_cap_yi": snap.get("float_cap_yi"),
                # --- history-derived --------------------------------------
                "technicals": tech,
                # --- provenance -------------------------------------------
                "price_source": price_source,
                "quote_age_ms": quote_age,
                "quote_fresh": quote_fresh,
            }
        )

    warnings = list(warnings)
    if technical_count < len(out):
        warnings.append(
            f"{len(out) - technical_count}/{len(out)} 个标的没有本地日线，"
            "技术指标为空；可在数据页同步后再看"
        )

    return {
        "rows": out,
        "count": len(out),
        "snapshot_latency_ms": round(snapshot_latency, 1) if snapshot_latency is not None else None,
        # What this screen can actually show. Declared rather than left blank so
        # nobody reads a missing column as "zero" or "the value is fine".
        "coverage": {
            "price": {"available": True, "source": "腾讯实时 + 新浪快照"},
            "valuation": {
                "available": bool(snapshot),
                "source": "新浪全市场快照",
                "detail": "PE / PB / 市值 / 换手 / 成交额",
            },
            "technicals": {
                "available": technical_count > 0,
                "symbols": technical_count,
                "source": "本地日线计算",
                "detail": "MA / RSI / 动量 / 量比 / ATR",
            },
            "industry": {
                "available": False,
                "source": None,
                "detail": "当前行情快照不含行业分类；需要额外接口，暂未接入",
            },
            "fund_flow": {
                "available": False,
                "source": None,
                "detail": "需要主力资金流接口，暂未接入",
            },
            "financials": {
                "available": False,
                "source": None,
                "detail": "季频财报，需要额外接口；8 年也只有 32 个季度，因子研究价值有限",
            },
        },
        "warnings": warnings,
    }
