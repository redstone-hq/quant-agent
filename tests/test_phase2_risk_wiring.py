"""Phase 2a — the deterministic risk primitives, wired into the real pipeline.

Covers the audit findings this tranche closes (§1.1, the drawdown-halve,
was retired entirely on 2026-09-20 — see docs/INCIDENT_HISTORY.md, item 32):

  §1.2  the Portfolio Manager receives the correlation matrix BEFORE it chooses
  §1.3  portfolio heat is computed and shown to PM and the Risk Manager
  §1.4  R-multiple reaches the Position Reviewer

Each test names the finding it pins so a later editor sees what breaks.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.config import RiskConfig
from src.models import Position, TradeDecision
from src.pipeline import HARD_BLOCK_RULES, TradingPipeline
from src.pipeline_context import PMFacts
from src.risk.rules import RiskRuleEngine
from tests.pipeline_factory import build_pipeline


def _buy(symbol="NVDA", alloc=10.0) -> TradeDecision:
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=115.0,
        reasoning="high conviction breakout",
    )


def _position(symbol="NVDA", qty=10, avg_entry=100.0, current_price=110.0, sector="Technology") -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector=sector,
    )


def _risk_config(**overrides) -> RiskConfig:
    fields = dict(
        max_position_pct=20,
        max_total_position_pct=90,
        require_stop_loss=True,
    )
    fields.update(overrides)
    return RiskConfig(**fields)


def test_correlation_clusters_group_transitively():
    """A theme is one bet even when its outer pair falls under the threshold."""
    from src.data.correlation import correlation_clusters

    matrix = {
        "OKLO": {"CEG": 0.82, "VST": 0.60},
        "CEG": {"OKLO": 0.82, "VST": 0.79},
        "VST": {"CEG": 0.79, "OKLO": 0.60},
        "KO": {},
    }
    assert correlation_clusters(["OKLO", "CEG", "VST", "KO"], matrix) == [
        ["CEG", "OKLO", "VST"],
    ]


def test_correlation_clusters_omit_singletons():
    """A name with no measured correlation is never dragged into a cluster."""
    from src.data.correlation import correlation_clusters

    matrix = {"A": {"B": 0.9}, "B": {"A": 0.9}}
    assert correlation_clusters(["A", "B", "LONE"], matrix) == [["A", "B"]]


def test_two_name_book_rations_as_one_cluster():
    """CHANGED 2026-09-30 (item 186), and deliberately.

    This used to assert that KO/PEP at 0.2 were NOT a cluster, because 0.2
    sat under a 0.7 cutoff. There is no cutoff any more: a two-name tree has
    exactly one edge and therefore no jump of its own to read, so the
    structural method falls back to its conservative answer and treats them
    as one bet. That over-rations rather than under-rations — the pair share
    a budget they may not need to share, instead of a real single bet being
    counted as two.
    """
    from src.data.correlation import correlation_clusters

    assert correlation_clusters(["KO", "PEP"], {"KO": {"PEP": 0.2}}) == [["KO", "PEP"]]


def test_correlation_clusters_are_stable_and_largest_first():
    from src.data.correlation import correlation_clusters

    matrix = {
        "A": {"B": 0.9},
        "B": {"A": 0.9},
        "C": {"D": 0.8, "E": 0.8},
        "D": {"C": 0.8},
        "E": {"C": 0.8},
    }
    assert correlation_clusters(["E", "A", "D", "C", "B"], matrix) == [
        ["C", "D", "E"],
        ["A", "B"],
    ]


def test_correlation_clusters_use_absolute_correlation():
    """An inverse ETF moving −0.9 against the book is the same single bet."""
    from src.data.correlation import correlation_clusters

    matrix = {"SPY": {"SH": -0.95}, "SH": {"SPY": -0.95}}
    assert correlation_clusters(["SPY", "SH"], matrix) == [["SH", "SPY"]]


def test_correlation_clusters_empty_without_a_matrix():
    from src.data.correlation import correlation_clusters

    assert correlation_clusters(["A", "B"], {}) == []


def test_ensure_correlation_matrix_is_memoized_on_the_context():
    """RiskStage must score PM against the numbers PM was actually shown, and
    must not pay to rebuild them."""
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(market=MagicMock())
    pipeline.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=1800))
    ctx = RunContext.start("morning")
    ctx.symbols_bars = {}

    with patch("src.data.correlation.build_correlation_matrix", return_value={"NVDA": {"AMD": 0.9}}) as build:
        first = pipeline._ensure_correlation_matrix(ctx, [])
        second = pipeline._ensure_correlation_matrix(ctx, [])

    assert first == second == {"NVDA": {"AMD": 0.9}}
    assert build.call_count == 1, "the matrix must be built once per run"
    assert ctx.correlation_matrix == {"NVDA": {"AMD": 0.9}}


def test_ensure_correlation_matrix_degrades_to_empty_on_failure():
    from src.pipeline_context import RunContext

    pipeline = build_pipeline(market=MagicMock())
    pipeline.config = SimpleNamespace(trading=SimpleNamespace(lookback_days=1800))
    ctx = RunContext.start("morning")
    ctx.symbols_bars = {}

    with patch("src.data.correlation.build_correlation_matrix", side_effect=RuntimeError("boom")):
        assert pipeline._ensure_correlation_matrix(ctx, []) == {}


# ===========================================================================
# §1.3 / §1.4 — the facts block PM actually reads
# ===========================================================================


def test_pm_facts_render_the_risk_block_with_headroom():
    from src.risk.metrics import portfolio_heat

    facts = PMFacts()
    facts.heat = portfolio_heat(
        positions=[_position("NVDA", qty=100, avg_entry=100.0, current_price=110.0)],
        equity=100_000.0,
        stops={"NVDA": 95.0},
    )
    facts.risk_ceiling_pct = 25.0
    rendered = facts.render()
    assert "Portfolio Risk" in rendered
    assert "headroom" in rendered
    assert "NVDA" in rendered


def test_pm_facts_say_unknown_when_heat_could_not_be_computed():
    """A failed heat build must never render as a risk-free book."""
    rendered = PMFacts().render()
    assert "not computed this run" in rendered
    assert "UNSOURCED:no_risk_data" in rendered


def test_pm_facts_render_correlation_clusters():
    facts = PMFacts()
    facts.correlation_clusters = [["AMD", "NVDA", "SMH"]]
    rendered = facts.render()
    assert "Correlation Clusters" in rendered
    assert "AMD / NVDA / SMH" in rendered
    assert "ONE bet" in rendered


def test_pm_facts_flag_missing_correlation_coverage_as_a_disabled_gate():
    facts = PMFacts()
    facts.correlation_coverage = False
    rendered = facts.render()
    assert "coverage MISSING" in rendered
    assert "disabled" in rendered


def test_pm_prompt_no_longer_carries_a_drawdown_multiplier():
    """No sizing haircut on a rolling-return reading survives anywhere: the
    formula never carried one and the engine's halving is now retired."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config" / "prompts" / "portfolio_manager.md").read_text()
    assert "drawdown   = 0.5" not in text
    assert "× stale × drawdown" not in text
    assert "no `drawdown` term" in text


