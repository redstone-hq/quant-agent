"""src.execution.broker_parts.trim_book -- a trimming exit shrinks its stop in place, never cancels it.

The broker-touching half of the trim path; reached ONLY through the broker
object (`AlpacaBroker.clear_stops_for_trim` / `settle_trim_book` /
`trim_keeps_shares`) so src.protection never imports src.execution. The
owner-facing half (refusal wording, WAL discharge) is src.protection.trim_amend.

THE DEFECT THIS CLOSES. The four trims (REDUCE, PARTIAL_SELL, TAKE_PROFIT,
SWEEP_SELL) used the full-exit discipline: cancel EVERY protective stop,
sell, wait for the fill, re-place a stop on what is left. Between the
cancel and the re-place the KEPT shares had no stop at all.

WHAT WAS MEASURED on the sandbox (regular hours, 2026-10-09): selling
shares a resting stop holds is refused (403 40310000 held_for_orders); a
whole-share quantity PATCH on the stop answers 200 with a NEW id and marks
the old order "replaced", after which the freed shares sell while the kept
stop stays live; a FRACTIONAL quantity PATCH is refused (422 "qty must be
an integer"); a fractional stop must be DAY; a second stop over held shares
is refused.

THE PATH, per leg class (`book_shape` in stop_invariant.py: W whole shares
on a GTC leg, F sub-share on a DAY sliver):

  1. the GTC leg is PATCHED down to the KEPT whole quantity and the replace
     is CONFIRMED (old order terminal as "replaced", new order live and not
     pending) BEFORE the sell is submitted -- a pending replace holds
     max(old, new), so a sell sent earlier is refused;
  2. a sliver the sell needs shares from is cancelled on the existing
     write-ahead path, the sell goes, and the finalizer re-runs the stop
     quantity invariant on the fill so the sliver the position still needs
     is re-placed. The naked window is NOT removed on this leg: it SHRINKS
     to the sub-share, because the broker allows no quantity change on it;
  3. a refused PATCH re-reads the held quantity first (a stop may have
     fired); unchanged and the tape OPEN, the old cancel-all path runs;
     unchanged and the tape SHUT, the trim is refused and logged loudly --
     out of hours a cancel can land while the resubmit is refused;
  4. a full exit (kept == 0) never comes here; it keeps cancel-all.

Shorts: the buy-stop covering a short is PATCHED down the same way. The
403 on a BUY-to-cover was NOT measured, so the measured-safe path (refuse,
never sell against an unconfirmed book) is what a refusal falls to.

Where the new stop id goes: the desk persists stop LEVELS
(`trades.stop_loss`, stop_records.write_back_stop_loss), never ids -- the
trailing amend persists none either -- and the fill reconciler attributes a
stop-out by diffing broker fills against the ledger's own order ids, so a
fill on the replaced id is written as a stop-out exactly as one on the
original would be. The new id is carried on the pending-protection record
and logged; there is no id store to write it to.
"""

from __future__ import annotations

import logging

from src.execution.broker_parts.cancel_confirm import _confirm_cancels_status
from src.execution.broker_parts.stop_clock import market_is_closed
from src.execution.broker_parts.stop_invariant import _Invariant, book_shape
from src.execution.order_gates import _FRACTIONAL_QTY_EPSILON, _split_protective_qty
from src.execution.order_statuses import PROTECTIVE_ORDER_ACTIVE_STATUSES
from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger("src.pipeline")

PATH = "protected_sell.trim_amend"
#: The exits that KEEP shares; every other label is a full exit on cancel-all.
TRIM_LABELS = frozenset({"REDUCE", "PARTIAL_SELL", "TAKE_PROFIT", "SWEEP_SELL"})


def trim_keeps_shares(label: str, position_qty_before_sell: float, qty: float) -> bool:
    """True when `label` is a trim AND shares remain after `qty` is shed."""
    try:
        kept = abs(float(position_qty_before_sell)) - abs(float(qty))
    except (TypeError, ValueError):
        return False
    return label in TRIM_LABELS and kept > _FRACTIONAL_QTY_EPSILON


