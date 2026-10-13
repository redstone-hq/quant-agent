"""The `trades.action` vocabulary and the TRAIL_STOP fill rules — defined ONCE.

Pure: no storage imports, no DB access, so storage (position chaining,
calibration) and non-storage readers (`src/quantities.py`, which `src/api/`
imports) share one copy instead of restating it.

Two TRAIL_STOP rules live here because they answer two different questions;
do NOT collapse them:
  - `trail_stop_reduced_position` — SHARE COUNTING: did this row take shares
    out of the book? (the WIDER rule: a partial fill later canceled counts).
  - `is_filled_trail_stop` — REALIZED EXIT: is this a priceable round-trip
    exit? (the NARROWER rule: status must be 'filled').
"""

from __future__ import annotations

#: Actions that OPEN a position chain, mapped to the direction it carries.
#: SHORT mints a chain exactly as BUY does — owner decision, 2026-08-31:
#: "money made lost, decisions made lost, it's the same thing as everything
#: else." Mirrors `src/conviction_ledger.py::_OPEN_ACTIONS`, which reduces
#: the resulting chain to a round trip.
POSITION_OPEN_ACTIONS: dict[str, str] = {"BUY": "long", "SHORT": "short"}

#: Actions that (fully or partially) REDUCE a held position once executed,
#: split by WHICH side they can retire. SWEEP_BUY / SWEEP_SELL are
#: deliberately absent from all three: the cash-sweep vehicle is stopless and
#: excluded from calibration for the same reason — it has no entry thesis or
#: stop to chain a position around.
#:
#: The COVER family was added on 2026-08-31 (owner decision: short trades are
#: recorded and scored exactly as longs are). Before that a short opened no
#: chain and a cover retired nothing, so no short round trip existed to score.
#: `EMERGENCY_COVER` was already recognized — the short-side twin of
#: EMERGENCY_SELL, see src/pipeline.py's `_forced_close_side_and_qty` — but
#: with no short chain to attach to it could never actually close one.
LONG_EXIT_ACTIONS: frozenset[str] = frozenset({"EMERGENCY_SELL"})
LONG_EXIT_PREFIXES: tuple[str, ...] = ("SELL", "PARTIAL_SELL")
SHORT_EXIT_ACTIONS: frozenset[str] = frozenset({"EMERGENCY_COVER"})
SHORT_EXIT_PREFIXES: tuple[str, ...] = ("COVER", "PARTIAL_COVER")
#: Exits that can retire EITHER side: a stop, a trail, a take-profit, a
#: deterministic de-lever or a reviewer REDUCE all fire against whatever
#: position is open, and the trades row records no side of its own.
EITHER_SIDE_EXIT_ACTIONS: frozenset[str] = frozenset(
    {
        "FORCE_DELEVER",
        "REDUCE",
        "TAKE_PROFIT",
        "STOP_OUT",
        "TRAIL_STOP",
        # RECONCILED_EXIT (item 173(a)): a broker-side exit the reconciler wrote
        # back but whose order_type it could NOT prove was a protective stop —
        # an honest "the broker closed this, cause unattributed" marker, never
        # a STOP_OUT it can't stand behind. It retires a real position exactly
        # like a filled STOP_OUT, so it belongs to the exit-side chain here for
        # position_id assignment and calibration to count it as a closed lot.
        "RECONCILED_EXIT",
    }
)

#: Kept as the flat union for every caller that only asks "is this row on the
#: exit side at all" (`backfill_position_ids`, `_categorize_exit_reason`).
POSITION_EXIT_ACTIONS: frozenset[str] = LONG_EXIT_ACTIONS | SHORT_EXIT_ACTIONS | EITHER_SIDE_EXIT_ACTIONS
POSITION_EXIT_PREFIXES: tuple[str, ...] = LONG_EXIT_PREFIXES + SHORT_EXIT_PREFIXES


def is_position_exit_action(action: str | None) -> bool:
    """True for any action that belongs to an open position's chain on the
    exit side — SELL/PARTIAL_SELL* and COVER/PARTIAL_COVER* by prefix (
    PARTIAL_SELL(15%) etc. carry the trim fraction in the action string
    itself) plus the fixed sets above.
    A TRAIL_STOP row is included even when it is only a stop *placement*
    (not yet filled) — Phase 6 spec: a chain inherits TRAIL_STOP rows
    unconditionally; only the qty math cares whether one actually fired."""
    act = (action or "").upper()
    return act.startswith(POSITION_EXIT_PREFIXES) or act in POSITION_EXIT_ACTIONS


