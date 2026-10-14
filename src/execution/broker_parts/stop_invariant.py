"""Item 201: the resting-stop QUANTITY invariant, enforced after every price amend.

Let H = |held|, W = floor(H), F = H - W, g = the whole-share (GTC) closing-stop
sum and d = the sub-share (DAY sliver) sum. Target: g == W and d == F, and at
NO instant may g + d exceed H. One rule for a long and a short alike (a short
is treated exactly like a long): the broker enforces the ceiling on a long by
holding the shares (`held_for_orders`); the desk enforces it on both sides so
a short can never be over-covered and flipped.

Why the ORDER of calls matters (GTC 1 + DAY 0.37, now 2.0 held): the broker
refuses an amend of the GTC leg to 2 while the 0.37 sliver still rests (the
hold counts max(old, new)), and it refuses ANY quantity amend of a fractional
order (42210000). The only working order is: cancel the sliver(s), CONFIRM
the cancel, then grow the GTC leg, then re-place the sliver the position
still needs. The GTC leg is never cancelled on this path.

A leg is classed by its quantity: a whole quantity is a durable GTC leg; a
sub-share quantity is a DAY sliver (the broker allows nothing else). Several
slivers summing to F are left alone; merging them would buy a window for
nothing. A gap BELOW F with free room is topped up with one more DAY leg.

Every failure path ends covered or loudly not: act, retry once, then protect;
the one branch that cannot prove coverage (`unknown`) says when the next
re-read happens, which is the existing pass cadence.
"""

from __future__ import annotations

import logging

from src.execution.broker_parts.cancel_confirm import _confirm_cancels_status
from src.execution.broker_parts.stop_window import UnprotectedWindow
from src.execution.order_gates import _FRACTIONAL_QTY_EPSILON, _split_protective_qty
from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger("src.execution.broker")

PATH = "stop_quantity_invariant"
#: What settles an `unknown` or `cancel_pending` outcome: the next intra
#: sweep re-reads the book (the existing cadence; no number of its own).
NEXT_REREAD = "the next intra sweep re-reads the book"
#: `amend_status` when shares are recorded as UNPROTECTED after the desk
#: acted, retried and still could not re-protect them.
EXPOSED = "exposed"
#: `amend_status` when a sliver cancel is still pending: nothing amended,
#: nothing resubmitted, coverage as it was until the cancel lands.
CANCEL_PENDING = "cancel_pending"


def _qty(spec: dict) -> float:
    try:
        return abs(float(spec.get("qty") or 0))
    except (TypeError, ValueError):
        return 0.0


def _r(value: float) -> float:
    """Quantity at the broker's own sub-share resolution (no new epsilon)."""
    return sum(_split_protective_qty(value))


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= _FRACTIONAL_QTY_EPSILON


def book_shape(specs: list[dict], held: float) -> dict:
    """H, W, F and the two leg classes of a resting book."""
    whole, frac = _split_protective_qty(held)
    gtc = [s for s in specs if _split_protective_qty(_qty(s))[1] == 0.0]
    day = [s for s in specs if s not in gtc]
    return {
        "held": abs(float(held)),
        "whole": whole,
        "frac": frac,
        "gtc": gtc,
        "day": day,
        "g": _r(sum(map(_qty, gtc))),
        "d": _r(sum(map(_qty, day))),
    }


def specs_after_amend(legs: list[dict]) -> list[dict]:
    """The resting book as the price amend left it, one spec per live leg."""
    return [
        {
            "id": str(leg["new_id"]),
            "qty": float(leg.get("new_qty") or leg["qty"]),
            "stop_price": leg["new_stop"],
            "limit_price": leg.get("new_limit"),
        }
        for leg in legs
        if leg.get("outcome") == "amended" and leg.get("new_id")
    ]


def enforce_stop_quantity_invariant(placer, symbol: str, amended, *, side: str, fresh: list):
    """Entry from `replace_stop_loss`: quantity work runs only on a book whose
    every leg is confirmed at one level; anything else is returned untouched
    for the next pass."""
    if not isinstance(amended, dict) or amended.get("amend_status") != "accepted" or not fresh:
        return amended
    legs = amended.get("legs") or []
    specs = specs_after_amend(legs)
    if not specs or len(specs) != len(legs):
        return amended
    return _Invariant(placer, symbol, specs, abs(float(fresh[0].qty)), side, amended).run()


