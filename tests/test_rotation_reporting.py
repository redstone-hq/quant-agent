"""Board item 219 — the pruning pass is reported to the owner, every session.

A silent pass is indistinguishable from a pass that never ran; that is the
whole defect. These tests are the specification for the four things the
report must always say.
"""

from src.rotation import (
    ROTATION_FULL_NOTHING_BETTER,
    ROTATION_ROOM_AVAILABLE,
    ROTATION_TELEMETRY_UNAVAILABLE,
    pruning_pass_lines,
)


def _record(**over) -> dict:
    record = {
        "outcome": ROTATION_ROOM_AVAILABLE,
        "held_examined": "AAA,BBB,CCC",
        "held_examined_count": 3,
        "held_below_entry_bar": "",
        "held_below_entry_bar_count": 0,
        "ranked_margin_enabled": False,
        "execute_enabled": True,
    }
    record.update(over)
    return record


def test_the_pass_says_it_ran_and_over_how_many_holdings():
    text = " ".join(pruning_pass_lines(_record()))
    assert "ran" in text
    assert "all 3 holdings" in text
    assert "AAA" in text and "BBB" in text and "CCC" in text


def test_the_kept_holdings_are_named_as_considered_and_kept():
    text = " ".join(pruning_pass_lines(_record()))
    assert "Considered and kept" in text
    assert "AAA, BBB, CCC" in text


def test_a_cut_is_reported_on_its_conviction_reason_not_on_pnl():
    lines = pruning_pass_lines(
        _record(
            outcome=ROTATION_FULL_NOTHING_BETTER,
            tier="ineligible_hold",
            held_symbol="BBB",
            held_reasons="rating fell below the entry bar",
            held_below_entry_bar="BBB",
            held_below_entry_bar_count=1,
        )
    )
    text = " ".join(lines)
    assert "BBB" in text
    assert "rating fell below the entry bar" in text
    assert "conviction reason" in text
    # The ruling: pruning is conviction-based, never P&L-based.
    assert "loss" not in text.lower().replace("profit-or-loss", "")
    # And the kept names must still be named, separately from the cut one.
    assert "Considered and kept: AAA, CCC" in text


def test_every_session_states_that_the_score_margin_tier_is_off():
    text = " ".join(pruning_pass_lines(_record()))
    assert "score-margin" in text
    assert "OFF" in text


def test_the_report_tells_the_truth_if_the_second_tier_is_ever_turned_on():
    text = " ".join(pruning_pass_lines(_record(ranked_margin_enabled=True)))
    assert "switched on" in text
    assert "OFF" not in text


def test_no_pruning_block_when_the_pass_did_not_run():
    """Claiming an examined count for a session that never compared
    anything would be the untrue owner line this item exists to remove."""
    assert pruning_pass_lines(_record(outcome=ROTATION_TELEMETRY_UNAVAILABLE)) == []


def test_an_empty_book_says_so_rather_than_claiming_a_count():
    text = " ".join(pruning_pass_lines(_record(held_examined="", held_examined_count=0)))
    assert "no holdings to examine" in text
    assert "score-margin" in text


# ---------------------------------------------------------------------------
# 2026-10-01. The gap that let a false owner line ship: NO test rendered
# `owner_precheck_lines` for an `ineligible_hold` opportunity at all. The
# categorical tier now runs before the "is the book even constrained"
# refusal, so the pre-check reaches the owner with a holding up to be cut,
# no replacement, and every limit slack.


def _precheck_rec(**over) -> dict:
    from src.rotation import ROTATION_HOLDING_BELOW_BAR

    record = {
        "outcome": ROTATION_HOLDING_BELOW_BAR,
        "headroom_pct": 14.5,
        "ceiling_pct": 25.0,
        "floor_pct": 0.5,
        "entry_budget_usd": 50_000.0,
        "min_entry_usd": 500.0,
        "binding": "",
        "tier": "ineligible_hold",
        "held_symbol": "OLD",
        "new_symbol": "",
        "held_reasons": "R2 rating below bar",
        "execute_enabled": True,
        "ranked_margin_enabled": False,
    }
    record.update(over)
    return record


