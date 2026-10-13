"""The ex-dividend stop shift as a PART (item 201 body, moved verbatim).

`StopShifter` is constructed from its six collaborators (keyword-only) and
exercised without a `StopPlacer` or a broker. `shift_stops_down`'s body is
byte-for-byte the mixin body it replaces; only the object that supplies the
collaborators changed. The placer builds one PER CALL (see stop_shift.py), so
a collaborator swapped on the placer after construction is what the body sees
-- the same live-handoff discipline as `AlpacaBroker._stop_placer`.

This is the live stop path: a shift is one-way and can tighten a real stop.
Nothing here alters a number, an ordering or a branch.
"""

from __future__ import annotations

import logging

from src.execution.broker_parts.stop_amend import _quantize_price
from src.execution.broker_parts.stop_clock import defer_shift_if_closed
from src.execution.broker_parts.stop_window import UnprotectedWindow, fallback_reason

logger = logging.getLogger("src.execution.broker")


def _specs_qty(specs: list[dict]) -> float | None:
    """Total resting stop quantity, or None when a spec is unreadable."""
    try:
        return sum(abs(float(s["qty"])) for s in specs)
    except (TypeError, ValueError, KeyError):
        return None


class StopShifter:
    """Lower every resting sell-stop for one symbol by a fixed amount.

    Collaborators (all keyword-only, all required):
      client                        -- the broker client, read by the
                                       market-closed gate in stop_clock
      list_open_sell_stop_orders    -- symbol -> resting sell-stop orders
      snapshot_stop_order           -- order -> spec dict or None
      stop_order_amendable_in_place -- order -> bool (measured-safe shape)
      amend_one_stop_price          -- (symbol=, spec=, new_price=) -> leg dict
      cancel_snapshotted_stops      -- (symbol, specs) -> cancel outcome
      restore_stop_orders           -- (symbol, specs, **kw) -> (restored, failed)
      window_log                    -- sink list for unprotected-window rows
                                       (item 201; recording only)
    """

    def __init__(
        self,
        *,
        client,
        list_open_sell_stop_orders,
        snapshot_stop_order,
        stop_order_amendable_in_place,
        amend_one_stop_price,
        cancel_snapshotted_stops,
        restore_stop_orders,
        window_log=None,
    ):
        self.client = client
        self._list_open_sell_stop_orders = list_open_sell_stop_orders
        self._snapshot_stop_order = snapshot_stop_order
        self._stop_order_amendable_in_place = stop_order_amendable_in_place
        self._amend_one_stop_price = amend_one_stop_price
        self.cancel_snapshotted_stops = cancel_snapshotted_stops
        self._restore_stop_orders = restore_stop_orders
        self._window_log = window_log if window_log is not None else []

    def shift_stops_down(self, symbol: str, amount: float) -> dict | None:
        """Lower EVERY open sell-stop for `symbol` by `amount`, preserving
        each stop's own level and qty.

        Ex-dividend flow (audit round 2): the old path read ONE stop level
        (first-match) and replace_stop_loss'd ALL stops with a single
        consolidated order — with per-BUY GTC stops now the steady state,
        that collapsed distinct per-lot levels into one and could TIGHTEN a
        wide lot's stop to the tightest lot's level. Shifting each spec
        keeps the per-lot geometry and just absorbs the mechanical gap.

        Item 201: a shift moves PRICE only, so when every resting stop is the
        measured-safe shape each one is AMENDED IN PLACE and nothing is ever
        cancelled — the position is protected before, during and after. The
        cancel+resubmit below is now the fallback for shapes the measurement
        did not cover, and `mode` in the return says which path ran.

        Returns {"id", "status", "symbol", "shifted", "total", "mode"} or None
        when nothing was shifted. Best-effort with rollback: on the fallback,
        cancel failures roll back already-cancelled stops and re-place failures
        restore the ORIGINAL spec for that stop; on the amend path a failure
        leaves that stop resting at its old level, which is tighter than
        intended but never absent.

        SELL-stops only, deliberately not generalised to a short's BUY-stop
        (shorts-safe, Stage 2): the caller (`pipeline._handle_ex_dividends`)
        already excludes shorts before reaching this method, because the
        economics are genuinely different, not just the arithmetic sign — a
        long owns the shares and receives the dividend (the mechanical
        gap-down needs absorbing); a short instead OWES the dividend to the
        lender, a cash liability with no corresponding price-gap-absorption
        logic here. Mirroring the sign without modelling that liability
        would be a guess, not a fix.
        """
        if amount <= 0:
            return None
        specs: list[dict] = []
        orders: list = []
        for order in self._list_open_sell_stop_orders(symbol):
            spec = self._snapshot_stop_order(order)
            if spec is None:
                logger.warning(
                    "shift_stops_down: cannot snapshot stop %s for %s — aborting",
                    getattr(order, "id", "<unknown>"),
                    symbol,
                )
                return None
            specs.append(spec)
            orders.append(order)
        if not specs:
            return None
        shifted = [
            {
                **spec,
                "stop_price": _quantize_price(spec["stop_price"] - amount),
                "limit_price": None,
            }
            for spec in specs
        ]
        if any(not s.get("stop_price") or s["stop_price"] <= 0 for s in shifted):
            logger.error(
                "shift_stops_down: shifting %s's stop(s) by %s would put a stop "
                "at or below zero — aborting, the existing stops stay resting",
                symbol,
                amount,
            )
            return None

        # IN-PLACE AMEND FIRST (item 201). A shift changes each stop's PRICE and
        # nothing else, which is exactly the operation the 2026-09-30 rehearsal
        # measurement established `replace_order_by_id(stop_price=...)` performs
        # atomically. Cancelling every stop and re-placing them left the whole
        # position naked for the width of that round trip, on a path that runs
        # on ordinary ex-dividend days against real open positions.
        #
        # All-or-nothing on the DECISION, per spec on the EXECUTION: if any one
        # resting order is not the measured-safe shape (a stop-limit fallback
        # leg, a bracket child), the legacy cancel+resubmit runs for the symbol
        # exactly as before rather than half the stops moving one way and half
        # the other.
        if all(self._stop_order_amendable_in_place(o) for o in orders):
            legs: list[dict] = []
            for spec, target in zip(specs, shifted):
                leg = self._amend_one_stop_price(
                    symbol=symbol,
                    spec=spec,
                    new_price=target["stop_price"],
                )
                legs.append(leg)
                if leg["outcome"] == "amended":
                    logger.info(
                        "shift_stops_down: %s stop %s AMENDED IN PLACE $%.4f -> "
                        "$%.4f, qty %s unchanged, new id %s (no cancel)",
                        symbol,
                        leg["id"],
                        leg["old_stop"],
                        leg["new_stop"],
                        leg["qty"],
                        leg["new_id"],
                    )
                elif leg["outcome"] == "refused":
                    # NOT a conservative outcome: across an ex-dividend open the
                    # un-shifted level is wrong by exactly the dividend, in the
                    # direction that TRIGGERS it. The caller records and alerts.
                    logger.error(
                        "shift_stops_down: %s stop %s was REFUSED the shift to "
                        "$%.4f (%s) — it is still resting at $%.4f, which the "
                        "ex-dividend opening gap may trigger on its own; "
                        "nothing cancelled",
                        symbol,
                        leg["id"],
                        leg["new_stop"],
                        leg["detail"],
                        leg["old_stop"],
                    )
                elif leg["outcome"] == "flat":
                    logger.info(
                        "shift_stops_down: %s stop %s could not be shifted and "
                        "the position is FLAT (%s) — nothing left to protect",
                        symbol,
                        leg["id"],
                        leg["detail"],
                    )
                elif leg["outcome"] == "naked":
                    logger.error(
                        "shift_stops_down: %s stop %s is GONE after a dead "
                        "replacement (%s) — the broker shows NO protective "
                        "stop for this symbol; the position is UNPROTECTED, "
                        "and this session's coverage repair has already run, "
                        "so the gap persists until the NEXT intra sweep",
                        symbol,
                        leg["id"],
                        leg["detail"],
                    )
                else:
                    logger.error(
                        "shift_stops_down: %s stop %s amend outcome UNKNOWN (%s) "
                        "— the desk does NOT know whether it rests at $%.4f or "
                        "$%.4f; nothing cancelled, re-read the book",
                        symbol,
                        leg["id"],
                        leg["detail"],
                        leg["old_stop"],
                        leg["new_stop"],
                    )
            amended = [l for l in legs if l["outcome"] == "amended"]
            unknown = [l for l in legs if l["outcome"] == "unknown"]
            if any(l["outcome"] == "naked" for l in legs):
                status = "naked"
            elif unknown:
                status = "unknown"
            elif legs and all(l["outcome"] == "flat" for l in legs):
                status = "flat"
            elif len(amended) == len(legs):
                status = "accepted"
            elif amended:
                status = "partial"
            else:
                status = "refused"
            logger.info(
                "shift_stops_down: %s — %d/%d stop(s) CONFIRMED amended in place, %d unknown, 0 cancelled (status=%s)",
                symbol,
                len(amended),
                len(legs),
                len(unknown),
                status,
            )
            return {
                # Only a CONFIRMED full shift carries an order id. A partial, a
                # refusal or an unknown must not read as an accepted stop order,
                # or the caller writes every leg back at the shifted level and
                # files a trade row for a stop that never moved.
                "id": amended[0]["new_id"] if status == "accepted" else None,
                "status": status,
                "symbol": symbol,
                "shifted": len(amended),
                "total": len(legs),
                "mode": "amend",
                "legs": legs,
            }

        if deferred := defer_shift_if_closed(self, symbol, specs, shifted, amount):
            return deferred  # out of hours: cancel NOTHING (see stop_clock.py)
        # Item 201: this fallback is the one shift path that still cancels, so
        # its naked window is TIMED and RECORDED exactly like replace_stop_loss's
        # rather than passing silently. Recording only; nothing here changes what
        # is cancelled, restored or returned.
        window = UnprotectedWindow(
            symbol,
            fallback_reason(specs, _specs_qty(specs)),
            self._window_log,
            path="shift_stops_down",
        )
        cancel = self.cancel_snapshotted_stops(symbol, specs)
        for spec in cancel.cancelled:
            window.cancelled(spec.get("id"))
        if not cancel.cleared:
            # Rollback handled inside; a shrunk-coverage outcome means the
            # ORIGINAL levels are gone and could not be put back, so the
            # un-restored specs are re-attempted here at their original
            # level before giving up. A failed shift must never end with
            # less protection than it started with.
            if cancel.coverage_shrank:
                self._restore_stop_orders(
                    symbol,
                    list(cancel.unprotected),
                    check_idempotency=True,
                )
            window.close("cancel_incomplete")
            return None
        restored, failed = self._restore_stop_orders(symbol, shifted)
        if failed:
            # Put the ORIGINAL levels back for whatever couldn't be shifted —
            # protection at the old level beats no protection.
            originals = [
                s
                for s in specs
                if any(
                    f.get("qty") == s["qty"] and abs(f.get("stop_price", 0) - (s["stop_price"] - amount)) < 0.02
                    for f in failed
                )
            ]
            if originals:
                self._restore_stop_orders(symbol, originals)
            logger.error(
                "shift_stops_down: %d/%d stop(s) failed to shift for %s — originals restored where possible",
                len(failed),
                len(specs),
                symbol,
            )
        if restored <= 0:
            window.close("not_restored")
            return None
        window.close("partial" if failed else "restored")
        return {
            "id": f"shift-{symbol}",
            "status": "accepted",
            "symbol": symbol,
            "shifted": restored,
            "total": len(specs),
            "mode": "cancel_resubmit",
        }
