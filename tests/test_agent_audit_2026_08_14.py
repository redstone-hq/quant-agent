"""Regression guards for the deferred half of the 2026-08-13 adversarial
agent audit.

`tests/test_prompt_audit_2026_08_13.py` landed the text-only findings and
explicitly held five back as "behaviour or data flow, needs external
architectural review". This file covers those five, now implemented:

  F4  premortem / observability — a mandatory audit step could vanish with
      no parse error, no log line and no reader.
  F5  PM / Risk independence — RM read PM's case for the plan before it saw
      a single primary number.
  F6  risk evidence completeness — RM's prompt made it the enforcer of two
      rules whose inputs never reached it.
  F7b earnings valuation data — decided NOT to route price data in; the
      guards here pin the decision and detect the claims it forbids.
  F8  inherited long bias — three behavioural priors fitted to one window
      of one account, stated as standing truths.

Every test names the finding it pins so a later editor — human, or the
meta_reflector's auto-evolve path — sees what breaks before removing it.

What is deliberately NOT here, because nothing changed: every deterministic
threshold, the veto/modification hierarchy, the schema optionality of
`continuity_check` / `premortem_check`, and Alpaca paper-only. Tests below
assert several of those are still intact.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.models import (
    EarningsAnalysis,
    PortfolioDecision,
    Position,
    ReasoningChain,
    TradeDecision,
)
from tests.pipeline_factory import build_pipeline

_REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = _REPO_ROOT / "config" / "prompts"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk_agent(cls):
    with patch("anthropic.Anthropic"):
        return cls(api_key="test", model="claude-sonnet-4-6")


def _position(
    symbol="NVDA", qty=10, avg_entry=100.0, current_price=110.0, market_value=None, unrealized_pnl=100.0, sector="Tech"
):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=market_value if market_value is not None else qty * current_price,
        unrealized_pnl=unrealized_pnl,
        sector=sector,
    )


def _rc(**overrides) -> ReasoningChain:
    fields = dict(
        macro_filter="macro is risk-on",
        news_check="nothing material",
        earnings_check="none queued",
        signal_conflicts="none",
        sizing_logic="base sizing per conviction",
        portfolio_balance="within caps",
        cash_target="12% cash",
        continuity_check="consistent with the week",
        premortem_check="bear case on NVDA is crowding",
    )
    fields.update(overrides)
    return ReasoningChain(**fields)


def _decision(action="BUY", symbol="NVDA", alloc=10.0):
    return TradeDecision(
        action=action,
        symbol=symbol,
        allocation_pct=alloc,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        reasoning="x",
        thesis_invalid_if="closes below support",
    )


def _pd(decisions=None, rc=None) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=rc if rc is not None else _rc(),
        decisions=decisions if decisions is not None else [_decision()],
        portfolio_view="constructive",
    )


def _rm_message(**overrides) -> str:
    from src.agents.risk_manager import RiskManagerAgent

    kwargs = dict(
        portfolio_decision=_pd(),
        positions=[_position()],
        macro_summary={},
        rule_violations=[],
    )
    kwargs.update(overrides)
    return _mk_agent(RiskManagerAgent).build_user_message(**kwargs)


# ===========================================================================
# F6 — risk evidence completeness
# ===========================================================================


def test_f6_rm_sees_position_age_as_plain_context() -> None:
    """`held: Nd` is informational only (spec item 25, 2026-09-03/04): the
    old flat `<5d` / `5-15d` / `>15d` tiers were replaced by a deterministic,
    data-driven check (`check_structural_protection`) that RM cannot see
    before it speaks, so the prompt no longer asserts a protection tier off
    the age alone — `Position` carries no entry date, so age still has to
    come from `position_history`, just without the retired tier labels.
    """
    msg = _rm_message(
        positions=[
            _position(symbol="NVDA"),
            _position(symbol="MSFT"),
            _position(symbol="JPM"),
        ],
        position_history={
            "NVDA": {"days_held": 2},
            "MSFT": {"days_held": 9},
            "JPM": {"days_held": 40},
        },
    )
    assert "held: 2d" in msg
    assert "held: 9d" in msg
    assert "held: 40d" in msg
    assert "PROTECTED" not in msg
    assert "maturity" not in msg


def test_f6_unknown_position_age_is_explicit_not_omitted() -> None:
    """A failed entry-date lookup must not render as a clean position. An
    omitted age reads as 'nothing to see'; `unknown` reads as 'this SELL is
    unverifiable', which is what the prompt tells RM to do with it."""
    msg = _rm_message(position_history={})
    assert "held: unknown" in msg


