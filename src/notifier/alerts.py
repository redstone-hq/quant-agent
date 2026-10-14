"""Own-message alerts: data quality, fill confirmation, records disagreement, stop-outs, reprotection.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from typing import Any

from src.notifier.base import (
    logger,
)
from src.notifier.sections import (
    _fmt_signed_money,
    _new_section,
    fmt_time_12h,
)
from src.notifier.markup import (
    _fmt_price,
    _fmt_qty,
)
from src.notifier.category import CATEGORY_OPERATIONAL
from src.notifier.owner_alert import (
    send_owner_alert,
)
from src.notifier.wording import (
    _ALERT_EXEMPT_PER_SEAT,
    describe_data_status,
    describe_evidence_freshness,
    describe_short_handed_decision,
)


def _append_evidence_freshness(lines: list[str], result: dict) -> None:
    """Put the freshness disclosure into a session message body, and — when
    the desk PROCEEDED with a seat absent — the short-handed mark (item 154).

    The short-handed mark is suppressed on an evidence-gate refusal: that
    path already carries its own "DECISION SKIPPED — NOTHING WAS TRADED"
    banner naming the missing seat, so marking it short-handed too would say
    the same thing twice and, worse, imply the desk went ahead when it did
    not."""
    if not isinstance(result, dict):
        return
    block = describe_evidence_freshness(result.get("evidence_freshness"))
    if block:
        _new_section(lines, *block)
    if result.get("status") != "evidence_gate_skip":
        short_handed = describe_short_handed_decision(result.get("evidence_freshness"))
        if short_handed:
            _new_section(lines, *short_handed)


def describe_skipped_decision(
    lost: Any,
    data_status: Any,
    *,
    include_next_pass: bool = True,
) -> list[str]:
    """The owner-facing account of an evidence-gate skip: a bold title line
    that states the conclusion, then one short bullet per idea.

    The single source of this wording, used by BOTH owner-facing renderers
    of the same event (the standalone alert in
    `pipeline._evidence_gate_skip` and the intraday tick banner in
    src/trader_feed.py), so the two can never drift apart again.

    What it deliberately does NOT do is show `EvidenceVerdict.reason`. That
    string is the durable machine record — it stays exactly as it is in the
    database, the event rows and the log, where it is correct — but it
    carries a source-file reference, an internal seat key, a raw state
    token and "N seat(s)", none of which mean anything on a phone. Every
    fact in it is said here in words instead, via `describe_data_status`,
    which describes an unmapped token rather than guessing at it.
    """
    seats = [str(seat) for seat in (lost or []) if seat]
    status = data_status if isinstance(data_status, dict) else {}
    bad = {seat: status.get(seat) for seat in seats}
    lines = ["<b>DECISION SKIPPED — NOTHING WAS TRADED</b>"]
    detail = describe_data_status(bad) if bad else []
    if not detail:
        detail = ["a research seat the desk needed did not return an answer"]
    lines += [f"   • {sentence}" for sentence in detail]
    lines += [
        "   • the desk declined to decide rather than guess",
        "   • no Portfolio Manager call was paid for",
        "   • every position keeps the stop it already had",
    ]
    if include_next_pass:
        lines.append("   • the next scheduled decision tries again — nothing for you to do")
    return lines


def maybe_alert_data_quality(result: dict | None, *, mode: str) -> bool:
    """Fire a standalone alert when any agent's data this session was not
    clean, so a bad analyst seat can never hide inside an otherwise-normal
    run summary. Returns whether an alert was sent.

    Tech's `low_confidence` is excluded on purpose — see the module comment
    above for why a single self-reported low-conviction read on ONE symbol
    shouldn't page the owner the same way a failed or silent seat does.
    Other seats' `low_confidence` (a whole-report signal, not per-symbol)
    is NOT exempt and pages normally.
    """
    if not isinstance(result, dict):
        return False
    data_status = result.get("data_status") or {}
    if not isinstance(data_status, dict):
        return False
    from src import evidence_gate

    bad = {
        k: v
        for k, v in data_status.items()
        if evidence_gate.counts_as_degraded(v) and v not in _ALERT_EXEMPT_PER_SEAT.get(k, ())
    }
    if not bad:
        return False
    # Board item 89 clarity defect: this line used to read "macro=failed,
    # tech=partial" — internal seat names and state tokens. Same facts, in
    # words; the raw pair is kept beneath, labelled, for anyone debugging.
    from src.trading_calendar import et_now

    when = fmt_time_12h(et_now())
    detail = "\n".join(f"  • {line}" for line in describe_data_status(bad))
    raw = ", ".join(f"{k}={v}" for k, v in sorted(bad.items()))
    # Board item 89's run-identifier removal landed on the evening message
    # only; this alert still carried one. A run id is a database key, not
    # something the owner can act on — it stays in the log line above and in
    # every stored row, and leaves the message.
    text = (
        f"DATA QUALITY ALERT — the {mode} session at {when} ran on "
        f"incomplete research\n"
        f"{detail}\n"
        "WHAT THIS MEANS FOR YOU: the Portfolio Manager and the Risk "
        "Manager may have sized or decided this session on incomplete or "
        "unreadable input from the seats above. Nothing was undone; read "
        "this session's decisions with that in mind, and check Mission "
        "Control if one of them looks wrong.\n"
        f"Machine record, kept for the log — nothing here needs anything "
        f"from you: {raw}"
    )
    return send_owner_alert(text, category=CATEGORY_OPERATIONAL)


# === Fill-confirmation alerts (own message, not bundled) ===
#
# 2026-09-17. The desk confirms a fill by polling the broker over REST
# inside a bounded window. That path is now the ONLY fill-confirmation
# mechanism: `execution.fill_stream_enabled` is off, because the
# `trade_updates` websocket has never once authenticated on this host (see
# that flag for the two confirmed blockers). Nothing about that is an
# incident — it is the intended configuration.
#
# What WAS silent to the owner is the case that actually matters: the REST
# window ending without the broker having confirmed what happened to an
# order, or the half-hourly reconciliation finding the desk's own record
# and the broker's record disagreeing with no sale to explain the gap.
# Before these two alerts, both only ever reached a log line the owner
# never reads — the same failure shape as the 2026-08-28 ONDS/CCJ
# stop-outs, which sat silent for a full trading day.
#
# WHAT MUST NEVER PAGE: the websocket being off. That is the configuration,
# not a fault, and alerting on it would move ~150 daily error lines out of
# the log and into Telegram, which is worse. Neither condition below looks
# at the socket at all — both are true or false identically with the flag
# on or off.
#
# Standard owner alert-design rules (2026-09-02), same as the data-quality
# alert above: its OWN Telegram message, never a line in a run summary;
# severity carried in TEXT, never colour; NOT deduplicated, so a still-
# broken thing keeps alerting. Plain English, no jargon, no run ids, and
# any time is 12-hour with AM/PM and the timezone (`fmt_time_12h`).
#
# Neither function raises. An alerting bug must not break the execution or
# reconciliation path it reports on.


def alert_order_outcome_unconfirmed(
    symbol: str,
    order_id: str,
    waited_seconds: float,
    last_status: str | None = None,
) -> bool:
    """PAGE: the desk could not confirm what happened to a live order.

    Fires when the bounded confirmation window closed — AND the follow-up
    cancel-and-recheck also closed — with the order still not in a terminal
    state. The desk therefore does not know whether it bought anything.

    `waited_seconds` is the window the caller actually used
    (`_ENTRY_FILL_TIMEOUT_S`), passed in rather than restated here: this
    alert introduces no threshold of its own.

    Does NOT fire on an order that filled, partially filled, was cancelled
    cleanly, expired or was rejected. Every one of those is a KNOWN
    outcome, and three of them already have their own reporting.
    """
    try:
        from src.trading_calendar import et_now

        when = fmt_time_12h(et_now())
        sym = str(symbol or "").strip() or "an order"
        whole = int(waited_seconds) if waited_seconds else 0
        body = (
            "ORDER OUTCOME NOT CONFIRMED — the desk does not know whether "
            "this trade happened\n"
            f"{sym}: the desk sent an order to the broker and waited the "
            f"full {whole} seconds it allows, then cancelled it and waited "
            "again. The broker never confirmed the result either time. The "
            f'last thing it said was "{(last_status or "nothing at all")}".\n'
            "\n"
            "WHAT THIS MEANS FOR YOU: this may have bought nothing, or it "
            f"may have bought {sym} shares that have no protective stop on "
            "them yet. The desk is assuming nothing was bought, which is "
            "the safe assumption but may be wrong. It did not invent a "
            "position or a price to cover the gap.\n"
            f"WHAT TO CHECK: your broker account's {sym} position and its "
            "order list, as of " + when + ". If shares are there, the desk "
            "re-checks stop coverage at the start of every scheduled check "
            "during market hours and will place a stop on anything it finds "
            "unprotected — but confirm it did."
        )
        return send_owner_alert(body, symbols=[sym])
    except Exception as exc:  # noqa: BLE001
        from src.sentinel.counted import record_swallowed

        record_swallowed("notifier.alerts.alert_order_outcome_unconfirmed", exc, log=logger)
        logger.warning(
            "unconfirmed-order alert for %s could not be sent: %s",
            symbol,
            exc,
        )
        return False


def alert_records_disagree_with_broker(
    symbol: str,
    desk_qty: float,
    broker_qty: float,
    lookback_days: int,
) -> bool:
    """PAGE: reconciliation found the desk's record and the broker's disagreeing.

    Fires on the `stop_out_gap_unexplained` outcome only: the desk believes
    it holds more shares than the broker shows, AND no untracked sale in the
    broker's own recent order history explains the difference. That is the
    case where neither record can be trusted and nothing can be written
    back without guessing.

    Deliberately NOT fired for `stop_out_pnl_unmatched`, which is a
    different thing: there the broker and the desk agree on what was sold,
    and only the desk's own older buy history is too thin to compute the
    profit. That needs review, not a page — it is not a live position
    mismatch.

    `lookback_days` is the reconciler's own configured search window
    (`ReconciliationConfig.stop_out_lookback_days`), passed in for the same
    reason as above: no threshold is invented here.
    """
    try:
        from src.trading_calendar import et_now

        when = fmt_time_12h(et_now())
        sym = str(symbol or "").strip() or "a position"
        body = (
            "RECORDS DISAGREE — the desk's records and the broker's do not "
            "match, and the desk cannot tell which is right\n"
            f"{sym}: the desk's own records say it holds "
            f"{_fmt_qty(desk_qty)} share(s). The broker shows "
            f"{_fmt_qty(broker_qty)}. The desk searched the broker's order "
            f"history for the last {int(lookback_days)} day(s) for a sale "
            "that would explain the difference and found none.\n"
            "\n"
            "WHAT THIS MEANS FOR YOU: one of the two is wrong. Until this "
            f"is resolved, treat the desk's profit-and-loss figures for "
            f"{sym} as unreliable. Nothing was changed, written back or "
            "estimated to make the two numbers agree — the desk stopped "
            "rather than guess.\n"
            f"WHAT TO CHECK: your broker account's {sym} position and order "
            "history, as of " + when + ". The likely causes are a sale the "
            "desk placed but never recorded, or a change you made in the "
            "broker account yourself."
        )
        return send_owner_alert(body, symbols=[sym])
    except Exception as exc:  # noqa: BLE001
        from src.sentinel.counted import record_swallowed

        record_swallowed("notifier.alerts.alert_records_disagree_with_broker", exc, log=logger)
        logger.warning(
            "records-disagree alert for %s could not be sent: %s",
            symbol,
            exc,
        )
        return False


def alert_stop_out_recorded(
    symbol: str,
    qty: float,
    price: float,
    realized_pnl: float | None = None,
) -> bool:
    """PAGE: the broker closed a position on its own protective stop.

    Fires when `_reconcile_stop_out_fills` writes back a broker-initiated
    protective-stop fill the ledger never saw — an exit the market FORCED,
    not one the system chose. A protective stop only fires on a loss, so
    this is always a real loss the owner had no way of knowing about
    otherwise: before this alert existed the write-back happened silently
    (a log line and a ledger row) and reached the owner NOWHERE.

    Gets its OWN standalone message, per the owner's alert-design rule that
    alerts are never bundled into a run summary — a forced exit is exactly
    the kind of thing that must not hide inside a "session OK" message.

    `realized_pnl` is whatever the ledger could compute (`None` when its own
    BUY history can't cover the exited quantity — that unpriced case is
    flagged separately and NOT guessed here).
    """
    try:
        from src.trading_calendar import et_now

        when = fmt_time_12h(et_now())
        sym = str(symbol or "").strip() or "a position"
        if realized_pnl is None:
            pnl_line = (
                "The desk could not compute the profit or loss on this exit "
                "from its own records — that is being reviewed separately, "
                "not guessed."
            )
        else:
            pnl_line = f"Realized profit-and-loss on this exit: {_fmt_signed_money(realized_pnl)}."
        body = (
            "BROKER STOPPED YOU OUT — a protective stop fired and closed a "
            "position; the desk did not choose this exit\n"
            f"{sym}: your broker's own protective stop order sold "
            f"{_fmt_qty(qty)} share(s) at ${_fmt_price(price)}. The desk did "
            "not decide to sell — a stop it had resting at the broker "
            "triggered on the price move and closed the position for you. A "
            "protective stop only fires on a loss.\n"
            "\n"
            f"WHY YOU'RE HEARING THIS: this exit happened at the broker with "
            f"no matching order in the desk's own records, so it was written "
            f"back into the ledger just now. {pnl_line}\n"
            f"WHAT TO CHECK: your broker account's {sym} history, as of "
            + when
            + ". The position is already closed; nothing further is "
            "required of you — this is a notice that the market took you out, "
            "not a request."
        )
        return send_owner_alert(body, symbols=[sym])
    except Exception as exc:  # noqa: BLE001
        from src.sentinel.counted import record_swallowed

        record_swallowed("notifier.alerts.alert_stop_out_recorded", exc, log=logger)
        logger.warning(
            "stop-out-recorded alert for %s could not be sent: %s",
            symbol,
            exc,
        )
        return False


def _unprotected_for(since: Any, now: Any) -> str:
    """Plain-words length of time a stop was missing ('' when the start was not recorded)."""
    from datetime import datetime, timezone

    try:
        began = datetime.fromisoformat(str(since)).replace(tzinfo=timezone.utc)
        minutes = int((now.astimezone(timezone.utc) - began).total_seconds() // 60)
    except (TypeError, ValueError, AttributeError):
        return ""
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(minutes / 60)
    return f"{hours} hours" if hours < 48 else f"{round(hours / 24)} days"


def _reprotected_line(rec: dict, now: Any) -> str:
    """One stock's line: its name, shares, the stop it got back, and how long it went without."""
    sym = str(rec.get("symbol") or "").strip() or "a position"
    prices = rec.get("stop_prices") or ([rec["stop_price"]] if rec.get("stop_price") else [])
    shares = float(rec.get("shares") or 0)
    parts = [f"{_fmt_qty(shares)} share{'' if shares == 1 else 's'}"] if shares > 0 else []
    if prices:
        parts.append(f"stop back at {' and '.join('$' + _fmt_price(p) for p in prices)}")
    gap = _unprotected_for(rec.get("unprotected_since"), now)
    if gap:
        parts.append(f"was without a stop for {gap}")
    return f"{sym}: " + ", ".join(parts) if parts else sym


