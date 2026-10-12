"""Rotation value types and the constants they carry (lifted verbatim from src/rotation.py)."""

from __future__ import annotations

from dataclasses import dataclass, field


#: Board item 39. Every downstream refusal path that can drop the rotation's
#: REPLACEMENT BUY *after* the SELL has already been submitted, and that is
#: knowable before the sale. A `ranked_margin` sale may not be built unless a
#: `RotationClearance` proves EVERY name here was evaluated this run against
#: PROJECTED POST-SALE state.
#:
#: The names are the `_record_execution_skip` reason codes the execution
#: stage itself uses, so this list and the code it mirrors can be compared
#: mechanically (`tests/test_rotation_sequencing.py` pins that).
#:
#: This list is deliberately NOT "every way a BUY can fail" — see
#: `rotation_sell_reason` for the paths that remain open by construction and
#: why no pre-check can close them.
REQUIRED_BUY_LEG_GATES = (
    # The list was SIX long until 2026-09-23. `daily_loss_recheck` (retired-ok) led it
    # (retired-ok), and it is gone because the refusal it named is gone:
    # the owner's ruling on board item 32 removed the account-level loss
    # halt outright (PR #584), so the execution stage no longer records a
    # skip under that name for a projection to anticipate. Nothing replaces
    # it and nothing
    # absorbs it — a gate for a refusal that cannot fire is a check that
    # always passes, which is worse than no check because it reads like
    # one. The gross-exposure half of what that halt used to police
    # survives untouched in the §11.2 ladder, gated through
    # `insufficient_cash`, measured by `_entry_deployment_budget` on its
    # ladder-backed branch.
    #
    # The list was FIVE long until 2026-09-24. `below_min_notional`
    # (retired-ok) is gone the same way: the flat $500 `min_order_usd` (deleted)
    # notional floor it named was an arbitrary round number
    # (config/number_ledger.yaml), not a broker minimum, and Alpaca charges
    # no stock commission — a genuine ~$295 / 2.95%-of-equity trade was
    # refused on it as "pays full commission". `_prevent_rotation_naked_sale`
    # no longer refuses a rotation's replacement buy for re-sizing small but
    # nonzero; only a genuine zero still refuses, via `insufficient_cash`.
    "no_price",
    "stale_entry",
    "qty_zero",
    # Added after adversary review of attempt 3. Deterministic function of
    # the POST-SALE book, knowable before the sale — and the refusal a
    # rotation is most likely to hit, because a rotation only surfaces when
    # the risk headroom is already under the floor. Leaving it out was the
    # same class of omission as attempt 1's, one layer further down.
    "insufficient_cash",
    # Owner rule 2026-08-27: below the minimum risk per position the desk
    # does not trade. Execution refuses a buy under it, so a rotation whose
    # replacement would land under it must be refused BEFORE the sale.
    "below_owner_min_risk",
)

#: Relative margin the best-ranked new candidate must clear over the
#: weakest still-eligible held position's score before a rotation is
#: surfaced. PROVISIONAL — see module docstring for the citations and why
#: this is the conservative end of a real but unpinned range, not a
#: measured fact.
ROTATION_MARGIN_PCT = 0.25

#: Prefix that tags a `candidate_eligibility` blocking reason as the 2026-09-25
#: conviction bar (R7). The STAY cull (rotation's `ineligible_hold` tier)
#: recognises ONLY these reasons; a held name failing an OLDER gate
#: (R2/R3/R5/R6) is culled through its own reason, exactly as before this change.
CONVICTION_BAR_REASON_PREFIX = "R7 conviction bar"

#: `PortfolioConstructor._build_sell` truncates an order's reasoning at 500
#: characters after appending the thesis condition. A rotation reason that
#: overruns it loses its LAST clause first — which is the one naming what
#: the sale was cleared on, or that it is contingent. Not a threshold on
#: anything traded: it is the length of a field this text has to fit in.
ROTATION_REASON_MAX_CHARS = 500


