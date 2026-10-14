"""A fake Alpaca that HOLDS shares behind a resting closing stop.

Promoted from tests/test_stop_invariant.py so the partial-sell path can be
proved against the same measured facts (practice account, 2026-10-09 to
2026-10-14): a resting closing stop holds its shares (a submit, amend or SELL
that would exceed what is free is refused with 403 / 40310000), a fractional
order's quantity can never be amended (422 / 42210000 "qty must be an
integer"), a FILLED order cannot be cancelled (422 / 42210000), an amend of
an order the broker holds in the "accepted" state (after hours) is refused
(422 / 42210000), and a hold is released only once a cancel is CONFIRMED
cancelled. A whole-share quantity PATCH answers 200 with a NEW id and marks
the old order "replaced". Cancels can be made to stay pending.

Refusals carry the real wire shape: like alpaca-py's APIError, `str(err)` is
the JSON body the broker sent, `.code` and `.message` parse it, and
`.status_code` is the HTTP status. Texts are copied from the logged bodies
(practice-account order test and exit test logs; only the broker's fixed
wording and fields are kept, ids and prices are invented), never composed.

Offline: no network, no real broker.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

SYM = "ZZZ"
OLD, NEW = 100.0, 105.0

# the codes seen on the wire
FORBIDDEN_QTY = 40310000  # 403: insufficient qty available (held_for_orders)
UNPROCESSABLE = 42210000  # 422: qty not an integer / already filled / cannot replace


class ApiErr(Exception):
    """alpaca-py APIError: str() is the JSON body; .code/.message parse it."""

    def __init__(self, body, status_code):
        super().__init__(body if isinstance(body, str) else json.dumps(body, sort_keys=True))
        self.status_code = status_code

    @property
    def code(self):
        return json.loads(str(self))["code"]

    @property
    def message(self):
        return json.loads(str(self))["message"]


def refusal(status, code, message, **extra):
    """The broker's refusal: a JSON body with `code` and `message` (+ extras)."""
    return ApiErr({"code": code, "message": message, **extra}, status)


def _num(x):
    """Quantity as the broker prints it: "10", "2.5", never "10.0"."""
    x = round(float(x), 9)
    return str(int(x)) if x == int(x) else repr(x)


class Order:
    def __init__(self, oid, qty, stop, side, status="new"):
        self.id, self.qty, self.stop_price, self.limit_price, self.side = oid, qty, stop, None, side
        self.status, self.order_type, self.order_class = status, "stop", "simple"
        self.legs = self.parent_id = None


class HoldingBroker:
    """Fake Alpaca: share holds, no fractional qty amend, cancels confirm or stay pending."""

    def __init__(self, held, *, pending_cancels=False, market_open=True):
        self.held, self.pending = float(held), pending_cancels
        self.market_open = market_open  # closed: new orders rest as "accepted" and cannot be amended
        self.orders: dict[str, Order] = {}
        self.calls: list = []
        self.n = 0
        self.refuse_submits = 0  # how many of the next submits to refuse
        self.refuse_qty_amend = False  # the broker says NO to a whole-share qty amend
        self.qty_amend_unknown = False  # no answer at all to a qty amend

    # -- the hold rule -------------------------------------------------
    def closing_side(self):
        return "buy" if self.held < 0 else "sell"

    def _holding_orders(self, excluding=None):
        return [
            o
            for o in self.orders.values()
            if o.id != excluding and o.side == self.closing_side() and o.status in ("new", "accepted", "pending_cancel")
        ]

    def holding(self, excluding=None):
        return sum(o.qty for o in self._holding_orders(excluding))

    def hold_check(self, extra, excluding=None):
        if self.holding(excluding) + extra > abs(self.held) + 1e-9:
            held_for = self.holding(excluding)
            avail = max(abs(self.held) - held_for, 0.0)
            raise refusal(
                403,
                FORBIDDEN_QTY,
                f"insufficient qty available for order (requested: {_num(extra)}, available: {_num(avail)})",
                available=_num(avail),
                existing_qty=_num(abs(self.held)),
                held_for_orders=_num(held_for),
                related_orders=[o.id for o in self._holding_orders(excluding)],
                symbol=SYM,
            )

    def wash_check(self, side):
        """Alpaca refuses an order while an OPPOSITE-side order is open on the
        same symbol (the block src/execution/scale_in.py documents: a resting
        SELL stop and a new BUY cannot both be working)."""
        if any(o.side != side and o.status in ("new", "accepted", "pending_cancel") for o in self.orders.values()):
            raise ApiErr("potential wash trade detected. use complex order", 403)

    def _rest_status(self):
        return "new" if self.market_open else "accepted"

    def rest(self, qty, stop=OLD):
        o = Order(self.new_id(), float(qty), stop, self.closing_side(), self._rest_status())
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
        return SimpleNamespace(is_open=self.market_open)

    def replace_order_by_id(self, oid, req):
        fields = {k: getattr(req, k) for k in ("qty", "stop_price") if getattr(req, k, None) is not None}
        self.calls.append(("replace", oid, fields))
        old = self.orders[oid]
        assert old.status in ("new", "accepted"), f"replace of a {old.status} order"
        if old.status == "accepted":  # measured after hours: ANY amend (price too) is refused
            raise refusal(422, UNPROCESSABLE, "cannot replace order in accepted status")
        if req.qty is not None:
            if old.qty != int(old.qty):
                raise refusal(422, UNPROCESSABLE, "qty must be an integer")
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
            self._rest_status(),
        )
        old.status = "replaced"
        self.orders[new.id] = new
        return new

    def cancel_order_by_id(self, oid):
        self.calls.append(("cancel", oid))
        if self.orders[oid].status == "filled":  # measured: DELETE on a filled order
            raise refusal(422, UNPROCESSABLE, 'order is already in "filled" state')
        self.orders[oid].status = "pending_cancel" if self.pending else "canceled"

    def submit_stop(self, symbol, qty, stop_price, *, side="sell"):
        self.calls.append(("submit", float(qty), side))
        if self.refuse_submits:
            self.refuse_submits -= 1
            raise ApiErr("422: submit refused", 422)
        self.wash_check(side)
        self.hold_check(float(qty))
        o = Order(self.new_id(), float(qty), float(stop_price), side, self._rest_status())
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
