"""THE write-back and reconcile for the desk's recorded protective stop.

`trades.stop_loss` on the opening row (BUY or SHORT) is the archive's
record of the live protective level. Until this module existed it was
written once at entry and never again, so a trail, an ex-div shift, a
scale-in rearm or an out-of-band broker edit left the archive days stale.
That manufactured the false "traded through its own stop" filings on Visa
and Disney (WORK.md items 35 and 69).

Two jobs, both fail-closed on missing data, neither invents a price:

1. Write the level the broker just accepted back onto the opening row.
   Replacements funnel through `replace_stop_and_record` (which is
   `AlpacaBroker.replace_stop_loss` plus this write-back). Coverage repair
   writes the level it actually placed. Other price-changing paths
   (ex-div shift, scale-in rearm, residual reprotect) call
   `write_back_stop_loss` after the broker accepts.

2. Reconcile that recorded level against the broker's live stop. When
   the live protective order exists, write THAT price back onto the
   opening row (COP/EQNR: archive lagged the desk's own stop). That is
   not an invented level. Remaining mismatches still page — never mute
   an alert without fixing the record.

`initial_stop_loss` on the same opening row is the entry bet, frozen on
the first write-back (and set at insert). R-multiple and the Type A
breakeven ratchet still read THAT number, never the live one.
"""

from __future__ import annotations


from src.sentinel.guarded import NO_LEDGER, record_guarded_pass
import logging
import math
from typing import Any, Callable

from src.execution.broker_parts.stop_window import record_unprotected_windows
from src.execution.pending_stop_amends import record_deferred_amend

logger = logging.getLogger(__name__)

# re-export mirror: defined there, still importable from here
from src.stop_price_classification import (
    STOP_ABSENT,
    STOP_UNUSABLE,
    STOP_USABLE,
    classify_stop_price,
)
from src.execution.stop_level_report import StopLevelMismatch, report_stop_level_mismatches

# ---------------------------------------------------------------------------
# THE one place a stop VALUE is judged usable (docs/WORK.md item 88).
# ---------------------------------------------------------------------------
# `0.0` was this codebase's sentinel for "no stop", so a garbage or
# miscomputed stop of zero REMOVED protection instead of refusing the
# trade — it failed open, in the one direction a risk path must never
# fail. NaN was the same bug in a different disguise and was closed for
# the entry lane only (PR #455).
#
# The fix is the DISTINCTION, not another threshold: "no stop was
# requested" and "a stop was requested and its value is garbage" are
# different facts with opposite correct responses. Absence is legitimate
# on exactly one path — the cash-sweep park, which passes
# `stop_loss_price=None` and says so in a comment. Garbage is never
# legitimate anywhere, and must be refused loudly rather than silently
# read as absence.
#
# No number is chosen here. Positive-and-finite is the arithmetic
# precondition for a price to be a price at all.


def usable_stop_prices(values: Any) -> list[float]:
    """Only the values that are real stop prices, in input order.

    For the callers that pick an extreme (most-protective) stop out of a
    set of recorded specs: `min`/`max` over a list holding None raises,
    and over a list holding 0.0 silently returns the sentinel.
    """
    out: list[float] = []
    for value in values or []:
        state, price = classify_stop_price(value)
        if state == STOP_USABLE:
            out.append(price)
    return out


# Alpaca's published stock ticks, the same split `_quantize_price` in
# `src/execution/broker.py` already uses: $0.01 at or above $1, $0.0001
# below. A mismatch smaller than one tick is the SDK round-trip, not a
# different stop. Not a trading threshold.
_ALPACA_TICK_AT_OR_ABOVE_DOLLAR = 0.01
_ALPACA_TICK_BELOW_DOLLAR = 0.0001


def _finite_price(value: Any) -> float:
    """The recorded level as a usable price, or 0.0 when it is not one.

    Delegates to `classify_stop_price` so "finite" means finite: the old
    `price != price` test caught NaN but passed ±Inf straight through,
    which is not a price either.
    """
    return classify_stop_price(value)[1]


def _prices_match(recorded: float, live: float) -> bool:
    """True when the two prints are the same stop at Alpaca's round-trip.

    Tick size is Alpaca's published split (`_quantize_price`): $0.01 at or
    above $1, $0.0001 below. The allowed delta is half a tick — this
    repo's own stated float<->Decimal round-trip (the reprotect path's
    half-penny), not a trading threshold. A full-tick difference is a
    different stop.
    """
    tick = _ALPACA_TICK_AT_OR_ABOVE_DOLLAR if min(recorded, live) >= 1.0 else _ALPACA_TICK_BELOW_DOLLAR
    return abs(recorded - live) <= (tick / 2.0)


