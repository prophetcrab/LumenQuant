"""Numeric helpers. Paper-trading math is float based with explicit rounding."""

from __future__ import annotations

PRICE_DECIMALS = 8
QTY_DECIMALS = 8
MONEY_DECIMALS = 8


def round_price(value: float) -> float:
    return round(float(value), PRICE_DECIMALS)


def round_qty(value: float) -> float:
    return round(float(value), QTY_DECIMALS)


def round_money(value: float) -> float:
    return round(float(value), MONEY_DECIMALS)


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default
