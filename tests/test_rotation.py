"""Phase 14 — opportunity-cost rotation surfacing.

Hand-computed scenarios, `RankedCandidate` built directly, taking `ranked`
as already sorted best-first — exactly the contract `rank_verdicts` itself
guarantees and `test_analyst_verdict.py` already pins separately.

Two builders, because as of docs/WORK.md item 66 the module reads more than
`.symbol` and `.score`: `_rc` sets a score with no verdicts behind it (fine
for every path that never reaches the Tier 2 like-for-like check), and
`_rcv` builds real `AnalystVerdict`s and DERIVES the score from them with
`src/verdicts.py`'s own arithmetic, so a Tier 2 scenario's coverage and its
score cannot silently disagree.
"""

from functools import partial

from src.models import AnalystVerdict, VerdictEvidence
from src.rotation import evaluate_rotation_opportunity as _evaluate_rotation_opportunity
from src.verdicts import RankedCandidate, score_verdict

# The desk's own ratified floor (`STARTER_POSITION_RISK_PCT` /
# `RiskConfig.min_position_risk_pct`) — the exact number
# `_render_rotation_section` passes as `floor_pct`. Pinned here as a literal
# so a future change to that constant fails this test loudly instead of the
# scenarios below silently testing a different threshold than production.
FLOOR_PCT = 0.5

# Owner decision 2026-10-15 (q07): the ranked-margin tier is judged against a
# MEASURED score-noise brake (absolute score units), threaded in by the caller.
# These scenarios supply one as a test input; it is not a desk number.
TEST_NOISE_BRAKE = 0.25
evaluate_rotation_opportunity = partial(_evaluate_rotation_opportunity, noise_brake=TEST_NOISE_BRAKE)


def _rc(symbol: str, score: float, direction: str = "bullish") -> RankedCandidate:
    return RankedCandidate(symbol=symbol, direction=direction, score=score)


# `magnitude=None` is a seat that states NO strength (item 65, 2026-09-26 —
# `src/models.py::NO_STATED_STRENGTH`), which is what news, macro,
# smart_money and earnings always send. It scores the same as the literal
# 0.0 these fixtures used to pass, which is the point: the rotation margins
# below are unchanged by the item-65 encoding change.
def _verdict(
    symbol: str, seat: str, magnitude: float | None, conviction: str, direction: str = "bullish"
) -> AnalystVerdict:
    return AnalystVerdict(
        seat=seat,
        symbol=symbol,
        direction=direction,
        magnitude=magnitude,
        conviction=conviction,
        invalidation="the level breaks",
        evidence=[VerdictEvidence(label="close", value=100.0)],
    )


def _rcv(symbol: str, seats: dict[str, tuple[float, str]], direction: str = "bullish") -> RankedCandidate:
    """A candidate whose score is DERIVED from its own seat coverage.

    `seats` maps seat name -> (magnitude, conviction), and the score is
    `sum(score_verdict(verdict))` — the identical
    arithmetic `rank_verdicts` performs, so these scenarios exercise the
    real relationship between coverage and score rather than asserting one.
    """
    verdicts = [
        _verdict(symbol, seat, magnitude, conviction, direction) for seat, (magnitude, conviction) in seats.items()
    ]
    score = round(sum(score_verdict(v) for v in verdicts), 4)
    return RankedCandidate(
        symbol=symbol,
        direction=direction,
        score=score,
        verdicts=sorted(verdicts, key=lambda v: v.seat),
    )


# --- (a) clearly-stronger new candidate vs clearly-weaker/stale holding ----


