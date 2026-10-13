"""The most recent reconciliation result, per reconciler kind.

Why: docs/FUTURE.md's heartbeat carries "last reconciliation", and until now
the reconcilers wrote back fixes but kept no record of WHEN they last ran or
WHETHER broker and ledger agreed. One row per run is appended; `status()`
reports 'agreed', 'disagreed' or 'not_run' -- the third is never collapsed
into either of the others. Nothing in the desk reads this yet.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from src.trading_calendar import sql_utc_stamp

logger = logging.getLogger(__name__)

AGREED = "agreed"
DISAGREED = "disagreed"
NOT_RUN = "not_run"


class ReconciliationLog:
    """Writer and reader for the `reconciliation_runs` table on one sqlite connection."""

    def __init__(self, *, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, *, kind: str, agreed: bool, detail: str = "", run_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO reconciliation_runs (ran_at, kind, agreed, detail, run_id) VALUES (?, ?, ?, ?, ?)",
            (sql_utc_stamp(), kind, 1 if agreed else 0, detail, run_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest(self, *, kind: str) -> dict | None:
        """The newest row for `kind`, or None when that reconciler has never run."""
        cur = self.conn.execute(
            "SELECT id, ran_at, kind, agreed, detail, run_id FROM reconciliation_runs"
            " WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (kind,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        out = dict(zip(cols, row))
        out["agreed"] = bool(out["agreed"])
        return out

    def status(self, *, kind: str) -> str:
        """'agreed' | 'disagreed' | 'not_run' -- three distinct answers, by design."""
        row = self.latest(kind=kind)
        if row is None:
            return NOT_RUN
        return AGREED if row["agreed"] else DISAGREED


def record_reconciliation(*, db, kind: str, result, run_id: str | None = None):
    """Hook for a reconciler's return site: record the outcome, hand `result` back unchanged.

    `result` is what the reconciler already returns -- a list of mismatches/
    gaps or a count of corrections. Empty or zero means broker and ledger
    agreed. `db` is whatever the caller holds; only a real sqlite connection
    on it is written to (the skip is logged, never hidden).
    """
    conn = getattr(db, "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        logger.debug("reconciliation %s not recorded: db has no sqlite connection", kind)
        return result
    agreed = not result
    try:
        detail = json.dumps(result, default=str) if not agreed else ""
        ReconciliationLog(conn=conn).record(kind=kind, agreed=agreed, detail=detail, run_id=run_id)
    except Exception:  # noqa: BLE001
        # An observer must never break the thing it observes. This call sits on
        # the return of the stop-coverage sweep, OUTSIDE that sweep's own try,
        # so a locked database or a missing table here would otherwise abort
        # protection itself. The failure is loud -- full traceback at error --
        # but it cannot take the money path down with it.
        logger.error("reconciliation %s could not be recorded", kind, exc_info=True)
    return result


def record_guarded_outcome(
    *,
    db,
    where: str,
    exc: BaseException | None = None,
    run_id: str | None = None,
    log=None,
    context: dict | None = None,
):
    """One counted row for ONE pass through a money-path catch-all.

    Why this exists: a broad ``except Exception`` on the trading path is
    there on purpose — removing it can turn a recoverable miss into a crash
    mid-order — but as written most of them record a one-line message with
    no traceback, so a programming error (a TypeError from an argument
    passed twice, an AttributeError from a renamed field) is indistinguishable
    from a quiet no-op. This makes the handler LOUD without changing what it
    does: full traceback at ERROR, plus one counted row on the EXISTING
    reconciliation channel. It never re-raises and never returns anything the
    caller branches on.

    Three states stay distinct, which is the whole point:
      * ``not_run``   — this site was never reached (no row at all)
      * ``agreed``    — it ran and swallowed nothing (``exc=None``)
      * ``disagreed`` — it ran and swallowed a fault (``exc`` set)

    `log` is the caller's module logger when given, so the traceback lands
    under the module that owns the handler rather than under this one, and
    `context` carries the per-name facts (symbol, row, order id) that the
    one-line log it replaces used to carry, into the counted row itself.
    """
    emitter = log or logger
    detail: list = []
    ctx = dict(context or {})
    if exc is not None:
        emitter.error(
            "money-path guard swallowed a fault at %s (%s): %s: %s",
            where,
            ctx,
            type(exc).__name__,
            exc,
            exc_info=exc,
        )
        detail = [{"where": where, "error": type(exc).__name__, "message": str(exc), **ctx}]
    record_reconciliation(db=db, kind=f"guarded:{where}", result=detail, run_id=run_id)
