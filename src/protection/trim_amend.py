"""src.protection.trim_amend -- the protection half of a trim that shrinks its stop in place.

The broker-touching half (shrink the GTC leg, confirm the replace, settle the
book through the stop quantity invariant) lives in
src/execution/broker_parts/trim_book.py and is reached ONLY through the
injected broker object, so this package imports nothing from src.execution.
What stays here: the owner-facing refusal wording and the finalizer step that
settles the book and discharges the write-ahead (WAL) row.
"""

from __future__ import annotations

import logging

from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger("src.pipeline")

PATH = "protected_sell.trim_amend"


def trim_book_call(owner, name: str, *args, **kwargs):
    """Call broker-side method `name` (the trim book, leg settlement, cover to
    held) for `owner` (a ProtectedSell or SellFinalization): src.protection
    reaches the broker seam only this way. The host pipeline injects the broker CLASS as
    `owner._trim_book`, so its real body runs against whatever broker object
    the owner holds; with nothing injected the broker's own bound method runs."""
    book = getattr(owner, "_trim_book", None)
    if book is not None:
        return getattr(book, name)(owner.broker, *args, **kwargs)
    return getattr(owner.broker, name)(*args, **kwargs)


def refusal_reason(refusal: str, side: str) -> str:
    """Why the desk declined the exit, in the words the owner alert carries.
    Only states what the recorded refusal establishes (see protected_sell)."""
    order = side.upper()
    return {
        "cancel_rolled_back": (
            "its protective stop could not be cancelled and was rolled back, so the stop is "
            f"still resting and the broker would reject the {order} on held_for_orders"
        ),
        "unreadable": (
            "the broker's open-order listing failed after retries, so whether a protective "
            "stop is resting on these shares is UNKNOWN — the desk will not submit an exit "
            "against a picture of the broker it could not read"
        ),
        "stop_fired": "its protective stop FILLED while the stop was being shrunk, so the position is already exiting",
        "amend_unknown": (
            "the broker gave no answer to the stop-quantity change, so how many shares the "
            "stop holds is UNKNOWN and nothing was cancelled"
        ),
        "held_changed": (
            "the broker refused the stop-quantity change and the held quantity had changed, so a stop may have fired"
        ),
        "market_closed": (
            "the broker refused the stop-quantity change and the market is closed, so "
            "cancelling the stop could leave the kept shares naked at the open"
        ),
    }.get(refusal, "the protective-stop clear failed for a reason it did not record")


def finalize_trim_amend(finalizer, prot: dict) -> bool:
    """After the trim's wait: settle the book (see `AlpacaBroker.settle_trim_book`) and
    discharge the WAL row. Returns True iff the book ends covered."""
    broker, symbol = finalizer.broker, prot["symbol"]
    side = prot.get("side") or "sell"
    status = trim_book_call(finalizer, "settle_trim_book", symbol, side)
    if status == "flat":
        finalizer._cancel_stray_stops_on_flat(symbol, **({} if side == "sell" else {"side": side}))
        return _discharge(finalizer, prot)
    if status == "no_stop":
        # The kept leg is gone (fired or cancelled elsewhere): the ordinary
        # finalize restores/reprotects from the cancelled specs.
        done, _ = finalizer._finalize_protection_after_sell(
            prot["order_id"],
            symbol,
            prot["position_qty_before_sell"],
            prot["specs"],
            wal_row_id=prot.get("wal_row_id"),
            side=side,
        )
        return bool(done)
    if status != "accepted":
        logger.error("%s: %s WAL row stays (%s)", PATH, symbol, status)
        return False
    return _discharge(finalizer, prot)


def _discharge(finalizer, prot: dict) -> bool:
    wal_row_id = prot.get("wal_row_id")
    if wal_row_id is not None:
        try:
            finalizer.db.delete_pending_protection_restore(wal_row_id)
            record_guarded_pass((finalizer.broker, finalizer.db), f"{PATH}.wal_discharge", context={"row": wal_row_id})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                (finalizer.broker, finalizer.db), f"{PATH}.wal_discharge", exc, context={"row": wal_row_id}
            )
            logger.warning("WAL: failed to clear row %d for %s: %s", wal_row_id, prot["symbol"], exc)
    return True