@dataclass(frozen=True)
class RotationOpportunity:
    """One surfaced comparison: the strongest idea there is no room for,
    against the weakest thing currently holding that room.

    `held_score` is `None` for the categorical tier — the held symbol has
    no rank at all because it failed the desk's own entry gates outright,
    not because it ranked low among names that passed them.
    """

    #: `None` on the categorical tier when NO new candidate exists. Owner
    #: ruling 2026-10-01: a holding that no longer clears the fresh-entry
    #: bar is sold on its own merits, so the replacement is no longer a
    #: precondition and must not be faked with a placeholder score.
    new_symbol: str | None
    new_score: float | None
    held_symbol: str
    held_score: float | None
    #: "ineligible_hold" (categorical — no margin needed) or "ranked_margin"
    #: (both sides eligible; the margin below was cleared).
    tier: str
    #: The held symbol's own blocking reasons, "ineligible_hold" tier only.
    reasons: tuple[str, ...] = field(default_factory=tuple)
    margin_pct: float = ROTATION_MARGIN_PCT
    #: Board item 39, "ineligible_hold" tier only. The WHOLE below-bar cull
    #: set this session, ordered worst-first (most blocking reasons first,
    #: then alphabetical) as `(symbol, reasons)` pairs — every held name that
    #: fails the desk's own entry bar today, not just the one surfaced in
    #: `held_symbol`/`reasons` (which are this tuple's first entry). The
    #: execution stage walks it so that when the worst name cannot be sold
    #: this run (structurally protected, in flight, already being closed by
    #: the PM) it advances to the next-worst rather than abandoning the whole
    #: rotation. Empty for the ranked-margin tier, which compares one weakest
    #: holding, and empty on a directly-constructed opportunity, where the
    #: execution stage falls back to the single `held_symbol`.
    ineligible_candidates: tuple[tuple[str, tuple[str, ...]], ...] = field(
        default_factory=tuple,
    )
    #: "ranked_margin" tier only — the seats that scored BOTH names, and
    #: each side's weighted sum over exactly those seats. This is the
    #: like-for-like comparison the margin was actually cleared on
    #: (`docs/INCIDENT_HISTORY.md` 2026-09-14); the full
    #: `new_score`/`held_score` above are
    #: coverage-sensitive and are not comparable term-for-term between two
    #: names whose seat sets differ. Empty for the categorical tier, which
    #: makes no ranking comparison at all.
    shared_seats: tuple[str, ...] = field(default_factory=tuple)
    held_shared_score: float | None = None
    new_shared_score: float | None = None


#: Every point `evaluate_rotation` can decline at, in the order it reaches
#: them. Each one writes a `RotationRefusal` carrying whatever of the
#: comparison was knowable, so the evening review and board item 39(a) read
#: a named ground rather than an absence.
#:
#: `tests/test_rotation.py` pins that every member is reachable and that no
#: return path leaves the refusal unset — a refusal point added to the
#: function and not to this tuple is the failure mode this list exists to
#: stop, because a silent path is exactly what was being fixed.
ROTATION_REFUSAL_POINTS: tuple[str, ...] = (
    # The precondition. Renamed from the old headroom-only test: the book
    # has room on EVERY constraint the desk enforces, so there is genuinely
    # nothing to rotate for.
    "book_not_constrained",
    "no_new_candidates",
    "no_ranked_holdings",
    "held_score_non_positive",
    "full_composite_margin_not_cleared",
    "no_shared_scoring_seat",
    "shared_held_score_non_positive",
    "shared_composite_margin_not_cleared",
)