def alert_positions_reprotected(restored: list[dict]) -> bool:
    """NOTICE: naked positions from a prior bail were re-protected.

    Fires when `_drain_pending_protection_restores` successfully rebuilds
    stop coverage for one or more positions left unprotected by an earlier
    session that bailed mid-finalize (a lingering SELL that hadn't converged,
    or a broker-API hiccup). The write-back already happened silently before
    this — this surfaces that a live-risk gap existed and is now closed, so a
    period of unprotected exposure never passes unreported.

    `restored` is the drain's per-stock records (symbol, shares, stop price,
    when the stop went missing); each stock is named on its own line so the
    owner can tell WHICH positions were exposed.

    Its own standalone message, same alert-design rule as the siblings above.
    """
    try:
        recs = [r for r in (restored or []) if isinstance(r, dict)]
        n = len(recs)
        if n <= 0:
            return False
        from src.trading_calendar import et_now

        now = et_now()
        when = fmt_time_12h(now)
        noun = "position" if n == 1 else "positions"
        body = (
            "PROTECTION RESTORED — a position that was left without a stop is "
            "covered again\n"
            f"The desk found {n} {noun} that an earlier run had left without "
            "a protective stop (an exit that didn't finish cleanly) and put "
            "the stop coverage back on just now, as of "
            + when
            + ".\n"
            + "\n".join(_reprotected_line(r, now) for r in recs)
            + "\n\n"
            "WHY YOU'RE HEARING THIS: for a short window that "
            f"{'position was' if n == 1 else 'those positions were'} exposed "
            "with no automatic downside protection. That gap is now closed; "
            "nothing is required of you — this is a notice that it happened "
            "and was fixed."
        )
        return send_owner_alert(body)
    except Exception as exc:  # noqa: BLE001
        from src.sentinel.counted import record_swallowed

        record_swallowed("notifier.alerts.alert_positions_reprotected", exc, log=logger)
        logger.warning(
            "positions-reprotected alert could not be sent: %s",
            exc,
        )
        return False
