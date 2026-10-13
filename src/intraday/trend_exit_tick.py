"""src.intraday.trend_exit_tick -- the trend exit, once per session at the first 30-minute check, on DAILY closes.

Owner ruling 2026-10-09 21:02 ET (final): the trend exit (sell on a confirmed
break of the last rising low; `src/exits/alignment_exit.py`) acts ONCE a day,
at the first 30-minute check after the open, on DAILY closes -- never
intraday bars. Later checks that session only RETRY a holding whose sale
FAILED (`error`) or was PUT OFF (`deferred`).

This module adds NO exit maths, NO number and NO sell door:
  * the verdict is the review's own memoised one (`_alignment_exit_cached`),
    read on the price feed's COMPLETED daily bars (`MarketData.get_ohlcv`
    drops today's unfinished bar in market hours; nothing intraday is read),
    so in session the newest bar read is the PRIOR session's close;
  * a cleared verdict is executed by the review's OWN guarded executor
    (`_midday_execute_llm_actions`, with no model review) -- its alignment
    scan re-raises the sale from the same memoised verdict and every gate it
    applies to a review-raised sale applies here unchanged: same-day trim,
    spent trigger, exit veto, AI Risk objection list, share-count sign, and
    the fallback that restores a displaced action on refusal. No AI seat is
    called: the review's AI Risk call happens before that executor and only
    sees the model's own exits, so a scan-raised sale never reaches it.

What this adds is WHEN it runs. The pass is keyed on the SESSION date (the ET
trading date of the tick), persisted per holding (`trend_exit_tick_checks`;
its `bar_date` column holds that session key):
  * a tick OUTSIDE regular hours (the 16:00 ET tick, any off-hours run) does
    no trend work at all -- it would read a just-closed, possibly not final
    bar, could sell at/after the close, and would use up the next morning's
    pass;
  * the first in-session tick with no marker for this session runs the pass
    (normally 09:30 ET; if that tick is missed, the next in-session tick runs
    it instead);
  * later ticks that session retry ONLY `deferred` (a working order was in
    the way, or the order listing was unreadable) and `error` (the sell path
    raised). A plain refusal (`refused`: a gate, veto or objection said no)
    is a decision, not a failure, and is not retried; `refused_final`
    (bought today, already cut today) is not retried either. There is no
    retry cap: retries are bounded by the session's remaining ticks.

Overlap with a running review (the tick is exempt from the session lock, and
that lock lapses): a holding with ANY working non-stop order at the broker is
not routed to the sell path; it is marked `deferred`, still trailed, and
retried next tick. An unreadable order listing defers every firing holding
(fail closed).

An exception from the sell path may come AFTER an order went out, so every
firing holding is marked `error`, kept out of this tick's trail, and the
failure is recorded durably; the retry next tick passes the open-order check
first.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.sentinel.guarded_site import record_site

#: Same logger the intra check itself writes under (see src/intraday/session.py).
logger = logging.getLogger("src.pipeline")

__all__ = [
    "OUTCOME_DEFERRED",
    "OUTCOME_ERROR",
    "OUTCOME_HOLD",
    "OUTCOME_REFUSED",
    "OUTCOME_REFUSED_FINAL",
    "OUTCOME_SOLD",
    "SellPathError",
    "trend_exit_on_tick",
    "wire_trend_exit",
    "working_exit_symbols",
]

OUTCOME_HOLD = "hold"
OUTCOME_SOLD = "sold"
#: A gate, veto or objection refused the sale: a decision, not retried this session.
OUTCOME_REFUSED = "refused"
#: Refused for a reason that cannot clear today (bought today, already cut today): not retried this session.
OUTCOME_REFUSED_FINAL = "refused_final"
#: A working non-stop order already exists (e.g. a review's sale): retried next tick.
OUTCOME_DEFERRED = "deferred"
#: The sell path raised; an order may have gone out. Retried next tick, after the open-order check.
OUTCOME_ERROR = "error"
#: Only a sale that FAILED or was PUT OFF is retried at a later tick (owner ruling 2026-10-09 21:02 ET).
_RETRY = frozenset({OUTCOME_DEFERRED, OUTCOME_ERROR})


class SellPathError(RuntimeError):
    """The guarded sell path raised; `no_trail` names every holding that may have an order out."""

    def __init__(self, message: str, no_trail: list):
        super().__init__(message)
        self.no_trail = list(no_trail)


def _symbol(position) -> str:
    return (getattr(position, "symbol", "") or "").strip().upper()


def _order_symbol(order) -> str:
    raw = order.get("symbol") if isinstance(order, dict) else getattr(order, "symbol", "")
    return (raw or "").strip().upper()


def _enum_text(value) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def working_exit_symbols(orders) -> set:
    """Symbols with a working NON-stop buy or sell order (a sale or cover may be in flight)."""
    out = set()
    for order in orders or []:
        kind = _enum_text(getattr(order, "order_type", None) or getattr(order, "type", None))
        if "stop" in kind:
            continue
        # A cancel is asynchronous at the broker: an order already being cancelled
        # (e.g. by the freeze sweep in the same tick) is not a sale in flight.
        if _enum_text(getattr(order, "status", None)) in ("pending_cancel", "canceled", "cancelled"):
            continue
        if _enum_text(getattr(order, "side", None)) in ("sell", "buy"):
            out.add(_order_symbol(order))
    return out


def _due(marker: dict | None, session_date: str) -> bool:
    """Run this session's first pass, or retry a failed/deferred outcome from it."""
    if marker is None or marker.get("bar_date") != session_date:
        return True
    return marker.get("outcome") in _RETRY


