"""Exit-settlement registration and the open-exit relief read, moved out of
``src/pipeline_protection.py`` and re-exported there.

The broker's terminal-status set is injected by the builder in
``src.pipeline_protection`` (the one place the broker seam is imported), so no
new layer crossing or import cycle is made.
"""

from __future__ import annotations

import logging

from src.models import TradeDecision
from src.pipeline_protection_record import record_protection_fault

logger = logging.getLogger("src.pipeline")


class ExitRelief:
    """Exit-settlement registration and the open-exit relief read; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        broker=None,
        state=None,
        terminal_states=frozenset(),
    ) -> None:
        self.broker = broker
        self._terminal_states = terminal_states
        self._state = state  # live get/set view of the host's attributes this part reads and assigns

    @property
    def _unsettled_exit_orders(self):
        return self._state.get("_unsettled_exit_orders")

    @_unsettled_exit_orders.setter
    def _unsettled_exit_orders(self, value) -> None:
        self._state.set("_unsettled_exit_orders", value)

    def _register_exit_settlement(self, prot: dict) -> None:
        """Record or clear one exit order in the unsettled register."""
        order_id = str(prot.get("order_id") or "")
        if not order_id:
            return
        register = getattr(self, "_unsettled_exit_orders", None)
        if register is None:
            register = {}
            self._unsettled_exit_orders = register
        status = str(prot.get("terminal_status") or "").lower()
        if status in self._terminal_states:
            register.pop(order_id, None)
            return
        register[order_id] = {
            "symbol": str(prot.get("symbol") or "").strip().upper(),
            "submitted_qty": abs(float(prot.get("submitted_qty") or 0.0)),
        }

    def _open_exit_relief(self, positions) -> tuple[list, bool]:
        """Exits still WORKING at the broker, as SELL decisions, plus whether
        any of them could not be measured.

        Re-polls every order in `_unsettled_exit_orders` (a settled one is
        dropped, so the register self-heals) and expresses each remaining
        open quantity as an ordinary SELL `TradeDecision`.
        `apply_gross_ceiling`'s STEP 1 already subtracts planned exits from
        the book before it judges anything, so handing these in makes a
        later pass cut the TRUE residual instead of re-cutting exposure that
        is already on its way out. That is the measured answer; refusing the
        whole pass was the blunt one, and refusing is worst exactly when this
        fires — a marketable limit that misses in fifteen seconds means a
        gap, a halt or a vanished book.

        The second return value is True when an order's state could not be
        read at all. Nothing is guessed there: the caller refuses to re-cut,
        because an unmeasurable in-flight exit is precisely the case where
        netting nothing would double the shed.
        """
        register = getattr(self, "_unsettled_exit_orders", None) or {}
        if not register:
            return [], False
        held = {str(getattr(p, "symbol", "") or "").strip().upper(): p for p in (positions or [])}
        relief: list = []
        unmeasurable = False
        for order_id, row in list(register.items()):
            try:
                info = self.broker.get_order_fill_info(order_id)
            except Exception as exc:  # noqa: BLE001
                record_protection_fault(self, "exit_relief.repoll", exc, order_id=order_id)
                logger.warning("open-exit re-poll failed for %s: %s", order_id, exc)
                info = None
            if info is None:
                unmeasurable = True
                continue
            status = str(info.get("status") or "").lower()
            if status in self._terminal_states:
                register.pop(order_id, None)
                continue
            symbol = row.get("symbol") or ""
            position = held.get(symbol)
            held_qty = abs(float(getattr(position, "qty", 0.0) or 0.0)) if position else 0.0
            open_qty = max(
                0.0,
                float(row.get("submitted_qty") or 0.0) - float(info.get("filled_qty") or 0.0),
            )
            if open_qty <= 0:
                continue
            if held_qty <= 0:
                # Still working against a position the book no longer shows:
                # nothing to net it against, and nothing safe to assume.
                unmeasurable = True
                continue
            relief.append(
                TradeDecision(
                    action="SELL",
                    symbol=symbol,
                    allocation_pct=min(100.0, open_qty / held_qty * 100.0),
                    entry_price=0.0,
                    stop_loss=0.0,
                    take_profit=0.0,
                    reasoning=(
                        f"Exit order {order_id} is still working at the broker "
                        f"({open_qty:g} of {held_qty:g}); it is netted out of the "
                        f"gross re-measure so the book is not sold down twice."
                    ),
                )
            )
        return relief, unmeasurable
