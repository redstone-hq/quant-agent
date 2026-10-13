"""TradeLedger backfills bodies, lifted VERBATIM from ledger.py.

`ledger` is the TradeLedger (`self` before the move).
"""

from __future__ import annotations

from src.storage.analytics.calibration import _POSITION_OPEN_ACTIONS
from src.storage.trades.position_chain import (
    _assign_position_ids,
    _event_order,
    _is_position_exit_action,
)
from src.storage.trades.exit_reasons import (
    _find_pm_target_for_symbol,
    _resolve_decision_id_status,
)


def backfill_position_ids(ledger, *, dry_run: bool = False) -> dict:
    """One-time reconstruction of `position_id` chains for trades rows
    written before this column existed (Phase 6, §6.2a).

    Uses the exact same FIFO logic `_assign_position_ids` derives
    (matched by symbol, oldest-first, a BUY or a SHORT opens/adds, a
    recognized exit-family action for that side reduces — mirroring the
    accounting `compute_trade_calibration` already trusts for win-rate /
    avg-hold-days) so the backfilled chains never disagree with those
    numbers. Re-running this after 2026-08-31 assigns chains to short
    history that could not receive one before; a row that already carries
    an id is still never reassigned.

    Never guesses: a row this can't confidently attach to an open chain
    — typically the ledger's very first record for a symbol whose real
    position predates this system (a SELL/exit with no prior BUY on
    record), or a stray exit after the book had already gone flat — is
    left NULL rather than assigned a fabricated chain.

    Idempotent and safe to run against a database where live trading has
    already assigned SOME rows (because this migration shipped and
    started minting ids for new trades before the backfill got run
    against older history): a row that already carries a position_id is
    never reassigned, and the chain state used to fill in the gaps
    around it treats that id as ground truth.

    `dry_run=True` computes and reports without writing anything.

    Returns:
        {"total": int,            # every trades row in the database
         "already_assigned": int, # had a position_id before this ran
         "assigned": int,         # newly assigned by this run
         "left_null_ambiguous": int,  # BUY/exit-family, but no chain
                                       # to confidently attach to
         "not_applicable": int}   # HOLD / SWEEP_BUY / SWEEP_SELL /
                                   # anything not part of a position
                                   # chain by design, not by ambiguity
    """

    def _do():
        rows = ledger.conn.execute(
            "SELECT id, symbol, action, qty, fill_qty, fill_status, "
            "position_id, filled_at, timestamp FROM trades ORDER BY symbol, timestamp, id"
        ).fetchall()
        rows = [dict(r) for r in rows]
        by_symbol: dict[str, list[dict]] = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], []).append(r)

        total = len(rows)
        already_assigned = sum(1 for r in rows if r.get("position_id"))
        assigned = 0
        left_null_ambiguous = 0
        not_applicable = 0
        updates: list[tuple[str, int]] = []

        for symbol_rows in by_symbol.values():
            # The fill walk's event order; legacy (timestamp, id) when it is unknown.
            new_assignments = _assign_position_ids(_event_order(symbol_rows) or symbol_rows)
            for r in symbol_rows:
                if r.get("position_id"):
                    continue  # a NULL backfill; set ids are re-derived on fill confirmation
                action = (r.get("action") or "").upper()
                is_positionable = action in _POSITION_OPEN_ACTIONS or _is_position_exit_action(action)
                new_id = new_assignments.get(r["id"])
                if new_id:
                    assigned += 1
                    updates.append((new_id, r["id"]))
                elif is_positionable:
                    left_null_ambiguous += 1
                else:
                    not_applicable += 1

        if not dry_run and updates:
            ledger.conn.executemany(
                "UPDATE trades SET position_id = ? WHERE id = ?",
                updates,
            )
            ledger.conn.commit()
        return {
            "total": total,
            "already_assigned": already_assigned,
            "assigned": assigned,
            "left_null_ambiguous": left_null_ambiguous,
            "not_applicable": not_applicable,
        }

    return ledger._locked_write(_do, label="backfill_position_ids")