def trend_exit_on_tick(
    *,
    positions,
    run_id: str,
    session_date: str | None,
    completed_bar_date: str,
    read_marker,
    write_marker,
    alignment_exit_cached,
    position_facts_for,
    execute_guarded_exits,
    open_orders_checked,
    final_today,
) -> dict:
    """Run this session's trend pass (or its retries) on completed daily bars; sell cleared ones via the guarded path.

    `session_date` is the ET trading date of an IN-SESSION tick, or None for a
    tick outside regular hours, which does nothing (`status: outside_session`).
    `completed_bar_date` is the newest completed daily bar (the prior close).

    Returns `{status, session_date, bar_date, checked, sold, refused, refused_final, deferred, no_trail}`;
    `no_trail` names every holding the trail must leave alone this tick. If the
    sell path raises, the firing holdings are marked `error` and `SellPathError`
    carries them to the caller.
    """
    summary: dict = {
        "status": "ran" if session_date else "outside_session",
        "session_date": session_date,
        "bar_date": completed_bar_date,
        "checked": [],
        "sold": [],
        "refused": [],
        "refused_final": [],
        "deferred": [],
        "no_trail": [],
    }

    if not session_date:
        return summary

    def mark(symbol: str, outcome: str) -> None:
        write_marker(symbol, bar_date=session_date, outcome=outcome, run_id=run_id)

    due = [p for p in positions or [] if _holding(p) and _due(read_marker(_symbol(p)), session_date)]
    if not due:
        return summary
    facts = position_facts_for(due) or {}
    cleared = []
    for position in due:
        verdict = _verdict(alignment_exit_cached, position, facts, run_id)
        summary["checked"].append(_symbol(position))
        if getattr(verdict, "exit_cleared", False):
            cleared.append(position)
        else:
            mark(_symbol(position), OUTCOME_HOLD)
    firing = _not_busy(cleared, open_orders_checked, mark, summary, completed_bar_date) if cleared else []
    if not firing:
        return summary
    symbols = [_symbol(p) for p in firing]
    try:
        orders = execute_guarded_exits(firing, run_id=run_id, position_facts=facts) or []
    except Exception as exc:
        for symbol in symbols:
            mark(symbol, OUTCOME_ERROR)
        raise SellPathError(f"sell path raised for {symbols}: {exc}", symbols) from exc
    _settle(symbols, orders, set(final_today(firing) or set()), mark, summary, completed_bar_date)
    summary["no_trail"] = list(summary["sold"])
    return summary


