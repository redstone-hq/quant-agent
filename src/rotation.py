"""Opportunity-cost rotation surfacing — Phase 14.

**The gap this closes.** `src/risk/budget.py::allocate_risk_budget` and the
gross-exposure ladder both correctly BLOCK a new candidate when the book's
risk ceiling is full — but neither one, nor anything upstream of them, ever
asks whether the new candidate is actually a BETTER opportunity than
something the book is already holding. A desk that is fully committed to
weak, stale or barely-justified positions can refuse a genuinely stronger
new idea for no reason other than "no room", with nothing in the system
that ever compares the two. This module is the missing comparison.

**Grounded in established practice, not invented.** Ranking current
holdings and candidates on one shared scale and replacing the weakest
holding with a stronger candidate — subject to a real margin so the system
does not churn on noise-level rank differences — is standard in
cross-sectional/systematic portfolio construction:

  * Grinold & Kahn, *Active Portfolio Management* (2000), formalise the
    "no-trade region": under transaction costs, a rebalance is only worth
    making once the expected improvement clears a real breakeven, not at
    every marginal rank change. The margin below is this desk's own
    breakeven proxy — expressed on the verdict score rather than in
    dollars, since that is the unit this desk already ranks on
    (`src/verdicts.py`).
  * FTSE Russell's own published index-reconstitution methodology applies
    exactly this shape in production: a "banding"/buffer rule requires a
    candidate to clear a MATERIALLY different bar than an incumbent before
    a membership swap happens, specifically to damp turnover from
    marginal, noise-level rank changes at the boundary (see FTSE Russell,
    "Russell US Indexes Construction and Methodology", banding sections;
    summarised at https://www.lseg.com/en/insights/ftse-russell —
    "percentile banding... allows previous membership to be considered in
    order to limit unnecessary index turnover").

Neither source hands over one universal number — practitioner tolerance-
band discussions for rebalancing cluster loosely in the 5%-25% relative-
band range depending on the asset class and cost profile (see e.g. Alpha
Architect's writing on rebalancing tolerance bands). This module takes the
CONSERVATIVE (hardest-to-trigger) end of that range, 25% relative on the
composite verdict score, and marks it PROVISIONAL — the same posture
`src/verdicts.py::SEAT_WEIGHT` already uses for its own literature-derived
but unmeasured numbers: a considered starting point, not a measured fact,
revisable the moment this desk has its own rotation-outcome data to spend.

**Two tiers, not one.** A held position that no longer clears this desk's
OWN entry gates (`PortfolioManagerAgent.candidate_eligibility`) needs no
margin at all to flag: it would not be bought today, by the identical rule
a brand-new buy must clear, so there is nothing "noise-level" about the
comparison — it is categorical, not a ranking judgement. Only among held
positions that ARE still eligible does the ranking-margin rule apply, to
protect against churning on a real but marginal rank difference.

**This module never places an order.** It returns one comparison — the
weakest thing to consider giving up, and the strongest thing there is no
room for — as data for the Portfolio Manager's OWN prompt
(`PortfolioManagerAgent._render_rotation_section`). It never sizes a
trade, and is silent unless capital is genuinely constrained (real headroom
under the desk's own existing risk-budget floor, `STARTER_POSITION_RISK_PCT`
/ `RiskConfig.min_position_risk_pct` — the same floor `allocate_risk_budget`
already uses to decide "not worth trading").

**Phase 14b — the comparison can now be ACTED ON, behind a flag.** Owner
request: positions are reassessed several times a day, and when a genuinely
better-ranked opportunity exists while the book is full of something weaker
and stagnant, the weak one should be pruned and the better trade taken —
rather than the desk merely showing the model a comparison and hoping it
acts. With `execution.rotation_enabled` ON (default OFF — see
`config/settings.yaml`), `DecisionStage` (`src/pipeline_stages.py::
_apply_rotation_execution`) turns the CATEGORICAL tier only into an
ordinary zero-size PM target for the held symbol, which then travels the
identical path every PM-decided exit travels: `PortfolioConstructor`
(`_build_sell`), the hard risk rules, the AI Risk Manager's review and
per-symbol refusal, the holding-discipline claim check, and
`_submit_protected_sell`'s cancel-write-ahead → submit → restore-on-failure
discipline. No gate is bypassed, and no new exempt reason category exists.

Why only the categorical tier is automated: an ineligible holding fails the
same rule a brand-new buy must clear today, so the case is a rule outcome,
not a ranking judgement. The ranked-margin tier stays information-only
because a 25% score gap is a PROVISIONAL, unmeasured band (see above), and
selling an eligible, thesis-intact position on an unmeasured margin would be
exactly the "arbitrary number decides a trade" pattern this desk refuses
elsewhere.

**Board item 39, 2026-09-20 — what changed and what did NOT.** The
ranked-margin tier now HAS an execution path, behind a second switch
(`execution.rotation_ranked_margin_enabled`, default False), and that path
is sequenced correctly: the replacement BUY is run through the downstream
refusal gates against the book as it will be AFTER the sale, and both legs
are withdrawn together if it would be refused, so the desk cannot end up
sold out of a position with nothing bought
(`src/pipeline_stages.py::_drop_rotation_legs_if_buy_would_be_refused`).
The sale additionally cannot be BUILT without a `RotationClearance` below —
an object carrying the projected numbers the buy was cleared on, which no
configuration can produce.

The paragraph above is still the reason the switch is OFF, and it is not a
formality. `ROTATION_MARGIN_PCT` is recorded in
`config/number_ledger.yaml` as `arbitrary` — "a round quarter percent with
no source". Closing the sequencing defect removed a reason this tier could
not be turned on; it did not answer the one stated here. `docs/WORK.md`
item 39(a) is the remaining blocker: it lists what has already been ruled
out (the citations above among them — the SHAPE is sourced, the NUMBER is
not) and what would settle it. Note what 39(a) itself says: "do not
promote it to an execution gate until answered."

Why the desk's holding discipline still binds: `docs/WORK.md` item 25 says a
position stays protected from a plain, no-real-trigger sale unless the level
backing its thesis has broken (confirmed on two consecutive closes, or the
noise-band fallback). Opportunity cost is not one of the three real triggers
that doctrine names, so a rotation may only close a holding whose
`check_structural_protection` verdict is ALREADY "not protected" — a stale
position whose thesis is still structurally intact is surfaced, never sold.
Whether opportunity cost should become a fourth trigger is an owner decision
this module does not take.

`rotation_sell_reason` writes the sale's reason from measured facts only —
the failed entry rules, the broken-protection basis, the candidate's rank and
score, the headroom — so the audit trail, the Risk Manager and the owner
alert all read the same checkable statement.

**Tier 2 is coverage-neutral as of 2026-09-14** (`docs/INCIDENT_HISTORY.md`,
that date, retired board item 66). On
2026-09-13 `src/verdicts.py::rank_verdicts` changed from a weighted AVERAGE
across seats to a weighted SUM — correctly: the average made a second,
fully AGREEING seat LOWER a candidate's rank. But a sum is not a rescaling
of an average; the divisor it deletes is the name's OWN seat count, which
differs per name. So the composite score of two different names stopped
being comparable term-for-term the moment their coverage differed, and
Tier 2's relative margin compares exactly two such names. It is a ratio, so
it survives a change of UNITS — it does not survive a per-name change of
term count, and the item's claim that "the margin is a ratio, so the scale
change does not affect it" was checked here and does not hold.

Concretely: a held name covered today by Technical alone (weighted score
2.4 at full strength) is displaced by a new name that Technical rates
identically but that also carries a live earnings filing and a confirmed
flow (2.4 + 1.2 + 0.4 = 4.0 >= 2.4 x 1.25). Nothing about the held name
changed; the earnings calendar moved. Measured against the desk's own
archive (`specialist_evidence`, 2026-08-17..2026-09-02), the set of seats
carrying a non-neutral read on a HELD name changes day to day, not over
weeks: DIS, MSFT and V each fell to zero scoring seats on individual
sessions and recovered, and RSG went from zero to one to two scoring seats
inside five sessions. Coverage churn is a daily fact of this book, so this
is reachable now, not eventually.

The fix introduces no number and does not touch the sum. Tier 2 now
computes the SAME weighted arithmetic over only the seats that scored BOTH
names — the intersection of their coverage — and requires the margin to
clear on that like-for-like sub-score as well as on the full composite. A
term present on one side and absent on the other cannot enter a comparison
between the two. When the two names share no scoring seat at all there is
no like-for-like comparison to make and Tier 2 declines outright, because
rotation refusing a sale it should have made costs an opportunity while
rotation making one it should not have costs a real position.

This is strictly a conjunction with the previous test: every rotation that
fires now would have fired before, and some that would have fired no longer
do. Tier 2 is therefore LESS likely to fire, never more — that is a
property of the arithmetic, not an estimate. Breadth is undiminished
everywhere else: it still orders `ranked`, still picks `best_new`, still
picks the weakest held name, and still drives Tier 1 in full.

**2026-09-23 — the precondition was pointed at a constraint this book has
never hit, so the comparison has never once been reached.** Across every
retained log rotation (2026-08-21 .. 2026-09-23) the rendered Opportunity
Rotation section took the "real room exists, so there is nothing to rotate
for" branch 51 times out of 51; "Capital is constrained" has occurred zero
times. Rendered RISK-budget headroom ranged 12.93%-24.53% against a 25%
ceiling and never came within twelve percentage points of the 0.50%
`STARTER_POSITION_RISK_PCT` floor the precondition tested it against.

The constraint that actually binds this desk is the §11.2 gross-exposure
ladder and settled cash. On 2026-09-23 the book was at 1.9908x gross
against a 2.00x ceiling with about $92 of ladder headroom on roughly
$10,000 of equity, and the PM's own prompt that run said "the book is
already invested at 199.1% of equity with only $92.20 of ladder headroom" —
while this module, looking at the same moment, saw 14.50% risk headroom and
stayed silent. The desk told its decision-maker there was real room at the
exact moment it could not fund anything.

So the precondition is now **constrained on ANY binding constraint**, not
re-pointed at the gross ladder alone. Two reasons, and the second is the
one that decides it:

  * What rotation is FOR is the question "we cannot take both — is the best
    idea better than the worst holding?" That question is live whenever the
    desk cannot take a new position, and it does not become a different
    question according to which limit stopped it.
  * Re-pointing at the ladder alone would reproduce this very defect
    mirrored. A book at 1.2x gross with plenty of cash but with its risk
    budget exhausted cannot fund a starter position either, and rotation
    would go silent again for exactly the reason it has been silent for a
    month. Replacing one single-constraint blind spot with another is not a
    fix, it is a rotation of the blind spot.

The funding half reuses the desk's own computation and does not write a
second one: `src/pipeline_stages.py::_entry_deployment_budget` is the
number EXECUTION sizes entries against, and it already resolves the §11.2
ladder, mins it against settled cash when margin is disabled, and falls
back to raw cash when the ladder is unreadable. The PM prompt is already
handed that exact figure (`margin_headroom_usd` / `margin_ladder_backed`,
threaded so the Margin Capacity section "never derives its own number"),
so the rotation pre-check is handed the same object rather than a second
opinion about it. "Cannot fund a starter position" was, until 2026-09-24,
the §10.3 notional floor `cash_sweep.min_order_usd` (deleted) — an arbitrary $500 with
no broker minimum behind it, and Alpaca charges no stock commission, so a
real rotation was being refused on a false "too small to matter" basis. The
`below_min_notional` gate this used to name is retired outright (see
`REQUIRED_BUY_LEG_GATES`) — a rotation whose replacement buy re-sizes to a
genuine ZERO is still refused, via `insufficient_cash`, but a small, nonzero
re-sized buy is no longer rejected for being under that flat floor. No
number is introduced here.

**And every refusal now writes a durable row.** This module used to return
`None` at seven distinct points and record nothing at any of them, while
`rotation_precheck` wrapped the result as a bare `opportunity=None`. The
prompt named no symbol, no score and no ratio, and nothing was persisted —
which is the desk's own standing rule against dropping a candidate without
a durable, per-symbol, machine-readable reason, breached in the one place
that most needs the dataset. `evaluate_rotation` now returns a
`RotationOutcome` carrying a `RotationRefusal` for whichever point was hit,
with the holding and candidate compared, the shared seats, both shared
scores and the ratio; `src/pipeline_stages.py::_record_rotation_refusal`
lands it as a `rotation`/`not_surfaced` pipeline_event through the same
`_record_pipeline_event` path `_rotation_skip` already uses for the
acted-on case. The comparison facts are computed even at the points that
refuse before the comparison matters, because a row saying only "the book
had room" is not a dataset and board item 39(a) — the open research
question behind the unmeasured 25% margin — cannot progress without one.

Neither change touches `ROTATION_MARGIN_PCT` or
`rotation_ranked_margin_enabled`. Both remain blocked by 39(a). What
changed is whether the comparison is ever REACHED and whether its outcome
is RECORDED.

**2026-09-23 — "natural selection" mandate: what it is, and the route that
was designed and REJECTED for it.** The owner's framing: the book runs full
up to the 2.00x gross ceiling (kept at 2.00x — the broker permits ~2x
overnight under Reg-T and the owner does not want forced daily trims), and
"every stock must keep earning its right to be in the portfolio"; when a
better candidate exists and the book is full, the weakest holding should be
displaced for it — a SWAP that never raises gross past the ceiling (the sale
frees the room the buy then takes, sequenced buy-behind-sell as board item
39 already builds). The owner defined "weakest" as lowest CONVICTION, not
worst P&L.

Read literally — rank the still-eligible holdings by conviction and prune
the lowest when a better candidate qualifies — that mandate IS the
ranked-margin tier, and it stays blocked, for two independent reasons, not
one:

  * board item 39(a): "how much better" is `ROTATION_MARGIN_PCT`, an
    arbitrary, UNIDENTIFIABLE constant on the ordinal score (only four
    distinct score ratios occur across the whole band; the fraction of pairs
    clearing any threshold from 0.05 to 0.25 is identical), and 39(a) says
    do not promote it to an execution gate until answered; and
  * holding discipline (retired item 25, `docs/INCIDENT_HISTORY.md`): a
    still-eligible, thesis-intact position is protected from a plain sale
    with no real trigger, and opportunity cost is not one of the triggers —
    so even a ZERO margin (pure "strictly better" ordering) would not make
    an eligible, protected holding sellable. The margin is not the only
    thing in the way.

What the desk therefore delivers, non-arbitrarily and today, is the
CATEGORICAL tier: a holding that has fallen below the desk's own fresh-entry
bar has, by the identical rule a new buy must clear, "stopped earning its
place," and a candidate that clears that same bar displaces it — pass/fail,
no score margin, no invented number. It is live and, since PR #604 pointed
the precondition at the union of every binding limit, actually reachable on
a full book. CORRECTED 2026-10-01 (this file previously said the tier "had fired zero
times in the retained logs" — that was FALSE and is a defect in the desk's
own record). MEASURED against the production `specialist_evidence` rows: it
fired 8 times on 24-25 Sep and died 8 of 8 at the buy-leg precondition,
recorded as `pm_did_not_target_new_candidate`. The trap was circular — the
tier only ran when the book was full, the prompt then told the model there
was no room to buy, the model never wrote the buy, so the sell was never
proposed. In 80 closed trades the desk had never once sold a holding for
ceasing to earn its place. That is why the owner removed the capital and
replacement preconditions from this tier on 2026-10-01.

REJECTED, do not re-propose (adversary review, 2026-09-23): choosing WHICH
below-the-bar holding to prune by seat-weighted CONVICTION score. The
categorical set (`blocked`) is not in `ranked` and carries no conviction
rank, so it would have to be scored on the full composite — the exact
coverage-sensitive weighted SUM the 2026-09-14 fix above proved is not
comparable term-for-term across names, and the pairwise shared-seat escape
cannot order N of them. It also inverts intent: an R3 "not BUY-eligible"
name keeps its high seat score and would sort SAFEST, while an R2-neutral
name (no read, score ~0) would always sort weakest. An ordering escapes the
LETTER of the no-arbitrary-numbers rule but not its spirit when the scale is
one the desk's own research condemns. The incumbent tie-break (most blocking
entry-rules failed, then alphabetical) is the better "most clearly stale"
proxy and is kept.

What this change adds is measurement, not a mechanism: `precheck_record`
now carries `held_below_entry_bar` / `_count` (`holdings_below_entry_bar`)
every session — the holdings that would not be bought today — so the desk
can finally see how many of its own names have stopped earning their place,
which is the dataset "natural selection" and 39(a) both lack and which no
new number can substitute for.
"""

