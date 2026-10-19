"""The 2026-09-25 owner ROLE-BASED conviction bar: earn the right to ENTER and STAY.

Mandate (verbatim intent):
  * The TECHNICAL (chart) seat is a TIMING VETO, not a weighted yes-vote. A
    broken/hostile chart blocks ENTRY even when the fundamental thesis is
    strong ("right name, wrong time"). Technical adds no positive weight; it
    only GATES.
  * The own-bar clears only when at least one NON-technical seat took a
    SUPPORTED DIRECTIONAL side (a real directional call backed by evidence and
    an invalidation, not a bare neutral shrug) AND no seat is opposed. This is
    NOT a genuine specificity/falsifiability test — News and Smart-money
    always synthesise their invalidation, so it cannot distinguish a templated
    reason from an analyst-authored one.
  * ONE bar, TWO verdicts (owner ruling 2026-09-25). ENTRY
    (`candidate_eligibility` R7) is full-strict: any failure refuses the buy.
    The STAY side is OPPOSITION-ONLY: a currently-HELD name is culled (into the
    same `blocked` set, via rotation's `ineligible_hold` tier) ONLY when a seat
    turns ACTIVELY OPPOSED to the held direction. A held name that merely fails
    the entry bar on SOFT grounds (no technical read, neutral/non-confirming
    technical, or support faded to neutral) is dropped from the fresh-entry
    ranking but NOT culled — it earns its right to stay. There is no
    confirmation counter; deterioration is handled by the separate
    price-thesis-break exit (`src.risk.exit_guard`).
"""

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import AnalystVerdict, rating_rung_strength, VerdictEvidence
from src.verdicts import RankedCandidate
from src.risk.rules import (
    OWN_BAR_REASON_PREFIX,
    own_bar_block_reason,
    own_bar_opposition_reason,
    seat_objections,
)
from src.rotation import (
    CONVICTION_BAR_REASON_PREFIX,
    holdings_below_entry_bar,
)


def _v(
    seat,
    symbol="X",
    *,
    direction="bullish",
    conviction="medium",
    invalidation="closes back below the breakout level",
    evidence=True,
):
    # Strength, item 65 (2026-09-26): every seat but technical states NONE,
    # which is `NO_STATED_STRENGTH` / `None` — the ABSENCE of a number, not
    # the number 0.0, which would be a strength read off a scale those seats
    # do not have. Technical does have one (its rating rungs), so a technical
    # fixture carries a real rung rather than a placeholder; a directional
    # verdict at a literal 0.0 is refused at construction now.
    if seat == "technical" and direction != "neutral":
        magnitude = rating_rung_strength("buy")
    else:
        magnitude = None
    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction=direction,
        magnitude=magnitude,
        conviction=conviction,
        evidence=(
            [VerdictEvidence(label="ev", text="a checkable observed fact")]
            if (evidence and direction != "neutral")
            else []
        ),
        invalidation=(invalidation if direction != "neutral" else ""),
    )


def _macro(symbol="X", *, direction="bearish", sector_specific):
    """A MACRO verdict. `sector_specific=True` carries a `sector_stance:<sector>`
    evidence label, exactly as `MacroAnalysis.to_verdict` stamps a sector-driven
    direction; `False` is the market-wide `equity_outlook` broadcast, whose
    evidence carries no such label."""
    label = "sector_stance:energy" if sector_specific else "equity_outlook"
    return AnalystVerdict(
        seat="macro",
        symbol=symbol,
        direction=direction,
        magnitude=None,  # macro states no strength — item 65, see `_v`
        conviction="medium",
        evidence=[VerdictEvidence(label=label, text="a checkable observed fact")],
        invalidation="the broad regime call reverses",
    )


def _rank(symbol, direction="bullish"):
    return RankedCandidate(symbol=symbol, direction=direction, score=1.0)


def _apply(ranked, verdicts, held=frozenset()):
    return PortfolioManagerAgent._apply_conviction_bar(
        ranked=ranked,
        blocked={},
        held_symbols=set(held),
        all_verdicts=verdicts,
    )


# ---------------------------------------------------------------------------
# The pure role-based bar
# ---------------------------------------------------------------------------


def test_reason_prefix_matches_rotation_gate():
    # The STAY cull and holdings_below_entry_bar recognise R7 by this prefix;
    # if the two ever drift, the cull stops recognising the right reasons.
    assert OWN_BAR_REASON_PREFIX == CONVICTION_BAR_REASON_PREFIX


def test_clears_when_technical_confirms_one_supportive_thesis_none_opposed():
    verdicts = [_v("technical"), _v("news")]
    assert own_bar_block_reason(verdicts, direction="bullish") is None


