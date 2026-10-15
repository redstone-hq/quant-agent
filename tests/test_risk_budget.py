"""Risk budgeting under the total ceiling — spec §2.2.

The owner removed the correlated-group (cluster) limits on 15 Oct 2026 and
nothing replaced them: only the total at-risk ceiling rations here.
"""

from src.risk.budget import RiskRequest, allocate_risk_budget


def _req(**pcts):
    return [RiskRequest(sym, pct) for sym, pct in pcts.items()]


# --------------------------------------------------------------------------
# The uncontended case
# --------------------------------------------------------------------------


def test_requests_within_every_ceiling_are_served_in_full():
    alloc = allocate_risk_budget(_req(AAPL=3.0, XOM=2.0))
    assert alloc.granted("AAPL") == 3.0
    assert alloc.granted("XOM") == 2.0
    assert all(g.limited_by is None for g in alloc.grants.values())
    assert alloc.committed_pct == 5.0
    assert alloc.headroom_pct == 20.0


def test_a_denial_names_the_ceiling_that_produced_it():
    """A denied request produces no order at all. The operator has to be able
    to tell "the PM never asked" from "the budget refused it"."""
    alloc = allocate_risk_budget(
        _req(OKLO=5.0, CEG=5.0, VST=5.0),
        existing_pct={"HELD": 15.0},
        ceiling_pct=25.0,
    )
    assert alloc.grants["VST"].denied
    assert alloc.grants["VST"].limited_by == "below_floor"
    assert "total ceiling leaves 0.00%" in alloc.grants["VST"].note


# --------------------------------------------------------------------------
# The total ceiling
# --------------------------------------------------------------------------


def test_total_ceiling_rations_the_remainder_to_the_next_request():
    alloc = allocate_risk_budget(
        _req(AAPL=20.0, XOM=8.0),
        ceiling_pct=25.0,
    )
    assert alloc.granted("AAPL") == 20.0
    assert alloc.granted("XOM") == 5.0  # 25 - 20
    assert alloc.grants["XOM"].limited_by == "total_ceiling"
    assert alloc.committed_pct == 25.0
    assert alloc.headroom_pct == 0.0


def test_largest_request_is_served_first_regardless_of_listing_order():
    """Rationing must not depend on the order the PM happened to list its
    targets, or the same decision produces different books run to run."""
    forward = allocate_risk_budget(_req(SMALL=2.0, BIG=24.0), ceiling_pct=25.0)
    backward = allocate_risk_budget(_req(BIG=24.0, SMALL=2.0), ceiling_pct=25.0)
    assert forward.grants["BIG"].granted_pct == backward.grants["BIG"].granted_pct == 24.0
    assert forward.grants["SMALL"].granted_pct == backward.grants["SMALL"].granted_pct


def test_request_cut_below_the_floor_is_denied_not_shrunk_to_a_token():
    """A 0.1%-risk position pays full commission and full attention for an
    immaterial payoff."""
    alloc = allocate_risk_budget(
        _req(AAPL=24.8, XOM=5.0),
        ceiling_pct=25.0,
        floor_pct=0.5,
    )
    assert alloc.granted("AAPL") == 24.8
    assert alloc.grants["XOM"].denied  # only 0.2% left, under the floor
    assert alloc.grants["XOM"].limited_by == "below_floor"
    assert "under the 0.50% minimum" in alloc.grants["XOM"].note


# --------------------------------------------------------------------------
# Held positions
# --------------------------------------------------------------------------


def test_held_positions_consume_budget_even_when_this_session_ignores_them():
    """The way to release budget is a stop reaching entry or a sale — never
    the allocator forgetting an open position is there."""
    alloc = allocate_risk_budget(
        _req(NEW=10.0),
        existing_pct={"HELD": 20.0},
        ceiling_pct=25.0,
    )
    assert alloc.granted("NEW") == 5.0
    assert alloc.grants["NEW"].limited_by == "total_ceiling"
    assert alloc.committed_pct == 25.0


def test_resizing_a_held_name_replaces_its_risk_rather_than_adding_to_it():
    """Otherwise holding a name would make adding to it cost double, and a
    trim would be charged as though it were a new bet."""
    alloc = allocate_risk_budget(
        _req(HELD=4.0),
        existing_pct={"HELD": 3.0},
        ceiling_pct=25.0,
    )
    assert alloc.granted("HELD") == 4.0
    assert alloc.committed_pct == 4.0  # not 7.0