from __future__ import annotations

from src.verdicts import RankedCandidate, score_verdict, seat_weight
from src.rotation_unrecorded import empty_pass_lines
from dataclasses import replace

from src.risk.min_risk import MinRiskFloor
from src.rotation_parts.constraints import (
    funding_view_measured,
    holdings_below_entry_bar,
    rotation_binding_constraints,
)
from src.rotation_parts.types import (
    CONVICTION_BAR_REASON_PREFIX,
    REQUIRED_BUY_LEG_GATES,
    ROTATION_MARGIN_PCT,
    ROTATION_REASON_MAX_CHARS,
    ROTATION_REFUSAL_POINTS,
    RotationClearance,
    RotationOpportunity,
    RotationOutcome,
    RotationPrecheck,
    RotationRefusal,
)
from src.rotation_parts.wording import (
    _ranked_margin_sell_reason,
    rotation_constraint_clause,
    rotation_proposal_reason,
    rotation_sell_reason,
)
from src.rotation_parts.reporting import (
    ROTATION_FULL_NOTHING_BETTER,
    ROTATION_FULL_OPPORTUNITY,
    ROTATION_HOLDING_BELOW_BAR,
    ROTATION_ROOM_AVAILABLE,
    ROTATION_TELEMETRY_UNAVAILABLE,
    _opt_float,
    _pct,
    _precheck_binding,
    precheck_outcome,
    precheck_record,
)
from src.rotation_parts.reporting_lines import (
    _full_book_cause,
    _tier_two_line,
    owner_precheck_lines,
    pruning_pass_lines,
)