# ===========================================================================
# §1.3 — the Risk Manager sees the same heat PM sized against
# ===========================================================================


def _rm_message(**overrides) -> str:
    from src.agents.risk_manager import RiskManagerAgent
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on",
            news_check="quiet",
            earnings_check="none",
            signal_conflicts="none",
            sizing_logic="per conviction",
            portfolio_balance="within caps",
            cash_target="10%",
        ),
        decisions=[_buy()],
        portfolio_view="constructive",
    )
    kwargs = dict(
        portfolio_decision=decision,
        positions=[_position()],
        macro_summary={},
        rule_violations=[],
    )
    kwargs.update(overrides)
    with patch("anthropic.Anthropic"):
        agent = RiskManagerAgent(api_key="test", model="claude-sonnet-4-6")
    return agent.build_user_message(**kwargs)


def test_rm_sees_the_portfolio_risk_block():
    from src.risk.metrics import portfolio_heat

    heat = portfolio_heat(
        positions=[_position("NVDA", qty=100, avg_entry=100.0, current_price=110.0)],
        equity=100_000.0,
        stops={"NVDA": 95.0},
    )
    msg = _rm_message(heat=heat, risk_ceiling_pct=25.0)
    assert "Portfolio Risk" in msg
    assert "headroom" in msg


def test_rm_is_told_when_risk_could_not_be_computed():
    msg = _rm_message(heat=None)
    assert "not computed this run" in msg
    assert "UNKNOWN" in msg


