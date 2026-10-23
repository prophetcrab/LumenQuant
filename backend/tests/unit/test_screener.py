"""A-share screener: normalisation, filtering and ordering.

The source is stubbed with recorded-shaped rows so the tests pin the field mapping
(Sina reports 成交额 in 元 and 市值 in 万元) and the filter semantics without
touching the network.
"""

from __future__ import annotations

import pytest

from app.modules.screener.schemas import ScreenFilter
from app.modules.screener.service import ScreenerService, board_of, preset_filters
from app.modules.screener.sources.sina import SinaMarketSource, SinaUnavailable


def row(
    *,
    symbol: str,
    code: str,
    name: str,
    trade: float,
    change: float,
    amount: float,
    turnover: float,
    per: str | float = "",
    pb: float = 1.0,
    mktcap: float = 1_000_000.0,
    nmc: float = 800_000.0,
) -> dict:
    """One Sina-shaped row. Defaults describe an ordinary mid-cap."""
    return {
        "symbol": symbol,
        "code": code,
        "name": name,
        "trade": str(trade),
        "changepercent": change,
        "amount": amount,
        "turnoverratio": turnover,
        "per": per,
        "pb": pb,
        "mktcap": mktcap,
        "nmc": nmc,
    }


class StubSource:
    name = "stub"

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls = 0

    def snapshot(self, *, sort: str = "symbol", max_rows: int | None = None) -> list[dict]:
        self.calls += 1
        return list(self.rows)


def service(rows: list[dict]) -> ScreenerService:
    return ScreenerService(StubSource(rows))


def test_board_is_derived_from_the_code_prefix():
    assert board_of("600519.SH") == "main"
    assert board_of("000001.SZ") == "main"
    assert board_of("300750.SZ") == "gem"
    assert board_of("301001.SZ") == "gem"
    assert board_of("688981.SH") == "star"
    assert board_of("920285.BJ") == "bse"


def test_units_are_converted_once():
    """成交额 元 -> 亿元, 市值 万元 -> 亿元.

    Getting this wrong is silent: a 2 亿 minimum would either match everything or
    nothing, and the number shown in the table would be off by 10,000x.
    """
    svc = service(
        [
            row(
                symbol="sh600519", code="600519", name="贵州茅台",
                trade=1318.0, change=1.0,
                amount=7_677_216_283.0,      # 元  -> 76.8 亿
                turnover=0.46,
                per=20.49, pb=6.64,
                mktcap=166_885_900.0,        # 万元 -> 16688.6 亿
                nmc=166_885_900.0,
            )
        ]
    )
    result = svc.screen(ScreenFilter())
    item = result["candidates"][0]
    assert item["amount_yi"] == pytest.approx(76.77, abs=0.01)
    assert item["market_cap_yi"] == pytest.approx(16688.59, abs=0.01)
    assert item["pe"] == pytest.approx(20.49)


def test_suspended_rows_are_dropped():
    """A halted name reports trade=0; screening it would match 'price below X'."""
    svc = service(
        [
            row(symbol="sh600519", code="600519", name="停牌股", trade=0.0,
                change=0.0, amount=0.0, turnover=0.0),
            row(symbol="sh600036", code="600036", name="正常股", trade=40.0,
                change=1.0, amount=1e8, turnover=1.0),
        ]
    )
    result = svc.screen(ScreenFilter())
    assert result["total_universe"] == 1
    assert result["candidates"][0]["code"] == "600036.SH"


def test_st_is_excluded_by_default_and_can_be_included():
    rows = [
        row(symbol="sz000001", code="000001", name="*ST海航", trade=3.0,
            change=1.0, amount=1e8, turnover=2.0),
        row(symbol="sh600036", code="600036", name="招商银行", trade=40.0,
            change=1.0, amount=1e8, turnover=2.0),
    ]
    svc = service(rows)
    assert [c["code"] for c in svc.screen(ScreenFilter())["candidates"]] == ["600036.SH"]
    included = svc.screen(ScreenFilter(exclude_st=False))
    assert {c["code"] for c in included["candidates"]} == {"000001.SZ", "600036.SH"}