def test_f6_no_drawdown_data_reads_as_unauditable_not_as_no_drawdown() -> None:
    """Fail-closed in the reporting sense: a missing input must never be
    presented as a benign value. 'not provided' is a different claim from
    'in_drawdown=false' and RM must be able to tell them apart."""
    msg = _rm_message(recent_performance={})
    assert "System performance: not provided" in msg
    # Wording changed when the halving became deterministic code (audit §1.1)
    # and again when the whole account-level loss alarm was retired
    # (2026-09-20, board item 32): the absent-input line reports unknown
    # STATE, and there is no flag left to render either way. The claim under
    # test — absence is never presented as a benign value — is unchanged.
    assert "drawdown state unknown this run" in msg
    assert "rolling 5d" not in msg.lower().split("not provided")[0][-200:]


def test_f6_account_section_survives_an_empty_book() -> None:
    """No equity and no positions previously produced no Account section at
    all; the drawdown line still needs a header to hang off."""
    msg = _rm_message(
        positions=[],
        recent_performance={"rolling_5d_pct": None, "rolling_20d_pct": None, "trailing_days": 0},
    )
    assert "## Account" in msg
    assert "## Current Positions" in msg


def test_f6_rm_prompt_declares_the_new_inputs() -> None:
    """The audit's root cause was a prompt that named inputs the renderer
    did not pass. Keep the two in sync in the other direction too.

    2026-09-20: `in_drawdown` is no longer asserted here. The flag was
    retired with the rest of the account-level loss alarms (board item 32),
    and the only remaining mention of that word in the sheet is the sentence
    saying it is gone — so asserting the identifier appears would have kept
    this test green off its own tombstone. What the sheet must still declare
    is the rolling returns the renderer does pass.
    """
    text = (PROMPT_DIR / "risk_manager.md").read_text()
    assert "rolling_5d_pct" in text
    assert "rolling_20d_pct" in text
    assert "held: Nd" in text
    # Renamed from "Drawdown-halve compliance" when audit §1.1 moved the
    # halving into deterministic code: RM is told the state, not made the
    # enforcer of a rule it can only ask about.
    assert "**Drawdown state**" in text
    # Owner ruling 2026-10-13 ("same rules as buying") replaced item 8:
    # RM no longer audits exits for triggers; one deterministic gate decides
    # every judgement sale after it.
    assert "Sales are not yours to judge" in text
    assert "Holding-discipline compliance" not in text
    # Audit §1.3 — the Portfolio Risk block RM's sizing_sanity step now reads.
    assert "Portfolio Risk" in text


def test_f6_risk_stage_forwards_the_evidence_it_was_given() -> None:
    """DecisionStage publishes both to ctx so RM audits PM against the SAME
    snapshot PM sized from, not one taken minutes later."""
    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    ctx.position_history = {"NVDA": {"days_held": 3}}
    ctx.recent_performance = {"rolling_5d_pct": -5.0, "rolling_20d_pct": -1.0, "trailing_days": 20}

    kwargs = _run_risk_stage_capturing_review(ctx)
    assert kwargs["position_history"] == {"NVDA": {"days_held": 3}}
    # A field production actually builds, so this cannot pass on a value the
    # test planted into a shape nothing emits (2026-09-20: `in_drawdown` was
    # exactly that after board item 32 retired it).
    assert kwargs["recent_performance"]["rolling_5d_pct"] == -5.0


def test_f6_risk_stage_rebuilds_the_evidence_on_the_resume_lane() -> None:
    """The RC2 resume lane re-enters at RiskStage without DecisionStage ever
    running, so ctx carries neither field. Rebuild rather than let RM
    silently lose the evidence for two of the rules it owns."""
    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    assert ctx.position_history == {} and ctx.recent_performance == {}

    kwargs = _run_risk_stage_capturing_review(
        ctx,
        position_history={"AAPL": {"days_held": 11}},
        recent_performance={"in_drawdown": False, "rolling_5d_pct": 0.4, "rolling_20d_pct": 1.1, "trailing_days": 25},
    )
    assert kwargs["position_history"] == {"AAPL": {"days_held": 11}}
    assert kwargs["recent_performance"]["trailing_days"] == 25
    # and it was cached back onto ctx for later stages
    assert ctx.position_history == {"AAPL": {"days_held": 11}}


