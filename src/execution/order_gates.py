"""Order-boundary gates that need no judgement call — the last deterministic
checks before a request object reaches the broker client. Kept OUT of
`src/execution/broker.py` so each can be built and tested alone.

`check_order_quantity` is the QUANTITY gate (2026-10-01 audit: the stop
PRICE was guarded at the boundary, the quantity was not — −5, 0, NaN and
1e9 all reached the client). It is a pure function; `AlpacaBroker`
gathers its live inputs and records a refusal as `rejected_bad_qty`,
the same shape as `rejected_bad_stop`.

No bound here is invented. Each cites its source:
  * finite / positive — inequalities, no number;
  * decimal places — 9: `src/pipeline_sizing.py` clamps the configured
    `fractional_share_decimals` to <= 9, the same grid
    `_FRACTIONAL_QTY_EPSILON` (1e-9) and `_split_protective_qty` use;
  * whole-share-only names — the broker's own `fractionable` flag
    (`AlpacaBroker.get_fractionability`);
  * notional ceiling — `RiskConfig.max_position_pct` (config/settings.yaml),
    the cap the risk stage already applies to the SUGGESTED allocation,
    re-applied here to the FINAL quantity x price;
  * sell ceiling — the quantity the broker itself reports held.
"""

import logging
import math
from src.sentinel.guarded import record_guarded_pass
from src.quantities import ORDER_QTY_DECIMALS
from decimal import Decimal

from alpaca.trading.enums import TimeInForce


# Spec §11.1 HYBRID FRACTIONAL STOPS. Measured 2026-09-01 against the live
# paper account — treat as broker capability, not account state:
#
#   * a fractional-quantity order MUST be time_in_force=DAY. A fractional
#     GTC order is refused outright: "fractional orders must be DAY orders"
#     (code 42210000).
#   * a fractional order must be market, limit, stop or stop_limit. A
#     fractional TRAILING stop is refused at EVERY tif.
#   * ACCEPTED fractional: STOP/DAY, STOP_LIMIT/DAY, LIMIT/DAY.
#   * whole-share GTC stops are unaffected (control probe accepted).
#
# So a position of N.f shares cannot be covered by one durable order. It is
# covered by TWO: a GTC stop over floor(N.f) — which survives the close —
# and a DAY stop over the sub-share remainder, which lapses at 16:00 ET by
# design and is re-placed at the start of the next session. The remainder is
# a deliberate overnight exposure the owner accepted in exchange for being
# able to hold expensive names at all on a ~$10k account. NOT bounded under
# one share — a position that is itself sub-one-share lapses in full (see
# config/settings.yaml), and "the next session" only exists while the desk
# is running: `src/coverage_watchdog.py` is what says so when it is not.
#
# `_derive_stop_tif` is where that rule is MECHANICALLY enforced: every stop
# this class submits goes through `_submit_stop_limit_order`, and the tif is
# derived from the quantity there rather than chosen by each caller. A path
# that forgets the rule cannot exist, because no path gets to state it.
_FRACTIONAL_QTY_EPSILON = 1e-9

# `ORDER_QTY_DECIMALS` (imported above) is the most decimal places an order
# quantity may carry: the same grid as `_FRACTIONAL_QTY_EPSILON` and the `<= 9`
# clamp in `src/pipeline_sizing.py`. It is defined in `src/quantities.py`, below
# the broker seam, so the lot walk's readers share it without importing
# src.execution. Shared by the gate below and by `src/execution/sell_quantity.py`,
# which floors every sell onto this grid before the gate sees it.


def _split_protective_qty(qty) -> tuple[float, float]:
    """Split a protective-stop quantity into (whole_shares, sub_share_remainder).

    The whole part is what a durable GTC stop can cover; the remainder is what
    only a DAY stop can. Both are returned as non-negative magnitudes — a
    short's signed qty is normalised by its callers long before this.

    The remainder is rounded to 9dp before the epsilon test so that float
    representation error (10.5 - 10.0 landing at 0.5000000000000007, or a qty
    of 7.000000000000001 arriving from a fill) cannot mint a phantom
    sub-share leg for a position that is really whole.
    """
    try:
        value = abs(float(qty))
    except (TypeError, ValueError):
        return 0.0, 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0, 0.0
    whole = float(math.floor(value))
    frac = round(value - whole, 9)
    if frac <= _FRACTIONAL_QTY_EPSILON:
        return whole, 0.0
    if frac >= 1.0:  # only reachable via the round() above on a near-integer
        return whole + 1.0, 0.0
    return whole, frac


logger = logging.getLogger("src.execution.broker")


def _derive_stop_tif(qty) -> TimeInForce:
    """The ONLY place a protective stop's time_in_force is decided.

    Whole share count → GTC, the durable order that survives 16:00 ET and is
    what every pre-fractional path already got. Fractional → DAY, because the
    broker refuses any other tif for a fractional quantity (see the block
    comment above). This is derived from the quantity rather than passed in
    by the caller on purpose: a caller that could ask for a fractional GTC
    would just be asking for a rejection, and the one thing this desk cannot
    afford is a protective order that was refused while the code believed it
    was placed.
    """
    _whole, frac = _split_protective_qty(qty)
    return TimeInForce.DAY if frac > 0 else TimeInForce.GTC


QTY_REJECTED = "rejected_bad_qty"


class BadOrderQuantity(ValueError):
    """Raised by the stop paths, whose contract is "placed or raised".
    `.detail` is the same plain-words sentence `submit_order` records."""

    status = QTY_REJECTED

    def __init__(self, detail: str):
        super().__init__(f"{QTY_REJECTED}: {detail}")
        self.detail = detail