@dataclass(frozen=True)
class RotationRefusal:
    """Why no rotation was surfaced this session, in a shape a machine can
    group on and a person can read.

    Nothing here is prose-only. `point` is the comparable CODE (one of
    `ROTATION_REFUSAL_POINTS`) and `detail` is the reader-facing sentence —
    the same split `_record_accounted_candidate` uses for exactly the same
    reason: two sessions are compared on the named ground, not on the
    module's choice of words.

    Every field the comparison produced is carried even when the refusal
    happened before that field mattered. A row saying only "the book had
    room" cannot answer board item 39(a); a row saying "the book had room,
    and had it not, NVDA at 4.80 would have been compared against RSG at
    4.20 on Technical+Earnings for a ratio of 1.14" can.
    """

    point: str
    detail: str
    #: Which holding was compared, and which candidate against it. `None`
    #: when this session had no such name at all (an empty book, or no new
    #: candidate ranked) — which is itself the finding.
    held_symbol: str | None = None
    new_symbol: str | None = None
    #: The full composite scores. Coverage-sensitive; see the module
    #: docstring. Kept because the margin's FIRST test is run on them.
    held_score: float | None = None
    new_score: float | None = None
    #: The seats that scored BOTH names, and each side's weighted sum over
    #: exactly those seats — the like-for-like comparison.
    shared_seats: tuple[str, ...] = field(default_factory=tuple)
    held_shared_score: float | None = None
    new_shared_score: float | None = None
    #: `new / held` on the like-for-like sub-score when there is one, else
    #: on the full composite. This is the quantity 39(a) has to be answered
    #: from: the distribution of ratios the desk actually sees. `None` when
    #: no comparable pair existed or the denominator was non-positive.
    ratio: float | None = None
    #: The margin the ratio was (or would have been) judged against, so a
    #: row read months later is not silently re-interpreted under a
    #: different one.
    margin_pct: float = ROTATION_MARGIN_PCT
    #: Which limits were binding when this refusal was taken, in
    #: `rotation_binding_constraints` order. Empty means none were, which
    #: is the `book_not_constrained` case.
    binding: tuple[str, ...] = field(default_factory=tuple)

    def event_kwargs(self) -> dict:
        """The `_record_pipeline_event` payload for this refusal.

        Flat, JSON-safe scalars only: the evidence row is stored as JSON and
        is queried by the evening review, so a nested object here would be
        one more thing to unpack before the dataset is usable.
        """
        return {
            "stage": "rotation",
            "outcome": "not_surfaced",
            "reason": self.point,
            "detail": self.detail[:400],
            "held_symbol": self.held_symbol,
            "new_symbol": self.new_symbol,
            "held_score": self.held_score,
            "new_score": self.new_score,
            "shared_seats": ",".join(self.shared_seats),
            "held_shared_score": self.held_shared_score,
            "new_shared_score": self.new_shared_score,
            "ratio": self.ratio,
            "margin_pct": self.margin_pct,
            "binding": ",".join(self.binding),
        }


@dataclass(frozen=True)
class RotationOutcome:
    """What one rotation evaluation produced: at most one of these is set.

    Exactly one is always set. A `None` opportunity with a `None` refusal
    would be the silent drop this shape exists to make impossible.
    """

    opportunity: RotationOpportunity | None = None
    refusal: RotationRefusal | None = None
    #: The funding half of the precondition, judged at the best candidate's
    #: own stop by `src.risk.min_risk.min_risk_shortfall`; `None` when no
    #: funding view or no candidate existed.
    funding: object | None = None

    @property
    def min_entry_usd(self) -> float | None:
        """Smallest position at the best candidate's stop that clears the
        owner's minimum risk; `None` when not measured."""
        return None if self.funding is None else self.funding.min_notional