def test_f6_risk_stage_degrades_open_when_the_rebuild_raises() -> None:
    """A local DB failure must cost the evidence, not the run. RM then sees
    'not provided' — which its prompt tells it to treat as unauditable."""
    from src.pipeline_context import RunContext

    ctx = RunContext.start("morning")
    kwargs = _run_risk_stage_capturing_review(
        ctx,
        position_history=RuntimeError("db locked"),
        recent_performance=RuntimeError("db locked"),
    )
    assert kwargs["position_history"] == {}
    assert kwargs["recent_performance"] == {}


def _run_risk_stage_capturing_review(ctx, *, position_history=None, recent_performance=None, reasoning_chain=None):
    """Drive RiskStage.run() far enough to capture the risk_manager.review
    kwargs, with every deterministic gate stubbed to a pass-through."""
    from src.models import RiskReasoningChain, RiskVerdict
    from src.pipeline import TradingPipeline
    from src.pipeline_stages import RiskStage

    decisions = [_decision()]
    pipeline = build_pipeline(
        db=MagicMock(),
        _sweeper=MagicMock(return_value=None),
        _filter_supported_symbols=MagicMock(return_value=(decisions, [])),
        _refuse_queued_earnings_buys=MagicMock(return_value=decisions),
        _filter_hard_risk_decisions=MagicMock(
            return_value=(decisions, [], []),
        ),
        _apply_risk_modifications=MagicMock(return_value=(decisions, [])),
        risk_manager=MagicMock(),
    )

    def _seam(value):
        if isinstance(value, BaseException):
            return MagicMock(side_effect=value)
        return MagicMock(return_value=value if value is not None else {})

    pipeline._build_position_history = _seam(position_history)
    pipeline._compute_recent_performance = _seam(recent_performance)

    verdict = RiskVerdict(
        approved=True,
        reasoning_chain=RiskReasoningChain(
            rr_audit="x",
            signal_fidelity="x",
            correlation_check="x",
            event_risk="x",
            sizing_sanity="x",
            overall="x",
        ),
        reasoning="ok",
    )
    rm_result = MagicMock()
    rm_result.used_fallback = False
    pipeline.risk_manager.review.return_value = (verdict, rm_result)

    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 20_000.0
    ctx.portfolio_decision = _pd(decisions, rc=reasoning_chain)

    RiskStage(pipeline=pipeline).run(ctx)
    pipeline.risk_manager.review.assert_called_once()
    return pipeline.risk_manager.review.call_args.kwargs


# ===========================================================================
# F5 — PM / Risk independence
# ===========================================================================


def test_f5_pm_claims_come_after_the_primary_evidence() -> None:
    """PM's reasoning chain used to be the FIRST block in RM's message, so
    RM read PM's case for the plan before seeing a single primary number and
    graded the story rather than the book. Order is now:

        proposed trades -> account/market facts -> PM's claims ->
        the deterministic engine's findings -> verdict

    so RM forms its own read first, and the last thing it reads before the
    verdict is the one input PM did not author.
    """
    msg = _rm_message(
        positions=[_position()],
        total_value=100_000.0,
        tech_analyses=[],
    )
    i_trades = msg.index("## Proposed Trades")
    i_positions = msg.index("## Current Positions")
    i_macro = msg.index("## Macro Context")
    i_claims = msg.index("## PM Reasoning Chain")
    i_engine = msg.index("## Engine Risk Check Results")

    assert i_trades < i_positions < i_macro < i_claims < i_engine, (
        "PM's self-justification must sit after the primary evidence and "
        "before the engine's findings; got order "
        f"trades={i_trades} positions={i_positions} macro={i_macro} "
        f"claims={i_claims} engine={i_engine}"
    )


def test_f5_pm_chain_is_labelled_as_claims_not_evidence() -> None:
    msg = _rm_message()
    assert "PM's CLAIMS about its own plan, not evidence" in msg
    assert "check it against the Account / Positions / Tech / Macro data" in msg