def _holding(position) -> bool:
    try:
        qty = float(getattr(position, "qty", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(_symbol(position)) and qty != 0


def _verdict(alignment_exit_cached, position, facts: dict, run_id: str):
    """Same arguments the review's alignment scan passes, so the executor's
    scan reads this verdict back from the memo (one chart read)."""
    own = facts.get(_symbol(position), {}) or {}
    return alignment_exit_cached(
        symbol=_symbol(position),
        thesis_invalid_if=getattr(position, "thesis_invalid_if", None) or own.get("thesis_invalid_if"),
        is_short=float(position.qty) < 0,
        entry_price=getattr(position, "avg_entry", None),
        stop_loss=getattr(position, "stop_loss", None) or own.get("stop_loss"),
        run_id=run_id,
    )


def _not_busy(cleared: list, open_orders_checked, mark, summary: dict, bar_date: str) -> list:
    """Drop (and mark `deferred`) every cleared holding with a working non-stop order; fail closed."""
    listed, open_orders = open_orders_checked()
    busy = working_exit_symbols(open_orders) if listed else {_symbol(p) for p in cleared}
    firing = []
    for position in cleared:
        symbol = _symbol(position)
        if symbol not in busy:
            firing.append(position)
            continue
        summary["deferred"].append(symbol)
        mark(symbol, OUTCOME_DEFERRED)
        logger.warning(
            "tick trend exit: %s cleared on the %s close but %s; deferred to the next tick",
            symbol,
            bar_date,
            "a working order exists for it" if listed else "the open-order listing failed",
        )
    return firing


def _settle(symbols: list, orders: list, final: set, mark, summary: dict, bar_date: str) -> None:
    """Mark each routed holding sold, refused or refused_final; none of these is retried this session."""
    ordered = {_order_symbol(order) for order in orders}
    for symbol in symbols:
        if symbol in ordered:
            outcome = OUTCOME_SOLD
        elif symbol in final:
            outcome = OUTCOME_REFUSED_FINAL
        else:
            outcome = OUTCOME_REFUSED
        summary[outcome].append(symbol)
        mark(symbol, outcome)
        if outcome == OUTCOME_REFUSED:
            logger.warning(
                "tick trend exit: %s cleared the trend exit on the %s close but the guarded sell path "
                "placed no order (refused); not retried this session",
                symbol,
                bar_date,
            )


def wire_trend_exit(host):
    """Bind `trend_exit_on_tick` to the pipeline's own collaborators, read through `host(name)`.

    Returns `callable(positions, *, run_id, total_value) -> dict` that never
    raises, or None when the host lacks any piece (the tick then reports the
    step unavailable).
    """
    from src.storage.trades import trend_tick_store as store
    from src import trading_calendar as cal

    db = host("db")
    broker = host("broker")
    cached = host("_alignment_exit_cached")
    execute = host("_midday_execute_llm_actions")
    build_facts = host("_build_position_facts")
    trimmed_today = host("_symbols_already_trimmed_today")
    opened_today = host("_position_opened_today")
    metric_deltas_for = host("_build_review_metric_deltas")
    if None in (db, broker, cached, execute, build_facts, trimmed_today, opened_today, metric_deltas_for):
        return None

    def execute_guarded_exits(firing, *, run_id: str, position_facts: dict) -> list:
        symbols = {_symbol(p) for p in firing}
        return execute(
            firing,
            None,
            run_id,
            already_trimmed_today=set(trimmed_today() or set()) & symbols,
            metric_deltas=metric_deltas_for(position_facts, run_id=run_id),
            position_facts=position_facts,
        )

    def final_today(positions) -> set:
        """Refusals that cannot clear today: opened in today's session, or already cut today.

        Takes the positions, not bare symbols, so a short is checked against its
        opening SHORT fill rather than a BUY it never had.
        """
        symbols = {_symbol(p) for p in positions}
        opened = {_symbol(p) for p in positions if opened_today(_symbol(p), is_short=float(p.qty) < 0)}
        return opened | (set(trimmed_today() or set()) & symbols)

    def _run_once(positions, *, run_id: str, total_value) -> dict:
        now = cal.et_now()
        if not cal.in_regular_session(now):
            # The 16:00 ET tick (and any off-hours run) does no trend work: see the module docstring.
            return trend_exit_on_tick(
                positions=positions,
                run_id=run_id,
                session_date=None,
                completed_bar_date=cal.last_completed_bar_date(now).isoformat(),
                read_marker=None,
                write_marker=None,
                alignment_exit_cached=None,
                position_facts_for=None,
                execute_guarded_exits=None,
                open_orders_checked=None,
                final_today=None,
            )
        ledger = db._trades()
        morning_trades = db.get_trades(today_only=True, executed_only=True)
        return trend_exit_on_tick(
            positions=positions,
            run_id=run_id,
            session_date=cal.session_date_key(now),
            completed_bar_date=cal.last_completed_bar_date(now).isoformat(),
            read_marker=lambda symbol: store.get(ledger, symbol),
            write_marker=lambda symbol, **kw: store.record(ledger, symbol, **kw),
            alignment_exit_cached=cached,
            position_facts_for=lambda due: build_facts(due, morning_trades, total_value),
            execute_guarded_exits=execute_guarded_exits,
            open_orders_checked=broker.list_open_orders_checked,
            final_today=final_today,
        )

    def run(positions, *, run_id: str, total_value=None) -> dict:
        try:
            return _run_once(positions, run_id=run_id, total_value=total_value)
        except Exception as exc:  # noqa: BLE001 — a broken trend step must not stop the other stops trailing
            # Recorded durably (guarded-outcome journal). If the SELL PATH raised, an order may
            # already be out: those holdings were marked `error` and are kept out of this tick's trail.
            record_site(SimpleNamespace(db=db), "intraday.trend_exit_tick", exc, log=logger)
            return {
                "status": "error",
                "reason": str(exc),
                "sold": [],
                "refused": [],
                "checked": [],
                "no_trail": list(getattr(exc, "no_trail", []) or []),
            }

    return run
