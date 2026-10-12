"""Rotation pre-check outcome vocabulary and durable record (lifted verbatim from src/rotation.py)."""

from __future__ import annotations

from src.rotation_parts.constraints import rotation_binding_constraints
from src.rotation_parts.types import RotationRefusal

# ---------------------------------------------------------------------------
# owner-facing account of the pre-check (board item: the missing "why not")
# ---------------------------------------------------------------------------
#
# The pre-check runs every session and its result reached NOTHING the owner
# reads. Worse, its commonest outcome — capital constrained AND no candidate
# good enough to sell a holding for — returned silently from
# `_apply_rotation_execution`, so it left no log line and no durable row
# either. The owner's words (2026-09-23): "Yes portfolio is full. But we're
# still reviewing things, which is how we built it. Report has to show that
# properly."
#
# ONE vocabulary, two consumers. `precheck_record` turns the pre-check into
# the durable audit row; `owner_precheck_lines` renders THAT SAME row for the
# owner. The model-facing prompt text in
# `PortfolioManagerAgent._render_rotation_section` is deliberately left
# byte-for-byte alone — rewording a live seat's prompt is a behaviour change
# on a trading path — but the four cases below are the same four cases it
# branches on, in the same order, and `tests/test_rotation.py` holds them to
# that.

#: The four mutually exclusive outcomes of one pre-check. A `reason` value,
#: i.e. a rule name and never prose — the same split `pm_accounting` uses.
ROTATION_TELEMETRY_UNAVAILABLE = "telemetry_unavailable"
ROTATION_ROOM_AVAILABLE = "room_available"
ROTATION_FULL_NOTHING_BETTER = "full_nothing_outranked_a_holding"
ROTATION_FULL_OPPORTUNITY = "full_candidate_outranked_a_holding"
#: 2026-10-01. The categorical tier now runs BEFORE the "is the book even
#: constrained" refusal, so a below-bar holding can be put up to be cut with
#: the book not full and no candidate ready to take its place. That is not
#: the outranked case and must never borrow its sentence: there is nothing
#: on the other side of the comparison to name.
ROTATION_HOLDING_BELOW_BAR = "holding_below_entry_bar_no_replacement"


def precheck_outcome(precheck) -> str:
    """Which of the four cases this pre-check is. Pure, no config read.

    2026-09-23: "is the book full" is `precheck.binding` — whether ANY of
    the desk's limits stops it taking a new position — and no longer the
    risk budget alone. It was the risk budget alone, which is why this
    reported `room_available` on all 51 retained sessions including the one
    where $92.20 of deployable budget could not fund a $500 order. The
    owner's report was telling him the desk had room at the moment it had
    none; see the module docstring.
    """
    if not getattr(precheck, "telemetry_available", True):
        return ROTATION_TELEMETRY_UNAVAILABLE
    opportunity = getattr(precheck, "opportunity", None)
    if opportunity is not None:
        # 2026-10-01. No replacement candidate means nothing outranked
        # anything: the holding is up to be cut on its own merits alone.
        if not str(getattr(opportunity, "new_symbol", "") or "").strip():
            return ROTATION_HOLDING_BELOW_BAR
        return ROTATION_FULL_OPPORTUNITY
    if _precheck_binding(precheck):
        return ROTATION_FULL_NOTHING_BETTER
    return ROTATION_ROOM_AVAILABLE


def _precheck_binding(precheck) -> tuple[str, ...]:
    """Which limits bound this pre-check, recomputed when the object does
    not carry them.

    A `RotationPrecheck` built before `binding` existed (an older durable
    row replayed, a test fixture, a caller that has not been updated) still
    carries `headroom_pct` and `floor_pct`, and dropping the risk-budget
    constraint for those would be a silent behaviour change in the opposite
    direction from the one this file is fixing. The recomputation is
    `rotation_binding_constraints` itself, not a second copy of the rule.
    """
    carried = tuple(getattr(precheck, "binding", ()) or ())
    if carried:
        return carried
    return rotation_binding_constraints(
        headroom_pct=float(getattr(precheck, "headroom_pct", 0.0) or 0.0),
        floor_pct=float(getattr(precheck, "floor_pct", 0.0) or 0.0),
        # The funding half is judged once, at the best candidate's own stop,
        # when the precheck is built (`evaluate_rotation`); a precheck that
        # carries no `binding` cannot be re-judged here without that stop.
        funding=None,
    )