def test_vlo_all_medium_two_supportive_but_no_technical_read_is_refused():
    """The VLO 2026-09-24 shape: two fundamental seats agree at MEDIUM, none
    opposed, but there is NO confirming technical read this review -> timing
    cannot be confirmed, so the name is refused (right name, wrong time)."""
    verdicts = [_v("news"), _v("macro")]  # no technical seat at all
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None
    assert reason.startswith(OWN_BAR_REASON_PREFIX)
    assert "technical" in reason.lower()


def test_strong_fundamental_but_broken_chart_is_refused():
    """A genuinely convinced fundamental seat with a specific thesis, but the
    chart is HOSTILE (technical bearish) -> Technical timing veto. Right name,
    wrong time."""
    verdicts = [
        _v("news", conviction="high"),
        _v("earnings", conviction="high"),
        _v("technical", direction="bearish"),
    ]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None and reason.startswith(OWN_BAR_REASON_PREFIX)


def test_neutral_chart_does_not_confirm_timing_is_refused():
    verdicts = [_v("news"), _v("technical", direction="neutral")]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None and "does not back the trade's direction" in reason


def test_one_opposed_seat_blocks_even_with_confirming_chart_and_thesis():
    """Desk archetype (2026-10-19): an opposed name-level seat no longer
    blocks; the name is admitted and the seat becomes an objection."""
    verdicts = [
        _v("technical"),
        _v("news"),
        _v("earnings", direction="bearish"),
    ]
    assert own_bar_block_reason(verdicts, direction="bullish") is None
    objections = seat_objections(verdicts, direction="bullish")
    assert [(o["seat"], o["direction"]) for o in objections] == [("earnings", "bearish")]
    assert objections[0]["reason"] == "a checkable observed fact"


def test_technical_alone_carries_no_positive_weight():
    """Technical confirming is necessary but NOT sufficient: with no
    name-level seat carrying a supported side, the bar refuses."""
    verdicts = [_v("technical", conviction="high")]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None
    assert "no name-level seat" in reason.lower()


def test_supportive_thesis_must_be_directional_not_a_neutral_shrug():
    """A non-technical seat that is merely NEUTRAL is not a specific falsifiable
    thesis, so a confirming chart plus only-neutral fundamentals refuses."""
    verdicts = [_v("technical"), _v("news", direction="neutral")]
    reason = own_bar_block_reason(verdicts, direction="bullish")
    assert reason is not None


def test_short_side_uses_bearish_as_supportive():
    verdicts = [
        _v("technical", direction="bearish"),
        _v("news", direction="bearish"),
    ]
    assert own_bar_block_reason(verdicts, direction="bearish") is None
    # a bullish name-level seat is an OBJECTION to a short, not a block
    verdicts.append(_v("earnings", direction="bullish"))
    assert own_bar_block_reason(verdicts, direction="bearish") is None
    assert [(o["seat"], o["direction"]) for o in seat_objections(verdicts, direction="bearish")] == [
        ("earnings", "bullish")
    ]


# ---------------------------------------------------------------------------
# ENTRY overlay (_apply_conviction_bar, consumed by the PM ranking)
# ---------------------------------------------------------------------------


def test_entry_vlo_shape_is_not_admitted():
    ranked = [_rank("VLO")]
    verdicts = [_v("news", "VLO"), _v("macro", "VLO")]  # no confirming chart
    survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []
    assert any(r.startswith(CONVICTION_BAR_REASON_PREFIX) for r in blocked["VLO"])


def test_entry_confirmed_thesis_is_admitted():
    ranked = [_rank("STRG")]
    verdicts = [_v("technical", "STRG"), _v("news", "STRG")]
    survivors, blocked = _apply(ranked, verdicts)
    assert [c.symbol for c in survivors] == ["STRG"]
    assert blocked == {}


def test_entry_broken_chart_is_refused():
    ranked = [_rank("RITE")]
    verdicts = [
        _v("news", "RITE", conviction="high"),
        _v("earnings", "RITE", conviction="high"),
        _v("technical", "RITE", direction="bearish"),
    ]
    survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []
    assert any(r.startswith(CONVICTION_BAR_REASON_PREFIX) for r in blocked["RITE"])


def test_entry_thinly_covered_single_seat_refused():
    ranked = [_rank("SOLO")]
    verdicts = [_v("technical", "SOLO")]  # confirming chart, no fundamental thesis
    survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []


