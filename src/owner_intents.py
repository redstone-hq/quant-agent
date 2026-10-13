"""The owner intent record and the flag-only actions Freeze, Stop and Start (panel instalment 1).

Stop (owner ruling 2026-10-09) = the desk is OFF: every job's pickup and every
one-shot entry exits without reads or AI once resting entries are cancelled;
positions and broker-held stops are kept. Start clears Stop and Freeze.

Stored action strings stay PAUSE / RESUME (aliased FREEZE / UNFREEZE) so old
rows replay unchanged; owner-facing outcomes say Freeze / Start.

DESK-SIDE ONLY. `src/api/` must never import this: it writes the database,
and tests/test_api_cannot_trade.py forbids that by construction. A future
raising door must live outside `src/api/`.

State is never stored twice: the flags are a replay of the intents that were
ACTED on, in order. Nothing here moves money or touches the broker; the
broker door reads `current_flags` (see src/execution/owner_flags_gate.py).

Staleness has no time constant. An intent is stale when the owner gave it an
`expires_at` that has passed by the time the desk picks it up (EXPIRED, with
that reason). A row naming an action this desk does not know is REFUSED, with
its reason. Freeze, Stop and Start name no position, so replaying them
in raised order is always correct.

There is no per-position "hands off" and no never-touch list: the desk manages
every position it holds. An owner action is an instruction the desk carries out
and keeps managing, recorded as his decision, never an exemption.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone

from src.owner_flags import (  # noqa: F401
    FREEZE,
    PAUSE,
    RESUME,
    STOP,
    UNFREEZE,
    Flags,
    current_flags,
    read_flags,
    stop_in_force,
)

logger = logging.getLogger(__name__)

ACTIONS = {PAUSE, RESUME, STOP}
_OWNER_WORDS = {
    PAUSE: "Freeze now in force: no new positions or adds; exits and stops keep working",
    RESUME: "Start now in force: the desk runs and may open new positions again",
    STOP: "Stop now in force: the desk is off; positions and broker-held stops are kept",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def raise_intent(conn, action, *, symbol=None, params=None, reason=None, expires_at=None, now=None) -> int:
    """Record what the owner asked for. Durable; acts on nothing."""
    if action not in ACTIONS:
        raise ValueError(f"unknown owner action {action!r}")
    sym = symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else None
    cur = conn.execute(
        "INSERT INTO owner_intents (action, symbol, params_json, raised_at, reason, expires_at) VALUES (?,?,?,?,?,?)",
        (
            action,
            sym,
            json.dumps(params or {}, sort_keys=True),
            _iso(now or _now()),
            reason,
            _iso(expires_at) if expires_at else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def process_pending(conn, *, now=None) -> list:
    """Resolve every RAISED intent in raised order: acted, refused or expired.

    Runs via `TradingPipeline.pickup_owner_intents`: once at desk start (main.py,
    before any session or scheduled job) and before EVERY scheduled job
    (scheduler._run_safe, the 30-minute checks included), so an intent raised
    while the desk was down is acted on (or refused with its reason) when it
    starts. That pickup also runs the Freeze sweep while Freeze is in force.
    """
    now = now or _now()
    done = []
    rows = conn.execute(
        "SELECT id, action, symbol, expires_at FROM owner_intents WHERE state='raised' ORDER BY id"
    ).fetchall()
    for rid, action, sym, exp in rows:
        if action not in ACTIONS:
            state, outcome = "refused", f"unknown action {action!r}"
        elif exp and datetime.fromisoformat(exp) <= now:
            state, outcome = "expired", f"expired at {exp} before the desk could act"
        else:
            state, outcome = "acted", _OWNER_WORDS[action]
        conn.execute(
            "UPDATE owner_intents SET state=?, outcome=?, resolved_at=? WHERE id=?", (state, outcome, _iso(now), rid)
        )
        done.append((rid, action, sym, state, outcome))
    conn.commit()
    return done


def intake(db_path) -> list:
    """Desk-side pickup: open its own connection, resolve pending intents."""
    if not db_path:
        return []
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        from src.storage.schema.owner_intent_tables import apply

        apply(conn)
        done = process_pending(conn)
        for rid, action, sym, state, outcome in done:
            logger.info("owner intent #%s %s %s -> %s (%s)", rid, action, sym, state, outcome)
        return done
    finally:
        conn.close()


STOP_CANCEL_FAILED = "owner_intents.stop_cancel_failed"


def record_stop_cancel_failure(exc, result, notifier=None, *, alert=True) -> None:
    """A Stop's cancel of resting entries failed: durable counted row plus a plain-words owner alert."""
    from src.notifier.owner_alert_funnel import send_owner_alert_with_outcome
    from src.sentinel.counted import record_swallowed

    record_swallowed(STOP_CANCEL_FAILED, exc, log=logger)
    if not alert:
        return
    if result is None:
        text = (
            "Stop is on, but the desk could not reach the broker to cancel unfilled orders. "
            "Check the broker and cancel any unfilled orders there by hand. Positions and stops are kept."
        )
    else:
        n = len(result.faults)
        text = (
            f"Stop is on, but {n} unfilled order{'s' if n != 1 else ''} could not be cancelled. "
            "Cancel them at the broker by hand. Positions and stops are kept."
        )
    send_owner_alert_with_outcome(text, notifier=notifier, kind="owner_stop_cancel_failed", pnl_header=False)


NOT_STOPPED, SWEPT, CANCEL_FAULTED = "not_stopped", "swept", "cancel_faulted"


def honour_stop(db_path, broker_factory) -> bool:
    """Owner Stop at a one-shot entry: True when Stop is in force and the caller must exit unworked.

    The in-run backstop every one-shot unit runs; a cancel fault alerts the owner.
    """
    return stop_outcome(db_path, broker_factory) != NOT_STOPPED


def stop_outcome(db_path, broker_factory, *, alert=True) -> str:
    """Pick up intents; if stopped, cancel resting entries. NOT_STOPPED, SWEPT or CANCEL_FAULTED.

    `broker_factory` is called ONLY when stopped, so a stopped process
    constructs nothing but the broker it cancels through; the cancel is the
    Freeze sweep (protective stops and exits are kept).

    The sweep never raises on a broker refusal or an unreachable broker -- it
    returns the fault in `faults` -- so the faults are read here, not just
    exceptions. Every fault writes a durable counted row; `alert=False` leaves
    the owner alert to the caller (main.py --mode honour_stop, whose caller
    the owner switch alerts on the exit code).
    """
    from src.sentinel.guarded import NO_LEDGER, record_guarded_pass

    if not db_path:
        return NOT_STOPPED
    try:
        intake(db_path)
    except Exception as exc:  # noqa: BLE001 - recorded; a Stop already acted is still read below
        record_guarded_pass(NO_LEDGER, "owner_intents.stop_pickup", exc, log=logger)
    if not stop_in_force(db_path):
        return NOT_STOPPED
    try:
        result = broker_factory().sweep_frozen_resting_orders(db_path)
    except Exception as exc:  # noqa: BLE001 - recorded (and alerted unless the caller alerts); Stop holds
        record_stop_cancel_failure(exc, None, alert=alert)
        return CANCEL_FAULTED
    if result is not None and result.faults:
        record_stop_cancel_failure(RuntimeError(f"cancel faults: {result.faults}"), result, alert=alert)
        return CANCEL_FAULTED
    logger.warning("owner Stop in force: desk off; resting entries swept (%s)", result)
    return SWEPT
