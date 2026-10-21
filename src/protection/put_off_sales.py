"""src.protection.put_off_sales -- the run that decided a sale owns it when its stop's cancel is unconfirmed.

A protected sale waits (the one order-terminal ceiling, ORDER_TERMINAL_CEILING_S)
for every cancelled leg to be confirmed dead. When the wait ends unconfirmed
the pass sends nothing and the sale is PUT OFF (refused sales, 13-14 Oct): the
write-ahead restore row stays, so the restore drain puts the stop back once
the cancel is terminal, and the put-off is kept in memory for the current run.

ONE seam, `run_owning_put_offs`, is where every session run ends -- the
scheduler's `_run_safe` and the one-shot `main.py` both run each session
through it, never a session file. Before the run returns it re-checks its
put-offs in rounds, all names together, so one stuck cancel never holds up
another: once a name's legs are terminal, its protection is put back up to
what is held (idempotent: a stop the restore drain already put back is not
placed twice) and its row discharged, then the WHOLE protected sale runs again
under the caller's own record (`book`). Re-running the whole sale, never a
send under the old row, is what makes the restore drain harmless: whether or
not it put the stop back first, the sale re-lists the live stops, cancels them
write-ahead and waits again. Put off again, it joins the next round.

The bound is the run's own existing deadline: the session's window end
(`SESSION_WINDOWS`) or the unit's hard kill (`SESSION_DEADLINE_ENV`, written
by scripts/run_if_et_window.sh), whichever is earlier. No new number: a name's
attempt starts only while the two order-terminal waits it can make (the sale's
own leg wait, then the wait for its fill) still fit before that bound. Past it,
nothing is sold and one alert names the stock.

Deciders that run again on fresh data take their put-offs back instead
(`claim_put_offs`): the trend tick marks them deferred for its next tick, and
the forced de-lever moves to its next name. A claimed sale is never sent
again here, but its protection still goes back in this run once its legs
settle, so it is not left to the restore drain.
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

#: The pause between re-checks; it and `_now` are module names so a test can stand in.
_pause = time.sleep


def _now() -> float:
    """Epoch seconds on the desk's one clock (`src.trading_calendar.utc_now`)."""
    from src.trading_calendar import utc_now

    return utc_now().timestamp()


@dataclass(eq=False)
class PutOff:
    """One sale the pass put off: the protected tail's own arguments, the legs it waits on, and its seller."""

    sale: dict
    legs: list
    reason: str
    seller: object
    book: object = None  # the caller's own record of a sent sale: book(order, qty)
    resell: bool = True  # False once its decider claimed it back: put the protection back only

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
    """Hand the current run's put-offs for `symbols` back to their decider; returns the symbols claimed.

    The re-check never sends a claimed sale, but still puts its protection back."""
    run = _RUN.get()
    if run is None:
        return set()
    want = {str(s).strip().upper() for s in symbols}
    mine = [p for p in run.put_offs if p.resell and p.symbol.strip().upper() in want]
    for record in mine:
        record.resell = False
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
    outcomes = _recheck_all(pipeline, run, run_deadline(run.session))
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


def _room(deadline: float | None, need: float) -> bool:
    return deadline is not None and deadline - _now() >= need


def _recheck_all(pipeline, run: _Run, deadline: float | None) -> list:
    """Rounds over every put-off until each is sold or refused, or the bound passes; outcomes in put-off order."""
    from src.protection.trim_amend import trim_book_call

    first = list(run.put_offs)
    outcomes: dict[int, dict] = {}
    try:
        ceiling = float(trim_book_call(first[0].seller, "order_terminal_ceiling_s"))
    except Exception as exc:  # noqa: BLE001
        return [_not_sold(run, record, f"the re-check failed: {exc}") for record in first]
    waiting = list(enumerate(first))
    while waiting and _room(deadline, ceiling):
        round_start = time.monotonic()
        still = []
        for i, record in waiting:
            # One deadline per round across every name: a stuck leg costs one wait, then the rest are read.
            outcome = _step(pipeline, run, record, round_start, deadline, ceiling)
            if isinstance(outcome, PutOff):
                still.append((i, outcome))
            else:
                outcomes[i] = outcome
        waiting = still
        if not any(record.resell for _, record in waiting):
            # Only a sale this run still owns keeps the run waiting: a claimed one
            # got this round's read; if unsettled, the restore drain puts it back.
            break
        _pause(max(0.0, round_start + ceiling - time.monotonic()))
    for i, record in waiting:
        if not record.resell:
            why = "handed back to its decider; its stop goes back when the broker confirms (restore drain)"
            outcomes[i] = {"symbol": record.symbol, "label": record.sale["label"], "outcome": "handed_back", "why": why}
            continue
        bound = "the run's bound passed first" if deadline is not None else "no run deadline is known"
        outcomes[i] = _not_sold(run, record, f"{record.reason}; {bound}")
    return [outcomes[i] for i in range(len(first))]


