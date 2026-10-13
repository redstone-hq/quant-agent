"""The Type A (range) R-multiple ratchets of the deterministic trail.

Moved verbatim out of `src/risk/trailing.py` (2026-10-04) so the arithmetic
can be built and exercised alone. The doctrine, the numbers and the result
types stay in `src/risk/trailing.py`; read its module docstring first.
"""

from __future__ import annotations

from src.risk.trailing import (
    RANGE_BREAKEVEN_R_MULTIPLE,
    RANGE_SECOND_RATCHET_LOCK_R,
    RANGE_SECOND_RATCHET_TRIGGER_R,
    TRAIL_CODE_RANGE_ALREADY_BREAKEVEN,
    TRAIL_CODE_RANGE_BELOW_1R,
    TRAIL_CODE_RANGE_BELOW_2R,
    TRAIL_CODE_RANGE_BREAKEVEN_OFF_SIDE,
    TRAIL_CODE_RANGE_NO_INITIAL_STOP,
    TRAIL_CODE_RANGE_SECOND_OFF_SIDE,
    TRAIL_CODE_RANGE_ZERO_RISK,
    TRAIL_CODE_TRAILED,
    TrailEvaluation,
    TrailProposal,
    _finite,
)

__all__ = ["_range_breakeven_ratchet", "_range_second_ratchet"]


def _range_breakeven_ratchet(
    *,
    symbol: str,
    ent: float,
    cur: float,
    stop: float,
    risk_per_share: float | None,
    is_short: bool,
    setup_type: str | None,
) -> TrailEvaluation:
    """Type A's +1R breakeven ratchet — see the module docstring's 2026-09-04
    fix #3 note.

    `risk_per_share` is 1R per held share: per-lot entry risk (every open lot
    against its OWN initial stop, `risk.metrics.held_position_risk_dollars`)
    / held qty, so "price reached +1R" is exactly "unrealised P&L >= per-lot
    dollar risk". Fails closed: with `risk_per_share` None (R unknown — any lot
    lacks a usable stop) it proposes nothing (`TRAIL_CODE_RANGE_NO_INITIAL_STOP`)
    rather than guessing at the risk that was taken.
    Deliberately skips the ordinary minimum-ratchet / noise-band invariants
    below — this move is not a structural ratchet being tuned to avoid
    churn, it is a one-time, always-worthwhile transition from "full initial
    risk" to "no risk", regardless of how small the percentage move to
    breakeven happens to be.
    """
    risk = _finite(risk_per_share) if risk_per_share is not None else None
    if risk is None:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_NO_INITIAL_STOP)
    if risk <= 0:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_ZERO_RISK)

    if is_short:
        trigger = ent - RANGE_BREAKEVEN_R_MULTIPLE * risk
        reached_1r = cur <= trigger
        already_protected = stop <= ent  # breakeven or better already
    else:
        trigger = ent + RANGE_BREAKEVEN_R_MULTIPLE * risk
        reached_1r = cur >= trigger
        already_protected = stop >= ent

    if not reached_1r:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_BELOW_1R)
    if already_protected:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_ALREADY_BREAKEVEN)

    candidate = round(ent, 2)
    if is_short:
        if not (cur < candidate < stop):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_BREAKEVEN_OFF_SIDE)
    else:
        if not (stop < candidate < cur):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_BREAKEVEN_OFF_SIDE)

    return TrailEvaluation(
        TrailProposal(
            symbol=symbol.upper(),
            new_stop=candidate,
            previous_stop=stop,
            source="breakeven_ratchet",
            reason=(
                f"deterministic trail (breakeven_ratchet): {setup_type or 'unknown'} "
                f"setup reached +{RANGE_BREAKEVEN_R_MULTIPLE:.0f}R (price ${cur:.2f}, "
                f"entry ${ent:.2f}, initial risk ${risk:.2f}); stop ${stop:.2f} -> "
                f"${candidate:.2f} (breakeven) per standard R-multiple practice "
                f"(Van Tharp / Elder) rather than staying fully unprotected until "
                f"the whole target is hit"
            ),
        ),
        TRAIL_CODE_TRAILED,
    )


def _range_second_ratchet(
    *,
    symbol: str,
    ent: float,
    cur: float,
    stop: float,
    risk_per_share: float | None,
    is_short: bool,
    setup_type: str | None,
) -> TrailEvaluation:
    """Type A's SECOND ratchet — item 142, owner-ratified 2026-09-25.

    Stacks ON TOP of `_range_breakeven_ratchet` and BELOW the target: once a
    range trade reaches +`RANGE_SECOND_RATCHET_TRIGGER_R`R (2R) of open profit,
    move the stop up to +`RANGE_SECOND_RATCHET_LOCK_R`R (1R), locking in one
    initial-risk-unit of gain. This REDUCES the give-back to a +1R floor once
    +2R is tagged; it does NOT close the gap — the stop is then pinned at +1R
    with no structural trail until the target is exceeded, so a run past +2R
    and a reversal still gives back down to +1R.

    Mirrors `_range_breakeven_ratchet` exactly: fails closed with no
    `risk_per_share` (per-lot R per held share) so R cannot be guessed; measures
    R the same way;
    and skips the ordinary minimum-ratchet / noise-band invariants because
    this is a one-time step-up in protection, not a structural trail being
    tuned against churn. The `stop < candidate < cur` guard (mirrored for a
    short) is what enforces NEVER LOOSEN A STOP: the +1R lock is proposed only
    when it beats the current stop and still sits below price.
    """
    risk = _finite(risk_per_share) if risk_per_share is not None else None
    if risk is None:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_NO_INITIAL_STOP)
    if risk <= 0:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_ZERO_RISK)

    if is_short:
        trigger = ent - RANGE_SECOND_RATCHET_TRIGGER_R * risk
        lock = ent - RANGE_SECOND_RATCHET_LOCK_R * risk
        reached_2r = cur <= trigger
    else:
        trigger = ent + RANGE_SECOND_RATCHET_TRIGGER_R * risk
        lock = ent + RANGE_SECOND_RATCHET_LOCK_R * risk
        reached_2r = cur >= trigger

    if not reached_2r:
        return TrailEvaluation(None, TRAIL_CODE_RANGE_BELOW_2R)

    candidate = round(lock, 2)
    if is_short:
        # Never loosen: the lock must sit BELOW the live stop (a real tighten)
        # and ABOVE price (still a stop). If the stop is already at or past the
        # lock, this returns without moving it.
        if not (cur < candidate < stop):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_SECOND_OFF_SIDE)
    else:
        if not (stop < candidate < cur):
            return TrailEvaluation(None, TRAIL_CODE_RANGE_SECOND_OFF_SIDE)

    return TrailEvaluation(
        TrailProposal(
            symbol=symbol.upper(),
            new_stop=candidate,
            previous_stop=stop,
            source="second_ratchet",
            reason=(
                f"deterministic trail (second_ratchet): {setup_type or 'unknown'} "
                f"setup reached +{RANGE_SECOND_RATCHET_TRIGGER_R:.0f}R (price "
                f"${cur:.2f}, entry ${ent:.2f}, initial risk ${risk:.2f}); stop "
                f"${stop:.2f} -> ${candidate:.2f}, locking in "
                f"+{RANGE_SECOND_RATCHET_LOCK_R:.0f}R of gain (owner appetite "
                f"ruling 2026-09-25, item 142) rather than giving it all back "
                f"between breakeven and target on a reversal"
            ),
        ),
        TRAIL_CODE_TRAILED,
    )
