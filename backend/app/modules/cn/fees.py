"""A-share fee schedule: commission (with minimum), stamp duty, transfer fee.

Fees are asymmetric: stamp duty is charged on sells only, which materially
changes the economics of high-turnover strategies versus a flat bps model.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.money import round_money


@dataclass(frozen=True)
class CnFeeSchedule:
    # 万 2.5 commission is a common retail rate; the floor is the binding cost
    # for small tickets.
    commission_bps: float = 2.5
    min_commission: float = 5.0
    stamp_duty_bps: float = 5.0  # 0.05%, sell side only
    transfer_fee_bps: float = 0.1  # 0.001%, both sides

    def breakdown(self, side: str, notional: float) -> dict[str, float]:
        notional = max(0.0, float(notional))
        if notional <= 0:
            return {"commission": 0.0, "stamp_duty": 0.0, "transfer_fee": 0.0, "total": 0.0}
        commission = round_money(max(notional * self.commission_bps / 10_000.0, self.min_commission))
        stamp = round_money(notional * self.stamp_duty_bps / 10_000.0) if side == "sell" else 0.0
        transfer = round_money(notional * self.transfer_fee_bps / 10_000.0)
        return {
            "commission": commission,
            "stamp_duty": stamp,
            "transfer_fee": transfer,
            "total": round_money(commission + stamp + transfer),
        }

    def total(self, side: str, notional: float) -> float:
        return self.breakdown(side, notional)["total"]

    def as_dict(self) -> dict[str, float]:
        return {
            "commission_bps": self.commission_bps,
            "min_commission": self.min_commission,
            "stamp_duty_bps": self.stamp_duty_bps,
            "transfer_fee_bps": self.transfer_fee_bps,
        }
