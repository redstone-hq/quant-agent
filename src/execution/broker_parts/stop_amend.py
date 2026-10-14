"""In-place protective-stop amendment, lifted verbatim from AlpacaBroker.

A `StopAmender` is built from its collaborators alone (keyword-only), so the
amend path can be exercised without an `AlpacaBroker` or a TradingPipeline.
`AlpacaBroker` keeps same-named thin shims that build one per call.
"""

from __future__ import annotations

import logging

from alpaca.trading.requests import ReplaceOrderRequest

from src.execution.broker_parts.stop_clock import deferred_payload, market_is_closed
from src.execution.broker_parts.stop_dead_replacement import classify_after_dead_replacement
from src.sentinel.guarded import record_guarded_pass

# re-export mirror: defined there, still importable from here
from src.execution.broker_parts.stop_amend_pure import _is_terminal_broker_rejection, _quantize_price

# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
logger = logging.getLogger("src.execution.broker")


# Sentinel returned by `_amend_resting_stop_price` to mean "the in-place
# amend was NOT attempted (or its outcome is unknown), so the caller must run
# the legacy cancel+resubmit path". It is deliberately distinct from `None`,
# which means "the broker REFUSED the amend, the original stop is still
# resting, and cancelling it now would open a naked window for nothing".
_AMEND_NOT_ATTEMPTED = object()


