"""The exit a breached stop takes, wired at the protection layer.

`src.execution.stop_repair` executes a recorded stop the open already gapped
through (k40, 2026-10-12), but the execution layer may not import the
protection layer (zero-import-cycle check). So the repair takes an INJECTED
closer, and the callers that sit above both layers build it here: the
session's coverage repair and the coverage watchdog. With no closer injected
the repair keeps its old refusal.

The closer is the desk's one protected sell (`ProtectedSell`: write-ahead
stop clear, fresh position re-read and re-size, guarded submit, restore on
failure) wired with the same collaborators `pipeline_protection` hands it,
minus a pipeline: the watchdog must never build one.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from src.protection.protected_sell import ProtectedSell
from src.protection.sell_finalization import _WAL_SELL_SENTINEL
from src.protection.wal_restore_write import write_ahead_restore_row

logger = logging.getLogger(__name__)


class _CallState:
    """The get/set view `ProtectedSell` keeps its per-call refusal in."""

    def __init__(self) -> None:
        self._values: dict = {}

    def get(self, name: str):
        return self._values.get(name)

    def set(self, name: str, value) -> None:
        self._values[name] = value


def _log_only_decline(symbol: str, *, side: str, why: str) -> None:
    """For a caller with no owner-alert path of its own to inject: the
    decline still reaches the owner through the repair's refusal row and the
    caller's own gap alert, which carries that row's reason."""
    logger.error("breached-stop exit declined for %s (%s): %s", symbol, side, why)


def build_breached_stop_closer(
    *,
    broker: Any,
    db: Any,
    order_accepted: Callable[..., bool],
    alert_exit_declined: Callable[..., None] | None = None,
) -> Callable[..., str | None]:
    """`close_breached(symbol, qty, is_short, label, evidence) -> order_id|None`.

    `evidence` must carry `held_now` (the fresh held quantity the order was
    sized from) and `print_price`; the closer writes `status` and, on a
    decline, `refusal` back onto it so the repair's durable row says why.
    """

    def write_ahead(symbol, position_qty_before_sell, specs, *, side="sell"):
        return write_ahead_restore_row(db, logger, _WAL_SELL_SENTINEL, symbol, position_qty_before_sell, specs, side)

    def close_breached(symbol: str, qty: float, is_short: bool, label: str, evidence: dict) -> str | None:
        state = _CallState()
        sale = ProtectedSell(
            broker=broker,
            db=db,
            alert_owner_exit_declined=alert_exit_declined or _log_only_decline,
            order_accepted=order_accepted,
            write_ahead_protection_restore=write_ahead,
            state=state,
        )._submit_protected_sell(
            symbol=symbol,
            qty=qty,
            limit_price=None,
            reference_price=evidence["print_price"],
            position_qty_before_sell=evidence["held_now"],
            label=label,
            side="buy" if is_short else "sell",
        )
        if sale is None:
            evidence["refusal"] = state.get("_last_stop_clear_refusal") or "the order was not accepted"
            return None
        order, _prot = sale
        evidence["status"] = order.get("status") if isinstance(order, dict) else None
        order_id = order.get("id") if isinstance(order, dict) else None
        return str(order_id) if order_id else None

    return close_breached
