"""The conviction ledger store — seat credits, scored on close.

A STANDALONE store, not a mixin: built by `build_conviction_ledger_store`
from plain collaborators (an open sqlite connection and the process
write-lock), reaching back into nothing. Lifted whole out of
`src.storage.db`; no threshold, ordering or refusal in it was altered.

READ vs WRITE stays distinguishable: the two `get`/`_scored` reads take
the plain lock and never commit, and the one write path goes through
`_insert_evidence` under the retrying lock.
"""

import logging
import sqlite3
import threading

from src.storage.locked_write import locked_write
from src.storage.seat_stances import read_seat_stances

logger = logging.getLogger(__name__)

CONVICTION_CREDIT_KIND = "conviction_credit"


def void_conviction_credit(conn: sqlite3.Connection, position_ids) -> int:
    """Delete the conviction credit of positions whose grouping the fill walk just changed.

    Credit is derived, never stored truth: `TradeLedger._regroup_position_ids_locked`
    calls this for every position id whose membership it changed, and the evening
    `resolve_conviction_ledger` re-scores those positions from the new grouping, so no
    stored score disagrees with the walk. Caller holds the ledger lock; no commit.
    """
    ids = sorted({str(p) for p in position_ids if p})
    if not ids:
        return 0
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'specialist_evidence'").fetchone()
    if table is None:  # a ledger-only database: no credit exists to void
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        "DELETE FROM specialist_evidence WHERE kind = ? "  # noqa: S608 - placeholders only
        f"AND json_extract(evidence_json, '$.position_id') IN ({marks})",
        (CONVICTION_CREDIT_KIND, *ids),
    )
    return cur.rowcount or 0


def _fill_walk_chains(rows) -> tuple[dict[str, list[dict]], set[str]]:
    """Position chains in the fill walk's grouping and event order, plus the ids left unscored.

    Stored ids ARE the walk's grouping: they are re-derived on every fill confirmation
    (`TradeLedger._regroup_position_ids_locked`, which voids the credit of any position it
    changes). A symbol whose live position's fill order is unknown has no walk to agree
    with, so none of its positions is scored; nor is a position before the last flat
    point the walk can order. Their ids come back in the second value.
    """
    from src.storage.trades.position_chain import _event_split

    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(dict(row))
    chains: dict[str, list[dict]] = {}
    unordered: set[str] = set()
    for symbol_rows in by_symbol.values():
        split = _event_split(symbol_rows)
        if split is None:
            unordered.update(r["position_id"] for r in symbol_rows if r["position_id"])
            continue
        early_ids = {r["position_id"] for r in split[0] if r["position_id"]}
        unordered.update(early_ids)
        for row in split[1]:
            if row["position_id"] and row["position_id"] not in early_ids:
                chains.setdefault(row["position_id"], []).append(row)
    return chains, unordered


