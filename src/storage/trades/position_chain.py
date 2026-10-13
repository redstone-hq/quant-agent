"""Position-id chaining, lifted VERBATIM from ledger.py (exit vocabulary now in src/trade_actions.py)."""

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
from src.trade_actions import is_filled_trail_stop as _is_filled_trail_stop
from src.trade_actions import is_position_exit_action as _is_position_exit_action
from src.trade_actions import trail_stop_reduced_position as _trail_stop_reduced_position


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


def _assign_position_ids(rows: list[dict]) -> dict[int, str | None]:
    """Walk one symbol's trades chronologically and derive position_id for
    every row. `rows` must already be ordered oldest-first (timestamp, id)
    and each dict needs at least id/action/qty/fill_qty/fill_status/
    position_id.

    Rule: a BUY (long) or a SHORT (short) from flat mints a fresh id; every
    subsequent entry on the same side (scale-in) and every recognized
    exit-family action for that side (see `_is_position_exit_action` and
    `_exit_action_side`) inherits it until the running executed-qty net
    returns to ~0, at which point the chain is closed and the next entry
    mints a new one. A row with no open chain to attach to (an exit that
    arrives while nothing is open — typically the ledger's oldest record for
    a symbol whose real position predates this system, or a stray SELL after
    the book already went flat) is left unassigned rather than guessed, per
    spec.

    **Shorts chain identically to longs** (owner decision, 2026-08-31). A
    SHORT opens, a COVER/PARTIAL_COVER/EMERGENCY_COVER retires, and a stop or
    trail retires either side. `qty` is the magnitude of shares moved on both
    sides — a short's net counts UP as it is opened and DOWN as it is
    covered, the same arithmetic as a long — so nothing here is negated or
    special-cased for direction. Before this, `is_open` was `action == "BUY"`
    alone: no SHORT ever received a position_id, so no short round trip
    existed for §9.5's ledger to score. That was the whole of the gap.

    Two guards keep the long side's existing chains untouched, both for
    histories the desk does not currently produce:
      - an entry on the OPPOSITE side while a chain is still open is passed
        through untouched rather than flipping or closing that chain (you
        cannot be long and short the same symbol at one broker, so this is a
        malformed history, not a position);
      - an exit whose side does not match the open chain (a SELL against a
        short, a COVER against a long) is left unattached rather than
        allowed to retire the wrong position.

    Rows that ALREADY carry a position_id are treated as ground truth. That
    is what makes this function safe to call from both the live per-insert
    resolver (which only ever has one new, unassigned row) and the one-time
    historical backfill (which may run against a database where live
    trading has already assigned some rows and older history is still
    NULL) without the two ever disagreeing or re-minting an id that already
    exists — including when the ALREADY-assigned row is not the first row
    in its chain (e.g. a legacy BUY with no id, followed by a live-inserted
    TRAIL_STOP that already minted one): grouping happens in a first,
    id-blind structural pass, and only THEN does a second pass pick, per
    group, whichever id (if any) already exists in it — never one id for
    the earlier rows and a different one for the later rows of what is
    structurally the same chain.
    """
    # Pass 1 — partition into chain groups using ONLY the net-qty rule,
    # ignoring any already-persisted id. Which rows belong to the same
    # chain is a purely structural fact (ends the moment net qty returns to
    # ~0); it does not depend on which of those rows happens to carry an id
    # already.
    groups: list[list[dict]] = []
    passthrough: dict[int, str | None] = {}
    current_group: list[dict] | None = None
    current_net = 0.0
    current_direction: str | None = None
    for row in rows:
        action = (row.get("action") or "").upper()
        open_direction = _POSITION_OPEN_ACTIONS.get(action)
        is_open = open_direction is not None
        is_exit = _is_position_exit_action(action)
        if not is_open and not is_exit:
            # HOLD / SWEEP_BUY / SWEEP_SELL / anything unrecognized: never
            # part of a position chain, and never disturbs one in progress.
            passthrough[row["id"]] = row.get("position_id")
            continue

        executed = _row_counts_as_executed(action, row.get("fill_status"), row.get("fill_qty"))
        qty = float(row.get("fill_qty") if row.get("fill_qty") else row.get("qty") or 0)
        filled_trail = action == "TRAIL_STOP" and _is_filled_trail_stop(row, action)
        chain_open = current_group is not None and current_net > 1e-6

        if is_open:
            if chain_open and open_direction != current_direction:
                # A SHORT while a long chain is still open (or the reverse).
                # Not producible by this desk and not a position either way:
                # passed through so the open chain is left exactly as it was.
                passthrough[row["id"]] = row.get("position_id")
                continue
            if not chain_open:
                current_group = []
                groups.append(current_group)
                current_net = 0.0
                current_direction = open_direction
            current_group.append(row)
            if executed:
                current_net += qty
        else:
            if not chain_open:
                # An exit with nothing open — its own singleton group,
                # which pass 2 resolves to None unless it happens to
                # already carry an id (a hand-corrected row: trust it,
                # still never mint a NEW one for an unattached exit).
                groups.append([row])
                current_group = None
                current_direction = None
                continue
            exit_side = _exit_action_side(action)
            if exit_side is not None and exit_side != current_direction:
                # A SELL against an open short, or a COVER against an open
                # long. It cannot be this chain's exit; unattached, and the
                # chain it does not belong to is left running.
                groups.append([row])
                continue
            current_group.append(row)
            if action == "TRAIL_STOP":
                if executed and filled_trail:
                    current_net -= qty
            elif executed:
                current_net -= qty
            if current_net <= 1e-6:
                current_group = None
                current_net = 0.0
                current_direction = None

    # Pass 2 — resolve one id per group. An id already present ANYWHERE in
    # the group wins (ground truth, whichever row in the chain happened to
    # carry it first); otherwise mint one fresh id for the whole group, but
    # only for a group that actually opened with a BUY or a SHORT — a lone
    # unattached exit (no entry, no existing id) stays unassigned rather
    # than guessed.
    assignments: dict[int, str | None] = dict(passthrough)
    for group in groups:
        existing_ids = [r.get("position_id") for r in group if r.get("position_id")]
        opened = any((r.get("action") or "").upper() in _POSITION_OPEN_ACTIONS for r in group)
        if existing_ids:
            resolved = existing_ids[0]
        elif opened:
            resolved = _new_position_id()
        else:
            resolved = None
        for r in group:
            assignments[r["id"]] = r.get("position_id") or resolved
    return assignments
