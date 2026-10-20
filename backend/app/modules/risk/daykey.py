"""Session/day boundaries for risk accounting.

Crypto trades continuously, so its "day" is the UTC calendar day. A-shares follow
the exchange calendar in China Standard Time and must reset at the session open,
not at midnight UTC (which is 08:00 Beijing, mid pre-open).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.modules.cn.calendar import CN_TZ, day_key


def risk_day_key(market: str, ts_ms: int | None = None) -> str:
    """Identifier of the current risk day for an account's market."""
    if ts_ms is None:
        now = datetime.now(timezone.utc)
        ts_ms = int(now.timestamp() * 1000)
    if market == "cn_stock":
        return day_key(ts_ms).isoformat()
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).date().isoformat()


def cn_session_open_today(ts_ms: int) -> bool:
    """True once the Beijing session has opened today (or it is a closed day)."""
    from app.modules.cn.calendar import is_trading_day, sessions

    day = day_key(ts_ms)
    if not is_trading_day(day):
        return False
    opens = sessions(day)
    return bool(opens) and ts_ms >= opens[0][0]
