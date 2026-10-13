"""Protective-stop submission, restore and replacement, lifted verbatim from AlpacaBroker.

Second broker instalment. A `StopPlacer` is built from its collaborators alone
(keyword-only), so stop submission with retry, restore after a cancel, `shift_stops_down` and `replace_stop_loss` run without an
`AlpacaBroker` or a TradingPipeline. `AlpacaBroker` keeps same-named thin
shims that build one per call, so a test that swaps the client (or one of the
cluster's own methods) after construction still hits the swap.

The module-level helpers and constants the bodies read as globals moved with
them; `src.execution.broker` re-exports every one so existing importers and
patch targets still resolve.
"""

from __future__ import annotations

import logging
import math
import re
import time

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import StopOrderRequest

from src.execution.broker_parts.stop_shift import shift_stops_down as _shift_stops_down_via_part
from src.execution.broker_parts.stop_window import UnprotectedWindow, fallback_reason
from src.execution.broker_parts.stop_amend import (
    _AMEND_NOT_ATTEMPTED,
    _is_terminal_broker_rejection,
    _quantize_price,
)
from src.execution.broker_parts.stop_clock import defer_if_closed, reprotect_or_naked
from src.execution.broker_parts.stop_invariant import enforce_stop_quantity_invariant
from src.execution.stop_records import STOP_USABLE, classify_stop_price

# Quantity rules + the quantity gate live in src/execution/order_gates.py (a
# leaf); the two names are re-exported here because callers patch them here.
from src.execution.order_gates import (  # noqa: F401 (re-exports keep patch targets)
    _FRACTIONAL_QTY_EPSILON,
    _split_protective_qty,
    _derive_stop_tif,
    BadOrderQuantity,
    check_order_quantity,
)
from src.execution.order_statuses import (  # noqa: F401 (re-exports keep patch targets)
    PROTECTIVE_ORDER_ACTIVE_STATUSES,
    PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES,
    PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES,
)
from src.execution import (
    order_idempotency as _idem,
)  # session key read via the module so one patch target serves every path
from src.execution.order_idempotency import (
    _is_dead_stop_result,
    _submit_stop_request_idempotent,
)

# No ledger handle on this per-call object: traceback is logged, the counted row is skipped until
# one is lent (flagged, no new channel).
from src.sentinel.guarded import record_guarded_pass

# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
logger = logging.getLogger("src.execution.broker")

_STOP_PLACEMENT_MAX_ATTEMPTS = 3

_STOP_PLACEMENT_BACKOFF_S = (0.5, 1.5)

# Spec §11.1 hybrid fractional stops: the measured broker rule, `_FRACTIONAL_QTY_EPSILON`,
# `_split_protective_qty` and `_derive_stop_tif` live in src/execution/order_gates.py (re-exported above).

from src.execution.broker_parts.stop_rejections import (  # noqa: F401
    _is_held_for_orders_error,
    log_terminal_stop_rejection,
    real_broker_order_id,
)


# Spelling translation moved to stop_symbols.py (re-exported; same patch targets).
from src.execution.broker_parts.stop_symbols import (  # noqa: E402,F401
    _alpaca_symbol,
    _internal_symbol,
)
# The protective-order status vocabulary lives in src/execution/order_statuses.py (re-exported above).


