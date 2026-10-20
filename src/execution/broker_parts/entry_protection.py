"""Entry protection, lifted verbatim from AlpacaBroker.place_entry_protection.

Wait for an entry to reach terminal, cancel a still-working remainder, then
place the protective stop for the ACTUAL filled quantity. `AlpacaBroker` keeps
a same-named one-line shim that calls `place_entry_protection(self, ...)`, and
every other broker member is reached through `broker.<name>`, so a class-level
patch on `AlpacaBroker` still bites.

`_ENTRY_FILL_TIMEOUT_S` stays defined (and ledgered) in `src.execution.broker`;
the shim passes the broker module's CURRENT value in by keyword, so a test that
patches `src.execution.broker._ENTRY_FILL_TIMEOUT_S` still reaches this body,
which reads the name byte-identically. `_ENTRY_SIDES` moved here with its
comment; `src.execution.broker` re-exports it.
"""

from __future__ import annotations

import logging

from src.execution.held_qty import broker_position_qty, cover_qty_for_rearm
from src.sentinel.guarded import record_guarded_pass

# The moved body logs under the broker's logger name, and the move must not
# change what callers and tests see.
logger = logging.getLogger("src.execution.broker")

#: Sentinel sell_order_id for a write-ahead restore row with no closing order to
#: query (same value as src.protection.sell_finalization._WAL_SELL_SENTINEL; the
#: drain reads the live broker position for such rows).
_WAL_SELL_SENTINEL = "__WAL_PENDING__"

#: Entry sides `place_entry_protection` will derive a protective side from.
#: Anything else is refused rather than guessed — see the fail-closed note in
#: `place_entry_protection`. "sell" and "sell_short" both open/extend a short.
_ENTRY_SIDES = frozenset({"buy", "sell", "sell_short"})


