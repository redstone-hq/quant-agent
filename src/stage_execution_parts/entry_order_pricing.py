"""The entry loop's pricing phase: what the order is sent as, and what the risk budget divides by.

Two order types, one switch (src/stage_execution_parts/entry_order_type.py):

* MARKET (the deployed default, owner ruling 2026-10-09): a plain DAY market
  order, no limit. Both divisors use the ARRIVAL PRICE: the latest real
  trade print for the name, and it must be from THIS regular session (stamped
  at or after today's open on the exchange calendar, src/session_calendar.py).
  No such print refuses the name with `no_arrival_print`. The RISK divisor
  is the arrival print, for a BUY and a SHORT alike. The ALLOCATION divisor
  is raised to the arrival print on a BUY (never fewer shares than the
  conservative print/entry bound allows) and left as it is for a SHORT.
  The trade row records the arrival print as its price; the broker's own
  average fill lands in `fill_price` at reconciliation, so slippage reads
  as fill against arrival (the implementation-shortfall benchmark).

  Why not the quote. This account's quotes are IEX only, one venue, while
  the broker fills against the all-exchange book. The IEX ask has run 4-5%
  above every real trade near the open (XOM 2026-10-12: trading near $172.44, ask
  $179.70; 2026-10-14: near $175.35, ask $184.13), which sized those buys
  at about half their allotted risk and wrote a false price on the trade row.
  entry_quote.py already documents the IEX top of book as untrustworthy.
  The bid / ask is still read and recorded on the order event, as data only.

  Why the 2026-10-04 overshoot does not apply. That fix measured positions
  carrying more risk than allotted (6.1% median, 25% worst) because the
  budget was divided by the morning ANALYSIS price while a limit order was
  priced 40bp away from it. The arrival print is a same-session trade taken
  seconds before a market order is sent, not an earlier analysis price, and
  the market order has no ceiling to be priced away to; any gap left is the
  real fill-vs-arrival slippage, which is now recorded truthfully.
* LIMIT (reachable only when `execution.entry_order_type: limit`): the
  marketable limit at the slippage ceiling / floor, priced by
  `entry_limit_from_quote`; the risk divisor is the limit itself, exactly as
  the 2026-10-04 fix left it.
"""

from __future__ import annotations

import math
from datetime import datetime

from src.pipeline_stages import _record_execution_skip, _sizing_read
from src.refusal_errors import SizingPriceUnavailable
from src.session_calendar import SessionCalendarUnavailable, session_for
from src.stage_execution_parts.entry_order_type import entry_orders_are_market
from src.stage_execution_parts.entry_quote import entry_limit_from_quote
from src.stage_execution_parts.state import SKIP
from src.trading_calendar import ET, et_now

NO_PRICE = "no_price"
NO_ARRIVAL_PRINT = "no_arrival_print"


def quote_side_paid(is_short: bool, bid, ask) -> float | None:
    """The live quote side a market order crosses: ask for a BUY, bid for a SHORT; None when unusable.

    Read by the rotation projection; the entry loop no longer sizes off it.
    """
    side = bid if is_short else ask
    if isinstance(side, bool) or not isinstance(side, (int, float)):
        return None
    value = float(side)
    return value if math.isfinite(value) and value > 0 else None


def no_price_detail(action: str, is_short: bool) -> str:
    side = "bid" if is_short else "ask"
    return (
        f"no live {side} at submit; a market {action} is sized against the "
        f"price it pays, never the print or the analyst entry"
    )


def _session_open(broker) -> datetime | str:
    """Today's regular-session open, or a plain-words reason there is none."""
    try:
        session = session_for(broker, et_now().date())
    except SessionCalendarUnavailable as exc:
        return f"the exchange calendar could not be read ({exc})"
    if not session.is_trading_day:
        return "today is not a trading day on the exchange calendar"
    if not isinstance(session.open_at, datetime) or session.open_at.tzinfo is None:
        return "the exchange calendar gave no usable open time for today"
    return session.open_at


def arrival_print(pipeline, symbol: str, fallback: float | None) -> float | str:
    """The arrival price for a market entry, or a plain-words reason to refuse.

    The latest real trade print, through the same stamped read the sizing
    print already uses (`get_latest_price_stamped`), accepted only when it
    is stamped at or after today's regular-session open. A pre-market or
    prior-session print is refused, as is a quote mid or a single side.

    Test compatibility, the same rule `_today_sizing_price` applies: a broker
    whose stamped getter is absent or does not return a real `LivePrice`
    (a MagicMock, whose `source` is not a string) gets `fallback`, the today
    print already sized off.
    """
    broker = getattr(pipeline, "broker", None)
    getter = getattr(broker, "get_latest_price_stamped", None)
    if not callable(getter):
        return fallback if fallback else "no trade-print reader on the broker"
    try:
        live = _sizing_read(pipeline, getter, symbol, symbol, "arrival")
    except SizingPriceUnavailable as exc:
        return f"the latest trade print could not be read ({exc})"
    if live is not None and not isinstance(getattr(live, "source", None), str):
        return fallback if fallback else "no trade print came back"
    if live is None or live.source != "last_trade" or not (live.price and live.price > 0):
        return "no trade print for this name, only a quote or nothing"
    trade_at = live.trade_at
    if not isinstance(trade_at, datetime) or trade_at.tzinfo is None:
        return "the latest trade print carries no usable time stamp"
    opened = _session_open(broker)
    if isinstance(opened, str):
        return opened
    if trade_at < opened:
        return (
            f"the latest trade print (${live.price:.2f} at "
            f"{trade_at.astimezone(ET):%Y-%m-%d %H:%M} ET) is from before "
            f"today's open ({opened.astimezone(ET):%H:%M} ET)"
        )
    return float(live.price)


def price_entry(run, leg, limit_price, sizing_price, risk_sizing_price):
    """Return `(limit_price, sizing_price, risk_sizing_price)`, or `SKIP` where the name is refused."""
    if entry_orders_are_market(run.pipeline):
        symbol = leg.decision.symbol
        arrival = arrival_print(run.pipeline, symbol, risk_sizing_price)
        if isinstance(arrival, str):
            _record_execution_skip(
                run.pipeline,
                run.ctx,
                symbol,
                NO_ARRIVAL_PRINT,
                (
                    f"{arrival}; a market {leg.decision.action} is sized against "
                    "a trade print from this session, never the IEX quote, the "
                    "analyst entry or an older print — refused"
                ),
            )
            return SKIP
        if not leg.is_short:
            sizing_price = max(sizing_price or 0.0, arrival)
        return None, sizing_price, arrival
    priced = entry_limit_from_quote(run, leg, limit_price, sizing_price)
    if priced is SKIP:
        return SKIP
    limit_price, sizing_price = priced
    if isinstance(limit_price, (int, float)) and limit_price > 0:
        risk_sizing_price = float(limit_price)
    return limit_price, sizing_price, risk_sizing_price
