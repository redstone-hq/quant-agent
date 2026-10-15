"""Target-revision vocabulary and level helpers, lifted verbatim from
`src.risk.target_revision`: trigger and refusal codes, the outcome record, and
the pure level/horizon helpers. Leaf module; re-exported by the original."""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.risk.exit_guard import BREAK_CONFIRMATION_ATR_MULTIPLE

# --- Triggers: what legitimises re-deriving -------------------------------

#: The level the target was measured against has been closed through by at
#: least one noise band, on two consecutive trading-day closes.
TRIGGER_LEVEL_BROKEN = "TARGET_LEVEL_BROKEN_CONFIRMED"

#: Today's ATR puts the stored target past `horizon_reach` for the pinned
#: horizon — the derivation would no longer accept it as reachable.
TRIGGER_TARGET_BEYOND_REACH = "TARGET_BEYOND_TODAYS_REACH"

#: Today's ATR puts the stored target inside the derivation's own noise
#: floor — it no longer clears the instrument's own daily range.
TRIGGER_TARGET_INSIDE_NOISE = "TARGET_INSIDE_TODAYS_NOISE_FLOOR"

#: A structural level still in the way now stands BETWEEN the entry and the
#: stored target — the target is aiming past a wall. See the module
#: docstring's trigger 3 for why this is the mirror of TRIGGER_LEVEL_BROKEN
#: and not a new kind of event.
TRIGGER_WALL_IN_FRONT_OF_TARGET = "STRUCTURAL_WALL_STANDING_IN_FRONT_OF_TARGET"

#: THE WAY IN (item 194). Recorded as the `seat` on every outcome that was
#: adjudicated because the position is open, not because a seat named it.
#: `src.models.TargetRevisionFlag` has exactly two fields, symbol and
#: evidence, and no price, so a seat flag supplies NO input to
#: `assess_target_revision` — every number it uses is recomputed from bars
#: or read off the ratified config. Gating the measurement on whether an
#: LLM happened to mention the symbol therefore does not make the write
#: safer; it only makes the coverage arbitrary. This label exists so the
#: record can always say which outcomes came from the unconditional sweep.
SEAT_STRUCTURAL_SWEEP = "structural_sweep"

#: The evidence string filed for a swept position. Deliberately states the
#: absence of a seat opinion rather than inventing one.
SWEEP_EVIDENCE = (
    "no seat raised this symbol; adjudicated because the position is open "
    "and its stored target is re-measured from the chart every session"
)

# --- Outcomes that are NOT a revision, each recorded by name --------------

#: The seat's flag was real but no structural event backs it. This is the
#: expected outcome for "the seat thinks there is more upside", and it is a
#: recorded refusal rather than a silent no-op.
REVISION_NO_TRIGGER = "REFUSAL_NO_STRUCTURAL_EVENT"

#: The target's level was closed through TODAY but not on the prior trading
#: day's close. Same one-day-spring guard as `check_structural_protection`.
REVISION_BREAK_PENDING_CONFIRMATION = "REFUSAL_BREAK_PENDING_CONFIRMATION"

#: Today's ATR puts the stored target outside the derivation's reach, but
#: the PRIOR trading day's close did not. Same gate, same mechanism and the
#: same stored state as the break above — see `raw_trigger_flags`.
REVISION_REACH_PENDING_CONFIRMATION = "REFUSAL_REACH_PENDING_CONFIRMATION"

#: A wall stands between entry and target on today's close but did not on
#: the prior trading day's. Same gate as the two above.
REVISION_WALL_PENDING_CONFIRMATION = "REFUSAL_WALL_PENDING_CONFIRMATION"

#: No `expected_horizon_sessions` on the trade row. Legacy rows predate the
#: pin; the horizon is never recomputed, so these can never be revised.
REVISION_NO_PINNED_HORIZON = "REFUSAL_NO_PINNED_HORIZON"

#: No usable take-profit on the row to revise in the first place.
REVISION_NO_STORED_TARGET = "REFUSAL_NO_STORED_TARGET"

#: Bars/indicators for the symbol could not be read, so the trigger tests
#: themselves cannot run. A DATA FAULT, not a judgement.
REVISION_UNMEASURABLE_INPUTS = "FAULT_NO_BARS_FOR_REVISION"