def test_trimming_is_never_blocked_by_a_full_budget():
    """Reducing risk must not require budget — a book over its ceiling would
    otherwise be unable to de-risk."""
    alloc = allocate_risk_budget(
        _req(HELD=1.0),
        existing_pct={"HELD": 30.0, "OTHER": 20.0},
        ceiling_pct=25.0,
    )
    assert alloc.granted("HELD") == 1.0
    assert alloc.grants["HELD"].limited_by is None


def test_closing_a_name_is_a_zero_grant_not_a_denial():
    alloc = allocate_risk_budget(_req(GONE=0.0), existing_pct={"GONE": 5.0})
    assert alloc.granted("GONE") == 0.0
    assert alloc.grants["GONE"].denied is False
    assert alloc.grants["GONE"].limited_by is None


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


def test_nan_and_negative_inputs_do_not_mint_budget():
    """Broker snapshots carry NaN and the PM is an LLM. Neither may produce a
    negative risk figure that credits the budget."""
    alloc = allocate_risk_budget(
        [RiskRequest("A", float("nan")), RiskRequest("B", -5.0), RiskRequest("C", float("inf"))],
        existing_pct={"HELD": float("nan"), "NEG": -10.0},
        ceiling_pct=25.0,
    )
    assert alloc.committed_pct == 0.0
    assert all(g.granted_pct == 0.0 for g in alloc.grants.values())


def test_a_duplicated_symbol_takes_the_last_request_not_the_sum():
    """Summing would silently double the size of a malformed PM decision."""
    alloc = allocate_risk_budget(
        [RiskRequest("AAPL", 3.0), RiskRequest("AAPL", 2.0)],
    )
    assert alloc.granted("AAPL") == 2.0


def test_symbols_are_matched_case_insensitively():
    alloc = allocate_risk_budget(
        [RiskRequest("oklo", 6.0)],
        existing_pct={"ceg": 23.0},
        ceiling_pct=25.0,
    )
    assert alloc.granted("OKLO") == 2.0


def test_zero_ceiling_denies_everything_without_raising():
    alloc = allocate_risk_budget(_req(AAPL=3.0), ceiling_pct=0.0)
    assert alloc.grants["AAPL"].denied
    assert alloc.headroom_pct == 0.0


# --------------------------------------------------------------------------
# retired board item 49 — best-ranked first (owner decision, 2026-09-12)
# --------------------------------------------------------------------------


def test_budget_is_spent_best_ranked_first_not_largest_request_first():
    """The item 49 defect, directly.

    WEAK asks for more risk than BEST. Under the pre-decision ordering
    (largest request first) WEAK took the ceiling and BEST got the remainder.
    The owner's decision is that the best-ranked idea is served first.
    """
    ranked = allocate_risk_budget(
        _req(WEAK=20.0, BEST=20.0),
        ceiling_pct=25.0,
        floor_pct=0.5,
        priority=["BEST", "WEAK"],
    )
    assert ranked.granted("BEST") == 20.0
    assert ranked.granted("WEAK") == 5.0

    # Same requests, ranking reversed: the ORDER, not the size, decides.
    reversed_ = allocate_risk_budget(
        _req(WEAK=20.0, BEST=20.0),
        ceiling_pct=25.0,
        floor_pct=0.5,
        priority=["WEAK", "BEST"],
    )
    assert reversed_.granted("WEAK") == 20.0
    assert reversed_.granted("BEST") == 5.0


def test_a_smaller_but_better_ranked_request_beats_a_bigger_worse_one():
    """The case the old ordering got exactly backwards: the best idea on the
    sheet asked for less risk than a weaker one, and lost the budget for it."""
    alloc = allocate_risk_budget(
        _req(WEAK=24.0, BEST=6.0),
        ceiling_pct=25.0,
        floor_pct=0.5,
        priority=["BEST", "WEAK"],
    )
    assert alloc.granted("BEST") == 6.0
    assert alloc.granted("WEAK") == 19.0
    assert alloc.grants["WEAK"].limited_by == "total_ceiling"


