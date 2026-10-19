"""PM TEST GATE item 7 — the decision seat gets bounded conclusions, not nulls.

These tests render the FROZEN `run_64290730` fixture through the real
`PortfolioManagerAgent.build_user_message` — the same fixture and the same
method `ops/model_policy/scenarios.py` grades model selection against — and
assert the properties the redesign has to hold, not a character count someone
liked the look of.

The two shape changes under test:

  * an earnings filing the seat read WITHOUT reaching a direction is one
    roll-up line naming the symbol, its form, its date and its conviction,
    instead of a four-line verdict block whose direction, thesis and
    falsifier are all absent;
  * a `neutral` technical read — which by construction has no entry, stop or
    target, so `risk_reward` is None — renders the analyst's one-sentence
    conclusion without the three `None` fields and the
    "Invalid if: (not specified)" line.

What the tests are really guarding is the constraint that makes the change
safe: coverage must never become invisible. A reader of the prompt has to be
able to tell "read, concluded nothing" from "never read" for every symbol,
and every dissenting or low-conviction stance has to keep reaching the seat
through the registry and the net-agreement arithmetic untouched.
"""

from __future__ import annotations

import importlib
import re

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent

scenarios = importlib.import_module("ops.model_policy.scenarios")

# The level-less 2026-09-01 day this audit's numbers were measured on.
# `pm_selection` itself moved to run-bba4d4f3 on 2026-09-14; this file
# stays pinned to the day its write-up describes.
_LEVEL_LESS = scenarios.load_frozen_selection(scenarios.LEVEL_LESS_SELECTION_FIXTURE)


# The marker the old null block printed, verbatim. Its ABSENCE is the point:
# a line that says the analyst disclosed nothing is not evidence, and four
# lines per filing of it was 16.7% of everything the decision seat read.
_NULL_FALSIFIER = "Invalidated if: not disclosed by the analyst"


@pytest.fixture(scope="module")
def rendered() -> str:
    sel = _LEVEL_LESS.raw
    account = sel["account"]
    memory = sel["memory"]
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    return agent.build_user_message(
        analyses=_LEVEL_LESS.analyses,
        positions=_LEVEL_LESS.positions,
        macro_analysis=sel["macro_analysis"],
        cash_balance=account["cash_balance"],
        reserve_balance=account["reserve_balance"],
        total_value=account["total_value"],
        news_intel=_LEVEL_LESS.news,
        earnings_analyses=sel["earnings_analyses"],
        recent_performance=sel["recent_performance"],
        position_history=sel["position_history"],
        yesterday_insights=sel["yesterday_insights"],
        weekly_narrative=memory["weekly_narrative"],
        macro_trajectory=memory["macro_trajectory"],
        active_state_changes=memory["active_state_changes"],
        projected_portfolio=memory["projected_portfolio"],
        calibration_note=memory["calibration_note"],
        recent_missed_lessons=memory["recent_missed_lessons"],
        recent_loss_pits=memory["recent_loss_pits"],
        allow_margin=account["allow_margin"],
        session_type=account["session_type"],
        allowed_buy_symbols=set(sel["allowed_buy_symbols"]),
        transient_admitted_symbols=set(sel["transient_admitted_symbols"]),
    )


def _section(text: str, heading: str) -> str:
    parts = re.split(r"\n(?=## )", text)
    for part in parts:
        if part.startswith("## " + heading):
            return part
    raise AssertionError(f"section not found: {heading}")


def _neutral_earnings_symbols() -> set[str]:
    """Symbols whose earnings read collapses to no direction, from the data.

    Derived the same way the renderer derives it — `_collapse_stances` over
    the seat's own `sentiment` — so the test cannot drift from the code by
    hardcoding a list someone typed once.
    """
    out = set()
    for item in _LEVEL_LESS.raw["earnings_analyses"]:
        analysis = item.get("analysis")
        if not isinstance(analysis, dict):
            continue
        impl = analysis.get("investment_implications") or {}
        stance = PortfolioManagerAgent._collapse_stances([impl.get("sentiment")])
        if stance in (None, "neutral"):
            out.add(str(item["symbol"]).strip().upper())
    return out


def test_fixture_still_contains_the_no_call_majority_this_is_measuring():
    """Guard the premise. If a fixture edit removes the no-call filings, every
    assertion below would pass vacuously and prove nothing."""
    neutral = _neutral_earnings_symbols()
    analysed = [item for item in _LEVEL_LESS.raw["earnings_analyses"] if isinstance(item.get("analysis"), dict)]
    assert len(analysed) == 65
    assert len(neutral) == 38


def test_no_call_filings_are_not_rendered_as_empty_verdict_blocks(rendered):
    earnings = _section(rendered, "Earnings Analysis")
    assert _NULL_FALSIFIER not in earnings