def test_f5_absent_reasoning_chain_is_stated_not_skipped() -> None:
    """An entirely missing chain used to render as empty string — RM could
    not distinguish 'PM had no audit trail' from 'there was no section'."""
    pd = _pd()
    pd.reasoning_chain = None
    msg = _rm_message(portfolio_decision=pd)
    assert "## PM Reasoning Chain" in msg
    assert "unaudited by construction" in msg


def test_f5_rm_prompt_no_longer_describes_the_removed_pm_verdict_feed() -> None:
    """The RM-verdict feed into PM's sheet was deleted 2026-10-18; the RM
    sheet must not describe it, and the false 25% cut stays gone."""
    text = (PROMPT_DIR / "risk_manager.md").read_text()
    assert "PM is SHOWN your history" not in text
    assert "_build_rm_recent_verdicts" not in text
    assert "anchoring on your history" not in text
    assert "base allocations 25%" not in text


def test_f5_rm_prompt_states_its_model_relationship_to_pm_accurately() -> None:
    """RM should know whether it shares PM's blind spots, and the prompt must
    say whichever is actually true.

    It said "you and PM currently run the same model" when that was the
    policy. PR #30's RM-only re-run found four candidates tied at 1.00/1.00
    at this seat, so the tie was spent on independence and the seats now
    diverge. A prompt asserting a shared model against a split policy is
    exactly the kind of stale claim the audit was about, so this test is
    pinned to `config/settings.yaml` rather than to a fixed sentence.
    """
    import yaml

    settings = yaml.safe_load((_REPO_ROOT / "config" / "settings.yaml").read_text())["llm"]
    shared = settings["risk_manager_model"] == settings["portfolio_manager_model"]

    text = (PROMPT_DIR / "risk_manager.md").read_text()
    assert "MODEL_ROUTING_POLICY.md" in text
    if shared:
        assert "same model" in text, (
            "PM and RM share a model — RM's prompt must disclose that it shares PM's blind spots"
        )
    else:
        assert "different model from PM" in text, (
            "PM and RM run different models — RM's prompt must not claim they share one"
        )
        assert "same model" not in text


def test_f5_independence_is_not_framed_as_disagreeing_more() -> None:
    """The failure mode of an 'be more independent' instruction is a veto
    layer that manufactures objections. Rejecting more often is not better
    and cannot be validated without paper-trading evidence."""
    text = (PROMPT_DIR / "risk_manager.md").read_text()
    assert "Independence does not mean disagreeing more often" in text
    assert "`clean` on a genuinely clean plan is the correct verdict" in text


@pytest.mark.parametrize(
    "anchor",
    (
        # Owner ruling 2026-09-24 (final): the seat has NO whole-batch veto. The
        # old "**Veto is nuclear.**" and "≥ 5 separate `modifications`" anchors were
        # REMOVED with the veto itself — the seat may only drop named NEW entries
        # and shrink sizing. These anchors pin the new contract.
        "You have no whole-plan veto. There is no whole-batch reject.",
        "no `approved: false` lever",
        # "R/R discipline is non-negotiable" was an anchor here until
        # 2026-09-11. It was REMOVED deliberately, by owner decision
        # (docs/WORK.md item 1(d)): a flat reward:risk bar applied to every
        # setup type was the largest measured cause of proposals that never
        # became trades, and it is meaningless on a trend trade with no
        # overhead level. What replaces it is anchored below.
        "R/R discipline is by SETUP TYPE, not universal",
        "Err on the side of capital preservation",
    ),
)
def test_f5_veto_hierarchy_is_unchanged(anchor: str) -> None:
    """Owner ruling 2026-09-24 (final): the whole-batch veto is REMOVED. The
    seat reduces risk only by dropping named NEW entries (`rejected_symbols`)
    and shrinking (`scale_all_buys` / `modifications`); `approved=False` is a
    recorded no-op. It can never block a protective exit or touch a holding.
    These anchors pin that contract in the prompt.
    """
    assert anchor in (PROMPT_DIR / "risk_manager.md").read_text()


# ===========================================================================
# F4 — premortem / observability
# ===========================================================================