def test_pe_filter_excludes_names_without_a_pe():
    """'PE below 20' must not silently include loss-makers.

    Sina reports `per` as empty (or 0) for a company with negative earnings. Those
    rows have no PE, so a naive `value <= 20` check would treat them as the
    cheapest — the exact opposite of what the filter means.
    """
    rows = [
        row(symbol="sz000001", code="000001", name="亏损股", trade=10.0,
            change=0.0, amount=1e8, turnover=1.0, per="", pb=2.0),
        row(symbol="sh600036", code="600036", name="盈利股", trade=40.0,
            change=0.0, amount=1e8, turnover=1.0, per=12.0, pb=1.2),
    ]
    svc = service(rows)
    result = svc.screen(ScreenFilter(pe_max=20.0))
    assert [c["code"] for c in result["candidates"]] == ["600036.SH"]


def test_board_filter_restricts_to_the_selected_boards():
    rows = [
        row(symbol="sh600519", code="600519", name="主板", trade=1300.0,
            change=1.0, amount=1e9, turnover=1.0),
        row(symbol="sz300750", code="300750", name="创业板", trade=230.0,
            change=1.0, amount=1e9, turnover=1.0),
        row(symbol="sh688981", code="688981", name="科创板", trade=100.0,
            change=1.0, amount=1e9, turnover=1.0),
    ]
    svc = service(rows)
    result = svc.screen(ScreenFilter(boards=["gem", "star"]))
    assert {c["board"] for c in result["candidates"]} == {"gem", "star"}


def test_missing_values_sort_last_in_both_directions():
    """A loss-maker must not win a 'cheapest PE' screen in either order."""
    rows = [
        row(symbol="sz000001", code="000001", name="无PE", trade=10.0,
            change=0.0, amount=1e8, turnover=1.0, per=""),
        row(symbol="sh600036", code="600036", name="PE低", trade=40.0,
            change=0.0, amount=1e8, turnover=1.0, per=8.0, pb=1.0),
        row(symbol="sh601318", code="601318", name="PE高", trade=50.0,
            change=0.0, amount=1e8, turnover=1.0, per=25.0, pb=1.0),
    ]
    svc = service(rows)
    worst_first = svc.screen(ScreenFilter(sort_by="pe", ascending=False))
    assert worst_first["candidates"][0]["code"] == "601318.SH"
    assert worst_first["candidates"][-1]["pe"] is None

    best_first = svc.screen(ScreenFilter(sort_by="pe", ascending=True))
    assert best_first["candidates"][0]["code"] == "600036.SH"
    assert best_first["candidates"][-1]["pe"] is None


def test_unknown_board_is_rejected():
    with pytest.raises(ValueError):
        service([row(symbol="sh600519", code="600519", name="x", trade=1.0,
                      change=0.0, amount=1e8, turnover=1.0)]).screen(
            ScreenFilter(boards=["nasdaq"])
        )


def test_universe_is_cached_between_screens():
    """Tweaking a filter must not re-download the market."""
    from app.core.cache import TTLCache

    stub = StubSource(
        [row(symbol="sh600036", code="600036", name="招商银行", trade=40.0,
             change=1.0, amount=1e8, turnover=2.0, per=8.0)]
    )
    svc = ScreenerService(stub, cache=TTLCache(ttl_seconds=60.0, max_entries=4))
    svc.screen(ScreenFilter())
    svc.screen(ScreenFilter(price_min=10.0))
    assert stub.calls == 1


