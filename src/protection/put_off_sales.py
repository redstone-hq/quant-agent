"""src.protection.put_off_sales -- the run that decided a sale owns it when its stop's cancel is unconfirmed.

A protected sale waits (the one order-terminal ceiling, ORDER_TERMINAL_CEILING_S)
for every cancelled leg to be confirmed dead. When the wait ends unconfirmed
the pass sends nothing and the sale is PUT OFF (refused sales, 13-14 Oct): the
write-ahead restore row stays, so the restore drain puts the stop back once
the cancel is terminal, and the put-off is kept in memory for the current run.

ONE seam, `run_owning_put_offs`, is where every session run ends -- the
scheduler's `_run_safe` and the one-shot `main.py` both run each session
through it, never a session file. Before the run returns it re-checks each
put-off: wait for the legs to be terminal, re-read the position, and send the
sale sized to what is held now through the same protected tail. Its bound is
the run's own existing deadline: the session's window end
(`SESSION_WINDOWS`) or the unit's hard kill (`SESSION_DEADLINE_ENV`, written
by scripts/run_if_et_window.sh), whichever is earlier. No new number: a
re-check is admitted only while a whole wait still fits before that bound.
Past it, nothing is sold and one alert names the stock.

The trend tick claims its own put-offs (`claim_put_offs`) and marks them
deferred, so they are retried next tick instead.
"""

import contextvars
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta

from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger("src.pipeline")

#: Epoch clock and the pause between re-checks; module names so a test can stand in.
_now = time.time
_pause = time.sleep


@dataclass(eq=False)
class PutOff:
    """One sale the pass put off: the protected tail's own arguments, the legs it waits on, and its seller."""

    sale: dict
    legs: list
    reason: str
    seller: object

    @property
    def symbol(self) -> str:
        return str(self.sale["symbol"])


@dataclass
class _Run:
    session: str
    run_id: str | None = None
    put_offs: list = field(default_factory=list)


_RUN: contextvars.ContextVar = contextvars.ContextVar("put_off_run", default=None)


def put_off(record: PutOff) -> bool:
    """Keep `record` for the current run's re-check; False when no run is open (nothing would re-check it)."""
    run = _RUN.get()
    if run is None:
        return False
    run.put_offs.append(record)
    return True


def note_run_id(run_id: str | None) -> None:
    """The current run's id (from `RunContext.start`), so a re-checked sale is recorded under it."""
    run = _RUN.get()
    if run is not None and run.run_id is None:
        run.run_id = run_id


def claim_put_offs(symbols) -> set:
    """Take the current run's put-offs for `symbols` away from its re-check; returns the symbols claimed."""
    run = _RUN.get()
    if run is None:
        return set()
    want = {str(s).strip().upper() for s in symbols}
    mine = [p for p in run.put_offs if p.symbol.strip().upper() in want]
    run.put_offs = [p for p in run.put_offs if p not in mine]
    return {p.symbol for p in mine}


def not_sold_why(session: str | None, reason: str) -> str:
    """The one owner line for a put-off sale that was not sent."""
    when = f"the next {session} run" if session else "its decider's next run"
    return f"not sold; its stop goes back when the broker confirms; decided again at {when} ({reason})"


def run_deadline(session: str) -> float | None:
    """Epoch at which this run's bound ends: its window end or the unit's hard kill, whichever is earlier."""
    from src.price_feed_preflight import SESSION_DEADLINE_ENV
    from src.trading_calendar import SESSION_WINDOWS, et_now

    bounds = []
    window = SESSION_WINDOWS.get(str(session))
    if window is not None:
        now = et_now()
        bounds.append(
            (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=window[1])).timestamp()
        )
    try:
        kill = float(os.environ.get(SESSION_DEADLINE_ENV, ""))
    except ValueError:
        kill = math.nan
    if math.isfinite(kill):
        bounds.append(kill)
    return min(bounds) if bounds else None


def run_owning_put_offs(pipeline, func, session: str):
    """THE seam every session run ends in: run `func`, then re-check the sales it put off, bounded by its deadline."""
    run = _Run(session=str(session))
    token = _RUN.set(run)
    try:
        result = func()
    except BaseException:
        _RUN.reset(token)
        for record in run.put_offs:
            _not_sold(run, record, "the run ended in an error before its re-check")
        raise
    _RUN.reset(token)
    if not run.put_offs:
        return result
    deadline = run_deadline(run.session)
    outcomes = [_recheck_safely(pipeline, run, record, deadline) for record in run.put_offs]
    if isinstance(result, dict):
        result["put_off_sales"] = outcomes
    return result


def annotate_message(message, result):
    """The session message plus one line per put-off sale's final outcome."""
    outcomes = result.get("put_off_sales") if isinstance(result, dict) else None
    if not outcomes or not isinstance(message, str):
        return message
    lines = []
    for o in outcomes:
        if o.get("outcome") == "sold":
            lines.append(f"• {o['symbol']} {o['label']} put off, then sold {o['qty']} once its stop cancel confirmed")
        else:
            lines.append(f"• {o['symbol']} {o['label']} put off and {o['why']}")
    return message + "\n\nSales put off (stop cancel unconfirmed):\n" + "\n".join(lines)