# ---------------------------------------------------------------------------
# Safety invariant: every ranked name already carries a technical verdict, so
# own_bar_block_reason's "absent technical read" branch is never the reason a
# REAL ranked name gets blocked. Nothing enforced this before; these two tests
# pin it so a future refactor that breaks the invariant is caught, not
# silently shipped as a mass-cull.
# ---------------------------------------------------------------------------


def test_ranked_names_with_technical_verdicts_never_hit_absent_technical_branch():
    """Every symbol here HAS a technical verdict (one clears, one clears with
    an objection, one has no supportive thesis). None of the blocked reasons
    may be the "no technical read this review" text."""
    ranked = [_rank("CLEAR"), _rank("OPPOSED"), _rank("NOTHESIS")]
    verdicts = [
        _v("technical", "CLEAR"),
        _v("news", "CLEAR"),
        _v("technical", "OPPOSED"),
        _v("news", "OPPOSED"),
        _v("earnings", "OPPOSED", direction="bearish"),
        _v("technical", "NOTHESIS"),
    ]
    survivors, blocked = _apply(ranked, verdicts)
    assert [c.symbol for c in survivors] == ["CLEAR", "OPPOSED"]
    assert set(blocked) == {"NOTHESIS"}
    absent_technical_text = "no technical read this review"
    for reasons in blocked.values():
        for r in reasons:
            assert absent_technical_text not in r


def test_ranked_name_missing_a_technical_verdict_is_blocked_not_silently_culled():
    """Documents the intended behavior of the invariant break itself: a
    ranked name that (contrary to the invariant) has NO technical verdict at
    all is REFUSED via the absent-technical reason and shows up in `blocked`
    -- it is never dropped from both `survivors` and `blocked` at once, which
    would be a silent cull a future change could introduce unnoticed."""
    ranked = [_rank("GHOST")]
    verdicts = [_v("news", "GHOST"), _v("macro", "GHOST")]  # no technical seat
    survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []
    assert "GHOST" in blocked
    assert any(r.startswith(CONVICTION_BAR_REASON_PREFIX) and "technical" in r.lower() for r in blocked["GHOST"])


def test_invariant_break_logs_a_warning_not_a_crash(caplog):
    """The runtime guard: a ranked name with no technical verdict at all is
    the upstream invariant breaking, which must be surfaced loudly (a
    warning) -- never raised, never silent."""
    import logging

    ranked = [_rank("GHOST")]
    verdicts = [_v("news", "GHOST")]  # no technical seat -> invariant broken
    with caplog.at_level(logging.WARNING, logger="src.agents.portfolio_manager"):
        survivors, blocked = _apply(ranked, verdicts)
    assert survivors == []
    assert any("GHOST" in rec.message and "technical" in rec.message.lower() for rec in caplog.records)