#: A trigger fired, the chart HAS structure, and the price has closed
#: decisively beyond all of it — nothing in the trade's direction is left to
#: measure against. Distinct from the derivation's REFUSAL_NO_STRUCTURE,
#: which asserts the history holds no level at all.
REVISION_NO_CEILING_LEFT = "REFUSAL_NO_STRUCTURE_LEFT_IN_DIRECTION"

#: A trigger fired and today's bars yield a target the price has already
#: passed. The old target stands — see the note at the check itself.
REVISION_BEHIND_PRICE = "REFUSAL_DERIVED_TARGET_BEHIND_PRICE"

#: A trigger fired and the re-derivation succeeded, but it landed on the
#: same price. Recorded so the flag is never a blank.
REVISION_NO_CHANGE = "NO_CHANGE_ON_REDERIVATION"

#: `TargetDerivation.basis` when the derivation picked a real structural
#: level rather than projecting a measured move. Mirrored from
#: `src.data.levels.derive_structural_target`, which writes the string
#: literally; `tests/test_target_revision.py` asserts the two still agree,
#: because the re-anchor below accepts ONLY this basis.
STRUCTURAL_LEVEL_BASIS = "structural_level"

#: The basis recorded when a target was re-derived with the reach anchored
#: on the latest completed close over the REMAINING horizon, after the
#: entry-anchored derivation had already refused as behind price. Distinct
#: from the plain `structural_level` on purpose: the record must say which
#: anchor produced the number, and a reader must be able to count these.
REANCHORED_BASIS = "structural_level_reanchored_on_remaining_horizon"


@dataclass(frozen=True)
class TargetRevisionOutcome:
    """What happened to one flag, on one symbol.

    Exactly one of `new_price` / `refusal` / `fault` is meaningful, mirroring
    `src.data.levels.TargetDerivation`'s own split between a trade judgement
    and a data fault. `code` is always set — a flag never produces a blank.
    """

    symbol: str
    code: str
    detail: str = ""
    trigger: str = ""
    new_price: float | None = None
    prior_price: float | None = None
    basis: str = ""
    level_used: float | None = None
    refusal: str = ""
    fault: str = ""

    @property
    def revised(self) -> bool:
        return self.new_price is not None and self.code == self.trigger


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def remaining_horizon_sessions(
    *,
    pinned_horizon_sessions: int | None,
    sessions_held: int | None,
) -> int | None:
    """How much of its pinned horizon this position has LEFT, in sessions.

    Both inputs are READ, never derived here and never defaulted:

    * `pinned_horizon_sessions` is `trades.expected_horizon_sessions`,
      written once at BUY and never recomputed.
    * `sessions_held` is the holiday-aware count of TRADING sessions since
      entry — `AlpacaBroker.trading_sessions_held` (item 165), the same
      count `exit_guard`'s noise band and the reviewer's position facts
      already use. Never a calendar-day count.

    Returns None when either input is unreadable, so a caller can refuse
    rather than invent a remaining horizon; returns 0 — a real answer, not
    a missing one — when the position has used its whole horizon or more.
    A negative result is impossible: a position past its horizon has no
    reach left, it does not have negative reach.
    """
    try:
        pinned = int(pinned_horizon_sessions)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    try:
        used = int(sessions_held)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pinned <= 0 or used < 0:
        return None
    return max(0, pinned - used)


def level_backing_target(
    *,
    stored_target: float | None,
    computed_levels: list | tuple | None,
    computed_level_zones: dict | None = None,
    atr: float | None = None,
) -> float | None:
    """Which computed structural level the stored target sits on, or None.

    The entry derivation recorded `TargetDerivation.level_used`, but that
    field is not persisted on the trade row — only the resulting price is.
    So the level is RECOVERED here: the closest computed level whose own
    MEASURED zone (`computed_level_zones[price] = (low, high)`, the span of
    the bars that drew it) contains the stored target. A level with no
    measured zone matches within one 14-day ATR (`atr`) — the stock's
    typical daily move, derived, not a chosen multiplier. With neither,
    that level cannot match (fail closed). No percentage is used; the old
    1% match band was deleted 2026-10-15 (docs/NUMBER_TRIAGE.md).

    Returning None is the honest answer for a `measured_move` target: it was
    never measured against a level, so no level of its can break. Such a
    position is still revisable on the ATR/reach trigger, which needs no
    level at all.
    """
    target = _finite(stored_target)
    if target is None or target <= 0:
        return None
    vol = _finite(atr)
    if vol is not None and vol <= 0:
        vol = None
    zones = computed_level_zones or {}
    best: float | None = None
    best_gap = float("inf")
    for raw in computed_levels or []:
        price = _finite(raw)
        if price is None or price <= 0:
            continue
        gap = abs(price - target)
        zone = zones.get(raw) if raw in zones else zones.get(price)
        lo = hi = None
        if zone is not None:
            try:
                lo, hi = _finite(zone[0]), _finite(zone[1])
            except (TypeError, IndexError):
                lo = hi = None
        if lo is not None and hi is not None and lo <= hi:
            inside = lo <= target <= hi
        elif vol is not None:
            inside = gap <= vol
        else:
            inside = False
        if inside and gap < best_gap:
            best, best_gap = price, gap
    return best


