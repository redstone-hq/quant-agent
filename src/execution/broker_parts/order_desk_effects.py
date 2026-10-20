"""Guarded-failure recording for the order desk, with what each failure means for the caller."""

import logging

from src.sentinel.guarded import record_guarded_pass

# ok(desk, site) records a clean pass; mark(desk, site, exc) records a swallowed fault.
_LOG = logging.getLogger("src.execution.broker")

EFFECT: dict[str, str] = {
    "cancel_all_open_orders": ("the caller is told zero orders were cancelled when the truth is unknown"),
    "cancel_open_entry_orders": ("recorded then RAISED to the caller; never reported as zero entry orders cancelled"),
    "list_open_entry_orders_checked": ("reported as a FAILED read, not as an empty book"),
    "list_open_orders_checked": (
        "reported as a FAILED read with an empty list, not as an empty book; the freeze sweep touches nothing"
    ),
    "open_buy_notional": ("None returned; the caller cannot size against open buy notional"),
    "list_recent_orders": ("None returned so the caller retries rather than misjudging the order absent"),
    "list_filled_sell_orders": (
        "None returned so the caller retries rather than concluding there was no fill; a missed stop-out is "
        "a money-relevant accounting gap"
    ),
    "get_order_fill_info": ("None returned; no fill information for reconciliation"),
    "read_order_status": ("None returned; the order status is unknown to the caller"),
    "poll_order_status": ("polling stops and the last known status is returned"),
    "cancel_entry_order": (
        "if this raced a partial fill the position may end up larger than intended; the next coverage "
        "reconcile must be checked"
    ),
    "resolve_replacement_chain": (
        "None returned so the caller retries rather than concluding the order was never replaced"
    ),
    "replace_entry_limit": (
        "the order most likely reached a terminal state first; the ORIGINAL id remains authoritative"
    ),
    "replace_order_qty": (
        "the broker refused the shrink; (False, detail) is returned and the order keeps its original, "
        "larger quantity, so a closing order bigger than the position could still flip it until the "
        "freeze sweep is rerun"
    ),
    "submit_order_rejected": ("the broker refused the order; a rejected result is returned and no order exists"),
    "replace_confirmation_wait": ("the replacement is treated as UNCONFIRMED"),
    "replace_confirmation_chain_reread": ("the replacement is treated as UNCONFIRMED"),
}


def ok(desk, site, **context):
    record_guarded_pass(desk.client, "order_desk." + site, context=context)


def mark(desk, site, exc, **context):
    context["effect"] = EFFECT[site]
    record_guarded_pass(desk.client, "order_desk." + site, exc, log=_LOG, context=context)
