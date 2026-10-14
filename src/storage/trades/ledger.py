"""Trade-ledger write path lifted VERBATIM from src/storage/db.py (db rebuild instalment 3).

Everything that writes or reads the `trades` table and its satellites
(trade_refusals, pending_protection_restores, protection_restore_wal_audit,
pending_repegs, positions): inserts, fill/submit state transitions, stop and
take-profit amendments, stop-out recording, excursion accumulation, the
restore/repeg recovery queues, pruning and the two backfills. Standalone:
collaborators are the open sqlite3 connection, the Database lock, the
locked-write runner and the three small SQL/time helpers, all keyword-only,
so it builds with no Database/TradingPipeline behind it
(tests/boundary_harness.py). Database keeps same-named thin shims that
construct this per call.

The module-level helpers (position-id assignment in position_chain.py;
exit-reason categorisation, decision-id resolution and PM-target extraction
in exit_reasons.py) are re-imported here, so `from src.storage.db import
_assign_position_ids` keeps working (one definition, there). The trades.action
vocabulary and the TRAIL_STOP fill rules are defined once in
src/trade_actions.py and re-exported here under their old private names.
The backfills, the restore/repeg recovery queues and the excursion/gap/
positions bodies live in backfills.py, recovery_queues.py and excursions.py;
the same-named methods below are thin shims over them.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from src.storage.analytics.calibration import _POSITION_OPEN_ACTIONS
from src.stop_price_classification import entry_stop_for_insert
from src.storage.trades import trade_refusals_store as _refusals_store
from src.storage.trades import backfills as _backfills
from src.storage.trades import recovery_queues as _recovery_queues
from src.storage.trades import excursions as _excursions
from src.trading_calendar import sql_utc_stamp, sql_utc_stamp_ago, utc_now
from src.util.time import UTC
from src.trade_actions import trail_stop_reduced_position as _trail_stop_reduced_position  # noqa: F401  re-export
from src.trade_actions import broker_fill_time
from src.storage.trades.position_chain import (  # noqa: F401  re-export; vocabulary defined in src/trade_actions.py
    _new_position_id,
    _LONG_EXIT_ACTIONS,
    _LONG_EXIT_PREFIXES,
    _SHORT_EXIT_ACTIONS,
    _SHORT_EXIT_PREFIXES,
    _EITHER_SIDE_EXIT_ACTIONS,
    _POSITION_EXIT_ACTIONS,
    _POSITION_EXIT_PREFIXES,
    _is_position_exit_action,
    _exit_action_side,
    _row_counts_as_executed,
    _assign_position_ids,
    _event_order,
    _event_split,
    _share_change,
)
from src.storage.trades.exit_reasons import (  # noqa: F401  re-export: defined there
    _EXIT_TRIGGER_CATEGORIES,
    _UNCATEGORISED_EXIT,
    _categorize_exit_reason,
    _NON_POSITIONAL_ACTIONS,
    _is_exit_family_for_decision_linking,
    _resolve_decision_id_status,
    _extract_pm_targets,
    _find_pm_target_for_symbol,
)

logger = logging.getLogger(__name__)


def _apply_opening_fill(inventory: float, average_price: float, signed_qty: float, price: float) -> tuple[float, float]:
    """Signed inventory and average entry price after a BUY (+qty) or SHORT (-qty) fill."""
    new_inventory = inventory + signed_qty
    if inventory * signed_qty > 1e-9:
        # Adding to the side already open: weighted average entry.
        return new_inventory, (abs(inventory) * average_price + abs(signed_qty) * price) / abs(new_inventory)
    if abs(new_inventory) <= 1e-9:
        return 0.0, 0.0
    if new_inventory * signed_qty > 0:
        # Opening from flat, or flipping through zero: what remains was opened at this fill.
        return new_inventory, price
    return new_inventory, average_price  # partly nets an open opposite side; its basis stands


def _closing_side(action: str, inventory: float) -> str | None:
    """Which side an exit closes: from its name, else from the position it closes."""
    if action.startswith("SWEEP_SELL"):
        return "long"
    side = _exit_action_side(action)
    if side is None:
        # Either-side exit (STOP_OUT, TRAIL_STOP, REDUCE, ...): whichever side is open.
        return "long" if inventory > 1e-9 else "short" if inventory < -1e-9 else None
    return side


class TradeLedger:
    """Trade-ledger write cluster; see module docstring."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        lock: threading.Lock,
        locked_write: Callable,
        executed_trade_predicate: Callable[[], str],
        sqlite_utc_timestamp: Callable[[datetime], str],
        et_day_utc_bounds: Callable[..., tuple[str, str]],
    ):
        self.conn = conn
        self._lock = lock
        self._locked_write = locked_write
        self._executed_trade_predicate = executed_trade_predicate
        self._sqlite_utc_timestamp = sqlite_utc_timestamp
        self._et_day_utc_bounds = et_day_utc_bounds

    def insert_trade(
        self,
        symbol: str,
        action: str,
        qty: float,
        price: float,
        reasoning: str,
        run_id: str,
        stop_loss: float = 0,
        take_profit: float = 0,
        broker_order_id: str | None = None,
        fill_status: str | None = None,
        decision_id: str | None = None,
        expected_horizon_sessions: int | None = None,
        setup_type: str | None = None,
        conviction: str | None = None,
        requested_risk_pct: float | None = None,
        allocated_risk_pct: float | None = None,
        decision_model: str | None = None,
        thesis_invalid_if: str | None = None,
        structural_ceiling: bool | None = None,
        entry_atr: float | None = None,
        stop_basis: str | None = None,
        stop_level_basis: str | None = None,
        filled_at: str | None = None,
    ) -> int:
        """Insert a trade record. Returns the new row's id.

        `filled_at` is the BROKER's own fill time (naive UTC) for a row written
        already executed; an executed share-moving insert is a fill
        confirmation, so the symbol's position ids are re-derived from the
        fill walk (`_regroup_position_ids_locked`).

        `entry_atr` / `stop_basis` are STOP-FLOOR EVIDENCE, pinned at entry
        only, and are recorded for one purpose: so a future pass can ask
        whether the ratified minimum stop width was ever VIOLATED in
        practice. See `_accumulate_excursions` for the MAE/MFE legs and
        for the explicit limits on what this data may be used for.


        `fill_status` semantics:
          - 'submitted'  — sent to broker, terminal status pending
          - 'filled'     — broker confirmed execution (full or partial)
          - 'canceled' / 'rejected' / 'expired' / 'done_for_day' — terminal broker
                           status; may still carry fill_qty/fill_price for partial fills
          - None         — legacy row or non-executed audit row (currently HOLD).
                           Legacy BUY/SELL rows still count as executed for back-compat;
                           synthetic HOLD rows are explicitly excluded from executed_only.

        `position_id`, `exit_reason_category`, and `decision_id_status`
        (Phase 6, §6.2a/e; conviction ledger §7.2) are ALWAYS derived here,
        never accepted as arguments — see `_resolve_new_row_position_id`,
        `_categorize_exit_reason`, and `_resolve_decision_id_status`. Every
        one of this method's ~12 call sites across the codebase gets the
        chain-linking, exit classification, and decision-link labelling for
        free with no change to the call.

        `conviction` / `requested_risk_pct` / `allocated_risk_pct` /
        `decision_model` are the conviction ledger (§7.2) — pinned at ENTRY
        only (see `TradeDecision` in models.py for what each figure means);
        every existing caller that never passes them gets None, which is
        correct for every non-entry row and every legacy caller.

        `thesis_invalid_if` mirrors that same entry-only pinning — see
        `TradeDecision.thesis_invalid_if` in models.py. None for every
        non-entry row, every legacy caller, and any entry whose target
        stated no falsifier condition.

        `structural_ceiling` (item 82) is the MEASURED half of construction's
        breakout verdict, pinned at ENTRY (BUY/SHORT) only — see
        `TradeDecision.structural_ceiling` in models.py. Stored as 0/1; None
        for every non-entry row and every legacy caller, so readers fall back
        to `setup_type` alone (the conservative side).
        """
        # 0/1 for storage, None stays NULL — see the column's migration note.
        structural_ceiling_stored = None if structural_ceiling is None else int(bool(structural_ceiling))

        def _do():
            position_id = self._resolve_new_row_position_id(
                symbol,
                action,
                qty=qty,
                fill_status=fill_status,
                fill_qty=None,
                filled_at=filled_at,
            )
            exit_category = _categorize_exit_reason(action, reasoning, fill_status, None)
            decision_link_status = _resolve_decision_id_status(action, decision_id)
            initial_stop_loss = entry_stop_for_insert(action, symbol, stop_loss, _POSITION_OPEN_ACTIONS)
            # Pin the entry target the same way, and for the same reason the
            # `initial_take_profit` migration note gives: `take_profit` is
            # mutable now that a structural event can trigger a
            # re-derivation, and progress/pace must keep measuring against
            # the yardstick the trade was opened on.
            try:
                target_at_insert = float(take_profit or 0)
            except (TypeError, ValueError):
                target_at_insert = 0.0
            initial_take_profit = target_at_insert if target_at_insert > 0 else None
            cur = self.conn.execute(
                "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, "
                "stop_loss, take_profit, broker_order_id, fill_status, decision_id, "
                "expected_horizon_sessions, setup_type, position_id, exit_reason_category, "
                "conviction, requested_risk_pct, allocated_risk_pct, decision_model, "
                "decision_id_status, thesis_invalid_if, initial_stop_loss, "
                "initial_take_profit, structural_ceiling, entry_atr, stop_basis, "
                "stop_level_basis, filled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol,
                    action,
                    qty,
                    price,
                    reasoning,
                    run_id,
                    stop_loss,
                    take_profit,
                    broker_order_id,
                    fill_status,
                    decision_id,
                    expected_horizon_sessions,
                    setup_type,
                    position_id,
                    exit_category,
                    conviction,
                    requested_risk_pct,
                    allocated_risk_pct,
                    decision_model,
                    decision_link_status,
                    thesis_invalid_if,
                    initial_stop_loss,
                    initial_take_profit,
                    structural_ceiling_stored,
                    entry_atr,
                    stop_basis,
                    stop_level_basis,
                    filled_at,
                ),
            )
            row_id = cur.lastrowid
            moved = {"action": action, "qty": qty, "fill_status": fill_status}
            if _row_counts_as_executed(action, fill_status, None) and _share_change(moved) is not None:
                self._regroup_position_ids_locked(symbol)
            self.conn.commit()
            return row_id

        return self._locked_write(_do, label="insert_trade")

    def insert_trade_refusal(self, **kwargs) -> int | None:
        """Lifted into `trade_refusals_store` (2026-10-05); see it for the column meanings."""
        return _refusals_store.insert(self, **kwargs)

    def get_trade_refusals(self, *, refusal: str | None = None, limit: int = 500) -> list[dict]:
        """Read back the durable refusal rows, newest first."""
        return _refusals_store.get_all(self, refusal=refusal, limit=limit)

    def update_open_stop_loss(
        self,
        symbol: str,
        new_stop_price: float,
        *,
        action: str | None = None,
    ) -> bool:
        """Write the live stop onto every opening row of this position.

        `action` is 'BUY' or 'SHORT' when the caller knows the side
        (repair, a short trail). Omitting it takes the most recent of
        either — only safe when a symbol cannot be both. Refuses an
        unknown action rather than defaulting to long.

        A scale-in leaves more than one opening row on the same
        `position_id`; the broker holds one consolidated stop, so every
        still-open row of that position is updated. Each row freezes its
        own `initial_stop_loss` if it had an entry stop; a row that opened
        with none does not mint one from the live level.
        """
        try:
            price = float(new_stop_price)
        except (TypeError, ValueError):
            return False
        if price != price or price <= 0:
            return False
        symbol_key = (symbol or "").strip()
        if not symbol_key:
            return False
        opening = (action or "").upper() or None
        if opening is not None and opening not in ("BUY", "SHORT"):
            logger.warning(
                "update_open_stop_loss: refusing unknown opening action %r for %s",
                action,
                symbol_key,
            )
            return False

        def _do():
            predicate = f"({self._executed_trade_predicate()} OR fill_status IN ('submitted', 'pending_submit'))"
            if opening:
                row = self.conn.execute(
                    "SELECT id, stop_loss, initial_stop_loss, position_id, action "
                    "FROM current_account_trades "
                    "WHERE symbol = ? AND action = ? "
                    f"AND {predicate} "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (symbol_key, opening),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT id, stop_loss, initial_stop_loss, position_id, action "
                    "FROM current_account_trades "
                    "WHERE symbol = ? AND action IN ('BUY', 'SHORT') "
                    f"AND {predicate} "
                    "ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (symbol_key,),
                ).fetchone()
            if row is None:
                logger.warning(
                    "update_open_stop_loss: no opening row for %s — live stop $%.4f was NOT recorded",
                    symbol_key,
                    price,
                )
                return False
            side = opening or (row["action"] if row["action"] in ("BUY", "SHORT") else None)
            position_id = row["position_id"]
            if position_id and side:
                self.conn.execute(
                    "UPDATE trades SET "
                    "stop_loss = ?, "
                    "initial_stop_loss = CASE "
                    "WHEN initial_stop_loss IS NOT NULL AND initial_stop_loss > 0 "
                    "THEN initial_stop_loss "
                    "WHEN stop_loss IS NOT NULL AND stop_loss > 0 THEN stop_loss "
                    "ELSE initial_stop_loss END "
                    f"WHERE position_id = ? AND action = ? AND {predicate}",
                    (price, position_id, side),
                )
            else:
                try:
                    current = float(row["stop_loss"] or 0)
                except (TypeError, ValueError):
                    current = 0.0
                try:
                    initial = float(row["initial_stop_loss"] or 0)
                except (TypeError, ValueError):
                    initial = 0.0
                frozen = initial if initial > 0 else (current if current > 0 else None)
                if frozen is None:
                    self.conn.execute(
                        "UPDATE trades SET stop_loss = ? WHERE id = ?",
                        (price, row["id"]),
                    )
                else:
                    self.conn.execute(
                        "UPDATE trades SET stop_loss = ?, initial_stop_loss = ? WHERE id = ?",
                        (price, frozen, row["id"]),
                    )
            self.conn.commit()
            return True

        return bool(self._locked_write(_do, label="update_open_stop_loss"))

    def _resolve_new_row_position_id(
        self,
        symbol: str,
        action: str,
        *,
        qty: float,
        fill_status: str | None,
        fill_qty: float | None,
        timestamp: str | None = None,
        filled_at: str | None = None,
    ) -> str | None:
        """Position-id for a row not yet inserted. Caller must already hold
        `self._lock` (called from inside a `_locked_write` closure).

        Replays `_assign_position_ids` - the fill walk - over this symbol's rows
        plus a placeholder for the new one, in event order (`_event_order`: a
        fill by its broker `filled_at`, anything else by when it was written),
        and returns what the placeholder was assigned. PROVISIONAL: a row's
        place in the fill sequence is known only once the broker says when it
        filled, so every fill confirmation re-derives the whole symbol
        (`_regroup_position_ids_locked`). Stored ids are never ground truth.
        When the fill order is unknown (an exit without a broker fill time) the
        rows are taken in `(timestamp, id)` order, as before, and the regroup
        leaves them alone - open R is unknown for the same reason.

        `timestamp` / `filled_at` come from callers that already know them
        (`insert_stop_out_trade`: the broker's fill time); otherwise the
        placeholder is stamped now and sorts after every row already written.

        The walk reads `current_account_trades`, so the account epoch is a
        forced flat point: a BUY on a new broker account never joins a
        position (or an unmatched lot) left on the old one.
        """
        rows = self.conn.execute(
            "SELECT id, action, qty, fill_qty, fill_status, filled_at, position_id, timestamp "
            "FROM current_account_trades WHERE symbol = ? ORDER BY timestamp, id",
            (symbol,),
        ).fetchall()
        history = [dict(r) for r in rows]
        new_row = float("inf")  # the placeholder's id: last among rows written in the same second
        placeholder = {
            "id": new_row,
            "action": action,
            "qty": qty,
            "fill_qty": fill_qty,
            "fill_status": fill_status,
            "filled_at": filled_at,
            "position_id": None,
            "timestamp": timestamp or sql_utc_stamp(),
        }
        ordered = _event_order([*history, placeholder])
        if ordered is None:
            ordered = sorted([*history, placeholder], key=lambda r: (str(r.get("timestamp") or ""), r["id"]))
        return _assign_position_ids(ordered).get(new_row)

    def _regroup_position_ids_locked(self, symbol: str) -> set[str] | None:
        """Re-derive `symbol`'s stored position ids from the fill walk. Caller holds `self._lock`; no commit.

        Runs on every fill confirmation (`insert_trade` of an executed fill,
        `update_trade_fill`, `insert_stop_out_trade`) and after the fill-time
        backfill, so every reader of `trades.position_id` (closed R, calibration,
        the scorecards, conviction credit, position history, excursions) reads
        the grouping the open-R walk uses: a fresh buy written before the old
        position's stop-out fill was known carried the old id, and moves to its
        own position here. Positions whose membership changed have their
        conviction credit voided, so the evening scorer re-scores them from the
        derived grouping. Rows before the last flat point the walk can order keep
        their ids. Returns those position ids, or None when the live position's
        fill order is unknown (ids left as they are).
        """
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, action, qty, fill_qty, fill_status, filled_at, position_id, timestamp "
                "FROM current_account_trades WHERE symbol = ? ORDER BY id",
                (symbol,),
            ).fetchall()
        ]
        split = _event_split(rows)
        if split is None:
            return None
        earlier, ordered = split
        # Rows before the last flat point the walk can order keep their ids; no later position reuses one.
        derived = _assign_position_ids(ordered, reserved={r["position_id"] for r in earlier if r.get("position_id")})
        changed = [(derived.get(r["id"]), r["id"], r.get("position_id")) for r in ordered]
        changed = [c for c in changed if c[0] != c[2]]
        if not changed:
            return set()
        self.conn.executemany("UPDATE trades SET position_id = ? WHERE id = ?", [(new, rid) for new, rid, _ in changed])
        affected = {pid for new, _, old in changed for pid in (new, old) if pid}
        from src.storage.conviction_ledger_store import void_conviction_credit

        void_conviction_credit(self.conn, affected)
        return affected

    def regroup_position_ids(self, symbol: str) -> set[str] | None:
        """Locked, committing `_regroup_position_ids_locked` (the fill-time backfill calls it)."""

        def _do():
            affected = self._regroup_position_ids_locked(symbol)
            self.conn.commit()
            return affected

        return self._locked_write(_do, label="regroup_position_ids")

    def backfill_fill_times(self, updates: list[tuple[int, str, str]]) -> set[str]:
        """Write broker fill times onto fills that have none, then re-derive the touched
        symbols' position ids, in ONE transaction through the desk's own write path.

        `_locked_write` is how every desk process serialises a write: the process lock,
        plus a bounded retry on SQLite's cross-process lock. A fill time already on a
        row is never overwritten. `updates` are `(row id, symbol, filled_at)`.
        """

        def _do():
            self.conn.executemany(
                "UPDATE trades SET filled_at = ? WHERE id = ? AND filled_at IS NULL",
                [(filled_at, row_id) for row_id, _, filled_at in updates],
            )
            affected: set[str] = set()
            for symbol in sorted({symbol for _, symbol, _ in updates}):
                affected |= self._regroup_position_ids_locked(symbol) or set()
            self.conn.commit()
            return affected

        return self._locked_write(_do, label="backfill_fill_times")

    def confirm_trade_submitted(
        self,
        row_id: int,
        broker_order_id: str | None,
    ) -> int:
        """Flip a pending_submit row to submitted after broker accepted.

        Part of the write-ahead-intent pattern for BUY submission (audit
        F4). The flow is:

            insert_trade(..., fill_status='pending_submit', broker_order_id=NULL)
            broker.submit_order(...)
            confirm_trade_submitted(row_id, broker_order_id)  ← this method

        On the crash window between submit_order returning and this call
        landing, the row stays as pending_submit with broker_order_id
        unset. Reconcile can detect orphans by (fill_status='pending_submit'
        AND broker_order_id IS NULL) and decide how to reconcile against
        the broker's order list.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET broker_order_id = ?, fill_status = 'submitted' WHERE id = ?",
                (broker_order_id, row_id),
            )
            self.conn.commit()
            return cur.rowcount

    def set_trade_client_order_id(self, row_id: int, client_order_id: str) -> int:
        """Record the idempotency key a pending_submit row's order is sent under.

        Written before the submit, so the orphan sweep can ask the broker for
        exactly this order when the submit's outcome is unknown."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET client_order_id = ? WHERE id = ?",
                (client_order_id, row_id),
            )
            self.conn.commit()
            return cur.rowcount

    def repoint_trade_broker_order_id(
        self,
        row_id: int,
        *,
        old_order_id: str,
        new_order_id: str,
    ) -> int:
        """Repoint a trade row at the order id an Alpaca replacement minted.

        A re-peg PATCHes a working entry limit; Alpaca answers by cancelling
        the old order and creating a NEW one with a NEW id. The old id is no
        longer authoritative — `_reconcile_fills` matches on
        `broker_order_id`, so leaving it stale makes reconciliation follow a
        dead order that will forever report status 'replaced' (a status the
        terminal sets do not cover) and conclude nothing ever happened.

        Guarded by `old_order_id` in the WHERE clause on purpose: this is
        called from a crash-recovery drain that may run twice on the same
        WAL row. Applying it a second time matches zero rows (the row now
        holds `new_order_id`) and returns 0 instead of clobbering a
        subsequent, newer re-peg's id. Idempotent by construction.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET broker_order_id = ? WHERE id = ? AND broker_order_id = ?",
                (new_order_id, row_id, old_order_id),
            )
            self.conn.commit()
            return cur.rowcount or 0

    def mark_trade_submit_failed(self, row_id: int) -> int:
        """Flag a pending_submit row as submit_failed.

        Used when broker.submit_order raised (broker may or may not have
        the order) OR when broker rejected the order (_order_accepted
        returned False). Distinct from rejected/canceled because those
        statuses imply the broker accepted then rejected; submit_failed
        means we don't know what the broker saw. Operator / reconcile
        sweeps these against the broker's order list by symbol + time.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET fill_status = 'submit_failed' WHERE id = ?",
                (row_id,),
            )
            self.conn.commit()
            return cur.rowcount

    def update_trade_fill(
        self,
        broker_order_id: str,
        fill_status: str,
        fill_qty: float | None = None,
        fill_price: float | None = None,
        filled_at: str | None = None,
    ) -> int:
        """Update a trade row's fill reconciliation after broker terminal status.

        Matches on broker_order_id. Returns row count updated. `filled_at` is
        the BROKER's own fill time (naive UTC) - never when we noticed; None
        (the broker reported none) keeps any time already recorded. Every
        confirmation re-derives the symbol's position ids from the fill walk.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE trades SET fill_status = ?, fill_qty = ?, fill_price = ?, "
                "fill_reconciled_at = ?, filled_at = COALESCE(?, filled_at) "
                "WHERE broker_order_id = ?",
                (fill_status, fill_qty, fill_price, sql_utc_stamp(), filled_at, broker_order_id),
            )
            try:
                has_fill = float(fill_qty or 0) > 0
            except (TypeError, ValueError):
                has_fill = False
            if has_fill and fill_price is not None:
                row = self.conn.execute(
                    "SELECT id, symbol, action, reasoning FROM trades WHERE broker_order_id = ?",
                    (broker_order_id,),
                ).fetchone()
                if row is not None and row["action"] not in {"BUY", "SWEEP_BUY", "HOLD"}:
                    realized = self._realized_pnl_through_trade(row["symbol"], row["id"])
                    # Recompute exit_reason_category now that the fill is
                    # CONFIRMED — the broker_stop_fill (TRAIL_STOP) and
                    # take_profit_target (TAKE_PROFIT) categories are gated
                    # on a real fill and are still None from insert time
                    # (submitted, outcome unknown) until this update lands.
                    # A no-op recompute for every other action (already
                    # settled from `reasoning` at insert time).
                    exit_category = _categorize_exit_reason(
                        row["action"],
                        row["reasoning"],
                        fill_status,
                        fill_qty,
                    )
                    self.conn.execute(
                        "UPDATE trades SET realized_pnl = ?, exit_reason_category = ? WHERE id = ?",
                        (realized, exit_category, row["id"]),
                    )
            symbols = self.conn.execute(
                "SELECT DISTINCT symbol FROM trades WHERE broker_order_id = ?", (broker_order_id,)
            ).fetchall()
            for found in symbols:
                self._regroup_position_ids_locked(found["symbol"])
            self.conn.commit()
            return cur.rowcount or 0

    def _realized_pnl_through_trade(self, symbol: str, through_id: int) -> float | None:
        """Average-cost P&L for one confirmed exit; caller holds ``_lock``.

        Walks SIGNED inventory (long positive, short negative) with the
        average ENTRY price of the open side. BUY / SWEEP_BUY add shares;
        SHORT / SELL / PARTIAL_SELL / REDUCE / SWEEP_SELL subtract; COVER adds.
        An exit that reduces |inventory| realises P&L: a long exit is
        ``(exit - avg) * qty``, a short cover is ``(avg - cover) * qty``.

        The trades table has no side column, so direction comes from the
        action name (`exit_action_side`: SELL* long, COVER* short) and, for
        either-side exits (STOP_OUT, TRAIL_STOP, REDUCE, ...), from the sign
        of the position being closed. Direction that cannot be known, or an
        exit larger than the open inventory, stays None: unknown stays
        unknown. Opening rows (BUY / SHORT) realise nothing, so the target
        row's P&L is only ever set by an exit.
        """
        rows = self.conn.execute(
            "SELECT id, action, qty, price, fill_status, fill_qty, fill_price "
            "FROM current_account_trades WHERE symbol = ? AND id <= ? ORDER BY id",
            (symbol, through_id),
        ).fetchall()
        inventory = 0.0  # signed: long > 0, short < 0
        average_price = 0.0  # average entry price of the open side
        target_pnl: float | None = None
        for row in rows:
            status = str(row["fill_status"] or "").lower()
            actual_qty = float(row["fill_qty"] or 0)
            actual_price = row["fill_price"]
            # Only broker-confirmed execution facts are safe cost basis.
            if (
                actual_qty <= 0
                or actual_price is None
                or status
                in {
                    "submitted",
                    "pending_submit",
                    "submit_failed",
                }
            ):
                continue
            actual_price = float(actual_price)
            action = str(row["action"] or "").upper()
            if action == "HOLD":
                continue
            if action in {"BUY", "SWEEP_BUY", "SHORT"}:
                inventory, average_price = _apply_opening_fill(
                    inventory, average_price, -actual_qty if action == "SHORT" else actual_qty, actual_price
                )
                continue
            closing = _closing_side(action, inventory)
            open_qty = inventory if closing == "long" else -inventory if closing == "short" else 0.0
            if closing is None or open_qty + 1e-9 < actual_qty:
                pnl = None  # incomplete canonical cost basis; unknown stays unknown
                if open_qty > 0:
                    # Oversize exit: the open side is gone, the excess is unknowable.
                    inventory = 0.0
                    average_price = 0.0
            else:
                pnl = round(
                    ((actual_price - average_price) if closing == "long" else (average_price - actual_price))
                    * actual_qty,
                    6,
                )
                inventory += -actual_qty if closing == "long" else actual_qty
                if abs(inventory) <= 1e-9:
                    inventory = 0.0
                    average_price = 0.0
            if row["id"] == through_id:
                target_pnl = pnl
        return target_pnl

    def get_symbols_with_open_ledger_qty(self) -> dict[str, float]:
        """Per-symbol net share count the `trades` ledger BELIEVES it holds.

        BUY / SWEEP_BUY add executed qty; every other non-HOLD executed
        action subtracts it — mirrors the accounting `_realized_pnl_
        through_trade` and `compute_trade_calibration` already do,
        collapsed to a running total per symbol instead of per-lot detail,
        because this function only needs to know WHETHER the ledger and
        the broker still agree, not how a mismatch would price out.

        This is the ledger's own, self-contained belief — it has no idea
        the broker did anything it was never told about. Comparing this
        number against `AlpacaBroker.get_positions()` is exactly how the
        2026-08-28 ONDS/CCJ gap was found: both BUY rows left this
        function reporting 17 and 2 shares respectively long after the
        broker's own book had gone to zero, because the protective stop
        that closed them was never written back to `trades`.
        `_reconcile_stop_out_fills` (src/pipeline.py) is the caller that
        acts on a mismatch.

        TRAIL_STOP IS THE ONE ACTION THIS CANNOT SIGN FROM THE ACTION NAME
        (fixed 2026-09-23). Every other exit-family row is written only
        once the desk has decided to sell, but a TRAIL_STOP row is written
        at PLACEMENT — protection resting at the broker, which may never
        fire. `_executed_trade_predicate` lets a legacy `fill_status IS
        NULL` placement through, and signing it -1 subtracted the whole
        protected position from the ledger's belief. Measured on the
        production DB 2026-09-23, that made the ledger read AMD 0 (1.7662
        actually held, so a real AMD stop-out would never have been
        detected — the caller skips any symbol it believes is flat) and
        drove COP/EQNR negative. The rule here is the quantity the broker
        actually EXECUTED (`_trail_stop_reduced_position`).

        `_is_filled_trail_stop`, `compute_trade_calibration`,
        `_assign_position_ids` and `_categorize_exit_reason` all separate
        a resting stop from a fired one too, but they ask the NARROWER
        question — "is this a priceable realized exit" — and this function
        deliberately departs from them on one row shape: a stop that
        partially filled and was then canceled is not a round trip they
        can price, yet its shares really did leave the book. See
        `_trail_stop_reduced_position` for why that is not drift.

        SIGNED BY POSITION SIDE, corrected on the short side (item 173(c),
        2026-09-25). Both short-retiring routes now sign +1: a COVER-family
        action (COVER / EMERGENCY_COVER / PARTIAL_COVER) is a buy-to-cover by
        name, and a FILLED TRAIL_STOP resting on a short is a buy-to-cover
        read from the running net (its side is not in its name). Before this,
        every non-BUY row signed -1, so a SHORT 36 covered in full read -72,
        not 0 whether it came as a COVER or as a filled buy-to-cover
        TRAIL_STOP [measured 2026-09-23]. The caller
        `_reconcile_stop_out_fills` is LONG-only and skips any negative, so
        no live behaviour changed today; the ledger's own belief is simply
        now correct for the day shorts are enabled.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT symbol, action, qty, fill_qty, fill_status FROM current_account_trades "
                f"WHERE {self._executed_trade_predicate()} ORDER BY id",
            ).fetchall()
        net: dict[str, float] = {}
        for row in rows:
            action = (row["action"] or "").upper()
            if action == "HOLD":
                continue
            if action == "TRAIL_STOP" and not _trail_stop_reduced_position(row, action):
                # Protection sitting at the broker, not a sale: no
                # quantity effect at all.
                continue
            qty = float(row["fill_qty"] if row["fill_qty"] else row["qty"] or 0)
            if qty <= 0:
                continue
            symbol = row["symbol"]
            running = net.get(symbol, 0.0)
            # Item 173(c): sign a share-moving row by the SIDE of the
            # position it acts on, not by a hard-coded BUY-vs-everything-else
            # split. A COVER-family action is a BUY-to-cover: it RETIRES a
            # short toward zero, so it ADDS shares (+1). The old rule signed
            # every non-BUY row -1, so a SHORT 36 covered in full read -72,
            # not 0 [measured 2026-09-23]. Normalise PARTIAL_COVER(50%) ->
            # PARTIAL_COVER first, exactly as `_symbols_already_trimmed_today`
            # does. A FILLED TRAIL_STOP carries no side in its name — it is a
            # long's protective SELL or a short's protective BUY-to-cover
            # depending on the position it guards — so read that side from the
            # running net for this symbol (rows are id-ordered, so the entry
            # always precedes its stop): a stop resting on a short is a
            # buy-to-cover and ADDS. Everything else — SELL / REDUCE /
            # STOP_OUT / SWEEP_SELL / a long's fired TRAIL_STOP, and SHORT
            # (a sell-to-open) — subtracts.
            base_action = action.split("(", 1)[0].strip()
            if base_action in ("BUY", "SWEEP_BUY", "COVER", "EMERGENCY_COVER", "PARTIAL_COVER"):
                sign = 1.0
            elif base_action == "TRAIL_STOP" and running < -1e-9:
                sign = 1.0  # fired protective stop on a SHORT = buy-to-cover
            else:
                sign = -1.0
            net[symbol] = running + sign * qty
        return net

    def get_known_broker_order_ids(self, symbol: str) -> set[str]:
        """Every `broker_order_id` already recorded in `trades` for `symbol`.

        The dedup key `_reconcile_stop_out_fills` uses to tell "the broker
        already told us about this order" apart from "this fill has never
        touched the ledger". The reconciler re-runs every session
        (morning / intra_check / midday / close / evening), so this set is
        what keeps recording a stop-out an exactly-once operation no
        matter how many passes see the same gap.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT broker_order_id FROM trades WHERE symbol = ? AND broker_order_id IS NOT NULL",
                (symbol,),
            ).fetchall()
        return {r[0] for r in rows}

    def insert_stop_out_trade(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        broker_order_id: str,
        filled_at: str | None,
        run_id: str | None = None,
        action: str = "STOP_OUT",
        reasoning: str | None = None,
    ) -> tuple[int, bool]:
        """Idempotently record a broker-initiated exit the ledger never saw.

        2026-08-28: ONDS (17 @ 8.53, stopped 7.93 → realized -$10.20) and
        CCJ (2 @ 107.465, stopped 102.955 → realized -$9.02) were both
        closed by their broker-resident protective stop with NO row ever
        written to `trades`. The stop order was placed by
        `AlpacaBroker.place_entry_protection` / `_repair_stop_coverage` /
        `shift_stops_down`, none of which log the ORDER ITSELF as a ledger
        row — unlike every system-DECIDED exit (SELL / REDUCE / TRAIL_STOP
        / SWEEP_SELL), which all call `insert_trade` at submission time and
        get picked up by `_reconcile_fills` once terminal. This is the
        write-back for that other class of order. There is no 'submitted'
        phase for a row created here: by the time `_reconcile_stop_out_
        fills` learns the order exists, the broker has already reported it
        as terminally filled.

        Idempotency: keyed on `broker_order_id`, checked and inserted
        under the SAME lock, so no matter how many times a session's
        reconciliation pass runs — or how many overlapping sessions
        observe the same gap — one broker order id can only ever produce
        ONE row. Mirrors `update_trade_fill`'s realized_pnl write, just
        for a row that does not exist yet rather than one already
        'submitted'.

        `realized_pnl` is computed the instant the row exists, via the
        SAME average-cost walk every other exit uses
        (`_realized_pnl_through_trade`) — it stays NULL, not a guess, when
        the ledger's own BUY history can't cover the exited quantity (an
        unmatched exit; `_reconcile_stop_out_fills` flags that case rather
        than silently accepting an unpriced row).

        `action` (item 173(a)): the caller sets this from the broker fill's
        own order_type — STOP_OUT only when the broker order really was a
        protective stop, SELL for a market/limit exit, and RECONCILED_EXIT
        when the type does not prove it was a stop. It defaults to STOP_OUT
        only for backward compatibility with callers that pass none; the
        reconciler always passes it explicitly so a recovered exit is never
        labelled a protective stop the broker record can't substantiate.

        Returns `(row_id, created)`. `created=False` means the order was
        already recorded — the existing row's id is returned so a caller
        never needs a second lookup to stay idempotent-safe.
        """
        if not broker_order_id:
            # Every caller constructs this from a REAL broker order dict
            # that is only ever produced with a non-empty id (see
            # AlpacaBroker.list_filled_sell_orders) — a falsy id here means
            # a caller bug, not a legitimate row. Refusing loudly beats
            # silently inserting a row the idempotency key can never find
            # again (broker_order_id IS NULL would never match on replay,
            # and this exit could get double-recorded on the next pass —
            # exactly the failure mode this function exists to prevent).
            raise ValueError(
                "insert_stop_out_trade requires a non-empty broker_order_id "
                "— it is the idempotency key that makes a stop-out record "
                "exactly-once across repeated reconciliation passes"
            )

        def _do():
            existing = self.conn.execute(
                "SELECT id FROM trades WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
            if existing is not None:
                return existing["id"], False
            # `timestamp` keeps the desk's one-second UTC stamp; `filled_at` keeps the broker's full precision.
            fill_utc = broker_fill_time(filled_at)
            ts = fill_utc[:19] if fill_utc else filled_at or self._sqlite_utc_timestamp(utc_now())
            act_norm = (action or "").upper()
            if reasoning:
                reasoning_final = reasoning
            elif act_norm == "STOP_OUT":
                reasoning_final = (
                    "Broker-initiated protective-stop fill — the system "
                    "never submitted this order as a decision; written "
                    "back by the stop-out reconciler (2026-08-28 "
                    "ONDS/CCJ gap; see ReconciliationConfig)."
                )
            elif act_norm == "RECONCILED_EXIT":
                # item 173(a): the reconciler proved a broker exit happened but
                # NOT that it was a protective stop — say exactly that, never a
                # cause it can't stand behind.
                reasoning_final = (
                    "Broker-initiated exit the ledger never saw; the broker's "
                    "order type did not identify it as a protective stop, so it "
                    "is recorded as an unattributed reconciled exit rather than "
                    "a STOP_OUT (item 173(a); see ReconciliationConfig)."
                )
            else:
                reasoning_final = (
                    f"Broker-initiated {act_norm or 'exit'} the ledger never "
                    "saw; written back by the exit reconciler (item 173(a); "
                    "see ReconciliationConfig)."
                )
            position_id = self._resolve_new_row_position_id(
                symbol,
                action,
                qty=qty,
                fill_status="filled",
                fill_qty=qty,
                timestamp=ts,
                filled_at=filled_at,
            )
            exit_category = _categorize_exit_reason(action, reasoning_final, "filled", qty)
            # Conviction ledger (§7.2): this function's entire reason for
            # existing is "the broker closed this with NO row and NO
            # decision ever written" (see docstring above) — there is no
            # decision_id parameter to accept here, so the label is always
            # the honest absence, never conditional.
            decision_link_status = "no_originating_decision"
            cur = self.conn.execute(
                "INSERT INTO trades (symbol, action, qty, price, reasoning, "
                "run_id, broker_order_id, fill_status, fill_qty, fill_price, "
                "fill_reconciled_at, timestamp, position_id, exit_reason_category, "
                "decision_id_status, filled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol,
                    action,
                    qty,
                    price,
                    reasoning_final,
                    run_id,
                    broker_order_id,
                    qty,
                    price,
                    sql_utc_stamp(),  # fill_reconciled_at, from the one clock
                    ts,
                    position_id,
                    exit_category,
                    decision_link_status,
                    filled_at,
                ),
            )
            row_id = cur.lastrowid
            realized = self._realized_pnl_through_trade(symbol, row_id)
            self.conn.execute(
                "UPDATE trades SET realized_pnl = ? WHERE id = ?",
                (realized, row_id),
            )
            self._regroup_position_ids_locked(symbol)
            self.conn.commit()
            return row_id, True

        return self._locked_write(_do, label="insert_stop_out_trade")

    def get_unreconciled_orders(self, run_id: str | None = None) -> list[dict]:
        """Trade rows with broker_order_id set but fill_status still 'submitted'.

        Pipeline's reconciliation step fetches these and asks the broker for
        their terminal status. Scoping to run_id lets per-run reconciliation
        not touch stragglers from other runs.
        """
        conditions = ["fill_status = 'submitted'", "broker_order_id IS NOT NULL"]
        params: list = []
        if run_id:
            conditions.append("run_id = ?")
            params.append(run_id)
        where = " AND ".join(conditions)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM current_account_trades WHERE {where}",
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_orphaned_pending_submits(
        self,
        min_age_seconds: int = 120,
    ) -> list[dict]:
        """BUY write-ahead rows the broker may or may not have received:
        fill_status 'pending_submit' with broker_order_id still NULL —
        a crash between submit_order() returning and
        confirm_trade_submitted() landing.

        audit F4: confirm_trade_submitted's docstring promised reconcile
        could detect orphans by exactly this predicate, but nothing swept
        them — a real broker fill could go forever untracked. Age-gated
        (timestamp older than min_age_seconds) so a same-process in-flight
        submit — converted to submitted/submit_failed within microseconds
        — is never misread as an orphan; real orphans are from a prior
        crashed session and are minutes-to-days old. The cutoff uses
        the desk clock rendered in SQLite's stored text format, so there's
        no host-TZ / format skew.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM current_account_trades WHERE fill_status = 'pending_submit' "
                "AND broker_order_id IS NULL "
                "AND timestamp < ? "
                "ORDER BY timestamp ASC",
                (sql_utc_stamp(utc_now() - timedelta(seconds=int(min_age_seconds))),),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_pending_action_for_symbol(
        self,
        symbol: str,
        action: str,
        today_only: bool = True,
    ) -> bool:
        """True if a (symbol, action) trade row exists with fill_status
        'submitted' and a broker_order_id — i.e., a previous submission
        is still in flight at the broker.

        Used to keep consecutive intra_check ticks from re-firing the same
        EMERGENCY_SELL while the first limit order is still pending fill.
        Without this, intra at T submits a -1% LIMIT EMERGENCY_SELL, the
        tape goes through it without filling, and intra at T+30min sees
        the position still on book and submits a duplicate — risking
        double-exit on a partial fill of the first order.

        today_only restricts the lookup to the current ET trading day so
        a stale 'submitted' row from a previous session can't permanently
        block a fresh exit. If your reconciliation pass updated the row
        to a terminal status, this returns False as expected.
        """
        conditions = [
            "fill_status = 'submitted'",
            "broker_order_id IS NOT NULL",
            "symbol = ?",
            "action = ?",
        ]
        params: list = [symbol, action]
        if today_only:
            start, end = self._et_day_utc_bounds()
            conditions.append("timestamp >= ?")
            conditions.append("timestamp < ?")
            params.extend([start, end])
        where = " AND ".join(conditions)
        with self._lock:
            row = self.conn.execute(
                f"SELECT 1 FROM current_account_trades WHERE {where} LIMIT 1",
                tuple(params),
            ).fetchone()
        return row is not None

    def insert_pending_protection_restore(
        self,
        *,
        symbol: str,
        sell_order_id: str,
        position_qty_before_sell: float,
        specs_json: str,
        run_id: str | None = None,
        side: str | None = None,
    ) -> int:
        return _recovery_queues.insert_pending_protection_restore(
            self,
            symbol=symbol,
            sell_order_id=sell_order_id,
            position_qty_before_sell=position_qty_before_sell,
            specs_json=specs_json,
            run_id=run_id,
            side=side,
        )

    def get_protection_restore_wal_audit(self) -> list[dict]:
        return _recovery_queues.get_protection_restore_wal_audit(self)

    def get_pending_protection_restores(self) -> list[dict]:
        return _recovery_queues.get_pending_protection_restores(self)

    def delete_pending_protection_restore(self, row_id: int) -> int:
        return _recovery_queues.delete_pending_protection_restore(self, row_id)

    def update_pending_protection_restore(
        self,
        row_id: int,
        *,
        sell_order_id: str | None = None,
        position_qty_before_sell: float | None = None,
        specs_json: str | None = None,
        side: str | None = None,
    ) -> int:
        return _recovery_queues.update_pending_protection_restore(
            self,
            row_id,
            sell_order_id=sell_order_id,
            position_qty_before_sell=position_qty_before_sell,
            specs_json=specs_json,
            side=side,
        )

    def update_pending_protection_restore_specs(
        self,
        row_id: int,
        specs_json: str,
    ) -> int:
        return _recovery_queues.update_pending_protection_restore_specs(self, row_id, specs_json)

    def insert_pending_repeg(
        self,
        *,
        trade_row_id: int | None,
        symbol: str,
        old_order_id: str,
        new_order_id: str,
        run_id: str | None = None,
    ) -> int:
        return _recovery_queues.insert_pending_repeg(
            self,
            trade_row_id=trade_row_id,
            symbol=symbol,
            old_order_id=old_order_id,
            new_order_id=new_order_id,
            run_id=run_id,
        )

    def get_pending_repegs(self) -> list[dict]:
        return _recovery_queues.get_pending_repegs(self)

    def resolve_pending_repeg(self, row_id: int, new_order_id: str) -> int:
        return _recovery_queues.resolve_pending_repeg(self, row_id, new_order_id)

    def delete_pending_repeg(self, row_id: int) -> int:
        return _recovery_queues.delete_pending_repeg(self, row_id)

    def prune_pending_repegs(self, keep_days: int = 30) -> int:
        return _recovery_queues.prune_pending_repegs(self, keep_days)

    def get_trades(
        self, symbol: str | None = None, limit: int = 100, today_only: bool = False, executed_only: bool = False
    ) -> list[dict]:
        conditions = []
        params: list = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if today_only:
            start_utc, end_utc = self._et_day_utc_bounds()
            conditions.append("timestamp >= ? AND timestamp < ?")
            params.extend([start_utc, end_utc])
        if executed_only:
            conditions.append(self._executed_trade_predicate())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            # Secondary order-by on id ensures tie-break ordering is
            # deterministic — SQLite's timestamp precision is 1 second, so
            # a BUY inserted at T0 and TAKE_PROFIT inserted at T0+0.01 both
            # carry the same timestamp string. Without id DESC, duplicate-
            # timestamp rows come back in indeterminate order and logic
            # that scans "trades newer than the most recent BUY" can miss
            # the newer row.
            rows = self.conn.execute(
                f"SELECT * FROM trades {where} ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _accumulate_excursions(self, position) -> None:
        return _excursions._accumulate_excursions(self, position)

    def record_overnight_gap(
        self,
        symbol: str,
        prev_close: float,
        open_price: float,
        session_date: str,
    ) -> bool:
        return _excursions.record_overnight_gap(self, symbol, prev_close, open_price, session_date)

    def _accumulate_level_distances(self, position) -> None:
        return _excursions._accumulate_level_distances(self, position)

    def sync_positions(self, positions) -> None:
        return _excursions.sync_positions(self, positions)

    def update_open_take_profit(
        self,
        symbol: str,
        new_target: float,
        *,
        action: str | None = None,
    ) -> bool:
        """Write a re-derived target onto every opening row of this position.

        Mirrors `update_open_stop_loss`. Never touches `initial_take_profit`
        — that column is the entry derivation and the denominator of
        progress/pace, and a revision must not be able to move it.

        Refuses a non-positive target rather than zeroing the field: a zero
        target would make `thesis_progress_pct` undefined, and a blank is
        exactly what the per-symbol refusal record exists to avoid.
        """
        try:
            target = float(new_target)
        except (TypeError, ValueError):
            return False
        if not target > 0:
            logger.error(
                "update_open_take_profit refused a non-positive target for %s: %r",
                symbol,
                new_target,
            )
            return False
        act = (action or "").strip().upper()
        if act and act not in ("BUY", "SHORT"):
            logger.error(
                "update_open_take_profit refused unknown action %r for %s",
                action,
                symbol,
            )
            return False

        def _do():
            sql = "UPDATE trades SET take_profit = ? WHERE symbol = ? AND UPPER(action) IN ('BUY', 'SHORT')"
            params: list = [target, symbol.upper()]
            if act:
                sql = "UPDATE trades SET take_profit = ? WHERE symbol = ? AND UPPER(action) = ?"
                params = [target, symbol.upper(), act]
            sql += (
                " AND position_id = (SELECT position_id FROM current_account_trades "
                "WHERE symbol = ? AND UPPER(action) IN ('BUY', 'SHORT') "
                "ORDER BY id DESC LIMIT 1)"
            )
            params.append(symbol.upper())
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur.rowcount > 0

        return self._locked_write(_do, label="update_open_take_profit")

    def prune_trades(self, keep_days: int = 365 * 5) -> int:
        """Delete trades rows older than keep_days. Default retention 5 years.

        Kept long for audit purposes — still finite to bound table size over a
        decade-plus horizon. Returns count deleted.
        """
        if keep_days <= 0:
            # a zero-day cutoff == 'now' → deletes the entire
            # trades audit log. Refuse rather than silently destroy
            # potentially years of broker history.
            raise ValueError(f"prune_trades: keep_days must be > 0, got {keep_days}")
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM trades WHERE timestamp < ?",
                (sql_utc_stamp_ago(days=keep_days),),
            )
            self.conn.commit()
            return cursor.rowcount or 0

    def prune_pending_protection_restores(self, keep_days: int = 30) -> int:
        return _recovery_queues.prune_pending_protection_restores(self, keep_days)

    def backfill_position_ids(self, *, dry_run: bool = False) -> dict:
        return _backfills.backfill_position_ids(self, dry_run=dry_run)

    def backfill_conviction_ledger(self, *, dry_run: bool = False) -> dict:
        return _backfills.backfill_conviction_ledger(self, dry_run=dry_run)