def test_no_ranking_supplied_keeps_the_pre_decision_ordering():
    """A caller with no ranking view must not have one invented for it: the
    backtest engine and every existing test path still ration largest-first."""
    alloc = allocate_risk_budget(_req(SMALL=2.0, BIG=24.0), ceiling_pct=25.0)
    assert alloc.granted("BIG") == 24.0
    assert alloc.granted("SMALL") == 1.0  # the remainder, largest served first


def test_an_unranked_symbol_never_outranks_a_ranked_one():
    """A PM target the ranking never scored is served AFTER every ranked
    name, however large its request — being unscored is not a promotion."""
    alloc = allocate_risk_budget(
        _req(UNRANKED=24.0, RANKED=6.0),
        ceiling_pct=25.0,
        floor_pct=0.5,
        priority=["RANKED"],
    )
    assert alloc.granted("RANKED") == 6.0
    assert alloc.granted("UNRANKED") == 19.0


def test_ranking_order_beats_listing_order():
    """Determinism, unchanged: the same decision must produce the same book
    whichever order the PM happened to emit its targets in."""
    forward = allocate_risk_budget(
        _req(A=20.0, B=20.0),
        ceiling_pct=25.0,
        priority=["B", "A"],
    )
    backward = allocate_risk_budget(
        _req(B=20.0, A=20.0),
        ceiling_pct=25.0,
        priority=["B", "A"],
    )
    assert forward.granted("B") == backward.granted("B") == 20.0
    assert forward.granted("A") == backward.granted("A") == 5.0


def test_ranking_is_case_and_whitespace_insensitive():
    alloc = allocate_risk_budget(
        _req(BEST=20.0, WEAK=20.0),
        ceiling_pct=25.0,
        priority=[" best ", "weak"],
    )
    assert alloc.granted("BEST") == 20.0


def test_a_closed_name_is_never_starved_by_its_place_in_the_ranking():
    """A zero request is PM closing the name. It consumes no budget, so its
    position in the queue cannot change the outcome — and it must never be
    turned into a refusal, which downstream reads as an order."""
    alloc = allocate_risk_budget(
        _req(GONE=0.0, BEST=25.0),
        existing_pct={"GONE": 10.0},
        ceiling_pct=25.0,
        priority=["BEST", "GONE"],
    )
    assert alloc.granted("GONE") == 0.0
    assert alloc.grants["GONE"].limited_by is None
    assert alloc.granted("BEST") == 25.0


# --- the open sub-question, both branches ---------------------------------


def test_partial_fit_is_fill_the_cut_line_candidate_is_taken_at_reduced_size():
    """`PARTIAL_FIT_POLICY == "fill"` — RATIFIED by the owner 2026-09-14
    (retired board item 49). Cutting the size does not damage the trade: same
    instrument, same stop, same reward-to-risk geometry, fewer shares. The
    existing floor, not a new number, decides when a remainder is too small."""
    from src.risk.budget import PARTIAL_FIT_POLICY

    assert PARTIAL_FIT_POLICY == "fill"
    alloc = allocate_risk_budget(
        _req(BEST=20.0, NEXT=10.0),
        ceiling_pct=25.0,
        floor_pct=0.5,
        priority=["BEST", "NEXT"],
    )
    assert alloc.granted("NEXT") == 5.0
    assert alloc.grants["NEXT"].limited_by == "total_ceiling"


def test_partial_fit_skip_branch_leaves_the_room_for_the_next_name_that_fits():
    """The REJECTED branch, kept executable rather than deleted.

    The owner ruled against it on 2026-09-14 — skipping the cut-line name and
    continuing down the ranking funds a worse-ranked idea purely because it
    costs less, which is the opposite of "go with the best". It stays written
    and covered so revisiting that ruling is a decision, not a rewrite. A
    skipped name is DENIED, never sized to zero-as-a-close.
    """
    alloc = allocate_risk_budget(
        _req(BEST=20.0, NEXT=10.0, THIRD=4.0),
        ceiling_pct=25.0,
        floor_pct=0.5,
        priority=["BEST", "NEXT", "THIRD"],
        partial_fit="skip",
    )
    assert alloc.granted("BEST") == 20.0
    assert alloc.granted("NEXT") == 0.0
    assert alloc.grants["NEXT"].limited_by == "partial_fit_skipped"
    assert alloc.grants["NEXT"].denied
    assert "skipped" in alloc.grants["NEXT"].note
    # The room NEXT did not take is still there for a name that fits in full.
    assert alloc.granted("THIRD") == 4.0