def exit_action_side(action: str | None) -> str | None:
    """Which direction of chain this exit can retire — "long", "short", or
    None for the either-side exits (stops, trails, take-profits, de-levers).

    Used only by `_assign_position_ids`, so a SELL can never be mistaken for
    the close of a short chain (or a COVER for the close of a long one). A
    row whose side does not match the chain that is open is left unattached
    rather than guessed at, exactly as an exit arriving with nothing open is.
    """
    act = (action or "").upper()
    if act.startswith(SHORT_EXIT_PREFIXES) or act in SHORT_EXIT_ACTIONS:
        return "short"
    if act.startswith(LONG_EXIT_PREFIXES) or act in LONG_EXIT_ACTIONS:
        return "long"
    return None


def is_filled_trail_stop(row, action: str) -> bool:
    """REALIZED-EXIT rule: True for a TRAIL_STOP row the broker actually EXECUTED
    as a priceable exit.

    A TRAIL_STOP row is written fill_status='submitted' at placement and only
    flipped to 'filled' by _reconcile_fills when the broker reports a fill, so
    the status is what distinguishes "protection sitting there" from "the stop
    sold our shares". Mirrors the same distinction in
    pipeline._build_post_exit_reality. Legacy rows can carry a NULL
    fill_status; those only count when a real fill_qty was recorded, so a
    never-filled stop can't book a phantom exit at its stop price.

    NOT the share-counting rule — see `trail_stop_reduced_position`.
    """
    if action != "TRAIL_STOP":
        return False
    try:
        status = (row["fill_status"] or "").lower()
    except (KeyError, IndexError, TypeError):
        status = ""
    if status == "filled":
        return True
    try:
        return status == "" and float(row["fill_qty"] or 0) > 0
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def trail_stop_reduced_position(row, action: str) -> bool:
    """SHARE-COUNTING rule: True when a TRAIL_STOP row actually took shares
    OUT of the book. The shares it took are its recorded `fill_qty`.

    Deliberately the WIDER of the two TRAIL_STOP rules — they are different
    questions, so do NOT collapse them. `is_filled_trail_stop` asks "is this a
    priceable realized exit" and therefore requires fill_status='filled'
    (or a legacy NULL status with a recorded fill_qty). A stop that filled
    PARTIALLY and was then canceled or expired carries a terminal status
    that is not 'filled' while still holding `fill_qty > 0`: there is no
    clean round trip to price, but those shares are genuinely gone from
    the broker's book, so a pure quantity ledger must subtract them or it
    will believe it holds stock it has already sold.

    Anything else — fill_status NULL / 'submitted' / 'pending_submit' with
    no fill, or a cancel or expiry that never traded — is protection
    resting at the broker and moves no shares.

    `action` is required and checked: this answers a question only about
    TRAIL_STOP rows; every other action's quantity effect is decided by the
    caller's signing rule, not here.
    """
    if (action or "").upper() != "TRAIL_STOP":
        return False
    try:
        executed = float(row["fill_qty"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        executed = 0.0
    if executed > 0:
        return True
    return is_filled_trail_stop(row, "TRAIL_STOP")


def executed_qty(row) -> float:
    """Shares this row actually moved: the reconciled `fill_qty` when one was
    recorded, otherwise the requested `qty`. The ONE share-quantity reader for
    share counting (position grouping, open-R lots, the chain-flat check)."""
    try:
        fill = float(row.get("fill_qty") or 0)
    except (TypeError, ValueError):
        fill = 0.0
    if fill > 0:
        return fill
    try:
        return float(row.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def share_change(row) -> str | None:
    """SHARE-COUNTING rule: "in" (an opening lot), "out" (a share-reducing exit)
    or None (moves no shares of the position). The ONE answer to "did shares
    leave", used by the position grouper, the open-R lot check and the API's
    chain-flat check alike.

    Classified by ACTION, never by the mere presence of a quantity. A TRAIL_STOP
    row is written for the FULL position at every stop move with no fill; it is
    protection sitting there, and counts as "out" only under
    `trail_stop_reduced_position` — which includes a stop that filled partially
    and was then canceled (its `fill_qty` shares are gone; `executed_qty`
    prefers that fill). Every other action (HOLD, SWEEP_*, records) is None.

    Says nothing about whether the row EXECUTED: callers that see unexecuted
    rows (a canceled SELL, a rejected BUY) must also apply their executed-trade
    test; the open-R reader's rows are already filtered by it.
    """
    action = str(row.get("action") or "").upper()
    if action in POSITION_OPEN_ACTIONS:
        return "in"
    if action == "TRAIL_STOP":
        return "out" if trail_stop_reduced_position(row, action) else None
    if is_position_exit_action(action):
        return "out"
    return None
