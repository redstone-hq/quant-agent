"""A fake Alpaca that HOLDS shares behind a resting closing stop.

Promoted from tests/test_stop_invariant.py so the partial-sell path can be
proved against the same three measured facts (sandbox, regular hours):
a resting closing stop holds its shares (a submit, amend or SELL that would
exceed what is free is refused with the 403 held_for_orders text), a
fractional order's quantity can never be amended (42210000 → 422), and a
hold is released only once a cancel is CONFIRMED cancelled. A whole-share
quantity PATCH answers 200 with a NEW id and marks the old order
"replaced". Cancels can be made to stay pending.

Offline: no network, no real broker.
"""

from __future__ import annotations

from types import SimpleNamespace

SYM = "ZZZ"
OLD, NEW = 100.0, 105.0


class ApiErr(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


class Order:
    def __init__(self, oid, qty, stop, side):
        self.id, self.qty, self.stop_price, self.limit_price, self.side = oid, qty, stop, None, side
        self.status, self.order_type, self.order_class = "new", "stop", "simple"
        self.legs = self.parent_id = None


class HoldingBroker:
    """Fake Alpaca: share holds, no fractional qty amend, cancels confirm or stay pending."""

    def __init__(self, held, *, pending_cancels=False):
        self.held, self.pending = float(held), pending_cancels
        self.orders: dict[str, Order] = {}
        self.calls: list = []
        self.n = 0
        self.refuse_submits = 0  # how many of the next submits to refuse
        self.refuse_qty_amend = False  # the broker says NO to a whole-share qty amend
        self.qty_amend_unknown = False  # no answer at all to a qty amend

    # -- the hold rule -------------------------------------------------
    def closing_side(self):
        return "buy" if self.held < 0 else "sell"

    def holding(self, excluding=None):
        return sum(
            o.qty
            for o in self.orders.values()
            if o.id != excluding and o.side == self.closing_side() and o.status in ("new", "accepted", "pending_cancel")
        )

    def hold_check(self, extra, excluding=None):
        if self.holding(excluding) + extra > abs(self.held) + 1e-9:
            raise ApiErr("insufficient qty available for order (held_for_orders)", 403)

    def wash_check(self, side):
        """Alpaca refuses an order while an OPPOSITE-side order is open on the
        same symbol (the block src/execution/scale_in.py documents: a resting
        SELL stop and a new BUY cannot both be working)."""
        if any(o.side != side and o.status in ("new", "accepted", "pending_cancel") for o in self.orders.values()):
            raise ApiErr("potential wash trade detected. use complex order", 403)

    def rest(self, qty, stop=OLD):
        o = Order(self.new_id(), float(qty), stop, self.closing_side())
        self.hold_check(o.qty)
        self.orders[o.id] = o
        return o.id

    def new_id(self):
        self.n += 1
        return f"o{self.n}"

    def book(self):
        return sorted((o.qty, o.stop_price) for o in self.orders.values() if o.status in ("new", "accepted"))

    # -- the client surface the desk calls --------------------------------
    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def replace_order_by_id(self, oid, req):
        fields = {k: getattr(req, k) for k in ("qty", "stop_price") if getattr(req, k, None) is not None}
        self.calls.append(("replace", oid, fields))
        old = self.orders[oid]
        assert old.status in ("new", "accepted"), f"replace of a {old.status} order"
        if req.qty is not None:
            if old.qty != int(old.qty):
                raise ApiErr("42210000: cannot replace qty in fractional stop order", 422)
            if self.qty_amend_unknown:
                raise ApiErr("gateway timeout", 504)
            if self.refuse_qty_amend:
                raise ApiErr("422: replace refused", 422)
            if float(req.qty) > old.qty:  # a reduction frees shares; only growth needs room
                self.hold_check(float(req.qty), excluding=oid)
        new = Order(
            self.new_id(),
            float(req.qty) if req.qty is not None else old.qty,
            float(req.stop_price or old.stop_price),
            old.side,
        )
        old.status = "replaced"
        self.orders[new.id] = new
        return new

    def cancel_order_by_id(self, oid):
        self.calls.append(("cancel", oid))
        self.orders[oid].status = "pending_cancel" if self.pending else "canceled"

    def submit_stop(self, symbol, qty, stop_price, *, side="sell"):
        self.calls.append(("submit", float(qty), side))
        if self.refuse_submits:
            self.refuse_submits -= 1
            raise ApiErr("422: submit refused", 422)
        self.wash_check(side)
        self.hold_check(float(qty))
        o = Order(self.new_id(), float(qty), float(stop_price), side)
        self.orders[o.id] = o
        return {"id": o.id, "status": "accepted", "symbol": symbol, "qty": qty}

    def wait_terminal(self, oid, **_kw):
        return self.orders[oid].status

    def by_side(self, symbol):
        live = [o for o in self.orders.values() if o.status in ("new", "accepted")]
        return [o for o in live if o.side == "sell"], [o for o in live if o.side == "buy"]

    def positions(self):
        return [SimpleNamespace(symbol=SYM, qty=self.held)]

    # -- surfaces the partial-sell path reads ------------------------------
    def get_order_by_id(self, oid):
        self.calls.append(("get_order", oid))
        return self.orders[oid]

    def sell(self, symbol, qty, side="sell"):
        """The trim order itself: refused like Alpaca when the shares it would
        shed are still held by a resting stop; otherwise filled at once."""
        self.calls.append(("sell", float(qty), side))
        self.wash_check(side)
        self.hold_check(float(qty))
        self.held -= float(qty) if self.held > 0 else -float(qty)
        o = Order(self.new_id(), float(qty), 0.0, side)
        o.status, o.filled_qty = "filled", float(qty)
        self.orders[o.id] = o
        return {"id": o.id, "status": "filled", "symbol": symbol, "qty": qty}

    def live_stop_qty(self):
        return sum(
            o.qty for o in self.orders.values() if o.side == self.closing_side() and o.status in ("new", "accepted")
        )