__all__ = [
    "REQUIRED_BUY_LEG_GATES",
    "ROTATION_MARGIN_PCT",
    "ROTATION_REFUSAL_POINTS",
    "RotationClearance",
    "RotationOpportunity",
    "RotationOutcome",
    "RotationPrecheck",
    "RotationRefusal",
    "CONVICTION_BAR_REASON_PREFIX",
    "evaluate_rotation",
    "evaluate_rotation_opportunity",
    "holdings_below_entry_bar",
    "rotation_binding_constraints",
    "funding_view_measured",
    "rotation_constraint_clause",
    "rotation_proposal_reason",
    "rotation_sell_reason",
]


def evaluate_rotation_opportunity(
    *,
    ranked: list[RankedCandidate],
    blocked: dict[str, list[str]],
    held_symbols: set[str],
    headroom_pct: float,
    floor_pct: float,
    margin_pct: float = ROTATION_MARGIN_PCT,
    entry_budget_usd: float | None = None,
    min_risk_floor: MinRiskFloor | None = None,
) -> RotationOpportunity | None:
    """`evaluate_rotation`'s opportunity, for callers that want only that.

    Kept because it is this module's long-standing entry point and because
    a caller that genuinely has nowhere to persist a refusal should not be
    forced to pretend otherwise. Every caller inside the pipeline uses
    `evaluate_rotation` and records the refusal.
    """
    return evaluate_rotation(
        ranked=ranked,
        blocked=blocked,
        held_symbols=held_symbols,
        headroom_pct=headroom_pct,
        floor_pct=floor_pct,
        margin_pct=margin_pct,
        entry_budget_usd=entry_budget_usd,
        min_risk_floor=min_risk_floor,
    ).opportunity