def _step(pipeline, run: _Run, record: PutOff, round_start: float, deadline: float | None, ceiling: float):
    """One name's turn in a round: its outcome, or the PutOff still waiting."""
    from src.protection.trim_amend import trim_book_call

    try:
        settlement = trim_book_call(record.seller, "settle_legs", record.legs, started=round_start)
    except Exception as exc:  # noqa: BLE001
        return _not_sold(run, record, f"the re-check failed: {exc}")
    if not settlement.settled:
        record.reason = settlement.reason
        return record
    if not record.resell:
        return _put_back_only(run, record)
    if not _room(deadline, 2 * ceiling):
        # The sale's own leg wait and its fill wait no longer fit; time only shrinks.
        return _not_sold(run, record, f"{record.reason}; too little of the run was left to sell")
    return _attempt_safely(pipeline, run, record, settlement, deadline, ceiling)


def _put_back_only(run: _Run, record: PutOff) -> dict:
    """A claimed sale's legs settled: its protection goes back now; its decider decides again."""
    seller, sale = record.seller, record.sale
    try:
        ok = seller._put_back(sale)
    except Exception as exc:  # noqa: BLE001
        return _not_sold(run, record, f"its stop could not be put back ({exc}); the restore drain retries it")
    if not ok:
        return _not_sold(run, record, "its stop could not be put back; the restore drain retries it")
    seller._discharge_wal(record.symbol, sale["wal_row_id"])
    why = "handed back to its decider, which decides again on fresh data; its stop is back"
    return {"symbol": record.symbol, "label": sale["label"], "outcome": "put_back", "why": why}


def _attempt_safely(pipeline, run: _Run, record: PutOff, settlement, deadline: float | None, ceiling: float):
    try:
        return _attempt(pipeline, run, record, settlement, deadline, ceiling)
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            getattr(record.seller, "db", None), "put_off_sales.recheck", exc, context={"symbol": record.symbol}
        )
        return _not_sold(run, record, f"the re-check failed: {exc}")


def _attempt(pipeline, run: _Run, record: PutOff, settlement, deadline: float | None, ceiling: float):
    """The legs are terminal: put the protection back, then run the whole protected sale again.

    The re-sale is sized on what the legs' fills left held (a stop can fill
    while its cancel is pending), not on the pass's snapshot.
    Returns the outcome, or a new PutOff when the sale was put off again."""
    seller, sale = record.seller, record.sale
    qty, held, refused = seller._resale_size(sale, settlement)
    if not seller._put_back(sale):
        # The cover is short of held: leave the row, so the restore drain keeps retrying the stop.
        return _not_sold(run, record, "its stop could not be put back first; the restore drain retries it")
    seller._discharge_wal(record.symbol, sale["wal_row_id"])
    if refused:
        logger.warning("put-off %s for %s not sent: %s", sale["label"], record.symbol, refused)
        seller._alert_owner_exit_declined(record.symbol, side=sale["side"], why=refused)
        return {"symbol": record.symbol, "label": sale["label"], "outcome": "not_sold", "why": refused}
    # A DAY MARKET order priced now, as the plain exit path prices; the pass's limit is stale.
    reference = _reference_now(seller.broker, sale)
    again = _Run(session=run.session, run_id=run.run_id)
    token = _RUN.set(again)
    try:
        result = seller._submit_protected_sell(
            symbol=sale["symbol"],
            qty=qty,
            limit_price=None,
            reference_price=reference,
            position_qty_before_sell=held,
            label=sale["label"],
            side=sale["side"],
            escalate_to_market_on_reject=sale["escalate_to_market_on_reject"],
            book=record.book,
        )
    finally:
        _RUN.reset(token)
    if again.put_offs:
        return again.put_offs[0]
    if result is None:
        # The protected sale logged and alerted its own per-symbol reason and put the stops back.
        refusal = seller._last_stop_clear_refusal
        why = seller._refusal_why(refusal) if refusal else "refused"
        return {"symbol": record.symbol, "label": sale["label"], "outcome": "not_sold", "why": why}
    order, prot = result
    # The order is live: a failed record must not stop its protection being finalized below.
    if record.book is not None:
        try:
            record.book(order, prot["submitted_qty"])
            record_guarded_pass(seller.db, "put_off_sales.book", context={"symbol": record.symbol})
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(seller.db, "put_off_sales.book", exc, context={"symbol": record.symbol})
    else:
        _record_trade(seller.db, run, record, order, prot["submitted_qty"], reference)
    pipeline._finalize_pending_protections([prot], context="Put-off sale", wait=_room(deadline, ceiling))
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
    """The plain trade row for a caller that handed no record of its own (`book`)."""
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