def _assert_no_placeholders(text: str) -> None:
    assert "?" not in text
    assert "None" not in text


def test_below_bar_holding_with_room_and_no_replacement_tells_the_truth():
    from src.rotation import owner_precheck_lines

    text = " ".join(owner_precheck_lines(_precheck_rec()))
    _assert_no_placeholders(text)
    # The three false statements this test exists to prevent.
    assert "FULL" not in text
    assert "outranks" not in text
    assert "the weakest thing currently using the room" not in text
    # And what is actually true.
    assert "has room" in text
    assert "OLD" in text
    assert "R2 rating below bar" in text
    assert "would not be bought today" in text
    assert "nothing was ready to replace it" in text


def test_below_bar_holding_in_a_full_book_still_says_full():
    from src.rotation import owner_precheck_lines

    text = " ".join(
        owner_precheck_lines(
            _precheck_rec(
                binding="risk_budget",
                headroom_pct=0.2,
            )
        )
    )
    _assert_no_placeholders(text)
    assert "the book is FULL" in text
    assert "0.20% of risk headroom" in text
    # Still no replacement, so still nothing outranked anything.
    assert "outranks" not in text
    assert "nothing was ready to replace it" in text


def test_the_original_outranked_case_is_unchanged():
    from src.rotation import (
        ROTATION_FULL_OPPORTUNITY,
        owner_precheck_lines,
        precheck_outcome,
    )
    from src.rotation import RotationOpportunity, RotationPrecheck

    opportunity = RotationOpportunity(
        new_symbol="NEW",
        new_score=1.8,
        held_symbol="OLD",
        held_score=0.9,
        tier="ranked_margin",
    )
    precheck = RotationPrecheck(
        opportunity=opportunity,
        headroom_pct=0.2,
        ceiling_pct=25.0,
        floor_pct=0.5,
    )
    assert precheck_outcome(precheck) == ROTATION_FULL_OPPORTUNITY
    text = " ".join(
        owner_precheck_lines(
            _precheck_rec(
                outcome=ROTATION_FULL_OPPORTUNITY,
                binding="risk_budget",
                headroom_pct=0.2,
                tier="ranked_margin",
                new_symbol="NEW",
            )
        )
    )
    _assert_no_placeholders(text)
    assert "the book is FULL" in text
    assert "NEW outranks OLD, the weakest thing currently using the room" in text


def test_an_opportunity_without_a_replacement_is_not_the_outranked_outcome():
    from src.rotation import (
        ROTATION_HOLDING_BELOW_BAR,
        RotationOpportunity,
        RotationPrecheck,
        precheck_outcome,
    )

    precheck = RotationPrecheck(
        opportunity=RotationOpportunity(
            new_symbol=None,
            new_score=None,
            held_symbol="OLD",
            held_score=None,
            tier="ineligible_hold",
            reasons=("R2 rating below bar",),
        ),
        headroom_pct=14.5,
        ceiling_pct=25.0,
        floor_pct=0.5,
        entry_budget_usd=50_000.0,
        min_entry_usd=500.0,
    )
    assert precheck_outcome(precheck) == ROTATION_HOLDING_BELOW_BAR


def test_the_intraday_message_carries_the_pruning_pass(monkeypatch):
    """The 30-minute message used to omit the pass the morning one reports."""
    from src import trader_feed
    from src.trader_feed import intraday

    snap = trader_feed._empty_snapshot()
    snap["rotation"] = _record()
    monkeypatch.setattr(intraday, "_read_run", lambda *a, **kw: snap)
    monkeypatch.setattr(trader_feed, "_lookup_company_profiles", lambda *a, **kw: {})
    nested = {"status": "ok", "run_id": "r"}
    text = intraday._format_intraday({"status": "ok", "run_id": "r"}, nested, 1.0)
    assert "Considered and kept" in text
    assert "AAA, BBB, CCC" in text