def evaluate_rotation(
    *,
    ranked: list[RankedCandidate],
    blocked: dict[str, list[str]],
    held_symbols: set[str],
    headroom_pct: float,
    floor_pct: float,
    margin_pct: float = ROTATION_MARGIN_PCT,
    entry_budget_usd: float | None = None,
    min_risk_floor: MinRiskFloor | None = None,
) -> RotationOutcome:
    """The one rotation comparison worth surfacing (`_evaluate_rotation_core`).

    The funding half is judged at the BEST new candidate's own stop: the
    deployable dollars, as a position at that stop, must clear the owner's
    minimum risk per position (owner rule 2026-08-27) through the one shared
    `src.risk.min_risk.min_risk_shortfall`. No candidate, or no floor
    threaded, means the funding view is not measured.
    """
    held = {s.upper() for s in held_symbols}
    best = next((c for c in ranked if c.symbol not in held), None)
    funding = None if min_risk_floor is None or best is None else min_risk_floor.check(best.symbol, entry_budget_usd)
    outcome = _evaluate_rotation_core(
        ranked=ranked,
        blocked=blocked,
        held_symbols=held_symbols,
        headroom_pct=headroom_pct,
        floor_pct=floor_pct,
        margin_pct=margin_pct,
        entry_budget_usd=entry_budget_usd,
        funding=funding,
    )
    return replace(outcome, funding=funding)