def place_entry_protection(
    broker,
    symbol: str,
    order_id: str,
    stop_price: float,
    *,
    requested_qty: float | None = None,
    side: str = "buy",
    superseded_filled_qty: float = 0.0,
    on_unfilled_cancel=None,
    cover_full_position: bool = False,
    held_qty_before: float = 0.0,
    persist_restore=None,
    _ENTRY_FILL_TIMEOUT_S: float,
) -> dict | None:
    """Wait for an entry order to reach terminal, then place a GTC
    protective stop (stop-MARKET, guaranteed exit) for the ACTUAL filled
    qty.

    If the entry is STILL WORKING after the wait (slow tape, wide limit),
    the unfilled remainder is CANCELLED first — audit round 2: the 15s
    wait treated "still live" identically to "terminal 0-fill" and walked
    away, so a DAY entry limit could fill hours later with no stop
    watching it (and a resting BUY could even re-buy into a crash after an
    emergency liquidation). Cancelling converges the order; whatever DID
    fill by then gets its stop from the post-cancel re-read. Losing the
    unfilled remainder is the accepted cost of protection-first.

    THIS CANCEL IS THE END-OF-CYCLE CANCEL (owner-approved 2026-09-12),
    and its timing is derived, not chosen. This desk does not run
    continuously: it runs as separate scheduled SESSIONS — see
    `SESSION_WINDOWS` in `src/trading_calendar.py` — each a single
    process that analyses at the prices and levels of that moment,
    proposes entries, submits them, protects the fills, and EXITS. New
    entries come only from the morning session; the midday and close
    sessions review positions. (The systemd/launchd timer ticks every 30
    minutes, but that tick only asks `scripts/run_if_et_window.sh`
    whether a session is due; it is not a re-scan.) So "the decision
    cycle that created the order" is this very process, and its boundary
    is the point where this process stops waiting for the fill and moves
    on — which is exactly here. An entry that outlived its own session
    would be resting on a thesis nobody is still holding: the next
    session re-analyses from scratch at real current prices and will
    re-propose the trade if it still wants it. Cancelling here — rather
    than leaving a DAY order resting until 16:00 ET — is therefore
    binding the order's life to the desk's own heartbeat, not to a
    timeout somebody picked. There is deliberately no separate "cancel
    after N minutes" constant: the boundary IS the end of this stage, and
    stays correct if the session schedule ever changes. The only number
    in play is `_ENTRY_FILL_TIMEOUT_S`, the in-cycle patience for a fill,
    which predates this and is unchanged.

    `on_unfilled_cancel`, when given, is called with a small dict
    (`order_id`, `status`, `filled_qty`) after a still-working entry was
    cancelled here and the post-cancel re-read shows NOTHING filled under
    any id in its chain. The caller uses it to page the owner with the
    prices that were tried — this method does not know them. Not invoked
    for a partial fill (shares were acquired and the stop covers them)
    or for an order that reached terminal on its own. Never allowed to
    raise into this method.

    `side` is the ENTRY order's own side — "buy" opens or adds to a long
    (the only side any order path in this repo has ever submitted, hence
    the default), "sell"/"sell_short" opens a short. The protective stop
    is always the OPPOSITE side: a SELL stop below a long, a BUY stop
    above a short, both stop-MARKET.

    `superseded_filled_qty` is shares this entry already acquired under a
    DIFFERENT order id — the ancestors of a re-peg chain. `order_id` is
    the last order in that chain, and Alpaca's fill counters do not carry
    across a replacement, so the shares an ancestor filled are invisible
    here. They are real shares in a real position, and a stop sized to
    only the last order's fill would leave them naked. Adding them is what
    keeps the invariant "every filled share is under a stop" true across a
    re-peg. Default 0.0: for every caller that never re-pegs, this method
    behaves exactly as it did before.

    `cover_full_position` (long scale-in path B, 2026-09-15): after a
    positive fill, size the protective sell to the broker's FULL
    position quantity, not this order's fill. A partial add on a name
    that already held shares would otherwise rearm a stop over the
    add alone and leave the original lot naked. `held_qty_before` is
    the fallback if the broker position cannot be read: fill + what
    was held, the two quantities already measured, not a third number.

    Returns the stop order dict, or None when nothing was placed (entry
    filled 0 / stop submit failed). Never raises — a failure here must not
    abort the session.

    Spec §11.1 guard 1: the stop submission now RETRIES immediately and
    hard before giving up (`_submit_protective_stop_retrying`). A None
    return therefore means the retries were exhausted, and the position is
    naked — the CALLER owes an owner alert on it (guard 2); the
    coverage-reconcile auto-repair belt remains the backstop, not the
    first line.
    """
    # Fail closed on a side we do not recognise, BEFORE touching the
    # broker. `"sell" if side == "buy" else "buy"` reads harmlessly but is
    # fail-OPEN: a typo, a None, or some future side string falls into the
    # short branch, and a LONG then gets a BUY stop placed ABOVE it — not
    # weak protection, but a standing order to buy more of a position that
    # is already losing.
    #
    # This returns rather than raising, because the contract above is that
    # this function never aborts a session. Returning None is the same
    # outcome as any other protection failure: logged at ERROR, position
    # left naked-but-KNOWN, and picked up by the coverage-reconcile
    # auto-repair belt. Naked-and-believed-covered is the state that
    # actually costs money, and refusing here is what prevents it.
    normalized = (side or "").strip().lower()
    if normalized not in _ENTRY_SIDES:
        logger.error(
            "entry protection: %s refusing to guess a protective side for "
            "entry side %r (expected one of %s) — NO stop placed, position "
            "will be left uncovered and must be repaired by reconcile",
            symbol,
            side,
            sorted(_ENTRY_SIDES),
        )
        return None

    try:
        status = broker.wait_for_order_terminal(
            order_id,
            timeout_seconds=_ENTRY_FILL_TIMEOUT_S,
        )
        record_guarded_pass(broker, "entry_protection.wait_terminal", context={"symbol": symbol, "order": order_id})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            broker,
            "entry_protection.wait_terminal",
            exc,
            log=logger,
            context={"symbol": symbol, "order": order_id, "effect": "status unknown"},
        )
        status = None

    cancelled_here = False
    if (status or "").lower() not in broker._TERMINAL_ORDER_STATES:
        # Still working at the end of its cycle — cancel the remainder so
        # it can't fill unwatched and so it stops resting on a thesis
        # this session is about to walk away from (see the docstring).
        # A fill can land during cancel propagation; the post-cancel
        # re-read below protects whatever landed.
        logger.warning(
            "entry protection: %s entry %s still working at the end of "
            "its session (status=%s) — cancelling the unfilled remainder "
            "so no share can fill without a stop watching it and no "
            "order outlives the analysis that created it",
            symbol,
            order_id,
            status or "unknown",
        )
        try:
            broker.client.cancel_order_by_id(order_id)
            cancelled_here = True
            record_guarded_pass(
                broker, "entry_protection.cancel_working_entry", context={"symbol": symbol, "order": order_id}
            )
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                broker,
                "entry_protection.cancel_working_entry",
                exc,
                log=logger,
                context={
                    "symbol": symbol,
                    "order": order_id,
                    "effect": "a later fill will be UNPROTECTED until the next coverage reconcile",
                },
            )
        try:
            status = (
                broker.wait_for_order_terminal(order_id)  # the one order-terminal ceiling
                or status
            )
            record_guarded_pass(
                broker, "entry_protection.wait_after_cancel", context={"symbol": symbol, "order": order_id}
            )
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                broker,
                "entry_protection.wait_after_cancel",
                exc,
                log=logger,
                context={
                    "symbol": symbol,
                    "order": order_id,
                    "effect": "falls through to the unconfirmed-outcome branch below",
                },
            )
        if (status or "").lower() not in broker._TERMINAL_ORDER_STATES:
            # Fill confirmation has genuinely DEGRADED: the bounded
            # window closed, the cancel-and-recheck closed too, and the
            # broker still has not said what happened to a live order.
            # The desk proceeds on filled_qty=0 below — the safe
            # assumption, possibly a wrong one — so the owner has to be
            # told, not just the log. This is NOT "the websocket is
            # off": it is reachable identically with the socket on, and
            # is exactly the outcome the REST path is supposed to
            # prevent. See src/notifier.py's fill-confirmation block.
            try:
                from src.notifier import alert_order_outcome_unconfirmed

                alert_order_outcome_unconfirmed(
                    symbol,
                    order_id,
                    waited_seconds=_ENTRY_FILL_TIMEOUT_S,
                    last_status=(status or "").lower() or None,
                )
                record_guarded_pass(
                    broker, "entry_protection.unconfirmed_alert", context={"symbol": symbol, "order": order_id}
                )
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(
                    broker,
                    "entry_protection.unconfirmed_alert",
                    exc,
                    log=logger,
                    context={
                        "symbol": symbol,
                        "order": order_id,
                        "effect": "the owner was NOT told the outcome is unconfirmed",
                    },
                )

    fill_info_lost = False
    try:
        info = broker.get_order_fill_info(order_id) or {}
        record_guarded_pass(broker, "entry_protection.fill_info", context={"symbol": symbol, "order": order_id})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            broker,
            "entry_protection.fill_info",
            exc,
            log=logger,
            context={
                "symbol": symbol,
                "order": order_id,
                "effect": "fill unknown; broker position re-read decides the stop, else the WAL restore owns it",
            },
        )
        info = {}
        fill_info_lost = True
    try:
        filled_qty = float(info.get("filled_qty") or 0)
    except (TypeError, ValueError):
        filled_qty = 0.0
    if fill_info_lost:
        # A lost fill reply proves nothing (docs/OUTCOME.md: protections fail
        # closed). The broker position is the source of truth: shares above
        # what was held before the entry are this entry's fill and get the
        # stop. If the position cannot be read either, hand the symbol to the
        # write-ahead restore so the next drain sizes a stop from live truth.
        current = broker_position_qty(broker, symbol)
        held_abs = abs(float(held_qty_before or 0))
        if current is None:
            spec_qty = float(requested_qty or 0) + float(superseded_filled_qty or 0)
            if cover_full_position:
                spec_qty += held_abs
            protective_side = "sell" if normalized == "buy" else "buy"
            if persist_restore is not None and spec_qty > 0:
                persist_restore(
                    _WAL_SELL_SENTINEL,
                    symbol,
                    held_abs + spec_qty,
                    [{"qty": spec_qty, "stop_price": float(stop_price), "side": protective_side}],
                    side=protective_side,
                )
                logger.error(
                    "entry protection: %s entry %s fill AND position unreadable — handed to the "
                    "write-ahead restore; the next drain places the stop from the broker position",
                    symbol,
                    order_id,
                )
            else:
                logger.error(
                    "entry protection: %s entry %s fill AND position unreadable and no restore "
                    "path wired — position may be unprotected",
                    symbol,
                    order_id,
                )
            return None
        new_shares = max(0.0, abs(float(current)) - held_abs)
        filled_qty = new_shares
        logger.warning(
            "entry protection: %s entry %s fill reply lost — broker position %.4f vs %.4f held before; "
            "protecting %.4f new share(s)",
            symbol,
            order_id,
            abs(float(current)),
            held_abs,
            new_shares,
        )
    try:
        carried = float(superseded_filled_qty or 0)
    except (TypeError, ValueError):
        carried = 0.0
    if carried > 0:
        logger.info(
            "entry protection: %s carries %.4f share(s) filled under a "
            "superseded order id; stop will cover %.4f + %.4f",
            symbol,
            carried,
            filled_qty,
            carried,
        )
        filled_qty += carried
    if filled_qty > 0 and cover_full_position:
        full_qty = cover_qty_for_rearm(
            broker,
            symbol=symbol,
            filled_qty=filled_qty,
            held_qty_before=held_qty_before,
        )
        if full_qty > filled_qty + 1e-9:
            logger.info(
                "entry protection: %s scale-in fill %.4f — stop sized to broker full position %.4f, not the add alone",
                symbol,
                filled_qty,
                full_qty,
            )
        if full_qty > 0:
            filled_qty = full_qty

    if filled_qty <= 0:
        logger.warning(
            "entry protection: %s entry %s filled 0 (status=%s) — no stop placed (nothing to protect)",
            symbol,
            order_id,
            status or "unknown",
        )
        if cancelled_here and on_unfilled_cancel is not None:
            try:
                on_unfilled_cancel(
                    {
                        "order_id": order_id,
                        "status": (status or "").lower() or "unknown",
                        "filled_qty": 0.0,
                    }
                )
                record_guarded_pass(
                    broker, "entry_protection.unfilled_cancel_callback", context={"symbol": symbol, "order": order_id}
                )
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(
                    broker,
                    "entry_protection.unfilled_cancel_callback",
                    exc,
                    log=logger,
                    context={
                        "symbol": symbol,
                        "order": order_id,
                        "effect": "the caller was not told the entry went unfilled",
                    },
                )
        return None
    if requested_qty and filled_qty < requested_qty and not cover_full_position:
        logger.warning(
            "entry protection: %s partially filled %.4f/%.4f — stop sized to the ACTUAL fill",
            symbol,
            filled_qty,
            requested_qty,
        )
    # The protective order's side is the OPPOSITE of the entry's: a BUY
    # entry (long) is protected by a SELL stop below it; a SELL/SELL_SHORT
    # entry (short) is protected by a BUY stop above it. Getting
    # this backwards is THE most dangerous bug in shorts-safe: the order
    # still submits without error, it just sits on the wrong side of the
    # trigger and can never fill, so the position runs unprotected in
    # exactly the direction it needed protecting.
    protective_side = "sell" if normalized == "buy" else "buy"
    stop_order = broker._submit_protective_stop_retrying(
        symbol=symbol,
        qty=filled_qty,
        stop_price=stop_price,
        side=protective_side,
    )
    if stop_order is None:
        logger.error(
            "entry protection FAILED for %s (%.4f shares held, stop $%.2f) "
            "— position is UNPROTECTED; the caller must "
            "raise an OWNER alert (spec §11.1 guard 2) and the coverage "
            "reconcile must repair it",
            symbol,
            filled_qty,
            stop_price,
        )
        return None
    return stop_order
