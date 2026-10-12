"""src.agents.portfolio_manager.rotation_rendering -- the standalone rotation-section piece.

Bodies moved verbatim from src/agents/portfolio_manager/rotation_section.py (RotationSectionMixin);
the mixin keeps same-named thin shims. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no agent behind it.
"""

from src.risk.budget import allocate_risk_budget
from src.risk.min_risk import MinRiskFloor
from src.risk.constants import STARTER_POSITION_RISK_PCT
from src.rotation import (
    RotationOpportunity,
    RotationPrecheck,
    evaluate_rotation,
    funding_view_measured,
    holdings_below_entry_bar,
    rotation_binding_constraints,
)
from src.verdicts import RankedCandidate


class RotationSection:
    """Rotation precheck and the rotation prompt section.

    Standalone: every collaborator is an explicit keyword-only constructor argument.
    Bodies moved verbatim from RotationSectionMixin (src/agents/portfolio_manager/rotation_section.py);
    the only mechanical changes are `cls` -> `self` (decorators dropped) and `self` inserted as the first
    parameter of each former staticmethod.
    """

    def __init__(
        self,
        *,
        rotation_constraint_line=None,
        rotation_precheck=None,
    ) -> None:
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if rotation_constraint_line is not None:
            self._rotation_constraint_line = rotation_constraint_line
        if rotation_precheck is not None:
            self.rotation_precheck = rotation_precheck

    def rotation_precheck(
        self,
        *,
        ranked: list[RankedCandidate],
        blocked: dict[str, list[str]],
        held_symbols: set[str],
        existing_risk_pct: dict[str, float] | None,
        ceiling_pct: float,
        entry_budget_usd: float | None = None,
        min_risk_floor: MinRiskFloor | None = None,
    ) -> RotationPrecheck:
        """Phase 14 — run the opportunity-cost comparison once and keep its
        inputs. `_render_rotation_section` renders this for the prompt;
        `DecisionStage` reads the same object to decide whether to act
        (Phase 14b). See `src/rotation.py`.

        `existing_risk_pct` is the book's risk BEFORE anything this session
        proposes — the same map `PortfolioConstructor` rations against
        (`src/pipeline_stages.py::_book_risk_inputs`). `None` means the
        book's risk is not visible this session (facts unavailable); the
        check is skipped rather than run against a fabricated "book is
        empty" view, the same fail-open posture `allocate_risk_budget`
        itself already requires of every caller.

        `entry_budget_usd` is `_entry_deployment_budget`'s own figure — the
        dollars EXECUTION will size this session's entries against, already
        carrying the §11.2 gross ladder, settled cash and the min of the two
        — and `min_risk_floor` the owner's minimum risk per position (owner
        rule 2026-08-27, the `min_position_risk_pct` setting) with each
        candidate's own entry and stop, so the funding test asks whether
        those dollars, as a position at the BEST candidate's stop, clear the
        floor (`src.risk.min_risk.min_risk_shortfall`). Together they are the
        funding half of the precondition (2026-09-23; see
        `src/rotation.py`'s docstring for the measurement that added it).
        Both `None` means the funding view was not resolvable this session,
        and the funding test is then simply absent — this agent never
        derives either number itself, for the same reason the Margin
        Capacity section does not.
        """
        held_below = holdings_below_entry_bar(blocked, held_symbols)
        # Board item 219. What the pass looked at, kept verbatim so the
        # owner's report states a measured count rather than an inference.
        held_examined = tuple(sorted(str(s).strip().upper() for s in held_symbols if str(s).strip()))
        if existing_risk_pct is None:
            return RotationPrecheck(
                opportunity=None,
                headroom_pct=0.0,
                ceiling_pct=ceiling_pct,
                floor_pct=STARTER_POSITION_RISK_PCT,
                telemetry_available=False,
                entry_budget_usd=entry_budget_usd,
                min_entry_usd=None,
                held_below_entry_bar=held_below,
                held_examined=held_examined,
            )
        headroom_pct = allocate_risk_budget(
            [],
            existing_pct=existing_risk_pct,
            clusters=None,
            ceiling_pct=ceiling_pct,
            floor_pct=STARTER_POSITION_RISK_PCT,
        ).headroom_pct
        outcome = evaluate_rotation(
            ranked=ranked,
            blocked=blocked,
            held_symbols=held_symbols,
            headroom_pct=headroom_pct,
            floor_pct=STARTER_POSITION_RISK_PCT,
            entry_budget_usd=entry_budget_usd,
            min_risk_floor=min_risk_floor,
        )
        opportunity: RotationOpportunity | None = outcome.opportunity
        return RotationPrecheck(
            opportunity=opportunity,
            headroom_pct=headroom_pct,
            ceiling_pct=ceiling_pct,
            floor_pct=STARTER_POSITION_RISK_PCT,
            refusal=outcome.refusal,
            entry_budget_usd=entry_budget_usd,
            min_entry_usd=outcome.min_entry_usd,
            binding=(() if outcome.refusal is None else outcome.refusal.binding)
            or rotation_binding_constraints(
                headroom_pct=headroom_pct,
                floor_pct=STARTER_POSITION_RISK_PCT,
                funding=outcome.funding,
            ),
            held_below_entry_bar=held_below,
            held_examined=held_examined,
        )

    def _rotation_constraint_line(
        self,
        precheck: RotationPrecheck,
        *,
        ceiling_pct: float,
    ) -> str:
        """The one sentence naming WHICH limit has the book pinned.

        2026-09-23. The old wording said "capital is constrained" and then
        quoted only the risk budget, because the risk budget was the only
        thing the pre-check looked at. Now that the funding view can be the
        binding one — and on this book it usually is, while the risk budget
        almost never is — the sentence has to say which, or the model reads
        a number that is not the one stopping it and plans around the wrong
        limit. That mis-statement is the same class of defect as the
        2026-09-17 CRM incident, where the prompt said no margin was
        deployable while $11.4k of ladder headroom existed.
        """
        parts: list[str] = []
        if "risk_budget" in precheck.binding:
            parts.append(
                f"only {precheck.headroom_pct:.2f}% risk headroom against "
                f"the {ceiling_pct:.2f}% ceiling (existing book only, "
                "before anything you propose today), under the "
                f"{precheck.floor_pct:.2f}% minimum tradeable size"
            )
        if "funding" in precheck.binding:
            budget = precheck.entry_budget_usd
            floor = precheck.min_entry_usd
            parts.append(
                f"only ${budget:,.2f} still deployable for new entries (the §11.2 "
                "ladder-and-cash budget execution sizes entries against), "
                f"below the ${floor:,.2f} position that, at the best "
                "candidate's own stop, carries the owner's minimum risk "
                "per position — so "
                "no new position can be funded at all without freeing "
                "capital first"
                if isinstance(budget, (int, float)) and isinstance(floor, (int, float))
                else "the deployable budget will not fund a new order"
            )
        if not parts:
            # Unreachable while callers check `precheck.binding` first; here
            # so a future caller that does not gets a true sentence rather
            # than an assertion of constraint that was never established.
            return (
                f"{precheck.headroom_pct:.2f}% risk headroom against the "
                f"{ceiling_pct:.2f}% ceiling; no constraint is binding."
            )
        return "Capital is constrained — " + " and ".join(parts) + "."

    def _render_rotation_section(
        self,
        *,
        ranked: list[RankedCandidate],
        blocked: dict[str, list[str]],
        held_symbols: set[str],
        existing_risk_pct: dict[str, float] | None,
        ceiling_pct: float,
        precheck: RotationPrecheck | None = None,
        execute_enabled: bool = False,
        #: Board item 39 — `execution.rotation_ranked_margin_enabled`, the
        #: SECOND switch. `execute_enabled` alone still means the
        #: categorical tier only, exactly as before.
        ranked_margin_enabled: bool = False,
    ) -> str:
        """Phase 14 — the opportunity-cost comparison, surfaced as
        information. See `src/rotation.py` for the rule, the margin and the
        citations behind it.

        `precheck` is the already-computed comparison (`rotation_precheck`);
        omitted, it is computed here from the same inputs. `execute_enabled`
        (Phase 14b, `execution.rotation_enabled`) only changes the WORDING:
        when the desk itself may act on the categorical tier, the model is
        told so, so its own plan can account for it — the decision to act is
        made in `DecisionStage`, never here.
        """
        header = "## Opportunity Rotation (deterministic pre-check, Phase 14)"
        if precheck is None:
            precheck = self.rotation_precheck(
                ranked=ranked,
                blocked=blocked,
                held_symbols=held_symbols,
                existing_risk_pct=existing_risk_pct,
                ceiling_pct=ceiling_pct,
            )
        if not precheck.telemetry_available:
            return (
                f"{header}\n"
                "(book risk telemetry unavailable this session — rotation "
                "check skipped, same as every other consumer of this data)"
            )
        headroom_pct = precheck.headroom_pct
        opportunity = precheck.opportunity
        constraint_line = self._rotation_constraint_line(
            precheck,
            ceiling_pct=ceiling_pct,
        )
        if opportunity is None:
            if precheck.binding:
                return (
                    f"{header}\n"
                    f"{constraint_line} But no eligible new candidate "
                    "outranks a held position by enough to recommend "
                    "trimming one for the other. Nothing to surface."
                )
            if funding_view_measured(
                precheck.entry_budget_usd,
                precheck.min_entry_usd,
            ):
                return (
                    f"{header}\n"
                    f"{headroom_pct:.2f}% risk headroom left against the "
                    f"{ceiling_pct:.2f}% ceiling, and "
                    f"${precheck.entry_budget_usd:,.2f} is still deployable "
                    "for new entries, at or above the smallest position that can "
                    "carry the owner's minimum risk per position at the best "
                    "candidate's own stop "
                    "— real room exists on every constraint, "
                    "so there is nothing to rotate for."
                )
            # Adversary review 2026-09-23: do NOT tell a seat that can sell
            # that room exists on a constraint that was never measured.
            return (
                f"{header}\n"
                f"{headroom_pct:.2f}% risk headroom left against the "
                f"{ceiling_pct:.2f}% ceiling. The session's deployable-entry "
                "budget could NOT be read, so the funding constraint was not "
                "tested and this check covers the risk budget only."
            )
        lines = [header, constraint_line]
        if opportunity.tier == "ineligible_hold":
            reasons = "; ".join(opportunity.reasons)
            lines.append(
                f"{opportunity.held_symbol} is currently held but would "
                f"NOT be bought today — it fails this desk's own entry "
                f"rules ({reasons})."
            )
            # OWNER RULING 2026-10-01: this tier is now reached with NO
            # replacement candidate, so `new_symbol`/`new_score` are None on
            # exactly the case this feature exists to create. Never format
            # them unguarded — that crashed `build_user_message` before the
            # decision stage was reached.
            if opportunity.new_symbol and opportunity.new_score is not None:
                lines.append(
                    f"{opportunity.new_symbol} ranks "
                    f"{opportunity.new_score:.2f} and clears every rule, but "
                    "there is no room to buy it without freeing capital "
                    "first."
                )
            else:
                lines.append(
                    "Nothing un-held ranked well enough to buy this session, "
                    "so there is no replacement candidate. This holding is "
                    "weighed on its own merits, not against anything else."
                )
        else:
            # `docs/INCIDENT_HISTORY.md` 2026-09-14: the composite score
            # is a weighted sum
            # over whichever seats cover each name today, so the two
            # totals are NOT comparable term-for-term. The margin was
            # cleared on both the totals and on the seats that scored both
            # names; state the second one, because it is the comparison
            # that is actually like-for-like.
            lines.append(
                f"{opportunity.held_symbol} is the weakest still-eligible "
                f"held position (score {opportunity.held_score:.2f}). "
                f"{opportunity.new_symbol} ranks {opportunity.new_score:.2f}, "
                f"at least {opportunity.margin_pct:.0%} higher — a real "
                "margin, not a noise-level difference (cross-sectional "
                "replacement-rule convention; see src/rotation.py) — but "
                "there is no room to buy it without freeing capital first."
            )
        if (
            opportunity.tier == "ranked_margin"
            and opportunity.held_shared_score is not None
            and opportunity.new_shared_score is not None
        ):
            shared = ", ".join(opportunity.shared_seats)
            lines.append(
                "Like-for-like check (this is the load-bearing one): those "
                "totals sum whichever seats cover each name TODAY, so a "
                "held name whose earnings or flow coverage has lapsed "
                "scores lower for that reason alone. On the seats that "
                f"scored BOTH names ({shared}), "
                f"{opportunity.held_symbol} is "
                f"{opportunity.held_shared_score:.2f} and "
                f"{opportunity.new_symbol} is "
                f"{opportunity.new_shared_score:.2f} — the same "
                f"{opportunity.margin_pct:.0%} margin clears there too, so "
                "this gap is not an artefact of coverage. Had it not, "
                "nothing would have been surfaced."
            )
        # Board item 39: the RANKED-MARGIN tier only, deliberately.
        #
        # "Doing nothing is another reasonable call" is not true once the
        # desk can close the name itself, and two adjacent paragraphs
        # claiming opposite things is a prompt that has rotted. That is
        # equally true of the CATEGORICAL tier, which is live today — but
        # rewording a live seat's prompt is a behaviour change on a path
        # that is trading, it is not required by this item, and it belongs
        # in a change a reviewer can judge on its own merits rather than
        # as a footnote in a sequencing fix. The categorical text below is
        # therefore byte-for-byte unchanged.
        desk_may_act = ranked_margin_enabled and opportunity.tier == "ranked_margin"
        if desk_may_act:
            lines.append(
                "This names the weakest thing currently using the room and "
                "the strongest thing there is no room for. Read the "
                "paragraph below before you plan: on this comparison the "
                "desk may close the held name ITSELF, so leaving it alone "
                "is not one of the outcomes. An edit to a held position "
                "still needs the same substantive justification any other "
                "exit does — this note is not one."
            )
        elif opportunity.new_symbol:
            lines.append(
                "This is a comparison, not an instruction: it names the "
                "weakest thing currently using the room and the strongest "
                "thing there is no room for. Trimming or exiting "
                f"{opportunity.held_symbol} to fund "
                f"{opportunity.new_symbol} is one reasonable call; doing "
                "nothing is another. Either way, an edit to a held "
                "position needs the same substantive justification any "
                "other exit does — this note is not one."
            )
        else:
            # OWNER RULING 2026-10-01: the categorical tier is reached with
            # no replacement, so there is nothing to "fund" and no second
            # name to compare against.
            lines.append(
                "This is an observation, not an instruction: it names a "
                "holding the desk would not buy today, with no replacement "
                "to fund. Exiting "
                f"{opportunity.held_symbol} is one reasonable call; doing "
                "nothing is another. Either way, an edit to a held "
                "position needs the same substantive justification any "
                "other exit does — this note is not one."
            )
        if ranked_margin_enabled and opportunity.tier == "ranked_margin":
            # Board item 39. The desk can now act on THIS tier too, behind
            # its own second switch. The model must be told, or it sizes a
            # plan as though no room is being freed — and the rotation's
            # own arithmetic then depends on that plan. A prompt that is
            # silent about what the desk will do on its own is wrong in the
            # same way stale code is.
            lines.append(
                "AUTOMATIC ROTATION IS ENABLED for this ranked-margin case: "
                f"if you include a BUY target for {opportunity.new_symbol} "
                f"and do not yourself close {opportunity.held_symbol}, the "
                f"desk will propose a full close of {opportunity.held_symbol}"
                " on its own — but ONLY if its structural protection has "
                "already broken under the holding-discipline check, it was "
                "not bought today, nothing is in flight on it, AND the "
                f"replacement buy of {opportunity.new_symbol} still clears "
                "every gate that can be KNOWN before the sale (the "
                "daily-loss limit, a usable price, a fresh entry, a "
                "tradeable size, and the funding), measured against the "
                "book as it would be AFTER the sale. Some refusals are not "
                "knowable in advance — a stale quote at the moment of "
                "submission, a broker rejection — and those are not "
                "covered. If the buy would be refused on anything that IS "
                "knowable, NEITHER leg happens and the holding stays. That "
                "proposal goes through the Risk Manager like any other "
                "exit.\n"
                "Do NOT size your other BUYs against the room this would "
                f"free. Size {opportunity.new_symbol} for the position you "
                "want and size everything else against the book as it "
                "stands. If the session's entries together ask for more "
                "than the freed room can fund, the funding gate refuses "
                "this replacement and BOTH legs are withdrawn — so "
                "spending the same room twice does not get you a bigger "
                "trade, it gets you no rotation."
            )
        elif execute_enabled and opportunity.tier == "ineligible_hold":
            # Phase 14b. Wording only — the act itself is decided in
            # `DecisionStage._apply_rotation_execution` from the desk's own
            # data, after this prompt returns.
            # OWNER RULING 2026-10-01. This paragraph describes the code in
            # `DecisionStage._apply_rotation_execution` and nothing beyond
            # it. Two conditions this text used to state were REMOVED by
            # that ruling: the close no longer requires that the holding's
            # structural protection has broken, and it no longer requires a
            # replacement BUY. Do not reinstate either in prose.
            replacement_clause = (
                f"If you include a BUY target for {opportunity.new_symbol}, size it for the room this close would free"
                if opportunity.new_symbol
                else "Nothing un-held ranked well enough to buy this session, so "
                "there is no replacement to size for and the freed room "
                "simply stays in the book"
            )
            lines.append(
                "AUTOMATIC ROTATION IS ENABLED for this categorical case: "
                f"if you do not yourself close {opportunity.held_symbol}, "
                "the desk will propose a full close of it on its own, "
                "because a holding that would not be bought today has "
                "stopped earning its place. That is so whether or not its "
                "structural protection is still intact, and whether or not "
                "a replacement is bought. The only remaining conditions are "
                "that the name was not bought today and that nothing is in "
                f"flight on it. {replacement_clause}. That proposal then "
                "goes through the Risk Manager like any other exit; do not "
                "assume it will happen."
            )
        return "\n".join(lines)
