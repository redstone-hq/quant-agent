"""Owner rule 2026-10-09: a stock with no chart level still trades.

A chart that was MEASURED and holds no structural level trades on the
2.5 ATR unbacked stop with no take-profit, managed as a trend trade. A missing
number is never invented. An UNUSABLE price history is a data fault and still
does not trade, under its own reason code. A wrong-side analyst guess is
flagged, not dropped.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.agents.position_reviewer import PositionReviewerAgent
from src.data.levels import (
    BASIS_NO_TARGET,
    COVERAGE_MEASURED,
    COVERAGE_UNUSABLE_BARS,
    FAULT_NO_STRUCTURE,
    REFUSAL_NO_STRUCTURE,
    derive_structural_target,
)
from src.models import Position, TargetPosition, TechAnalysisResult, TradeDecision
from src.models.analysis import TechReasoningChain
from src.portfolio_constructor import PortfolioConstructor
from src.portfolio_constructor.target_derivation import NO_TARGET_ROW_TAG
from src.stage_execution_parts.entry_geometry import entry_stop_price


def _analysis(*, rating="buy", entry=100.0, stop=99.0, target=130.0, atr=2.0, computed=None):
    return TechAnalysisResult(
        symbol="NVDA",
        rating=rating,
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[90.0],
        resistance_levels=[],
        computed_levels=[] if computed is None else computed,
        atr_14=atr,
        setup_type="range",
        expected_horizon_sessions=20,
        reasoning_chain=TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x"),
        thesis_invalid_if="closes below support",
    )


def _target():
    return TargetPosition(
        symbol="NVDA",
        direction="long",
        target_weight_pct=15.0,
        conviction="high",
        thesis="t",
    )


def test_a_no_level_chart_ships_an_order_on_the_atr_stop_with_no_target(caplog):
    analysis = _analysis()
    analysis.levels_coverage = COVERAGE_MEASURED
    constructor = PortfolioConstructor()
    with caplog.at_level(logging.WARNING):
        decisions = constructor.construct_orders(
            targets=[_target()],
            positions=[],
            analyses=[analysis],
            total_value=100_000,
            price_map={"NVDA": 100.0},
        )
    buys = [d for d in decisions if d.action == "BUY"]
    assert len(buys) == 1, constructor.last_drop_reasons
    order = buys[0]
    assert order.take_profit is None
    # 2.5 ATR unbacked stop: 100 - 2.5 * 2.0.
    assert order.stop_loss == 95.0
    assert "target ABSENT" in order.reasoning
    assert constructor.last_data_faults == {}
    assert "NVDA" not in constructor.last_refusals
    rows = [r.getMessage() for r in caplog.records if r.getMessage().startswith(NO_TARGET_ROW_TAG)]
    assert len(rows) == 1 and '"symbol": "NVDA"' in rows[0]


def test_the_derivation_marks_it_a_trend_trade_and_not_a_refusal():
    result = derive_structural_target(
        entry_price=100.0,
        direction="long",
        levels=[],
        atr=2.0,
        horizon_sessions=20,
        setup_type="range",
        levels_coverage=COVERAGE_MEASURED,
    )
    assert result.price is None
    assert result.no_target and not result.refused
    assert result.basis == BASIS_NO_TARGET
    assert result.level_used is None  # -> structural_ceiling=False -> trend trade
    assert result.refusal == "" and result.fault == ""


def test_unusable_history_still_fails_under_its_own_reason():
    result = derive_structural_target(
        entry_price=100.0,
        direction="long",
        levels=[],
        atr=2.0,
        horizon_sessions=20,
        setup_type="range",
        levels_coverage=COVERAGE_UNUSABLE_BARS,
    )
    assert result.refused and result.fault == FAULT_NO_STRUCTURE
    assert not result.no_target
    assert FAULT_NO_STRUCTURE != BASIS_NO_TARGET != REFUSAL_NO_STRUCTURE


def test_a_wrong_side_analyst_guess_is_flagged_not_dropped():
    analysis = _analysis(target=97.0)  # below entry on a buy
    assert analysis.reference_target_wrong_side is True
    labels = [e.label for e in analysis.to_verdict().evidence]
    assert "reference_target_wrong_side" in labels
    assert _analysis(target=130.0).reference_target_wrong_side is False


def test_decision_and_execution_accept_a_missing_target():
    decision = TradeDecision(
        action="BUY",
        symbol="NVDA",
        allocation_pct=5.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=None,
        reasoning="no level",
    )
    assert decision.take_profit is None
    ctx = SimpleNamespace(symbols_bars={})
    # sizing above the decision entry changes the geometry -> the take_profit branch runs.
    assert entry_stop_price(ctx, decision, False, 101.0) == 95.0


def test_the_reviewer_prompt_says_the_target_is_absent():
    agent = PositionReviewerAgent(api_key="k", model="claude-opus-4-7", max_tokens=1024)
    position = Position(
        symbol="NVDA",
        qty=10,
        avg_entry=100.0,
        current_price=101.0,
        market_value=1010.0,
        unrealized_pnl=10.0,
        sector="Tech",
    )
    msg = agent.build_user_message(
        positions=[position],
        macro_summary={},
        cash_balance=1000.0,
        total_value=2010.0,
        position_facts={"NVDA": {"distance_to_stop_pct": 5.0}},
    )
    assert "Reference target: absent" in msg
    assert "$0.00" not in msg.split("Reference target")[1].splitlines()[0]
