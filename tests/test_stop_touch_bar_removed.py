"""The touch bar on stop placement is gone: any real level backs a stop.

14 Oct 2026 study, 148 stocks: a long stop just beyond a real computed level
is hit 26.5% of the time against 30.3% with no level at the same distance,
and extra touches add nothing. The owner's 9 Oct ruling keeps levels as one
weighted input to the odds, never a gate on their own. So the same rule now
holds at every site that asks "does a real level back this stop": the entry
stop, the no-volatility structural anchor, and the exit guard.
"""

from __future__ import annotations

from src.data.levels import CLUSTER_TOLERANCE_PCT
from src.models import TargetPosition, TechAnalysisResult, TechReasoningChain
from src.portfolio_constructor import STOP_RULE_STRUCTURAL_NO_ATR, ConstructorConfig, PortfolioConstructor
from src.portfolio_constructor.stops import StopRules
from src.risk.exit_guard import check_structural_protection

ENTRY = 100.0
ATR = 2.35
ANALYST_STOP = 95.00  # inside the 2.5-range band (94.12), outside the 1-range floor
LEVEL = 95.20  # a real level the stop sits just beyond
TARGET_LEVEL = 107.85


def _analysis(level_touches: int) -> TechAnalysisResult:
    """A long whose analyst stop sits just beyond a real level drawn by a bar
    whose range contains the stop."""
    floor = round(ENTRY * 0.5, 2)
    return TechAnalysisResult(
        symbol="AAPL",
        rating="buy",
        entry_price=ENTRY,
        stop_loss=ANALYST_STOP,
        reference_target=TARGET_LEVEL,
        reasoning="test",
        support_levels=[LEVEL],
        resistance_levels=[TARGET_LEVEL],
        setup_type="range",
        expected_horizon_sessions=60,
        atr_14=ATR,
        computed_levels=[LEVEL, TARGET_LEVEL, floor],
        computed_level_touches={LEVEL: level_touches, TARGET_LEVEL: level_touches, floor: level_touches},
        computed_level_bars={
            LEVEL: [(94.90, 95.40)],
            TARGET_LEVEL: [(TARGET_LEVEL, TARGET_LEVEL)],
            floor: [(floor, floor)],
        },
        reasoning_chain=TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x"),
        thesis_invalid_if="closes below support",
    )


def test_a_two_touch_level_keeps_the_analyst_stop_instead_of_the_wide_fallback() -> None:
    decisions = PortfolioConstructor().construct_orders(
        targets=[TargetPosition(symbol="AAPL", risk_allocation_pct=1.0, conviction="high", thesis="t")],
        positions=[],
        analyses=[_analysis(level_touches=2)],
        total_value=100_000.0,
        price_map={"AAPL": ENTRY},
    )
    assert len(decisions) == 1
    assert decisions[0].stop_loss == ANALYST_STOP


def test_the_no_range_anchor_uses_the_nearest_real_level_at_one_touch() -> None:
    rules = StopRules(read_cfg=lambda: ConstructorConfig(), entry_stop_resolver=lambda: None)
    analysis = _analysis(level_touches=1)
    analysis.atr_14 = None
    placed = rules._derive_structural_stop_no_atr(analysis, ENTRY, is_short=False)
    assert placed is not None
    level, stop, rule = placed
    assert level == LEVEL
    assert stop < LEVEL
    assert rule == STOP_RULE_STRUCTURAL_NO_ATR


def test_the_exit_guard_treats_a_two_touch_level_as_backing_the_stop() -> None:
    check = check_structural_protection(
        thesis_invalid_if=None,
        current_price=97.0,
        entry_price=ENTRY,
        stop_loss=ANALYST_STOP,
        atr=ATR,
        computed_levels=[LEVEL],
        computed_level_touches={LEVEL: 2},
        computed_level_bars={LEVEL: [(94.90, 95.40)]},
        level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
    )
    assert check.basis == "structural_level_intact"
    assert check.protected is True
