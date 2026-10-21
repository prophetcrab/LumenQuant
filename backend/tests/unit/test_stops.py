"""Stop-loss / take-profit / trailing-stop semantics."""

from __future__ import annotations

import pytest

from app.modules.risk.stops import StopRules, evaluate, evaluate_tick, parse_rules, update_peak


def bar(open_: float, high: float, low: float):
    return {"bar_open": open_, "bar_high": high, "bar_low": low}


def test_disabled_rules_never_trigger():
    decision = evaluate(
        entry_price=100, current_peak=100, rules=StopRules(), **bar(50, 50, 50)
    )
    assert decision.triggered is False


def test_stop_loss_triggers_on_low():
    decision = evaluate(
        entry_price=100,
        current_peak=100,
        rules=StopRules(stop_loss_pct=0.05),
        **bar(100, 101, 94),
    )
    assert decision.triggered
    assert decision.reason == "stop_loss"
    assert decision.exit_price == pytest.approx(95.0)


def test_stop_loss_gap_down_fills_at_open():
    """A gap through the stop must fill at the open, not the stop price."""
    decision = evaluate(
        entry_price=100,
        current_peak=100,
        rules=StopRules(stop_loss_pct=0.05),
        **bar(90, 91, 88),
    )
    assert decision.triggered
    assert decision.exit_price == pytest.approx(90.0)


def test_take_profit_triggers_on_high():
    decision = evaluate(
        entry_price=100,
        current_peak=100,
        rules=StopRules(take_profit_pct=0.10),
        **bar(100, 111, 99),
    )
    assert decision.triggered
    assert decision.reason == "take_profit"
    assert decision.exit_price == pytest.approx(110.0)


def test_take_profit_gap_up_fills_at_open():
    decision = evaluate(
        entry_price=100,
        current_peak=100,
        rules=StopRules(take_profit_pct=0.10),
        **bar(120, 121, 118),
    )
    assert decision.triggered
    assert decision.exit_price == pytest.approx(120.0)


def test_stop_wins_when_both_levels_touched():
    """Worst-case resolution: a bar touching stop and target is a stop."""
    decision = evaluate(
        entry_price=100,
        current_peak=100,
        rules=StopRules(stop_loss_pct=0.05, take_profit_pct=0.10),
        **bar(100, 111, 94),
    )
    assert decision.triggered
    assert decision.reason == "stop_loss"


def test_trailing_stop_tracks_the_peak():
    rules = StopRules(trailing_stop_pct=0.10)
    # Price runs to 120: peak updates, nothing triggers.
    first = evaluate(entry_price=100, current_peak=100, rules=rules, **bar(100, 120, 100))
    assert first.triggered is False
    assert first.new_peak == pytest.approx(120.0)
    # From a 120 peak, a 10% trail sits at 108; a dip to 107 exits.
    second = evaluate(entry_price=100, current_peak=first.new_peak, rules=rules, **bar(110, 111, 107))
    assert second.triggered
    assert second.reason == "trailing_stop"
    assert second.exit_price == pytest.approx(108.0)


def test_trailing_stop_not_armed_before_profit():
    """A trail must not act like a stop-loss on a fresh position."""
    decision = evaluate(
        entry_price=100,
        current_peak=100,
        rules=StopRules(trailing_stop_pct=0.10),
        **bar(100, 100.5, 95),
    )
    assert decision.triggered is False


def test_peak_only_moves_up():
    assert update_peak(100, 0, 105) == pytest.approx(105)
    assert update_peak(100, 105, 102) == pytest.approx(105)
    assert update_peak(100, 105, 130) == pytest.approx(130)
    # A peak below the entry is ignored (seeded from entry).
    assert update_peak(100, 0, 90) == pytest.approx(100)


def test_tick_variant_uses_quote_for_all_prices():
    decision = evaluate_tick(
        entry_price=100, current_peak=100, last_price=94, rules=StopRules(stop_loss_pct=0.05)
    )
    assert decision.triggered
    assert decision.exit_price == pytest.approx(94.0)


def test_parse_rules_accepts_percent_and_fraction():
    assert parse_rules(stop_loss_pct=5).stop_loss_pct == pytest.approx(0.05)
    assert parse_rules(stop_loss_pct=0.05).stop_loss_pct == pytest.approx(0.05)
    assert parse_rules(stop_loss_pct=0).stop_loss_pct == 0.0
    assert parse_rules(trailing_stop_pct=12).trailing_stop_pct == pytest.approx(0.12)


def test_zero_entry_price_is_safe():
    decision = evaluate(
        entry_price=0,
        current_peak=0,
        rules=StopRules(stop_loss_pct=0.05),
        **bar(0, 0, 0),
    )
    assert decision.triggered is False
