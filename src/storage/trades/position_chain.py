"""Position-id chaining: stored `trades.position_id`, re-derived from the one fill walk in src/trade_actions.py."""

from __future__ import annotations

import uuid

# The action vocabulary and both TRAIL_STOP rules live in src/trade_actions.py;
# imported under their historical private names so every existing importer of
# this module (ledger.py, db.py) keeps working unchanged.
from src.trade_actions import EITHER_SIDE_EXIT_ACTIONS as _EITHER_SIDE_EXIT_ACTIONS
from src.trade_actions import LONG_EXIT_ACTIONS as _LONG_EXIT_ACTIONS
from src.trade_actions import LONG_EXIT_PREFIXES as _LONG_EXIT_PREFIXES
from src.trade_actions import POSITION_EXIT_ACTIONS as _POSITION_EXIT_ACTIONS
from src.trade_actions import POSITION_EXIT_PREFIXES as _POSITION_EXIT_PREFIXES
from src.trade_actions import POSITION_OPEN_ACTIONS as _POSITION_OPEN_ACTIONS
from src.trade_actions import SHORT_EXIT_ACTIONS as _SHORT_EXIT_ACTIONS
from src.trade_actions import SHORT_EXIT_PREFIXES as _SHORT_EXIT_PREFIXES
from src.trade_actions import exit_action_side as _exit_action_side
from src.trade_actions import executed_qty as _executed_qty
from src.trade_actions import is_position_exit_action as _is_position_exit_action
from src.trade_actions import fill_time_split as _fill_time_split
from src.quantities import ORDER_QTY_DECIMALS
from src.trade_actions import fill_sort_key as _fill_sort_key
from src.trade_actions import share_change as _share_change
from src.trade_actions import walk_positions as _walk_positions


def _new_position_id() -> str:
    """Opaque, stable identifier minted when a BUY opens a position from
    flat. Same shape as this codebase's run/decision ids
    (`RunContext.start`, `decision_id` in pipeline_stages.py) — a short
    prefix plus a uuid4 hex fragment — so it reads the same way in logs
    and URLs, without claiming to BE a run or decision id."""
    return f"pos-{uuid.uuid4().hex[:12]}"


def _row_counts_as_executed(action: str | None, fill_status, fill_qty) -> bool:
    """Python-side mirror of `Database._executed_trade_predicate()`,
    usable on values pulled out of a row (rather than in a WHERE clause).
    Kept in sync deliberately — see that method's docstring."""
    status = fill_status or ""
    if status == "" and (action or "").upper() != "HOLD":
        return True
    if status == "filled":
        return True
    try:
        return float(fill_qty or 0) > 0
    except (TypeError, ValueError):
        return False


def _executed_row(row: dict) -> bool:
    return _row_counts_as_executed(row.get("action"), row.get("fill_status"), row.get("fill_qty"))


def _event_split(rows: list[dict]) -> tuple[list[dict], list[dict]] | None:
    """`(earlier, ordered)`: one symbol's rows split at the oldest flat point the fill walk
    can order from (`trade_actions.fill_time_split`, against the ledger's own net).

    `ordered` is in the order the position walk takes it: a fill by its broker fill
    time, anything else by when it was written (`trade_actions.fill_sort_key`). With
    every fill timed nothing is earlier. None when a fill with no broker fill time sits
    inside the live position - the same rows make open R unknown.
    """
    moving = [r for r in rows if _share_change(r) is not None and _executed_row(r)]
    split = _fill_time_split(moving, held=None, decimals=ORDER_QTY_DECIMALS)
    if split is None:
        return None
    ordered = sorted(rows, key=_fill_sort_key)
    if not split[0]:
        return [], ordered
    if split[1]:
        start = _fill_sort_key(split[1][0])
        cut = next((i for i, r in enumerate(ordered) if _fill_sort_key(r) >= start), len(ordered))
    else:
        last = max(_fill_sort_key(r) for r in moving)
        cut = next((i for i, r in enumerate(ordered) if _fill_sort_key(r) > last), len(ordered))
    return ordered[:cut], ordered[cut:]


def _event_order(rows: list[dict]) -> list[dict] | None:
    """One symbol's rows in walk order, or None unless EVERY fill can be ordered.

    The regroup and the conviction scorer use `_event_split` instead, so an old fill
    with no fill time no longer blocks the positions after the last flat point.
    """
    split = _event_split(rows)
    return None if split is None or split[0] else split[1]


def _assign_position_ids(rows: list[dict], *, reserved=()) -> dict:
    """Derive position_id for every row of ONE symbol, walked in the order given.

    `rows` must already be in event order (`_event_order`); each needs at least
    id/action/qty/fill_qty/fill_status/position_id. The grouping IS
    `trade_actions.walk_positions` - the walk the open-R lot reader uses - with
    `_row_counts_as_executed` deciding whose shares moved: a BUY (long) or SHORT
    (short) from flat opens a position, later entries on that side and exits that
    can retire it join it, and it closes when the running executed share count
    returns to zero. Shorts chain exactly as longs (owner decision, 2026-08-31).

    Ids: a position keeps an id one of its rows already carries unless an EARLIER
    position kept that id - then it mints a fresh one. Stored ids are NOT ground
    truth: an id is stamped when a row is written, before the broker says when it
    filled, so a fresh buy written before the old position's stop-out fill was known
    carries the old id and moves to its own position here. A row the walk attaches
    to no position (an exit with nothing open, a wrong-side exit, an other-side entry
    while a position is open) keeps its id only if no position kept it, so it never
    joins a position the walk says it is not part of; rows that are neither entries
    nor exits (HOLD, SWEEP_*) keep theirs. A `reserved` id (carried by rows before
    the last flat point the walk can order) is never kept by a walked position.
    """
    groups, unattached, _ = _walk_positions(rows, decimals=ORDER_QTY_DECIMALS, executed=_executed_row)
    assignments: dict = {}
    claimed: set[str] = set(reserved)
    for group in groups:
        kept = next((pid for r in group if (pid := r.get("position_id")) and pid not in claimed), None)
        resolved = kept or _new_position_id()
        claimed.add(resolved)
        for r in group:
            assignments[r["id"]] = resolved
    for r in unattached:
        pid = r.get("position_id")
        action = str(r.get("action") or "").upper()
        positional = action in _POSITION_OPEN_ACTIONS or _is_position_exit_action(action)
        assignments[r["id"]] = pid if not positional or (pid and pid not in claimed) else None
    return assignments
