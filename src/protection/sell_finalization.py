"""Protection after a sell: the write-ahead restore row, the post-sell finalisation of stop coverage, and the restore after an unconfirmed sell.

Lifted verbatim out of `ProtectionMixin` (src/pipeline_protection.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
import json as _json

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
from src.sentinel.guarded import record_guarded_pass
from src.protection.wal_restore_write import write_ahead_restore_row

logger = logging.getLogger("src.pipeline")

# audit F1: a pending_protection_restores row written BEFORE the SELL is
# submitted carries this as sell_order_id — it means "protective stops
# were cancelled but the SELL was never confirmed at the broker" (crash
# in the cancel→submit→record window). The drain pass recognises it and
# restores coverage from the broker's CURRENT position rather than
# querying a SELL order that may not exist.
_WAL_SELL_SENTINEL = "__WAL_PENDING__"


class SellFinalization:
    """Protection after a sell: the write-ahead restore row, the post-sell finalisation of stop coverage, and the restore after an unconfirmed sell."""

    def __init__(
        self,
        *,
        broker,
        db,
        terminal_order_statuses,
        finalize_protection_after_sell,
        register_exit_settlement,
        finalize_protection_after_sell_core,
        cancel_stray_stops_on_flat,
        current_position_qty_for_finalize,
        persist_orphaned_protection_restore,
        reprotect_residual_after_partial_sell,
        derive_close_side_for_drain,
        trim_book=None,
    ) -> None:
        self.broker = broker
        self._trim_book = trim_book  # the broker CLASS (host-injected): src.protection imports no src.execution
        self.db = db
        self._TERMINAL_ORDER_STATUSES = terminal_order_statuses
        self._finalize_protection_after_sell = finalize_protection_after_sell
        self._register_exit_settlement = register_exit_settlement
        self._finalize_protection_after_sell_core = finalize_protection_after_sell_core
        self._cancel_stray_stops_on_flat = cancel_stray_stops_on_flat
        self._current_position_qty_for_finalize = current_position_qty_for_finalize
        self._persist_orphaned_protection_restore = persist_orphaned_protection_restore
        self._reprotect_residual_after_partial_sell = reprotect_residual_after_partial_sell
        self._derive_close_side_for_drain = derive_close_side_for_drain

    def _current_position_qty_for_finalize(self, symbol: str) -> float | None:
        """Re-read broker position for finalize residual / restore math.

        intra_check is exempt from the cross-mode session lock, so an
        EMERGENCY_SELL on the same symbol can reduce position between
        when this SELL submitted and when this finalize runs. The cached
        ``position_qty_before_sell`` no longer reflects reality —
        ``position_qty_before_sell - my_fill_qty`` over-states residual
        and the resulting reprotect / restore would submit for more
        shares than exist (broker rejects on insufficient qty, finalize
        bails, drain persists a row, drain replays same wrong math,
        row stays stuck forever).

        Returns:
            >0 — broker reports this many shares held now
            0  — symbol no longer held (concurrent path fully exited)
            None — could not determine (broker error, mocked test path)
        """
        try:
            positions = self.broker.get_positions()
            record_guarded_pass((self.db, self.broker), "sell_finalization.position_qty_for_finalize")
        except Exception as exc:
            record_guarded_pass(
                (self.db, self.broker),
                "sell_finalization.position_qty_for_finalize",
                exc,
                log=logger,
                context={"effect": "cached residual math"},
            )
            return None
        if not isinstance(positions, list):
            return None
        for p in positions:
            sym = getattr(p, "symbol", None)
            if sym == symbol:
                qty = getattr(p, "qty", None)
                if qty is None:
                    return None
                try:
                    return float(qty)
                except (TypeError, ValueError):
                    return None
        return 0.0

    def _finalize_pending_protections(
        self,
        pending_protections: list[dict],
        *,
        context: str,
        wait: bool = True,
    ) -> None:
        """Tail half of the SELL discipline: drain a batch of stashed
        protection-restore intents after a round of SELLs.

        For each stashed ``{order_id, symbol, position_qty_before_sell, specs,
        wal_row_id}``: (optionally) block until the SELL reaches terminal,
        finalize stop coverage on the ACTUAL fill (reprotect residual / restore
        originals / no-op on full exit), and log when coverage couldn't be
        rebuilt (the WAL row drives a retry next session).

        Previously copy-pasted near-verbatim at 6 call sites — that duplication
        is exactly how a step once went missing (ExecutionStage lacked the wait
        try/except until an audit caught it). Centralizing makes the discipline
        one tested path.

        ``wait=False`` for callers (ExecutionStage) that already waited for
        terminal in an earlier loop — the orders are terminal, so re-waiting
        would be a redundant no-op; skipping it preserves their prior behavior.
        ``context`` is the human-readable log prefix (e.g. 'FORCE DE-LEVER').
        """
        for prot in pending_protections:
            if wait:
                try:
                    # Kept on the intent so a caller can record the outcome
                    # (the gross-exposure de-lever's shortfall row, item 112).
                    prot["terminal_status"] = self.broker.wait_for_order_terminal(
                        prot["order_id"],
                    )
                except Exception as exc:  # noqa: BLE001
                    # Always LEAVE THE KEY, even on failure: a caller reading
                    # it back (the gross-ceiling race guard) must be able to
                    # tell "waited, not terminal" from "never waited".
                    prot["terminal_status"] = None
                    logger.warning(
                        "%s: wait failed for %s order %s: %s — finalize will use whatever fill_info reads now",
                        context,
                        prot["symbol"],
                        prot["order_id"],
                        exc,
                    )
                self._register_exit_settlement(prot)
            finalize_side = prot.get("side")
            side_kwargs = {} if not finalize_side or finalize_side == "sell" else {"side": finalize_side}
            if prot.get("kept_leg"):
                # A trim whose stop was shrunk in place: the whole-share leg
                # is live, so restoring the cancelled specs over it would be
                # refused. The quantity invariant settles the book instead.
                from src.protection.trim_amend import finalize_trim_amend

                prot["coverage_confirmed"] = finalize_trim_amend(self, prot)
                continue
            ok, _retry_specs = self._finalize_protection_after_sell(
                prot["order_id"],
                prot["symbol"],
                prot["position_qty_before_sell"],
                prot["specs"],
                wal_row_id=prot.get("wal_row_id"),
                **side_kwargs,
            )
            prot["coverage_confirmed"] = bool(ok)
            if not ok:
                logger.warning(
                    "%s: finalize for %s (order %s) did not confirm stop "
                    "coverage — recovery intent persisted; drain rebuilds "
                    "next session",
                    context,
                    prot["symbol"],
                    prot["order_id"],
                )

    def _finalize_protection_after_sell(
        self,
        order_id: str,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        from_drain: bool = False,
        wal_row_id: int | None = None,
        side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """Thin wrapper over the finalize core (audit F1 WAL lifecycle).

        ``wal_row_id`` is the pending_protection_restores row written
        BEFORE cancel_protective_stops (write-ahead). The core's bail
        branches UPDATE that row instead of INSERTing a duplicate; here,
        once the core confirms coverage is good (ok=True), the
        write-ahead row is deleted — the recovery intent is discharged.
        ``from_drain`` rows manage their own lifecycle, so the wrapper
        never deletes for them. Backward compatible: callers/tests that
        omit wal_row_id get exactly the pre-F1 behaviour.

        ``side`` — see ``_submit_protected_sell``: 'sell' (default) for a
        long, 'buy' for a short's cover. Passed straight through to the
        core.
        """
        ok, retry_specs = self._finalize_protection_after_sell_core(
            order_id,
            symbol,
            position_qty_before_sell,
            cancelled_specs,
            from_drain=from_drain,
            wal_row_id=wal_row_id,
            side=side,
        )
        if ok and wal_row_id is not None and not from_drain:
            try:
                self.db.delete_pending_protection_restore(wal_row_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WAL: failed to clear discharged protection-restore "
                    "row %d for %s: %s (drain will no-op it next session)",
                    wal_row_id,
                    symbol,
                    exc,
                )
        return ok, retry_specs

    def _finalize_protection_after_sell_core(
        self,
        order_id: str,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        from_drain: bool = False,
        wal_row_id: int | None = None,
        side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """Decide stop coverage based on the actual SELL fill outcome,
        not on submit acceptance.

        ``side`` — 'sell' (default, unchanged) for a long being sold; 'buy'
        for a short being covered. ``position_qty_before_sell`` and every
        qty this function reads back from the broker
        (``_current_position_qty_for_finalize``) are ALWAYS treated as
        non-negative magnitudes here (the broker's own signed qty is
        abs()'d on read) — a short's -73 shares and a long's 73 shares
        drive identical arithmetic; only ``side`` decides which stop side
        gets cancelled/restored/re-placed.

        Submit-acceptance is too early — Alpaca can accept a LIMIT and
        then have it expire / cancel / get rejected later in the session
        without ever filling. If we reprotected on the residual qty at
        accept-time and the SELL doesn't fill, the to-be-sold portion
        rides naked for the rest of the day. PR I (#55) had this gap.

        Reads broker.get_order_fill_info() AFTER wait_for_order_terminal
        has returned, so the fill_qty is final:

        1. ``fill_qty == 0`` (cancelled/expired/rejected after acceptance):
           the position is unchanged but we cancelled the protective
           stops. Restore the original specs covering the full position.
        2. ``0 < fill_qty < position_qty``: protect the actual residual
           ``position_qty_before_sell - fill_qty`` at the most-protective
           cancelled stop_price.
        3. ``fill_qty == position_qty``: full exit, nothing to protect.

        Special case: if get_order_fill_info reports a NON-terminal status
        (the SELL is still 'new' / 'accepted' / 'pending_new' because
        wait_for_order_terminal hit its 15s ceiling without the order
        reaching terminal), finalizing now would race with the broker —
        restoring stops while the SELL is open triggers held_for_orders
        rejection on the new stop submit. Force terminal state by
        cancelling the lingering SELL, then re-read fill_info and
        proceed normally. Codex r5 caught this exact gap.

        Returns ``(success, retry_specs)``:
          - success: True iff coverage is in a known-good state (specs
            were successfully restored / residual was reprotected /
            no residual existed / there were no specs at all). False on
            any bail or restore/reprotect failure.
          - retry_specs: when success=False, the subset of cancelled_specs
            that still need a protection retry. For partial-restore this
            is ONLY the failed specs (the ones that landed are already
            alive at the broker). For other failure modes it's the full
            cancelled_specs list. Empty when success=True.

        ``from_drain=True`` skips the persist-on-bail step (drain
        already has a row). Drain uses retry_specs to NARROW the
        existing row to just what still needs retry — avoids the next
        drain re-submitting a stop that already landed (codex r10 #1).

        No-op when there were no specs to begin with — a position that
        had no protective stop pre-SELL has nothing to restore.
        """
        if not cancelled_specs:
            return True, []

        # Built once, reused at every broker call below that's keyed on the
        # STOP side — omitted entirely for the (default, pre-existing) long
        # case so every downstream call is byte-identical to before shorts.
        side_kwargs = {} if side == "sell" else {"side": side}
        order_word = "BUY" if side == "buy" else "SELL"

        fill_info = self.broker.get_order_fill_info(order_id) or {}
        status = (fill_info.get("status") or "").lower()

        if status not in self._TERMINAL_ORDER_STATUSES:
            # The wait window expired with the order still live. We
            # cannot leave this state — restoring or reprotecting now
            # races with the broker. Cancel the lingering SELL so
            # status converges to terminal.
            logger.warning(
                "%s on %s did not reach terminal in wait window "
                "(status=%s) — cancelling so protection state can settle",
                order_word,
                symbol,
                status or "?",
            )
            try:
                if not self.broker.cancel_entry_order(order_id):
                    raise RuntimeError("lingering order not cancelled (failed, or refused by an owner flag)")
                # The one order-terminal ceiling, not a site literal.
                self.broker.wait_for_order_terminal(order_id)
            except Exception as exc:
                logger.warning(
                    "Failed to cancel lingering %s on %s (order %s): %s "
                    "— persisting orphaned restore intent for next session.",
                    order_word,
                    symbol,
                    order_id,
                    exc,
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id,
                        symbol,
                        position_qty_before_sell,
                        cancelled_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(cancelled_specs)
            # Re-read post-cancel — broker may report partial fill that
            # landed during cancel propagation.
            fill_info = self.broker.get_order_fill_info(order_id) or {}
            status = (fill_info.get("status") or "").lower()
            logger.info(
                "Cancelled lingering %s on %s — post-cancel status=%s, filled_qty=%s",
                order_word,
                symbol,
                status,
                fill_info.get("filled_qty"),
            )
            # Cancel propagation can take longer than the 5s wait window,
            # especially during halts or illiquid conditions. If status
            # is still non-terminal, persist the restore intent and bail
            # — next session's drain pass picks it up. Without persistence
            # the previous bail was a slow leak: the warning promised
            # "next session reconcile rebuilds coverage" but
            # _reconcile_fills only updates fill columns. Codex r7 #3.
            if status not in self._TERMINAL_ORDER_STATUSES:
                logger.warning(
                    "Cancel of lingering %s on %s did not converge to "
                    "terminal within 5s (post-cancel status=%s) — "
                    "persisting orphaned restore intent for next session.",
                    order_word,
                    symbol,
                    status or "?",
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id,
                        symbol,
                        position_qty_before_sell,
                        cancelled_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(cancelled_specs)

        fill_qty_raw = fill_info.get("filled_qty")
        try:
            fill_qty = float(fill_qty_raw) if fill_qty_raw is not None else 0.0
        except (TypeError, ValueError):
            fill_qty = 0.0

        if fill_qty <= 0:
            # Concurrent-SELL guard: a parallel intra_check EMERGENCY_SELL
            # (exempt from cross-mode lock) may have reduced or zeroed
            # position while this SELL sat unfilled. If broker now shows
            # 0 shares we'd be restoring stops on a phantom position;
            # broker rejects → finalize bails → drain replays same math →
            # row stuck forever. Re-read position and skip / clip
            # accordingly.
            current_qty_raw = self._current_position_qty_for_finalize(symbol)
            # Broker reports the SIGNED position (negative for a short);
            # every comparison below is magnitude-only, so normalize once
            # here rather than abs()-ing at each use.
            current_qty = current_qty_raw if current_qty_raw is None else abs(current_qty_raw)
            if current_qty == 0:
                logger.info(
                    "%s on %s had no fill, but broker reports position=0 "
                    "— concurrent path fully exited; skipping restore",
                    order_word,
                    symbol,
                )
                self._cancel_stray_stops_on_flat(symbol, **side_kwargs)
                return True, []
            if current_qty is not None:
                total_spec_qty = sum(float(s.get("qty", 0) or 0) for s in cancelled_specs)
                if current_qty + 1e-6 < total_spec_qty:
                    # Concurrent SELL reduced position below original
                    # stop coverage. Restoring all specs would over-protect
                    # → broker rejects. Collapse to a single reprotect at
                    # the most-protective stop_price for the actual qty.
                    logger.warning(
                        "%s on %s had no fill, but broker position=%.4f "
                        "< original spec qty=%.4f — concurrent path reduced "
                        "position; collapsing restore to single reprotect",
                        order_word,
                        symbol,
                        current_qty,
                        total_spec_qty,
                    )
                    if not self._reprotect_residual_after_partial_sell(
                        symbol,
                        current_qty,
                        cancelled_specs,
                        **side_kwargs,
                    ):
                        if not from_drain:
                            self._persist_orphaned_protection_restore(
                                order_id,
                                symbol,
                                current_qty,
                                cancelled_specs,
                                wal_row_id=wal_row_id,
                                side=side,
                            )
                        return False, list(cancelled_specs)
                    return True, []
            try:
                # Drain replays may re-encounter specs that landed in a
                # prior pass; check_idempotency=from_drain prevents the
                # re-submit dupes that broke down on held_for_orders
                # before the audit fix.
                restored, failed_specs = self.broker._restore_stop_orders(
                    symbol,
                    cancelled_specs,
                    check_idempotency=from_drain,
                    **side_kwargs,
                )
                logger.info(
                    "%s on %s terminated with no fill (status=%s) — restored %d/%d original protective stop(s)",
                    order_word,
                    symbol,
                    status or "?",
                    restored,
                    len(cancelled_specs),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to restore stops for %s after no-fill %s: %s — persisting recovery intent",
                    symbol,
                    order_word,
                    exc,
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id,
                        symbol,
                        position_qty_before_sell,
                        cancelled_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(cancelled_specs)
            # PARTIAL restore is incomplete coverage — restoring 1 of 2
            # original stops still leaves the slice covered by the failed
            # spec naked. Codex r9: previously we only flagged 0 of N as
            # failure; now any partial-restore persists ONLY the failed
            # specs (not the originals — the ones that DID restore are
            # already alive at the broker, retrying would double-stack).
            if failed_specs:
                logger.warning(
                    "Restore for %s submitted %d/%d stops — %d failed; persisting failed spec(s) for retry",
                    symbol,
                    restored,
                    len(cancelled_specs),
                    len(failed_specs),
                )
                if not from_drain:
                    self._persist_orphaned_protection_restore(
                        order_id,
                        symbol,
                        position_qty_before_sell,
                        failed_specs,
                        wal_row_id=wal_row_id,
                        side=side,
                    )
                return False, list(failed_specs)
            return True, []

        computed_residual = position_qty_before_sell - fill_qty
        # Concurrent-SELL guard: same reasoning as the fill_qty<=0 branch.
        # cached `position_qty_before_sell - fill_qty` can over-state
        # residual if intra_check liquidated some shares while this SELL
        # was in flight. Clip to actual broker position. Magnitude-only,
        # same normalization as the fill_qty<=0 branch above.
        current_qty_raw = self._current_position_qty_for_finalize(symbol)
        current_qty = current_qty_raw if current_qty_raw is None else abs(current_qty_raw)
        if current_qty == 0:
            logger.info(
                "Finalize for %s: cached residual=%.4f but broker shows "
                "position=0 — concurrent path fully exited; skipping reprotect",
                symbol,
                computed_residual,
            )
            self._cancel_stray_stops_on_flat(symbol, **side_kwargs)
            return True, []
        if current_qty is not None and current_qty + 1e-6 < computed_residual:
            logger.warning(
                "Finalize for %s: clipping residual from %.4f to %.4f "
                "(broker position decreased — concurrent SELL took shares)",
                symbol,
                computed_residual,
                current_qty,
            )
            actual_residual = current_qty
        else:
            actual_residual = computed_residual
        if actual_residual <= 0:
            # Full exit — no residual to re-protect. NO stray-stop cleanup
            # here, deliberately (item 127(b) must fail CLOSED): the only
            # broker-CONFIRMED flat (`current_qty == 0`) already returned
            # above and did the cleanup there. Reaching this line means
            # `current_qty` is either None — the position read FAILED, so we
            # cannot confirm flat — or > 0 — the broker still reports shares
            # (a concurrent re-entry / scale-in) while cached math says
            # residual<=0. Cancelling a stop in either case would strip
            # protection off live-or-unconfirmed shares. Leave the stop
            # standing, exactly as main did.
            return True, []  # full exit — no residual to re-protect

        if not self._reprotect_residual_after_partial_sell(
            symbol,
            actual_residual,
            cancelled_specs,
            **side_kwargs,
        ):
            # Reprotect submit raised. Persist so a later session can retry.
            # Codex r9 #1: previously this just returned False without
            # persisting, and the SELL-path callers ignored that bool —
            # the recovery intent was silently lost.
            #
            # Persist the PRE-sell qty, not `actual_residual` (2026-07-16
            # audit): the drain replays this row through the same finalize
            # core, which recomputes `position_qty_before_sell - fill_qty`
            # from the SAME order. Passing the post-sell residual made the
            # replay subtract the fill twice — for a SELL that filled exactly
            # what it asked for, the recomputed residual hit 0, took the
            # "full exit — nothing to re-protect" early return, reported
            # success, and DELETED the row. Net effect: the residual position
            # stayed naked forever and the recovery intent was destroyed.
            # The drain's downward clip against the live broker position keeps
            # this correct even if a concurrent SELL took shares meanwhile.
            if not from_drain:
                self._persist_orphaned_protection_restore(
                    order_id,
                    symbol,
                    position_qty_before_sell,
                    cancelled_specs,
                    wal_row_id=wal_row_id,
                    side=side,
                )
            return False, list(cancelled_specs)
        return True, []

    def _cancel_stray_stops_on_flat(self, symbol: str, *, side: str = "sell") -> None:
        """Clear any protective stop left resting on a symbol this SELL/COVER
        just took FLAT.

        Board item 127(b), owner ruling 2026-09-25: a forced/emergency exit
        must fire immediately and never wait on stop-work, so a concurrent
        stop-repair can re-add a protective stop inside the cancel-then-sell
        window. That stop is not in this finalize's ``cancelled_specs`` (it
        was placed after the pre-sell snapshot), the reprotect path skips it
        on a full exit, and ``_reconcile_stop_coverage`` never inspects a
        flat symbol — so it would rest forever and could later elect into an
        unintended short. This is the cheap cleanup that replaces the
        rejected lock-wait: no lock, no timeout, no new number, best-effort,
        and never fatal to the exit that already succeeded.
        """
        try:
            self.broker.cancel_stray_protective_stops(symbol, side=side)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stray-stop cleanup after full exit of %s failed: %s — a "
                "protective stop may still rest on the flat position; the "
                "operator should confirm it is gone",
                symbol,
                exc,
            )

    def _write_ahead_protection_restore(
        self,
        symbol: str,
        position_qty_before_sell: float,
        specs: list[dict],
        *,
        side: str = "sell",
    ) -> int | None:
        """audit F1: persist the protection-restore intent.

        The recovery-intent persist used to live only inside finalize's
        bail branches, which run AFTER the whole cancel -> submit ->
        wait -> finalize loop. A SIGKILL / reboot / `timeout
        --kill-after` anywhere in that window left the broker with no
        stop and the DB with no recovery row — the position rode naked
        indefinitely (the in-process try/except does NOT survive a
        process kill).

        Called by _cancel_stops_with_write_ahead AFTER snapshotting the
        stops but BEFORE cancelling them (audit F1 review #1), so the
        sentinel row is durable before any broker mutation — there is no
        "stops cancelled but nothing recorded" window. The row is
        flipped to the real order id by finalize's bail and deleted once
        finalize confirms coverage. Returns the row id (to thread
        through), or None when there was nothing to protect or the DB
        write failed (no worse than the pre-F1 behaviour — logged).

        ``side`` (Stage 3, shorts) — the closing order's side, passed
        straight through to ``insert_pending_protection_restore``: 'sell'
        (default) for a long being sold, 'buy' for a short being covered.
        This is the REAL side, known here at write time — recorded so the
        drain path (``_drain_pending_protection_restores``) doesn't have
        to guess it back from live broker state later.
        """
        return write_ahead_restore_row(
            self.db,
            logger,
            _WAL_SELL_SENTINEL,
            symbol,
            position_qty_before_sell,
            specs,
            side,
        )

    def _restore_after_unconfirmed_sell(
        self,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """drain handler for a write-ahead row whose SELL was never
        confirmed (sentinel sell_order_id) — a crash between
        cancel_protective_stops() and recording the SELL.

        There is no SELL order to query, and the broker may or may not
        have received/filled a SELL (crash could land before submit, or
        after submit but before we stored the id). The only trustworthy
        signal is the broker's CURRENT position. Conservative:
          - position 0  → SELL filled / position gone → nothing to
            protect (success).
          - position unknown → don't guess; leave the row.
          - position < original spec coverage → collapse to one
            most-protective stop on the actual shares.
          - position intact → restore the original specs idempotently
            (a prior inline reject-restore or partial drain may have
            already replaced some).
        Returns (ok, retry_specs) like the finalize core.

        ``side`` — 'sell' (default) for a long, 'buy' for a short's cover;
        see ``_submit_protected_sell``. The caller (the drain loop, via
        ``_resolve_wal_row_side``) prefers this row's own persisted `side`
        column (Stage 3) and only derives it from LIVE broker position
        sign as a fallback for a row written before that column existed.
        """
        if not cancelled_specs:
            return True, []
        side_kwargs = {} if side == "sell" else {"side": side}
        current_raw = self._current_position_qty_for_finalize(symbol)
        # Magnitude-only from here — broker reports the SIGNED qty
        # (negative for a short); `side` (not the sign) drives which stop
        # side gets touched.
        current = current_raw if current_raw is None else abs(current_raw)
        if current == 0:
            logger.info(
                "WAL drain: %s now flat — SELL must have filled / position gone; no protection to restore",
                symbol,
            )
            return True, []
        if current is None:
            logger.warning(
                "WAL drain: %s position unknown (broker error) — leaving row for next session",
                symbol,
            )
            return False, list(cancelled_specs)
        total_spec_qty = sum(float(s.get("qty", 0) or 0) for s in cancelled_specs)
        if current + 1e-6 < total_spec_qty:
            logger.warning(
                "WAL drain: %s position=%.4f < original spec qty=%.4f "
                "(SELL partially filled before crash) — collapsing to a "
                "single most-protective stop",
                symbol,
                current,
                total_spec_qty,
            )
            if not self._reprotect_residual_after_partial_sell(
                symbol,
                current,
                cancelled_specs,
                **side_kwargs,
            ):
                return False, list(cancelled_specs)
            return True, []
        try:
            restored, failed = self.broker._restore_stop_orders(
                symbol,
                cancelled_specs,
                check_idempotency=True,
                **side_kwargs,
            )
        except Exception as exc:
            logger.warning(
                "WAL drain: restore raised for %s: %s — leaving row",
                symbol,
                exc,
            )
            return False, list(cancelled_specs)
        if failed:
            logger.warning(
                "WAL drain: %s restored %d/%d stop(s) — %d still failing",
                symbol,
                restored,
                len(cancelled_specs),
                len(failed),
            )
            return False, list(failed)
        logger.info(
            "WAL drain: %s restored %d original protective stop(s)",
            symbol,
            restored,
        )
        return True, []

    def _persist_orphaned_protection_restore(
        self,
        order_id: str,
        symbol: str,
        position_qty_before_sell: float,
        cancelled_specs: list[dict],
        *,
        wal_row_id: int | None = None,
        side: str = "sell",
    ) -> dict | None:
        """Persist (or update) a protection-restore recovery intent.

        Used by the bail branches of the finalize core: cancel raised,
        OR cancel was accepted but didn't converge to terminal in 5s, OR
        a restore/reprotect failed. The position is sitting with the
        original stops cancelled and a maybe-still-live SELL — neither
        restoring nor reprotecting is safe right now. Record the intent
        and let the next session's drain pass act once broker state
        settles.

        audit F1: when ``wal_row_id`` is set there is already a
        write-ahead row (inserted BEFORE cancel_protective_stops) — flip
        it to the real order id + final specs via UPDATE instead of
        INSERTing a duplicate. Without a wal_row_id (legacy callers /
        tests) it INSERTs as before. Best-effort — DB failure logs but
        never propagates (the immediate path already had no good
        option).

        ``side`` (Stage 3, shorts) — the closing order's side ('sell' for
        a long, 'buy' for a short's cover), passed through to the
        DB layer either way: on UPDATE it re-affirms the value the
        write-ahead row was created with (belt-and-suspenders — the
        write-ahead insert already set it correctly); on INSERT (the
        legacy-caller / no-prior-row path) it's the only place this row
        will ever get a side recorded.

        Returns ``None`` when the intent is persisted (the drain owns it) or
        when, with the write-ahead row ALSO unwritable, the immediate
        re-protect through the broker landed. Returns the broker's
        ``naked`` payload when that re-protect failed too: the position
        has no stop and no recovery row, and the caller reports coverage
        unconfirmed (docs/OUTCOME.md: protections fail closed, never
        "logged and moved on").
        """
        if not cancelled_specs:
            return None
        import json as _json

        specs_json = _json.dumps(cancelled_specs)
        try:
            if wal_row_id is not None:
                self.db.update_pending_protection_restore(
                    wal_row_id,
                    sell_order_id=order_id,
                    position_qty_before_sell=position_qty_before_sell,
                    specs_json=specs_json,
                    side=side,
                )
                logger.info(
                    "WAL: updated protection-restore row %d for %s "
                    "(order %s, %d cancelled stop(s)) — drain retries next "
                    "session",
                    wal_row_id,
                    symbol,
                    order_id,
                    len(cancelled_specs),
                )
            else:
                self.db.insert_pending_protection_restore(
                    symbol=symbol,
                    sell_order_id=order_id,
                    position_qty_before_sell=position_qty_before_sell,
                    specs_json=specs_json,
                    side=side,
                )
                logger.info(
                    "Persisted orphaned protection-restore for %s (order %s, "
                    "%d cancelled stop(s)) — drain pass will retry next session",
                    symbol,
                    order_id,
                    len(cancelled_specs),
                )
        except Exception as exc:
            record_guarded_pass(
                (self.db, self.broker),
                "sell_finalization.persist_orphaned_protection_restore",
                exc,
                log=logger,
                context={
                    "symbol": symbol,
                    "order": order_id,
                    "effect": "no recovery row; re-protecting now at the most protective cancelled stop",
                },
            )
            return self._reprotect_after_lost_recovery_row(symbol, position_qty_before_sell, cancelled_specs, side)
        return None

    def _reprotect_after_lost_recovery_row(
        self, symbol: str, position_qty_before_sell: float, cancelled_specs: list[dict], side: str
    ) -> dict | None:
        """Restore failed AND the write-ahead row could not be written: the
        only thing left that can protect the shares is a stop placed NOW.
        Sized to the live broker position (the pre-sale quantity when the
        broker cannot be read — the broker rejects an over-sized stop and
        the payload then says naked), at the most protective cancelled
        stop: the highest for a long's SELL stop, the lowest for a short's
        BUY stop."""
        prices = [float(spec.get("stop_price") or 0) for spec in cancelled_specs if spec.get("stop_price")]
        if not prices:
            return None
        intended = max(prices) if side == "sell" else min(prices)
        current_raw = self._current_position_qty_for_finalize(symbol)
        qty = abs(float(current_raw)) if current_raw is not None else float(position_qty_before_sell or 0)
        if qty <= 0:
            return None
        try:
            payload = self.broker._reprotect_or_naked(symbol, qty, side, intended, cancelled_specs)
            record_guarded_pass((self.db, self.broker), "sell_finalization.reprotect_after_lost_recovery_row")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                (self.db, self.broker),
                "sell_finalization.reprotect_after_lost_recovery_row",
                exc,
                log=logger,
                context={"symbol": symbol, "effect": "no stop placed; reported as naked"},
            )
            return {"symbol": symbol, "status": "naked", "amend_status": "naked", "legs": []}
        if isinstance(payload, dict) and payload.get("status") == "naked":
            logger.error(
                "%s is UNPROTECTED: restore failed, the recovery row could not be written and the "
                "immediate re-protect at $%.4f failed — manual intervention required",
                symbol,
                intended,
            )
        return payload

    def _derive_close_side_for_drain(self, symbol: str) -> str | None:
        """Which stop side an orphaned WAL row needs, derived from LIVE
        broker truth rather than the row itself.

        Stage 3 (shorts): ``pending_protection_restores`` NOW carries a
        persisted ``side`` column (see ``insert_pending_protection_restore``
        / ``_write_ahead_protection_restore``) written at the moment the
        row is created, by whoever is closing the position and therefore
        already knows which side it is. This function is no longer the
        primary source of truth — see ``_resolve_wal_row_side``, which
        prefers the row's own persisted value and calls this ONLY as the
        fallback for a row written before the migration (persisted
        ``side IS NULL``). For those legacy rows this is still the only
        signal available: reading the broker's CURRENT signed position for
        the symbol, fresh (not trusted from whenever the row was written,
        since it can be arbitrarily stale by the time drain gets to it).

        Returns 'sell' / 'buy' when the position is currently held one way
        or the other. Returns None both when the position can't be read
        (broker error — the caller must NOT default to 'sell': that's
        exactly the "guess a side" the design review forbids, and for a
        short's row it would try to restore a SELL stop on a position that
        has no shares to back it) and when the position is already flat
        (0) — the caller's downstream restore/reprotect call independently
        re-checks flatness before ever touching a side-dependent broker
        call, so which side an already-flat symbol "would have" used is
        moot, and returning a value here would look like a real answer.
        """
        raw = self._current_position_qty_for_finalize(symbol)
        if raw is None or raw == 0:
            return None
        return "buy" if raw < 0 else "sell"

    def _resolve_wal_row_side(self, row: dict, symbol: str) -> dict:
        """The ``side`` kwargs (``{}`` or ``{"side": "buy"}``) a drained
        WAL row needs, preferring the row's OWN persisted value.

        Stage 3 (shorts): every row written after the ``side`` column
        migration carries the real answer, recorded at write time by
        whoever created it — no broker lookup, no guessing. A row written
        BEFORE the migration carries ``side IS NULL``; for those, and only
        those, this degrades to the pre-migration behaviour — deriving the
        side from the broker's live position via
        ``_derive_close_side_for_drain`` — logged so the legacy fallback is
        visible in operator logs rather than silent.
        """
        persisted = str(row.get("side") or "").strip().lower()
        if persisted in ("buy", "sell"):
            return {} if persisted == "sell" else {"side": "buy"}
        logger.info(
            "WAL drain: row for %s has no persisted side (written before "
            "the Stage 3 side-column migration) — falling back to the "
            "live-broker-derived side, same as pre-migration behaviour",
            symbol,
        )
        return {"side": "buy"} if self._derive_close_side_for_drain(symbol) == "buy" else {}