def accepted_stop_order(order: Any) -> bool:
    """True only when the broker payload carries a real order id.

    Kill-switch and some reject paths return a dict with `id=None`
    without raising. Treating those as success would write a stop the
    broker does not hold.
    """
    if not isinstance(order, dict):
        return False
    oid = order.get("id")
    return bool(oid) and str(oid) not in {"None", "kill_switch_halted"}


def _holding_is_short(broker: Any, symbol: str) -> bool | None:
    """True/False from a real positions list; None when direction is unknown.

    `get_positions()` on a test double is often a MagicMock — iterating that
    is not a list of holdings. Fail closed to "unknown" rather than guess.
    """
    try:
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(broker, "stop_records.holding_is_short", exc, context={"symbol": symbol})
        return None
    record_guarded_pass(broker, "stop_records.holding_is_short", context={"symbol": symbol})
    if not isinstance(positions, list):
        return None
    for position in positions:
        if getattr(position, "symbol", None) != symbol:
            continue
        try:
            return float(getattr(position, "qty", 0) or 0) < 0
        except (TypeError, ValueError):
            return None
    return None


def recorded_initial_stop(row: dict | None) -> float:
    """The stop AT ENTRY, even after later write-backs of the live level.

    Prefers `initial_stop_loss` when that column is present on the row.
    A present-but-empty value is 0 — it must not fall back to `stop_loss`,
    because after a write-back that column is the live level, and a row
    that opened with no stop must not mint an entry bet from a later
    repair. Test doubles that omit the key entirely still fall back to
    `stop_loss` (legacy shape, entry == live). Never invents a price.
    """
    if not row:
        return 0.0
    if "initial_stop_loss" in row:
        return _finite_price(row.get("initial_stop_loss"))
    return _finite_price(row.get("stop_loss"))


def write_back_stop_loss(
    db: Any,
    symbol: str,
    stop_price: float,
    *,
    is_short: bool | None = None,
) -> bool:
    """Persist `stop_price` onto the symbol's latest BUY/SHORT row.

    `is_short=True` updates the SHORT row; `False` the BUY row. Omit only
    when the caller cannot tell — then the latest of either is used.
    Never raises: a failed archive write must not unwind a broker stop
    that already landed. Returns True only when the DB method reports it
    wrote.
    """
    price = _finite_price(stop_price)
    if price <= 0 or db is None:
        return False
    updater = getattr(db, "update_open_stop_loss", None)
    if not callable(updater):
        logger.error(
            "stop write-back: %s has no update_open_stop_loss — live stop $%.4f was NOT recorded",
            symbol,
            price,
        )
        return False
    kwargs: dict[str, Any] = {}
    if is_short is True:
        kwargs["action"] = "SHORT"
    elif is_short is False:
        kwargs["action"] = "BUY"
    try:
        return bool(updater(symbol, price, **kwargs))
    except TypeError:
        # A test double / older signature that only takes (symbol, price).
        try:
            return bool(updater(symbol, price))
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(db, "stop_records.write_back_stop_loss", exc)
            logger.error(
                "stop write-back FAILED for %s @ $%.4f: %s — broker holds "
                "the live level; the archive is stale until the next "
                "successful write-back or reconcile report",
                symbol,
                price,
                exc,
            )
            return False
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(db, "stop_records.write_back_stop_loss", exc)
        logger.error(
            "stop write-back FAILED for %s @ $%.4f: %s — broker holds the "
            "live level; the archive is stale until the next successful "
            "write-back or reconcile report",
            symbol,
            price,
            exc,
        )
        return False


def replace_stop_and_record(
    broker: Any,
    db: Any,
    symbol: str,
    new_stop_price: float,
    run_id: str | None = None,
    caller: str = "replace_stop_and_record",
    **kwargs: Any,
) -> dict | None:
    """The replacement funnel: broker replace, then archive write-back.

    Callers go through here so a successful replace cannot silently leave
    `trades.stop_loss` on the entry level. A failed replace writes no level.
    """
    order = broker.replace_stop_loss(symbol, new_stop_price, **kwargs)
    # Item 1512: the window row carries the calling session, so a naked
    # position can be joined to the run that produced it.
    record_unprotected_windows(broker, db, symbol, run_id=run_id, caller=caller)
    if isinstance(order, dict) and order.get("amend_status") == "market_closed":
        return record_deferred_amend(
            db,
            symbol,
            new_stop_price,
            order,
            is_short=bool(_holding_is_short(broker, symbol)),
        )
    if accepted_stop_order(order):
        recorded = write_back_stop_loss(
            db,
            symbol,
            new_stop_price,
            is_short=_holding_is_short(broker, symbol),
        )
        if not recorded:
            logger.error(
                "stop replace accepted for %s @ $%.4f but archive write-back "
                "did not persist — reconcile will surface the mismatch",
                symbol,
                new_stop_price,
            )
    return order