def test_invariant_holds_no_warning_when_technical_verdict_present(caplog):
    """The counterpart: when the invariant holds (every ranked name has a
    technical verdict), the guard must stay silent -- it only fires on the
    break, never as noise on the normal path."""
    import logging

    ranked = [_rank("KEEP2")]
    verdicts = [_v("technical", "KEEP2"), _v("news", "KEEP2")]
    with caplog.at_level(logging.WARNING, logger="src.agents.portfolio_manager"):
        survivors, blocked = _apply(ranked, verdicts)
    assert [c.symbol for c in survivors] == ["KEEP2"]
    assert not any("ranked-implies-technical" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# The OPPOSITION-only subset — the STAY cull test (owner ruling 2026-09-25)
# ---------------------------------------------------------------------------


def test_opposition_reason_none_on_soft_misses():
    """A confirming/absent/neutral chart with no opposed seat is NOT opposition
    — the STAY side must return None for every soft case so a held name is not
    culled on it."""
    # supported, clears entirely -> not opposition
    assert own_bar_opposition_reason([_v("technical"), _v("news")], direction="bullish") is None
    # no technical read this review (soft) -> not opposition
    assert own_bar_opposition_reason([_v("news"), _v("macro")], direction="bullish") is None
    # neutral/non-confirming technical (soft) -> not opposition
    assert own_bar_opposition_reason([_v("technical", direction="neutral"), _v("news")], direction="bullish") is None
    # support faded to neutral, chart still fine (soft) -> not opposition
    assert own_bar_opposition_reason([_v("technical"), _v("news", direction="neutral")], direction="bullish") is None


def test_opposition_reason_fires_only_on_active_opposition():
    # technical opposed
    r = own_bar_opposition_reason([_v("technical", direction="bearish"), _v("news")], direction="bullish")
    assert r is not None and "technical" in r.lower() and r.startswith(OWN_BAR_REASON_PREFIX)
    # a name-level seat opposed with the chart confirming: an objection for
    # the reviewer, not a cull (desk archetype, 2026-10-19)
    r = own_bar_opposition_reason(
        [_v("technical"), _v("news"), _v("earnings", direction="bearish")], direction="bullish"
    )
    assert r is None


# ---------------------------------------------------------------------------
# STAY through _apply_conviction_bar: held names are opposition-only culls
# ---------------------------------------------------------------------------


def test_stay_soft_neutral_read_is_not_culled():
    """(a) A HELD name whose technical read is NEUTRAL this review fails the
    strict entry bar, but no seat is opposed — it must NOT land in `blocked`,
    so the ineligible_hold cull never sees it. It earns its right to stay."""
    survivors, blocked = _apply(
        [_rank("HELD")],
        [_v("technical", "HELD", direction="neutral"), _v("news", "HELD")],
        held={"HELD"},
    )
    assert survivors == []  # dropped from the fresh-entry order
    assert "HELD" not in blocked
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ()


def test_stay_no_technical_read_is_not_culled():
    """A HELD name with no chart read this review (soft): not opposition, not
    culled."""
    survivors, blocked = _apply(
        [_rank("HELD")],
        [_v("news", "HELD"), _v("macro", "HELD")],
        held={"HELD"},
    )
    assert survivors == []
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ()


def test_stay_support_faded_to_neutral_is_not_culled():
    """A HELD name whose only fundamental support faded to NEUTRAL while the
    chart still confirms: soft miss, no opposition -> not culled."""
    survivors, blocked = _apply(
        [_rank("HELD")],
        [_v("technical", "HELD"), _v("news", "HELD", direction="neutral")],
        held={"HELD"},
    )
    assert survivors == []
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ()


def test_stay_technical_opposed_is_culled_on_first_review():
    """(b) A HELD name with the chart actively HOSTILE (technical opposed) IS
    culled on the FIRST such review — no counter, no second-miss wait."""
    survivors, blocked = _apply(
        [_rank("HELD")],
        [_v("technical", "HELD", direction="bearish"), _v("news", "HELD")],
        held={"HELD"},
    )
    assert survivors == []
    assert any(r.startswith(CONVICTION_BAR_REASON_PREFIX) for r in blocked["HELD"])
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ("HELD",)


def test_stay_nontechnical_seat_opposed_is_culled_on_first_review():
    """A HELD name with a confirming chart but a name-level seat opposed is
    NOT culled any more: the objection is carried for the reviewer."""
    survivors, blocked = _apply(
        [_rank("HELD")],
        [
            _v("technical", "HELD"),
            _v("news", "HELD"),
            _v("earnings", "HELD", direction="bearish"),
        ],
        held={"HELD"},
    )
    assert [c.symbol for c in survivors] == ["HELD"]
    assert blocked == {}
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ()
    assert [o["seat"] for o in PortfolioManagerAgent._candidate_ranking.last_objections["HELD"]] == ["earnings"]


def test_stay_intact_thesis_held_name_is_kept_and_not_culled():
    """A held name that CLEARS the bar stays in the survivors and is never
    offered to the cull tier."""
    survivors, blocked = _apply(
        [_rank("KEEP")],
        [_v("technical", "KEEP"), _v("news", "KEEP")],
        held={"KEEP"},
    )
    assert [c.symbol for c in survivors] == ["KEEP"]
    assert holdings_below_entry_bar(blocked, {"KEEP"}) == ()


def test_entry_candidate_soft_miss_is_still_blocked_strictly():
    """(c) The SAME soft shape that spares a HELD name must still REFUSE an
    ENTRY candidate — entry stays full-strict."""
    survivors, blocked = _apply(
        [_rank("NEW")],
        [_v("technical", "NEW", direction="neutral"), _v("news", "NEW")],
        held=frozenset(),  # not held -> entry candidate
    )
    assert survivors == []
    assert any(r.startswith(CONVICTION_BAR_REASON_PREFIX) for r in blocked["NEW"])
    assert holdings_below_entry_bar(blocked, {"NEW"}) == ("NEW",)


def test_no_dead_references_to_removed_stay_counter():
    """(d) The made-up two-review counter and all its plumbing are gone."""
    import src.rotation as rotation
    import src.storage.db as db_mod
    from src.agents.portfolio_manager import PortfolioManagerAgent as PM

    assert not hasattr(rotation, "CONVICTION_STAY_CONFIRMATION_REVIEWS")
    assert not hasattr(rotation, "apply_stay_confirmation")
    assert not hasattr(rotation, "_strip_conviction_reason")
    assert not hasattr(db_mod.Database, "save_conviction_bar_miss")
    assert not hasattr(db_mod.Database, "get_prior_conviction_bar_miss")
    assert not hasattr(db_mod.Database, "CONVICTION_BAR_MISS_KIND")
    import inspect

    assert "held_conviction_miss_prior" not in inspect.signature(PM.decide).parameters


# ---------------------------------------------------------------------------
# MACRO broadcast vs sector-specific: a market-wide macro flip is NOT
# per-name opposition (adversary bug on #723 — one bearish equity_outlook was
# culling the whole non-price-protected long book on a single review).
# ---------------------------------------------------------------------------


def test_broadcast_bearish_macro_is_not_opposition():
    """A held long with a confirming chart, a real supportive thesis, and a
    market-wide bearish macro BROADCAST (no sector stance) is NOT culled — the
    broad view is not a name-specific edge."""
    verdicts = [_v("technical"), _v("news"), _macro(sector_specific=False)]
    assert own_bar_opposition_reason(verdicts, direction="bullish") is None
    survivors, blocked = _apply(
        [_rank("HELD")],
        [_v("technical", "HELD"), _v("news", "HELD"), _macro("HELD", sector_specific=False)],
        held={"HELD"},
    )
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ()


def test_sector_specific_bearish_macro_is_opposition():
    """Desk archetype (2026-10-19): a sector-specific bearish macro stance is
    no longer opposition — macro tilts size only — so it never culls."""
    verdicts = [_v("technical"), _v("news"), _macro(sector_specific=True)]
    assert own_bar_opposition_reason(verdicts, direction="bullish") is None
    assert seat_objections(verdicts, direction="bullish") == []
    survivors, blocked = _apply(
        [_rank("HELD")],
        [_v("technical", "HELD"), _v("news", "HELD"), _macro("HELD", sector_specific=True)],
        held={"HELD"},
    )
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ()


def test_broadcast_bearish_macro_does_not_block_entry():
    """The same carve-out on the ENTRY side: a broad bearish macro must not
    refuse a new name that has a confirming chart and a supportive thesis."""
    verdicts = [_v("technical", "NEW"), _v("news", "NEW"), _macro("NEW", sector_specific=False)]
    assert own_bar_block_reason(verdicts, direction="bullish") is None
    survivors, blocked = _apply([_rank("NEW")], verdicts, held=frozenset())
    assert [c.symbol for c in survivors] == ["NEW"]


def test_sector_specific_bearish_macro_blocks_entry():
    """Desk archetype (2026-10-19): a sector-specific bearish macro does NOT
    refuse entry any more; with chart and news for, the name is admitted."""
    verdicts = [_v("technical", "NEW"), _v("news", "NEW"), _macro("NEW", sector_specific=True)]
    assert own_bar_block_reason(verdicts, direction="bullish") is None


def test_name_specific_opposition_culls_regardless_of_structure_or_capital():
    """Desk archetype (2026-10-19): a held name whose news seat turned
    against it is not culled by the bar; it stays out of `blocked` and its
    objection goes to the position reviewer (the keep rule)."""
    survivors, blocked = _apply(
        [_rank("HELD")],
        [_v("technical", "HELD"), _v("news", "HELD", direction="bearish")],
        held={"HELD"},
    )
    assert holdings_below_entry_bar(blocked, {"HELD"}) == ()
    assert "HELD" not in blocked


def test_sector_macro_is_one_vote_outweighed_by_two_name_level_supporters():
    """Owner mandate 2026-10-09: a sector-specific bearish macro stance is ONE
    opposing vote, not a veto. Two name-level seats backing the trade outnumber
    it, so the name clears both the ENTRY bar and the STAY test."""
    verdicts = [_v("technical", "NEW"), _v("news", "NEW"), _v("earnings", "NEW"), _macro("NEW", sector_specific=True)]
    assert own_bar_block_reason(verdicts, direction="bullish") is None
    assert own_bar_opposition_reason(verdicts, direction="bullish") is None


def test_sector_macro_vote_still_blocks_when_not_outweighed():
    """The retired 9 Oct majority vote: a sector-macro stance against one
    supporter no longer blocks, and an opposed name-level seat is an
    objection, not a block."""
    tie = [_v("technical"), _v("news"), _macro(sector_specific=True)]
    assert own_bar_block_reason(tie, direction="bullish") is None
    name_opposed = [_v("technical"), _v("news"), _v("earnings"), _v("smart_money", direction="bearish")]
    assert own_bar_block_reason(name_opposed, direction="bullish") is None
    assert [o["seat"] for o in seat_objections(name_opposed, direction="bullish")] == ["smart_money"]