class StopAmender:
    """Amend resting stop orders in place. Every collaborator is explicit."""

    def __init__(self, *, client, list_open_stop_orders_by_side, snapshot_stop_order):
        self.client = client
        self._list_open_stop_orders_by_side = list_open_stop_orders_by_side
        self._snapshot_stop_order = snapshot_stop_order

    #: Order statuses that mean the amended order is NOT resting at the new
    #: level. Anything else coming back from a replace is treated as live.
    _AMEND_DEAD_STATES = frozenset({"rejected", "canceled", "cancelled", "expired", "done_for_day"})

    @staticmethod
    def _failed_amend_payload(symbol: str, legs: list[dict]) -> dict:
        """The non-success return of an in-place amend.

        A bare `None` preserved protection and threw the evidence away: the
        caller could not tell a partial from a refusal, could not record which
        leg moved, and could not alert. `id` is None so `accepted_stop_order`
        still rejects it — nothing is written back — but the legs travel.
        """
        amended = [l for l in legs if l.get("outcome") == "amended"]
        if any(l.get("outcome") == "naked" for l in legs):
            status = "naked"
        elif any(l.get("outcome") == "unknown" for l in legs):
            status = "unknown"
        elif amended:
            status = "partial"
        elif legs and all(l.get("outcome") == "flat" for l in legs):
            status = "flat"
        else:
            status = "refused"
        return {
            "id": None,
            "status": status,
            "amend_status": status,
            "symbol": symbol,
            "legs": legs,
            "shifted": len(amended),
            "total": len(legs),
        }

    def _classify_after_dead_replacement(self, **kw) -> str:
        """Body moved to stop_dead_replacement.py (see its docstring)."""
        return classify_after_dead_replacement(self, **kw)

    def _amend_one_stop_price(self, *, symbol: str, spec: dict, new_price: float, new_qty: int | None = None) -> dict:
        """Amend ONE resting stop's price and report what is KNOWN afterwards.

        The single place both the trailing path and the ex-dividend shift
        classify an amend's outcome. Sharing only the shape test and leaving
        the failure classification to each caller is sharing the half that does
        not lose money. Returns a leg record whose `outcome` is one of:

          "amended" — the broker answered with a live order id (confirmed);
          "refused" — the broker ANSWERED no (400/404/422, or a dead status),
                      so the ORIGINAL stop is still resting at its old level;
          "unknown" — no broker answer, or an answer with no id. The amend MAY
                      have been applied. Nothing may be cancelled on this
                      outcome and nothing may be STATED about where the stop is.
        """
        leg = {
            "id": str(spec.get("id") or ""),
            "qty": spec.get("qty"),
            "old_stop": spec.get("stop_price"),
            "new_stop": new_price,
            "new_id": None,
            "outcome": "unknown",
            "detail": "",
        }
        request_fields: dict = {"stop_price": new_price}
        # A stop-LIMIT leg keeps its own limit distance from the trigger.
        old_limit = spec.get("limit_price")
        if old_limit and spec.get("stop_price"):
            new_limit = _quantize_price(new_price + (old_limit - spec["stop_price"]))
            if new_limit is not None and new_limit > 0:
                request_fields["limit_price"] = leg["new_limit"] = new_limit
        if new_qty is not None:
            request_fields["qty"] = leg["new_qty"] = int(new_qty)
        try:
            replaced = self.client.replace_order_by_id(
                leg["id"],
                ReplaceOrderRequest(**request_fields),
            )
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self, "stop_amend.amend_one_stop_price", exc, log=logger, context={"leg": str(leg.get("id"))}
            )
            if _is_terminal_broker_rejection(exc):
                leg["outcome"] = "refused"
                leg["detail"] = f"broker refused the amend: {exc}"
            else:
                leg["outcome"] = "unknown"
                leg["detail"] = (
                    f"no broker status on the amend ({exc}) — it may have been applied before the answer was lost"
                )
            return leg
        new_id = str(getattr(replaced, "id", "") or "")
        status_attr = getattr(replaced, "status", None)
        status = str(getattr(status_attr, "value", status_attr) or "accepted").lower()
        if not new_id:
            leg["detail"] = "broker accepted the amend but returned no order id"
            return leg
        leg["new_id"] = new_id
        leg["status"] = status
        if status in self._AMEND_DEAD_STATES:
            leg["detail"] = f"the replacement order came back {status}"
            leg["outcome"] = self._classify_after_dead_replacement(
                symbol=symbol,
                spec=spec,
                new_price=new_price,
                leg=leg,
            )
            return leg
        leg["outcome"] = "amended"
        return leg

    @staticmethod
    def _stop_order_amendable_in_place(order) -> bool:
        """True when `order` is the SHAPE the 2026-09-30 rehearsal measurement
        covered for `replace_order_by_id(stop_price=...)`: one plain, parentless
        stop-MARKET order with no legs.

        Shared by `_amend_resting_stop_price` (trailing) and `shift_stops_down`
        (ex-dividend) so the two paths cannot drift on what "measured-safe"
        means. Every rejection here routes to a cancel+resubmit fallback, which
        is the measured-safe outcome for an unrecognised shape.
        """
        # A bracket/OTO PARENT (it carries `legs`) is not a stop. A CHILD stop
        # leg amends: one `PATCH /orders/{id}` serves every order and the
        # request model excludes no class, while cancelling a child would also
        # pull its OCO sibling. UNMEASURED; a refusal leaves the original resting.
        if getattr(order, "legs", None):
            return False
        # `order_class` and `legs` sit on the PARENT on Alpaca, so a child leg
        # can present as class "" with no legs; `parent_id` is populated ON the
        # child. Children now amend (above), so this only informs the reader.
        # A stop-LIMIT leg amends stop AND limit, so both must be readable.
        otype = getattr(order, "order_type", None) or getattr(order, "type", None)
        otype = str(getattr(otype, "value", otype) or "").lower()
        if otype == "stop_limit":
            try:
                return float(getattr(order, "limit_price", 0) or 0) > 0
            except (TypeError, ValueError):
                return False
        return otype == "stop"

    def _amend_resting_stop_price(
        self,
        *,
        symbol: str,
        live_orders: list,
        stop_specs: list[dict],
        new_stop_price: float,
        position_qty: float,
    ):
        """Amend EVERY resting protective stop's price in place, at one level.

        Returns a {id, status, symbol} dict on success, `None` when the broker
        REFUSED the amend (the original stop is still resting, protection is
        intact, and the caller must NOT cancel it), or `_AMEND_NOT_ATTEMPTED`
        when this path does not apply and the caller should run the legacy
        cancel+resubmit fallback.

        A whole-share QUANTITY amend was ALSO measured working on 2026-09-30
        (3 shares to 2, one open stop); only a FRACTIONAL quantity amend is
        refused, which is why stop_invariant.py cancels a sliver before it grows
        a GTC leg. A bracket/OTO child is UNMEASURED, not known-unamendable.

        Measured against the broker on rehearsal account <redacted-rehearsal-account> on
        2026-09-30: `replace_order_by_id(id, ReplaceOrderRequest(stop_price=X))`
        moves a resting protective stop atomically (old order -> REPLACED, new
        id issued, exactly one open stop on the symbol at every instant), and a
        refused amend leaves the original `new` at its old price. Only the
        shapes that measurement covered take this path; everything else falls
        back.
        """
        if market_is_closed(self.client) is True:
            # Owner ruling 2026-10-02: a shut tape cannot elect a resting stop, so
            # the broker's refusal of an amend out of hours costs nothing. Cancel
            # nothing; the level is owed and recorded for the next open.
            logger.warning(
                "replace_stop_loss: market CLOSED, %s's stop NOT amended to $%.4f; owed to the open",
                symbol,
                new_stop_price,
            )
            return deferred_payload(symbol, stop_specs, new_stop_price)
        if not stop_specs:
            return _AMEND_NOT_ATTEMPTED
        if not all(self._stop_order_amendable_in_place(o) for o in live_orders):
            return _AMEND_NOT_ATTEMPTED
        # MULTI-LEG (item 201). 9 of the 11 open positions are fractional
        # [measured 2026-10-01, production quant_agent.db, read-only]. That a
        # fractional position carries the spec 11.1 hybrid PAIR — a durable GTC
        # whole-share leg plus a DAY sliver leg — is ASSUMED, not measured: the
        # production database holds positions, not an order book. What would
        # settle it: one read of the open orders for a fractional holding. Restricting the atomic path to
        # exactly ONE resting order therefore left the trailing stop cancelling
        # and resubmitting on most of the book while the ex-dividend shift no
        # longer did, which is protection moving in two directions at once.
        # Each leg keeps its own id, quantity and time-in-force under a
        # price-only amend, so moving them all to the new trigger does to the
        # LEVEL exactly what the cancel+resubmit fallback already did, minus
        # the unprotected window.
        try:
            covered = sum(abs(float(spec["qty"])) for spec in stop_specs)
        except (TypeError, ValueError, KeyError):
            return _AMEND_NOT_ATTEMPTED
        # One whole-share leg on a whole-share position: the quantity amend was
        # measured working (2026-09-30), so it rides along with the price amend.
        # Every other coverage gap is closed AFTER the price amend, on the
        # confirmed book, by stop_invariant.py (item 201) -- never by cancelling.
        new_qty = None
        if (
            abs(covered - position_qty) > 1e-9
            and len(stop_specs) == 1
            and covered == int(covered)
            and position_qty == int(position_qty)
            and position_qty >= 1
        ):
            new_qty = int(position_qty)
        price = _quantize_price(new_stop_price)
        if price is None or price <= 0:
            return _AMEND_NOT_ATTEMPTED

        legs = [
            self._amend_one_stop_price(symbol=symbol, spec=spec, new_price=price, new_qty=new_qty)
            for spec in stop_specs
        ]
        amended = [l for l in legs if l["outcome"] == "amended"]
        unknown = [l for l in legs if l["outcome"] == "unknown"]
        if any(l["outcome"] == "naked" for l in legs):
            # Read off the broker, not inferred: the symbol has no resting
            # protective stop. Say UNPROTECTED and let coverage repair place
            # one; cancelling or resubmitting from here would race it.
            # Coverage repair has ALREADY run this session — `_reconcile_stop_
            # coverage` executes earlier in the same position review than the
            # trails — so the gap is NOT closed by this session. It is closed
            # by the next intra sweep, which is why the owner is alerted.
            logger.error(
                "replace_stop_loss: after a dead replacement %s has NO resting "
                "protective stop — the position is UNPROTECTED, nothing was "
                "cancelled, and this session's coverage repair has already "
                "run, so the gap persists until the NEXT intra sweep",
                symbol,
            )
            return self._failed_amend_payload(symbol, legs)
        if len(amended) == len(legs):
            logger.info(
                "Trailing stop AMENDED IN PLACE for %s: %d leg(s) moved to "
                "$%.4f, quantities unchanged (no cancel, no unprotected window)",
                symbol,
                len(legs),
                price,
            )
            return {
                "id": amended[0]["new_id"],
                "status": amended[0].get("status", "accepted"),
                "amend_status": "accepted",
                "symbol": symbol,
                "legs": legs,
            }
        if unknown:
            # The amend MAY have landed. Cancelling now could cancel a stop the
            # broker already moved, so the fallback must NOT run: take the
            # "refused, do not cancel" channel and let the next pass re-read.
            logger.error(
                "replace_stop_loss: %d of %d in-place amends for %s came back "
                "with NO broker answer — the desk does not know which level "
                "each leg is at; nothing cancelled, nothing written back, "
                "re-read the book before trailing %s again",
                len(unknown),
                len(legs),
                symbol,
                symbol,
            )
            return self._failed_amend_payload(symbol, legs)
        if amended:
            # NARROW HEAL (item 201, adversary round 3). Only the legs THIS
            # amend failed to move, and only to THIS proposal's intended
            # level. Deliberately NOT "the most protective level already
            # resting": per-lot stop levels are a design choice the desk
            # maintains (see `shift_stops_down`'s docstring on the audit-round-2
            # fix), so collapsing them would tighten a lot the desk chose to
            # keep wide — a worse failure than the straddle. A leg whose
            # outcome is unknown or naked is never retried, because the desk
            # does not know where it is.
            #
            # Exactly ONE retry, inside the trailing proposal's own gates
            # (ratchet floor, tightening cooldown, no-proposal-on-a-stalled-
            # price): nothing here re-attempts an unchanged outcome on a later
            # pass, so a leg the broker keeps refusing cannot spin.
            laggards = [l for l in legs if l["outcome"] == "refused"]
            if laggards:
                retried = {}
                for lag in laggards:
                    again = self._amend_one_stop_price(
                        symbol=symbol,
                        spec={"id": lag["id"], "qty": lag["qty"], "stop_price": lag["old_stop"]},
                        new_price=price,
                        new_qty=new_qty,
                    )
                    again["retry_of"] = lag["id"]
                    retried[lag["id"]] = again
                legs = [retried.get(l["id"], l) for l in legs]
                amended = [l for l in legs if l["outcome"] == "amended"]
                unknown = [l for l in legs if l["outcome"] == "unknown"]
                if len(amended) == len(legs):
                    logger.info(
                        "replace_stop_loss: %s's lagging stop leg(s) came up to "
                        "$%.4f on one retry — all %d leg(s) now at the intended "
                        "level, nothing cancelled",
                        symbol,
                        price,
                        len(legs),
                    )
                    return {
                        "id": amended[0]["new_id"],
                        "status": amended[0].get("status", "accepted"),
                        "amend_status": "accepted",
                        "symbol": symbol,
                        "legs": legs,
                    }
            if not amended:
                return self._failed_amend_payload(symbol, legs)
            logger.error(
                "replace_stop_loss: only %d of %d stop legs for %s moved to "
                "$%.4f; the rest were REFUSED and are still resting at their "
                "old levels even after one retry. Coverage is intact and "
                "nothing was cancelled, and the straddle is LEFT IN PLACE: "
                "pulling the laggards to another resting level would collapse "
                "per-lot geometry the desk maintains on purpose. The next "
                "accepted proposal moves them all together.",
                len(amended),
                len(legs),
                symbol,
                price,
            )
            return self._failed_amend_payload(symbol, legs)
        logger.warning(
            "replace_stop_loss: the broker REFUSED the in-place amend of all "
            "%d stop leg(s) for %s to $%.4f — the ORIGINAL stops are still "
            "resting, so protection is intact and nothing is cancelled",
            len(legs),
            symbol,
            price,
        )
        return self._failed_amend_payload(symbol, legs)
