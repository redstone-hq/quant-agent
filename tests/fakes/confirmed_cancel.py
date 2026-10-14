"""A MagicMock broker that confirms protective-stop cancels, as Alpaca does.

The desk now waits for the broker to report every cancelled stop dead (read
by id) before it sells (refused sales, ETN and NET 13 Oct). A bare MagicMock
answers that wait with a MagicMock, or with whatever status the test set for
its SELL orders, so a test that never meant to model the cancel would see no
sale. `confirm_cancels` models it: each order sent through
`cancel_snapshotted_stops` reads `canceled` with nothing filled, or, with
``confirms=False``, stays `pending_cancel` (the owed-sale case). Every other
order keeps whatever the test set, set before or after this call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import DEFAULT


def confirm_cancels(broker, *, confirms: bool = True):
    """Wire `broker` (a MagicMock) to confirm its stop cancels; returns it.

    ``broker.cancelled_stop_ids`` is the set of ids it was asked to cancel,
    so a test can tell its SELL waits apart from the cancel waits."""
    cancelled: set[str] = set()
    broker.cancelled_stop_ids = cancelled
    cancel, wait, read = broker.cancel_snapshotted_stops, broker.wait_for_order_terminal, broker.client.get_order_by_id
    inner_cancel, inner_wait, inner_read = cancel.side_effect, wait.side_effect, read.side_effect

    def _cancel(symbol, specs, *a, **kw):
        cancelled.update(str(s["id"]) for s in specs or [] if isinstance(s, dict) and s.get("id"))
        return inner_cancel(symbol, specs, *a, **kw) if inner_cancel else DEFAULT

    def _wait(order_id, *a, **kw):
        if str(order_id) in cancelled:
            return "canceled" if confirms else "pending_cancel"
        return inner_wait(order_id, *a, **kw) if inner_wait else DEFAULT

    def _read(order_id, *a, **kw):
        if str(order_id) in cancelled:
            return SimpleNamespace(id=order_id, status="canceled" if confirms else "pending_cancel", filled_qty="0")
        return inner_read(order_id, *a, **kw) if inner_read else DEFAULT

    cancel.side_effect, wait.side_effect, read.side_effect = _cancel, _wait, _read
    return broker