def test_stronger_new_candidate_rotates_out_a_stale_ineligible_holding():
    """STALE is caught categorically: OLD fails the desk's own eligibility
    gates outright (R4/R5), so no margin is needed at all — it would not be
    bought today by the identical rule a new buy must clear."""
    # OLD is held but its own eligibility row carries real blocking reasons
    # — it never enters `ranked` at all, because `rank_candidates` only
    # ranks ELIGIBLE names. The categorical branch reads only its presence
    # (with reasons) in `blocked`, never a score.
    ranked = [_rc("NEW", 1.8)]
    blocked = {"OLD": ["R4 R/R 0.80 under the 1.50 floor and no current state-change row names it"]}
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked=blocked,
        held_symbols={"OLD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.tier == "ineligible_hold"
    assert opp.new_symbol == "NEW"
    assert opp.held_symbol == "OLD"
    assert opp.held_score is None
    assert opp.reasons


def test_stronger_new_candidate_rotates_out_a_weak_but_still_eligible_holding():
    """Both sides eligible: OLD ranks last among held names, NEW clears the
    25% margin (0.75 * 1.25 = 0.9375 <= 1.5).

    Both names are covered by the SAME single seat, so the item-66
    like-for-like sub-score is the whole score and the two checks coincide.
    """
    ranked = [
        _rcv("NEW", {"technical": (1.0, "medium")}),  # 1.5
        _rcv("OLD", {"technical": (0.75, "low")}),  # 0.75
    ]
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols={"OLD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.tier == "ranked_margin"
    assert opp.new_symbol == "NEW"
    assert opp.new_score == 1.5
    assert opp.held_symbol == "OLD"
    assert opp.held_score == 0.75
    assert opp.noise_brake == TEST_NOISE_BRAKE
    # Item 66: the comparison that was actually cleared is recorded.
    assert opp.shared_seats == ("technical",)
    assert opp.held_shared_score == 0.75
    assert opp.new_shared_score == 1.5


# --- (b) marginally-better new candidate does NOT trigger (respects margin) -


def test_marginal_edge_does_not_trigger_rotation():
    """NEW beats OLD but by less than the 25% margin: 0.75 * 1.25 = 0.9375,
    and 0.9 falls short of that — no churn on a noise-level difference."""
    ranked = [
        _rcv("NEW", {"technical": (0.9, "low")}),  # 0.9
        _rcv("OLD", {"technical": (0.75, "low")}),  # 0.75
    ]
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols={"OLD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_a_gap_exactly_at_the_measured_noise_does_not_trigger():
    """Owner decision 2026-10-15 (q07): the gap must EXCEED the measured
    score noise. A gap equal to it is indistinguishable from jitter."""
    ranked = [
        _rcv("NEW", {"technical": (0.9375, "low")}),  # 0.9375
        _rcv("OLD", {"technical": (0.75, "low")}),  # 0.75
    ]
    opp = _evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols={"OLD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
        noise_brake=ranked[0].score - ranked[1].score,
    )
    assert opp is None


# --- (c) capital NOT constrained -> no comparison runs at all --------------


def test_real_headroom_means_no_rotation_check_at_all():
    """Even an enormous, obviously-qualifying gap is not surfaced when the
    book has real room left — refusal-driven comparison only activates when
    it is actually needed, never as a standing "could we do better" nudge."""
    ranked = [_rc("NEW", 5.0), _rc("OLD", 0.1)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols={"OLD"},
        headroom_pct=10.0,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_headroom_exactly_at_the_floor_is_not_constrained():
    """`headroom_pct >= floor_pct` reads as "a new floor-sized idea still
    fits" — the boundary itself is not yet a constraint."""
    ranked = [_rc("NEW", 5.0), _rc("OLD", 0.1)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols={"OLD"},
        headroom_pct=FLOOR_PCT,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None


# --- (d) nothing to recommend -> no change to existing behaviour ----------


def test_no_new_candidate_means_nothing_to_recommend():
    ranked = [_rc("OLD", 0.9)]  # only a held name is even ranked
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols={"OLD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_no_held_position_ranked_or_blocked_means_nothing_to_compare():
    """Constrained capital, a strong new idea, but nothing held to weigh it
    against (e.g. an empty book) — there is no rotation to propose."""
    ranked = [_rc("NEW", 1.8)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols=set(),
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_empty_blocked_reasons_are_not_treated_as_a_blocking_row():
    """A `blocked` dict may carry a symbol with an empty reasons list (this
    codebase's own "empty list = eligible" convention, `candidate_eligibility`
    docstring) — that must not be misread as a categorical hit."""
    ranked = [
        _rcv("NEW", {"technical": (0.9, "low")}),
        _rcv("OLD", {"technical": (0.75, "low")}),
    ]
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={"OLD": []},
        held_symbols={"OLD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None  # falls through to the ranked-margin tier, which the
    # 0.9 vs 0.75 gap (same as the marginal test above) still does not clear


def test_multiple_ineligible_holdings_pick_the_worse_one_deterministically():
    """More blocking reasons is the worse candidate to keep; ties break
    alphabetically, matching this module's own documented tie-break."""
    ranked = [_rc("NEW", 1.8)]
    blocked = {
        "AAA": ["R4 one reason"],
        "BBB": ["R4 one reason", "R5 net evidence -1 if long — no rung"],
    }
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked=blocked,
        held_symbols={"AAA", "BBB"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.held_symbol == "BBB"
    # Board item 39: the WHOLE below-bar cull set is carried, worst-first, so
    # the execution stage can advance to the next-worst if BBB cannot be sold.
    assert opp.ineligible_candidates == (
        ("BBB", ("R4 one reason", "R5 net evidence -1 if long — no rung")),
        ("AAA", ("R4 one reason",)),
    )


def test_a_held_name_that_is_itself_the_best_ranked_candidate_is_not_compared_against_itself():
    """The strongest name overall happens to already be held — nothing to
    rotate into, since the new-candidate pool excludes anything held."""
    ranked = [_rc("HELD", 5.0), _rc("NEW", 1.0)]
    opp = evaluate_rotation_opportunity(
        ranked=ranked,
        blocked={},
        held_symbols={"HELD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    # NEW (1.0) vs HELD (5.0): 5.0 * 1.25 = 6.25 > 1.0, well under margin.
    assert opp is None


# --- (e) item 66: Tier 2 is coverage-neutral -------------------------------
#
# `rank_verdicts` became a weighted SUM on 2026-09-13 (correctly). A sum is
# not a rescaling of the average it replaced: the divisor it deletes is the
# name's own seat count, which differs per name. So two names' composites
# stopped being comparable term-for-term the moment their coverage differed
# — and Tier 2 compares exactly two such names. These pin that the margin
# must now also clear on the seats that scored BOTH names.


def test_coverage_decay_alone_does_not_rotate_a_held_name_out():
    """The item-66 case, end to end. HELD and NEW carry an IDENTICAL
    technical read — the strongest one that seat can give. NEW additionally
    has a live earnings filing and a confirmed flow; HELD's lapsed weeks
    ago. On the full composite that is 2.0 vs 3.5 and the 25% margin
    clears, so the pre-fix code would have surfaced a sale. On the one seat
    that scored both names it is 2.0 vs 2.0 — nothing about HELD is worse,
    so nothing is surfaced.
    """
    held = _rcv("HELD", {"technical": (1.0, "high")})
    new = _rcv(
        "NEW",
        {
            "technical": (1.0, "high"),  # 2.0, identical to HELD
            "earnings": (None, "high"),  # 1.0, coverage HELD lost
            "smart_money": (None, "medium"),  # 0.5, coverage HELD lost
        },
    )
    assert held.score == 2.0
    assert new.score == 3.5
    # The pre-fix comparison, spelled out so this test fails loudly if the
    # full-score margin ever stops being cleared by these inputs.
    assert new.score - held.score > TEST_NOISE_BRAKE

    opp = evaluate_rotation_opportunity(
        ranked=[new, held],
        blocked={},
        held_symbols={"HELD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_no_shared_scoring_seat_declines_rather_than_comparing():
    """Fail toward NOT selling. HELD is scored by earnings alone, NEW by
    technical alone; the full composites clear the margin (1.0 vs 2.0) but
    there is no seat that has an opinion on both, so there is nothing
    like-for-like to compare and Tier 2 declines."""
    held = _rcv("HELD", {"earnings": (None, "high")})  # 1.0
    new = _rcv("NEW", {"technical": (1.0, "high")})  # 2.0
    assert new.score - held.score > TEST_NOISE_BRAKE

    opp = evaluate_rotation_opportunity(
        ranked=[new, held],
        blocked={},
        held_symbols={"HELD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is None


def test_a_real_like_for_like_gap_still_rotates_and_records_the_comparison():
    """The check is not a blanket refusal. HELD's own chart is weak where
    NEW's is strong, so the gap survives on the shared seat and the
    rotation is surfaced — with the seats and both sub-scores recorded, so
    the audit trail shows which comparison was actually cleared."""
    held = _rcv("HELD", {"technical": (0.75, "low")})  # 0.75
    new = _rcv(
        "NEW",
        {
            "technical": (1.0, "high"),  # 2.0
            "earnings": (None, "high"),  # 1.0
        },
    )
    opp = evaluate_rotation_opportunity(
        ranked=[new, held],
        blocked={},
        held_symbols={"HELD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
    )
    assert opp is not None
    assert opp.tier == "ranked_margin"
    assert opp.held_symbol == "HELD"
    assert opp.new_symbol == "NEW"
    assert opp.shared_seats == ("technical",)
    assert opp.held_shared_score == 0.75
    assert opp.new_shared_score == 2.0
    # And the sub-score is a strict partial of the real composite, never a
    # second scoring scheme: earnings is the only term dropped from NEW.
    assert round(opp.new_score - opp.new_shared_score, 4) == 1.0


def test_the_like_for_like_check_can_only_remove_rotations_never_add_one():
    """The property, not an example: the shared-seat test is a CONJUNCTION
    with the full-score test, so every surfaced ranked-margin rotation also
    clears the original comparison. Swept over a grid of coverage and
    strength combinations."""
    convictions = ("low", "medium", "high")
    # `None` = the seat states no strength at all (item 65) — the case that
    # used to be spelled 0.0 and is the ONLY thing four of the five seats
    # ever send. A literal 0.0 on a directional verdict is refused at
    # construction now, so sweeping it here would test an impossible input.
    magnitudes = (None, 0.5, 1.0)
    seat_sets = (
        ("technical",),
        ("technical", "earnings"),
        ("earnings", "smart_money"),
        ("technical", "earnings", "smart_money", "news", "macro"),
    )
    surfaced = 0
    for held_seats in seat_sets:
        for new_seats in seat_sets:
            for magnitude in magnitudes:
                for conviction in convictions:
                    held = _rcv(
                        "HELD",
                        {s: (0.5, "low") for s in held_seats},
                    )
                    new = _rcv(
                        "NEW",
                        {s: (magnitude, conviction) for s in new_seats},
                    )
                    opp = evaluate_rotation_opportunity(
                        ranked=[new, held],
                        blocked={},
                        held_symbols={"HELD"},
                        headroom_pct=0.1,
                        floor_pct=FLOOR_PCT,
                    )
                    if opp is None:
                        continue
                    surfaced += 1
                    assert opp.tier == "ranked_margin"
                    # (1) the original full-composite margin still holds
                    assert new.score - held.score > TEST_NOISE_BRAKE
                    # (2) and so does the like-for-like one
                    assert opp.new_shared_score - opp.held_shared_score > TEST_NOISE_BRAKE
                    # (3) which was computed over a genuinely shared set
                    assert set(opp.shared_seats) == (set(held_seats) & set(new_seats))
    assert surfaced > 0, "grid surfaced nothing — it is not testing anything"


# --- (b) "no private execution path" regression guard ----------------------
#
# Phase 14b (2026-09-12) lets the desk ACT on the categorical tier behind
# `execution.rotation_enabled` — but only by appending an ordinary zero-size
# `TargetPosition` to the PM's plan in `DecisionStage`
# (`pipeline_stages._apply_rotation_execution`, tested in
# `tests/test_rotation_execute.py`). Everything downstream of that stays
# rotation-blind on purpose: `PortfolioConstructor` never imports or names
# this module, and `PortfolioDecision` / `TradeDecision` carry no rotation
# field, so a rotation close is built, risk-checked, reviewed and executed
# by EXACTLY the code a PM-authored close goes through. These tests pin that
# there is no second, rotation-specific route into order construction — if
# one is ever added, it starts by failing here.

import ast
import inspect
import pathlib

import src.portfolio_constructor as portfolio_constructor_module
from src.models import PortfolioDecision, TradeDecision


def _package_source(module) -> str:  # every *.py of the package, so no mixin hides
    return "\n".join(p.read_text() for p in sorted(pathlib.Path(inspect.getsourcefile(module)).parent.glob("*.py")))


def _imports_rotation(source: str) -> bool:
    """True if `source` imports anything from/as `src.rotation` or
    `rotation`, via any `import`/`from ... import` form (not just a
    textual grep, so a rename or an aliased import is still caught)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] == "rotation" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[-1] == "rotation":
                return True
    return False


def test_portfolio_constructor_module_never_imports_rotation():
    """The order-construction path must never even import `src.rotation` —
    a rotation close must reach it as a plain target, indistinguishable
    from a PM-authored one. A PR wiring rotation INTO construction would
    start here, and this fails the moment it does."""
    source = _package_source(portfolio_constructor_module)
    assert not _imports_rotation(source), (
        "src/portfolio_constructor.py must not import src.rotation — a "
        "rotation close is an ordinary zero-size target and the constructor "
        "must stay unable to tell it apart from a PM-authored one"
    )


def test_portfolio_constructor_construct_orders_never_references_rotation_by_name():
    """Belt-and-suspenders on top of the import check: even a local import
    inside a function body, or a same-module symbol literally named after
    rotation, would show up in the source text of the constructor's own
    module — catches the failure mode without depending on import style."""
    source = _package_source(portfolio_constructor_module)
    assert "rotation" not in source.lower(), (
        "src/portfolio_constructor.py source must not mention rotation at "
        "all — the constructor is the real order-execution path and must "
        "have no rotation-specific branch; a rotation close is built by the "
        "same code as any PM close"
    )


def test_portfolio_decision_and_trade_decision_carry_no_rotation_field():
    """The two data structures that actually reach execution/RM review must
    never carry a `RotationOpportunity` (or any rotation-named) field. The
    rotation close rides as an ordinary `TargetPosition`; its bookkeeping
    lives on `RunContext.rotation`, outside the decision objects, so the
    Risk Manager and the executor see a SELL like any other."""
    for model in (PortfolioDecision, TradeDecision):
        field_names = set(model.model_fields.keys())
        rotation_fields = {f for f in field_names if "rotation" in f.lower()}
        assert not rotation_fields, (
            f"{model.__name__} must not carry a rotation field, found "
            f"{rotation_fields} — RotationOpportunity is prompt-text only"
        )


def test_render_rotation_section_returns_plain_text_not_structured_data():
    """`_render_rotation_section` is the ONE place `RotationOpportunity`
    is consumed. Its contract must stay "renders to a prompt string" — not
    "returns something that could be attached to a decision object". If it
    is ever changed to return the `RotationOpportunity`/a dict/anything
    structured (the shape a future "wire it into the decision" change would
    need), this fails immediately."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    result = PortfolioManagerAgent._render_rotation_section(
        ranked=[_rc("NEW", 1.8)],
        blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
        held_symbols={"OLD"},
        existing_risk_pct=None,
        ceiling_pct=25.0,
    )
    assert isinstance(result, str)
    # `None` book-risk telemetry is the fail-open/skip path — still a str.
    assert "Opportunity Rotation" in result


def test_render_states_the_like_for_like_comparison_for_the_ranked_tier():
    """Item 66. When a ranked-margin rotation IS surfaced, the model is
    shown the coverage-neutral comparison that justified it, not only the
    two coverage-sensitive totals."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    result = PortfolioManagerAgent._render_rotation_section(
        ranked=[
            _rcv("NEW", {"technical": (1.0, "high"), "earnings": (None, "high")}),
            _rcv("HELD", {"technical": (0.75, "low")}),
        ],
        blocked={},
        held_symbols={"HELD"},
        existing_risk_pct={"HELD": 24.9},
        ceiling_pct=25.0,
        precheck=PortfolioManagerAgent.rotation_precheck(
            ranked=[
                _rcv("NEW", {"technical": (1.0, "high"), "earnings": (None, "high")}),
                _rcv("HELD", {"technical": (0.75, "low")}),
            ],
            blocked={},
            held_symbols={"HELD"},
            existing_risk_pct={"HELD": 24.9},
            ceiling_pct=25.0,
            score_noise_brake=TEST_NOISE_BRAKE,
        ),
    )
    assert "Like-for-like check" in result
    assert "technical" in result
    assert "0.75" in result and "2.00" in result


# ---------------------------------------------------------------------------
# 2026-09-23 — the precondition pointed at a constraint this book never hit.
#
# Across every retained log rotation (2026-08-21 .. 2026-09-23) the rendered
# section took the "real room exists" branch 51 times out of 51 and "Capital
# is constrained" never once, because the only test was RISK-budget headroom
# against the 0.50% floor while what actually binds this desk is the §11.2
# gross-exposure ladder and cash. The regression fixture below is the real
# book of 2026-09-23: 1.9908x gross of a 2.00x ceiling on ~$10,000 equity,
# $92.20 of ladder headroom, and 14.50% risk headroom of a 25% ceiling.
# ---------------------------------------------------------------------------

#: The real 2026-09-23 book, from the run log of that date.
TODAY_RISK_HEADROOM_PCT = 14.50
TODAY_DEPLOYABLE_USD = 92.20
#: The owner's minimum risk per position (owner rule 2026-08-27) judged at
#: the candidate's OWN stop: $100,000 equity, every candidate entered at
#: $100 with a $90 stop (10% away), so the smallest position clearing 0.5%
#: risk is 0.5% x $100,000 / 10% = $5,000 -- not 0.5% of equity, which
#: would only be right for a 100% stop.
TODAY_EQUITY_USD = 100_000.0
MIN_RISK_PCT = 0.5
MIN_ENTRY_USD = 5_000.0


class _EveryCandidateAt(dict):
    """Every candidate symbol previews at the same $100 entry / $90 stop."""

    def get(self, key, default=None):
        return (100.0, 90.0)


def _floor():
    from src.risk.min_risk import MinRiskFloor

    return MinRiskFloor(floor_pct=MIN_RISK_PCT, equity_usd=TODAY_EQUITY_USD, entry_stop=_EveryCandidateAt())


def _funding(entry_budget_usd):
    """The funding check exactly as `evaluate_rotation` builds it."""
    return _floor().check("NEW", entry_budget_usd)


def _today_ladder_full_kwargs(**over):
    """The 2026-09-23 book: real risk-budget room, no funding room at all."""
    kwargs = dict(
        headroom_pct=TODAY_RISK_HEADROOM_PCT,
        floor_pct=FLOOR_PCT,
        entry_budget_usd=TODAY_DEPLOYABLE_USD,
        min_risk_floor=_floor(),
    )
    kwargs.update(over)
    return kwargs


def test_a_book_full_on_the_gross_ladder_reaches_the_comparison():
    """THE REGRESSION. 14.50% risk headroom is nowhere near the 0.50% floor,
    so the old precondition returned `None` on the spot — at the exact
    moment $92.20 of deployable budget could not fund the $500 minimum
    order. The comparison must now be reached."""
    from src.rotation import evaluate_rotation

    outcome = evaluate_rotation(
        ranked=[_rc("NEW", 1.8)],
        blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
        held_symbols={"OLD"},
        **_today_ladder_full_kwargs(),
    )
    assert outcome.opportunity is not None, "the book could not fund a $500 order; the comparison must be made"
    assert outcome.opportunity.tier == "ineligible_hold"
    assert outcome.opportunity.held_symbol == "OLD"
    assert outcome.opportunity.new_symbol == "NEW"
    assert outcome.refusal is None


def test_the_risk_budget_test_still_binds_on_its_own():
    """The funding test is an addition, not a replacement. A book whose RISK
    budget is exhausted while it still has cash must still rotate — the
    failure mode of re-pointing at the ladder alone."""
    from src.rotation import evaluate_rotation

    outcome = evaluate_rotation(
        ranked=[_rc("NEW", 1.8)],
        blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
        held_symbols={"OLD"},
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
        entry_budget_usd=25_000.0,
        min_risk_floor=_floor(),
    )
    assert outcome.opportunity is not None
    assert outcome.refusal is None


def test_a_below_bar_holding_is_culled_even_with_room_on_every_constraint():
    """OWNER RULING 2026-10-01. The book having money is no longer a reason
    to keep a name that would not be bought today — that was the exact state
    on the live book (15.09% headroom, $1,266 deployable) while three held
    names sat below the bar."""
    from src.rotation import evaluate_rotation

    outcome = evaluate_rotation(
        ranked=[_rc("NEW", 1.8)],
        blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
        held_symbols={"OLD"},
        headroom_pct=TODAY_RISK_HEADROOM_PCT,
        floor_pct=FLOOR_PCT,
        entry_budget_usd=9_000.0,
        min_risk_floor=_floor(),
    )
    assert outcome.refusal is None
    assert outcome.opportunity is not None
    assert outcome.opportunity.tier == "ineligible_hold"
    assert outcome.opportunity.held_symbol == "OLD"


def test_a_below_bar_holding_is_culled_with_no_replacement_candidate():
    """And no candidate to rotate INTO is not a reason either. The
    replacement carries `None` rather than a placeholder score."""
    from src.rotation import evaluate_rotation

    outcome = evaluate_rotation(
        ranked=[_rc("OLD", 0.4)],  # every ranked name is already held
        blocked={"OLD": ["R3 not BUY-eligible"]},
        held_symbols={"OLD"},
        headroom_pct=0.2,
        floor_pct=FLOOR_PCT,
        entry_budget_usd=10.0,
        min_risk_floor=_floor(),
    )
    assert outcome.opportunity is not None
    assert outcome.opportunity.new_symbol is None
    assert outcome.opportunity.new_score is None


def test_room_everywhere_and_nothing_below_the_bar_is_still_silence():
    """The precondition still governs the RANKED-MARGIN tier, which sells a
    still-eligible name purely to fund a replacement."""
    from src.rotation import evaluate_rotation

    outcome = evaluate_rotation(
        ranked=[_rc("NEW", 1.8), _rc("OLD", 1.0)],
        blocked={},
        held_symbols={"OLD"},
        headroom_pct=TODAY_RISK_HEADROOM_PCT,
        floor_pct=FLOOR_PCT,
        entry_budget_usd=9_000.0,
        min_risk_floor=_floor(),
    )
    assert outcome.opportunity is None
    assert outcome.refusal is not None
    assert outcome.refusal.point == "book_not_constrained"
    assert outcome.refusal.binding == ()


def test_an_unresolvable_funding_view_does_not_read_as_room_or_as_full():
    """`None` is "not measured". It must not switch the funding test ON
    (which would rotate on an unread number) nor silently assert room."""
    from src.rotation import rotation_binding_constraints

    assert (
        rotation_binding_constraints(
            headroom_pct=14.5,
            floor_pct=FLOOR_PCT,
            funding=_funding(None),
        )
        == ()
    )
    assert rotation_binding_constraints(
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
        funding=_funding(None),
    ) == ("risk_budget",)
    assert rotation_binding_constraints(
        headroom_pct=14.5,
        floor_pct=FLOOR_PCT,
        funding=_funding(TODAY_DEPLOYABLE_USD),
    ) == ("funding",)
    assert rotation_binding_constraints(
        headroom_pct=0.1,
        floor_pct=FLOOR_PCT,
        funding=_funding(TODAY_DEPLOYABLE_USD),
    ) == ("risk_budget", "funding")


def test_exactly_the_minimum_order_is_not_constrained():
    """The boundary. `min_entry_usd` dollars deployable funds the minimum
    order, so the book is not full — strictly less than, not at-or-below."""
    from src.rotation import rotation_binding_constraints

    assert (
        rotation_binding_constraints(
            headroom_pct=14.5,
            floor_pct=FLOOR_PCT,
            funding=_funding(MIN_ENTRY_USD),
        )
        == ()
    )
    assert rotation_binding_constraints(
        headroom_pct=14.5,
        floor_pct=FLOOR_PCT,
        funding=_funding(MIN_ENTRY_USD - 0.01),
    ) == ("funding",)


# ---------------------------------------------------------------------------
# 2026-09-23 — every refusal point writes its durable row.
# ---------------------------------------------------------------------------


def _refusal(**over):
    from src.rotation import evaluate_rotation

    outcome = evaluate_rotation(**{"noise_brake": TEST_NOISE_BRAKE, **_today_ladder_full_kwargs(**over)})
    assert outcome.opportunity is None
    assert outcome.refusal is not None, "a refusal must never be silent"
    return outcome.refusal


def test_no_refusal_point_is_silent_and_every_one_is_reachable():
    """The list and the code must agree. A refusal point that exists in the
    function but not in `ROTATION_REFUSAL_POINTS` is exactly the silent
    drop this change exists to remove, and one in the tuple that nothing
    can reach is a check nobody has seen fire."""
    from src.rotation import ROTATION_REFUSAL_POINTS

    # `_rcv` scores: technical weight 1.2, earnings weight 1.2 here.
    weak = _rcv("OLD", {"technical": (0.75, "low")})  # 0.9
    strong = _rcv("NEW", {"technical": (1.0, "medium")})  # 1.8
    seen = {
        # (a) the precondition
        _refusal(
            ranked=[strong],
            blocked={},
            held_symbols={"OLD"},
            entry_budget_usd=9_000.0,
        ).point,
        # (b) nothing to rotate INTO. With no candidate there is no stop to
        # judge the funding half at (owner rule 2026-08-27 is per position,
        # at that position's stop), so the risk budget is what binds here.
        _refusal(ranked=[weak], blocked={}, held_symbols={"OLD"}, headroom_pct=0.1).point,
        # (c) nothing held is ranked, and nothing held is blocked either
        _refusal(ranked=[strong], blocked={}, held_symbols={"GONE"}).point,
        # (d) the weakest holding scores zero or below
        _refusal(
            ranked=[strong, _rc("OLD", 0.0)],
            blocked={},
            held_symbols={"OLD"},
        ).point,
        # (e) the full composite margin is not cleared (1.0 < 0.9 * 1.25)
        _refusal(
            ranked=[_rcv("NEW", {"technical": (0.83, "low")}), weak],
            blocked={},
            held_symbols={"OLD"},
        ).point,
        # (f) no seat scored both names
        _refusal(
            ranked=[_rcv("NEW", {"earnings": (1.0, "high")}), weak],
            blocked={},
            held_symbols={"OLD"},
        ).point,
        # (g) the shared sub-score's denominator is non-positive: OLD's
        # whole score comes from `news`, which does not cover NEW, and the
        # one seat they share rates it zero (magnitude 0, conviction low).
        _refusal(
            ranked=[
                _rcv("NEW", {"technical": (1.0, "high"), "earnings": (1.0, "high")}),  # 4.8
                _rcv(
                    "OLD",
                    {
                        "technical": (None, "low"),  # 0.0
                        "news": (0.5, "low"),
                    },
                ),  # 0.5
            ],
            blocked={},
            held_symbols={"OLD"},
        ).point,
        # (h) cleared on the full composite, not on the shared seats
        _refusal(
            ranked=[
                _rcv("NEW", {"technical": (0.8, "low"), "earnings": (1.0, "high")}),
                _rcv("OLD", {"technical": (0.75, "low")}),
            ],
            blocked={},
            held_symbols={"OLD"},
        ).point,
        # (i) owner decision 2026-10-15: no measured score noise, no swap
        _refusal(
            ranked=[strong, weak],
            blocked={},
            held_symbols={"OLD"},
            noise_brake=None,
        ).point,
    }
    assert seen == set(ROTATION_REFUSAL_POINTS), (
        f"unreached: {set(ROTATION_REFUSAL_POINTS) - seen}; unlisted: {seen - set(ROTATION_REFUSAL_POINTS)}"
    )


def test_the_refusal_row_carries_the_named_fields():
    """The dataset board item 39(a) needs: which holding, which candidate,
    the shared seats, both shared scores, the ratio, and which point."""
    refusal = _refusal(
        ranked=[
            _rcv("NEW", {"technical": (0.8, "low"), "earnings": (1.0, "high")}),
            _rcv("OLD", {"technical": (0.75, "low")}),
        ],
        blocked={},
        held_symbols={"OLD"},
    )
    assert refusal.point == "shared_composite_margin_not_cleared"
    assert refusal.held_symbol == "OLD"
    assert refusal.new_symbol == "NEW"
    assert refusal.shared_seats == ("technical",)
    assert refusal.held_shared_score == 0.75
    assert refusal.new_shared_score == 0.8
    # The ratio is taken on the LIKE-FOR-LIKE sub-score, which is the
    # comparison the margin is actually judged on, not on the
    # coverage-sensitive totals.
    assert refusal.ratio == round(0.96 / 0.9, 4)
    assert refusal.noise_brake == TEST_NOISE_BRAKE
    assert refusal.binding == ("funding",)
    assert refusal.detail


def test_the_row_describes_the_comparison_even_when_the_book_had_room():
    """The sessions with room are 51 of the last 51 and are most of the
    population 39(a) must be answered from. A row saying only "there was
    room" is not a dataset."""
    refusal = _refusal(
        ranked=[
            _rcv("NEW", {"technical": (1.0, "medium")}),
            _rcv("OLD", {"technical": (0.75, "low")}),
        ],
        blocked={},
        held_symbols={"OLD"},
        entry_budget_usd=9_000.0,
    )
    assert refusal.point == "book_not_constrained"
    assert refusal.held_symbol == "OLD" and refusal.new_symbol == "NEW"
    assert refusal.shared_seats == ("technical",)
    assert refusal.ratio == 2.0


def test_the_refusal_payload_is_flat_json_safe_scalars():
    """The row is stored as JSON and read by the evening review. A nested
    object here would be one more thing to unpack before it is usable."""
    import json

    payload = _refusal(
        ranked=[_rc("NEW", 1.8)],
        blocked={},
        held_symbols={"GONE"},
    ).event_kwargs()
    assert payload["stage"] == "rotation"
    assert payload["outcome"] == "not_surfaced"
    assert payload["reason"] == "no_ranked_holdings"
    for key, value in payload.items():
        assert isinstance(value, (str, int, float, type(None))), (key, value)
    assert json.loads(json.dumps(payload))["shared_seats"] == ""


def test_the_legacy_entry_point_still_returns_the_opportunity_alone():
    """`evaluate_rotation_opportunity` is this module's long-standing name
    and keeps its shape; the pipeline uses `evaluate_rotation` so it can
    record the refusal."""
    opp = evaluate_rotation_opportunity(
        ranked=[_rc("NEW", 1.8)],
        blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
        held_symbols={"OLD"},
        **_today_ladder_full_kwargs(),
    )
    assert opp is not None and opp.held_symbol == "OLD"


def test_the_prompt_names_the_constraint_that_is_actually_binding():
    """2026-09-17 CRM: a prompt that quotes a limit which is NOT the one
    stopping the desk makes the model plan around the wrong number. With
    only the funding constraint binding, the section must say so in
    dollars, not quote 14.50% of risk headroom as though it were tight."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    result = PortfolioManagerAgent._render_rotation_section(
        ranked=[_rc("NEW", 1.8)],
        blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
        held_symbols={"OLD"},
        existing_risk_pct={"OLD": 10.5},
        ceiling_pct=25.0,
        precheck=PortfolioManagerAgent.rotation_precheck(
            ranked=[_rc("NEW", 1.8)],
            blocked={"OLD": ["R4 R/R 0.80 under the 1.50 floor"]},
            held_symbols={"OLD"},
            existing_risk_pct={"OLD": 10.5},
            ceiling_pct=25.0,
            entry_budget_usd=TODAY_DEPLOYABLE_USD,
            min_risk_floor=_floor(),
        ),
    )
    assert "Capital is constrained" in result
    assert "$92.20 still deployable for new entries" in result
    assert (
        "below the $5,000.00 position that, at the best candidate's own stop, carries the owner's minimum risk"
        in result
    )
    assert "real room exists" not in result


# --- owner "natural selection" mandate 2026-09-23 ---------------------------
# The set of holdings that would NOT be bought today. A categorical
# membership (are you in `blocked`?), never a score — so it introduces no
# arbitrary number and cannot re-open board item 39(a). Recorded every
# session so the desk can see how many of its own names have stopped earning
# their place; see `holdings_below_entry_bar` and `precheck_record`.


def test_holdings_below_entry_bar_is_held_names_that_fail_the_entry_bar():
    from src.rotation import holdings_below_entry_bar

    blocked = {
        "OLD": ["R5 net evidence -1 if long — no rung"],
        "STALE": ["R2 neutral rating", "R5 net evidence 0 if long — no rung"],
        "NEWIDEA": ["R3 not BUY-eligible"],  # a candidate, not held
    }
    held = {"OLD", "STALE", "STRONG"}  # STRONG is held and eligible (not blocked)
    # Only the HELD names that are also blocked, upper-cased and sorted.
    assert holdings_below_entry_bar(blocked, held) == ("OLD", "STALE")


def test_holdings_below_entry_bar_ignores_empty_reason_rows_and_is_case_insensitive():
    from src.rotation import holdings_below_entry_bar

    # An empty reason list is not a blocking row (matches
    # `test_empty_blocked_reasons_are_not_treated_as_a_blocking_row`), and a
    # held-symbol set in another case still matches.
    blocked = {"old": ["R5 net evidence -1 if long — no rung"], "FINE": []}
    assert holdings_below_entry_bar(blocked, {"OLD", "fine"}) == ("OLD",)


def test_precheck_records_the_holdings_below_the_entry_bar_every_session():
    """The telemetry rides in the SAME row `precheck_record` already writes,
    whether or not the book is constrained — a name that decayed below the
    bar while the book still had room is exactly the case natural selection
    is not yet acting on, so gating the record on 'constrained' would hide
    it."""
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.rotation import precheck_record

    # Real room on every constraint (not constrained), yet a held name is
    # already below the entry bar.
    precheck = PortfolioManagerAgent.rotation_precheck(
        ranked=[_rc("NEW", 1.8), _rc("KEEP", 1.2)],
        blocked={"OLD": ["R5 net evidence -1 if long — no rung"]},
        held_symbols={"OLD", "KEEP"},
        existing_risk_pct={"OLD": 2.0, "KEEP": 2.0},
        ceiling_pct=25.0,
    )
    # Owner ruling 2026-10-01: room on the book no longer silences the cull.
    assert precheck.opportunity is not None
    assert precheck.opportunity.tier == "ineligible_hold"
    assert precheck.held_below_entry_bar == ("OLD",)
    record = precheck_record(
        precheck,
        execute_enabled=True,
        ranked_margin_enabled=False,
    )
    assert record["held_below_entry_bar"] == "OLD"
    assert record["held_below_entry_bar_count"] == 1


def test_the_natural_selection_telemetry_introduces_no_margin_change():
    """Guard: this change is measurement only. The provisional margin and
    the two switches it gates are untouched — the categorical tier is what
    delivers the mandate, and the ranked-margin number stays blocked by
    39(a)."""
    import src.rotation as rotation

    # Owner decision 2026-10-15 (q07): the 25% constant is gone; the brake
    # is the measured score noise, passed in by the caller.
    assert not hasattr(rotation, "ROTATION_MARGIN_PCT")
