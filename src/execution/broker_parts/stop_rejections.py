"""Broker-rejection classifiers lifted out of `stop_place` so that file does not grow.

Re-exported there under the same names.
"""

from __future__ import annotations


def _is_held_for_orders_error(exc: BaseException) -> bool:
    """True when the broker refused because shares are reserved by an open order.

    Alpaca surfaces this as `held_for_orders` and/or `insufficient qty
    available` (2026-04-25 AMZN, 2026-09-16 BRK-B DAY sliver).
    """
    text = str(exc).lower()
    return "held_for_orders" in text or "insufficient qty" in text


def real_broker_order_id(value: object) -> str:
    """The broker order id in `value`, or "" when there ISN'T one.

    `_snapshot_stop_order` stamps `"id": str(order.id)`, so an order that
    reached it without an id carries the four-character string "None" —
    which is TRUTHY. Every `if spec.get("id")` filter therefore counted a
    missing id as a present one, and the resulting "id" then matched no
    open order at the broker, ever. Judge the value, don't test the
    stringified None for truthiness.
    """
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return ""
    return text


def log_terminal_stop_rejection(log, leg, symbol, exc, attempt, attempts) -> None:
    """Say why a protective-stop retry burst is cut short.

    Board item 129: a 400/404/422 will fail identically on every retry -- it
    is not a blip, it is the broker's answer. Burning the rest of the burst on
    a doomed resubmit only delays the alert this ceiling exists to deliver
    promptly. `log` is the caller's logger so the record keeps its name.
    """
    log.error(
        "protective stop [%s] for %s got a terminal broker "
        "rejection (status %s) on attempt %d/%d — this will "
        "not change on retry, escalating now instead of "
        "spending the rest of the budget.",
        leg,
        symbol,
        getattr(exc, "status_code", None),
        attempt,
        attempts,
    )