def precheck_record(
    precheck,
    *,
    execute_enabled: bool,
    ranked_margin_enabled: bool,
) -> dict:
    """The durable audit payload for one pre-check, and the only input
    `owner_precheck_lines` reads.

    Carries the two switches as measured facts, not as advice: the report
    must be able to say that a comparison the desk is not permitted to act
    on was information only, rather than letting the owner believe the desk
    weighed it and declined.
    """
    opportunity = getattr(precheck, "opportunity", None)
    record = {
        "outcome": precheck_outcome(precheck),
        "headroom_pct": float(getattr(precheck, "headroom_pct", 0.0) or 0.0),
        "ceiling_pct": float(getattr(precheck, "ceiling_pct", 0.0) or 0.0),
        "floor_pct": float(getattr(precheck, "floor_pct", 0.0) or 0.0),
        "execute_enabled": bool(execute_enabled),
        "ranked_margin_enabled": bool(ranked_margin_enabled),
        # 2026-09-23. The funding view the precondition was decided on, and
        # which limits were binding when it was. Without these the row
        # cannot be read back: "the book was full" and "the book had room"
        # are the same sentence under two different definitions of full.
        "entry_budget_usd": _opt_float(getattr(precheck, "entry_budget_usd", None)),
        "min_entry_usd": _opt_float(getattr(precheck, "min_entry_usd", None)),
        "binding": ",".join(_precheck_binding(precheck)),
        # Owner mandate 2026-09-23. How many current holdings would not be
        # bought today, and which — the "has every stock kept earning its
        # place" signal. Recorded every session, constrained or not, because
        # a name that decayed below the entry bar while the book still had
        # room is precisely the case natural selection is not yet acting on.
        "held_below_entry_bar": ",".join(getattr(precheck, "held_below_entry_bar", ()) or ()),
        "held_below_entry_bar_count": len(getattr(precheck, "held_below_entry_bar", ()) or ()),
        # Board item 219. The pass ran, and this is what it ran over.
        "held_examined": ",".join(getattr(precheck, "held_examined", ()) or ()),
        "held_examined_count": len(getattr(precheck, "held_examined", ()) or ()),
    }
    if opportunity is not None:
        record.update(
            {
                "tier": str(getattr(opportunity, "tier", "") or ""),
                "held_symbol": str(getattr(opportunity, "held_symbol", "") or ""),
                "new_symbol": str(getattr(opportunity, "new_symbol", "") or ""),
                # Board item 219. The conviction reasons the held name failed
                # the desk's own entry bar on — the ONLY grounds the
                # categorical tier cuts on, and therefore the reason the
                # owner's report must give. Never a P&L figure.
                "held_reasons": ",".join(str(r) for r in (getattr(opportunity, "reasons", ()) or ())),
            }
        )
        return record
    # 2026-09-23 — the near-miss half. `evaluate_rotation` declined at one of
    # `ROTATION_REFUSAL_POINTS` and knows which holding was weighed against
    # which candidate, on what seats, at what ratio. Folded into THIS row
    # rather than written as a second one: two rows for one session's one
    # comparison is the duplicate-shape defect this file has already been
    # bitten by, and the reader would have to join them to learn anything.
    refusal = getattr(precheck, "refusal", None)
    if isinstance(refusal, RotationRefusal):
        record.update(refusal.event_kwargs())
        # `event_kwargs` carries its own stage/outcome/reason for the
        # standalone shape; this row's outcome is the pre-check's, and the
        # refusal point rides as its own named field so neither overwrites
        # the other.
        record.pop("stage", None)
        record.pop("outcome", None)
        record["refusal_point"] = record.pop("reason", "")
        record["outcome"] = precheck_outcome(precheck)
    return record


def _opt_float(value) -> float | None:
    """A finite float, or `None` for anything that is not one. `None` here
    means NOT MEASURED, and must never be flattened to 0.0 — a zero budget
    and an unread budget are opposite facts about whether the desk may
    trade."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _pct(value) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "?"