def test_every_no_call_symbol_is_still_named_in_the_prompt(rendered):
    """The load-bearing one. Shortening the input must not make coverage
    invisible — a symbol the seat read and declined to call has to stay
    distinguishable from a symbol nobody looked at."""
    earnings = _section(rendered, "Earnings Analysis")
    rollup = earnings[earnings.index("### Read, no call") :]
    listed = set(re.findall(r"^- ([A-Z][A-Z0-9.\-]*) \| ", rollup, re.M))
    assert listed == _neutral_earnings_symbols()


def test_no_call_lines_carry_form_date_and_conviction(rendered):
    earnings = _section(rendered, "Earnings Analysis")
    rollup = earnings[earnings.index("### Read, no call") :]
    for line in rollup.splitlines():
        if not line.startswith("- "):
            continue
        # symbol | FORM (YYYY-MM-DD) | conviction X [provenance]
        assert re.match(
            r"^- [A-Z][A-Z0-9.\-]* \| \S+ \(\d{4}-\d{2}-\d{2}\) \| conviction \S+",
            line,
        ), line


def test_directional_filings_keep_the_full_bounded_verdict(rendered):
    """The roll-up must not swallow a real call. A directional read still
    ships its call, conviction, thesis, falsifier and audit pointer."""
    earnings = _section(rendered, "Earnings Analysis")
    blocks = [
        b
        for b in re.split(r"\n(?=### )", earnings)
        if b.startswith("### ")
        and not b.startswith("### Read, no call")
        # The two JUST FILED placeholders are a third case: a filing dropped
        # today with the seat still working. They have no analysis at all, so
        # they are neither a call nor a no-call, and they keep their own block.
        and "JUST FILED" not in b.splitlines()[0]
    ]
    assert len(blocks) == 65 - 38
    for block in blocks:
        assert "- Call: " in block
        assert "- Thesis: " in block
        assert "- Invalidated if: " in block
        assert "- Full 8-field extraction" in block
        assert re.search(r"- Call: (bullish|bearish|mixed) ", block), block


def test_a_mixed_read_is_never_rolled_up():
    """A conclusion that hides a disagreement is worse than the raw prose.
    `mixed` is a split between sources, not an absence of one, so it must
    keep its full block even though it is not a clean direction."""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    assert agent._collapse_stances(["mixed"]) == "mixed"
    assert agent._collapse_stances(["bullish", "bearish"]) == "mixed"
    # And the renderer's partition only rolls up None/neutral.
    assert agent._collapse_stances(["neutral"]) == "neutral"
    assert agent._collapse_stances([None]) is None
    assert agent._collapse_stances(["bullish"]) == "bullish"


def test_neutral_technical_reads_drop_the_none_fields_not_the_conclusion(rendered):
    tech = _section(rendered, "Technical Analysis Reports")
    neutral_lines = [ln for ln in tech.splitlines() if re.match(r"^- \S+: neutral ", ln)]
    assert neutral_lines, "fixture should carry neutral technical reads"
    for line in neutral_lines:
        assert "Entry: None" not in line
        assert "Stop: None" not in line
        assert "Target: None" not in line
        assert "R/R n/a" not in line
        assert "not sizeable this session" in line


def test_every_analysed_symbol_still_appears_in_the_technical_section(rendered):
    tech = _section(rendered, "Technical Analysis Reports")
    listed = set(re.findall(r"^- ([A-Z][A-Z0-9.\-]*): ", tech, re.M))
    assert listed == {a.symbol for a in _LEVEL_LESS.analyses}


def test_neutral_reads_keep_the_analysts_own_sentence(rendered):
    """The one-sentence conclusion IS the content of a neutral read. It is
    rendered verbatim and untruncated — the change is a shape change, not a
    cap."""
    tech = _section(rendered, "Technical Analysis Reports")
    by_symbol = {a.symbol: a for a in _LEVEL_LESS.analyses}
    for symbol, analysis in by_symbol.items():
        if analysis.rating != "neutral" or analysis.risk_reward is not None:
            continue
        if not analysis.reasoning:
            continue
        assert f"  Reasoning: {analysis.reasoning}" in tech


def test_a_read_with_geometry_keeps_every_field(rendered):
    tech = _section(rendered, "Technical Analysis Reports")
    sized = [a for a in _LEVEL_LESS.analyses if a.rating != "neutral" and a.risk_reward is not None]
    assert sized
    for analysis in sized:
        assert f"Entry: {analysis.entry_price} | Stop: {analysis.stop_loss}" in tech