class _Invariant:
    """One pass of the invariant over one symbol's freshly amended book."""

    def __init__(self, placer, symbol, specs, held, side, base):
        self.p, self.symbol, self.specs, self.held = placer, symbol, list(specs), held
        self.side, self.base, self.price = side, base, float(specs[0]["stop_price"])
        self.work: list[dict] = []
        self.window: UnprotectedWindow | None = None

    # ----------------------------------------------------------- the steps
    def run(self) -> dict:
        shape = self.shape()
        if _same(shape["g"], shape["whole"]) and _same(shape["d"], shape["frac"]):
            return self.result("accepted")
        halted = self.fit_gtc(shape)
        if halted is None:
            halted = self.fit_slivers(self.shape())
        return self.result("accepted") if halted is None else halted

    def fit_gtc(self, shape: dict) -> dict | None:
        """Step 2/3: bring the GTC sum to W. Returns a payload only on a halt."""
        need = _r(shape["whole"] - shape["g"]) if shape["whole"] >= shape["g"] else -_r(shape["g"] - shape["whole"])
        if _same(need, 0):
            return None
        if need < 0:
            return self.shrink(shape, -need)
        free = _r(shape["held"] - shape["g"] - shape["d"]) if shape["held"] >= shape["g"] + shape["d"] else 0.0
        if free + _FRACTIONAL_QTY_EPSILON >= need:
            outcome = self.grow(shape, need)
            return None if outcome == "amended" else self.halt(outcome, "grow")
        return self.grow_without_room(shape, need)

    def shrink(self, shape: dict, excess: float) -> dict | None:
        """Amend the largest GTC leg down in place; a leg that is surplus in
        full is cancelled (confirmed) before the next is considered."""
        for spec in sorted(shape["gtc"], key=_qty, reverse=True):
            if _qty(spec) > excess + _FRACTIONAL_QTY_EPSILON:
                outcome = self.amend_qty(spec, _qty(spec) - excess)
                return None if outcome == "amended" else self.halt(outcome, "shrink")
            left = _r(shape["g"] - _qty(spec) + shape["d"])
            status, cancelled = self.cancel_legs([spec], max(0.0, shape["held"] - left), "GTC")
            if status != "confirmed":
                return self.after_cancel_failure(status, cancelled)
            shape = self.shape()
            excess = _r(excess - _qty(spec))
            if _same(excess, 0):
                return None
        return None

    def grow(self, shape: dict, need: float) -> str:
        """Grow the largest GTC leg by `need` whole shares, or place one."""
        if shape["gtc"]:
            spec = max(shape["gtc"], key=_qty)
            return self.amend_qty(spec, _qty(spec) + need)
        return "amended" if self.submit(float(int(round(need))), "GTC") else "refused"

    def grow_without_room(self, shape: dict, need: float) -> dict | None:
        """Step 3: the slivers hold the room the GTC leg needs. Cancel them
        FIRST, confirm, then grow; the window carries the TRUE exposure, which
        is everything not under the GTC leg (a whole share in 2.0 held / GTC 1
        + DAY 0.37), not just the cancelled sliver."""
        status, cancelled = self.cancel_legs(shape["day"], shape["held"] - shape["g"], "DAY")
        if status != "confirmed":
            return self.after_cancel_failure(status, cancelled)
        outcome = self.grow(self.shape(), need)
        if outcome == "amended":
            return None
        if outcome == "unknown":
            # The amend MAY have landed: restoring a sliver could push the
            # book over H. Nothing more is cancelled, nothing restored.
            self.close_window("unknown")
            self.alert(
                f"{self.symbol}: the GTC stop leg's quantity amend got NO broker answer after "
                f"{len(cancelled)} sliver(s) were cancelled; nothing restored (a restore could "
                f"exceed the position if the amend landed); {NEXT_REREAD}"
            )
            return self.result("unknown", next_reread=NEXT_REREAD)
        return self.restore_or_expose(cancelled, "refused", "restored")

    def fit_slivers(self, shape: dict) -> dict | None:
        """Step 4: bring the DAY sum to F. Below F with room: top up, no cancel."""
        frac, d = shape["frac"], shape["d"]
        if _same(d, frac):
            return None
        if d < frac:  # free room is F - d by construction once g == W
            return None if self.submit(_r(frac - d), "DAY") else self.result("sliver_refused")
        status, cancelled = self.cancel_legs(shape["day"], shape["held"] - shape["g"], "DAY")
        if status != "confirmed":
            return self.after_cancel_failure(status, cancelled)
        if frac > 0 and not self.submit(_r(frac), "DAY"):
            return self.restore_or_expose(cancelled, "sliver_refused", "restored")
        return None

    # ------------------------------------------------------- broker calls
    def shape(self) -> dict:
        return book_shape(self.specs, self.held)

    def note(self, step: str, **facts) -> None:
        self.work.append({"step": step, **facts})

    def amend_qty(self, spec: dict, new_qty: float) -> str:
        qty = int(round(new_qty))
        leg = self.p._amend_one_stop_price(symbol=self.symbol, spec=spec, new_price=self.price, new_qty=qty)
        self.note(
            "amend_qty",
            order=spec["id"],
            old_qty=spec["qty"],
            new_qty=qty,
            outcome=leg["outcome"],
            new_id=leg.get("new_id"),
            detail=leg.get("detail"),
        )
        if leg["outcome"] == "amended":
            self.specs = [{**s, "id": str(leg["new_id"]), "qty": float(qty)} if s is spec else s for s in self.specs]
        return leg["outcome"]

    def submit(self, qty: float, kind: str) -> bool:
        """Place one closing leg of `qty`; tif follows the quantity (a sub-share
        is DAY). A kill-switch block comes back without an id and counts as
        refused, exactly like a broker refusal."""
        where = f"{PATH}.submit_{kind.lower()}"
        try:
            order = self.p._submit_stop_limit_order(symbol=self.symbol, qty=qty, stop_price=self.price, side=self.side)
            record_guarded_pass(self.p, where, context={"symbol": self.symbol, "qty": qty})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self.p, where, exc, log=logger, context={"symbol": self.symbol, "qty": qty, "effect": "leg not placed"}
            )
            self.note("submit", kind=kind, qty=qty, outcome="refused", detail=str(exc))
            return False
        oid = order.get("id") if isinstance(order, dict) else None
        if not oid:
            self.note(
                "submit",
                kind=kind,
                qty=qty,
                outcome="refused",
                detail=str((order or {}).get("status") if isinstance(order, dict) else order),
            )
            return False
        self.specs.append({"id": str(oid), "qty": float(qty), "stop_price": self.price, "limit_price": None})
        self.note("submit", kind=kind, qty=qty, outcome="placed", order=str(oid))
        return True

    def cancel_legs(self, legs: list[dict], exposed: float, kind: str) -> tuple[str, list[dict]]:
        """Cancel `legs` and CONFIRM each with the existing cancel-status reader
        (confirmed / filled / unconfirmed); an unconfirmed answer is asked once
        more. The window opens on the first cancel and records `exposed`."""
        if self.window is None:
            self.window = UnprotectedWindow(
                self.symbol,
                "fractional_quantity_change",
                self.p._window_log,
                path=PATH,
                exposed_qty=_r(max(0.0, exposed)),
                leg_kind=kind,
            )
        cancelled: list[dict] = []
        for spec in legs:
            try:
                self.p.client.cancel_order_by_id(spec["id"])
                record_guarded_pass(self.p, f"{PATH}.cancel", context={"symbol": self.symbol, "order": spec["id"]})
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(
                    self.p,
                    f"{PATH}.cancel",
                    exc,
                    log=logger,
                    context={
                        "symbol": self.symbol,
                        "order": spec["id"],
                        "effect": "already-cancelled legs are restored",
                    },
                )
                self.note("cancel", order=spec["id"], qty=spec["qty"], outcome="refused")
                return "refused", cancelled
            cancelled.append(spec)
            self.window.cancelled(spec["id"])
            self.note("cancel", order=spec["id"], qty=spec["qty"], outcome="sent")
        status, detail = _confirm_cancels_status(self.p, cancelled)
        if status == "unconfirmed":
            status, detail = _confirm_cancels_status(self.p, cancelled)
        self.note("confirm_cancels", status=status, detail=detail)
        if status == "confirmed":
            self.specs = [s for s in self.specs if s not in cancelled]
        return status, cancelled

    def after_cancel_failure(self, status: str, cancelled: list[dict]) -> dict:
        if status == "refused":
            return self.restore_or_expose(cancelled, "cancel_refused", "cancel_failed_restored")
        if status == "filled":
            logger.warning(
                "%s: a protective stop of %s FILLED during its cancel; the position "
                "is exiting, nothing is amended or restored",
                PATH,
                self.symbol,
            )
            self.close_window("stop_filled")
            return self.result("filled")
        logger.error(
            "%s: %s's sliver cancel is still PENDING (%d leg(s)); the GTC leg is NOT "
            "amended and nothing is resubmitted while the hold stands; %s",
            PATH,
            self.symbol,
            len(cancelled),
            NEXT_REREAD,
        )
        self.close_window(CANCEL_PENDING)
        return self.result(CANCEL_PENDING, next_reread=NEXT_REREAD)

    def restore_or_expose(self, cancelled: list[dict], status: str, closed_as: str) -> dict:
        """Act, retry, then protect: resubmit the cancelled legs at their old
        quantity; a second refusal records the shares as EXPOSED and alerts."""
        restored, failed = 0, list(cancelled)
        if failed:
            restored, failed = self.p._restore_stop_orders(self.symbol, failed, side=self.side)
        if failed:
            again, failed = self.p._restore_stop_orders(self.symbol, failed, side=self.side)
            restored += again
        self.note("restore", restored=restored, failed=len(failed))
        if failed:
            exposed = _r(sum(map(_qty, failed)))
            self.close_window("restore_refused")
            self.alert(
                f"{self.symbol}: {exposed} share(s) are WITHOUT a protective stop — the "
                f"sliver restore was refused twice after a refused quantity change "
                f"({status}); the position is recorded as exposed; {NEXT_REREAD}"
            )
            return self.result(EXPOSED, exposed_qty=exposed)
        self.specs.extend(cancelled)
        self.close_window(closed_as)
        logger.warning(
            "%s: %s's quantity change was refused (%s); %d leg(s) restored, coverage is as it was",
            PATH,
            self.symbol,
            status,
            restored,
        )
        return self.result(status)

    def halt(self, outcome: str, step: str) -> dict:
        if outcome == "unknown":
            self.alert(
                f"{self.symbol}: the {step} of its GTC stop leg got NO broker answer; "
                f"nothing cancelled, nothing written back; {NEXT_REREAD}"
            )
            return self.result("unknown", next_reread=NEXT_REREAD)
        logger.warning(
            "%s: the broker refused the %s of %s's GTC stop leg; the resting legs "
            "are unchanged and nothing was cancelled",
            PATH,
            step,
            self.symbol,
        )
        return self.result("refused")

    def alert(self, text: str) -> None:
        logger.error(text)
        try:
            from src.notifier import send_owner_alert

            send_owner_alert(text, symbols=[self.symbol])
            record_guarded_pass(self.p, f"{PATH}.owner_alert", context={"symbol": self.symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self.p,
                f"{PATH}.owner_alert",
                exc,
                log=logger,
                context={"symbol": self.symbol, "effect": "owner not told"},
            )

    def close_window(self, outcome: str) -> None:
        if self.window is not None:
            self.window.close(outcome)
            self.window = None

    def result(self, status: str, **extra) -> dict:
        self.close_window("replaced" if status == "accepted" else status)
        shape = self.shape()
        head = shape["gtc"] or shape["day"]
        out = dict(self.base)
        out.update(
            {
                "id": head[0]["id"] if head and status == "accepted" else None,
                "amend_status": status,
                "symbol": self.symbol,
                "quantity_work": self.work,
                "book": {"held": shape["held"], "gtc": shape["g"], "day": shape["d"]},
                **extra,
            }
        )
        if status != "accepted":
            out["status"] = status
        return out


def _held_on(placer, symbol: str, side: str) -> float:
    """Shares a `side` protective stop must cover now: the long for 'sell',
    the short for 'buy'; 0 when none. Raises when the read fails."""
    want = symbol.strip().upper().replace("/", "")
    signed = sum(float(p.qty) for p in placer.get_positions() if str(p.symbol).strip().upper().replace("/", "") == want)
    held = signed if side == "sell" else -signed
    return _r(held) if _r(held) > 0 else 0.0


def cover_to_held(placer, symbol: str, specs: list[dict], *, side: str = "sell") -> bool:
    """Put protective cover back UP TO what is held now, never above it.

    Fresh held read; the cancelled legs are restored in order, the last cut
    to fit, and any shortfall (an entry filled mid-cancel) is added to the
    largest leg at its level. Restored with idempotency on, so a stop still
    dying (pending_cancel, or a cancelled id not yet read dead) is never
    counted as cover and never placed over. A held read that fails restores
    the legs as they were. False, with an ERROR line the health report's
    `stop_missing_or_failed` check reads, when cover is short of held."""
    kwargs = {} if side == "sell" else {"side": side}
    try:
        held = _held_on(placer, symbol, side)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(placer, "cover_to_held.held_read", exc, log=logger, context={"symbol": symbol})
        held = None
    if held == 0:
        return True
    sized: list[dict] = []
    room = held
    for spec in specs or []:
        take = _qty(spec) if room is None else min(_qty(spec), room)
        if take > 0:
            sized.append(spec if take == _qty(spec) else {**spec, "qty": take})
            room = None if room is None else _r(room - take)
    if room and sized:
        big = max(range(len(sized)), key=lambda i: _qty(sized[i]))
        sized[big] = {**sized[big], "qty": _r(_qty(sized[big]) + room)}
    outcome = placer._restore_stop_orders(symbol, sized, check_idempotency=True, **kwargs)
    failed = list(outcome[1]) if isinstance(outcome, tuple) else []
    if failed:
        cover = _r(sum(_qty(x) for x in sized) - sum(_qty(x) for x in failed))
        logger.error("cover_to_held: protective stop put-back FAILED for %s — cover %s of held %s", symbol, cover, held)
        return False
    return True
