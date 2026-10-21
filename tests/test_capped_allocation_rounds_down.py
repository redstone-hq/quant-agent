"""A cap-bound top-up must round DOWN, never up past the single-name cap.

The planner caps a top-up at "single-name cap minus what is already held",
then used to `round(x, 2)`, which could lift it up to 0.005 points over the
cap; the risk gate then refused the order outright (seen live, 2026-10-21).
These drive the real builders (via `construct_orders`) on both sides.
"""

from __future__ import annotations

import pytest

from src.models import Position, TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import ConstructorConfig, PortfolioConstructor

EQUITY = 100_000.0
CAP = 15.0


def _rc() -> TechReasoningChain:
    return TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x")


def _analysis(symbol: str, short: bool) -> TechAnalysisResult:
    entry, stop, target = (100.0, 120.0, 40.0) if short else (100.0, 80.0, 160.0)
    levels = [target, 150.0] if short else [stop, target]
    return TechAnalysisResult(
        symbol=symbol,
        rating="sell" if short else "buy",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[target] if short else [stop],
        resistance_levels=[stop] if short else [target],
        computed_levels=levels,
        computed_level_touches={p: 5 for p in levels},
        computed_level_bars={p: [(p, p)] for p in levels},
        atr_14=20.0 / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=_rc(),
        thesis_invalid_if="closes below support",
    )


def _position(symbol: str, held_pct: float, short: bool) -> Position:
    qty = held_pct * EQUITY / 100 / 100.0  # price 100
    qty = -qty if short else qty
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=100.0,
        current_price=100.0,
        market_value=qty * 100.0,
        unrealized_pnl=0.0,
        sector="Technology",
    )


def _target(symbol: str, short: bool) -> TargetPosition:
    if short:
        return TargetPosition(symbol=symbol, direction="short", target_weight_pct=20.0, conviction="high", thesis="t")
    return TargetPosition(symbol=symbol, risk_allocation_pct=5.0, conviction="high", thesis="t")


def _top_up(held_pct: float, short: bool):
    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=CAP))
    decisions = constructor.construct_orders(
        targets=[_target("NVDA", short)],
        positions=[_position("NVDA", held_pct, short)],
        analyses=[_analysis("NVDA", short)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    return decisions[0]


@pytest.mark.parametrize("short", [False, True], ids=["long", "short"])
@pytest.mark.parametrize(
    "room, expected",
    [(3.2551, 3.25), (3.2549, 3.25), (3.2599, 3.25), (3.2501, 3.25)],
)
def test_capped_top_up_never_exceeds_the_cap_after_rounding(short, room, expected):
    held = CAP - room
    decision = _top_up(held, short)
    assert decision.allocation_pct == expected
    assert held + decision.allocation_pct <= CAP


@pytest.mark.parametrize("short", [False, True], ids=["long", "short"])
def test_room_exactly_two_decimals_is_not_dropped_a_cent(short):
    decision = _top_up(CAP - 3.25, short)
    assert decision.allocation_pct == 3.25


@pytest.mark.parametrize("short", [False, True], ids=["long", "short"])
def test_uncapped_allocation_keeps_its_ordinary_rounding(short):
    constructor = PortfolioConstructor(ConstructorConfig(max_position_pct=CAP))
    target = TargetPosition(
        symbol="NVDA",
        direction="short" if short else "long",
        target_weight_pct=3.2551,
        conviction="high",
        thesis="t",
    )
    decisions = constructor.construct_orders(
        targets=[target],
        positions=[],
        analyses=[_analysis("NVDA", short)],
        total_value=EQUITY,
        price_map={"NVDA": 100.0},
    )
    assert len(decisions) == 1
    assert decisions[0].allocation_pct == 3.26