def _not_sold(run: _Run, record: PutOff, reason: str) -> dict:
    why = not_sold_why(run.session, reason)
    sale = record.sale
    logger.warning("put-off %s for %s %s", sale["label"], record.symbol, why)
    try:
        record.seller._alert_owner_exit_declined(record.symbol, side=sale["side"], why=why)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            getattr(record.seller, "db", None), "put_off_sales.alert", exc, context={"symbol": record.symbol}
        )
    return {"symbol": record.symbol, "label": sale["label"], "outcome": "not_sold", "why": why}


def _recheck_safely(pipeline, run: _Run, record: PutOff, deadline: float | None) -> dict:
    try:
        return _recheck(pipeline, run, record, deadline)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            getattr(record.seller, "db", None), "put_off_sales.recheck", exc, context={"symbol": record.symbol}
        )
        return _not_sold(run, record, f"the re-check failed: {exc}")


def _settled_in_time(record: PutOff, deadline: float | None):
    """The legs' settlement once terminal inside the bound, else (None, why)."""
    from src.execution.broker_parts.cancel_confirm import settle_legs
    from src.execution.broker_parts.order_desk import ORDER_TERMINAL_CEILING_S

    reason = record.reason
    while deadline is not None and deadline - _now() >= ORDER_TERMINAL_CEILING_S:
        started = _now()
        settlement = settle_legs(record.seller.broker, record.legs)
        if settlement.settled:
            return settlement, ""
        reason = settlement.reason
        _pause(max(0.0, started + ORDER_TERMINAL_CEILING_S - _now()))
    return (
        None,
        f"{reason}; the run's bound passed first" if deadline is not None else f"{reason}; no run deadline is known",
    )


def _row_pending(db, wal_row_id) -> bool:
    """The put-off's write-ahead row is still waiting (its stop not yet put back by the restore drain)."""
    from src.protection.sell_finalization import _WAL_SELL_SENTINEL

    if wal_row_id is None:
        return True  # nothing was cancelled into a row (an entry-order leg alone)
    rows = db.get_pending_protection_restores() or []
    return any(r.get("id") == wal_row_id and r.get("sell_order_id") == _WAL_SELL_SENTINEL for r in rows)


def _recheck(pipeline, run: _Run, record: PutOff, deadline: float | None) -> dict:
    settlement, why = _settled_in_time(record, deadline)
    if settlement is None:
        return _not_sold(run, record, why)
    seller, sale = record.seller, record.sale
    if not _row_pending(seller.db, sale["wal_row_id"]):
        return _not_sold(run, record, "its stop was already put back before the re-check")
    seller._state.set("_leg_settlement", settlement)
    reference = _reference_now(seller.broker, sale)
    # A DAY MARKET order priced now, as the plain exit path prices; the pass's limit is stale.
    result = seller._send_cleared(**dict(sale, limit_price=None, reference_price=reference))
    if result is None:
        # The tail logged and alerted its own per-symbol reason and put the stops back.
        why = seller._last_stop_clear_refusal or "refused"
        return {"symbol": record.symbol, "label": sale["label"], "outcome": "not_sold", "why": why}
    order, prot = result
    _record_trade(seller.db, run, record, order, prot["submitted_qty"], reference)
    from src.execution.broker_parts.order_desk import ORDER_TERMINAL_CEILING_S

    wait = deadline is not None and deadline - _now() >= ORDER_TERMINAL_CEILING_S
    pipeline._finalize_pending_protections([prot], context="Put-off sale", wait=wait)
    logger.warning(
        "put-off %s for %s SENT: %s once its stop cancel confirmed", sale["label"], record.symbol, prot["submitted_qty"]
    )
    return {
        "symbol": record.symbol,
        "label": sale["label"],
        "outcome": "sold",
        "qty": prot["submitted_qty"],
        "order_id": order.get("id"),
    }


def _reference_now(broker, sale: dict):
    try:
        from src.exit_quote import read_exit_quote

        quote = read_exit_quote(broker, sale["symbol"]) or {}
        return quote.get("bid" if sale["side"] == "sell" else "ask") or sale["reference_price"]
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(broker, "put_off_sales.quote", exc, context={"symbol": sale["symbol"]})
        return sale["reference_price"]


def _record_trade(db, run: _Run, record: PutOff, order: dict, qty: float, price) -> None:
    sale = record.sale
    try:
        db.insert_trade(
            symbol=record.symbol,
            action=sale["label"],
            qty=qty,
            price=price,
            reasoning=f"put-off {sale['label']}: sent by its deciding run once the cancel confirmed ({record.reason})",
            run_id=run.run_id,
            broker_order_id=order.get("id"),
            fill_status="submitted",
        )
        record_guarded_pass(db, "put_off_sales.insert_trade", context={"symbol": record.symbol})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(db, "put_off_sales.insert_trade", exc, context={"symbol": record.symbol})