def test_f4_missing_premortem_renders_as_missing_to_rm() -> None:
    """`premortem_check` is mandatory in PM's prompt and optional in the
    schema, so skipping it produced a clean parse. RM's renderer showed only
    seven of the nine fields — and the two it dropped were exactly the two
    that can be empty. The only reviewer positioned to notice was the one
    not being shown them."""
    msg = _rm_message(portfolio_decision=_pd(rc=_rc(premortem_check="")))
    assert "Pre-mortem check: [MISSING" in msg
    assert "MANDATORY in PM's prompt but optional in the schema" in msg
    assert "NOT PERFORMED" in msg


def test_f4_missing_continuity_renders_as_missing_to_rm() -> None:
    msg = _rm_message(portfolio_decision=_pd(rc=_rc(continuity_check="")))
    assert "Continuity check: [MISSING" in msg


def test_f4_all_nine_fields_reach_rm_when_present() -> None:
    rc = _rc(continuity_check="week arc intact", premortem_check="crowding risk")
    msg = _rm_message(portfolio_decision=_pd(rc=rc))
    for label, value in (
        ("Macro filter", rc.macro_filter),
        ("News check", rc.news_check),
        ("Earnings check", rc.earnings_check),
        ("Signal conflicts", rc.signal_conflicts),
        ("Sizing logic", rc.sizing_logic),
        ("Portfolio balance", rc.portfolio_balance),
        ("Cash target", rc.cash_target),
        ("Continuity check", rc.continuity_check),
        ("Pre-mortem check", rc.premortem_check),
    ):
        assert f"- {label}: {value}" in msg, f"{label} did not reach RM"


def test_f4_missing_step_raises_an_engine_advisory() -> None:
    """Observability lands on the existing advisory seam — the same
    non-blocking channel `data_degraded` and `correlation_coverage_gap` use,
    and one RM's prompt already requires it to answer. No new
    infrastructure, and no order is blocked by it."""
    from src.pipeline_context import RunContext

    kwargs = _run_risk_stage_capturing_review(
        RunContext.start("morning"),
        reasoning_chain=_rc(premortem_check="", continuity_check=""),
    )
    rules = [v.rule for v in kwargs["rule_violations"]]
    assert "pm_audit_step_missing" in rules
    message = next(v.message for v in kwargs["rule_violations"] if v.rule == "pm_audit_step_missing")
    assert "premortem_check" in message and "continuity_check" in message


def test_f4_complete_chain_raises_no_advisory() -> None:
    from src.pipeline_context import RunContext

    kwargs = _run_risk_stage_capturing_review(
        RunContext.start("morning"),
        reasoning_chain=_rc(),
    )
    assert "pm_audit_step_missing" not in [v.rule for v in kwargs["rule_violations"]]


def test_f4_schema_stays_backward_compatible() -> None:
    """INTENTIONALLY RETAINED. Making the two fields `min_length=1` would be
    the obvious 'fix' and would break replay of every pre-2026-06 log, which
    carries neither. Enforcement belongs at the observability layer, not by
    making historical data unparseable."""
    old_log_shape = ReasoningChain(
        macro_filter="x",
        news_check="x",
        earnings_check="x",
        signal_conflicts="x",
        sizing_logic="x",
        portfolio_balance="x",
        cash_target="x",
    )
    assert old_log_shape.premortem_check == ""
    assert old_log_shape.continuity_check == ""


def test_f4_pm_prompt_explains_why_the_schema_is_permissive() -> None:
    text = (PROMPT_DIR / "portfolio_manager.md").read_text()
    assert "pm_audit_step_missing" in text
    assert "backward-compat with pre-2026-06 logs" in text