def reconcile_recorded_stop_levels(
    *,
    broker: Any,
    last_buy: Callable[..., dict | None],
    positions: list,
    sweep_symbol: str | None = None,
    skip_symbols: set[str] | None = None,
    db: Any,
) -> list[StopLevelMismatch]:
    """Compare each holding's recorded stop to the broker's live stop.

    Reports only. Does not write, does not place, does not invent a level.
    A missing live stop is coverage's job (`_reconcile_stop_coverage` /
    the watchdog) and is skipped here — there is no price to compare.
    A live stop with no recorded row, or a recorded row whose price is
    not the live one (beyond the broker's own round-trip epsilon), is
    the mismatch this exists to surface.

    `last_buy(symbol, action='BUY'|'SHORT')` is the same lookup coverage
    repair uses, so a long and a short cannot read each other's row.
    """
    mismatches: list[StopLevelMismatch] = []
    skip = set(skip_symbols or ())
    if sweep_symbol:
        skip.add(sweep_symbol)
    if not callable(last_buy):
        return mismatches
    for position in positions or []:
        symbol = getattr(position, "symbol", None)
        try:
            qty = float(getattr(position, "qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or qty == 0 or symbol in skip:
            continue
        is_short = qty < 0
        from src.execution.stop_read import read_stop

        _sr = read_stop(broker, symbol, db=db, context="stop-level reconcile")
        if not _sr.found:
            continue  # unreadable is recorded and alerted by read_stop
        live_px = _sr.price
        opening = "SHORT" if is_short else "BUY"
        try:
            row = last_buy(symbol, action=opening) or {}
        except TypeError:
            # A last_buy that still only takes the symbol (legacy tests /
            # purchase-memory callables). Direction is then whatever row
            # that callable returns; a BUY-only lookup on a short is a
            # mismatch of "no recorded SHORT stop", which is the honest
            # report.
            try:
                row = last_buy(symbol) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "stop-level reconcile: last-open lookup failed for %s: %s",
                    symbol,
                    exc,
                )
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stop-level reconcile: last-open lookup failed for %s: %s",
                symbol,
                exc,
            )
            continue
        recorded_px = _finite_price((row or {}).get("stop_loss"))
        if recorded_px <= 0:
            mismatches.append(
                StopLevelMismatch(
                    symbol=str(symbol),
                    recorded=None,
                    live=live_px,
                    is_short=is_short,
                    reason=(f"broker stop ${live_px:.4f} has no recorded {opening} stop_loss"),
                )
            )
            continue
        if not _prices_match(recorded_px, live_px):
            mismatches.append(
                StopLevelMismatch(
                    symbol=str(symbol),
                    recorded=recorded_px,
                    live=live_px,
                    is_short=is_short,
                    reason=(f"recorded {opening} stop_loss ${recorded_px:.4f} != broker stop ${live_px:.4f}"),
                )
            )
    return mismatches


def write_back_live_protective_stops(
    db: Any,
    mismatches: list[StopLevelMismatch],
) -> list[StopLevelMismatch]:
    """Copy the desk's live protective order onto the opening row.

    `live` came from `broker.get_current_stop_price` — an order we already
    hold, not a number we invented. Returns mismatches that could not be
    written (those still page). Successful write-back is the fix; it is
    not a mute.
    """
    remaining: list[StopLevelMismatch] = []
    for item in mismatches or []:
        live = _finite_price(item.live)
        if live <= 0:
            remaining.append(item)
            continue
        recorded = write_back_stop_loss(
            db,
            item.symbol,
            live,
            is_short=item.is_short,
        )
        if not recorded:
            remaining.append(item)
            continue
        logger.info(
            "stop-level reconcile: wrote live protective stop $%.4f back onto %s archive (was %s)",
            live,
            item.symbol,
            item.recorded,
        )
    return remaining
