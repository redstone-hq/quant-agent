"""Order submission, replacement, cancellation and order reads, lifted verbatim from AlpacaBroker.

Third broker instalment. An `OrderDesk` is built from its collaborators alone
(keyword-only), so `submit_order`, `replace_entry_limit`, the replacement-chain
follow, the polling waits and the open/filled-order reads run without an
`AlpacaBroker` or a TradingPipeline. `AlpacaBroker` keeps same-named thin shims
that build one per call, so a test that swaps the client (or one of the
cluster's own methods) after construction still hits the swap.

The stream-backed waits (`_wait_for_order_status`, `_wait_for_order_status_via_stream`
and its `_locked` half) stay on the broker: they read and write the live
trade-updates hub, the lease slot and the warm-up record, which are broker
state. They reach this desk through its shims and this desk reaches them as
collaborators.

The module-level helpers the bodies read as globals moved with them;
`src.execution.broker` re-exports every one so existing importers and patch
targets still resolve.
"""

from __future__ import annotations

import logging

from src.execution.broker_parts import order_desk_reads as _reads
from src.execution.broker_parts.order_desk_effects import mark, ok
import math
import time

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, ReplaceOrderRequest

from src.execution.broker_parts.stop_amend import _quantize_price
from src.execution.broker_parts.stop_place import _alpaca_symbol, _internal_symbol
from src.execution.order_gates import (  # noqa: F401 (re-exports keep patch targets)
    _PLAIN_PRICE_LABELS,
    _outlier_refusal_detail,
    QTY_REJECTED,
    quantity_refusal_live,
)
from src.execution import (
    order_idempotency as _idem,
)  # session key read via the module so one patch target serves every path
from src.execution.order_idempotency import _client_order_id, _submit_entry_request_idempotent
from src.execution.sell_quantity import floor_sell_qty_at_submission
from src.execution.stop_records import STOP_USABLE, classify_stop_price

# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
logger = logging.getLogger("src.execution.broker")


# `_PLAIN_PRICE_LABELS` / `_outlier_refusal_detail` live in src/execution/order_gates.py (re-exported above).


from src.execution.broker_parts.submission_rejection import _is_terminal_submission_rejection  # noqa: F401 (re-export)

#: The one ceiling on waiting for an order to reach a terminal state. Every
#: cancel-then-act site waits on this (no per-site literal). A cancel not
#: confirmed inside it puts the sale off: nothing is sent into a stop still
#: holding the shares, and the deciding run re-checks it before it ends
#: (src/protection/put_off_sales.py; the trend tick defers it to its next tick).
ORDER_TERMINAL_CEILING_S = 15.0


