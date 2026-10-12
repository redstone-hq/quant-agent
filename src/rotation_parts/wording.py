"""Rotation sell-reason wording (lifted verbatim from src/rotation.py)."""

from __future__ import annotations

from src.rotation_parts.types import (
    REQUIRED_BUY_LEG_GATES,
    ROTATION_REASON_MAX_CHARS,
    RotationClearance,
    RotationOpportunity,
)


def rotation_constraint_clause(
    *,
    binding: tuple[str, ...] = (),
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    entry_budget_usd: float | None = None,
    min_entry_usd: float | None = None,
) -> str:
    """The clause a rotation's own audit record carries naming WHY there was
    no room. One sentence, checkable, and about the limit that was actually
    binding.

    2026-09-23, found by adversary review of this change. Re-pointing the
    precondition at the funding constraint without re-pointing this made the
    SALE's reason false in exactly the new case: the string written onto the
    broker order and handed to the Risk Manager would have read "Headroom
    14.50% of the 25.00% risk ceiling, under the 0.50% minimum" — an
    arithmetic claim that is plainly untrue — on a live sale, produced only
    by the branch this change exists to open. The PM prompt and the owner's
    report were re-pointed and this was not. Same class as the 2026-09-17
    CRM incident, one function over.

    An empty `binding` reproduces the legacy sentence byte-for-byte, so every
    caller that has not been threaded the funding view is unchanged.
    """
    if "funding" in binding and isinstance(entry_budget_usd, (int, float)) and isinstance(min_entry_usd, (int, float)):
        risk = (
            f"Risk headroom {headroom_pct:.2f}% of {ceiling_pct:.2f}%, under the {floor_pct:.2f}% minimum; "
            if "risk_budget" in binding
            else ""
        )
        return (
            f"{risk}${float(entry_budget_usd):,.0f} deployable, under the "
            f"${float(min_entry_usd):,.0f} position that carries the owner's "
            "minimum risk per position at the candidate's own stop."
        )
    return f"Headroom {headroom_pct:.2f}% of the {ceiling_pct:.2f}% risk ceiling, under the {floor_pct:.2f}% minimum."


def rotation_sell_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    clearance: "RotationClearance | None" = None,
    #: 2026-09-23. The binding constraints and the funding view, so the
    #: clause naming WHY there was no room states the limit that actually
    #: bound rather than always quoting the risk ceiling. Empty/`None`
    #: reproduces the legacy sentence byte-for-byte.
    binding: tuple[str, ...] = (),
    entry_budget_usd: float | None = None,
    min_entry_usd: float | None = None,
) -> str:
    """The checkable reason a rotation sale carries, from measured facts only.

    Every clause names something recorded elsewhere this run: the held
    symbol's own `candidate_eligibility` failures, the `StructuralProtection
    Check` basis and detail, the new candidate's rank score, and the book's
    headroom. Nothing here is a feeling or a forecast, so the Risk Manager
    can check every claim against the blocks it is shown and the evening
    review can grade it.

    Kept compact (the rule list is capped at 100 characters, the protection
    detail at 60) because `PortfolioConstructor._build_sell` appends the
    thesis condition and then truncates the order's reasoning at 500; the
    untruncated detail lives in the `rotation` pipeline_event.
    """
    if opportunity.tier == "ranked_margin":
        # Board item 39. The ranked-margin tier sells a position that still
        # passes the desk's own entry gates, purely to fund a replacement.
        # If the replacement is then refused downstream the desk has closed
        # a position for a reason that never materialised — so the sale may
        # only be built once the replacement has been run through
        # `REQUIRED_BUY_LEG_GATES` against the book as it will be AFTER the
        # sale.
        #
        # This raise is unconditional and reads no config. A feature flag
        # decides whether the CALLER gets as far as asking; it cannot
        # decide whether the question is answerable.
        if not isinstance(clearance, RotationClearance):
            raise ValueError(
                "rotation_sell_reason: the ranked-margin tier requires a "
                "RotationClearance proving the replacement BUY was checked "
                "against projected post-sale state "
                f"(gates: {', '.join(REQUIRED_BUY_LEG_GATES)}). No config "
                "flag substitutes for it — board item 39, attempt 1."
            )
        if not clearance.covers(
            held_symbol=opportunity.held_symbol,
            new_symbol=opportunity.new_symbol,
        ):
            raise ValueError(
                "rotation_sell_reason: the RotationClearance supplied is not "
                f"for {opportunity.held_symbol} -> {opportunity.new_symbol} "
                "with every required gate checked (it covers "
                f"{clearance.held_symbol} -> {clearance.new_symbol}, gates "
                f"{', '.join(clearance.gates_checked)})."
            )
        return _ranked_margin_sell_reason(
            opportunity,
            protection_basis=protection_basis,
            protection_detail=protection_detail,
            headroom_pct=headroom_pct,
            ceiling_pct=ceiling_pct,
            floor_pct=floor_pct,
            clearance=clearance,
            binding=binding,
            entry_budget_usd=entry_budget_usd,
            min_entry_usd=min_entry_usd,
        )
    if opportunity.tier != "ineligible_hold":
        raise ValueError(
            "rotation_sell_reason knows two tiers, 'ineligible_hold' and "
            f"'ranked_margin'; {opportunity.tier!r} is neither and is not "
            "executable"
        )
    failed_rules = ("; ".join(opportunity.reasons) or "entry rules")[:100]
    # Owner ruling 2026-10-01. The sale stands on the entry bar alone, so the
    # reason states the bar it fails and nothing it does not depend on. The
    # replacement, where one exists, is named as a SEPARATE decision — never
    # as this sale's justification — and is omitted entirely when there is
    # none, rather than rendered from a placeholder score.
    replacement = ""
    if opportunity.new_symbol and opportunity.new_score is not None:
        replacement = (
            f" Best-ranked candidate {opportunity.new_symbol} (score "
            f"{opportunity.new_score:.2f}) is its own separate decision."
        )
    return (
        f"ROTATION (deterministic, src/rotation.py): {opportunity.held_symbol} "
        f"fails the desk's own entry rules today ({failed_rules}); it would "
        f"not be bought today, so it has stopped earning its place (owner "
        f"ruling 2026-10-01 — neither a full book nor a replacement is "
        f"required). Protection: {protection_basis}: "
        f"{protection_detail[:40]}.{replacement}"
    )