class ConvictionLedgerStore:
    """Persisted per-seat credit rows and the score-on-close pass."""

    CONVICTION_CREDIT_KIND = CONVICTION_CREDIT_KIND

    def __init__(self, *, conn: sqlite3.Connection, lock: threading.Lock):
        self.conn = conn
        self._lock = lock

    def _insert_evidence(
        self,
        *,
        run_id: str,
        agent_name: str,
        kind: str,
        scope: str,
        evidence_json: str,
        symbol: str | None = None,
        decision_id: str | None = None,
    ) -> int:
        """The one WRITE path, under the shared retrying process lock."""

        def _do():
            cur = self.conn.execute(
                "INSERT INTO specialist_evidence "
                "(run_id, decision_id, agent_name, kind, scope, symbol, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, decision_id, agent_name, kind, scope, symbol, evidence_json),
            )
            self.conn.commit()
            return cur.lastrowid or 0

        return locked_write(self._lock, _do, label="insert_conviction_credit")

    def get_conviction_credits(
        self,
        *,
        seat: str | None = None,
        limit: int | None = None,
    ) -> list:
        """Every persisted `SeatCredit`, oldest first. Read back, not recomputed.

        This is what makes the ledger cheap to display: scoring happens once,
        when a position closes, and the aggregate
        (`src.conviction_ledger.aggregate_seat_records`) is pure arithmetic
        over these rows.

        **The one thing that IS derived here, and why.** Credit rows written
        before 2026-08-31 stored a conviction-WEIGHTED `credit` (R x 1.0/0.6/
        0.3 by declared confidence); rows written after store raw signed R,
        the weight having been removed by owner decision. Rather than migrate
        or mix the two, `credit` is recomputed on every read from the stored
        `r_multiple` and `side`, which is exact and lossless: `r_multiple` was
        always persisted unweighted and `side` says which way to sign it. Old
        and new rows therefore mean the same thing, no stored row is rewritten
        and no reader ever sees a weighted figure. The stored `credit` and the
        historical `weight` key are deliberately ignored.
        """
        from src.conviction_ledger import SeatCredit

        sql = "SELECT evidence_json FROM specialist_evidence WHERE kind = ?"
        params: list = [CONVICTION_CREDIT_KIND]
        if seat:
            sql += " AND agent_name = ?"
            params.append(str(seat).strip().lower())
        sql += " ORDER BY timestamp, id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        import json as _json

        out = []
        for row in rows:
            try:
                data = _json.loads(row["evidence_json"])
                r = float(data["r_multiple"])
                out.append(
                    SeatCredit(
                        seat=data["seat"],
                        symbol=data["symbol"],
                        side=data["side"],
                        stance=data.get("stance", ""),
                        conviction=data.get("conviction", "medium"),
                        r_multiple=r,
                        credit=round(r if data["side"] == "supported" else -r, 4),
                        resolved_at=data.get("resolved_at", ""),
                        position_id=data.get("position_id"),
                        decision_id=data.get("decision_id"),
                        direction=data.get("direction", "long"),
                        nominated=bool(data.get("nominated")),
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Skipping malformed conviction_credit row: %s", e)
        return out

    def _scored_position_ids(self) -> set[str]:
        """position_ids that already carry credit rows — the idempotency key."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT json_extract(evidence_json, '$.position_id') AS pid "
                "FROM specialist_evidence WHERE kind = ?",
                (CONVICTION_CREDIT_KIND,),
            ).fetchall()
        return {r["pid"] for r in rows if r["pid"]}

    def resolve_conviction_ledger(self) -> dict:
        """Score every newly-closed position into per-seat credit rows.

        Spec §9.5 "score on close". For each `position_id` chain that has
        gone flat and has not been scored before:

          1. reduce it to a round trip (`summarize_closed_position`),
          2. compute its realized R per lot, as that round trip reports it
             (`ClosedPosition.r_multiple`), against
             each lot's own opening stop,
          3. credit every seat that took a side at decision time
             (`score_position`) — raw signed R, unweighted,
          4. persist one `conviction_credit` evidence row per seat.

        Long and short chains are handled by exactly the same path. A
        profitable short arrives from `r_multiple` as a POSITIVE R (the
        round trip carries a negative qty, which is the only place direction
        appears) and its supporters are credited positively, identically to a
        profitable long. Nothing is inverted for direction.

        Idempotent by construction: a position whose id already appears in a
        credit row is skipped, so this is safe to run every evening.

        Never guesses. A chain with no entry stop on record has no honest
        R-multiple denominator and is counted in `skipped_no_r` rather than
        scored; a chain whose decision recorded no seat stances is counted in
        `skipped_no_stances`. Neither is retried into a fabricated number.

        Returns a counters dict — `{"closed_positions", "scored_positions",
        "credits_written", "skipped_already_scored", "skipped_no_r",
        "skipped_no_stances"}`. Purely observational: nothing in the trading
        chain reads this method or the rows it writes.
        """
        import json as _json
        from src.conviction_ledger import score_position
        from src.quantities import summarize_closed_position

        with self._lock:
            rows = self.conn.execute(
                "SELECT id, position_id, symbol, action, qty, price, fill_qty, "
                "fill_price, fill_status, filled_at, stop_loss, initial_stop_loss, decision_id, run_id, "
                "timestamp FROM trades ORDER BY symbol, id",
            ).fetchall()
        chains, unordered = _fill_walk_chains(rows)

        already = self._scored_position_ids()
        counters = {
            "closed_positions": 0,
            "scored_positions": 0,
            "credits_written": 0,
            "skipped_already_scored": 0,
            "skipped_no_r": 0,
            "skipped_no_stances": 0,
            "skipped_fill_time_missing": len(unordered),
        }
        for position_id, chain in chains.items():
            closed = summarize_closed_position(chain)
            if closed is None:
                continue  # still open, or never opened — not an outcome yet
            counters["closed_positions"] += 1
            if position_id in already:
                counters["skipped_already_scored"] += 1
                continue
            r = closed.r_multiple
            if r is None:
                counters["skipped_no_r"] += 1
                continue
            stances = read_seat_stances(
                self.conn,
                self._lock,
                decision_id=closed.decision_id or "",
                symbol=closed.symbol,
            )
            if not stances:
                counters["skipped_no_stances"] += 1
                continue
            credits = score_position(
                symbol=closed.symbol,
                direction=closed.direction,
                r_multiple=r,
                stances=stances,
                position_id=position_id,
                decision_id=closed.decision_id,
                resolved_at=closed.closed_at,
            )
            if not credits:
                counters["skipped_no_stances"] += 1
                continue
            run_id = (
                next(
                    (str(row.get("run_id") or "") for row in chain if row.get("run_id")),
                    "",
                )
                or f"ledger-{position_id}"
            )
            for credit in credits:
                self._insert_evidence(
                    run_id=run_id,
                    decision_id=credit.decision_id,
                    agent_name=credit.seat,
                    kind=CONVICTION_CREDIT_KIND,
                    scope="symbol",
                    symbol=credit.symbol,
                    evidence_json=_json.dumps(
                        {
                            "seat": credit.seat,
                            "symbol": credit.symbol,
                            "side": credit.side,
                            "stance": credit.stance,
                            # `conviction` is recorded, never applied — no
                            # `weight` key is written any more (2026-08-31).
                            "conviction": credit.conviction,
                            "r_multiple": credit.r_multiple,
                            "credit": credit.credit,
                            "resolved_at": credit.resolved_at,
                            "position_id": credit.position_id,
                            "decision_id": credit.decision_id,
                            "direction": credit.direction,
                            "nominated": credit.nominated,
                        },
                        sort_keys=True,
                    ),
                )
                counters["credits_written"] += 1
            counters["scored_positions"] += 1
        return counters


def build_conviction_ledger_store(
    *,
    conn: sqlite3.Connection,
    lock: threading.Lock,
) -> ConvictionLedgerStore:
    """Build the store from its collaborators BY VALUE."""
    return ConvictionLedgerStore(conn=conn, lock=lock)