def test_position_facts_carry_the_r_multiple_against_the_entry_stop():
    """The denominator is the bet that was made, not the trailed level."""
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.db.get_symbol_last_buy.return_value = {
        "stop_loss": 90.0,
        "qty": 10,
        "price": 100.0,
        "take_profit": 130.0,
        "timestamp": None,
    }
    pipeline.broker.get_current_stop_price.return_value = 105.0  # trailed up
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)

    facts = pipeline._build_position_facts(
        positions=[_position("NVDA", qty=10, avg_entry=100.0, current_price=120.0)],
        morning_trades=[],
        total_value=100_000.0,
    )
    # Risked $10/share at entry, now +$20 → 2R, NOT (120-100)/(100-105).
    assert facts["NVDA"]["r_multiple"] == 2.0
    assert facts["NVDA"]["initial_stop"] == 90.0
    # Live stop 105 is above entry 100 → budget risk released.
    assert facts["NVDA"]["risk_released"] is True


def test_position_facts_omit_r_when_no_risk_was_defined_at_entry():
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.db.get_symbol_last_buy.return_value = {
        "stop_loss": 0.0,
        "take_profit": 130.0,
        "timestamp": None,
    }
    pipeline.broker.get_current_stop_price.return_value = None
    pipeline._atr_for_symbol = MagicMock(return_value=2.0)

    facts = pipeline._build_position_facts(
        positions=[_position("NVDA", qty=10, avg_entry=100.0, current_price=120.0)],
        morning_trades=[],
        total_value=100_000.0,
    )
    assert facts["NVDA"]["r_multiple"] is None
    assert facts["NVDA"]["initial_stop"] is None


def test_reviewer_renders_r_multiple_in_the_metrics_line():
    from src.agents.position_reviewer import PositionReviewerAgent

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-sonnet-4-6")
    msg = agent.build_user_message(
        positions=[_position("NVDA", qty=10, avg_entry=100.0, current_price=120.0)],
        macro_summary={},
        cash_balance=10_000.0,
        total_value=100_000.0,
        position_facts={
            "NVDA": {
                "days_held": 6,
                "r_multiple": 2.0,
                "risk_released": True,
                "thesis_progress_pct": 40.0,
                "distance_to_stop_pct": 12.5,
            }
        },
        session_type="midday",
    )
    assert "R=+2.00" in msg
    assert "risk_released" in msg


def test_reviewer_prompt_documents_r_before_thesis_progress():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config" / "prompts" / "position_reviewer.md").read_text()
    assert "R-multiple" in text
    assert text.index("`R` = the **R-multiple**") < text.index("`thesis_progress_pct` =")


# ===========================================================================
# The Risk Manager must be GIVEN reward:risk, never left to divide it itself
#
# 2026-08-31 incident. The RM received the constructed order as bare
# Entry/Stop/Target text with no ratio, did the arithmetic inside the model,
# and produced TWO DIFFERENT ANSWERS IN ONE RESPONSE for a BUY on RSG
# (entry 221.14, stop 207.90, target 242.96): `rr_audit` said "R/R = 1.65 ...
# above 1.5, so compliant", `reasoning` said "R/R = 1.31, which is below the
# 1.5 floor". The pipeline acts on `reasoning`, so a compliant trade was
# rejected and the desk took no position that session. 1.65 is correct; 1.31
# matches no combination of the inputs.
#
# The seam these cover — build a real constructed order, render it into the
# RM prompt, assert the ratio is present and correct — had NO test at all.
# ===========================================================================


def _decision(action="BUY", entry=221.14, stop=207.90, target=242.96, setup_type=None, structural_ceiling=None):
    from src.models import TradeDecision

    return TradeDecision(
        action=action,
        symbol="RSG",
        allocation_pct=5.0,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        reasoning="constructed order under test",
        setup_type=setup_type,
        structural_ceiling=structural_ceiling,
    )


def test_trade_decision_computes_reward_risk_in_python():
    """The exact 2026-08-31 order. 1.65, deterministically, every time."""
    assert _decision().reward_risk == 1.65


def test_reward_risk_mirrors_the_short_side():
    d = _decision(action="SHORT", entry=100.0, stop=110.0, target=80.0)
    assert d.reward_risk == 2.0


def test_reward_risk_is_none_when_there_is_no_entry_geometry():
    """SELL/COVER reduce an existing position; HOLD opens nothing. A ratio
    rendered for these would be fiction."""
    for action in ("SELL", "COVER", "HOLD"):
        assert _decision(action=action).reward_risk is None


def test_malformed_buy_geometry_is_rejected_before_a_ratio_can_exist():
    """A BUY whose stop sits above entry never becomes a TradeDecision at all
    — `validate_buy_prices` refuses it at construction. Asserted here so the
    reward_risk guard's None-branch is understood as defence in depth behind
    an existing validator, not as the only thing standing between the desk and
    a fake ratio."""
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        _decision(stop=230.0)


