"""Board item 186 — the realised portfolio at-risk recording.

Table, writer and payload shaping all live here, together, so a new recording
adds no mass to `src/storage/db.py` or to the schema manager (both are under
the rebuild's shrink-only size ratchet). `src.number_sources._DB_SOURCE_PATHS`
names this file, the same way the trades write path was named when it was
lifted out, so the settlement-route guard still sees the fields it writes.

WHY IT EXISTS. `max_portfolio_risk_pct` (25) is owner-ratified appetite with
no measurement behind it. The owner removed the correlated-cluster limits on
15 Oct 2026; the `cluster_share_pct` and `cluster_shares_json` columns stay in
the live table for old rows but are no longer written.

RECORDING ONLY. Nothing may read these rows back into a sizing, ordering or
refusal decision, and they may NEVER be swept for the ceiling that would have
performed best — that is fitting a number to this desk's own record, which
`docs/OUTCOME.md` bars.

UNIT: percent of equity AT RISK (per-name loss-if-stopped / equity), not
notional weight.

UNKNOWN STAYS NULL: `allocation is None` means the held book's risk could not
be read and the allocator never ran — the state in which the ceiling goes
UNENFORCED — which is not the same fact as a book carrying no risk. Every
measurement is then None and `allocator_ran` is 0.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
from datetime import datetime
from src.trading_calendar import utc_now

from src.util.time import UTC, et_today

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS realised_risk_budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    run_id TEXT,
    session_date TEXT,
    allocator_ran INTEGER NOT NULL,
    equity REAL,
    committed_pct REAL,
    ceiling_pct REAL,
    cluster_share_pct REAL,
    held_only_pct REAL,
    cluster_shares_json TEXT,
    grants_json TEXT,
    rationed_names INTEGER,
    UNIQUE (run_id)
)
"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_realised_risk_budget_date ON realised_risk_budget (session_date)"


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _held_only_pct(committed, granted_total):
    """At-risk carried by HELD names that no new request touched.

    The allocator documents `committed_pct` as "held + granted", so this is
    exactly `committed - sum(granted)` and nothing is invented. It matters
    because a row showing only this session's grants would read as a
    near-empty budget while the ceiling was in fact already spent.
    """
    if committed is None or granted_total is None:
        return None
    return round(committed - granted_total, 6)


def shape_risk_budget_row(*, allocation, equity) -> dict:
    """Pure. Turn the allocator's output into the values the row stores."""
    ran = allocation is not None
    row = {
        "allocator_ran": 1 if ran else 0,
        "equity": _num(equity),
        "held_only_pct": None,
        "committed_pct": None,
        "ceiling_pct": None,
        "grants_json": None,
        "rationed_names": None,
    }
    if not ran:
        return row
    committed = _num(getattr(allocation, "committed_pct", None))
    row["committed_pct"] = committed
    row["ceiling_pct"] = _num(getattr(allocation, "ceiling_pct", None))
    grants = []
    rationed = 0
    for sym, g in sorted((getattr(allocation, "grants", None) or {}).items()):
        limited_by = getattr(g, "limited_by", None)
        if limited_by:
            rationed += 1
        grants.append(
            {
                "symbol": str(sym),
                "requested_pct": _num(getattr(g, "requested_pct", None)),
                "granted_pct": _num(getattr(g, "granted_pct", None)),
                "limited_by": limited_by,
            }
        )
    row["grants_json"] = json.dumps(grants)
    row["rationed_names"] = rationed
    row["held_only_pct"] = _held_only_pct(
        committed,
        sum(g["granted_pct"] or 0.0 for g in grants),
    )
    return row


def ensure_table(conn) -> None:
    """Idempotent. The DDL lives with its writer, not in the schema manager."""
    conn.execute(_DDL)
    conn.execute(_INDEX)


def record_realised_risk_budget(
    db,
    *,
    allocation,
    equity,
    run_id: str | None = None,
    session_date: str | None = None,
) -> bool:
    """Write one row for this run. Idempotent per run (UNIQUE on `run_id`).

    Takes the allocator's OWN output, so the figures are what rationed the
    orders and cannot disagree with the sizing they describe. Never raises:
    a recording that failed must not stop a session.
    """
    row = shape_risk_budget_row(
        allocation=allocation,
        equity=equity,
    )
    try:
        lock = getattr(db, "_lock", None) or contextlib.nullcontext()
        with lock:
            ensure_table(db.conn)
            db.conn.execute(
                "INSERT OR REPLACE INTO realised_risk_budget ("
                "  timestamp, run_id, session_date, allocator_ran,"
                "  equity, committed_pct, ceiling_pct,"
                "  held_only_pct, grants_json,"
                "  rationed_names"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    utc_now().isoformat(sep=" ", timespec="seconds"),
                    run_id or None,
                    session_date or str(et_today()),
                    row["allocator_ran"],
                    row["equity"],
                    row["committed_pct"],
                    row["ceiling_pct"],
                    row["held_only_pct"],
                    row["grants_json"],
                    row["rationed_names"],
                ),
            )
            db.conn.commit()
        return True
    except Exception as e:  # noqa: BLE001 — a recording never blocks a trade
        logger.warning(
            "realised risk budget for run %s was not recorded (%s)",
            run_id,
            e,
        )
        return False