def _held_now(broker, symbol: str) -> float | None:
    """|held| re-read from the broker; None when the read failed."""
    try:
        positions = broker.get_positions()
        record_guarded_pass((broker, None), f"{PATH}.reread_held", context={"symbol": symbol})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass((broker, None), f"{PATH}.reread_held", exc, context={"symbol": symbol})
        return None
    for pos in positions or []:
        if getattr(pos, "symbol", None) == symbol:
            return abs(float(getattr(pos, "qty", 0) or 0))
    return 0.0


class TrimAmend:
    """Shrink the resting stops for ONE trim; refuse or fall back when the broker will not."""

    def __init__(self, *, broker, cancel_specs_with_write_ahead, state) -> None:
        self.broker = broker
        self._cancel_specs_with_write_ahead = cancel_specs_with_write_ahead
        self._state = state

    def _refuse(self, reason: str) -> tuple[bool, list[dict], None, None]:
        self._state.set("_last_stop_clear_refusal", reason)
        return False, [], None, None

    def clear_for_trim(
        self, symbol: str, held: float, sell_qty: float, *, side: str = "sell"
    ) -> tuple[bool, list[dict], int | None, dict | None]:
        """Returns ``(ok, cancelled_specs, wal_row_id, kept_leg)``.

        ``cancelled_specs`` is ONLY what was cancelled (a sliver, or nothing);
        the shrunk GTC leg is never in it because it is still live. ``kept_leg``
        names that live leg so the finalizer re-runs the quantity invariant on
        the fill instead of restoring over it. ``kept_leg`` is None when the
        cancel-all path ran (the caller then finalizes exactly as before).
        """
        snapshot_kwargs = {} if side == "sell" else {"side": side}
        ok, specs = self.broker.snapshot_protective_stops(symbol, **snapshot_kwargs)
        if not ok:
            return self._refuse("unreadable")
        if not specs:
            self._state.set("_last_stop_clear_refusal", "")
            return True, [], None, None
        held = abs(float(held))
        shape = book_shape(specs, held)
        kept_whole, kept_frac = _split_protective_qty(held - abs(float(sell_qty)))
        gtc = shape["gtc"]
        if shape["g"] > kept_whole + _FRACTIONAL_QTY_EPSILON:
            if len(gtc) != 1 or kept_whole < 1:
                # Several GTC legs, or no whole share kept: no single PATCH
                # expresses it. The measured-safe path for the shape.
                return self._fall_back(symbol, held, specs, side, "no single whole-share leg to shrink")
            outcome = self._shrink_confirmed(symbol, gtc[0], int(kept_whole), side)
            if outcome != "amended":
                return self._after_refused_patch(symbol, held, specs, side, outcome)
            specs = [s for s in specs if s is not gtc[0]] + [self._kept]
            shape = book_shape(specs, held)
        kept_leg = dict(shape["gtc"][0]) if shape["gtc"] else None
        cancelled: list[dict] = []
        wal_row_id = None
        if shape["d"] > kept_frac + _FRACTIONAL_QTY_EPSILON:
            # The sell needs shares the DAY sliver holds. The sub-share IS
            # naked from this cancel until the finalizer re-places it; the
            # broker refuses every quantity change on a fractional order.
            ok, cancelled, wal_row_id = self._cancel_specs_with_write_ahead(
                symbol, held - shape["g"], shape["day"], side=side
            )
            if not ok:
                return False, [], None, None
            # The cancel above waited for the broker to confirm the sliver
            # dead (one shared deadline); the protected sell sends nothing
            # until it has, and owes the sale when it has not.
        self._state.set("_last_stop_clear_refusal", "")
        return True, cancelled, wal_row_id, kept_leg

    # ------------------------------------------------------------ the PATCH
    def _shrink_confirmed(self, symbol: str, spec: dict, new_qty: int, side: str) -> str:
        """PATCH one GTC leg to `new_qty` and CONFIRM the replace landed:
        the old order terminal as "replaced" (the same wait the invariant
        uses on its cancels) and the new one live, not pending. Returns the
        leg outcome: amended / refused / unknown / fired."""
        leg = self.broker._amend_one_stop_price(
            symbol=symbol, spec=spec, new_price=float(spec["stop_price"]), new_qty=new_qty
        )
        if leg.get("outcome") != "amended":
            return str(leg.get("outcome") or "unknown")
        status, detail = _confirm_cancels_status(self.broker, [spec])
        if status == "unconfirmed":
            status, detail = _confirm_cancels_status(self.broker, [spec])
        if status == "filled":
            return "fired"
        if status != "confirmed":
            logger.error("%s: %s stop PATCH to %d not confirmed (%s); nothing sold", PATH, symbol, new_qty, detail)
            return "unknown"
        new_id = str(leg.get("new_id") or "")
        info = self.broker.get_order_fill_info(new_id) or {}
        new_status = str(info.get("status") or "").lower()
        if new_status not in PROTECTIVE_ORDER_ACTIVE_STATUSES:
            logger.error(
                "%s: %s replacement stop %s is %r, not live; nothing sold",
                PATH,
                symbol,
                new_id,
                new_status or "unreadable",
            )
            return "unknown"
        self._kept = {**spec, "id": new_id, "qty": float(new_qty), "replaced": spec["id"]}
        logger.info(
            "%s: %s stop %s shrunk in place to %d (now order %s), kept shares stay covered",
            PATH,
            symbol,
            spec["id"],
            new_qty,
            new_id,
        )
        return "amended"

    def _after_refused_patch(self, symbol, held, specs, side, outcome: str):
        if outcome == "fired":
            logger.error(
                "%s: %s's protective stop FILLED during the PATCH; the position is exiting, the trim is not sent",
                PATH,
                symbol,
            )
            return self._refuse("stop_fired")
        if outcome == "unknown":
            # The PATCH may have landed; the hold is max(old, new). Nothing
            # is cancelled on an answer the broker never gave.
            return self._refuse("amend_unknown")
        now = _held_now(self.broker, symbol)
        if now is None or abs(now - held) > _FRACTIONAL_QTY_EPSILON:
            logger.error(
                "%s: %s PATCH refused and held changed %s -> %s (a stop may have fired); the trim is not sent",
                PATH,
                symbol,
                held,
                now,
            )
            return self._refuse("held_changed")
        return self._fall_back(symbol, held, specs, side, "the broker refused the quantity PATCH")

    def _fall_back(self, symbol, held, specs, side, why: str):
        """Cancel-all ONLY on an open tape; shut, refuse loudly and cancel nothing."""
        closed = market_is_closed(self.broker.client)
        if closed is not False:
            logger.error(
                "%s: %s trim REFUSED out of hours (%s, clock=%s): a cancel now could leave the kept "
                "shares naked at the open",
                PATH,
                symbol,
                why,
                "unreadable" if closed is None else "closed",
            )
            return self._refuse("market_closed")
        logger.warning("%s: %s falls back to cancel-all during regular hours (%s)", PATH, symbol, why)
        ok, cancelled, wal_row_id = self._cancel_specs_with_write_ahead(symbol, held, specs, side=side)
        return ok, cancelled, wal_row_id, None


def settle_book(broker, symbol: str, side: str = "sell") -> str:
    """Re-read the position and the live book and let the stop quantity
    invariant settle both legs: grow the GTC leg back when the sell did not
    fill (rejected, raised, expired), re-place the sliver the kept position
    needs, nothing on an exact fill. Returns the invariant's status, or
    "unreadable" / "flat" / "no_stop"."""
    held = _held_now(broker, symbol)
    if held is None:
        logger.error("%s: %s position unreadable after the trim", PATH, symbol)
        return "unreadable"
    snapshot_kwargs = {} if side == "sell" else {"side": side}
    ok, specs = broker.snapshot_protective_stops(symbol, **snapshot_kwargs)
    if not ok:
        return "unreadable"
    if held <= _FRACTIONAL_QTY_EPSILON:
        return "flat"
    if not specs:
        return "no_stop"
    status = str(_Invariant(broker, symbol, specs, held, side, {}).run().get("amend_status"))
    if status != "accepted":
        logger.error("%s: %s book not settled after the trim (%s)", PATH, symbol, status)
    return status