def test_presets_are_all_screenable():
    """Every shipped preset must produce a valid filter for the engine."""
    svc = service(
        [row(symbol="sh600036", code="600036", name="招商银行", trade=40.0,
             change=5.0, amount=6e8, turnover=9.0, per=8.0, pb=1.2,
             mktcap=900_000_000.0, nmc=900_000_000.0)]
    )
    for preset in preset_filters():
        result = svc.screen(preset["filter"])
        assert result["total_universe"] == 1
        assert preset["key"] == preset["key"]
        assert preset["description"]


def test_empty_upstream_raises_instead_of_returning_silence():
    svc = ScreenerService(StubSource([]))
    with pytest.raises(SinaUnavailable):
        svc.screen(ScreenFilter())


def test_numeric_strings_and_blanks_are_tolerated():
    """The feed mixes numbers and strings, and blanks mean 'unknown'."""
    rows = [
        row(symbol="sh600036", code="600036", name="招商银行", trade=40.0,
            change=1.0, amount="1.5e8", turnover="2.5", per="8.5", pb=1.0),
        {  # sparse row: everything optional missing
            "symbol": "sz000001", "code": "000001", "name": "稀疏",
            "trade": "11.3", "changepercent": "-1.2",
        },
    ]
    svc = service(rows)
    result = svc.screen(ScreenFilter())
    assert result["total_universe"] == 2
    by_code = {c["code"]: c for c in result["candidates"]}
    assert by_code["600036.SH"]["pe"] == pytest.approx(8.5)
    assert by_code["000001.SZ"]["amount_yi"] == 0.0
    assert by_code["000001.SZ"]["pe"] is None


def test_every_sort_field_actually_orders_the_rows():
    """The request's field names must map onto the keys the rows carry.

    The rows hold `amount_yi` / `turnover_pct` / `market_cap_yi` (the unit is part
    of the name), while the request says "amount" / "turnover" / "market_cap".
    Treating the two as identical made every sort a silent no-op: the table came
    back in source order while `criteria.sort_by` echoed what was asked for.
    """
    rows = []
    for index, (amount, turnover, cap, pe, pb, price) in enumerate(
        [
            (3.0, 1.0, 500.0, 30.0, 3.0, 10.0),
            (1.0, 5.0, 100.0, 10.0, 1.0, 50.0),
            (2.0, 3.0, 300.0, 20.0, 2.0, 30.0),
        ]
    ):
        rows.append(
            row(
                symbol=f"sh60000{index}", code=f"60000{index}", name=f"股票{index}",
                trade=price, change=float(index), amount=amount * 1e8,
                turnover=turnover, per=pe, pb=pb,
                mktcap=cap * 1_0000, nmc=cap * 1_0000,
            )
        )
    svc = service(rows)

    expected_desc = {
        "amount": [3.0, 2.0, 1.0],
        "turnover": [5.0, 3.0, 1.0],
        "market_cap": [500.0, 300.0, 100.0],
        "pe": [30.0, 20.0, 10.0],
        "pb": [3.0, 2.0, 1.0],
        "price": [50.0, 30.0, 10.0],
        "change_pct": [2.0, 1.0, 0.0],
    }
    observed = {
        "amount": [c["amount_yi"] for c in svc.screen(ScreenFilter(sort_by="amount"))["candidates"]],
        "turnover": [c["turnover_pct"] for c in svc.screen(ScreenFilter(sort_by="turnover"))["candidates"]],
        "market_cap": [c["market_cap_yi"] for c in svc.screen(ScreenFilter(sort_by="market_cap"))["candidates"]],
        "pe": [c["pe"] for c in svc.screen(ScreenFilter(sort_by="pe"))["candidates"]],
        "pb": [c["pb"] for c in svc.screen(ScreenFilter(sort_by="pb"))["candidates"]],
        "price": [c["price"] for c in svc.screen(ScreenFilter(sort_by="price"))["candidates"]],
        "change_pct": [c["change_pct"] for c in svc.screen(ScreenFilter(sort_by="change_pct"))["candidates"]],
    }
    for field, values in expected_desc.items():
        assert values == pytest.approx(observed[field]), field
