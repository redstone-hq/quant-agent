"""src.protection.protected_sell -- the protected sell and its write-ahead stop cancel.

Bodies moved verbatim from src/pipeline_protection.py (`ProtectionMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body are the HOST's shim, handed in, never a body this part owns.
Host attributes a body reads with a defaulted getattr or ASSIGNS go through `state`
(a live get/set view the shim hands in), never a copy.
"""

import logging

from src.protection.trim_amend import refusal_reason, trim_book_call
from src.quantities import ORDER_QTY_DECIMALS
from src.sentinel.guarded import record_guarded_pass

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class ProtectedSell:
    """The protected sell and its write-ahead stop cancel; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        broker=None,
        db=None,
        alert_owner_exit_declined=None,
        order_accepted=None,
        write_ahead_protection_restore=None,
        cancel_stops_with_write_ahead=None,
        state=None,
        trim_book=None,
    ) -> None:
        self.broker = broker
        self._trim_book = trim_book  # the broker CLASS (host-injected): src.protection imports no src.execution
        self.db = db
        self._alert_owner_exit_declined = alert_owner_exit_declined
        self._order_accepted = order_accepted
        self._write_ahead_protection_restore = write_ahead_protection_restore
        if cancel_stops_with_write_ahead is not None:
            self._cancel_stops_with_write_ahead = cancel_stops_with_write_ahead  # else: this part's own body
        self._state = state  # live get/set view of the host's attributes this part reads and assigns

    @property
    def _last_stop_clear_refusal(self):
        return self._state.get("_last_stop_clear_refusal")

    @_last_stop_clear_refusal.setter
    def _last_stop_clear_refusal(self, value) -> None:
        self._state.set("_last_stop_clear_refusal", value)

    def _submit_protected_sell(
        self,
        *,
        symbol: str,
        qty: float,
        limit_price: float | None,
        reference_price: float,
        position_qty_before_sell: float,
        label: str,
        side: str = "sell",
        escalate_to_market_on_reject: bool = False,
        book=None,
    ) -> tuple[dict, dict] | None:
        """Head half of the SELL/COVER discipline: clear protective stops
        (write-ahead) → submit the order → guarantee stops are restored if
        the order never reaches the broker.

        Returns ``(order, pending_protection)`` on broker acceptance, or
        ``None`` when the symbol must be skipped (including a sale PUT OFF
        because a cancelled leg was not confirmed dead in the wait: then
        ``_last_stop_clear_refusal`` names it and the deciding run's seam,
        src/protection/put_off_sales.py, re-checks it; ``book(order, qty)``, when
        given, is the caller's own record of a sent sale, which that re-check
        writes if it sends the sale later) — stop-clear failed, the
        submit raised, or the broker rejected. In every skip case the
        protective stops are already restored (or were never cancelled), so
        the caller just ``continue``s with no naked-position window.

        The caller owns qty/price selection, ``insert_trade``, the orders list,
        and any accounting (projected_proceeds, sell_order_ids); this owns the
        cancel → submit → accept → restore-on-failure invariant so no SELL path
        can silently skip a step (CLAUDE.md's longest convention). ``label`` is
        both the order's action tag and the log context (e.g. 'EMERGENCY_SELL',
        'FORCE_DELEVER', 'SELL').

        ``position_qty_before_sell`` is the FULL held qty (drives the WAL +
        finalize residual math); ``qty`` is the order quantity (may be a
        partial / reduce / trim). Both are always non-negative magnitudes —
        never the broker's signed position qty — so every comparison and
        every arithmetic step downstream (WAL specs, fill_qty, residual math)
        stays identical in shape whether this is closing a long or covering
        a short.

        ``side`` (forced-close support, added alongside the emergency-
        liquidation short-close gap fix): the CLOSING order's side —
        ``'sell'`` (default, unchanged for every pre-existing caller — none
        of them pass this) flattens a long; ``'buy'`` covers a short. It
        doubles as the STOP order's own side to cancel/restore, because a
        long's protective stop and its closing order are BOTH 'sell', and a
        short's protective stop and its closing order (a BUY-to-cover) are
        BOTH 'buy' — one parameter, not two, so there's no way for the
        closing side and the stop-clearing side to disagree.
        """
        # audit F1 review #1: snapshot → persist WAL → cancel, so the recovery
        # row is durable BEFORE any broker mutation.
        #
        # Full exits also cancel the day's resting entry BUY for the SAME
        # symbol first (audit round 2): a still-working DAY entry limit would
        # silently re-open a position the reviewer/breaker just decided to
        # close — and can trip Alpaca's wash-trade rejection of this SELL.
        # Best-effort + symbol-scoped; partial trims (REDUCE, PARTIAL_SELL,
        # TAKE_PROFIT, SWEEP_SELL) keep their entries — trimming isn't exiting.
        # EMERGENCY_COVER is the short-side twin of EMERGENCY_SELL added
        # here: it runs the same entry-order cancel a long exit does.
        # `cancel_open_entry_orders` (src/execution/broker.py) now cancels
        # a resting entry order on EITHER side — BUY-to-open-long or
        # SELL-to-open-short — so an EMERGENCY_COVER here also stops a
        # still-live SHORT entry from re-opening the position it just
        # covered (previously flagged, fixed alongside the review-path
        # COVER gap).
        full_exit = self._begin_sale(label, position_qty_before_sell, qty)
        if label in ("SELL", "EMERGENCY_SELL", "EMERGENCY_COVER", "FORCE_DELEVER"):
            try:
                # Kept as legs: the sale waits for these cancels too.
                self._state.set("_entry_legs", _legs_of(self.broker.cancel_open_entry_orders(symbol=symbol)))
                record_guarded_pass((self.broker, self.db), "protected_sell.entry_cancel", context={"symbol": symbol})
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(
                    (self.broker, self.db), "protected_sell.entry_cancel", exc, context={"symbol": symbol}
                )
                logger.warning("%s: entry-order cancel failed for %s: %s", label, symbol, exc)
                # Fail closed (docs/OUTCOME.md): whether an entry still rests
                # is UNKNOWN, so the sale is PUT OFF before any stop is touched
                # — the deciding run re-runs the whole protected sale (entry
                # cancel included) once it re-checks. Same reason and path as
                # an unreadable cancelled leg.
                from src.execution.broker_parts.cancel_confirm import CANCEL_READ_FAILED, LegSettlement

                self._state.set("_leg_settlement", LegSettlement(reason=CANCEL_READ_FAILED))
                self._put_off_if_unconfirmed(
                    {
                        "symbol": symbol,
                        "qty": abs(float(qty)),
                        "limit_price": limit_price,
                        "reference_price": reference_price,
                        "position_qty_before_sell": position_qty_before_sell,
                        "label": label,
                        "side": side,
                        "escalate_to_market_on_reject": escalate_to_market_on_reject,
                        "full_exit": full_exit,
                        "stop_specs": [],
                        "wal_row_id": None,
                        "kept_leg": None,
                    },
                    book,
                )
                return None
        stop_side_kwargs = {} if side == "sell" else {"side": side}
        ok, stop_specs, wal_row_id, kept_leg = self._clear_stops_for(label, symbol, qty, position_qty_before_sell, side)
        if not ok:
            # WHY THE DESK STILL DECLINES THE EXIT, and why it is no longer
            # silent about it.
            #
            # Rejection is the MEASURED outcome of selling into a resting
            # protective stop on this desk: 2026-04-25, AMZN, a REDUCE
            # rejected with the trail stop holding all 51 shares. An
            # OVERSOLD or reversed position is evidenced by nothing — no
            # entry in `docs/INCIDENT_HISTORY.md`, none in the git log, and
            # Alpaca's documented model is reservation then rejection
            # (`Position.qty_available` is "total shares minus open orders
            # / locked"). But that documentation covers longs; nothing
            # fetched covers a BUY-to-cover against a resting BUY stop, and
            # `docs/OUTCOME.md` makes broker protections fail CLOSED. An
            # unbounded, unmeasured harm on one side beats a bounded,
            # measured one on the other, so the desk declines.
            #
            # What was actually wrong was never the decline — it was that
            # the decline was silent and that this line asserted ONE cause
            # for two different states. The listing failing means nobody
            # knows whether a stop is resting; the cancel rolling back
            # means one verifiably is. Only the second supports the
            # held_for_orders claim, and this said it for both.
            refusal = getattr(self, "_last_stop_clear_refusal", "") or "unknown"
            why = _OWN_REASONS.get(refusal) or refusal_reason(refusal, side)
            logger.warning("%s: skipping %s — %s", label, symbol, why)
            # A skipped exit reaches the owner BY SYMBOL, on the same path
            # and the same once-per-symbol-per-day claim as the unreadable
            # stop itself. Five call sites upstream do `if sale is None:
            # continue`, so without this the desk decides not to leave a
            # position and nobody is told — and after the account-level
            # halt was removed, one of those call sites is the de-levering
            # ladder, which would then trim less than it reports.
            self._alert_owner_exit_declined(symbol, side=side, why=why)
            return None
        sale = {
            "symbol": symbol,
            "qty": abs(float(qty)),
            "limit_price": limit_price,
            "reference_price": reference_price,
            "position_qty_before_sell": position_qty_before_sell,
            "label": label,
            "side": side,
            "escalate_to_market_on_reject": escalate_to_market_on_reject,
            "full_exit": full_exit,
            "stop_specs": stop_specs,
            "wal_row_id": wal_row_id,
            "kept_leg": kept_leg,
        }
        if self._put_off_if_unconfirmed(sale, book):
            return None
        return self._send_cleared(**sale)

    def _put_back(self, sale: dict) -> bool:
        """Put a sale's cleared protection back: cancelled legs up to what is held now, a trim's shrunk stop grown back.

        False when the cover is short of held (the broker part logs it for the health report),
        or a trim's book did not settle covered (a kept leg gone, or not grown back)."""
        ok = True
        if sale["stop_specs"]:
            covered = trim_book_call(self, "cover_to_held", sale["symbol"], sale["stop_specs"], side=sale["side"])
            ok = covered is not False
        if sale["kept_leg"] is not None:
            status = trim_book_call(self, "settle_trim_book", sale["symbol"], sale["side"])
            ok = ok and status in ("accepted", "flat")
        return ok

    def _resale_size(self, sale: dict, settlement) -> tuple[float, float, str]:
        """A put-off sale's ``(qty, position_qty_before_sell, refusal)`` once its legs settled.

        The pass's snapshot plus every leg's fill during the cancel is what is
        held now, so the re-sale is decided on that, by the pass's own rules
        (`sale_refusal`): a full exit sells all of it, a trim keeps its size,
        and a trim whose stop filled in part is refused. ``refusal`` is the
        owner wording, empty when the sale may go."""
        held = _q(abs(float(sale["position_qty_before_sell"])) + settlement.closable_change(sale["side"]))
        if held <= 0:
            return 0.0, 0.0, _OWN_REASONS["stop_fired"]
        if not sale["full_exit"] and _q(settlement.filled_qty) > 0:
            return 0.0, held, _OWN_REASONS["stop_filled_in_part"]
        return (held if sale["full_exit"] else abs(float(sale["qty"]))), held, ""

    def _send_cleared(
        self,
        *,
        symbol,
        qty,
        limit_price,
        reference_price,
        position_qty_before_sell,
        label,
        side,
        escalate_to_market_on_reject,
        full_exit,
        stop_specs,
        wal_row_id,
        kept_leg,
    ):
        """Tail half: every cancelled leg is terminal, so size to held and send, or put the stops back.

        Called by the pass once its legs settled, and by the deciding run's
        re-check (src/protection/put_off_sales.py) for a sale it put off."""

        def _put_back_protection() -> None:
            # Cancelled legs are restored; a stop shrunk in place for a trim
            # is grown back by the quantity invariant (nothing was sold).
            self._put_back({"symbol": symbol, "side": side, "stop_specs": stop_specs, "kept_leg": kept_leg})

        # The caller sized `qty` from a position read taken BEFORE the stops
        # were cleared. A resting stop can fill (fully or partly) in between,
        # so the order is re-sized against a FRESH read taken now that no stop
        # can fill any more: selling the old size would open a short (and a
        # cover would open a long). Nothing held -> no order at all.
        sized = self._size_to_held(
            label, symbol, side, qty, position_qty_before_sell, wal_row_id, _put_back_protection, full_exit=full_exit
        )
        if sized is None:
            return None
        qty, position_qty_before_sell = sized

        try:
            order = self.broker.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=limit_price,
                reference_price=reference_price,
            )
            record_guarded_pass((self.broker, self.db), "protected_sell.submit", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass((self.broker, self.db), "protected_sell.submit", exc, context={"symbol": symbol})
            # Submit raised AFTER the desk read the order back by its own key
            # and did not find it (a found order comes back as placed and
            # takes the accepted path below): the outcome is UNKNOWN, not
            # "nothing sold". The put-back reads what is held NOW and covers
            # only that (`cover_to_held`), so a sale that did land leaves no
            # stop over shares already gone.
            self._submit_unknown(label, symbol, side, exc, _put_back_protection)
            return None
        if not self._order_accepted(order, symbol, side):
            # A MUST-FILL emergency de-lever cannot afford to skip a name here.
            # A wide-spread live quote (e.g. a LULD halt-reopen or a thin /
            # inverse name in a fast market) can make the marketable limit
            # deviate >20% from the mid, so the broker's own fat-finger guard
            # returns `rejected_outlier`. Skipping the name would re-open
            # exactly the over-ceiling / uncleared-deficit miss the de-lever
            # exists to kill. So the de-lever callers pass
            # `escalate_to_market_on_reject=True`: on a NON-accept of the
            # marketable LIMIT, escalate once to a MARKET order — the
            # guaranteed fill, which carries no limit and so skips the guard.
            # Every other caller keeps the prior skip-on-reject behaviour
            # (default False). The stops are still cancelled and the rejected
            # order did not fill, so the position is intact; submit the market
            # order against the SAME cancelled stops (no restore in between)
            # and let finalize rebuild coverage on its fill, exactly as for the
            # limit.
            if escalate_to_market_on_reject and limit_price is not None:
                rejected_status = order.get("status") if isinstance(order, dict) else order
                logger.warning(
                    "%s: marketable-limit exit for %s was not accepted (%s) — "
                    "escalating to a MARKET order (guaranteed fill).",
                    label,
                    symbol,
                    rejected_status,
                )
                try:
                    order = self.broker.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side=side,
                        limit_price=None,
                        reference_price=reference_price,
                    )
                    record_guarded_pass(
                        (self.broker, self.db), "protected_sell.market_escalation", context={"symbol": symbol}
                    )
                except Exception as exc:  # noqa: BLE001
                    record_guarded_pass(
                        (self.broker, self.db), "protected_sell.market_escalation", exc, context={"symbol": symbol}
                    )
                    self._submit_unknown(label, symbol, side, exc, _put_back_protection)
                    return None
            if not self._order_accepted(order, symbol, side):
                # Broker rejected (no escalation, or the market order itself was
                # not accepted) — restore the stops we just cancelled.
                _put_back_protection()
                return None
        # audit F5: tag the order dict so the notifier's intervention banner +
        # inline action labels fire (broker.submit_order returns no 'action').
        if isinstance(order, dict):
            order.setdefault("action", label)
        # Defer the reprotect/restore decision to finalize (after the wait) —
        # an accepted limit can still cancel/expire without filling, in which
        # case the FULL original protection is what the position needs.
        prot = {
            "order_id": order["id"],
            "symbol": symbol,
            "position_qty_before_sell": position_qty_before_sell,
            # How much this order asked the broker to shed. Needed to net an
            # order still working out of a later gross re-measure — see
            # `_register_exit_settlement`.
            "submitted_qty": abs(float(qty or 0.0)),
            "specs": stop_specs,
            "wal_row_id": wal_row_id,
            "side": side,
        }
        if kept_leg is not None:
            # The shrunk stop is LIVE (new broker id after the PATCH); the
            # finalizer settles the book with the quantity invariant instead
            # of restoring a stop over it.
            prot["kept_leg"] = kept_leg
        return order, prot

    def _submit_unknown(self, label, symbol, side, exc, put_back) -> None:
        """A submit whose outcome is unknown: cover what is held now, and tell the owner."""
        put_back()
        self._last_stop_clear_refusal = "submit_unknown"
        why = _OWN_REASONS["submit_unknown"]
        logger.error("%s: %s — %s (%s)", label, symbol, why, exc)
        self._alert_owner_exit_declined(symbol, side=side, why=why)

    def _clear_stops_for(self, label, symbol, qty, position_qty_before_sell, side):
        """A trim KEEPS shares, so its stop is shrunk in place (whole-share
        PATCH, confirmed) rather than cancelled; only a DAY sliver the sell
        needs is cancelled (src/execution/broker_parts/trim_book.py, via the broker). Every full exit
        keeps cancel-all. Returns ``(ok, cancelled_specs, wal_row_id, kept_leg)``."""
        if trim_book_call(self, "trim_keeps_shares", label, position_qty_before_sell, qty):
            return trim_book_call(
                self,
                "clear_stops_for_trim",
                symbol,
                position_qty_before_sell,
                qty,
                side=side,
                cancel_specs_with_write_ahead=self._cancel_specs_with_write_ahead,
                state=self._state,
            )
        stop_side_kwargs = {} if side == "sell" else {"side": side}
        ok, specs, wal_row_id = self._cancel_stops_with_write_ahead(
            symbol, position_qty_before_sell, **stop_side_kwargs
        )
        return ok, specs, wal_row_id, None

    def _cancel_stops_with_write_ahead(
        self,
        symbol: str,
        position_qty_before_sell: float,
        *,
        side: str = "sell",
    ) -> tuple[bool, list[dict], int | None]:
        """Snapshot protective stops -> persist WAL recovery intent ->
        THEN cancel the stops. audit F1 review #1: true write-ahead.

        The previous F1 fix wrote the WAL row AFTER
        cancel_protective_stops, which had already cancelled the stops
        at the broker — a kill inside / just after that call left a
        naked position with no recovery intent. Ordering snapshot →
        persist → cancel guarantees the recovery row is durable BEFORE
        any broker mutation. A kill before the cancel is harmless (stops
        still live; drain's sentinel path re-reads the position and the
        idempotent restore is a no-op). A kill during/after the cancel
        is recoverable from the row.

        ``side`` is the STOP order's own side — 'sell' (default, byte-
        identical to every call site before shorts existed) snapshots the
        SELL stops protecting a long; 'buy' snapshots the BUY stops
        protecting a short. Passed through unchanged to
        ``snapshot_protective_stops``.

        Returns ``(ok, specs, wal_row_id)``. ``ok=False`` ⇒ skip the
        SELL: either the snapshot failed, or the cancel failed and was
        rolled back (position still protected, SELL would be rejected on
        held_for_orders anyway). When there were no stops to begin with,
        returns ``(True, [], None)`` — nothing to protect, SELL proceeds.
        """
        snapshot_kwargs = {} if side == "sell" else {"side": side}
        ok, specs = self.broker.snapshot_protective_stops(symbol, **snapshot_kwargs)
        if not ok:
            # STATE ONE of the two `ok=False` means: the broker's order
            # listing failed (already retried inside the snapshot), so
            # whether a stop is resting is UNKNOWN. Distinguished from
            # state two below, because the two states support different
            # statements and the log line used to make the same one for
            # both. Stamped so the caller can say only what is established
            # and can page the owner by symbol.
            self._last_stop_clear_refusal = "unreadable"
            return False, [], None
        if not specs:
            return True, [], None
        return self._cancel_specs_with_write_ahead(symbol, position_qty_before_sell, specs, side=side)

    def _cancel_specs_with_write_ahead(
        self,
        symbol: str,
        position_qty_before_sell: float,
        specs: list[dict],
        *,
        side: str = "sell",
    ) -> tuple[bool, list[dict], int | None]:
        """WAL row for `specs`, then cancel exactly those legs. The tail of
        `_cancel_stops_with_write_ahead`, split out so a trim can cancel a
        DAY sliver alone while its whole-share leg stays live."""
        wal_row_id = self._write_ahead_protection_restore(
            symbol,
            position_qty_before_sell,
            specs,
            side=side,
        )
        cancel = self.broker.cancel_snapshotted_stops(symbol, specs)
        if not cancel.cleared:
            # STATE THREE: rollback failed, shares naked NOW, WAL row is the repair.
            from src.stop_cancel_outcome import keep_lost_coverage_row

            if cancel.coverage_shrank and keep_lost_coverage_row(self, symbol, wal_row_id, cancel, logger):
                return False, [], None
            if wal_row_id is not None:
                try:
                    self.db.delete_pending_protection_restore(wal_row_id)
                    record_guarded_pass(
                        (self.broker, self.db), "protected_sell.wal_discharge", context={"symbol": symbol}
                    )
                except Exception as exc:  # noqa: BLE001
                    record_guarded_pass(
                        (self.broker, self.db), "protected_sell.wal_discharge", exc, context={"symbol": symbol}
                    )
                    logger.warning(
                        "WAL: failed to discharge row %d after cancel "
                        "rollback for %s: %s (drain will idempotently "
                        "no-op it)",
                        wal_row_id,
                        symbol,
                        exc,
                    )
            # The stops are verified resting, so "the broker would reject
            # the SELL on held_for_orders" is an evidenced statement here —
            # measured live 2026-04-25 on AMZN, where a REDUCE was rejected
            # with the trail stop holding all 51 shares.
            detail = getattr(cancel, "detail", "")
            gone = isinstance(detail, str) and detail.startswith("position_gone")
            # A failed cancel on an empty position is a stop that FILLED: say
            # so, rather than claim a stop is still resting (nothing was rolled back).
            self._last_stop_clear_refusal = "stop_fired" if gone else "cancel_rolled_back"
            return False, [], None
        self._last_stop_clear_refusal = ""
        # Wait for the broker to confirm every cancel before anything sells:
        # these stops and the entry / leftover orders, one shared deadline.
        legs = [dict(x, side=side, kind="stop") for x in specs if x.get("id")]
        legs += self._held_legs("_entry_legs") or []
        self._state.set("_leg_settlement", trim_book_call(self, "settle_legs", legs))
        return True, specs, wal_row_id

    def _held_legs(self, name: str):
        """This sale's per-call state (cancelled legs, their settlement); None before a sale set it."""
        try:
            return self._state.get(name)
        except AttributeError:
            return None

    def _begin_sale(self, label: str, position_qty_before_sell: float, qty: float) -> bool:  # noqa: ARG002
        """Reset this call's leg settlement; True when the sale is a full exit."""
        self._state.set("_leg_settlement", None)
        self._state.set("_entry_legs", [])
        return _q(abs(float(qty))) >= _q(abs(float(position_qty_before_sell)))

    def _put_off_if_unconfirmed(self, sale: dict, book=None) -> bool:
        """True when a cancelled leg is not confirmed dead inside the pass's wait: the sale is PUT OFF.

        The shares may still be held by a dying stop: a SELL now is refused
        (403 40310000, ETN and NET 13 Oct) and a new stop is refused too.
        Nothing is sent and nothing is put back this pass. The write-ahead
        restore row stays, so the restore drain puts the stop back once the
        cancel is terminal; the run that decided re-checks the sale before it
        ends (src/protection/put_off_sales.py). Covers a leg still live or
        `replaced` (cancel_unconfirmed, incl. a stop part-filled and still
        working) and an unreadable leg (cancel_read_failed)."""
        settlement = self._held_legs("_leg_settlement")
        if settlement is None and self._held_legs("_entry_legs"):
            # No stop was cancelled: the entry / leftover order cancels alone.
            settlement = trim_book_call(self, "settle_legs", self._held_legs("_entry_legs"))
            self._state.set("_leg_settlement", settlement)
        if settlement is None or settlement.settled:
            return False
        from src.protection import put_off_sales

        legs = [dict(x, side=sale["side"], kind="stop") for x in sale["stop_specs"] or [] if x.get("id")]
        legs += self._held_legs("_entry_legs") or []
        self._last_stop_clear_refusal = settlement.reason
        why = _OWN_REASONS[settlement.reason]
        logger.warning("%s: no order for %s this pass — %s", sale["label"], sale["symbol"], why)
        record = put_off_sales.PutOff(sale=dict(sale), legs=legs, reason=settlement.reason, seller=self, book=book)
        if not put_off_sales.put_off(record):
            # No run owns it (no seam is open): nothing will re-check it, so say so now.
            why = put_off_sales.not_sold_why(None, settlement.reason)
            self._alert_owner_exit_declined(sale["symbol"], side=sale["side"], why=why)
        return True

    def _size_to_held(self, label, symbol, side, qty, position_qty_before_sell, wal_row_id, put_back, *, full_exit):
        """``(qty, position_qty_before_sell)`` from a fresh read, or None when no order goes.

        Exactly the intended size or nothing: a full exit sells all that is
        held; a trim sells its own size. A shortfall is never cut down to a
        different size; it is a per-symbol reason, and the stops go back."""
        read = self._closable_now(symbol, side)
        if read is None:
            put_back()
            self._last_stop_clear_refusal = "position_unreadable"
            why = _OWN_REASONS["position_unreadable"]
            logger.warning("%s: skipping %s — %s", label, symbol, why)
            # One alert per outage, not one per symbol: a broker that cannot
            # list positions fails for every name in the pass at once. The
            # latch lives on the host (survives this per-call part) and is
            # cleared by the next successful read.
            if not self._reread_alerted():
                self._state.set("_position_reread_alerted", True)
                self._alert_owner_exit_declined(symbol, side=side, why=why + " (further names this outage: log only)")
            return None
        held, free = read
        if held <= 0:
            self._discharge_wal(symbol, wal_row_id)
            self._last_stop_clear_refusal = "stop_fired"
            why = _OWN_REASONS["stop_fired"]
            logger.warning("%s: no order for %s — %s", label, symbol, why)
            self._alert_owner_exit_declined(symbol, side=side, why=why)
            return None
        settlement = self._held_legs("_leg_settlement")
        legs = settlement is not None and settlement.legs
        reason = sale_refusal(
            qty,
            held,
            free,
            full_exit=full_exit,
            snapshot=position_qty_before_sell,
            net_change=settlement.closable_change(side) if legs else None,
            stops_filled=settlement.filled_qty if legs else 0.0,
        )
        if reason:
            put_back()
            self._last_stop_clear_refusal = reason
            why = _OWN_REASONS[reason]
            logger.warning("%s: no order for %s — %s (held %s, free %s, asked %s)", label, symbol, why, held, free, qty)
            self._alert_owner_exit_declined(symbol, side=side, why=why)
            return None
        qty = held if full_exit else abs(float(qty))
        position_qty_before_sell = held if held < position_qty_before_sell else position_qty_before_sell
        return qty, position_qty_before_sell

    def _closable_now(self, symbol: str, side: str) -> tuple[float, float | None] | None:
        """``(closable, free)`` for a `side` order from a fresh broker read: the
        long for a 'sell', the short for a 'buy'; free is the broker's own
        qty_available (None when not reported). None when the read fails."""
        try:
            read = closable_and_free(self.broker, symbol, side)
            record_guarded_pass((self.broker, self.db), "protected_sell.position_reread", context={"symbol": symbol})
            self._state.set("_position_reread_alerted", False)
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                (self.broker, self.db), "protected_sell.position_reread", exc, context={"symbol": symbol}
            )
            return None
        return read

    def _reread_alerted(self) -> bool:
        """True once this outage's single unreadable-position alert has gone out."""
        try:
            return self._state.get("_position_reread_alerted") is True
        except AttributeError:  # host never set it: no alert has gone out
            return False

    def _discharge_wal(self, symbol: str, wal_row_id) -> None:
        """Nothing is held, so the cancelled stops must NOT be restored: drop the recovery row."""
        if wal_row_id is None:
            return
        try:
            self.db.delete_pending_protection_restore(wal_row_id)
            record_guarded_pass((self.broker, self.db), "protected_sell.wal_discharge", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass((self.broker, self.db), "protected_sell.wal_discharge", exc, context={"symbol": symbol})
            logger.warning("WAL: failed to discharge row %d for flat %s: %s", wal_row_id, symbol, exc)


#: Owner-alert wording for the refusals this step records itself.
_OWN_REASONS = {
    "submit_unknown": (
        "the exit order's submission failed without a broker answer and the order could not be found by its "
        "own key, so whether it reached the broker is unknown; protection was put back over what is held now "
        "and the next pass decides again"
    ),
    "stop_fired": (
        "its protective stop FILLED before the exit was sent, so the position is already "
        "exiting through the stop and no order was sent"
    ),
    "position_unreadable": (
        "the broker position read failed after the stops were cleared, so the exit could not be "
        "sized to what is held; the stops were put back and no order was sent"
    ),
    "cancel_unconfirmed": (
        "the broker had not confirmed the protective stop's cancel, so the shares may still be held by it "
        "and a sale would be refused; no order was sent this pass and the run that decided re-checks it "
        "before it ends"
    ),
    "cancel_read_failed": (
        "the read of the cancelled stop's status failed, so whether it still holds the shares is unknown; "
        "no order was sent this pass and the run that decided re-checks it before it ends"
    ),
    "position_disagrees": (
        "the position the broker now reports does not match what was held less the stop fills, or is "
        "smaller than the order; no order was sent, the stops were put back, and the next pass decides again"
    ),
    "stop_filled_in_part": (
        "the protective stop filled in part while it was being cancelled, so this trim was decided on a "
        "position that no longer exists; no order was sent, the stops were put back, and the next pass decides again"
    ),
    "shares_reserved": (
        "fewer shares are free than the order needs (other open orders reserve the rest); a smaller sale "
        "is never sent in its place, so no order was sent and the stops were put back"
    ),
}


def _legs_of(cancelled) -> list[dict]:
    """The cancelled orders `cancel_open_entry_orders` carries as legs."""
    return list(getattr(cancelled, "legs", ()) or ())


def _q(value) -> float:
    """A quantity on the broker's own grid, so equality has no tolerance."""
    return round(float(value), ORDER_QTY_DECIMALS)


def closable_and_free(broker, symbol: str, side: str) -> tuple[float, float | None]:
    """``(closable, free)`` for a `side` order from a fresh position read.

    closable = the long for a 'sell', the short for a 'buy' (0 when nothing
    is held on that side); free = the broker's qty_available magnitude, None
    when the broker did not report it. Raises when the read fails."""
    want = symbol.strip().upper().replace("/", "")
    mine = [p for p in broker.get_positions() if str(p.symbol).strip().upper().replace("/", "") == want]
    signed = sum(float(p.qty) for p in mine)
    closable = signed if side == "sell" else -signed
    reported = [getattr(p, "qty_available", None) for p in mine]
    free = abs(sum(float(v) for v in reported)) if mine and None not in reported else None
    return (_q(closable) if _q(closable) > 0 else 0.0), free


def sale_refusal(intended, held, free, *, full_exit, snapshot=None, net_change=None, stops_filled=0.0) -> str:
    """Why exactly-the-intended sale may not go now; empty when it may.

    Checked after every cancelled leg is terminal. ``snapshot`` plus
    ``net_change`` (every leg's fill during the cancel, signed by its own
    side) gives the cross-check: it must equal what is held now."""
    if snapshot is not None and net_change is not None and _q(abs(float(snapshot)) + net_change) != _q(held):
        return "position_disagrees"
    if not full_exit and _q(stops_filled) > 0:
        return "stop_filled_in_part"
    want = held if full_exit else abs(float(intended))
    if _q(want) > _q(held):
        return "position_disagrees"
    if free is not None and _q(want) > _q(free):
        return "shares_reserved"
    return ""
