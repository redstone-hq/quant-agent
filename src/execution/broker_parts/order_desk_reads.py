"""Order-listing reads lifted verbatim from OrderDesk, taking the desk explicitly.

`OrderDesk` keeps a same-named thin shim for each function here, so every caller
and patch target on the desk still resolves. The bodies read only `desk.client`,
`desk.get_latest_price` and the order-desk effect recorders, so they run against
a fake desk with no broker or TradingPipeline.
"""

from __future__ import annotations

from alpaca.trading.enums import OrderSide, QueryOrderStatus

from src.execution.broker_parts.order_desk_effects import mark, ok
from src.execution.broker_parts.stop_place import _alpaca_symbol, _internal_symbol


def list_open_entry_order_ids(
    desk,
    symbol: str,
    *,
    side: str | None = None,
) -> list[str]:
    """Ids of working non-stop BUY/SELL orders for `symbol`.

    The discriminator matches `cancel_open_entry_orders`: any *stop*
    order is a protective leg and is omitted; every other working
    BUY or SELL is an entry. Named so scale-in crash recovery can
    confirm leftover DAY adds are gone before it rearms a protective
    sell (a working BUY plus a new SELL stop is the wash-trade block
    the scale-in sequence exists to walk around).

    `side`, when given ("buy" / "sell"), returns only that side. The
    short scale-in wash-trade guard passes ``side="buy"`` to find any
    FOREIGN working BUY (a resting cover-limit / take-profit) that would
    collide with its SELL add — protective buy-stops are stop orders and
    are already excluded here, so a returned BUY is never the protection.
    Default None keeps every existing caller's both-sides behaviour.

    Returns [] on an API failure (fail-OPEN) — the leftover-entry drain
    check treats that the same as "none working". A caller that must
    tell "confirmed empty" from "could not read" — the wash-trade guard,
    which cancels protection on the answer — uses
    `list_open_entry_orders_checked` instead.
    """
    _ok, ids = desk.list_open_entry_orders_checked(symbol, side=side)
    return ids


def list_open_orders_checked(desk) -> tuple[bool, list]:
    """`(ok, orders)` for every working order, flat (nested=False) so each
    bracket leg is its own row; ``ok`` is FALSE when the listing FAILED
    (vs a genuine empty list). The freeze sweep touches nothing on FALSE.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest

        orders = desk.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=False))
        ok(desk, "list_open_orders_checked")
    except Exception as exc:  # noqa: BLE001
        mark(desk, "list_open_orders_checked", exc)
        return False, []
    return True, list(orders or [])


def list_open_entry_orders_checked(
    desk,
    symbol: str,
    *,
    side: str | None = None,
) -> tuple[bool, list[str]]:
    """`(ok, ids)` for working non-stop orders — same discriminator as
    `list_open_entry_order_ids`, but ``ok`` is FALSE when the broker's
    order listing itself FAILED (vs a genuine empty list, ``(True, [])``).

    The short scale-in wash-trade guard must FAIL CLOSED: it is about to
    cancel a protective buy-stop and submit a SELL add, and it may not do
    that on an unverified assumption that Alpaca will bounce a self-cross
    (paper may not enforce it). ``ok=False`` lets it refuse rather than
    guess "no foreign buy" from a swallowed API error.
    """
    want_side = str(side).lower() if side is not None else None
    try:
        from alpaca.trading.requests import GetOrdersRequest

        orders = desk.client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[_alpaca_symbol(symbol)],
                nested=True,
            )
        )
        ok(desk, "list_open_entry_orders_checked", symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        mark(desk, "list_open_entry_orders_checked", exc, symbol=symbol)
        return False, []
    ids: list[str] = []
    for order in orders or []:
        order_id = getattr(order, "id", None)
        order_side = str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).lower()
        order_type = str(getattr(getattr(order, "order_type", None), "value", getattr(order, "order_type", ""))).lower()
        if order_side not in ("buy", "sell") or not order_id:
            continue
        if "stop" in order_type:
            continue
        if want_side is not None and order_side != want_side:
            continue
        ids.append(str(order_id))
    return True, ids


def _stated_order_price(order) -> float | None:
    """The order's own limit price, else its stop price, when either is a positive number."""
    price = None
    for attr in ("limit_price", "stop_price"):
        raw = getattr(order, attr, None)
        if raw is not None:
            try:
                candidate = float(raw)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                price = candidate
                break
    return price