def _evaluate_rotation_core(
    *,
    ranked: list[RankedCandidate],
    blocked: dict[str, list[str]],
    held_symbols: set[str],
    headroom_pct: float,
    floor_pct: float,
    margin_pct: float,
    entry_budget_usd: float | None = None,
    funding=None,
) -> RotationOutcome:
    """The one rotation comparison worth surfacing this session, or `None`.

    `ranked` / `blocked` are `PortfolioManagerAgent.rank_candidates`'s own
    output — both current holdings and new candidates already share one
    scale, since eligibility and ranking are computed over every analysed
    symbol without regard to whether it is currently held (Technical reads
    the whole configured universe every session, held names included).

    Silent unless capital is genuinely constrained — on ANY of the limits
    the desk enforces, not just the risk budget. `rotation_binding_
    constraints` is the test: the risk-budget headroom against the EXISTING
    book is under `floor_pct`, OR the dollars `_entry_deployment_budget`
    says are still deployable will not fund the smallest position that can
    carry the owner's minimum risk at the best new candidate's own stop. A book
    with real room on every constraint has nothing to rotate for; refusing
    on "we might want the room later" is not this desk's rule anywhere else
    and is not invented here. See the module docstring for why this is the
    union and for the measurement that forced the change.

    Returns a `RotationOutcome`. When no opportunity is surfaced it carries
    a `RotationRefusal` naming which of `ROTATION_REFUSAL_POINTS` was hit
    and every comparison fact that was knowable there — never a bare
    `None`.
    """
    held = {s.upper() for s in held_symbols}
    min_entry_usd = None if funding is None else funding.min_notional
    binding = rotation_binding_constraints(
        headroom_pct=headroom_pct,
        floor_pct=floor_pct,
        funding=funding,
    )

    new_candidates = [c for c in ranked if c.symbol not in held]
    best_new = new_candidates[0] if new_candidates else None
    held_ranked = [c for c in ranked if c.symbol in held]
    weakest_held = held_ranked[-1] if held_ranked else None

    def _refuse(point: str, detail: str) -> RotationOutcome:
        """One refusal row, with the comparison described as far as it
        exists.

        The description is computed for EVERY point, including the ones
        that decline before the comparison could matter. That is the whole
        value of the row: 39(a) needs the distribution of ratios this book
        actually throws off, and the sessions where the book had room are
        not a different population — they are most of the population.
        """
        return RotationOutcome(
            refusal=_describe_refusal(
                point=point,
                detail=detail,
                best_new=best_new,
                weakest_held=weakest_held,
                margin_pct=margin_pct,
                binding=binding,
            )
        )

    # Tier 1 — categorical. OWNER RULING 2026-10-01 ("every position needs
    # to justify its reason to be there"): this tier is evaluated BEFORE the
    # capital precondition and BEFORE any replacement candidate is required.
    # A holding that would not be bought today is sold on its own merits —
    # not because the book is full, and not because something is queued to
    # take its place. The replacement BUY, where one exists, stays an
    # ordinary separate decision. Both preconditions below still gate the
    # RANKED-MARGIN tier, which sells a still-eligible name purely to fund a
    # replacement and therefore genuinely needs both.
    # Tier 1 — categorical. A held name failing the desk's own entry gates
    # needs no ranking margin: it would not be bought today. Ties broken by
    # the MOST blocking reasons first (worse, more clearly stale), then
    # alphabetically, so the choice is reproducible when more than one held
    # name is ineligible.
    ineligible_held = {sym: tuple(reasons) for sym, reasons in blocked.items() if sym in held and reasons}
    if ineligible_held:
        # Worst-first: MOST blocking reasons (more clearly stale), then
        # alphabetically, so the order is reproducible. The whole set is
        # carried on the opportunity (board item 39) — `_apply_rotation_
        # execution` walks it and closes the first name that can actually be
        # sold this run, rather than abandoning the rotation when only the
        # single worst name is structurally protected. `held_symbol`/`reasons`
        # stay the worst name so every existing reader and the audit row are
        # byte-for-byte unchanged when nothing displaces it.
        ordered = sorted(
            ineligible_held,
            key=lambda s: (-len(ineligible_held[s]), s),
        )
        held_symbol = ordered[0]
        return RotationOutcome(
            opportunity=RotationOpportunity(
                new_symbol=best_new.symbol if best_new else None,
                new_score=best_new.score if best_new else None,
                held_symbol=held_symbol,
                held_score=None,
                tier="ineligible_hold",
                reasons=ineligible_held[held_symbol],
                margin_pct=margin_pct,
                ineligible_candidates=tuple((sym, ineligible_held[sym]) for sym in ordered),
            )
        )

    if not binding:
        measured = funding_view_measured(entry_budget_usd, min_entry_usd)
        return _refuse(
            "book_not_constrained",
            f"risk headroom {headroom_pct:.2f}% is at or above the "
            f"{floor_pct:.2f}% floor, and "
            + (
                f"${entry_budget_usd:,.2f} deployable is at or above "
                "the smallest position that can carry the owner's minimum "
                "risk at the best candidate's stop — real room on every "
                "constraint"
                if measured
                else "the funding view was NOT MEASURED this session, so no funding constraint could be tested"
            ),
        )

    if best_new is None:
        return _refuse(
            "no_new_candidates",
            f"every one of the {len(ranked)} ranked names is already held, so there is no candidate to rotate INTO",
        )

    # Tier 2 — ranked margin. Both sides eligible; the weakest held name is
    # simply the last held entry in `ranked`'s own (already-sorted) order.
    if weakest_held is None:
        # nothing held is even ranked — no comparison to make
        return _refuse(
            "no_ranked_holdings",
            f"{len(held)} symbol(s) held but none of them appears in the "
            f"ranking, so there is no holding to compare against",
        )
    if weakest_held.score <= 0:
        # A relative margin against a non-positive score is not a
        # meaningful comparison; a score this weak with no eligibility
        # failure recorded is a state this module does not attempt to
        # interpret further, rather than divide by (near) zero and guess.
        return _refuse(
            "held_score_non_positive",
            f"{weakest_held.symbol} scores {weakest_held.score:.4f}; a "
            f"relative margin against a non-positive score is not a "
            f"meaningful comparison",
        )
    if best_new.score < weakest_held.score * (1.0 + margin_pct):
        return _refuse(
            "full_composite_margin_not_cleared",
            f"{best_new.symbol} ({best_new.score:.4f}) does not clear "
            f"{weakest_held.symbol} ({weakest_held.score:.4f}) by "
            f"{margin_pct:.0%} on the full composite",
        )

    # `docs/INCIDENT_HISTORY.md` 2026-09-14. The full composite is a
    # weighted SUM over
    # whichever seats happen to cover each name today, so the two totals
    # above are not comparable term-for-term once the two names' coverage
    # differs — and it does, daily (see the module docstring for the
    # archive measurement). Re-run the identical arithmetic over the seats
    # that scored BOTH names and require the same margin there too. No
    # constant is introduced: `seat_weight` and `score_verdict` are
    # `src/verdicts.py`'s own, unchanged, and the margin is the same one.
    shared = _shared_seat_comparison(held=weakest_held, new=best_new)
    if shared is None:
        # No seat scored both names. There is no like-for-like comparison
        # to make, so this module declines rather than compare two
        # differently-composed sums. Failing toward NOT selling is the
        # asymmetry the desk chose: a missed rotation costs an
        # opportunity, a wrong one costs a real position.
        return _refuse(
            "no_shared_scoring_seat",
            f"no seat scored both {weakest_held.symbol} and "
            f"{best_new.symbol}, so the two weighted sums are not "
            f"comparable term-for-term",
        )
    shared_seats, held_shared, new_shared = shared
    if held_shared <= 0:
        # same non-positive-denominator refusal as above
        return _refuse(
            "shared_held_score_non_positive",
            f"{weakest_held.symbol} scores {held_shared:.4f} over the "
            f"shared seats ({', '.join(shared_seats)}); no relative margin "
            f"is meaningful against that",
        )
    if new_shared < held_shared * (1.0 + margin_pct):
        return _refuse(
            "shared_composite_margin_not_cleared",
            f"{best_new.symbol} ({new_shared:.4f}) does not clear "
            f"{weakest_held.symbol} ({held_shared:.4f}) by {margin_pct:.0%} "
            f"on the seats scoring both ({', '.join(shared_seats)}), though "
            f"it cleared on the full composite",
        )

    return RotationOutcome(
        opportunity=RotationOpportunity(
            new_symbol=best_new.symbol,
            new_score=best_new.score,
            held_symbol=weakest_held.symbol,
            held_score=weakest_held.score,
            tier="ranked_margin",
            margin_pct=margin_pct,
            shared_seats=shared_seats,
            held_shared_score=round(held_shared, 4),
            new_shared_score=round(new_shared, 4),
        )
    )


