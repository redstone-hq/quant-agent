"""Deterministic risk arithmetic — R-multiple, per-position risk, portfolio heat.

Covers `src/risk/metrics.py` (spec Phase 2 §2.3/§2.4, audit §1.3/§1.4).
"""

import math

import pytest

from src.models import Position
from src.risk.metrics import (
    PortfolioHeat,
    format_heat_block,
    portfolio_heat,
    position_risk,
    r_multiple,
)


def _pos(symbol: str, qty: float, entry: float, price: float) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=entry,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=qty * (price - entry),
        sector="Technology",
    )


# --------------------------------------------------------------------------
# R-multiple (audit §1.4)
# --------------------------------------------------------------------------


def test_r_multiple_is_profit_in_units_of_risk_taken():
    # Risked $10/share (100 → 90), now +$20 → 2R.
    assert r_multiple(current_price=120.0, entry=100.0, initial_stop=90.0) == 2.0


def test_r_multiple_is_negative_while_underwater():
    assert r_multiple(current_price=95.0, entry=100.0, initial_stop=90.0) == -0.5


def test_r_multiple_is_none_when_initial_stop_was_not_below_entry():
    """No risk defined at entry means no denominator — never fabricate one."""
    assert r_multiple(current_price=120.0, entry=100.0, initial_stop=100.0) is None
    assert r_multiple(current_price=120.0, entry=100.0, initial_stop=110.0) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "n/a"])
def test_r_multiple_rejects_non_finite_inputs(bad):
    assert r_multiple(current_price=bad, entry=100.0, initial_stop=90.0) is None
    assert r_multiple(current_price=120.0, entry=bad, initial_stop=90.0) is None
    assert r_multiple(current_price=120.0, entry=100.0, initial_stop=bad) is None


def test_r_multiple_uses_the_entry_stop_not_the_trailed_one():
    """The denominator is the bet that was actually made."""
    risk = position_risk(
        symbol="AAA",
        qty=10,
        entry=100.0,
        current_price=130.0,
        stop=125.0,  # trailed up
        initial_stop=90.0,  # the bet was $10/share
    )
    assert risk.r_multiple == 3.0


# --------------------------------------------------------------------------
# Per-position risk and the release rule (spec §2.3)
# --------------------------------------------------------------------------


def test_budget_risk_is_measured_against_entry_not_current_price():
    risk = position_risk("AAA", qty=100, entry=50.0, current_price=60.0, stop=45.0)
    assert risk.budget_risk_dollars == pytest.approx(500.0)  # 100 × (50 − 45)
    assert risk.open_risk_dollars == pytest.approx(1500.0)  # 100 × (60 − 45)
    assert risk.risk_released is False


def test_risk_is_released_once_the_stop_reaches_entry():
    """A stop at or above entry can no longer lose money vs cost basis."""
    at_entry = position_risk("AAA", qty=100, entry=50.0, current_price=60.0, stop=50.0)
    assert at_entry.risk_released is True
    assert at_entry.budget_risk_dollars == 0.0
    # ...but it still carries open risk from today's price.
    assert at_entry.open_risk_dollars == pytest.approx(1000.0)

    above = position_risk("AAA", qty=100, entry=50.0, current_price=60.0, stop=55.0)
    assert above.risk_released is True
    assert above.budget_risk_dollars == 0.0


def test_unprotected_position_is_charged_full_notional_not_zero():
    """No stop is not no risk. Scoring it 0 would rank the riskiest book safest."""
    risk = position_risk("AAA", qty=100, entry=50.0, current_price=60.0, stop=None)
    assert risk.protected is False
    assert risk.risk_released is False
    assert risk.budget_risk_dollars == pytest.approx(6000.0)
    assert risk.open_risk_dollars == pytest.approx(6000.0)


def test_zero_or_negative_stop_is_treated_as_no_stop():
    for bad_stop in (0.0, -5.0):
        risk = position_risk("AAA", qty=10, entry=50.0, current_price=50.0, stop=bad_stop)
        assert risk.protected is False


def test_open_risk_floors_at_zero_when_price_is_below_the_stop():
    """A gapped-through stop cannot report negative heat."""
    risk = position_risk("AAA", qty=100, entry=50.0, current_price=40.0, stop=45.0)
    assert risk.open_risk_dollars == 0.0
    assert risk.budget_risk_dollars == pytest.approx(500.0)


def test_initial_stop_defaults_to_the_live_stop():
    risk = position_risk("AAA", qty=10, entry=100.0, current_price=110.0, stop=90.0)
    assert risk.initial_stop == 90.0
    assert risk.r_multiple == 1.0


def test_non_finite_broker_price_does_not_poison_the_arithmetic():
    risk = position_risk("AAA", qty=10, entry=100.0, current_price=float("nan"), stop=90.0)
    assert math.isfinite(risk.budget_risk_dollars)
    assert math.isfinite(risk.open_risk_dollars)