def check_order_quantity(
    qty,
    *,
    side: str,
    fractionable: bool | None = None,
    price: float | None = None,
    equity: float | None = None,
    max_position_pct: float | None = None,
    held_qty: float | None = None,
) -> str | None:
    """None when `qty` may be submitted, else the plain-words refusal.

    An input the caller does not have is passed as None and that check is
    skipped — EXCEPT equity: once a cap is configured, an unreadable
    equity refuses an ENTRY (the final authority fails closed). A sell
    never meets the cap: it reduces exposure.
    """
    try:
        value = float(qty)
    except (TypeError, ValueError):
        return f"the quantity {qty!r} is not a number"
    if not math.isfinite(value):
        return f"the quantity {qty!r} is not a finite number"
    if value <= 0:
        return f"the quantity {value:g} is not positive"
    # Decimals as the SDK will SEND them (str of the float). 9 is the
    # desk's own ceiling: `src/pipeline_sizing.py` clamps
    # `execution.fractional_share_decimals` to <= 9, the grid
    # `_FRACTIONAL_QTY_EPSILON` (1e-9) and `_split_protective_qty` use.
    if -Decimal(repr(value)).as_tuple().exponent > ORDER_QTY_DECIMALS:
        return f"the quantity {value!r} carries more than the {ORDER_QTY_DECIMALS} decimal places an order can"
    s = side.lower()
    if fractionable is False and _split_protective_qty(value)[1] > 0:
        return f"the quantity {value:g} is fractional but this name trades in whole shares only"
    if s in ("buy", "sell_short") and max_position_pct is not None:
        if equity is None or not math.isfinite(equity) or equity <= 0:
            return "the account equity could not be read, so the per-position cap cannot be checked"
        if price is not None and math.isfinite(price) and price > 0:
            notional, cap = value * price, equity * max_position_pct / 100.0
            if notional > cap:
                return (
                    f"{value:g} shares at ${price:,.2f} is "
                    f"${notional:,.0f}, over the {max_position_pct:g}% "
                    f"per-position cap (${cap:,.0f} of "
                    f"${equity:,.0f} equity)"
                )
    if s == "sell" and held_qty is not None:
        if value > held_qty + _FRACTIONAL_QTY_EPSILON:
            return f"selling {value:g} shares but only {held_qty:g} are held"
    return None


def quantity_refusal_live(
    symbol: str,
    alpaca_symbol: str,
    qty,
    side: str,
    *,
    price: float | None,
    client,
    get_fractionability,
    get_account,
    max_position_pct,
) -> str | None:
    """Live inputs for the pure quantity gate, each read only when
    that check applies. Equity: a failed read refuses an entry (gate
    fails closed). Held qty: a failed read is logged and the sell
    goes on — an exit must not be blocked by a read outage."""
    s, fractionable, equity, held = side.lower(), None, None, None
    if s in ("buy", "sell_short"):
        if _split_protective_qty(qty)[1] > 0 and get_fractionability is not None:
            fractionable = get_fractionability(symbol)["fractionable"]
        if max_position_pct is not None:
            try:
                equity = get_account()["portfolio_value"]
                record_guarded_pass(client, "order_gates.equity_read", context={"symbol": symbol})
            except Exception as exc:  # noqa: BLE001
                record_guarded_pass(client, "order_gates.equity_read", exc, context={"symbol": symbol})
                logger.error("Quantity gate: equity read failed for %s: %s", symbol, exc)
    elif s == "sell":
        try:
            held = 0.0
            for pos in client.get_all_positions() or []:
                if str(pos.symbol).upper() == alpaca_symbol.upper():
                    held = max(0.0, float(pos.qty))
            record_guarded_pass(client, "order_gates.positions_read", context={"symbol": symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(client, "order_gates.positions_read", exc, context={"symbol": symbol})
            held = None
            logger.error("Quantity gate: positions read failed for %s: %s — sell proceeds unchecked.", symbol, exc)
    return check_order_quantity(
        qty,
        side=side,
        fractionable=fractionable,
        price=price,
        equity=equity,
        max_position_pct=max_position_pct,
        held_qty=held,
    )


_PLAIN_PRICE_LABELS = {
    "limit_price": "limit price",
    "stop_loss_price": "stop",
    "take_profit_price": "target price",
}


def _outlier_refusal_detail(
    label: str,
    candidate: float,
    reference_price: float,
    *,
    symbol: str,
    atr: float | None,
) -> str:
    """The owner-facing sentence for a fat-finger refusal.

    A bare deviation percentage is not something a trader can judge: 24% is
    absurd on a utility and an ordinary couple of sessions on a $7 name. So
    the sentence puts the stock's OWN normal daily range next to it.

    `atr` is the desk's already-measured ATR(14) for this symbol, handed
    down by the caller (`src/pipeline_stages.py` reads it off the same
    analysis the constructor sized from). Nothing is fetched and nothing is
    estimated here: if the caller has no ATR, the range clause is simply
    omitted rather than filled with an invented number.

    Plain words only — no field names, no jargon, no "ATR". The owner is
    not a developer and reads these in a Telegram alert.
    """
    deviation_pct = abs(candidate - reference_price) / reference_price * 100
    sentence = (
        f"{_PLAIN_PRICE_LABELS.get(label, label)} "
        f"${candidate:,.2f} is {deviation_pct:.0f}% "
        f"from price ${reference_price:,.2f}"
    )
    if atr is not None and math.isfinite(atr) and atr > 0:
        atr_pct = atr / reference_price * 100
        sentence += f" — {symbol} normally moves about ${atr:,.2f} ({atr_pct:.0f}%) in a day"
    return sentence
