"""Realised `(sector, side)` weight recording (board item 224), moved out of
`src/storage/db.py` verbatim so the row writer can be exercised against a stub
(`conn`, `_lock`, `_sqlite_utc_timestamp`, `REALISED_SECTOR_WEIGHT_DENOMINATOR`)
without building `Database`. RECORDING ONLY; see the function's docstring.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from src.trading_calendar import utc_now

from src.util.time import UTC, et_today

logger = logging.getLogger(__name__)


def record_realised_sector_weights(
    self,
    *,
    decisions,
    sectors,
    total_value,
    run_id: str | None = None,
    session_date: str | None = None,
) -> bool:
    """Record the REALISED `(sector, side)` weights of one run's orders.

    ITEM 224 RECORDING, RECORDING ONLY, and it decides nothing. Read the
    `realised_sector_weights` note in `_migrate` for why it exists (the
    pre-decision projection was removed as undeliverable) and for the
    hard limit on its use: nothing may read it back into a sizing,
    ordering or refusal decision, and it may NEVER be swept for the
    sector cap that would have performed best.

    `decisions` is the FINISHED order list the constructor returned, so
    the figures are what was built and not what was hoped for; `sectors`
    is the constructor's own `last_order_sectors`, the sector it already
    resolved while sizing, so this write buys no market data and cannot
    disagree with the sizing it describes.

    Unknown sector stays NULL inside the JSON, never an "other" bucket.
    New recordings never have a NULL `weights_json`. A run that built no
    entry or reducing orders writes `[]`; a historical NULL remains unknown.

    Idempotent per run (UNIQUE on `run_id`).
    """
    sectors = sectors or {}
    entries: list = []
    reducing = 0
    reducers: list = []
    for d in decisions or ():
        action = getattr(d, "action", None)
        if action in ("BUY", "SHORT"):
            entries.append(d)
        elif action in ("SELL", "COVER"):
            reducing += 1
            reducers.append(d)

    def _num(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    buckets: dict[tuple[str | None, str], dict] = {}
    unknown_orders = 0
    for d in entries:
        sym = getattr(d, "symbol", None)
        sector = sectors.get(sym)
        sector = (sector or None) if isinstance(sector, str) else None
        if sector is None:
            unknown_orders += 1
        side = "short" if getattr(d, "action", None) == "SHORT" else "long"
        key = (sector, side)
        slot = buckets.setdefault(
            key,
            {"sector": sector, "side": side, "weight_pct": 0.0, "orders": 0},
        )
        slot["orders"] += 1
        w = _num(getattr(d, "allocation_pct", None))
        if w is not None:
            slot["weight_pct"] += w
    # Reducing orders (SELL closes a long, COVER closes a short) are
    # recorded under kind "reduce" so a reduce-only session is not an
    # empty row; entry buckets are unchanged and carry no "kind".
    for d in reducers:
        sym = getattr(d, "symbol", None)
        sector = sectors.get(sym)
        sector = (sector or None) if isinstance(sector, str) else None
        side = "short" if getattr(d, "action", None) == "COVER" else "long"
        slot = buckets.setdefault(
            (sector, side, "reduce"),
            {"sector": sector, "side": side, "kind": "reduce", "weight_pct": 0.0, "orders": 0},
        )
        slot["orders"] += 1
        w = _num(getattr(d, "allocation_pct", None))
        if w is not None:
            slot["weight_pct"] += w
    rows = sorted(
        buckets.values(),
        key=lambda r: (r["sector"] or "", r["side"], r.get("kind", "")),
    )
    for r in rows:
        r["weight_pct"] = round(r["weight_pct"], 6)
    payload = json.dumps(rows)  # never NULL: [] means no orders of either kind
    try:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO realised_sector_weights ("
                "  timestamp, run_id, session_date, weights_json,"
                "  denominator, total_value, entry_orders_built,"
                "  reducing_orders_built, unknown_sector_orders"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self._sqlite_utc_timestamp(utc_now()),
                    run_id or None,
                    session_date or str(et_today()),
                    payload,
                    self.REALISED_SECTOR_WEIGHT_DENOMINATOR,
                    _num(total_value),
                    len(entries),
                    reducing,
                    unknown_orders,
                ),
            )
            self.conn.commit()
        return True
    except Exception as e:  # noqa: BLE001 — a recording never blocks a trade
        logger.warning(
            "realised sector weights for run %s were not recorded (%s)",
            run_id,
            e,
        )
        return False
