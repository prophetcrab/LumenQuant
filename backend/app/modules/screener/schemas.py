"""Screener request/response contracts.

Units are chosen for the person reading the form, not for the upstream feed:
成交额 and 市值 are in 亿元, percentages are in percent points. The source returns
元 and 万元 respectively, and `service.py` normalises once.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Boards the filter understands; "" (empty list) means "every board".
BOARDS = ("main", "gem", "star", "bse")

SORT_FIELDS = (
    "change_pct",
    "amount",
    "turnover",
    "pe",
    "pb",
    "market_cap",
    "price",
)


class ScreenFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boards: list[str] = Field(default_factory=list)
    exclude_st: bool = True
    # A brand-new listing has no mean-reversion history and often trades at the
    # limit for days; including it by default would flood every "strong" screen.
    min_listed_days: int | None = Field(default=None, ge=0)

    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    change_pct_min: float | None = None
    change_pct_max: float | None = None
    # 亿元
    amount_min: float | None = Field(default=None, ge=0)
    turnover_min: float | None = Field(default=None, ge=0)
    turnover_max: float | None = Field(default=None, ge=0)
    pe_min: float | None = None
    pe_max: float | None = None
    pb_min: float | None = None
    pb_max: float | None = None
    # 亿元
    market_cap_min: float | None = Field(default=None, ge=0)
    market_cap_max: float | None = Field(default=None, ge=0)

    sort_by: str = "change_pct"
    ascending: bool = False
    limit: int = Field(default=50, ge=1, le=500)
    with_technicals: bool = False

    def normalised_boards(self) -> set[str]:
        wanted = {b.strip().lower() for b in self.boards if b and b.strip()}
        unknown = wanted - set(BOARDS)
        if unknown:
            raise ValueError(f"unknown board(s): {', '.join(sorted(unknown))}")
        return wanted


class ScreenPreset(BaseModel):
    key: str
    name: str
    description: str
    filter: ScreenFilter


class CandidateOut(BaseModel):
    code: str
    name: str
    board: str
    is_st: bool
    price: float
    change_pct: float
    amount_yi: float
    turnover_pct: float
    pe: float | None
    pb: float | None
    market_cap_yi: float
    float_cap_yi: float
    # Populated only when `with_technicals` is requested and candles are stored.
    technicals: dict | None = None


class ScreenResponse(BaseModel):
    total_universe: int
    matched: int
    returned: int
    source: str
    latency_ms: float
    criteria: dict
    candidates: list[CandidateOut]
    warnings: list[str] = Field(default_factory=list)


class AddSymbolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=12)
    name: str = ""


class AddBatchRequest(BaseModel):
    """Register several screened names in one action.

    The UI offers 50 candidates; clicking "add" fifty times at ~1.5s each is two
    minutes of waiting for something the backend can do in a single sync pass.
    """

    model_config = ConfigDict(extra="forbid")

    codes: list[str] = Field(min_length=1, max_length=50)
    sync: bool = True
