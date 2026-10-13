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


def honour_stop(db_path, broker_factory) -> bool:
    """Owner Stop at a one-shot entry: pick up intents; if stopped, cancel resting entries.

    Returns True when Stop is in force, and the caller must then exit without
    doing any work. `broker_factory` is called ONLY when stopped, so a stopped
    process constructs nothing but the broker it cancels through; the cancel is
    the Freeze sweep (protective stops and exits are kept).
    """
    from src.sentinel.guarded import NO_LEDGER, record_guarded_pass

    if not db_path:
        return False
    try:
        intake(db_path)
    except Exception as exc:  # noqa: BLE001 - recorded; a Stop already acted is still read below
        record_guarded_pass(NO_LEDGER, "owner_intents.stop_pickup", exc, log=logger)
    if not stop_in_force(db_path):
        return False
    broker = None
    try:
        broker = broker_factory()
        result = broker.sweep_frozen_resting_orders(db_path)
        logger.warning("owner Stop in force: desk off; resting entries swept (%s)", result)
        _mark_stop_swept(db_path)
    except Exception as exc:  # noqa: BLE001 - recorded; Stop still holds and the next pickup retries
        record_guarded_pass(broker or NO_LEDGER, "owner_intents.stop_sweep", exc, log=logger)
    return True


def _swept_file(db_path) -> str:
    return f"{db_path}.stop_swept"


def _latest_stop_id(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT MAX(id) FROM owner_intents WHERE state='acted' AND action=?", (STOP,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _mark_stop_swept(db_path) -> None:
    """Remember which acted Stop the resting-entry sweep has been done for."""
    try:
        stop_id = _latest_stop_id(db_path)
        with open(_swept_file(db_path), "w") as fh:
            fh.write(str(stop_id))
    except Exception as exc:  # noqa: BLE001 - recorded; the sweep is simply owed again
        from src.sentinel.guarded import NO_LEDGER, record_guarded_pass

        record_guarded_pass(NO_LEDGER, "owner_intents.stop_swept_mark", exc, log=logger)


def stop_sweep_owed(db_path) -> bool:
    """True until a resting-entry sweep has succeeded for the latest acted Stop.

    The owner Stop gate (src/owner_stop_gate.py) holds no broker credentials,
    so it cannot sweep; it reads this to let one credentialed session unit
    through to `honour_stop`, which sweeps, marks, and exits without work.
    A missing or unreadable mark means owed: an extra sweep is harmless, a
    missed one leaves entries resting at the broker.
    """
    stop_id = _latest_stop_id(db_path)
    if stop_id is None:
        return False
    try:
        with open(_swept_file(db_path)) as fh:
            return fh.read().strip() != str(stop_id)
    except OSError:
        return True