# --------------------------------------------------------------------------
# Portfolio heat (audit §1.3)
# --------------------------------------------------------------------------


def test_portfolio_heat_sums_budget_and_open_risk():
    heat = portfolio_heat(
        positions=[_pos("AAA", 100, 50.0, 60.0), _pos("BBB", 50, 20.0, 22.0)],
        equity=100_000.0,
        stops={"AAA": 45.0, "BBB": 18.0},
    )
    assert heat.budget_risk_dollars == pytest.approx(600.0)  # 500 + 100
    assert heat.budget_risk_pct == pytest.approx(0.6)
    assert heat.open_risk_dollars == pytest.approx(1700.0)  # 1500 + 200
    assert heat.open_risk_pct == pytest.approx(1.7)


def test_portfolio_heat_headroom_against_the_ratified_ceiling():
    heat = portfolio_heat(
        positions=[_pos("AAA", 1000, 50.0, 60.0)],
        equity=100_000.0,
        stops={"AAA": 45.0},  # $5,000 at risk = 5%
    )
    assert heat.budget_risk_pct == pytest.approx(5.0)
    assert heat.headroom_pct(25.0) == pytest.approx(20.0)


def test_headroom_never_goes_negative():
    heat = portfolio_heat(
        positions=[_pos("AAA", 10_000, 50.0, 60.0)],
        equity=100_000.0,
        stops={"AAA": 45.0},  # $50,000 at risk = 50%, over the ceiling
    )
    assert heat.budget_risk_pct == pytest.approx(50.0)
    assert heat.headroom_pct(25.0) == 0.0


def test_released_winners_free_budget_so_the_book_can_expand():
    """Spec §2.3 — the point of the whole release rule."""
    stops = {"WIN": 45.0, "NEW": 18.0}
    before = portfolio_heat(
        positions=[_pos("WIN", 100, 50.0, 70.0), _pos("NEW", 50, 20.0, 22.0)],
        equity=100_000.0,
        stops=stops,
    )
    stops["WIN"] = 55.0  # trail above entry
    after = portfolio_heat(
        positions=[_pos("WIN", 100, 50.0, 70.0), _pos("NEW", 50, 20.0, 22.0)],
        equity=100_000.0,
        stops=stops,
    )
    assert after.budget_risk_dollars < before.budget_risk_dollars
    assert after.released == ["WIN"]
    assert after.headroom_pct(25.0) > before.headroom_pct(25.0)


def test_positions_without_a_known_stop_are_listed_as_unprotected():
    heat = portfolio_heat(
        positions=[_pos("AAA", 100, 50.0, 60.0), _pos("BBB", 50, 20.0, 22.0)],
        equity=100_000.0,
        stops={"AAA": 45.0},  # BBB has none
    )
    assert heat.unprotected == ["BBB"]
    assert heat.budget_risk_dollars == pytest.approx(500.0 + 1100.0)


def test_cash_equivalent_sweep_is_excluded_not_counted_as_unprotected():
    """SGOV is deliberately stopless; it is not a risk position."""
    heat = portfolio_heat(
        positions=[_pos("AAA", 100, 50.0, 60.0), _pos("SGOV", 89, 100.0, 100.5)],
        equity=100_000.0,
        stops={"AAA": 45.0},
        exclude_symbols={"SGOV"},
    )
    assert heat.unprotected == []
    assert [p.symbol for p in heat.per_position] == ["AAA"]


def test_closed_and_zero_qty_positions_are_skipped():
    heat = portfolio_heat(
        positions=[_pos("AAA", 0, 50.0, 60.0)],
        equity=100_000.0,
        stops={"AAA": 45.0},
    )
    assert heat.per_position == []
    assert heat.budget_risk_pct == 0.0


def test_zero_equity_does_not_divide_by_zero():
    heat = portfolio_heat(
        positions=[_pos("AAA", 100, 50.0, 60.0)],
        equity=0.0,
        stops={"AAA": 45.0},
    )
    assert heat.budget_risk_pct == 0.0
    assert heat.open_risk_pct == 0.0


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


def test_format_heat_block_states_headroom_and_flags():
    heat = portfolio_heat(
        positions=[
            _pos("AAA", 100, 50.0, 60.0),
            _pos("WIN", 100, 50.0, 70.0),
            _pos("BBB", 50, 20.0, 22.0),
        ],
        equity=100_000.0,
        stops={"AAA": 45.0, "WIN": 55.0},  # WIN released, BBB unprotected
    )
    block = format_heat_block(heat, ceiling_pct=25.0)
    assert "headroom" in block
    assert "RELEASED" in block and "WIN" in block
    assert "UNPROTECTED" in block and "BBB" in block
    assert "AAA" in block


def test_format_heat_block_on_an_empty_book_states_full_budget():
    block = format_heat_block(PortfolioHeat(equity=100_000.0), ceiling_pct=25.0)
    assert "No risk-bearing positions" in block
    assert "25%" in block