def test_rm_prompt_carries_the_computed_reward_risk_for_each_trade():
    """The regression that matters: the ratio must reach the RM's prompt, so
    the model never has to derive the number its own floor is judged against."""
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on",
            news_check="quiet",
            earnings_check="none",
            signal_conflicts="none",
            sizing_logic="per conviction",
            portfolio_balance="within caps",
            cash_target="10%",
        ),
        decisions=[_decision()],
        portfolio_view="constructive",
    )
    msg = _rm_message(portfolio_decision=decision)
    assert "R/R 1.65:1" in msg, (
        "the constructed order's computed reward:risk must be rendered into "
        "the Risk Manager prompt — without it the model divides the prices "
        "itself, which is exactly what failed on 2026-08-31"
    )


def test_rm_prompt_omits_reward_risk_where_none_exists():
    """A SELL must not carry a ratio — rendering 'R/R None:1' would be worse
    than rendering nothing."""
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on",
            news_check="quiet",
            earnings_check="none",
            signal_conflicts="none",
            sizing_logic="per conviction",
            portfolio_balance="within caps",
            cash_target="10%",
        ),
        decisions=[_decision(action="SELL")],
        portfolio_view="constructive",
    )
    msg = _rm_message(portfolio_decision=decision)
    assert "R/R None" not in msg


def test_rm_prompt_shows_no_ratio_for_a_breakout_and_says_why():
    """2026-09-11, docs/WORK.md item 1(d). The arithmetic would still divide
    for a breakout — `take_profit` carries a measured-move reference — but no
    gate in the pipeline consults that number for this setup type any more.
    Showing it to a seat whose prompt calls R/R discipline non-negotiable
    invited exactly the refusal the change removes. Silence would be worse
    than a bad number (the RM would read it as a missing field), so the line
    says what the trade is and what to judge it on instead."""
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on",
            news_check="quiet",
            earnings_check="none",
            signal_conflicts="none",
            sizing_logic="per conviction",
            portfolio_balance="within caps",
            cash_target="10%",
        ),
        decisions=[_decision(setup_type="breakout")],
        portfolio_view="constructive",
    )
    msg = _rm_message(portfolio_decision=decision)
    assert "R/R 1.65:1" not in msg
    assert "R/R n/a — BREAKOUT setup" in msg
    assert "do NOT refuse or resize it on a reward:risk figure" in msg

    # A range order on the identical geometry still shows its real ratio.
    decision.decisions = [_decision(setup_type="range")]
    assert "R/R 1.65:1" in _rm_message(portfolio_decision=decision)


def test_rm_reads_the_constructor_measured_breakout_not_just_the_label():
    """Item 82. `setup_type` is classified TWICE: construction's real verdict
    is `is_trend_trade(setup_type, structural_ceiling=...)` — label OR
    measurement, either sufficient — but the analyst's raw label alone used
    to be all that reached the RM. A trade the analyst still labelled "range"
    while construction's own level computation found nothing overhead
    (`structural_ceiling=False`) IS a breakout by construction's own verdict,
    and must be shown to the RM as one — not as a real, cuttable ratio."""
    from src.models import PortfolioDecision, ReasoningChain

    decision = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on",
            news_check="quiet",
            earnings_check="none",
            signal_conflicts="none",
            sizing_logic="per conviction",
            portfolio_balance="within caps",
            cash_target="10%",
        ),
        decisions=[_decision(setup_type="range", structural_ceiling=False)],
        portfolio_view="constructive",
    )
    msg = _rm_message(portfolio_decision=decision)
    assert "R/R 1.65:1" not in msg
    assert "R/R n/a — BREAKOUT setup" in msg
    assert "do NOT refuse or resize it on a reward:risk figure" in msg


# ===========================================================================
# The constructor must SAY what it removed, or the RM vetoes the survivors
#
# 2026-08-31, first forced session of the day. The constructor struck a BUY on
# NVDA (reward:risk 1.42 against the 1.50 floor). PM's reasoning_chain, written
# BEFORE the constructor ran, still argued for it. The RM saw a narrative about
# a trade absent from the order list and rejected the ENTIRE plan:
#
#   "PM constructs a detailed narrative around a BUY NVDA trade that does not
#    exist in the proposed orders. This undermines trust in the decision logic.
#    While COP and V are valid, the plan as presented is not internally
#    consistent and cannot be approved."
#
# Two trades it had just called valid died for a bookkeeping mismatch. The RM
# was reasoning correctly from what it was shown; it was shown the wrong thing.
# ===========================================================================


