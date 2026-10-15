"""Invented reward:risk leftover gates are gone (owner 2026-09-17).

The universal 1.5 entry refuse was already retired. What remained on live
main, and killed RSG on 2026-09-16, was:

  * execution skip ``geometry_rr`` against ``EXECUTION_REWARD_RISK_BELT = 1.2``
  * PM starter-size cap when a measurable range R/R was under 1.5

Overnight bind: unmeasurable payoff honesty may stay as a recorded fact /
ranking hint with ZERO refuse, ZERO size floor, ZERO sub-floor branch.
A computed ratio cannot skip or shrink a ticket. An unmeasurable one
cannot either.
"""

from __future__ import annotations

from pathlib import Path

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import PortfolioDecision, TechAnalysisResult, TechReasoningChain, TradeDecision
from src.pipeline_stages import _execution_payoff_skip_reason
from src.risk.constants import STARTER_POSITION_RISK_PCT

REPO = Path(__file__).resolve().parents[1]
STAGES = (REPO / "src" / "pipeline_stages.py").read_text()

EQUITY = 100_000.0
ENTRY = 100.0
STOP = 95.0


def _rc() -> TechReasoningChain:
    return TechReasoningChain(
        trend="x",
        momentum="x",
        volatility="x",
        volume="x",
        support_resistance="x",
    )


def _analysis(symbol: str, *, setup_type: str = "range") -> TechAnalysisResult:
    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        conviction="medium",
        entry_price=ENTRY,
        stop_loss=STOP,
        reference_target=102.0,
        support_levels=[STOP],
        resistance_levels=[102.0],
        computed_levels=[STOP, 102.0],
        atr_14=(ENTRY - STOP) / 3.5,
        setup_type=setup_type,
        expected_horizon_sessions=60,
        reasoning="validated production-like trend and momentum evidence",
        reasoning_chain=_rc(),
        thesis_invalid_if="closes below support",
    )


def _decision(symbol: str, *, risk: float = 2.5) -> PortfolioDecision:
    return PortfolioDecision.model_validate(
        {
            "reasoning_chain": {
                "macro_filter": "Macro checked.",
                "news_check": "News checked.",
                "earnings_check": "Earnings checked.",
                "signal_conflicts": "None material.",
                "sizing_logic": "Sizing checked.",
                "portfolio_balance": "Book checked.",
                "cash_target": "Cash checked.",
            },
            "targets": [
                {
                    "symbol": symbol,
                    "conviction": "medium",
                    "direction": "long",
                    "thesis": f"{symbol} setup.",
                    "catalyst": "",
                    "risk_allocation_pct": risk,
                    "provenance": [
                        {
                            "source": "technical",
                            "observed_stance": "buy",
                            "relationship": "supports",
                            "evidence": "current-run rating",
                        }
                    ],
                }
            ],
            "portfolio_view": "Test decision.",
        }
    )


def test_geometry_rr_is_not_written_as_an_execution_skip_reason():
    """The RSG skip token must not be emit-able any more."""
    assert '"geometry_rr"' not in STAGES
    assert "'geometry_rr'" not in STAGES


def test_geometry_unmeasurable_is_not_written_as_an_execution_skip_reason():
    """Renaming the belt is also a defect."""
    assert '"geometry_unmeasurable"' not in STAGES
    assert "'geometry_unmeasurable'" not in STAGES


def test_the_retired_1_2_belt_is_not_importable():
    import src.pipeline_stages as stages

    assert not hasattr(stages, "EXECUTION_REWARD_RISK_BELT")
    assert not hasattr(stages, "_execution_rr_floor")
    assert not hasattr(stages, "GEOMETRY_UNMEASURABLE_SKIP")