def test_f4_decision_stage_logs_all_nine_fields(caplog) -> None:
    """The operator-facing log line printed seven fields, omitting the two
    that can be empty — so the log could not distinguish 'PM red-teamed its
    book' from 'PM skipped the step'."""
    import logging
    import src.pipeline_stages as ps

    with caplog.at_level(logging.INFO, logger=ps.logger.name):
        ps.logger.info(
            "PM Reasoning Chain:\n  Macro: %s\n  News: %s\n  Earnings: %s\n  "
            "Conflicts: %s\n  Sizing: %s\n  Balance: %s\n  Cash: %s\n  "
            "Continuity: %s\n  Pre-mortem: %s\n  Macro audit: %s",
            "a",
            "b",
            "c",
            "d",
            "e",
            "f",
            "g",
            "" or "[MISSING]",
            "" or "[MISSING]",
            "" or "[MISSING]",
        )
    # `DecisionStage` moved to `src/stage_decision.py` verbatim (item 210,
    # step 10); the log line travelled with it.
    from src import stage_decision as _stage_decision

    source = Path(ps.__file__).read_text() + Path(_stage_decision.__file__).read_text()
    # Every field the schema lets default to "" must appear here, or the log
    # cannot tell a performed audit step from a skipped one. `macro_audit`
    # joined them 2026-09-14 (item 18e).
    assert "Continuity: %s\\n  Pre-mortem: %s\\n  Macro audit: %s" in source, (
        "DecisionStage's reasoning-chain log line must carry every field of "
        "the schema, the optional-default ones especially"
    )
    from src.models import ReasoningChain

    for name, field in ReasoningChain.model_fields.items():
        if field.default == "":
            assert name in source, (
                f"`{name}` validates when empty but DecisionStage never "
                f"logs it, so a skipped step is invisible to the operator."
            )


# ===========================================================================
# F7b — earnings valuation evidence
# ===========================================================================


def _earnings_analysis(valuation_context: str) -> EarningsAnalysis:
    return EarningsAnalysis(
        symbol="AAPL",
        form_type="10-Q",
        filing_date="2026-03-15",
        revenue={"total": "$95.4B"},
        profitability={},
        cash_flow={},
        balance_sheet={},
        guidance="flat",
        data_quality="complete",
        investment_implications={
            "sentiment": "bullish",
            "conviction": "medium",
            "key_thesis": "services mix",
            "reasoning_chain": {
                "fundamental_quality": "strong",
                "growth_trajectory": "stable",
                "strategic_risks": "vision pro",
                "management_execution": "credible",
                "valuation_context": valuation_context,
            },
        },
    )


def _flag(valuation_context: str, source: str = "llm") -> list[str]:
    from src.agents.earnings_analyst import EarningsAnalystAgent

    report = MagicMock()
    report.symbol = "AAPL"
    report.form_type = "10-Q"
    return EarningsAnalystAgent._flag_unsourced_valuation_claims(
        report,
        _earnings_analysis(valuation_context),
        source,
    )


@pytest.mark.parametrize(
    "text,label",
    (
        ("trading at ~28x forward earnings", "trading at"),
        ("the P/E of 34 is rich", "P/E"),
        ("EV/EBITDA near 22x", "EV/x"),
        ("market cap implies a premium", "market cap"),
        ("share price already reflects this", "share price"),
        ("roughly 6.4x sales", "Nx earnings/sales"),
        ("a PEG above 2 is stretched", "PEG"),
    ),
)
def test_f7b_price_derived_claims_are_detected(text: str, label: str) -> None:
    """`build_user_message` passes filing text plus symbol/form/date and
    nothing else — no price, no market cap, no multiple. Every claim here
    requires a share price, so the filing cannot support it. The worked
    example that shipped for months invented '~28x forward earnings' and PM
    sizes off this field."""
    assert label in _flag(text)


@pytest.mark.parametrize(
    "text",
    (
        "net debt sits at 2.1x EBITDA, down from 2.6x",
        "interest coverage of 8.4x on the disclosed schedule",
        "the Services mix shift is doing the margin work; a deceleration below +10% removes it",
        "[UNSOURCED:no_market_data]",
    ),
)
def test_f7b_filing_grounded_statements_are_not_flagged(text: str) -> None:
    """Leverage and coverage ratios ARE disclosed in a 10-Q/10-K. A detector
    that fires on them would train the agent away from citing real filing
    numbers, which is the opposite of the intent."""
    assert _flag(text) == []


def test_f7b_cached_analyses_are_checked_too() -> None:
    """Analyses are written to disk once and re-served for the life of the
    filing, so an invented multiple written before the prompt was corrected
    keeps arriving at PM until something can see it."""
    assert _flag("trading at 28x forward earnings", source="cache") == [
        "trading at",
        "Nx earnings/sales",
    ]