def open_buy_notional(desk) -> float | None:
    """Dollar notional of all OPEN BUY orders, or None when the query fails.

    Used by the cash sweeper: Alpaca's `cash` field does not subtract
    open-order holds, so parking must leave room for still-working BUY
    limits. The None-vs-0.0 distinction matters — a transient API failure
    must read as "unknowable" (caller skips parking), never as "no
    pending buys" (caller would sweep cash a pending fill needs).
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest

        orders = desk.client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                side=OrderSide.BUY,
                nested=True,
            )
        )
        total = 0.0
        for order in orders or []:
            order_side = getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))
            if str(order_side).lower() != "buy":
                continue
            try:
                qty = float(getattr(order, "qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0
            price = _stated_order_price(order)
            if price is None:
                # Market order with no price attached — estimate from the
                # live quote; on failure treat the whole answer as
                # unknowable rather than under-counting the hold.
                live = desk.get_latest_price(getattr(order, "symbol", ""))
                if not live or live <= 0:
                    return None
                price = live
            total += qty * price
        ok(desk, "open_buy_notional")
        return total
    except Exception as exc:
        mark(desk, "open_buy_notional", exc)
        return None


def list_recent_orders(
    desk,
    symbol: str,
    side: str,
    after,
) -> list[dict] | None:
    """All of `symbol`'s orders (any status) on `side` since `after`.

    audit F4: used by the orphan-pending_submit sweep to match a DB
    write-ahead row to a broker order whose id we lost to a crash
    between submit_order() and confirm_trade_submitted(). Returns
    light dicts {id, symbol, side, qty, status}.

    audit F4 (review #2): the return distinguishes "query succeeded,
    zero orders" ([]) from "query FAILED" (None). The caller must
    NOT treat a transient Alpaca/API failure as "submit never
    landed" — doing so would mark a possibly-real / already-filled
    BUY as submit_failed. None ⇒ leave the row and retry next
    session; [] ⇒ genuinely no such order.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest

        want = side.lower()
        req_side = OrderSide.BUY if want == "buy" else OrderSide.SELL
        orders = desk.client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                symbols=[_alpaca_symbol(symbol)],
                side=req_side,
                after=after,
                nested=False,
            )
        )
        out: list[dict] = []
        for o in orders or []:
            o_side = str(getattr(getattr(o, "side", None), "value", getattr(o, "side", ""))).lower()
            if o_side != want:
                continue
            try:
                oqty = float(getattr(o, "qty", 0) or 0)
            except (TypeError, ValueError):
                oqty = 0.0
            oid = str(getattr(o, "id", "") or "")
            if not oid:
                continue
            out.append(
                {
                    "id": oid,
                    "symbol": _internal_symbol(getattr(o, "symbol", "") or ""),
                    "side": o_side,
                    "qty": oqty,
                    "status": str(getattr(getattr(o, "status", None), "value", getattr(o, "status", ""))).lower(),
                }
            )
        ok(desk, "list_recent_orders", symbol=symbol, side=side)
        return out
    except Exception as exc:
        mark(desk, "list_recent_orders", exc, symbol=symbol, side=side)
        return None