def test_dissent_and_the_net_agreement_arithmetic_are_untouched(rendered):
    """Every registry symbol still gets its aligned/opposed/net line in both
    directions. This is how a seat arguing the other way reaches the PM, and
    nothing in the shape change may quieten it."""
    agreement = _section(rendered, "Independent Source Agreement")
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    registry = agent.build_evidence_registry(
        analyses=_LEVEL_LESS.analyses,
        positions=_LEVEL_LESS.positions,
        news_intel=_LEVEL_LESS.news,
        earnings_analyses=_LEVEL_LESS.raw["earnings_analyses"],
        macro_analysis=_LEVEL_LESS.raw["macro_analysis"],
        smart_money_findings=[],
        symbol_sectors={},
    )
    assert registry
    for symbol in registry:
        assert re.search(
            rf"^- {re.escape(symbol)}: if long \d+ seat\(s\) for / \d+ against, entry backstop [+-]\d+; "
            rf"if short \d+ seat\(s\) for / \d+ against, entry backstop [+-]\d+",
            agreement,
            re.M,
        ), symbol


def test_a_no_call_earnings_stance_still_reaches_the_registry(rendered):
    """A neutral earnings read is rolled up in the prose section but must
    still count as a non-aligned source in the block that ceilings size —
    otherwise shortening the input would have quietly RAISED sizing."""
    registry_section = _section(rendered, "Canonical Current Evidence Registry")
    neutral = _neutral_earnings_symbols()
    covered = {symbol for symbol in neutral if f'"{symbol}"' in registry_section}
    assert covered, "no rolled-up symbol survived into the registry"
    for symbol in covered:
        block = registry_section[registry_section.index(f'"{symbol}"') :][:400]
        assert '"earnings"' in block, symbol


# ---------------------------------------------------------------------------
# item 18e — an instruction in the briefing must have somewhere to send its
# answer
# ---------------------------------------------------------------------------


def test_the_macro_audit_instruction_has_an_output_field(rendered: str) -> None:
    """The briefing ships the macro seat's whole reasoning chain verbatim
    and tells the seat to audit it for logic errors. Until 2026-09-14 the
    output schema had no field of any kind such a finding could go in, so
    the instruction asked for work with nowhere to put the answer — which is
    worse than bulk, because it is bulk that also asks for effort.

    This is the whole of the claim, and it is deliberately structural: this
    desk has no rig able to validate a prompt rewrite (the rehearsal rig
    replays recorded answers into a changed prompt and passes regardless),
    so what is asserted here is that the channel EXISTS, never that the
    seat decides any better for having it.
    """
    from src.models import ReasoningChain

    assert "audit these for logic errors" in rendered, (
        "the briefing no longer asks PM to audit macro's reasoning; if that "
        "was deliberate, this test and `macro_audit` should go with it"
    )
    assert "macro_audit" in ReasoningChain.model_fields, (
        "the briefing asks PM to audit macro's reasoning chain for logic "
        "errors and the schema has nowhere to report one"
    )
    # The instruction must name the field, or the seat has to guess where an
    # answer goes — which is the same defect one step removed.
    assert "reasoning_chain.macro_audit" in rendered


def test_macro_audit_is_not_mandatory_at_the_schema_layer() -> None:
    """Deliberately NOT `min_length=1`. Forcing a non-empty string when the
    macro chain is sound is exactly how the fabricated `or "n/a"`
    placeholders started on this desk (see `ExitReviewChain`). The prompt
    asks for a real verdict either way — name the error, or say none was
    found — and "no logic error found" is an answer, not a placeholder.

    It is also what keeps every archived log parseable: all of them predate
    the field.
    """
    from src.models import ReasoningChain

    field = ReasoningChain.model_fields["macro_audit"]
    assert field.default == "", "macro_audit must validate when absent"
    assert (
        ReasoningChain.model_validate(
            {
                "macro_filter": "m",
                "news_check": "n",
                "earnings_check": "e",
                "signal_conflicts": "s",
                "sizing_logic": "z",
                "portfolio_balance": "b",
                "cash_target": "c",
            }
        ).macro_audit
        == ""
    )


def test_the_risk_seat_can_see_whether_the_macro_audit_happened() -> None:
    """A channel nobody reads is not a channel. The seat whose job is to
    audit PM gets `macro_audit` as its own labelled row, and a blank one
    renders as NOT PERFORMED rather than silently as nothing."""
    from src.agents import risk_review_mode

    rows = risk_review_mode.chain_rows(risk_review_mode.MORNING_PLAN)
    by_attr = {attr: (label, mandatory) for label, attr, mandatory in rows}
    assert "macro_audit" in by_attr, (
        "the risk seat never sees PM's macro audit, so nothing downstream can tell a performed audit from a skipped one"
    )
    label, mandatory = by_attr["macro_audit"]
    assert mandatory, (
        "macro_audit is mandatory in PM's prompt and optional in the "
        "schema, so a blank one must banner as NOT PERFORMED"
    )
    assert "macro" in label.lower() and "audit" in label.lower()