def _describe_refusal(
    *,
    point: str,
    detail: str,
    best_new: RankedCandidate | None,
    weakest_held: RankedCandidate | None,
    margin_pct: float,
    binding: tuple[str, ...],
) -> RotationRefusal:
    """One refusal row with the comparison filled in as far as it exists.

    The like-for-like sub-score is computed here even at the refusal points
    that never needed it, and the ratio is reported on that sub-score when
    there is one and on the full composite otherwise. The sub-score is the
    comparison the margin is actually judged on
    (`docs/INCIDENT_HISTORY.md` 2026-09-14), so a dataset that recorded
    only the full-composite ratio would be a dataset about the wrong
    quantity.

    Nothing here can raise: this runs on the refusal path, and a row that
    fails to be written is the defect being fixed, not a smaller version of
    it.
    """
    held_symbol = weakest_held.symbol if weakest_held is not None else None
    new_symbol = best_new.symbol if best_new is not None else None
    held_score = weakest_held.score if weakest_held is not None else None
    new_score = best_new.score if best_new is not None else None
    shared_seats: tuple[str, ...] = ()
    held_shared: float | None = None
    new_shared: float | None = None
    if best_new is not None and weakest_held is not None:
        shared = _shared_seat_comparison(held=weakest_held, new=best_new)
        if shared is not None:
            shared_seats, held_raw, new_raw = shared
            held_shared = round(held_raw, 4)
            new_shared = round(new_raw, 4)
    # The ratio 39(a) has to be answered from: like-for-like where one
    # exists, full composite where it does not, and `None` rather than an
    # infinity or a divide-by-zero where the denominator is non-positive.
    ratio: float | None = None
    if held_shared is not None and new_shared is not None and held_shared > 0:
        ratio = round(new_shared / held_shared, 4)
    elif held_score is not None and new_score is not None and held_score > 0:
        ratio = round(new_score / held_score, 4)
    return RotationRefusal(
        point=point,
        detail=detail,
        held_symbol=held_symbol,
        new_symbol=new_symbol,
        held_score=None if held_score is None else round(held_score, 4),
        new_score=None if new_score is None else round(new_score, 4),
        shared_seats=shared_seats,
        held_shared_score=held_shared,
        new_shared_score=new_shared,
        ratio=ratio,
        margin_pct=margin_pct,
        binding=binding,
    )


