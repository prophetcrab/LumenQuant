"""A-share price-limit, lot-size and settlement rules.

Pure functions only: the trading service and the backtester share this module so
paper trading and research can never drift apart on a rule like T+1.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ConflictError, ValidationError

# Board-specific daily price limits (fractions).
BOARD_LIMITS: dict[str, float] = {
    "main": 0.10,   # 主板
    "gem": 0.20,    # 创业板
    "star": 0.20,   # 科创板
    "bse": 0.30,    # 北交所
}
ST_LIMIT = 0.05
DEFAULT_TICK = 0.01
CN_LOT = 100


@dataclass(frozen=True)
class PriceBand:
    up: float
    down: float
    pct: float

    def contains(self, price: float, tick: float = DEFAULT_TICK) -> bool:
        return self.down - tick / 2 <= price <= self.up + tick / 2


def limit_pct(board: str, is_st: bool = False) -> float:
    if is_st:
        return ST_LIMIT
    return BOARD_LIMITS.get(board, BOARD_LIMITS["main"])


def round_tick(value: float, tick: float = DEFAULT_TICK) -> float:
    if tick <= 0:
        return round(value, 4)
    return round(round(value / tick) * tick, 10)


def price_band(prev_close: float, board: str, is_st: bool = False, tick: float = DEFAULT_TICK) -> PriceBand | None:
    """Daily limit band. None when there is no valid previous close."""
    if prev_close is None or prev_close <= 0:
        return None
    pct = limit_pct(board, is_st)
    up = round_tick(prev_close * (1 + pct), tick)
    down = round_tick(prev_close * (1 - pct), tick)
    return PriceBand(up=up, down=down, pct=pct)


def at_limit_up(price: float, band: PriceBand | None, tick: float = DEFAULT_TICK) -> bool:
    # A quote rounded up to the limit counts as limit-up even with float noise.
    return band is not None and price >= band.up - tick / 2


def at_limit_down(price: float, band: PriceBand | None, tick: float = DEFAULT_TICK) -> bool:
    return band is not None and price <= band.down + tick / 2


def validate_quantity(side: str, quantity: float, *, position_quantity: float, available_quantity: float) -> None:
    """Lot rules: buys must be 100-share multiples; odd lots only via full exit."""
    if quantity <= 0:
        raise ValidationError("quantity must be positive")
    whole_lots = abs(quantity / CN_LOT - round(quantity / CN_LOT)) < 1e-9
    if side == "buy":
        if not whole_lots:
            raise ValidationError(f"A股买入数量必须为 {CN_LOT} 股的整数倍")
        return
    if not whole_lots and abs(quantity - position_quantity) > 1e-9:
        raise ValidationError(
            f"A股卖出数量必须为 {CN_LOT} 股的整数倍，零股仅可一次性全部卖出"
        )
    if quantity > available_quantity + 1e-9:
        raise ConflictError(
            f"可用数量不足：可卖 {available_quantity:g} 股（T+1 当日买入次日可用）"
        )


def validate_limit_price(price: float, band: PriceBand | None, tick: float = DEFAULT_TICK) -> None:
    if band is not None and not band.contains(price, tick):
        raise ValidationError(
            f"委托价 {price:g} 超出涨跌停区间 [{band.down:g}, {band.up:g}]"
        )


def build_backtest_band(prev_close: float, limit_pct_value: float, tick: float = DEFAULT_TICK) -> PriceBand | None:
    """Backtester already knows the symbol's limit percentage."""
    if prev_close <= 0 or limit_pct_value <= 0:
        return None
    return PriceBand(
        up=round_tick(prev_close * (1 + limit_pct_value), tick),
        down=round_tick(prev_close * (1 - limit_pct_value), tick),
        pct=limit_pct_value,
    )


def floor_to_lot(quantity: float, lot: int = CN_LOT) -> float:
    return float(int(quantity // lot) * lot)