@dataclass(frozen=True)
class RotationPrecheck:
    """Everything the PM's rotation section was rendered from, kept so the
    pipeline can act on the SAME numbers the model was shown — never a
    second evaluation a moment later against possibly different inputs.

    `opportunity` is `None` when nothing qualified; `telemetry_available`
    is False when the book's risk was not visible this session (the
    fail-open skip), in which case `headroom_pct` is meaningless.

    `refusal` is set whenever `opportunity` is `None` and the evaluation
    actually ran, and is what `_record_rotation_refusal` persists. It is
    `None` only in the telemetry-unavailable case, where there was no
    evaluation to refuse — that case has always had its own prompt line and
    is not a silent drop.

    `entry_budget_usd` / `min_entry_usd` / `binding` are the funding view the
    precondition was decided on, kept for the same reason `headroom_pct` is:
    the prompt and the execution stage must read the SAME numbers the
    comparison was made against, never a second measurement taken a moment
    later.
    """

    opportunity: RotationOpportunity | None
    headroom_pct: float
    ceiling_pct: float
    floor_pct: float
    telemetry_available: bool = True
    refusal: RotationRefusal | None = None
    entry_budget_usd: float | None = None
    min_entry_usd: float | None = None
    binding: tuple[str, ...] = field(default_factory=tuple)
    #: Owner mandate 2026-09-23 ("every stock must keep earning its place").
    #: Every held name that would NOT be bought today because it now fails
    #: the desk's own entry bar (`holdings_below_entry_bar`). Session-level
    #: telemetry, recorded whether or not the book is constrained: it is the
    #: set the categorical tier may prune from, and its size says whether
    #: natural selection has anything to act on. A categorical membership,
    #: never a score — no arbitrary number.
    held_below_entry_bar: tuple[str, ...] = field(default_factory=tuple)
    #: Board item 219. Every holding the pass actually looked at this
    #: session, upper-cased and sorted. A raw membership, not a score: it
    #: is simply `held_symbols` as the pre-check received it, kept so the
    #: owner's report can say the pass RAN and over what, instead of
    #: leaving a session that examined the whole book indistinguishable
    #: from one that never ran.
    held_examined: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RotationClearance:
    """Proof that a rotation's replacement BUY was run through every gate in
    `REQUIRED_BUY_LEG_GATES` against PROJECTED POST-SALE state, and survived.

    **This object is the safety net, and it is not a flag.** Board item 39's
    first fix attempt replaced the structural barrier that made a
    `ranked_margin` sale impossible to build with a plain config boolean,
    which meant one truthy value anywhere in the settings chain was enough
    to put a real sale on the wire. A boolean cannot carry evidence. This
    can, and `rotation_sell_reason` refuses — unconditionally, with no
    config read anywhere in the refusal — to build the sale's reason
    without one whose contents match the opportunity in hand.

    `projected_positions` / `projected_equity` / `projected_entry_budget` /
    `projected_budget_basis` are recorded so the audit row states the
    numbers the sale was actually cleared on, rather than asserting that a
    check happened.
    """

    held_symbol: str
    new_symbol: str
    #: Every gate evaluated. Must cover `REQUIRED_BUY_LEG_GATES`.
    gates_checked: tuple[str, ...]
    #: What `_entry_deployment_budget` said was deployable on the projected
    #: post-sale book, and the note it gave for WHICH pool that was — the
    #: §11.2 gross-ladder headroom or settled cash. This pair replaced the
    #: projected day-change and its limit rung on 2026-09-23, when the
    #: account-level loss halt those described was removed (PR #584). It is
    #: the quantity the funding gates below actually clear the sale on, so
    #: it is the one the audit row should carry.
    projected_entry_budget: float
    projected_budget_basis: str
    #: Held names remaining after every SELL/COVER this session, the
    #: rotation's own included, and the equity those were measured against.
    projected_positions: tuple[str, ...]
    projected_equity: float

    def covers(self, *, held_symbol: str, new_symbol: str) -> bool:
        """Is this clearance about THIS rotation, and did it check every
        gate? A clearance minted for a different pair, or one missing a
        gate, is not a clearance for this sale."""
        if self.held_symbol.strip().upper() != held_symbol.strip().upper():
            return False
        if self.new_symbol.strip().upper() != new_symbol.strip().upper():
            return False
        checked = {str(g).strip() for g in self.gates_checked}
        return all(gate in checked for gate in REQUIRED_BUY_LEG_GATES)