def _ranked_margin_sell_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    clearance: RotationClearance | None,
    binding: tuple[str, ...] = (),
    entry_budget_usd: float | None = None,
    min_entry_usd: float | None = None,
) -> str:
    """The checkable reason a RANKED-MARGIN rotation sale carries.

    Same discipline as the categorical reason above — every clause names
    something recorded elsewhere this run — with two additions the
    categorical tier does not need:

      * the like-for-like sub-score the margin was actually cleared on
        (`shared_seats`, `docs/INCIDENT_HISTORY.md` 2026-09-14), because
        the full composite is a coverage-sensitive weighted SUM and is not
        comparable term-for-term between two names; and
      * the deployable budget the projected post-sale book produced, which
        is what the replacement BUY was cleared against, so the Risk
        Manager and the evening review can check that the sale was
        sequenced behind the buy's gates rather than ahead of them (board
        item 39).

    Kept compact for the same 500-character truncation in
    `PortfolioConstructor._build_sell`.
    """
    seats = ("; ".join(opportunity.shared_seats) or "none")[:40]
    held_shared = opportunity.held_shared_score
    new_shared = opportunity.new_shared_score
    held_score = opportunity.held_score
    tail = (
        f"BUY pre-cleared post-sale (deployable "
        f"${clearance.projected_entry_budget:.0f}, "
        f"{clearance.projected_budget_basis})."
        if clearance is not None
        else "CONTINGENT on the replacement BUY clearing its gates; withdrawn with it if it does not."
    )
    constraint_clause = rotation_constraint_clause(
        binding=binding,
        headroom_pct=headroom_pct,
        ceiling_pct=ceiling_pct,
        floor_pct=floor_pct,
        entry_budget_usd=entry_budget_usd,
        min_entry_usd=min_entry_usd,
    )
    reason = (
        f"ROTATION (ranked margin, src/rotation.py): {opportunity.held_symbol}"
        f" is the weakest still-eligible holding (score "
        f"{0.0 if held_score is None else held_score:.2f}); "
        f"{opportunity.new_symbol} ({opportunity.new_score:.2f}) clears the "
        f"{opportunity.margin_pct * 100:.0f}% margin on the seats covering "
        f"both ({seats}: {held_shared} vs {new_shared}). Protection not "
        f"intact ({protection_basis}: {protection_detail[:40]}). "
        f"{constraint_clause} {tail}"
    )
    # `PortfolioConstructor._build_sell` appends the thesis condition and
    # truncates the order's reasoning at 500 characters, and the clause
    # that makes a CONTINGENT proposal honest is the last one — so it is
    # the first thing a long seat list or a long protection detail would
    # eat. The two variable-length clauses are capped above, and this
    # asserts the result rather than trusting the caps
    # (`tests/test_rotation_sequencing.py` pins the worst case).
    if len(reason) > ROTATION_REASON_MAX_CHARS:
        raise ValueError(
            f"rotation reason is {len(reason)} characters, past the "
            f"{ROTATION_REASON_MAX_CHARS} the constructor truncates at — "
            f"the clause naming what the sale was cleared on would be cut"
        )
    return reason


def rotation_proposal_reason(
    opportunity: RotationOpportunity,
    *,
    protection_basis: str,
    protection_detail: str,
    headroom_pct: float,
    ceiling_pct: float,
    floor_pct: float,
    binding: tuple[str, ...] = (),
    entry_budget_usd: float | None = None,
    min_entry_usd: float | None = None,
) -> str:
    """The reason text a PROPOSED rotation close carries into the Risk
    Manager's review — never an authorisation to sell.

    Board item 39. The ranked-margin tier's replacement BUY cannot be
    checked at proposal time: its entry price, stop and size do not exist
    until `PortfolioConstructor` has run, and the gates that refuse it read
    live quotes at execution. So the proposal says so, in the text the Risk
    Manager and the audit row both read, and the SALE itself is built
    separately by `rotation_sell_reason` — which will not produce a string
    at all without a `RotationClearance`.

    Keeping these two apart is the point. A single function that returned a
    usable reason with and without evidence would put the entire guard on
    the caller remembering to pass the evidence.
    """
    if opportunity.tier == "ranked_margin":
        return _ranked_margin_sell_reason(
            opportunity,
            protection_basis=protection_basis,
            protection_detail=protection_detail,
            headroom_pct=headroom_pct,
            ceiling_pct=ceiling_pct,
            floor_pct=floor_pct,
            clearance=None,
            binding=binding,
            entry_budget_usd=entry_budget_usd,
            min_entry_usd=min_entry_usd,
        )
    return rotation_sell_reason(
        opportunity,
        protection_basis=protection_basis,
        protection_detail=protection_detail,
        headroom_pct=headroom_pct,
        ceiling_pct=ceiling_pct,
        floor_pct=floor_pct,
        binding=binding,
        entry_budget_usd=entry_budget_usd,
        min_entry_usd=min_entry_usd,
    )