def _shared_seat_comparison(
    *,
    held: RankedCandidate,
    new: RankedCandidate,
) -> tuple[tuple[str, ...], float, float] | None:
    """Each name's weighted score over ONLY the seats that scored both.

    Returns `(shared_seats, held_score, new_score)`, or `None` when the two
    names share no scoring seat.

    The arithmetic is `src/verdicts.py`'s own and is reproduced, not
    re-derived: a seat's contribution to `RankedCandidate.score` is
    `seat_weight(seat) * (magnitude + conviction_score)`, which is exactly
    `seat_weight(seat) * score_verdict(verdict)`. Summing that over a
    subset of seats therefore yields the same composite restricted to that
    subset — a partial sum of the real score, not a second scoring scheme.

    Neutral seats are deliberately NOT shared coverage. A seat that looked
    and came back with no lean contributes nothing to either name's score
    (`RankedCandidate.neutral_seats`, never `.verdicts`), so including it
    would add two zero terms and change nothing except to make an
    incomparable pair look comparable.
    """
    held_by_seat = {v.seat: v for v in held.verdicts}
    new_by_seat = {v.seat: v for v in new.verdicts}
    shared = sorted(set(held_by_seat) & set(new_by_seat))
    if not shared:
        return None
    held_total = sum(seat_weight(seat) * score_verdict(held_by_seat[seat]) for seat in shared)
    new_total = sum(seat_weight(seat) * score_verdict(new_by_seat[seat]) for seat in shared)
    return tuple(shared), held_total, new_total