def backfill_conviction_ledger(ledger, *, dry_run: bool = False) -> dict:
    """One-time reconstruction of the conviction-ledger columns (spec
    §7.2) for `trades` rows written before they existed.

    Two INDEPENDENT repairs, run together because both read `trades` in
    one pass (mirrors `backfill_position_ids`'s shape and safety
    posture — dry-run by default, idempotent, never guesses):

    1. `decision_id_status` on every exit-family row (see
       `_is_exit_family_for_decision_linking`) that predates the
       column. This is NEVER ambiguous, unlike `position_id`'s
       `left_null_ambiguous` case: every `insert_trade` / `insert_
       stop_out_trade` call site in this codebase is enumerated, and an
       exit row's `decision_id` is NULL if and only if the code path
       that wrote it never had one to attach. So every eligible row
       gets EITHER 'linked' or 'no_originating_decision' — there is no
       third "can't tell" bucket the way position_id has.

    2. `conviction` / `requested_risk_pct` / `decision_model` on BUY/
       SHORT rows that already carry a real `decision_id`: recovered by
       joining `agent_logs` (agent_name='portfolio_manager', matching
       decision_id) and reading the `targets` entry matching this
       trade's symbol out of `full_response` (see
       `_find_pm_target_for_symbol` — handles both the fenced-```json
       and raw-JSON formats seen in real history).

       `allocated_risk_pct` (the POST-clamp figure the constructor's
       RiskPlan actually granted) is DELIBERATELY NEVER backfilled —
       it was never persisted anywhere retroactively readable (only
       the PM's pre-clamp ask survives, inside `full_response`), and
       reconstructing the granted figure would mean re-running the
       constructor's budget rationing against point-in-time book state
       this database does not fully preserve. Every backfilled row
       gets `allocated_risk_pct = NULL`, always, and the returned dict
       reports that as `allocated_risk_pct_recoverable: 0` rather than
       letting a caller assume the gap was closed.

    Idempotent: an exit row is only touched while `decision_id_status
    IS NULL`; an entry row only while `conviction IS NULL AND
    decision_model IS NULL` (a row already touched by this backfill,
    or by live trading after this column existed, is never
    reprocessed). `dry_run=True` (default) computes and returns counts
    without writing.
    """

    def _do():
        # ---- 1. exit rows: decision_id_status (fully recoverable) ----
        exit_rows = ledger.conn.execute(
            "SELECT id, action, decision_id FROM trades WHERE decision_id_status IS NULL",
        ).fetchall()
        exit_updates: list[tuple[str, int]] = []
        exit_linked = 0
        exit_no_originating_decision = 0
        exit_not_applicable = 0
        for r in exit_rows:
            status = _resolve_decision_id_status(r["action"], r["decision_id"])
            if status is None:
                exit_not_applicable += 1
                continue
            exit_updates.append((status, r["id"]))
            if status == "linked":
                exit_linked += 1
            else:
                exit_no_originating_decision += 1

        # ---- 2. entry rows: conviction / requested_risk_pct / decision_model ----
        entry_rows = ledger.conn.execute(
            "SELECT id, symbol, decision_id FROM trades "
            "WHERE action IN ('BUY', 'SHORT') AND decision_id IS NOT NULL "
            "AND conviction IS NULL AND decision_model IS NULL",
        ).fetchall()
        decision_ids = sorted({r["decision_id"] for r in entry_rows if r["decision_id"]})
        pm_logs: dict[str, dict] = {}
        if decision_ids:
            placeholders = ",".join("?" for _ in decision_ids)
            for row in ledger.conn.execute(
                "SELECT decision_id, model, full_response FROM agent_logs "
                f"WHERE agent_name = 'portfolio_manager' AND decision_id IN ({placeholders})",
                tuple(decision_ids),
            ).fetchall():
                # First row wins on a duplicate decision_id (retries are
                # not expected to share an id, but never overwrite a
                # resolved match with a later, possibly-unrelated one).
                pm_logs.setdefault(row["decision_id"], dict(row))

        entry_updates: list[tuple] = []  # (conviction, requested_risk_pct, decision_model, id)
        entry_recovered = 0
        entry_unrecoverable_no_agent_log = 0
        entry_unrecoverable_no_matching_target = 0
        for r in entry_rows:
            log_row = pm_logs.get(r["decision_id"])
            if log_row is None:
                entry_unrecoverable_no_agent_log += 1
                continue
            target = _find_pm_target_for_symbol(log_row.get("full_response"), r["symbol"])
            if target is None:
                entry_unrecoverable_no_matching_target += 1
                continue
            entry_updates.append(
                (
                    target.get("conviction"),
                    target.get("risk_allocation_pct"),
                    log_row.get("model"),
                    r["id"],
                )
            )
            entry_recovered += 1

        if not dry_run:
            if exit_updates:
                ledger.conn.executemany(
                    "UPDATE trades SET decision_id_status = ? WHERE id = ?",
                    exit_updates,
                )
            if entry_updates:
                ledger.conn.executemany(
                    "UPDATE trades SET conviction = ?, requested_risk_pct = ?, decision_model = ? WHERE id = ?",
                    entry_updates,
                )
            ledger.conn.commit()

        return {
            "exit_rows_considered": len(exit_rows),
            "exit_linked": exit_linked,
            "exit_no_originating_decision": exit_no_originating_decision,
            "exit_not_applicable": exit_not_applicable,
            "entry_rows_considered": len(entry_rows),
            "entry_recovered": entry_recovered,
            "entry_unrecoverable_no_agent_log": entry_unrecoverable_no_agent_log,
            "entry_unrecoverable_no_matching_target": entry_unrecoverable_no_matching_target,
            # Always 0 — see docstring. Never silently "improves" as a
            # side effect of a future change without this comment being
            # revisited: allocated_risk_pct becoming recoverable would
            # require a NEW data source, not a smarter backfill.
            "allocated_risk_pct_recoverable": 0,
        }

    return ledger._locked_write(_do, label="backfill_conviction_ledger")
