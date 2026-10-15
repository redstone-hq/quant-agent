"""Two live defects in how exits are justified and judged (2026-09-18).

Defect 1 — the exit gate rewarded reciting a phrase. On 2026-09-16 the two
real exits (COP SELL, EQNR REDUCE, run `midday-d8996a51`) carried the
reason ``"adverse news"``, verbatim and in their entirety. They passed the
substring gate because the phrase is on the list, and
`holding_discipline_claim_check` returned "ok" because it read the prose
for a claim it knew how to check and those two words made none. A seat
that phrased an exit as an unverifiable external event always got its way;
one that honestly described a stall got audited by the metric veto.

Defect 2 — `distance_to_stop_pct = (current - stop) / current * 100` is in
`_HIGHER_IS_BETTER`, so WIDENING the stop makes a position look improved.
Real recorded snapshots, 2026-08-31 close -> 2026-09-01 midday: V 1.42 ->
3.41 "improved" while r_multiple fell -0.11 -> -0.82; CMCSA 3.53 -> 5.58
while r fell -0.04 -> -0.33; DIS 1.85 -> 5.07 while progress fell -3.06 ->
-12.50.

Kept to the minimum that pins the structured trigger and the closed
loophole.
"""

import pytest

from src.models import PositionAction
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.risk.exit_guard import compute_deltas, holding_discipline_claim_check
from src.risk.exit_trigger import ExitTrigger, check_exit_trigger


# ---------------------------------------------------------------------------
# Defect 1 — the structured trigger
# ---------------------------------------------------------------------------


def test_the_trigger_vocabulary_cannot_diverge_from_the_executor_gate():
    """Mechanical, because the rule that relies on remembering slips.

    The assertion itself lives in `tests/test_exit_trigger_canonical_names.py`
    and is CALLED here, not copied: this file once carried its own copy, the
    chart-verified carve-out was added to the other one only, and the stale
    copy then failed a branch it was not describing.
    """
    from tests.test_exit_trigger_canonical_names import (
        assert_trigger_vocabulary_matches_executor_gate,
    )

    assert_trigger_vocabulary_matches_executor_gate()


def test_the_2026_09_16_two_word_exit_is_now_unsubstantiated():
    """The reason that executed twice on the live desk."""
    action = PositionAction(action="SELL", symbol="COP", reason="adverse news")
    check = check_exit_trigger(
        action=action.action,
        exit_trigger=action.exit_trigger,
        trigger_evidence=action.trigger_evidence,
        reason=action.reason,
        symbol=action.symbol,
    )
    # Mechanically healed to the trigger the prose names — nothing invented.
    assert check.trigger is ExitTrigger.ADVERSE_NEWS
    # ...but there is nothing behind it, so it is UNVERIFIABLE and re-asked.
    assert check.verdict == "unverifiable"
    assert check.needs_reask is True
    # Three-valued shape held: unverifiable LOGS, it does not block.
    assert check.blocks is False
    assert check.finding


def test_evidence_that_only_repeats_the_trigger_is_not_substantiation():
    check = check_exit_trigger(
        action="SELL",
        exit_trigger="adverse_news",
        trigger_evidence="adverse news",
        reason="adverse news",
        symbol="COP",
    )
    assert check.verdict == "unverifiable"
    assert check.needs_reask is True


def test_a_substantiated_exit_passes_clean():
    check = check_exit_trigger(
        action="SELL",
        exit_trigger="adverse_news",
        trigger_evidence=("Active News State Change 2026-09-16, COP(bearish), HIGH: oil below $100"),
        reason="adverse news weakens the commodity-price leg",
        symbol="COP",
    )
    assert check.verdict == "ok"
    assert check.substantiated is True
    assert check.needs_reask is False


def test_cannot_substantiate_is_a_first_class_recordable_outcome():
    """The seat must never be pushed into faking a trigger. Saying so is a
    legitimate answer that logs and re-asks — it does not block and is not
    an error."""
    action = PositionAction(
        action="SELL",
        symbol="V",
        reason="I want out of this one",
        exit_trigger="cannot_substantiate",
    )
    assert action.exit_trigger is ExitTrigger.CANNOT_SUBSTANTIATE
    check = check_exit_trigger(
        action=action.action,
        exit_trigger=action.exit_trigger,
        trigger_evidence=action.trigger_evidence,
        reason=action.reason,
        symbol=action.symbol,
    )
    assert check.verdict == "unverifiable"
    assert check.blocks is False
    assert check.needs_reask is True


