"""Coverage election: the row filed for a position whose protective stop has fired but not filled, with the through-stop and notional helpers it reads.

Lifted verbatim out of `ProtectionMixin` (src/pipeline_protection.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
import math

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _price_is_through_stop(price: float, stop_price: float, *, is_short: bool) -> bool:
    """Has the tape passed a protective stop's trigger?

    A long's protective stop is a SELL stop and fires as price FALLS
    through it; a short's is a BUY stop and fires as price RISES through
    it. Same arithmetic, mirrored.

    NO TOLERANCE, no grace band, no minimum distance and no percentage
    lives here, by design: this is a comparison of two numbers the desk
    already holds every session, which is the whole reason this detector
    could be built without inventing a constant. The inequality is STRICT,
    so "price exactly at the trigger" is deliberately NOT through it — the
    only float-equality case is resolved towards silence rather than
    towards an epsilon nobody chose.

    Returns False on any unusable number rather than guessing.
    """
    try:
        px = float(price)
        stop = float(stop_price)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(px) and math.isfinite(stop)):
        return False
    if px <= 0 or stop <= 0:
        return False
    return (px > stop) if is_short else (px < stop)


def _position_notional(position, qty: float) -> float:
    """Dollar value of `qty` shares of `position`, or 0.0 if unknowable.

    Spec §11.1 hybrid fractional stops, observability half. The owner's
    standing objection to invisible risk is that "a number he can look at
    beats a guarantee he has to trust" — so the overnight sub-share
    exposure is reported in DOLLARS, not in shares. A share count is
    meaningless across a book that holds both a $12 name and a $900 one,
    and the whole reason fractional sizing exists here is the $900 one.

    Uses the price already on the broker's position snapshot rather than
    a fresh quote: this runs inside the coverage sweep's per-position
    loop, and an extra round-trip per held name to decorate an alert
    would be paid on every sweep of every session. Returns 0.0 rather
    than guessing when the snapshot carries no usable price — an omitted
    number is honest, an invented one is not.
    """
    try:
        price = float(getattr(position, "current_price", 0) or 0)
        shares = float(qty)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(price) and price > 0):
        return 0.0
    if not (math.isfinite(shares) and shares > 0):
        return 0.0
    return round(price * shares, 2)


class CoverageElection:
    """Coverage election: the row filed for a position whose protective stop has fired but not filled, with the through-stop and notional helpers it reads."""

    def __init__(self) -> None:
        pass

    def _elected_unfilled_stop_row(
        self,
        position,
        specs,
        *,
        is_short: bool,
    ) -> dict | None:
        """One row per position whose protective stop has FIRED and has not
        FILLED, or None when nothing is in that state. Never raises.

        `specs` is what `snapshot_protective_stops` just returned: open
        protective stop orders at the broker. "Open" is the load-bearing
        word — an order the broker has filled is no longer in that list, so
        a stop order that is still listed has not filled. Comparing the
        live price against its trigger therefore answers the whole question:
        price through the trigger + order still open = elected and unfilled.

        This is the gap-past-the-limit state a stop-LIMIT can sit in, which
        nothing could previously see. Protective stops are now stop-MARKET
        and fill when elected (the stop-limit fallback was deleted
        2026-10-13), so this only fires for a stop-limit still resting from
        before then — kept as its backstop.

        DETECTS ONLY. Nothing here sells, cancels, replaces or re-prices
        anything — an exit decision on an unfilled stop is an owner-level
        change and is not made here.
        """
        try:
            symbol = str(getattr(position, "symbol", "") or "").strip().upper()
            price = float(getattr(position, "current_price", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not symbol or not (math.isfinite(price) and price > 0):
            return None
        through: list[dict] = []
        for spec in specs or []:
            try:
                stop_price = float(spec.get("stop_price", 0) or 0)
                stop_qty = float(spec.get("qty", 0) or 0)
            except (TypeError, ValueError):
                continue
            if stop_qty <= 0:
                continue
            if _price_is_through_stop(price, stop_price, is_short=is_short):
                through.append({"stop_price": stop_price, "qty": stop_qty})
        if not through:
            return None
        # The trigger the tape is FURTHEST past: for a long that is the
        # highest elected stop, for a short the lowest. Derived from the
        # orders themselves, not chosen.
        worst = (min if is_short else max)(
            through,
            key=lambda r: r["stop_price"],
        )
        stop_price = float(worst["stop_price"])
        distance = (price - stop_price) if is_short else (stop_price - price)
        stranded = sum(float(r["qty"]) for r in through)
        logger.critical(
            "PROTECTIVE STOP ELECTED AND UNFILLED: %s %s at $%.2f is $%.2f "
            "through its $%.2f protective stop, whose order is still OPEN at "
            "the broker over %.4f share(s) — the stop fired and did not "
            "fill, so the coverage sweep counts those shares as protected "
            "while nothing is standing watch. Detected only; nothing was "
            "sold, cancelled or replaced.",
            "short" if is_short else "long",
            symbol,
            price,
            distance,
            stop_price,
            stranded,
        )
        return {
            "symbol": symbol,
            "held_qty": float(getattr(position, "qty", 0) or 0),
            "price": price,
            "stop": stop_price,
            "through": distance,
            "stranded_qty": stranded,
            "is_short": is_short,
            "unprotected_value": _position_notional(position, stranded),
            # Rendered by the shared coverage bullet as the trailing plain
            # sentence. Says the two numbers and nothing else.
            "note": (
                f"the protective order at ${stop_price:,.2f} fired and did "
                f"not fill \u2014 price ${price:,.2f} is ${distance:,.2f} past it, "
                "so those shares have nothing standing watch over them"
            ),
        }