def test_f7b_the_disclosure_does_not_trip_its_own_detector() -> None:
    """The redaction text contains "share price" and "market cap", two of the
    detector's own patterns. Unguarded, every run after the first loaded the
    redacted cache, matched on the desk's own boilerplate, redacted it to the
    identical text and rewrote the identical file — measured at 106
    detect/re-save pairs for two symbols over five days, not one of them a
    real fabricated claim."""
    from src.agents.earnings_analyst import _UNSOURCED_VALUATION_DISCLOSURE

    assert _flag(_UNSOURCED_VALUATION_DISCLOSURE, source="cache") == []
    assert _flag(f"  {_UNSOURCED_VALUATION_DISCLOSURE}  ") == []


@pytest.mark.parametrize(
    "wrap",
    (
        lambda d: f"{d} The P/E of 34 is rich.",
        lambda d: f"The P/E of 34 is rich. {d}",
    ),
)
def test_f7b_disclosure_marker_cannot_be_used_to_smuggle_a_claim(wrap) -> None:
    """The exemption is exact equality, never a prefix or substring test: a
    model that pastes the marker around an invented multiple must still be
    caught, or the guard becomes the evasion."""
    from src.agents.earnings_analyst import _UNSOURCED_VALUATION_DISCLOSURE

    assert "P/E" in _flag(wrap(_UNSOURCED_VALUATION_DISCLOSURE))


def test_f7b_real_claims_still_caught_alongside_the_exemption() -> None:
    """The exemption must not weaken ordinary detection."""
    assert _flag("trading at 28x forward earnings", source="cache") != []


def test_f7b_detection_is_advisory_and_never_drops_the_analysis() -> None:
    """A false positive must not cost the whole filing read — it is the only
    fundamentals input PM and position_reviewer get for that name."""
    from src.agents.earnings_analyst import EarningsAnalystAgent

    agent = _mk_agent(EarningsAnalystAgent)
    report = MagicMock()
    report.symbol = "AAPL"
    report.form_type = "10-Q"
    report.filing_date = "2026-03-15"

    parsed = _earnings_analysis("trading at 28x forward earnings").model_dump()
    validated = agent._validate_analysis(report, parsed, source="llm")
    assert validated is not None, "an unsourced claim must not reject the analysis"


def test_f7b_decision_not_to_route_price_data_is_recorded() -> None:
    """INTENTIONALLY RETAINED. Routing `trailing_pe` / `forward_pe` /
    `ps_ratio` here was the audit's suggested follow-up and is NOT done:
    the analysis is cached for the life of the filing, so a price-derived
    figure in it is stale the day after it is written, while the conditional
    reading stays true as long as the filing does. The live multiples are
    fetched fresh each session and given to `tech_analyst`, the seat that
    reads them while they are current."""
    text = (PROMPT_DIR / "earnings_analyst.md").read_text()
    assert "that is deliberate" in text, (
        "the earnings prompt must record WHY price data is withheld, or a "
        "later editor reads the gap as an oversight and plumbs it"
    )
    assert "stale the day after it is written" in text
    assert "tech_analyst" in text
    assert "_flag_unsourced_valuation_claims" in text


def test_f7b_schema_comment_matches_the_corrected_meaning() -> None:
    """The schema still described `valuation_context` as 'is the market
    pricing this fairly' — the exact question the seat cannot answer, left
    contradicting the prompt that had just been corrected."""
    src = (_REPO_ROOT / "src" / "models" / "earnings.py").read_text()
    assert "# is the market pricing this fairly given the above?" not in src, (
        "the trailing schema comment still defines valuation_context as a price judgement, contradicting the prompt"
    )
    assert 'NOT "is the market pricing this fairly"' in src
    assert "how conditional is the story" in src


# ===========================================================================
# F8 — inherited behavioural priors
# ===========================================================================


@pytest.mark.parametrize("threshold", ("20%", "40%", "5.0", "base × rr_mult"))
def test_f8_no_sizing_threshold_moved(threshold: str) -> None:
    """INTENTIONALLY RETAINED. Making a prior explicit is a statement about
    its evidence, not a decision to act on it differently. Whether the long
    bias should be weakened is a question paper trading answers, not a
    prompt edit."""
    assert threshold in (PROMPT_DIR / "portfolio_manager.md").read_text()