def test_rsg_like_executed_ratio_below_the_retired_belt_does_not_skip():
    """RSG 2026-09-16: executed 0.45 vs approved 0.81, former belt 1.2.

    Geometry changed (limit raised). The computed ratio is thin. That is
    not a skip.
    """
    order = TradeDecision(
        action="BUY",
        symbol="RSG",
        allocation_pct=5.0,
        entry_price=223.99,
        stop_loss=218.51,
        take_profit=228.46,
        reasoning="r",
        setup_type="range",
    )
    assert order.reward_risk is not None
    assert order.reward_risk < 1.2
    reason = _execution_payoff_skip_reason(
        order,
        sizing_price=225.37,
        stop_price=218.51,
        geometry_changed=True,
        is_short=False,
    )
    assert reason is None


def test_a_range_buy_does_not_skip_when_executed_payoff_cannot_be_computed():
    order = TradeDecision(
        action="BUY",
        symbol="AAA",
        allocation_pct=5.0,
        entry_price=ENTRY,
        stop_loss=STOP,
        take_profit=110.0,
        reasoning="r",
        setup_type="range",
    )
    reason = _execution_payoff_skip_reason(
        order,
        sizing_price=ENTRY,
        stop_price=ENTRY,  # stop at entry → ratio unmeasurable
        geometry_changed=True,
        is_short=False,
    )
    assert reason is None


def test_a_breakout_is_not_skipped_even_when_executed_payoff_is_unmeasurable():
    order = TradeDecision(
        action="BUY",
        symbol="AAA",
        allocation_pct=5.0,
        entry_price=ENTRY,
        stop_loss=STOP,
        take_profit=110.0,
        reasoning="r",
        setup_type="breakout",
    )
    reason = _execution_payoff_skip_reason(
        order,
        sizing_price=ENTRY,
        stop_price=ENTRY,
        geometry_changed=True,
        is_short=False,
    )
    assert reason is None


def test_measurable_range_under_1_5_keeps_the_pm_asked_size():
    """The retired starter-size cap must not shrink a computed ratio."""
    result = PortfolioManagerAgent._apply_subfloor_catalyst_rule(
        _decision("RSG", risk=2.5),
        analyses=[_analysis("RSG")],
        positions=[],
        total_value=EQUITY,
        active_state_changes="",
        starter_risk_pct=STARTER_POSITION_RISK_PCT,
        real_reward_risk_by_symbol={"RSG": 0.81},
    )
    assert [t.symbol for t in result.targets] == ["RSG"]
    assert result.targets[0].risk_allocation_pct == 2.5
    assert result.targets[0].risk_allocation_pct != STARTER_POSITION_RISK_PCT


def test_an_unmeasurable_range_without_catalyst_keeps_the_asked_size():
    result = PortfolioManagerAgent._apply_subfloor_catalyst_rule(
        _decision("RSG", risk=2.5),
        analyses=[_analysis("RSG")],
        positions=[],
        total_value=EQUITY,
        active_state_changes="",
        starter_risk_pct=STARTER_POSITION_RISK_PCT,
        real_reward_risk_by_symbol={"RSG": None},
    )
    assert [t.symbol for t in result.targets] == ["RSG"]
    assert result.targets[0].risk_allocation_pct == 2.5
    assert result.targets[0].subfloor_catalyst_verified is False


def test_rr_floor_argument_cannot_reinstate_the_size_cap():
    """Passing 99.0 as rr_floor used to cap everything measurable under it."""
    result = PortfolioManagerAgent._apply_subfloor_catalyst_rule(
        _decision("AAA", risk=3.0),
        analyses=[_analysis("AAA")],
        positions=[],
        total_value=EQUITY,
        active_state_changes="",
        starter_risk_pct=STARTER_POSITION_RISK_PCT,
        real_reward_risk_by_symbol={"AAA": 3.0},
    )
    assert result.targets[0].risk_allocation_pct == 3.0


def test_risk_seat_not_told_thin_ratio_was_paid_for_in_size():
    """The tech-signals block once said a thin range ratio 'has already been
    paid for in size by the constructor'. The constructor computes the ratio
    for ranking/logging and resizes nothing on it; the seat must be told so."""
    src = (Path(__file__).resolve().parents[1] / "src/agents/risk_manager.py").read_text()
    assert "paid for in size" not in src
    flat = " ".join(src.replace('"', " ").split())
    assert "is NOT resized by anything" in flat
    assert "ranking and logging only" in flat
