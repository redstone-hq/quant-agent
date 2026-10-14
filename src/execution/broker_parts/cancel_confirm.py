"""Read whether cancelled protective stops really reached a terminal state.

Lifted verbatim from src/execution/scale_in.py so the stop invariant
(src/execution/broker_parts/stop_invariant.py) can confirm its cancels
without importing scale_in, whose import chain reaches broker.py and would
close an import cycle through stop_place -> stop_invariant. scale_in
re-exports every name here, so existing callers and patch targets hold.
Leaf module: imports nothing from the broker.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from src.execution.scale_in_loud import record_scale_in

logger = logging.getLogger(__name__)

#: Terminal statuses that mean the protective sell is gone and did not
#: sell shares. `filled` is deliberately NOT here: a stop that fires
#: during cancel is a real exit and the BUY add must not proceed.
_CANCEL_CONFIRMED = frozenset(
    {
        "canceled",
        "cancelled",
        "expired",
        "rejected",
        "replaced",
    }
)
_STOP_FILLED = frozenset({"filled"})


def cancelled_stop_specs(specs: list[dict] | None) -> list[dict]:
    """Specs that were actually at the broker (have an id)."""
    out: list[dict] = []
    for spec in specs or []:
        if spec.get("id"):
            out.append(spec)
    return out


def _confirm_cancels_status(broker: Any, specs: list[dict]) -> tuple[str, str]:
    """Terminal state of the cancelled protective stops, as a status token.

    Returns ``(status, detail)`` where status is one of:

      * ``"confirmed"`` — every spec with an id reached a cancelled-like
        terminal state; the add may proceed.
      * ``"filled"``    — a protective stop FIRED during the cancel. The
        position was EXITED. A long's sell-stop firing sold the long; a
        short's buy-stop firing COVERED the short. Either way the add must
        abort, and a short must NOT restore a stop onto a now-flat name.
      * ``"unconfirmed"`` — no id to confirm, the wait raised, or the broker
        did not report a cancelled-like terminal state in the window.

    Wait is `wait_for_order_terminal` (websocket-first with the fill stream
    on since 2026-09-18, bounded REST otherwise) — unchanged either way.
    """
    for spec in cancelled_stop_specs(specs):
        order_id = str(spec.get("id") or "")
        if not order_id:
            return "unconfirmed", "a cancelled stop had no id to confirm"
        try:
            status = broker.wait_for_order_terminal(order_id)
        except Exception as exc:  # noqa: BLE001
            record_scale_in(broker, "confirm_cancels", exc, order=order_id)
            return "unconfirmed", f"cancel confirm raised for {order_id}: {exc}"
        else:
            record_scale_in(broker, "confirm_cancels")
        status = str(status or "").lower()
        if status in _STOP_FILLED:
            return "filled", (
                f"protective stop {order_id} FILLED during cancel — the add is aborted rather than adding into an exit"
            )
        if status not in _CANCEL_CONFIRMED:
            return "unconfirmed", (f"protective stop {order_id} not confirmed cancelled (status={status or 'unknown'})")
    return "confirmed", ""


#: Why a sale waiting on cancelled legs is not sent this pass. Each is
#: per symbol and leaves the sale owed (see protected_sell.record_owed_sale).
CANCEL_UNCONFIRMED = "cancel_unconfirmed"
CANCEL_READ_FAILED = "cancel_read_failed"


@dataclass(frozen=True)
class LegResult:
    """One cancelled leg as the broker reported it, read by id."""

    order_id: str
    status: str
    filled_qty: float
    elapsed_ms: float


@dataclass(frozen=True)
class LegSettlement:
    """Every cancelled leg of one sale, waited against ONE shared deadline.

    ``reason`` is empty only when every leg is terminal and its fill is
    known; the sale may then go. Otherwise it names why it may not:
    ``cancel_unconfirmed`` (a leg still live at the ceiling) or
    ``cancel_read_failed`` (a status or fill read failed, e.g. one REST
    hiccup with the stream off, so the leg's state is unknown).
    """

    legs: tuple[LegResult, ...] = ()
    reason: str = ""

    @property
    def settled(self) -> bool:
        return not self.reason

    @property
    def filled_qty(self) -> float:
        return sum(leg.filled_qty for leg in self.legs)

    @property
    def any_live(self) -> bool:
        """A leg not yet read dead (unread counts as live). A `replaced` leg
        is dead; its successor is a new order a fresh snapshot will find."""
        return any(leg.status not in _CANCEL_CONFIRMED | _STOP_FILLED for leg in self.legs)


def _filled_qty(broker: Any, order_id: str) -> float | None:
    """The leg's filled quantity, read by id; None when the read fails."""
    try:
        order = broker.client.get_order_by_id(order_id)
        return abs(float(getattr(order, "filled_qty", None) or 0))
    except Exception as exc:  # noqa: BLE001
        record_scale_in(broker, "settle_legs_fill", exc, order=order_id)
        return None


def settle_legs(broker: Any, specs: list[dict] | None, *, started: float | None = None) -> LegSettlement:
    """Wait until every cancelled leg is terminal at the broker, by id.

    One deadline for all legs: the existing order-terminal ceiling, counted
    from ``started`` (the last cancel request, monotonic clock). Each leg is
    given only the time remaining, so N legs never stack N windows. Stream
    on, a leg returns the instant the broker reports; stream off, REST reads
    it by id every poll interval until the shared deadline.
    """
    from src.execution.broker_parts.order_desk import ORDER_TERMINAL_CEILING_S

    t0 = time.monotonic() if started is None else started
    deadline = t0 + ORDER_TERMINAL_CEILING_S
    legs: list[LegResult] = []
    reason = ""
    for spec in cancelled_stop_specs(specs):
        order_id = str(spec["id"])
        remaining = max(0.0, deadline - time.monotonic())
        try:
            status = broker.wait_for_order_terminal(order_id, timeout_seconds=remaining)
            record_scale_in(broker, "settle_legs")
        except Exception as exc:  # noqa: BLE001
            record_scale_in(broker, "settle_legs", exc, order=order_id)
            status = None
        elapsed_ms = (time.monotonic() - t0) * 1000
        status = str(status or "").lower()
        filled = _filled_qty(broker, order_id) if status in _CANCEL_CONFIRMED | _STOP_FILLED else 0.0
        if not status or filled is None:
            reason = reason or CANCEL_READ_FAILED
        elif status not in _CANCEL_CONFIRMED | _STOP_FILLED or status == "replaced":
            # Still live, or `replaced`: its successor holds the shares now.
            reason = CANCEL_UNCONFIRMED
        legs.append(LegResult(order_id, status, filled or 0.0, elapsed_ms))
        logger.info("settle_legs: %s %s after %.0f ms (filled %s)", order_id, status or "unread", elapsed_ms, filled)
    return LegSettlement(tuple(legs), reason)