def levels_still_in_the_way(
    *,
    computed_levels: list | tuple | None,
    close_price: float | None,
    atr: float | None,
    is_short: bool,
    break_margin_atr_multiple: float = BREAK_CONFIRMATION_ATR_MULTIPLE,
) -> list[float]:
    """Drop the levels TODAY'S CLOSE has already decisively cleared.

    WHY THIS IS NECESSARY, not a refinement. `find_structural_levels` reads
    pivots out of the whole bar history, so a ceiling price has gapped
    through is STILL in its output — reclassified as support, but present in
    the union of levels. And `derive_structural_target` partitions that
    union against the ENTRY price, so a broken overhead level is still
    "above entry" and would simply be picked again. Re-deriving without this
    filter returns the same number in exactly the motivating case: the
    ceiling is gone, and the derivation hands it back.

    The test applied is the SAME break test as `target_level_broken` — one
    `BREAK_CONFIRMATION_ATR_MULTIPLE` beyond the level on a completed daily close —
    applied to every level rather than only the one the target sat on. No
    new constant, and no second definition of "broken": a resistance the
    price has closed decisively above is not overhead any more, whichever
    level it happens to be.

    Degrades to the levels unchanged when the close or ATR is missing, so a
    missing input can never silently empty the level set and turn a
    structural read into a measured move.
    """
    levels = [p for p in (_finite(lv) for lv in computed_levels or ()) if p is not None]
    close = _finite(close_price)
    vol = _finite(atr)
    if close is None or vol is None or vol <= 0:
        return levels
    margin = vol * break_margin_atr_multiple
    if is_short:
        # A short's targets sit below; a level the close has fallen a noise
        # band beneath is no longer a floor in the way.
        return [p for p in levels if close > p - margin]
    return [p for p in levels if close < p + margin]


def walls_between(
    *,
    stored_target: float | None,
    reference_price: float | None,
    surviving_levels: list[float] | tuple[float, ...] | None,
    is_short: bool,
) -> list[float]:
    """Every structural level standing BETWEEN the position and its stored
    target, nearest first. Empty is the healthy answer.

    PURE, and deliberately independent of the derivation so it can be
    tested without bars. `surviving_levels` must already have been put
    through `levels_still_in_the_way`, because a level price has closed
    decisively beyond is not a wall any more and counting it would
    manufacture a finding out of a broken ceiling.

    `reference_price` is where the position is measured FROM, and every
    caller passes the ENTRY, not the current price — the same anchor the
    whole module holds fixed. Measuring from the latest close would flag
    every position that has moved away from its entry, which is a
    statement about the price move and not about the target.

    A level exactly ON the target is not between anything and is excluded
    — that is the target sitting on its own wall, which is the correct
    outcome, not a finding. Strict inequalities on both ends do that.

    THIS FUNCTION LIVES HERE, not in the script that first needed it
    (`scripts/check_stored_targets.py`, which now imports it), because it
    is now also the trigger test `assess_target_revision` runs. Two copies
    of "is a wall in the way" would let the scheduled report and the live
    revision path disagree about the same chart, which is the exact class
    of failure this module's one-body re-derivation exists to prevent.
    """
    target = _finite(stored_target)
    ref = _finite(reference_price)
    if target is None or ref is None or target <= 0 or ref <= 0:
        return []
    out: list[float] = []
    for raw in surviving_levels or ():
        level = _finite(raw)
        if level is None or level <= 0:
            continue
        if is_short:
            # A short's target sits below; a wall is a floor it must get
            # through on the way down.
            if target < level < ref:
                out.append(level)
        elif ref < level < target:
            out.append(level)
    return sorted(out, reverse=bool(is_short))