def test_an_unrecognised_trigger_never_drops_the_action():
    """Losing an exit to a misspelled enum is the wrong failure direction —
    it reads as 'no trigger named' and is healed from the prose instead."""
    action = PositionAction(
        action="SELL",
        symbol="COP",
        reason="adverse news on the oil complex",
        exit_trigger="ADVERSE-NEWS-ISH",
    )
    assert action.exit_trigger is None
    check = check_exit_trigger(
        action=action.action,
        exit_trigger=action.exit_trigger,
        trigger_evidence="2026-09-16 COP(bearish) HIGH row",
        reason=action.reason,
        symbol=action.symbol,
    )
    assert check.trigger is ExitTrigger.ADVERSE_NEWS
    assert check.verdict == "ok"


def test_hold_is_never_asked_to_substantiate_anything():
    check = check_exit_trigger(
        action="HOLD",
        exit_trigger=None,
        trigger_evidence="",
        reason="thesis intact",
        symbol="NVDA",
    )
    assert check.verdict == "ok"
    assert check.needs_reask is False


@pytest.mark.parametrize(
    "state_changes, expected, blocks",
    [
        # No same-day row names COP either way -> cannot be checked. Log only.
        ("", "unverifiable", False),
        # A same-day row names COP BULLISH -> the claim is provably false.
        ("- [2026-09-18] Oil rally lifts producers → COP(bullish)\n", "false", True),
    ],
)
def test_the_structured_trigger_makes_provable_falsity_decidable(
    state_changes,
    expected,
    blocks,
):
    """Same two-word prose, same protected position. Without the structured
    trigger the fact-check returns "ok" and nothing is checked; with it, the
    claim is routed to the record it is about and the ratified three-valued
    outcome applies."""
    import datetime

    kwargs = dict(
        action="SELL",
        reason="adverse news",
        symbol="COP",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        state_change_parser=PortfolioManagerAgent._state_change_symbols_by_date,
        active_state_changes=state_changes,
        asof=datetime.date(2026, 9, 18),
    )
    # Before: the prose made no claim either regex recognised.
    assert holding_discipline_claim_check(**kwargs).verdict == "ok"
    # After: the trigger is in a field, so there is something to check.
    check = holding_discipline_claim_check(
        **kwargs,
        exit_trigger=ExitTrigger.ADVERSE_NEWS,
    )
    assert check.verdict == expected
    assert check.blocks is blocks


# ---------------------------------------------------------------------------
# Defect 2 — a wider stop is not an improvement
# ---------------------------------------------------------------------------

#: The three real 2026-09-01 midday snapshots, against the 2026-08-31 close.
#: Stops and prices are the implied ones (V entered at 381.18 with a 374.27
#: stop; the distance and r_multiple pairs are exactly as recorded).
_WIDENED = [
    ("V", 1.42, 3.41, 374.27, 362.70, 379.66, 375.51),
    ("CMCSA", 3.53, 5.58, 25.80, 24.65, 26.74, 26.10),
    ("DIS", 1.85, 5.07, 105.80, 100.10, 107.79, 105.45),
]


@pytest.mark.parametrize(
    "symbol, before, after, stop_then, stop_now, px_then, px_now",
    _WIDENED,
)
def test_a_wider_stop_is_not_an_improvement(
    symbol,
    before,
    after,
    stop_then,
    stop_now,
    px_then,
    px_now,
):
    d = compute_deltas(
        symbol,
        prior={"distance_to_stop_pct": before, "stop_loss": stop_then, "current_price": px_then},
        current={"atr_pct": 1.0, "distance_to_stop_pct": after, "stop_loss": stop_now, "current_price": px_now},
    )
    assert d.stop_driven == ["distance_to_stop_pct"]
    assert d.improved == []
    assert d.net_improved is False
    # And the reviewer is never shown the word "improved" for it.
    assert "(improved)" not in d.render()
    assert "wider stop, NOT an improvement" in d.render()


def test_price_moving_away_from_a_fixed_stop_still_counts():
    """What decomposition preserves that deletion would have lost: the
    genuine case the metric exists for."""
    d = compute_deltas(
        "AAA",
        prior={"distance_to_stop_pct": 2.0, "stop_loss": 98.0, "current_price": 100.0},
        current={"atr_pct": 1.0, "distance_to_stop_pct": 8.9, "stop_loss": 98.0, "current_price": 107.6},
    )
    assert d.stop_driven == []
    assert d.improved == ["distance_to_stop_pct"]
    assert d.net_improved is True


def test_a_tightened_stop_still_reads_as_worsened():
    """`worsened` is deliberately NOT decomposed. Discounting a stop-driven
    FALL could turn `net_improved` True and make the veto block an exit on
    paperwork — the wrong failure direction on this path."""
    d = compute_deltas(
        "AAA",
        prior={"distance_to_stop_pct": 8.0, "r_multiple": 0.5, "stop_loss": 92.0, "current_price": 100.0},
        current={
            "atr_pct": 1.0,
            "distance_to_stop_pct": 3.0,
            "r_multiple": 0.9,
            "stop_loss": 97.0,
            "current_price": 100.0,
        },
    )
    assert d.worsened == ["distance_to_stop_pct"]
    assert d.net_improved is False