class StopPlacer:
    """Place, submit, restore and replace protective stops. Every collaborator is explicit.

    The six cluster-internal collaborators default to this object's own bodies;
    the broker shim passes its own (patchable) bound methods instead.
    """

    def __init__(
        self,
        *,
        client,
        list_open_stop_orders_by_side,
        list_open_protective_stop_orders,
        list_open_sell_stop_orders,
        snapshot_stop_order,
        amend_one_stop_price,
        amend_resting_stop_price,
        stop_order_amendable_in_place,
        cancel_snapshotted_stops,
        get_positions,
        kill_switch_active,
        kill_switch_path,
        protective_stop_block_recorder,
        submit_protective_stop_retrying=None,
        submit_stop_leg_retrying=None,
        existing_stop_covering_qty=None,
        submit_stop_limit_order=None,
        submit_stop_legs=None,
        restore_stop_orders=None,
        window_log=None,
        wait_for_order_terminal=None,
    ):
        self.client, self.wait_for_order_terminal = client, wait_for_order_terminal
        self._window_log = [] if window_log is None else window_log
        self._list_open_stop_orders_by_side = list_open_stop_orders_by_side
        self._list_open_protective_stop_orders = list_open_protective_stop_orders
        self._list_open_sell_stop_orders = list_open_sell_stop_orders
        self._snapshot_stop_order = snapshot_stop_order
        self._amend_one_stop_price = amend_one_stop_price
        self._amend_resting_stop_price = amend_resting_stop_price
        self._stop_order_amendable_in_place = stop_order_amendable_in_place
        self.cancel_snapshotted_stops = cancel_snapshotted_stops
        self.get_positions = get_positions
        self._kill_switch_active = kill_switch_active
        self._kill_switch_path = kill_switch_path
        self.protective_stop_block_recorder = protective_stop_block_recorder
        if submit_protective_stop_retrying is not None:
            self._submit_protective_stop_retrying = submit_protective_stop_retrying
        if submit_stop_leg_retrying is not None:
            self._submit_stop_leg_retrying = submit_stop_leg_retrying
        if existing_stop_covering_qty is not None:
            self._existing_stop_covering_qty = existing_stop_covering_qty
        if submit_stop_limit_order is not None:
            self._submit_stop_limit_order = submit_stop_limit_order
        if submit_stop_legs is not None:
            self._submit_stop_legs = submit_stop_legs
        if restore_stop_orders is not None:
            self._restore_stop_orders = restore_stop_orders

    shift_stops_down = _shift_stops_down_via_part  # body: stop_shifter.StopShifter

    def _existing_stop_covering_qty(
        self,
        symbol: str,
        *,
        qty: float,
        side: str,
        stop_price: float,
    ) -> dict | None:
        """Return a live stop dict if the broker already covers this sliver.

        Qty match uses the same fractional epsilon as the hybrid split —
        not a trading threshold. Price match is Alpaca's published tick
        (half-tick, same as stop_records._prices_match).
        """
        try:
            orders = self._list_open_protective_stop_orders(symbol, side=side)
            record_guarded_pass(self, "stop_place.existing_stop_covering_qty.list", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self,
                "stop_place.existing_stop_covering_qty.list",
                exc,
                log=logger,
                context={**{"symbol": symbol}, "effect": "treated as no matching stop"},
            )
            return None
        tick = 0.01 if stop_price >= 1.0 else 0.0001
        for order in orders or []:
            snap = self._snapshot_stop_order(order)
            if snap is None:
                continue
            if abs(float(snap.get("qty") or 0) - qty) > _FRACTIONAL_QTY_EPSILON:
                continue
            live_px = float(snap.get("stop_price") or 0)
            if live_px <= 0:
                continue
            if abs(live_px - stop_price) > (tick / 2.0):
                continue
            return {
                "id": snap["id"],
                "qty": snap["qty"],
                "stop_price": snap["stop_price"],
                "already_live": True,
            }
        return None

    def _submit_stop_leg_retrying(
        self,
        *,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: float | None = None,
        side: str,
        leg: str,
    ) -> dict | None:
        """One protective-stop LEG, with spec §11.1 guard 1's retry burst.

        Extracted from `_submit_protective_stop_retrying` so the hybrid split
        can run the identical retry discipline over each of its two legs
        instead of a second, weaker copy of it. `leg` is log context only
        ('GTC', 'GTC whole-share', 'DAY fractional') — the tif itself is
        derived from `qty` inside `_submit_stop_limit_order` and is not a
        decision made here.

        Returns the broker's response dict, or None when every attempt
        failed. Never raises.

        A kill-switch refusal does NOT raise (`_submit_stop_limit_order`
        returns a dict with `id=None`) — until this check existed that fell
        straight through to the success branch below and was logged and
        returned as a PLACED stop. The switch is a stable ops halt, not a
        transient broker error, so this does not burn the retry burst on
        it: one refusal is reported as blocked and the leg fails now,
        exactly as if the broker itself had refused every attempt.
        """
        attempts = max(1, int(_STOP_PLACEMENT_MAX_ATTEMPTS))
        last_exc: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                order = self._submit_stop_limit_order(
                    symbol=symbol,
                    qty=qty,
                    stop_price=stop_price,
                    limit_price=limit_price,
                    side=side,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                record_guarded_pass(
                    self,
                    "stop_place.submit_stop_leg_retrying",
                    exc,
                    log=logger,
                    context={"symbol": symbol, "leg": leg, "attempt": attempt},
                )
                logger.error(
                    "protective stop [%s] attempt %d/%d FAILED for %s (qty=%.4f, stop $%.2f): %s",
                    leg,
                    attempt,
                    attempts,
                    symbol,
                    qty,
                    stop_price,
                    exc,
                )
                if _is_terminal_broker_rejection(exc):
                    # Board item 129: a terminal 400/404/422 fails identically on every retry.
                    log_terminal_stop_rejection(logger, leg, symbol, exc, attempt, attempts)
                    break
                if attempt < attempts:
                    delay = _STOP_PLACEMENT_BACKOFF_S[min(attempt - 1, len(_STOP_PLACEMENT_BACKOFF_S) - 1)]
                    time.sleep(delay)
                continue
            if _is_dead_stop_result(order, PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES):
                # Adversary 2026-10-01: a dead-status response holds no shares.
                logger.error(
                    "protective stop [%s] attempt %d/%d for %s came back with "
                    "status %r — not a live stop; not reporting it as placed.",
                    leg,
                    attempt,
                    attempts,
                    symbol,
                    order.get("status"),
                )
                last_exc = None
                if attempt < attempts:
                    delay = _STOP_PLACEMENT_BACKOFF_S[min(attempt - 1, len(_STOP_PLACEMENT_BACKOFF_S) - 1)]
                    time.sleep(delay)
                continue
            from src.execution.exit_path_records import is_kill_switch_block

            if is_kill_switch_block(order):
                logger.critical(
                    "protective stop [%s] for %s qty=%.4f BLOCKED by the "
                    "desk's own kill switch — nothing was sent to the "
                    "broker; NOT reporting this as placed.",
                    leg,
                    symbol,
                    qty,
                )
                return None
            if attempt > 1:
                logger.warning(
                    "protective stop [%s] placed for %s on attempt %d/%d — the "
                    "position was briefly unprotected and is now covered",
                    leg,
                    symbol,
                    attempt,
                    attempts,
                )
            else:
                logger.info(
                    "entry protection: [%s] %s protective stop placed for %s qty=%.4f @ stop $%.2f",
                    leg,
                    side,
                    symbol,
                    qty,
                    stop_price,
                )
            return order
        # Concurrent morning + intra_check both repair the same DAY sliver:
        # the second hits held_for_orders because the first already placed
        # it. Treat an existing stop covering this qty as success — do not
        # leave the remainder flagged uncovered when the broker already
        # holds the order.
        if last_exc is not None and _is_held_for_orders_error(last_exc):
            existing = self._existing_stop_covering_qty(
                symbol,
                qty=qty,
                side=side,
                stop_price=stop_price,
            )
            if existing is not None:
                logger.info(
                    "protective stop [%s] for %s qty=%.4f already live at "
                    "the broker (held_for_orders on submit) — treating as "
                    "covered",
                    leg,
                    symbol,
                    qty,
                )
                return existing
        return None

    def _submit_protective_stop_retrying(
        self,
        *,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: float | None = None,
        side: str,
    ) -> dict | None:
        """Spec §11.1 guard 1 — submit protective stop coverage for `qty`,
        retrying immediately and hard on failure. Returns a stop order dict,
        or None when nothing at all could be placed.

        The retry happens HERE, in the same call, milliseconds after the
        failure — not queued, not deferred to the next 30-minute sweep. The
        position is already open; a deferred retry is an open position with
        no stop for however long the defer lasts, which is the exact failure
        mode §11.1 was required to bound. See `_STOP_PLACEMENT_MAX_ATTEMPTS`
        for why the budget is three attempts over ~2 seconds and not more.

        Never raises. Returning None is the signal the CALLER must escalate
        on — a naked position that nobody is told about is strictly worse
        than one that fails loudly.

        WHOLE SHARE COUNTS (every short, and every long while
        `execution.fractional_enabled` is off) take a single GTC stop and
        this function behaves exactly as it always has, down to the returned
        shape: the broker's own response, untouched.

        A FRACTIONAL qty takes the HYBRID split instead, because the broker
        will not carry one durable order over it (measured 2026-09-01; see
        the `_FRACTIONAL_QTY_EPSILON` block comment):

            leg A   GTC stop over floor(qty)   — durable, survives the close
            leg B   DAY stop over the sub-share remainder — lapses at 16:00
                    ET BY DESIGN and is re-placed by the next session's
                    coverage sweep

        Under one share there is no leg A, so a sub-share position carries a
        DAY stop only. This REPLACES the old "try the exact fractional qty
        three times, then fall back to a whole-share stop" path: those three
        attempts are now known to be three guaranteed rejections costing ~2
        seconds of naked position each time, and the whole-share fallback
        they led to is exactly leg A, reached immediately instead.

        The returned dict is leg A's response (leg B's when there is no leg
        A) annotated with `covered_qty` / `uncovered_qty` / `gtc_qty` /
        `day_qty` / `hybrid`. `uncovered_qty` keeps the meaning every caller
        already reads it with: shares the broker is NOT watching right now.
        It is 0.0 when both legs land, which is the ordinary fractional
        success — so guard 2 stays silent on success and still fires on a
        genuine partial cover.
        """
        # docs/WORK.md item 88. Refuse a garbage trigger BEFORE burning the
        # retry burst on it: a zero/negative/NaN/Inf stop is not a transient
        # broker failure, so three attempts and ~2 seconds of sleeps cannot
        # turn it into a placed order. None is the caller's escalate signal
        # and every caller on this path already treats it as one, so the
        # gap stays flagged instead of being reported as covered.
        if classify_stop_price(stop_price)[0] != STOP_USABLE:
            logger.critical(
                "protective stop REFUSED for %s (qty=%s): the requested "
                "trigger %r cannot be a stop price. Nothing was placed and "
                "the position stays flagged as uncovered — a garbage stop "
                "is never treated as 'no stop needed'.",
                symbol,
                qty,
                stop_price,
            )
            return None

        whole, frac = _split_protective_qty(qty)
        if frac <= 0:
            # Whole-share: unchanged in every observable way.
            return self._submit_stop_leg_retrying(
                symbol=symbol,
                qty=qty,
                stop_price=stop_price,
                limit_price=limit_price,
                side=side,
                leg="GTC",
            )

        logger.info(
            "protective stop for %s is HYBRID: DAY over %s sub-share remainder "
            "+ GTC over %.0f whole share(s), both @ stop $%.2f. DAY is placed "
            "first so the GTC hold cannot starve the sliver (held_for_orders). "
            "The DAY leg lapses at the close by design and is re-placed at "
            "the next session's open.",
            symbol,
            frac,
            whole,
            stop_price,
        )
        day_order = self._submit_stop_leg_retrying(
            symbol=symbol,
            qty=frac,
            stop_price=stop_price,
            limit_price=limit_price,
            side=side,
            leg="DAY fractional",
        )
        if day_order is None:
            logger.error(
                "protective stop: the DAY fractional leg FAILED for %s (%s "
                "share(s), stop $%.2f) after %d attempt(s) — the sub-share "
                "remainder is uncovered NOW, during the session, which is not "
                "the expected overnight lapse.",
                symbol,
                frac,
                stop_price,
                _STOP_PLACEMENT_MAX_ATTEMPTS,
            )
        gtc_order = None
        if whole >= 1:
            gtc_order = self._submit_stop_leg_retrying(
                symbol=symbol,
                qty=whole,
                stop_price=stop_price,
                limit_price=limit_price,
                side=side,
                leg="GTC whole-share",
            )
            if gtc_order is None:
                logger.critical(
                    "protective stop: the DURABLE whole-share GTC leg FAILED "
                    "for %s (%.0f share(s), stop $%.2f) after %d attempt(s). "
                    "This is the leg that must never be missing; the caller "
                    "alerts the owner.",
                    symbol,
                    whole,
                    stop_price,
                    _STOP_PLACEMENT_MAX_ATTEMPTS,
                )

        gtc_qty = whole if gtc_order is not None else 0.0
        day_qty = frac if day_order is not None else 0.0
        covered = gtc_qty + day_qty
        if covered <= 0:
            return None
        # A COPY — never annotate the broker's own response object in place;
        # a caller holding that dict must not have its shape changed
        # underneath it. Leg A is the base when it exists: it is the durable
        # order, and it is the id worth carrying forward.
        base = gtc_order if gtc_order is not None else day_order
        return {
            **base,
            "covered_qty": covered,
            "uncovered_qty": max(0.0, round(qty - covered, 9)),
            "gtc_qty": gtc_qty,
            "day_qty": day_qty,
            "gtc_stop_id": (gtc_order or {}).get("id"),
            "day_stop_id": (day_order or {}).get("id"),
            "hybrid": True,
        }

    def _submit_stop_limit_order(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: float | None = None,
        *,
        side: str = "sell",
    ) -> dict:
        """Submit a protective stop. `side` is the STOP ORDER's own
        side — "sell" (default) protects a long and fires as price falls;
        "buy" protects a short and fires as price rises. Defaults to "sell"
        so every pre-shorts call site (none of which pass `side`) keeps its
        behaviour.

        PRIMARY: a stop-MARKET (`StopOrderRequest`) — owner ratified
        2026-09-25 for a GUARANTEED exit. An elected market stop fills at the
        next print instead of resting unfilled past a limit on a gap, which
        is the exposure the stop exists to close. `limit_price` is therefore
        IGNORED on the primary order (a market stop has no limit).

        NO FALLBACK. The stop-LIMIT fallback (and its 3% buffer) was deleted
        2026-10-13: it was taken 0 times in 60 recorded placements. A broker
        refusal of the stop-MARKET now RAISES like any other refusal, so
        `_submit_protective_stop_retrying` retries it and, if every attempt
        fails, logs CRITICAL "UNPROTECTED" and returns None — the caller's
        owner alert and the coverage repair then take over. `limit_price` is
        accepted for caller compatibility and ignored.

        TIME IN FORCE IS DERIVED FROM `qty`, NOT PASSED IN (spec §11.1
        hybrid fractional stops). Whole share counts get GTC exactly as
        before — every existing call site is byte-identical. A FRACTIONAL
        qty gets DAY, because the broker refuses a fractional GTC order
        outright (measured; see `_derive_stop_tif`). Putting the rule here
        rather than at each call site means every path that can submit a
        fractional stop — entry protection, the coverage repair, the WAL
        restore, the partial-sell reprotect, the ex-dividend shift — becomes
        broker-legal at once, and no future path can forget it.
        """
        if self._kill_switch_active():
            # Guard 1: a protective stop is risk-REDUCING (it only ever
            # tightens protection), yet the kill switch still blocks it —
            # this is the one deliberate exception in the codebase; see
            # RiskConfig.kill_switch_path. An already-resting stop from
            # before the halt is untouched; this only refuses a NEW one.
            logger.error(
                "KILL SWITCH ACTIVE (%s exists): refusing protective stop for %s qty=%s stop=$%.4f.",
                self._kill_switch_path,
                symbol,
                qty,
                stop_price,
            )
            # Until 2026-09-19 this refusal left no record, and the repair
            # path told the owner the BROKER had refused the stop. The
            # durable row goes through the recorder the database's owner
            # wires in; `detail` is the plain sentence any caller can show.
            from src.execution.exit_path_records import kill_switch_blocked_text

            recorder = self.protective_stop_block_recorder
            if recorder is not None:
                try:
                    recorder(
                        symbol=symbol,
                        qty=qty,
                        stop_price=stop_price,
                        side=side,
                        kill_switch_path=str(self._kill_switch_path),
                    )
                    record_guarded_pass(self, "stop_place.kill_switch_block_record", context={"symbol": symbol})
                except Exception as exc:  # noqa: BLE001 — never trading authority
                    record_guarded_pass(
                        self,
                        "stop_place.kill_switch_block_record",
                        exc,
                        log=logger,
                        context={**{"symbol": symbol}, "effect": "block record not written"},
                    )
            return {
                "id": None,
                "status": "kill_switch_halted",
                "symbol": symbol,
                "blocked_by": "kill_switch",
                "detail": kill_switch_blocked_text(symbol),
            }
        # docs/WORK.md item 88 — the LAST authority before the broker, for
        # the callers that reach this directly (the partial-exit reprotect
        # and the restore paths) rather than through
        # `_submit_protective_stop_retrying`. A zero trigger quantizes to
        # 0.0 and a non-finite one quantizes to None, and both used to be
        # handed to the SDK: one becomes a broker rejection, the other a
        # serializer error, and neither says what was wrong. Raising is the
        # contract this method's callers already handle ("the submit either
        # worked or it raised"), so a garbage stop can never be mistaken
        # for a placed one.
        if classify_stop_price(stop_price)[0] != STOP_USABLE:
            raise ValueError(
                f"refusing a protective stop for {symbol}: the requested "
                f"trigger {stop_price!r} is not a usable stop price "
                f"(must be finite and positive)"
            )
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        time_in_force = _derive_stop_tif(qty)
        if time_in_force is TimeInForce.DAY:
            logger.info(
                "protective stop for %s is FRACTIONAL (qty=%s) — submitting "
                "DAY, the only tif the broker accepts for a fractional order. "
                "It lapses at the close and is re-placed by the next session's "
                "coverage sweep.",
                symbol,
                qty,
            )
        qty_refusal = check_order_quantity(qty, side=side)
        if qty_refusal is not None:  # same contract: refused = raised
            raise BadOrderQuantity(f"{symbol}: {qty_refusal}")
        stop_price_q = _quantize_price(stop_price)
        # PRIMARY: stop-MARKET (guaranteed exit) — owner ratified 2026-09-25.
        # Idempotency keys + duplicate read-back: src/execution/order_idempotency.py
        # (trigger price is in the key, so a ratcheted trail is a NEW intent).
        session_date = _idem._session_date_key()

        def _market_request(client_order_id: str) -> StopOrderRequest:
            return StopOrderRequest(
                symbol=_alpaca_symbol(symbol),
                qty=qty,
                side=order_side,
                time_in_force=time_in_force,
                stop_price=stop_price_q,
                client_order_id=client_order_id,
            )

        order = _submit_stop_request_idempotent(
            self.client,
            _market_request,
            purpose="STP",
            alpaca_symbol=_alpaca_symbol(symbol),
            side=side,
            session_date=session_date,
            qty=qty,
            price=stop_price_q,
            holds_shares_statuses=PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES,
        )
        # Unwrap OrderStatus enum value (see submit_order — same reason).
        return {
            "id": str(order.id),
            "status": str(getattr(order.status, "value", order.status)),
            "symbol": _internal_symbol(symbol),
        }

    def _submit_stop_legs(
        self,
        *,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: float | None = None,
        side: str = "sell",
    ) -> list[dict]:
        """Submit the protective stop LEG(S) covering `qty`, all-or-nothing.

        A whole share count is ONE GTC stop — byte-identical to the bare
        `_submit_stop_limit_order` call this replaced. A fractional qty is
        the spec §11.1 hybrid PAIR: a durable GTC stop over floor(qty) plus a
        DAY stop over the sub-share remainder, both at the same trigger.

        WHY EVERY RE-PLACEMENT PATH NEEDS THIS, not just entry protection.
        `replace_stop_loss` (trailing) and the partial-sell reprotect both
        cancel a position's existing stops and submit ONE order for the whole
        remaining quantity. On a fractional position that single order is
        necessarily fractional, therefore necessarily DAY, therefore gone at
        16:00 ET — and the position would have SILENTLY LOST its durable
        whole-share GTC leg. The next overnight sweep would then see zero
        coverage on 12.3456 held shares, correctly call it NO STOP AT ALL,
        and page the owner. A trailing-stop ratchet must not be able to
        convert a properly protected position into a nightly false alarm.

        Raises if any leg is rejected, after cancelling any leg that already
        landed. Callers here have an all-or-nothing rollback contract ("the
        submit either worked or it raised"); a half-placed pair left behind
        would be read by the coverage sweep as a mis-sized stop and by the
        rollback as nothing at all.
        """
        whole, frac = _split_protective_qty(qty)
        # DAY remainder FIRST. Measured 2026-09-16: placing the GTC
        # whole-share leg first made Alpaca report held_for_orders /
        # insufficient qty on the 0.4393 BRK-B DAY sliver (the GTC hold
        # reserved the position). The remainder is the smaller qty; placing
        # it first leaves the whole shares free for the durable GTC.
        legs: list[float] = []
        if frac > 0:
            legs.append(frac)
        if whole >= 1:
            legs.append(whole)
        if not legs:
            legs = [qty]
        placed: list[dict] = []
        for leg_qty in legs:
            try:
                leg_order = self._submit_stop_limit_order(
                    symbol=symbol,
                    qty=leg_qty,
                    stop_price=stop_price,
                    limit_price=limit_price,
                    side=side,
                )
                # A kill-switch refusal does not raise (`id=None` dict) —
                # this docstring's own "either worked or raised" contract
                # means a refusal MUST become an exception here too, or the
                # caller (replace_stop_loss) logs and returns it as a
                # placed trailing stop. Raising drives the same
                # already-placed-leg rollback below as any other failure.
                from src.execution.exit_path_records import is_kill_switch_block

                if is_kill_switch_block(leg_order):
                    raise RuntimeError(
                        f"protective stop leg for {symbol} qty={leg_qty} blocked by the desk's own kill switch"
                    )
                placed.append(leg_order)
            except Exception:
                for done in placed:
                    try:
                        self.client.cancel_order_by_id(done.get("id"))
                        record_guarded_pass(
                            self,
                            "stop_place.submit_stop_legs.rollback_cancel",
                            context={"symbol": symbol, "order": done.get("id")},
                        )
                    except Exception as cancel_exc:  # noqa: BLE001
                        record_guarded_pass(
                            self,
                            "stop_place.submit_stop_legs.rollback_cancel",
                            cancel_exc,
                            log=logger,
                            context={
                                **{"symbol": symbol, "order": done.get("id")},
                                "effect": "partial leg may remain; coverage sweep reconciles",
                            },
                        )
                raise
        return placed

    def _restore_stop_orders(
        self,
        symbol: str,
        stop_specs: list[dict],
        *,
        check_idempotency: bool = False,
        side: str = "sell",
    ) -> tuple[int, list[dict]]:
        """Re-submit a set of cancelled stop specs. Best-effort per-spec —
        a single broker rejection doesn't abort the loop.

        `side` is the specs' own side — "sell" (default) restores stops that
        protect a long. `replace_stop_loss` is the one caller that can pass
        `side="buy"`, when the position it's trailing is a short; every
        other caller only ever restores a long's SELL stops, so the default
        keeps them unchanged.

        ``check_idempotency`` controls whether we first query broker for
        already-alive stops and skip matching specs:

        - **False (default; in-line rollback path)** — the caller just
          cancelled these specs moments ago in the same method
          (cancel_protective_stops or replace_stop_loss partial-cancel
          rollback). The cancelled stops are not alive anymore at the
          broker by construction; checking would just slow the rollback
          and risk false-positives on Alpaca's eventual-consistency
          window (pending_cancel orders sometimes still appear in
          get_orders briefly).

        - **True (drain path)** — the caller is replaying a recovery
          intent persisted from a previous session. Specs that landed
          successfully in an earlier drain pass are alive at the broker;
          re-submitting them now would trigger held_for_orders /
          duplicate-protection rejections. Query open sell-stops first
          and skip specs whose (qty, stop_price) match a live stop
          within 1¢ tolerance. This closes the drain re-submission race
          documented in the design audit — finalize's
          reprotect-raised / restore-raised paths return the full
          cancelled_specs list, and drain's length-equality narrowing
          (pipeline.py:1039) can't distinguish "all failed" from
          "partial succeeded then raised", so it leaves the row's
          specs unchanged. Idempotency defends against the resulting
          re-submit dupes at the broker layer.

        Returns ``(restored_count, failed_specs)``. With idempotency on,
        ``restored_count`` includes already-alive-skipped specs (from
        the caller's perspective, coverage is intact either way).
        """
        existing_alive: list[dict] = []
        if check_idempotency:
            try:
                for order in self._list_open_protective_stop_orders(symbol, side=side):
                    snap = self._snapshot_stop_order(order)
                    if snap is not None:
                        existing_alive.append(snap)
                record_guarded_pass(self, "stop_place.restore_stop_orders.list_existing", context={"symbol": symbol})
            except Exception as exc:
                # If we can't see existing stops, fall through to the
                # non-idempotent behavior — broker's own duplicate
                # detection is the last line.
                record_guarded_pass(
                    self,
                    "stop_place.restore_stop_orders.list_existing",
                    exc,
                    log=logger,
                    context={**{"symbol": symbol}, "effect": "idempotency check skipped"},
                )

        def _spec_matches(spec: dict, alive: dict) -> bool:
            """Two specs match when qty and stop_price are within rounding."""
            try:
                if abs(float(spec.get("qty", 0)) - float(alive.get("qty", 0))) > 1e-6:
                    return False
                spec_stop = float(spec.get("stop_price", 0))
                alive_stop = float(alive.get("stop_price", 0))
                # 1 cent tolerance covers _quantize_price rounding.
                return abs(spec_stop - alive_stop) <= 0.01
            except (TypeError, ValueError):
                return False

        restored = 0
        skipped_already_alive = 0
        failed_specs: list[dict] = []
        for spec in stop_specs:
            if existing_alive and any(_spec_matches(spec, alive) for alive in existing_alive):
                # Already alive at broker (likely landed in a prior
                # drain pass). Treat as restored from the caller's
                # perspective; do NOT re-submit.
                skipped_already_alive += 1
                restored += 1
                logger.info(
                    "_restore_stop_orders: %s @ $%.2f qty=%s already alive at broker — skipping re-submit (idempotent)",
                    symbol,
                    float(spec.get("stop_price", 0)),
                    spec.get("qty"),
                )
                continue
            try:
                restore_result = self._submit_stop_limit_order(
                    symbol=symbol,
                    qty=spec["qty"],
                    stop_price=spec["stop_price"],
                    limit_price=spec.get("limit_price"),
                    side=side,
                )
                record_guarded_pass(self, "stop_place.restore_stop_orders.resubmit", context={"symbol": symbol})
            except Exception as exc:
                record_guarded_pass(
                    self,
                    "stop_place.restore_stop_orders.resubmit",
                    exc,
                    log=logger,
                    context={**{"symbol": symbol}, "effect": "prior stop not restored"},
                )
                failed_specs.append(spec)
                continue
            # A kill-switch refusal does not raise — it comes back as a
            # dict with `id=None` — so without this check the loop above
            # counted a refused restore as `restored += 1`, and the caller
            # (the WAL drain, and replace_stop_loss's own rollback) then
            # treated the position as re-covered and discharged its
            # recovery row over a stop that was never sent to the broker.
            if _is_dead_stop_result(restore_result, PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES):
                # Adversary 2026-10-01: a dead-status restore is NOT coverage.
                logger.critical(
                    "replace_stop_loss: restore of prior stop for %s @ $%.2f came "
                    "back with status %r — NOT a live stop; NOT counting this as "
                    "restored; the position stays flagged uncovered.",
                    symbol,
                    spec["stop_price"],
                    restore_result.get("status"),
                )
                failed_specs.append(spec)
                continue
            from src.execution.exit_path_records import is_kill_switch_block

            if is_kill_switch_block(restore_result):
                logger.critical(
                    "replace_stop_loss: restore of prior stop for %s @ "
                    "$%.2f BLOCKED by the desk's own kill switch — NOT "
                    "counting this as restored; the position stays flagged "
                    "uncovered.",
                    symbol,
                    spec["stop_price"],
                )
                failed_specs.append(spec)
                continue
            restored += 1
        if restored:
            new_submits = restored - skipped_already_alive
            if skipped_already_alive:
                logger.warning(
                    "replace_stop_loss rollback: restored %d/%d prior stop order(s) "
                    "for %s (%d newly submitted, %d already alive)",
                    restored,
                    len(stop_specs),
                    symbol,
                    new_submits,
                    skipped_already_alive,
                )
            else:
                logger.warning(
                    "replace_stop_loss rollback: restored %d/%d prior stop order(s) for %s",
                    restored,
                    len(stop_specs),
                    symbol,
                )
        return restored, failed_specs

    def replace_stop_loss(
        self,
        symbol: str,
        new_stop_price: float,
        *,
        allow_lowering: bool = False,
    ) -> dict | None:
        """Replace an existing protective stop with rollback so protection is preserved on failure.

        Used by the midday trailing-stop logic. PREFERRED PATH: when exactly one
        plain (non-bracket/OTO) protective stop covers the whole position, the stop's
        price is amended ATOMICALLY via Alpaca's replace endpoint — measured against
        the broker on rehearsal account <redacted-rehearsal-account> on 2026-09-30: the old order goes
        to REPLACED, a new id is issued, and exactly ONE open stop covers the symbol at
        every instant. A refused amend leaves the ORIGINAL order resting untouched.

        FALLBACK PATH: the older cancel + resubmit sequence, kept for the shapes that
        measurement did not cover (more than one resting stop, a bracket/OTO leg, a stop
        whose qty does not match the position, or an amend whose outcome is unknown).
        That sequence is not atomic — it is the origin of the naked-position window this
        method now avoids — so it still snapshots existing stops and best-effort restores
        them if the replacement submit fails.
        Returns {id, status, symbol} on successful replacement, else None.

        A short's protective stop is a BUY stop above the market, and
        "trailing" for a short means ratcheting it DOWN — the mirror of a
        long's stop-only-rises rule. Direction is read from whichever side
        ALREADY has a live stop (`_list_open_stop_orders_by_side` checks
        both with one fetch, since a long-only "sell" listing would make a
        short's BUY stop invisible); the position's own qty sign — read
        below to confirm the position exists, same as before shorts were
        possible — is the authoritative vote once there's something to cross-
        check it against, and the sole vote when there was no live stop yet
        to infer direction from.
        """
        if new_stop_price <= 0:
            logger.warning("replace_stop_loss ignored: non-positive new_stop_price=%s", new_stop_price)
            return None

        sell_orders, buy_orders = self._list_open_stop_orders_by_side(symbol)
        if sell_orders and buy_orders:
            # A single symbol can't legitimately be both long and short at
            # once, so live stops on both sides means stale orders survived
            # a direction flip. Reporting/acting on either would be a guess
            # — fail closed instead.
            logger.error(
                "replace_stop_loss: %s carries BOTH sell-stops and buy-stops "
                "— direction is ambiguous, refusing to trail",
                symbol,
            )
            return None
        # "sell" is also the default when NEITHER side has a live stop yet;
        # the position check below is what actually decides direction in
        # that case (see the qty_side cross-check).
        side = "buy" if buy_orders else "sell"

        live_orders = list(buy_orders or sell_orders)
        stop_specs: list[dict] = []
        for order in live_orders:
            spec = self._snapshot_stop_order(order)
            if spec is None:
                logger.warning(
                    "replace_stop_loss: cannot safely snapshot existing stop %s for %s; aborting replacement",
                    getattr(order, "id", "<unknown>"),
                    symbol,
                )
                return None
            stop_specs.append(spec)

        # Direction check: "trailing" means the stop moves toward less risk
        # — UP for a long, DOWN for a short — never the other way. If the
        # LLM hallucinates a stop on the wrong side (or the caller passes the
        # wrong value), accepting it would weaken existing protection. Ex-
        # dividend adjustments would intentionally lower a LONG's stop to
        # absorb tomorrow's mechanical dividend gap, and that is what the
        # allow_lowering=True opt-in is for.
        #
        # VERIFIED 2026-09-30: NO caller anywhere in src/ passes
        # allow_lowering=True — grep finds the name only in this file's own
        # signature and comments. The ex-dividend caller this comment
        # described does not exist, so every call today takes the
        # never-loosen branch below. Stated rather than removed because the
        # parameter is still reachable from tests and from a future caller,
        # but do not cite the ex-div caller as if it were live.
        if stop_specs and not allow_lowering:
            if side == "buy":
                tightest_existing = min(spec["stop_price"] for spec in stop_specs)
                if new_stop_price >= tightest_existing:
                    logger.warning(
                        "replace_stop_loss rejected for %s: new_stop $%.4f is "
                        "not below lowest existing buy-stop $%.4f — a "
                        "short's trailing stop must ratchet down only "
                        "(protection would weaken).",
                        symbol,
                        new_stop_price,
                        tightest_existing,
                    )
                    return None
            else:
                tightest_existing = max(spec["stop_price"] for spec in stop_specs)
                if new_stop_price <= tightest_existing:
                    logger.warning(
                        "replace_stop_loss rejected for %s: new_stop $%.4f is not "
                        "above highest existing stop $%.4f — trailing stops must "
                        "ratchet up only (protection would weaken).",
                        symbol,
                        new_stop_price,
                        tightest_existing,
                    )
                    return None

        positions = [p for p in self.get_positions() if p.symbol == symbol]
        if not positions or positions[0].qty == 0:
            logger.warning("replace_stop_loss: no open position in %s, nothing to protect", symbol)
            return None
        qty_side = "buy" if positions[0].qty < 0 else "sell"
        if stop_specs and qty_side != side:
            # Live stops on one side, but the held position is on the other
            # — the same stale-order shape as the both-sides check above,
            # just caught against the position instead of the order book.
            logger.error(
                "replace_stop_loss: %s has live %s-stop(s) but qty=%.4f says "
                "the opposite side — refusing to trail an ambiguous position",
                symbol,
                side,
                positions[0].qty,
            )
            return None
        side = qty_side  # authoritative now that a position confirms direction

        # PREFERRED: amend the resting stop's price in place. The sentinel
        # means "not attempted / outcome unknown" and drops through to the
        # legacy cancel+resubmit below; None means the broker REFUSED and the
        # original stop is still resting, so we must NOT cancel anything.
        # Re-read the position IMMEDIATELY before the amend: a fill landing
        # between a stale read and the amend could leave the stop covering more
        # than is held. A read failure drops to the fallback, which repairs it.
        try:
            fresh = [p for p in self.get_positions() if getattr(p, "symbol", None) == symbol]
            record_guarded_pass(self, "stop_place.replace_stop_loss.reread_position", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self,
                "stop_place.replace_stop_loss.reread_position",
                exc,
                log=logger,
                context={**{"symbol": symbol}, "effect": "fell to cancel+resubmit path"},
            )
            fresh = []
        amended = (
            _AMEND_NOT_ATTEMPTED
            if not fresh
            else self._amend_resting_stop_price(
                symbol=symbol,
                live_orders=live_orders,
                stop_specs=stop_specs,
                new_stop_price=new_stop_price,
                position_qty=abs(float(fresh[0].qty)),
            )
        )
        if amended is not _AMEND_NOT_ATTEMPTED:  # item 201: then fit the QUANTITIES to what is held
            return enforce_stop_quantity_invariant(self, symbol, amended, side=side, fresh=fresh)

        if deferred := defer_if_closed(self, symbol, stop_specs, new_stop_price, fresh):
            return deferred  # out of hours: cancel NOTHING (see stop_clock.py)
        # Genuinely un-amendable from here: the window is timed and RECORDED.
        window = UnprotectedWindow(
            symbol, fallback_reason(stop_specs, abs(float(fresh[0].qty)) if fresh else None), self._window_log
        )

        cancelled_specs: list[dict] = []
        for spec in stop_specs:
            try:
                self.client.cancel_order_by_id(spec["id"])
                cancelled_specs.append(spec)
                window.cancelled(spec["id"])
                record_guarded_pass(
                    self,
                    "stop_place.replace_stop_loss.cancel",
                    context={"symbol": symbol, "order": spec["id"]},
                )
            except Exception as exc:
                record_guarded_pass(
                    self,
                    "stop_place.replace_stop_loss.cancel",
                    exc,
                    log=logger,
                    context={**{"symbol": symbol, "order": spec["id"]}, "effect": "restore of cancelled stops follows"},
                )
                # Always restore whatever we already cancelled. The previous
                # "if no open stops remain" gate was wrong for partial
                # failures: with [A, B, C], if A and B cancel cleanly and C
                # fails, the broker now shows [C] — the gate sees something
                # open and skips restore, leaving A's and B's qty
                # unprotected. Restore is safe even when C is still live;
                # at worst we end up with slightly more stops than minimal,
                # but full original coverage is preserved.
                if cancelled_specs:
                    restored, _failed = self._restore_stop_orders(symbol, cancelled_specs, side=side)
                    logger.warning(
                        "replace_stop_loss: rolled back %d/%d already-cancelled "
                        "stop(s) for %s after partial cancel failure",
                        restored,
                        len(cancelled_specs),
                        symbol,
                    )
                    window.close("cancel_failed_restored" if restored else "cancel_failed_no_stop_confirmed")
                return None

        # Re-read position right before submit — in the sub-second window
        # between our cancel-stops and this submit, the position may have
        # been closed (liquidated by another path, or market-sold into a
        # fill). If it's gone, the new-stop submit would fail with a qty
        # mismatch AND our rollback would then re-attach a phantom stop to
        # a non-existent position. Bail cleanly in that case.
        fresh_positions = [p for p in self.get_positions() if p.symbol == symbol]
        if not fresh_positions or fresh_positions[0].qty == 0:
            logger.warning(
                "replace_stop_loss: %s was closed between cancel and submit; "
                "NOT restoring old stops (position no longer exists)",
                symbol,
            )
            window.close("position_closed")
            return None
        # Order qty is always the unsigned share count — the SIDE parameter
        # carries direction. `fresh_positions[0].qty` is negative for a
        # short; submitting that raw would hand Alpaca a negative qty.
        qty = abs(fresh_positions[0].qty)
        try:
            # Spec §11.1: a fractional position is re-protected by the HYBRID
            # PAIR, not by one fractional order that would be DAY-only and
            # gone by tomorrow morning. Whole-share positions submit exactly
            # one GTC order, unchanged.
            legs = self._submit_stop_legs(
                symbol=symbol,
                qty=qty,
                stop_price=new_stop_price,
                side=side,
            )
            order = legs[0]
            logger.info(
                "Trailing stop placed for %s: replaced %d old stop(s), new %s stop @ $%.2f across %d leg(s)",
                symbol,
                len(cancelled_specs),
                side,
                new_stop_price,
                len(legs),
            )
            window.close("replaced")
            record_guarded_pass(self, "stop_place.replace_stop_loss.submit_new", context={"symbol": symbol})
            return order
        except Exception as exc:
            record_guarded_pass(
                self,
                "stop_place.replace_stop_loss.submit_new",
                exc,
                log=logger,
                context={**{"symbol": symbol}, "effect": "rollback of cancelled stops follows"},
            )
            # The Alpaca QueryOrderStatus.OPEN filter INCLUDES transitional
            # statuses (pending_cancel / pending_replace), so the orders we
            # just cancelled can still appear in this list for ~1s after the
            # cancel call returns AND a *different* stop placed by another
            # path could itself be in pending_cancel. Three things must all
            # be true for "visible" to count as real protection:
            #   1. the order's id is NOT in cancelled_specs (PR #75)
            #   2. the order's status is in an active state, not pending_*
            #   3. the *sum* of active stop qtys covers the current position
            # Miss any of those and `cancelled_specs` must be restored.

            def _is_live_protection(order) -> bool:
                if str(getattr(order, "id", "")) in cancelled_ids:
                    return False
                status_attr = getattr(order, "status", None)
                status = str(getattr(status_attr, "value", status_attr) or "").lower()
                return status in PROTECTIVE_ORDER_ACTIVE_STATUSES

            def _stop_qty(order) -> float:
                try:
                    return float(getattr(order, "qty", 0) or 0)
                except (TypeError, ValueError):
                    return 0.0

            cancelled_ids = {
                real_broker_order_id(spec.get("id")) for spec in cancelled_specs if real_broker_order_id(spec.get("id"))
            }
            visible = self._list_open_protective_stop_orders(symbol, side=side)
            live_stops = [o for o in visible if _is_live_protection(o)]
            covered_qty = sum(_stop_qty(o) for o in live_stops)
            position_qty = qty  # captured pre-submit above; the position
            # cannot have grown between then and now (this
            # path doesn't BUY/SELL_SHORT to open), so this
            # is an upper bound for required coverage.
            if live_stops and covered_qty >= position_qty:
                logger.warning(
                    "replace_stop_loss: %d active stop(s) cover %.4f >= position %.4f for %s after submit failure; leaving stop state unchanged",
                    len(live_stops),
                    covered_qty,
                    position_qty,
                    symbol,
                )
                window.close("covered_by_other_stop")
                return None
            if live_stops:
                logger.warning(
                    "replace_stop_loss: %d active stop(s) cover only %.4f of %.4f shares for %s; restoring cancelled specs to close the gap",
                    len(live_stops),
                    covered_qty,
                    position_qty,
                    symbol,
                )
            restored, _failed = self._restore_stop_orders(symbol, cancelled_specs, side=side)
            if restored == 0:  # NOTHING is resting: re-protect now, or say so
                return reprotect_or_naked(self, symbol, qty, side, new_stop_price, cancelled_specs, window)
            window.close("restored")
            return None