def list_filled_sell_orders(desk, symbol: str, after) -> list[dict] | None:
    """Every FILLED sell-side order for `symbol` whose FILL happened at
    or after `after` — broker truth, independent of anything this
    process itself submitted or remembers.

    2026-08-28 ONDS/CCJ: both positions were closed by their broker-
    resident protective stop (a GTC stop-MARKET order — stop-limit only
    on the unsupported-combo fallback — placed by
    `place_entry_protection` / `_repair_stop_coverage` /
    `shift_stops_down`), and none of those paths ever write the STOP
    ORDER ITSELF into `trades` — only every system-DECIDED exit (SELL /
    REDUCE / TRAIL_STOP / SWEEP_SELL) does that, at submission time.
    `_reconcile_stop_out_fills` (src/pipeline.py) uses this method to
    ask the broker directly rather than trusting the ledger's own
    opinion of what happened, then diffs the result against
    `Database.get_known_broker_order_ids` to find fills the ledger has
    never recorded.

    `after` is applied CLIENT-SIDE against each order's `filled_at`,
    deliberately NOT passed to Alpaca's own `after=` query parameter
    (unlike `list_recent_orders`, which correctly uses it that way for
    its own purpose). Alpaca's `after`/`until` filter on `submitted_at`
    — when it was ACCEPTED, not when it EXECUTED — and a GTC protective
    stop is typically submitted at entry and can rest for a long time
    before firing. Verified 2026-08-28 against the real paper account
    (MRVL): the stop was submitted 2026-08-21 13:35 and filled
    2026-08-24 13:48 — a naive `after=now-7d` broker-side query anchored
    4 days before "now" would have excluded it entirely (its
    submitted_at sat 7h before that cutoff) even though the FILL was
    comfortably inside the 7-day window everyone actually cares about.
    Silently missing a stop-out because the underlying order happened
    to be placed slightly outside an arbitrary lookback is exactly the
    failure mode this reconciler exists to prevent, so the broker query
    below is intentionally unbounded on symbol+side and every date
    filtering happens here, against the field that actually means
    "when did this become a real exit".

    Distinct from `list_recent_orders`: that method returns orders of
    ANY status and is used by the orphan-BUY sweep to match a KNOWN
    write-ahead row by qty, submitted within a tight recent window —
    `submitted_at` is exactly the right anchor there. This method is
    scoped to already-FILLED sells and is used to discover fills the
    ledger has NEVER SEEN, including ones this process itself placed at
    the broker (a protective stop) but never logged — `filled_at` is
    the only anchor that means what the caller needs it to mean.

    Returns None on a query failure — the caller must retry on the
    next reconciliation pass rather than concluding "no fills" and
    risking a missed exit (same None-means-retry contract as
    `list_recent_orders`). On success, a list of lightweight dicts:
    {id, symbol, qty (the ACTUAL filled qty), price (the ACTUAL filled
    avg price), filled_at (ISO-8601 UTC string, or None if the broker
    didn't report one), order_type} — orders with no filled_at at all
    are KEPT (never silently excluded by the date filter; None means
    "unknown timing", not "too old").
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest

        orders = desk.client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                symbols=[_alpaca_symbol(symbol)],
                side=OrderSide.SELL,
                nested=False,
            )
        )
        out: list[dict] = []
        for o in orders or []:
            status = str(getattr(getattr(o, "status", None), "value", getattr(o, "status", ""))).lower()
            if status != "filled":
                continue
            oid = str(getattr(o, "id", "") or "")
            if not oid:
                continue
            try:
                filled_qty = float(getattr(o, "filled_qty", 0) or 0)
            except (TypeError, ValueError):
                filled_qty = 0.0
            try:
                filled_avg_price = float(getattr(o, "filled_avg_price", 0) or 0)
            except (TypeError, ValueError):
                filled_avg_price = 0.0
            if filled_qty <= 0 or filled_avg_price <= 0:
                # "filled" with no actual qty/price is not a real fill
                # to reconstruct a ledger row from — nothing to record.
                continue
            filled_at = getattr(o, "filled_at", None)
            if filled_at is not None and after is not None:
                cutoff = (
                    after
                    if getattr(after, "tzinfo", None)
                    else after.replace(
                        tzinfo=filled_at.tzinfo,
                    )
                )
                if filled_at < cutoff:
                    continue
            order_type = getattr(o, "type", None) or getattr(o, "order_type", None)
            out.append(
                {
                    "id": oid,
                    "symbol": _internal_symbol(getattr(o, "symbol", "") or ""),
                    "qty": filled_qty,
                    "price": filled_avg_price,
                    "filled_at": filled_at.isoformat() if hasattr(filled_at, "isoformat") else None,
                    "order_type": str(getattr(order_type, "value", order_type)) if order_type else None,
                }
            )
        ok(desk, "list_filled_sell_orders", symbol=symbol)
        return out
    except Exception as exc:
        mark(desk, "list_filled_sell_orders", exc, symbol=symbol)
        return None


def get_order_fill_info(desk, order_id: str) -> dict | None:
    """Return {status, filled_qty, filled_avg_price, filled_at} for an order, or None.

    Used by Phase 3 reconciliation. The caller decides whether the
    returned status is terminal; this method does not block / poll.
    """
    try:
        order = desk.client.get_order_by_id(order_id)
        ok(desk, "get_order_fill_info", order=order_id)
    except Exception as exc:
        mark(desk, "get_order_fill_info", exc, order=order_id)
        return None
    status = str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
    try:
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        filled_qty = 0.0
    try:
        filled_avg_price = float(getattr(order, "filled_avg_price", 0) or 0)
    except (TypeError, ValueError):
        filled_avg_price = 0.0
    # The broker's own fill time (ISO-8601), the clock the lot walk orders fills
    # by; None when it reported none - never a substitute such as now.
    from datetime import datetime as _datetime

    raw_filled_at = getattr(order, "filled_at", None)
    if isinstance(raw_filled_at, _datetime):
        filled_at = raw_filled_at.isoformat()
    elif isinstance(raw_filled_at, str) and raw_filled_at.strip():
        filled_at = raw_filled_at.strip()
    else:
        filled_at = None
    return {
        "status": status,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "filled_at": filled_at,
    }