def _pd_with(dropped, decisions=None):
    from src.models import PortfolioDecision, ReasoningChain

    return PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="risk-on",
            news_check="quiet",
            earnings_check="none",
            signal_conflicts="none",
            sizing_logic="per conviction",
            portfolio_balance="within caps",
            cash_target="10%",
        ),
        decisions=decisions if decisions is not None else [_decision()],
        constructor_dropped=dropped,
        portfolio_view="constructive",
    )


def test_rm_is_told_which_symbols_the_constructor_removed():
    msg = _rm_message(portfolio_decision=_pd_with(["NVDA", "VLO"]))
    assert "Removed Before You Saw This" in msg
    assert "NVDA" in msg and "VLO" in msg


def test_rm_is_told_removal_was_deterministic_and_not_incoherence():
    """The block must not merely list symbols — it must tell the RM that a
    narrative mentioning them is EXPECTED, which is the part that stops the
    full-plan veto."""
    msg = _rm_message(portfolio_decision=_pd_with(["NVDA"]))
    assert "EXPECTED" in msg
    assert "do not veto the surviving trades" in msg


def test_the_removal_note_does_not_assert_a_cause_it_cannot_know():
    """The list is now recomputed just before review, so a symbol on it may
    have been removed by a later gate rather than by the constructor's own
    measurement. The note must not name a specific rule it cannot know
    applied — the durable per-symbol reason lives in the evidence trail."""
    msg = _rm_message(portfolio_decision=_pd_with(["NVDA"]))
    assert "reward:risk could not be measured" not in msg
    assert "recorded per symbol" in msg


def test_the_risk_manager_is_never_told_a_reward_risk_floor_exists():
    """The floor was removed on 2026-09-11, but this seat's own briefing went
    on calling 1.5 "the only one to enforce" and claiming sub-floor orders had
    already been refused deterministically. Three of the four whole-plan
    vetoes in the pre-reset archive cite that floor by name."""
    from tests.test_agreement_sizing import _analysis

    tech = _analysis("NVDA")
    msg = _rm_message(portfolio_decision=_pd_with([]), tech_analyses=[tech])
    assert "Tech Analyst Signals" in msg  # the section really rendered
    assert "NVDA" in msg
    assert "1.5 floor" not in msg
    assert "There is no reward:risk floor" in msg


def test_no_removal_block_when_the_constructor_dropped_nothing():
    """A clean plan must not carry an empty scary heading."""
    msg = _rm_message(portfolio_decision=_pd_with([]))
    assert "Removed Before You Saw This" not in msg


def test_constructor_dropped_defaults_empty_so_old_call_sites_are_unaffected():
    from src.models import PortfolioDecision, ReasoningChain

    pd = PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter="a",
            news_check="b",
            earnings_check="c",
            signal_conflicts="d",
            sizing_logic="e",
            portfolio_balance="f",
            cash_target="g",
        ),
        portfolio_view="x",
    )
    assert pd.constructor_dropped == []


def test_a_symbol_removed_after_construction_is_still_reported_as_removed():
    """The drop list used to be frozen immediately after construction. Between
    that point and the review, the symbol guard, the queued-earnings clamp and
    the hard-risk gate each remove SOME orders and pass the rest through — so a
    name struck by one of them was missing from the order list AND missing from
    the drop list, which is precisely the 2026-08-31 failure on a path the
    original fix never covered."""
    from src.pipeline_stages import _dropped_since_proposal

    class _T:
        def __init__(self, symbol):
            self.symbol = symbol

    class _PD:
        targets = [_T("AAA"), _T("BBB"), _T("CCC")]
        decisions = [_T("aaa")]  # BBB dropped by the constructor,
        # CCC by a later gate; case-insensitive
        constructor_dropped = []

    assert _dropped_since_proposal(_PD()) == ["BBB", "CCC"]


def test_a_held_name_is_not_reported_as_removed():
    """A HOLD survived review; it is simply not being traded today."""
    from src.pipeline_stages import _dropped_since_proposal

    class _T:
        def __init__(self, symbol):
            self.symbol = symbol

    class _PD:
        targets = [_T("AAA")]
        decisions = [_T("AAA")]
        constructor_dropped = []

    assert _dropped_since_proposal(_PD()) == []
