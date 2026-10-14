"""src.protection.protected_sell -- the protected sell and its write-ahead stop cancel.

Bodies moved verbatim from src/pipeline_protection.py (`ProtectionMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body are the HOST's shim, handed in, never a body this part owns.
Host attributes a body reads with a defaulted getattr or ASSIGNS go through `state`
(a live get/set view the shim hands in), never a copy.
"""

import json
import logging

from src.execution.broker_parts.cancel_confirm import settle_legs
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
    ) -> tuple[dict, dict] | None:
        """Head half of the SELL/COVER discipline: clear protective stops
        (write-ahead) → submit the order → guarantee stops are restored if
        the order never reaches the broker.

        Returns ``(order, pending_protection)`` on broker acceptance, or
        ``None`` when the symbol must be skipped — stop-clear failed, the
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
                self.broker.cancel_open_entry_orders(symbol=symbol)
                record_guarded_pass((self.broker, self.db), "protected_sell.entry_cancel", context={"symbol": symbol})
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(
                    (self.broker, self.db), "protected_sell.entry_cancel", exc, context={"symbol": symbol}
                )
                logger.warning("%s: entry-order cancel failed for %s: %s", label, symbol, exc)
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
        if self._owe_if_unconfirmed(
            symbol,
            side,
            wal_row_id,
            {"label": label, "qty": abs(float(qty)), "full_exit": full_exit, "limit_price": limit_price}
            | {"reference_price": reference_price},
        ):
            return None

        def _put_back_protection() -> None:
            # Cancelled legs are restored; a stop shrunk in place for a trim
            # is grown back by the quantity invariant (nothing was sold).
            if stop_specs:
                self.broker._restore_stop_orders(
                    symbol,
                    stop_specs,
                    check_idempotency=False,
                    **stop_side_kwargs,
                )
            if kept_leg is not None:
                trim_book_call(self, "settle_trim_book", symbol, side)

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
            # Submit raised → the position is intact but its stops are
            # cancelled. Restore them in-session rather than waiting for the
            # next drain (this used to vary by site — only the since-deleted
            # auto take-profit restored; the others rode naked until drain).
            logger.error("%s: submit failed for %s: %s", label, symbol, exc)
            _put_back_protection()
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
                    logger.error(
                        "%s: MARKET escalation submit failed for %s: %s",
                        label,
                        symbol,
                        exc,
                    )
                    _put_back_protection()
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
        # one shared deadline, the existing order-terminal ceiling.
        self._state.set("_leg_settlement", settle_legs(self.broker, specs))
        return True, specs, wal_row_id

    def _begin_sale(self, label: str, position_qty_before_sell: float, qty: float) -> bool:  # noqa: ARG002
        """Reset this call's leg settlement; True when the sale is a full exit."""
        self._state.set("_leg_settlement", None)
        return _q(abs(float(qty))) >= _q(abs(float(position_qty_before_sell)))

    def _owe_if_unconfirmed(self, symbol: str, side: str, wal_row_id, sale: dict) -> bool:
        """True when a cancelled leg is not confirmed dead, so nothing may sell.

        The shares may still be held by a dying stop: a SELL now is refused
        (403 40310000, ETN and NET 13 Oct) and a new stop is refused too.
        Nothing is sent and nothing is put back; the sale is OWED on the
        recovery row and the intra_safety drain sends it."""
        settlement = self._state.get("_leg_settlement")
        if settlement is None or settlement.settled:
            return False
        record_owed_sale(self.db, wal_row_id, symbol, dict(sale, reason=settlement.reason))
        self._last_stop_clear_refusal = settlement.reason
        why = _OWN_REASONS[settlement.reason]
        logger.warning("%s: no order for %s this pass — %s", sale["label"], symbol, why)
        self._alert_owner_exit_declined(symbol, side=side, why=why)
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
        settlement = self._state.get("_leg_settlement")
        legs_filled = settlement.filled_qty if settlement is not None and settlement.legs else None
        reason = sale_refusal(
            qty, held, free, full_exit=full_exit, snapshot=position_qty_before_sell, legs_filled=legs_filled
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
        "and a sale would be refused; no order was sent and the sale is owed to the next safety tick"
    ),
    "cancel_read_failed": (
        "the read of the cancelled stop's status failed, so whether it still holds the shares is unknown; "
        "no order was sent and the sale is owed to the next safety tick"
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

#: A recovery row whose sale is OWED carries this prefix in its sell_order_id
#: (the write-ahead sentinel's place) followed by the sale as JSON.
OWED_SALE_PREFIX = "__OWED_SALE__"


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


def sale_refusal(intended, held, free, *, full_exit, snapshot=None, legs_filled=None) -> str:
    """Why exactly-the-intended sale may not go now; empty when it may.

    Checked after every cancelled leg is terminal. ``snapshot`` and
    ``legs_filled`` (the stop fills during the cancel) give the cross-check:
    what was held less what the stops sold must equal what is held now."""
    if snapshot is not None and legs_filled is not None:
        if _q(abs(float(snapshot)) - legs_filled) != _q(held):
            return "position_disagrees"
        if not full_exit and _q(legs_filled) > 0:
            return "stop_filled_in_part"
    want = held if full_exit else abs(float(intended))
    if _q(want) > _q(held):
        return "position_disagrees"
    if free is not None and _q(want) > _q(free):
        return "shares_reserved"
    return ""


def record_owed_sale(db, wal_row_id, symbol: str, sale: dict) -> bool:
    """Write the unsent sale onto its recovery row, so the drain sends it."""
    if wal_row_id is None:
        logger.error("owed sale for %s has no recovery row; the coverage audit is its only repair", symbol)
        return False
    try:
        db.update_pending_protection_restore(wal_row_id, sell_order_id=OWED_SALE_PREFIX + json.dumps(sale))
        record_guarded_pass(db, "protected_sell.owe_sale", context={"symbol": symbol})
        return True
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(db, "protected_sell.owe_sale", exc, context={"symbol": symbol})
        logger.error("owed sale for %s could not be recorded on row %s: %s", symbol, wal_row_id, exc)
        return False


def owed_sale(row: dict) -> dict | None:
    """The owed sale a recovery row carries, or None when it carries none."""
    order_id = str(row.get("sell_order_id") or "")
    if not order_id.startswith(OWED_SALE_PREFIX):
        return None
    try:
        return json.loads(order_id[len(OWED_SALE_PREFIX) :])
    except ValueError:
        return {}


def drain_owed_sales(host) -> int:
    """Send each owed sale whose cancelled legs are now dead; return how many went.

    Runs in the intra_safety preamble, before the restore drain reads the same
    rows. Per owed row: a leg still live (or unread) keeps the sale owed for
    the next tick. Otherwise the position is re-checked (held on the row's
    side, exactly the intended size, against the shares free now); a refusal
    is logged by symbol and the row goes back to the write-ahead sentinel so
    the restore drain puts the stops back at held. A sale that may go is sent
    through the protected sell, and the row then tracks that SELL, so the
    restore drain re-protects any residual once it is terminal."""
    from src.protection.sell_finalization import _WAL_SELL_SENTINEL

    try:
        rows = host.db.get_pending_protection_restores()
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass((host.broker, host.db), "owed_sales.read_rows", exc)
        return 0
    sent = 0
    for row in rows:
        sale = owed_sale(row)
        if sale is not None and _drain_one_owed_sale(host, row, sale, _WAL_SELL_SENTINEL):
            sent += 1
    return sent


def _owed_sale_refusal(host, row: dict, sale: dict) -> str | None:
    """None while a leg is still live or the position is unreadable (stay
    owed); otherwise the per-symbol refusal, empty when the sale may go."""
    symbol, label, side = row["symbol"], sale.get("label") or "SELL", row.get("side") or "sell"
    try:
        specs = json.loads(row["specs_json"])
    except ValueError:
        specs = []
    settlement = settle_legs(host.broker, specs)
    if settlement.any_live:
        logger.warning("owed %s for %s still not sent — %s", label, symbol, _OWN_REASONS[settlement.reason])
        return None
    try:
        held, free = closable_and_free(host.broker, symbol, side)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass((host.broker, host.db), "owed_sales.position_read", exc, context={"symbol": symbol})
        logger.warning("owed %s for %s still not sent — %s", label, symbol, _OWN_REASONS["position_unreadable"])
        return None
    sale["held"] = held
    if held <= 0:
        return "stop_fired"
    full_exit = bool(sale.get("full_exit"))
    reason = sale_refusal(sale.get("qty") or 0, held, free, full_exit=full_exit)
    if not reason and not full_exit and _q(settlement.filled_qty) > 0:
        reason = "stop_filled_in_part"
    if reason:
        logger.warning(
            "owed %s for %s not sent — %s (held %s, free %s, owed %s)",
            label,
            symbol,
            _OWN_REASONS[reason],
            held,
            free,
            sale.get("qty"),
        )
    return reason


def _drain_one_owed_sale(host, row: dict, sale: dict, sentinel: str) -> bool:
    """One owed row: True when its sale was sent."""
    symbol, row_id, side = row["symbol"], row["id"], row.get("side") or "sell"
    label = sale.get("label") or "SELL"
    reason = _owed_sale_refusal(host, row, sale)
    if reason is None:
        return False
    if reason == "stop_fired":
        host.db.delete_pending_protection_restore(row_id)  # nothing held: nothing to sell or protect
        return False
    host.db.update_pending_protection_restore(row_id, sell_order_id=sentinel)
    if reason:
        return False  # the restore drain puts the stops back at held this tick
    held = sale["held"]
    qty = held if sale.get("full_exit") else abs(float(sale["qty"]))
    result = host._submit_protected_sell(
        symbol=symbol,
        qty=qty,
        limit_price=sale.get("limit_price"),
        reference_price=sale.get("reference_price"),
        position_qty_before_sell=held,
        label=label,
        side=side,
    )
    if result is None:
        return False  # the protected sell logged its own reason; the restore drain re-protects
    order, prot = result
    logger.warning("owed %s for %s SENT: %s after its stop cancel confirmed", label, symbol, qty)
    _record_owed_trade(host, row, sale, order, qty)
    if prot.get("specs") or prot.get("kept_leg") is not None:
        # This sale cleared stops of its own: its own row protects the
        # residual, so the owed row's older specs are spent.
        host.db.delete_pending_protection_restore(row_id)
        host._finalize_pending_protections([prot], context="Owed sale")
    else:
        host.db.update_pending_protection_restore(row_id, sell_order_id=str(order.get("id")))
    return True


def _record_owed_trade(host, row: dict, sale: dict, order: dict, qty: float) -> None:
    symbol = row["symbol"]
    try:
        host.db.insert_trade(
            symbol=symbol,
            action=sale.get("label") or "SELL",
            qty=qty,
            price=sale.get("reference_price"),
            reasoning=f"owed sale, sent once its stop cancel was confirmed ({sale.get('reason')})",
            run_id=row.get("run_id"),
            broker_order_id=order.get("id"),
            fill_status="submitted",
        )
        record_guarded_pass((host.broker, host.db), "owed_sales.insert_trade", context={"symbol": symbol})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass((host.broker, host.db), "owed_sales.insert_trade", exc, context={"symbol": symbol})