class OrderDesk:
    """The broker's order-submission and order-read cluster, standalone.

    Every collaborator is a keyword-only constructor argument. The four
    optional ones are bodies this class already owns; pass one only to
    replace it (a test stand-in), never the broker's shim for it -- the
    broker's `_order_desk` factory guards that.
    """

    def __init__(
        self,
        *,
        client,
        kill_switch_active,
        kill_switch_path,
        wait_for_order_status,
        wait_for_order_status_via_stream,
        get_latest_price,
        order_terminal_states,
        order_replaceable_states,
        max_replacement_hops,
        wait_for_order_terminal=None,
        resolve_replacement_chain=None,
        wait_for_order_status_via_polling=None,
        list_open_entry_orders_checked=None,
        get_fractionability=None,
        get_account=None,
        max_position_pct=None,
    ):
        self.client = client
        # Live reads for the quantity gate (src/execution/order_gates.py);
        # `max_position_pct=None` = no notional check (read-only constructions).
        self._get_fractionability = get_fractionability
        self._get_account = get_account
        self._max_position_pct = max_position_pct
        self._kill_switch_active = kill_switch_active
        self._kill_switch_path = kill_switch_path
        self._wait_for_order_status = wait_for_order_status
        self._wait_for_order_status_via_stream = wait_for_order_status_via_stream
        self.get_latest_price = get_latest_price
        self._ORDER_TERMINAL_STATES = order_terminal_states
        self._ORDER_REPLACEABLE_STATES = order_replaceable_states
        self._MAX_REPLACEMENT_HOPS = max_replacement_hops
        if wait_for_order_terminal is not None:
            self.wait_for_order_terminal = wait_for_order_terminal
        if resolve_replacement_chain is not None:
            self.resolve_replacement_chain = resolve_replacement_chain
        if wait_for_order_status_via_polling is not None:
            self._wait_for_order_status_via_polling = wait_for_order_status_via_polling
        if list_open_entry_orders_checked is not None:
            self.list_open_entry_orders_checked = list_open_entry_orders_checked

    def cancel_open_orders(self) -> int:
        """Cancel all open orders. Returns count of cancelled orders."""
        try:
            cancelled = self.client.cancel_orders()
            count = len(cancelled) if cancelled else 0
            if count:
                logger.info("Cancelled %d open order(s)", count)
            ok(self, "cancel_all_open_orders")
            return count
        except Exception as exc:
            mark(self, "cancel_all_open_orders", exc)
            return 0

    def cancel_open_entry_orders(self, symbol: str | None = None) -> int:
        """Cancel open entry orders on EITHER side — BUY-to-open-long and
        SELL-to-open-short — while preserving protective stop legs on
        either side.

        `symbol` scopes the cancel to one name — used by the full-exit
        SELL/COVER discipline (audit round 2: a fully-exited symbol could
        still carry the same day's resting DAY entry BUY, which would
        silently re-open the position — or, in the emergency-liquidation
        case, re-buy into the crash the breaker just sold). Stage 3 (shorts)
        gap fix: an EMERGENCY_COVER used to leave a resting SELL-to-open
        entry order untouched, which could fill and re-open the exact short
        exposure the emergency close just cleared — the short-side mirror
        of the BUY case above.

        Pre-shorts this only ever needed to filter by SIDE: entries were
        always BUY and protective legs were always SELL, so a bare
        `side == "buy"` filter could never touch a stop. Now that BUY-side
        protective covers (a short's stop) and SELL-side entries (a
        short-open) both exist, side alone stopped being a safe proxy for
        "is this an entry" — the discriminator has to be ORDER TYPE. Any
        *stop* order (stop / stop_limit / trailing_stop — the same
        `"stop" in order_type` test `_list_open_sell_stop_orders` /
        `_list_open_stop_orders_by_side` already use to find a protective
        leg) is left alone regardless of side; every other BUY or SELL
        order is a plain entry and gets cancelled.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            req_kwargs = dict(status=QueryOrderStatus.OPEN, nested=True)
            if symbol:
                req_kwargs["symbols"] = [_alpaca_symbol(symbol)]
            orders = self.client.get_orders(filter=GetOrdersRequest(**req_kwargs))
            from src.execution.broker_parts.cancel_confirm import CancelledEntries, now_iso

            legs: list[dict] = []
            for order in orders or []:
                order_id = getattr(order, "id", None)
                order_side = str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).lower()
                order_type = str(
                    getattr(getattr(order, "order_type", None), "value", getattr(order, "order_type", ""))
                ).lower()
                if order_side not in ("buy", "sell") or not order_id:
                    continue
                if "stop" in order_type:
                    continue  # protective leg on either side — preserve it
                sent_at = now_iso()
                self.client.cancel_order_by_id(order_id)
                legs.append({"id": str(order_id), "side": order_side, "kind": "order", "cancel_sent_at": sent_at})
            if legs:
                logger.info("Cancelled %d open entry order(s)", len(legs))
            ok(self, "cancel_open_entry_orders", symbol=symbol)
            # Still the count every caller reads; `.legs` lets a sale wait on them.
            return CancelledEntries(legs)
        except Exception as exc:
            mark(self, "cancel_open_entry_orders", exc, symbol=symbol)
            return 0

    def list_open_entry_order_ids(
        self,
        symbol: str,
        *,
        side: str | None = None,
    ) -> list[str]:
        """Ids of working non-stop BUY/SELL orders for `symbol` (see order_desk_reads)."""
        return _reads.list_open_entry_order_ids(self, symbol, side=side)

    def list_open_entry_orders_checked(
        self,
        symbol: str,
        *,
        side: str | None = None,
    ) -> tuple[bool, list[str]]:
        """`(ok, ids)` for working non-stop orders (see order_desk_reads)."""
        return _reads.list_open_entry_orders_checked(self, symbol, side=side)

    def open_buy_notional(self) -> float | None:
        """Dollar notional of all OPEN BUY orders, or None on failure (see order_desk_reads)."""
        return _reads.open_buy_notional(self)

    def list_recent_orders(
        self,
        symbol: str,
        side: str,
        after,
    ) -> list[dict] | None:
        """All of `symbol`'s orders on `side` since `after` (see order_desk_reads)."""
        return _reads.list_recent_orders(self, symbol, side, after)

    def list_filled_sell_orders(self, symbol: str, after) -> list[dict] | None:
        """Every FILLED sell-side order for `symbol` filled since `after` (see order_desk_reads)."""
        return _reads.list_filled_sell_orders(self, symbol, after)

    def get_order_fill_info(self, order_id: str) -> dict | None:
        """Return {status, filled_qty, filled_avg_price} for an order, or None (see order_desk_reads)."""
        return _reads.get_order_fill_info(self, order_id)

    def wait_for_order_at_exchange(
        self,
        order_id: str,
        timeout_seconds: float = 5.0,
        poll_interval: float = 1.0,
        use_stream: bool = True,
    ) -> str | None:
        """Wait until the order has LEFT the not-yet-at-exchange states.

        Returns the last known status (lowercased) or None when nothing could
        be read. The status returned may be:
          * terminal (`_ORDER_TERMINAL_STATES`) — it filled/cancelled/etc.
            while we waited; the caller has nothing to reprice;
          * `new` / `partially_filled` — at the venue, working;
          * still `accepted` / `pending_new` — the window ran out before the
            venue acknowledged it. The caller MUST NOT attempt a replace on
            this outcome: the broker would reject it anyway.

        Same shape as `wait_for_order_terminal`: the real-time `trade_updates`
        websocket first (the stream emits an update the instant the status
        changes, so an ordinary open-time acknowledgement costs milliseconds,
        not the whole window), then one REST read to report the last known
        status if nothing arrived, and the full REST polling loop only when
        the stream could not be used at all. No new polling machinery.
        """
        stop_states = self._ORDER_TERMINAL_STATES | self._ORDER_REPLACEABLE_STATES | frozenset({"partially_filled"})
        return self._wait_for_order_status(
            order_id,
            timeout_seconds,
            poll_interval,
            stop_states=stop_states,
            use_stream=use_stream,
            unavailable_log=(
                "order-status stream unavailable for %s — falling back to REST polling for exchange acknowledgement"
            ),
        )

    def wait_for_order_terminal(
        self,
        order_id: str,
        timeout_seconds: float = ORDER_TERMINAL_CEILING_S,
        poll_interval: float = 1.0,
        use_stream: bool = True,
    ) -> str | None:
        """Wait for an order to reach a terminal state and return its last known status.

        2026-09-10: watches Alpaca's real-time `trade_updates` websocket
        first — see `_wait_for_order_terminal_via_stream` — instead of
        polling `get_order_by_id` on a fixed interval. A fill, cancel or
        reject is detected the instant Alpaca reports it, so the timeout no
        longer trades detection speed against giving a slow-to-fill order
        enough room; it is now purely a ceiling. This was the actual
        question the owner asked when the fixed-timeout number was under
        discussion: not "what should the number be" but "why are we
        guessing at all when Alpaca tells you the instant it happens."

        Three distinct outcomes from the stream attempt, each handled
        differently on purpose:
          - a terminal event arrived for THIS order -> return it immediately,
            no REST call needed at all.
          - the stream connected fine but nothing terminal arrived before
            `timeout_seconds` (a genuinely still-open order) -> one single
            REST check, to preserve this function's existing contract of
            returning the LAST KNOWN status (which may be non-terminal,
            e.g. "new") rather than None.
          - the stream itself could not be used at all (library missing,
            auth/network failure) -> fall back to the full REST polling
            loop exactly as this function worked before this change, so a
            websocket outage degrades to the old behaviour rather than to
            no behaviour.

        `use_stream=False` skips straight to REST polling — an explicit
        escape hatch for a misbehaving stream in production, and what unit
        tests use to exercise the polling path deterministically without
        real network I/O.
        """
        return self._wait_for_order_status(
            order_id,
            timeout_seconds,
            poll_interval,
            stop_states=self._ORDER_TERMINAL_STATES,
            use_stream=use_stream,
            unavailable_log=("order-fill stream unavailable for %s — falling back to REST polling"),
        )

    def _get_order_status_once(self, order_id: str) -> str | None:
        """Single REST read of an order's current status, lowercased. None
        on any failure — callers already treat None as "no information"."""
        try:
            order = self.client.get_order_by_id(order_id)
            status = str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
            ok(self, "read_order_status", order=order_id)
            return status or None
        except Exception as exc:
            mark(self, "read_order_status", exc, order=order_id)
            return None

    def _wait_for_order_terminal_via_stream(
        self,
        order_id: str,
        timeout_seconds: float,
        poll_interval: float = 1.0,
    ) -> tuple[str | None, bool]:
        """Terminal-state wait on the websocket. See
        `_wait_for_order_status_via_stream` — this is that method with the
        stop set fixed to `_ORDER_TERMINAL_STATES`, kept under its original
        name because the polling/stream tests and `wait_for_order_terminal`
        address it directly."""
        return self._wait_for_order_status_via_stream(
            order_id,
            timeout_seconds,
            stop_states=self._ORDER_TERMINAL_STATES,
            poll_interval=poll_interval,
        )

    def _wait_for_order_terminal_via_polling(
        self,
        order_id: str,
        timeout_seconds: float,
        poll_interval: float,
    ) -> str | None:
        """The original REST-polling implementation, kept as the fallback
        path for when the real-time stream cannot be used at all."""
        return self._wait_for_order_status_via_polling(
            order_id,
            timeout_seconds,
            poll_interval,
            stop_states=self._ORDER_TERMINAL_STATES,
        )

    def _wait_for_order_status_via_polling(
        self,
        order_id: str,
        timeout_seconds: float,
        poll_interval: float,
        *,
        stop_states: frozenset,
    ) -> str | None:
        """REST polling until the status is in `stop_states`, or the window
        ends. Returns the last known status (possibly one outside the set)."""
        deadline = time.monotonic() + timeout_seconds
        last_status = None

        while time.monotonic() < deadline:
            try:
                order = self.client.get_order_by_id(order_id)
                status = str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
                ok(self, "poll_order_status", order=order_id)
            except Exception as exc:
                mark(self, "poll_order_status", exc, order=order_id)
                # The read failed: the status is UNKNOWN now, whatever an
                # earlier read said. Returning the stale one made a REST
                # hiccup look like a live order (cancel_unconfirmed) instead
                # of a failed read (cancel_read_failed).
                return None

            last_status = status or last_status
            if status in stop_states:
                return status
            time.sleep(poll_interval)

        return last_status

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        limit_price: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        reference_price: float | None = None,
        atr: float | None = None,
    ) -> dict:
        """Submit an entry or exit order.

        `atr` is OPTIONAL and is used for the OWNER-FACING WORDING ONLY —
        never for a gate, a threshold or a size. It is the desk's own
        already-measured ATR(14) for this symbol, so that a fat-finger
        refusal can state the stock's normal daily range beside the
        deviation instead of a bare percentage the owner cannot judge. No
        code path branches on it.
        """
        if self._kill_switch_active():
            # Guard 1: deliberately unconditional. This is the ONE check in
            # the order-submission path that does NOT exempt a SELL/COVER —
            # see RiskConfig.kill_switch_path.
            logger.error(
                "KILL SWITCH ACTIVE (%s exists): refusing %s %s %s. Every "
                "order — entry or exit — is halted until the file is "
                "removed.",
                self._kill_switch_path,
                side.upper(),
                qty,
                symbol,
            )
            return {
                "id": None,
                "status": "kill_switch_halted",
                "symbol": _internal_symbol(symbol),
                "detail": "the trading kill switch is active — every order is halted until the file is removed",
            }
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        internal_symbol = _internal_symbol(symbol)
        alpaca_symbol = _alpaca_symbol(internal_symbol)

        # Captured BEFORE `_quantize_price`, which maps NaN/Inf to None (see
        # its docstring). A non-finite STOP would therefore vanish silently
        # and `use_stop` below would go False — submitting the entry with no
        # protective stop at all, which is the worst possible outcome of a
        # bad number. The stop checks below refuse it instead.
        stop_was_supplied = stop_loss_price is not None
        stop_was_finite = stop_loss_price is not None and math.isfinite(stop_loss_price)

        # Normalize to Alpaca's tick size — sub-penny values from quote-midpoint
        # math or LLM outputs get Alpaca error 42210000 and a rejected order.
        limit_price = _quantize_price(limit_price)
        stop_loss_price = _quantize_price(stop_loss_price)
        take_profit_price = _quantize_price(take_profit_price)

        # ------------------------------------------------------------------
        # Fat-finger / outlier price guardrail — ENTRY/LIMIT PRICE ONLY.
        # ------------------------------------------------------------------
        # If the caller passed a reference_price (today's quote) and the
        # price we would TRANSACT AT is more than `OUTLIER_MAX_DEVIATION`
        # away from it, the number is almost certainly garbage — a
        # data-source glitch ($0.01 quote on a $300 stock, or an LLM
        # hallucinated entry). Submitting would turn qty sizing into
        # nonsense (5% alloc / $0.01 = 500x expected shares) and blow
        # through every risk check. Refuse the order.
        #
        # WHY THIS NO LONGER APPLIES TO THE STOP (2026-09-17, FLNC).
        # `OUTLIER_MAX_DEVIATION` is an unsourced 20% inherited from the
        # upstream project (ca4c51d9, yebof, 2026-04-18) and it was being
        # applied to the stop as well. A flat percentage band is the wrong
        # shape of test for a stop, for three reasons:
        #
        #   1. It is measured in the wrong units. A stop's distance from
        #      entry is a volatility distance, not a fixed fraction of
        #      price. FLNC's own measured ATR(14) on 2026-09-17 was $0.75
        #      against a $7.785 price — 9.6% — so a flat 20% band refuses
        #      any stop wider than ~2.1x that stock's ordinary daily range,
        #      while on a $500 name with a 1% ATR the same band permits 20
        #      ATRs. It bans nothing on a quiet stock and bans normal
        #      structure on a volatile one.
        #   2. It only catches the SAFE direction. A too-WIDE stop, under
        #      risk-based sizing, makes the position SMALLER (qty = risk
        #      budget / stop distance). The dangerous error is a too-TIGHT
        #      stop, which inflates size — and a deviation band never
        #      catches a tight stop at all, because a tight stop sits close
        #      to the reference by definition.
        #   3. It refused a trade the desk had already paid for. On
        #      2026-09-17 17:05:30 (production log) a risk-manager-approved
        #      SELL_SHORT FLNC — entry $7.785, stop $9.66, correct side,
        #      R/R 1.51:1, the constructor's own reading recorded as "stop
        #      width 2.50 x ATR, touch probability 20.7%" — was rejected
        #      here AFTER every analyst, portfolio-manager and risk-manager
        #      call had been billed.
        #
        # No replacement percentage is invented for the stop: there is no
        # published source for one, and per the desk's no-arbitrary-numbers
        # rule a fitted number would be no better than this one. What the
        # stop gets instead, below, are the two checks that need no number
        # at all — finiteness and side. The width question already belongs
        # to `src/portfolio_constructor.py::_widen_stop_past_noise`, which
        # measures it in the instrument's own ATR and honours a
        # level-backed stop however tight (spec §12.1). Nothing here
        # second-guesses that in percent.
        OUTLIER_MAX_DEVIATION = 0.20
        if reference_price and reference_price > 0:
            label, candidate = "limit_price", limit_price
            if candidate is not None and candidate > 0:
                deviation = abs(candidate - reference_price) / reference_price
                if deviation > OUTLIER_MAX_DEVIATION:
                    logger.error(
                        "Fat-finger guard: %s %s — %s=$%.4f deviates %.1f%% from reference $%.2f. "
                        "Order REJECTED (likely data glitch or LLM hallucination).",
                        side.upper(),
                        symbol,
                        label,
                        candidate,
                        deviation * 100,
                        reference_price,
                    )
                    return {
                        "id": None,
                        "status": "rejected_outlier",
                        "symbol": internal_symbol,
                        # Surfaced downstream (src/pipeline_stages.py's
                        # execution-skip record, then the Telegram alert) so
                        # the operator reads the REAL blocker — QAMC's own
                        # price-sanity check, before this order ever reached
                        # the broker — instead of a generic "broker
                        # rejected", which used to read as if the broker had
                        # refused a perfectly sane order. Plain words, not
                        # the internal field name/precision the log line
                        # above carries (owner-facing text, not a log):
                        # "limit price $9.66 is 24% from price $7.79", never
                        # "limit_price=$9.6600 deviates 24.1% from
                        # reference $7.79 (likely ... hallucinated)".
                        #
                        # The bare percentage alone is not judgeable: 24%
                        # is an outrage on a utility and an ordinary two
                        # days on a $7 name. So the message carries the
                        # stock's OWN measured daily range beside it —
                        # `atr` is the desk's already-computed ATR(14) for
                        # this symbol, passed down from the entry stage; it
                        # is never fetched or estimated here, and when the
                        # caller has none the sentence simply omits it
                        # rather than inventing a range.
                        "detail": _outlier_refusal_detail(
                            label,
                            candidate,
                            reference_price,
                            symbol=internal_symbol,
                            atr=atr,
                        ),
                    }

        # ------------------------------------------------------------------
        # Stop-price sanity — the checks appropriate to a STOP.
        # ------------------------------------------------------------------
        # Only on the two ENTRY sides, which are the only sides that ever
        # pass a stop (see `use_stop` below): 'sell' is this codebase's
        # word for reducing/closing a long and never supplies one.
        if side.lower() in ("buy", "sell_short") and stop_was_supplied:
            # docs/WORK.md item 88. A stop WAS requested, so from here the
            # only two legal outcomes are "a usable price" and "refused".
            # `0.0` used to be a third: this codebase's sentinel for "no
            # stop", which made a degenerate ATR output or a miscomputed
            # level REMOVE protection instead of refusing the trade. The
            # sentinel survives only where nothing was supplied at all
            # (`stop_loss_price=None`, the cash-sweep park's deliberate
            # stopless buy) — that distinction is the whole fix. NaN/Inf
            # was the same hole in a different disguise, closed for this
            # lane by PR #455; zero is closed here.
            stop_state, _stop_px = classify_stop_price(stop_loss_price if stop_was_finite else float("nan"))
            if stop_state != STOP_USABLE:
                logger.error(
                    "Stop sanity: %s %s — a stop was requested and its value "
                    "(%r) cannot be a stop price. Order REJECTED rather than "
                    "submitted naked. A garbage stop is not an absent stop: "
                    "only a caller that passes no stop at all (None) is "
                    "allowed a stopless order.",
                    side.upper(),
                    symbol,
                    stop_loss_price,
                )
                return {
                    "id": None,
                    "status": "rejected_bad_stop",
                    "symbol": internal_symbol,
                    "detail": "the stop price is not a usable number",
                }
            # Side check. A stop on the wrong side of the price we are
            # entering at protects nothing and would fire instantly — the
            # same refusal the constructor makes against its own entry
            # (`STOP_REFUSAL_WRONG_SIDE`), repeated here because this is
            # the last deterministic gate before the broker and Invariant 2
            # requires the final authority to fail closed. Measured against
            # the price this order actually transacts at (the limit),
            # falling back to the quote when it is a market order. No
            # number is chosen: it is an inequality.
            entry_ref = (
                limit_price
                if (limit_price and limit_price > 0)
                else (reference_price if (reference_price and reference_price > 0) else None)
            )
            if (
                entry_ref is not None
                and stop_loss_price is not None
                # Unreachable unless usable now — the refusal above returns
                # on anything else. Kept as a precondition, not a sentinel.
                and stop_loss_price > 0
            ):
                is_short_entry = side.lower() == "sell_short"
                wrong_side = stop_loss_price <= entry_ref if is_short_entry else stop_loss_price >= entry_ref
                if wrong_side:
                    logger.error(
                        "Stop sanity: %s %s — stop=$%.4f is on the wrong "
                        "side of the $%.4f entry, so it protects nothing. "
                        "Order REJECTED.",
                        side.upper(),
                        symbol,
                        stop_loss_price,
                        entry_ref,
                    )
                    return {
                        "id": None,
                        "status": "rejected_bad_stop",
                        "symbol": internal_symbol,
                        "detail": (
                            f"stop ${stop_loss_price:,.2f} is on the wrong "
                            f"side of the ${entry_ref:,.2f} entry, so it "
                            f"would protect nothing"
                        ),
                    }

        # The ONE rounding point for every SELL (src/execution/sell_quantity.py):
        # callers compute quantities raw, so floor onto the gate's own decimal
        # grid here, before the gate, which stays exactly as strict.
        qty, floor_refusal = floor_sell_qty_at_submission(qty, side=side, symbol=symbol)
        if floor_refusal is not None:
            return {"id": None, "status": QTY_REJECTED, "symbol": internal_symbol, "detail": floor_refusal}

        # Quantity gate (src/execution/order_gates.py): the stop PRICE was
        # refused here, the quantity never was (2026-10-01 audit).
        qty_refusal = quantity_refusal_live(
            internal_symbol,
            alpaca_symbol,
            qty,
            side,
            price=limit_price if (limit_price and limit_price > 0) else reference_price,
            client=self.client,
            get_fractionability=self._get_fractionability,
            get_account=self._get_account,
            max_position_pct=self._max_position_pct,
        )
        if qty_refusal is not None:
            logger.error("Quantity gate: %s %s %s — %s. Order REJECTED.", side.upper(), qty, symbol, qty_refusal)
            return {"id": None, "status": QTY_REJECTED, "symbol": internal_symbol, "detail": qty_refusal}

        # Protective stop for a BUY is placed as a SEPARATE GTC stop-MARKET
        # (guaranteed exit; stop-limit only on the unsupported-combo fallback)
        # AFTER the entry fills — NOT as an OTO leg.
        #
        # WHY (2026-07-16 audit, CRITICAL): `StopLossRequest` carries no
        # time_in_force of its own, so an OTO child leg inherits the PARENT's
        # TIF. The parent must be DAY (an unfilled entry limit must die at the
        # close, never fill into a stale thesis the next morning) — which
        # silently made every BUY-attached stop a DAY order too. Alpaca expired
        # it at 16:00 ET the same session, so any position bought in the
        # morning and not later given a midday/close TRAIL_STOP (which uses the
        # GTC `_submit_stop_limit_order` path) sat NAKED overnight — precisely
        # when gap risk is the reason the stop exists. Confirmed in production:
        # VST bought 2026-06-26 09:47 ET with SL=$158.75; the same evening's
        # coverage reconcile logged `VST held=31.0000 but only 0.0000 covered`;
        # it was ultimately exited at $152.77 for ~$185 more loss than the stop
        # would have capped. This also contradicted the close-session prompt,
        # which tells the reviewer to hold overnight *because* the broker stop
        # is standing watch.
        #
        # Placing the stop post-fill also fixes a second latent bug: the OTO
        # leg was sized to the REQUESTED qty, so a partial entry fill left a
        # stop covering more shares than we own. `_place_entry_protection`
        # keys the stop to the ACTUAL filled qty.
        # Stage 3 (shorts, D7): a SHORT entry (side='sell_short') owes a
        # protective stop exactly the way a BUY entry does — it just gets
        # placed on the opposite side by `place_entry_protection`. 'sell'
        # deliberately stays OUT of this: that's this codebase's convention
        # for REDUCING/closing a long (`_submit_protected_sell`'s default),
        # which never passes `stop_loss_price` and so never reaches here
        # regardless — 'sell_short' is the only sell-side string an ENTRY
        # ever uses.
        use_stop = stop_loss_price is not None and stop_loss_price > 0 and side.lower() in ("buy", "sell_short")

        # Idempotency key (src/execution/order_idempotency.py): a retried
        # submission reuses it and the broker refuses the duplicate.
        client_order_id = _client_order_id(
            purpose="ENT",
            symbol=alpaca_symbol,
            side=side,
            session_date=_idem._session_date_key(),
            qty=qty,
            price=limit_price,
        )
        if limit_price is not None:
            request = LimitOrderRequest(
                symbol=alpaca_symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                client_order_id=client_order_id,
            )
        else:
            request = MarketOrderRequest(
                symbol=alpaca_symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )

        try:
            order = _submit_entry_request_idempotent(
                self.client,
                request,
                client_order_id=client_order_id,
                side=side,
                qty=qty,
                symbol=symbol,
            )
        except Exception as exc:  # noqa: BLE001
            # Owner ruling 2026-09-30 (board item 183): the constructor's
            # flat `min_trade_weight_delta` churn floor is gone, so a
            # genuinely tiny, desk-requested nudge now reaches THIS call for
            # the first time — and the broker has its own real, documented
            # floors this desk never chose: a $1 minimum notional on a BUY
            # entry (https://alpaca.markets/support/can-we-submit-orders-
            # smaller-than-1-usd-in-notional-value), Alpaca's tick size
            # (already normalized above by `_quantize_price`), and
            # fractional support per asset (already read live by
            # `get_fractionability`). WHICH status codes actually mean
            # "this submission was refused" is answered by
            # `_is_terminal_submission_rejection` — read off Alpaca's own
            # create-order documentation (422 only). #786 first reused the
            # stop-placement retry classifier's 400/404/422 set here without
            # re-checking that it means the same thing on the SUBMISSION
            # endpoint; it does not, and the narrowed test is derived from
            # two fetched Alpaca sources cited in that function. Any other
            # failure still propagates, exactly as before this change, so
            # the caller's orphan-sweep recovery (src/pipeline_stages.py)
            # still runs for the ambiguous case where the broker may or may
            # not have the order.
            if _is_terminal_submission_rejection(exc):
                mark(self, "submit_order_rejected", exc, side=side)
                logger.warning(
                    "Order rejected by broker for %s %s %s: %s",
                    side,
                    qty,
                    symbol,
                    exc,
                )
                return {
                    "id": None,
                    "status": "rejected_by_broker",
                    "symbol": internal_symbol,
                    "detail": str(exc),
                }
            raise
        bracket_info = f" [SL=${stop_loss_price} to be placed on fill]" if use_stop else ""
        logger.info(
            "Order submitted: %s %s %s @ %s%s — status: %s",
            side,
            qty,
            symbol,
            limit_price or "market",
            bracket_info,
            str(getattr(order.status, "value", order.status)),
        )
        return {
            "id": str(order.id),
            # alpaca-py OrderStatus is `(str, Enum)`. Plain `str(enum)`
            # returns 'OrderStatus.REJECTED' (the repr), not 'rejected'
            # (the value). `_order_accepted`'s rejection filter
            # lowercases and checks for the *value* form, so without
            # the .value unwrap a real broker rejection would slip past
            # as "accepted" and proceed through the pipeline (audit
            # 2026-05-27).
            "status": str(getattr(order.status, "value", order.status)),
            "symbol": _internal_symbol(order.symbol),
            # Echo back the parameters so downstream consumers (notifier,
            # audit log, finalize) can render orders without having to
            # join against the trades table for what was JUST submitted.
            # Pre-2026-05-12 this dict was {id, status, symbol} only and
            # the notifier could only show "BUY NVDA qty=?" — now it can
            # show "BUY NVDA qty=27 @$238.63 SL=$230".
            "side": side.lower(),
            "qty": qty,
            "limit_price": limit_price,
            "stop_loss_price": stop_loss_price if use_stop else None,
            # Signals the caller that this entry still OWES a protective stop
            # (see _place_entry_protection). Absent/None => nothing to place.
            "pending_stop_price": stop_loss_price if use_stop else None,
        }

    def cancel_entry_order(self, order_id: str) -> bool:
        """Cancel one order by id. True when the broker accepted the cancel.

        A named seam rather than a raw `client.cancel_order_by_id` call so the
        re-peg race path — "the superseded order filled, kill the replacement
        before it buys the same idea again" — is explicit, mockable, and
        cannot be confused with `cancel_open_entry_orders`, which cancels
        every working entry for a symbol.
        """
        try:
            self.client.cancel_order_by_id(order_id)
            ok(self, "cancel_entry_order", order=order_id)
            return True
        except Exception as exc:  # noqa: BLE001
            mark(self, "cancel_entry_order", exc, order=order_id)
            return False

    def replace_order_qty(self, order_id: str, qty: int) -> tuple[bool, str]:
        """Amend one resting order's quantity in place; `(ok, detail)`.

        The freeze sweep's shrink: a closing order larger than the position is
        cut to the held size so it can never flip side, without cancelling
        the protection it is. Whole shares only (ReplaceOrderRequest types
        `qty` as int); callers pass the held size, never a larger one.
        """
        try:
            self.client.replace_order_by_id(order_id, ReplaceOrderRequest(qty=int(qty)))
            ok(self, "replace_order_qty", order=order_id)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            mark(self, "replace_order_qty", exc, order=order_id)
            return False, f"{type(exc).__name__}: {exc}"

    def list_open_orders_checked(self) -> tuple[bool, list]:
        """`(ok, orders)` for EVERY working order, flat (see order_desk_reads)."""
        return _reads.list_open_orders_checked(self)

    def resolve_replacement_chain(self, order_id: str) -> str | None:
        """Follow Alpaca's `replaced_by` links to the order that is live now.

        A replaced order keeps its own identity forever: status 'replaced',
        `filled_qty` frozen at whatever it filled before the swap, and
        `replaced_by` pointing at its successor. This walks that chain and
        returns the id at the end of it — which is the only id worth polling
        for a fill.

        Returns the input id unchanged when the order was never replaced.
        Returns None when the broker read FAILED, which callers must treat as
        "unknown, retry later" and never as "no replacement" — repointing a
        trades row on a failed read would be inventing a fact.
        """
        current = str(order_id)
        for _ in range(self._MAX_REPLACEMENT_HOPS):
            try:
                order = self.client.get_order_by_id(current)
                ok(self, "resolve_replacement_chain", order=current)
            except Exception as exc:  # noqa: BLE001
                mark(self, "resolve_replacement_chain", exc, order=current)
                return None
            status = str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
            successor = getattr(order, "replaced_by", None)
            successor = str(successor) if successor else ""
            if status != "replaced" or not successor or successor == current:
                return current
            current = successor
        logger.error(
            "resolve_replacement_chain: %s exceeded %d hops — refusing to keep walking",
            order_id,
            self._MAX_REPLACEMENT_HOPS,
        )
        return None

    def replace_entry_limit(
        self,
        order_id: str,
        new_limit_price: float,
        *,
        qty: float | None = None,
    ) -> dict:
        """PATCH a working entry limit to a new price. Returns the NEW order id.

        This is the only place in the codebase that calls Alpaca's replace
        endpoint, and the reason it is wrapped rather than inlined is the
        footgun: **the replacement is a different order**. The response
        carries a new id; the id passed in is dead from that moment.

        `qty` is passed through explicitly rather than left to the broker's
        default. The caller only ever re-pegs an order that has filled ZERO
        shares, so "remaining" and "original" are the same number here — but
        stating it removes any dependence on how the endpoint interprets an
        omitted qty against a partially filled order, which is exactly the
        ambiguity that turns a re-peg into an over-buy.

        Never raises. Failure shapes, all with `id=None`:
          - 'replace_invalid_price' — nothing was sent to the broker.
          - 'replace_rejected'      — the broker refused. The overwhelmingly
            likely cause is that the order reached a terminal state (it
            FILLED) between the caller's check and this call. The caller must
            re-read the ORIGINAL id, which is still authoritative in that
            case, and must not retry blindly.
          - 'kill_switch_halted'    — Guard 1: ops has halted the desk. The
            ORIGINAL id remains authoritative and simply does not chase.
        """
        if self._kill_switch_active():
            logger.error(
                "KILL SWITCH ACTIVE (%s exists): refusing to re-peg entry order %s to $%.4f.",
                self._kill_switch_path,
                order_id,
                new_limit_price,
            )
            return {"id": None, "status": "kill_switch_halted"}
        price = _quantize_price(new_limit_price)
        if price is None or price <= 0:
            logger.warning(
                "replace_entry_limit refused for %s: non-quotable price %r",
                order_id,
                new_limit_price,
            )
            return {"id": None, "status": "replace_invalid_price"}

        # Spec §11.1: a FRACTIONAL entry cannot be re-pegged. Alpaca's
        # ReplaceOrderRequest types `qty` as an int, and the two ways out of
        # that are both worse than refusing: truncating 1.5625 to 1 silently
        # SHRINKS a position the risk math already sized, and omitting qty
        # reintroduces exactly the "how does the endpoint read an omitted qty"
        # ambiguity this wrapper documents itself as removing. Refusing means
        # the original order stays authoritative and simply does not chase —
        # the caller's existing `id=None` path, and the safe direction.
        try:
            is_fractional = qty is not None and not float(qty).is_integer()
        except (TypeError, ValueError):
            is_fractional = False
        if is_fractional:
            logger.info(
                "replace_entry_limit refused for %s: fractional qty %s cannot "
                "be re-pegged — the original order remains authoritative",
                order_id,
                qty,
            )
            return {"id": None, "status": "replace_unsupported_fractional_qty"}

        kwargs: dict = {"limit_price": price}
        if qty is not None:
            try:
                int_qty = int(qty)
            except (TypeError, ValueError):
                int_qty = 0
            if int_qty > 0:
                kwargs["qty"] = int_qty

        try:
            order = self.client.replace_order_by_id(
                order_id,
                ReplaceOrderRequest(**kwargs),
            )
            ok(self, "replace_entry_limit", order=order_id)
        except Exception as exc:  # noqa: BLE001
            mark(self, "replace_entry_limit", exc, order=order_id)
            return {"id": None, "status": "replace_rejected", "detail": str(exc)}

        new_id = str(getattr(order, "id", "") or "")
        if not new_id:
            logger.error(
                "replace_entry_limit: broker accepted the replacement of %s "
                "but returned no order id — treating as rejected so the "
                "caller keeps polling the original",
                order_id,
            )
            return {"id": None, "status": "replace_rejected"}
        status = str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
        logger.info(
            "replace_entry_limit: %s → %s @ $%.4f (status %s)",
            order_id,
            new_id,
            price,
            status or "unknown",
        )
        return {
            "id": new_id,
            "status": status or "accepted",
            "limit_price": price,
            "replaces": str(order_id),
        }

    def await_replacement_confirmed(
        self,
        old_order_id: str,
        new_order_id: str,
        timeout_seconds: float = 5.0,
    ) -> bool:
        """Block until Alpaca has FINISHED replacing `old_order_id`.

        2026-09-12: the entry chase is now a SINGLE decisive reprice (see
        `_repeg_entry_order`), so there is no second replace to sequence and
        this is no longer on the entry hot path. Kept because it is the
        correct primitive if a second replace is ever needed again, and the
        reasoning below is the reason a ladder was retired: every extra
        replace is another `pending_replace` window to get stuck in.

        Why this exists as a hard gate rather than an optimistic assumption:
        Alpaca refuses to replace an order whose status is `accepted`,
        `pending_new`, `pending_cancel` **or `pending_replace`** (its own
        Replace-Order reference). A replace is therefore not an instant
        edit — the order sits in `pending_replace` while the broker works,
        and a SECOND replace fired into that window is rejected outright.
        Chasing a running market means issuing several replaces in a row, so
        the sequencing is not a nicety: without this gate the second re-peg
        of any chase is a coin flip on broker timing.

        The confirmation signal is the OLD order reaching the terminal
        status `replaced` — which is exactly when Alpaca has completed the
        swap. That is read from the real-time `trade_updates` websocket
        first via `wait_for_order_terminal` (PR #287), so the ordinary case
        costs a few milliseconds rather than the whole timeout, and a
        websocket outage degrades to that method's own REST fallback rather
        than to no confirmation at all.

        Returns True only on positive evidence the swap completed:
          * the old order reports `replaced`, or
          * `resolve_replacement_chain` independently shows the old id now
            points at `new_order_id`.

        Anything else — timeout, an unreadable broker, a status that never
        settles — returns False, and the caller MUST stop chasing. "I could
        not confirm" and "it is safe to send another replace" are different
        statements, and conflating them is the whole failure mode this
        guards. Never raises.
        """
        if not old_order_id or not new_order_id:
            return False
        try:
            status = self.wait_for_order_terminal(
                str(old_order_id),
                timeout_seconds=timeout_seconds,
                poll_interval=min(1.0, max(0.1, timeout_seconds)),
            )
            ok(self, "replace_confirmation_wait", order=str(old_order_id))
        except Exception as exc:  # noqa: BLE001
            mark(self, "replace_confirmation_wait", exc, order=str(old_order_id))
            return False
        if str(status or "").lower() == "replaced":
            return True

        # No `replaced` event inside the window. Ask the broker directly
        # rather than concluding either way from silence.
        try:
            resolved = self.resolve_replacement_chain(str(old_order_id))
            ok(self, "replace_confirmation_chain_reread", order=str(old_order_id))
        except Exception as exc:  # noqa: BLE001
            mark(self, "replace_confirmation_chain_reread", exc, order=str(old_order_id))
            return False
        if resolved is not None and str(resolved) == str(new_order_id):
            return True
        logger.warning(
            "replace confirmation: %s → %s could not be confirmed within "
            "%.1fs (last status %r, chain %r) — the chase stops here rather "
            "than firing a second replace into a pending_replace window",
            old_order_id,
            new_order_id,
            timeout_seconds,
            status,
            resolved,
        )
        return False

    def close_position(self, symbol: str) -> dict:
        order = self.client.close_position(_alpaca_symbol(symbol))
        logger.info("Closed position: %s", symbol)
        # Unwrap OrderStatus enum value (see submit_order — same reason).
        return {"id": str(order.id), "status": str(getattr(order.status, "value", order.status))}
