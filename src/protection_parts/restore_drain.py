"""The pending protection-restore drain, lifted verbatim out of `src/pipeline_protection.py`
(2026-10-09, ceiling split). Behaviour is unchanged; only its module moved."""

import json as _json
import logging

from src.protection.sell_finalization import _WAL_SELL_SENTINEL
from src.sentinel.reconciliation import record_guarded_outcome

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _restore_record(row: dict) -> dict:
    """What the owner is told about one stop put back: which stock, how many shares, at what stop.

    Read from the pending row the drain just discharged. `unprotected_since` is when the row was
    written, i.e. when the stop was taken off. An unreadable or empty spec list still yields a
    record (the stock is still named), with no stop price rather than a guessed one.
    """
    try:
        specs = _json.loads(row.get("specs_json") or "[]")
    except Exception:  # noqa: BLE001 - the record only describes; the restore itself already happened
        specs = []
    shares = 0.0
    stop_prices: list[float] = []
    for spec in specs if isinstance(specs, list) else []:
        if not isinstance(spec, dict):
            continue
        try:
            shares += float(spec.get("qty") or 0)
            price = float(spec.get("stop_price") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and price not in stop_prices:
            stop_prices.append(price)
    return {
        "symbol": row["symbol"],
        "shares": shares or float(row.get("position_qty_before_sell") or 0),
        "stop_price": stop_prices[0] if stop_prices else None,
        "stop_prices": stop_prices,
        "unprotected_since": row.get("created_at"),
    }


class RestoreDrain:
    """The write-ahead protection-restore drain; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        broker=None,
        db=None,
        terminal_order_statuses=None,
        finalize_protection_after_sell=None,
        resolve_wal_row_side=None,
        restore_after_unconfirmed_sell=None,
    ) -> None:
        self.broker = broker
        self.db = db
        self._TERMINAL_ORDER_STATUSES = terminal_order_statuses
        self._finalize_protection_after_sell = finalize_protection_after_sell
        self._resolve_wal_row_side = resolve_wal_row_side
        self._restore_after_unconfirmed_sell = restore_after_unconfirmed_sell

    def _drain_pending_protection_restores(self) -> list[dict]:
        """Re-attempt orphaned protection restores from previous sessions.

        For each persisted row: re-query the SELL's terminal status. If
        terminal, run finalize from the persisted specs; on success,
        delete the row. If still non-terminal, leave the row for next
        session. Returns one record per row successfully drained (symbol, shares,
        stop price, when it was left unprotected) so the owner is told WHICH stocks.

        Called at the start of each pipeline session so a single bail
        doesn't leave a position permanently unprotected.
        """
        try:
            rows = self.db.get_pending_protection_restores()
        except Exception as exc:
            logger.warning("drain_pending_protection_restores: DB read failed: %s", exc)
            record_guarded_outcome(db=self.db, where="drain.read_rows", exc=exc, log=logger)
            return []
        if not rows:
            return []

        restored: list[dict] = []
        for row in rows:
            row_id = row["id"]
            symbol = row["symbol"]
            order_id = row["sell_order_id"]

            from src.execution.scale_in import WAL_SCALE_IN_SENTINEL, drain_scale_in_row

            if order_id == WAL_SCALE_IN_SENTINEL:
                try:
                    ok = drain_scale_in_row(self.broker, self.db, row)
                except Exception as exc:  # noqa: BLE001
                    record_guarded_outcome(
                        db=self.db,
                        where="drain.scale_in_restore",
                        exc=exc,
                        log=logger,
                        context={"symbol": symbol, "row": row_id, "effect": "leaving for next session"},
                    )
                    continue
                if ok:
                    try:
                        self.db.delete_pending_protection_restore(row_id)
                    except Exception as exc:
                        record_guarded_outcome(db=self.db, where="drain.delete_after_scale_in", exc=exc, log=logger)
                    restored.append(_restore_record(row))
                    logger.info(
                        "drain: scale-in recovery rebuilt coverage for %s (row %d cleared)",
                        symbol,
                        row_id,
                    )
                continue

            # audit F1: a write-ahead row whose SELL was never confirmed
            # submitted (crash in the cancel→submit→record window). There
            # is no SELL order to query — restore coverage from the
            # broker's CURRENT position instead.
            if order_id == _WAL_SELL_SENTINEL:
                try:
                    wal_specs = _json.loads(row["specs_json"])
                except Exception as exc:
                    record_guarded_outcome(
                        db=self.db,
                        where="drain.wal_specs_parse",
                        exc=exc,
                        log=logger,
                        context={"row": row_id, "effect": "deleting orphan to unblock the queue"},
                    )
                    try:
                        self.db.delete_pending_protection_restore(row_id)
                    except Exception as exc:
                        record_guarded_outcome(
                            db=self.db, where="drain.delete_unparseable_wal_row", exc=exc, log=logger
                        )
                    continue
                # Stage 3 (shorts): the row now carries its own `side` —
                # written at creation time by whoever closed the position,
                # so this is no longer a guess reconstructed from live
                # broker state. `_resolve_wal_row_side` prefers that
                # persisted value and only falls back to the live-broker
                # derivation (`_derive_close_side_for_drain`, defaulting to
                # 'sell' when unreadable) for a row written BEFORE this
                # column existed (`side IS NULL`) — logged when that
                # fallback fires. The premise this comment used to state —
                # "shorts cannot be opened through this system, so the gap
                # is moot" — is no longer true now that they can be.
                side_kwargs = self._resolve_wal_row_side(row, symbol) if wal_specs else {}
                try:
                    ok, retry = self._restore_after_unconfirmed_sell(
                        symbol,
                        float(row["position_qty_before_sell"]),
                        wal_specs,
                        **side_kwargs,
                    )
                except Exception as exc:
                    record_guarded_outcome(
                        db=self.db,
                        where="drain.wal_restore",
                        exc=exc,
                        log=logger,
                        context={"symbol": symbol, "row": row_id, "effect": "leaving for next session"},
                    )
                    continue
                if ok:
                    try:
                        self.db.delete_pending_protection_restore(row_id)
                    except Exception as exc:
                        record_guarded_outcome(db=self.db, where="drain.delete_after_wal_restore", exc=exc, log=logger)
                    restored.append(_restore_record(row))
                    logger.info(
                        "drain: WAL recovery rebuilt coverage for %s (row %d cleared)",
                        symbol,
                        row_id,
                    )
                elif retry and len(retry) < len(wal_specs):
                    try:
                        self.db.update_pending_protection_restore_specs(
                            row_id,
                            _json.dumps(retry),
                        )
                    except Exception as exc:
                        record_guarded_outcome(
                            db=self.db, where="drain.narrow_wal_row", exc=exc, log=logger, context={"row": row_id}
                        )
                continue

            try:
                fill_info = self.broker.get_order_fill_info(order_id) or {}
            except Exception as exc:
                record_guarded_outcome(
                    db=self.db,
                    where="drain.broker_fill_query",
                    exc=exc,
                    log=logger,
                    context={
                        "symbol": symbol,
                        "order": order_id,
                        "row": row_id,
                        "effect": "leaving row for next session",
                    },
                )
                continue
            status = (fill_info.get("status") or "").lower()
            if status not in self._TERMINAL_ORDER_STATUSES:
                logger.info(
                    "drain: %s (order %s) still non-terminal (status=%s) — leaving row %d for next session",
                    symbol,
                    order_id,
                    status,
                    row_id,
                )
                continue
            try:
                cancelled_specs = _json.loads(row["specs_json"])
            except Exception as exc:
                record_guarded_outcome(
                    db=self.db,
                    where="drain.specs_parse",
                    exc=exc,
                    log=logger,
                    context={"row": row_id, "effect": "deleting orphan to unblock the queue"},
                )
                try:
                    self.db.delete_pending_protection_restore(row_id)
                except Exception as exc:
                    record_guarded_outcome(db=self.db, where="drain.delete_unparseable_row", exc=exc, log=logger)
                continue
            # Same persisted-side-first resolution as the sentinel branch
            # above (see `_resolve_wal_row_side`): a row written after the
            # Stage 3 migration carries its own real side; only a legacy
            # `side IS NULL` row falls back to the live-broker derivation.
            finalize_side_kwargs = self._resolve_wal_row_side(row, symbol) if cancelled_specs else {}
            # Order is terminal; replay finalize from persisted specs.
            # finalize itself reads fill_info again — same broker call,
            # cheap. ``from_drain=True`` so finalize doesn't re-persist
            # if it bails (the row already exists). Only delete the row
            # when finalize CONFIRMS coverage was actually rebuilt — if
            # restore_stop_orders submits 0/N or reprotect raises, the
            # row stays and the next session retries. Codex r8 #3.
            try:
                ok, retry_specs = self._finalize_protection_after_sell(
                    order_id=order_id,
                    symbol=symbol,
                    position_qty_before_sell=float(row["position_qty_before_sell"]),
                    cancelled_specs=cancelled_specs,
                    from_drain=True,
                    **finalize_side_kwargs,
                )
                if not ok:
                    # Narrow the row to retry_specs if a partial restore
                    # made progress: re-submitting an already-alive stop
                    # next pass would create duplicates / hit
                    # held_for_orders. Codex r10 #1.
                    if retry_specs and len(retry_specs) < len(cancelled_specs):
                        try:
                            self.db.update_pending_protection_restore_specs(
                                row_id,
                                _json.dumps(retry_specs),
                            )
                            logger.info(
                                "drain: row %d narrowed from %d to %d spec(s) (partial restore made progress)",
                                row_id,
                                len(cancelled_specs),
                                len(retry_specs),
                            )
                        except Exception as exc:
                            record_guarded_outcome(
                                db=self.db, where="drain.narrow_row", exc=exc, log=logger, context={"row": row_id}
                            )
                    logger.warning(
                        "drain: finalize for %s row %d did not rebuild coverage — leaving row for next session",
                        symbol,
                        row_id,
                    )
                    continue
                self.db.delete_pending_protection_restore(row_id)
                restored.append(_restore_record(row))
                logger.info(
                    "drain: replayed protection finalize for %s (order %s, row %d cleared)",
                    symbol,
                    order_id,
                    row_id,
                )
            except Exception as exc:
                record_guarded_outcome(
                    db=self.db,
                    where="drain.finalize_replay",
                    exc=exc,
                    log=logger,
                    context={"symbol": symbol, "row": row_id, "effect": "leaving row for next session"},
                )
        if restored:
            logger.info("drain: cleared %d orphaned protection-restore row(s)", len(restored))
        # Proof the drain RAN. Without this, "no fault rows" is ambiguous
        # between a clean pass and a drain that was never called at all.
        record_guarded_outcome(db=self.db, where="drain.completed", log=logger)
        return restored
