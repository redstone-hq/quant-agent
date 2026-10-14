"""Owner-facing text for the coverage watchdog, lifted verbatim from
`src/coverage_watchdog.py`.

Pure renderers: every function reads its inputs from its arguments and
returns a string or a plain dict. Nothing here touches the broker, the
database or the state file. `src.coverage_watchdog` re-exports every name so
all existing imports and patch targets keep working unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from src.log_markers import SWEEP_LOG_NAME  # noqa: F401  re-exported; one home for writer and reader

if TYPE_CHECKING:  # annotations only -- no runtime import, no cycle
    from src.coverage_watchdog import CoverageStatus, UnreadableStop


def exit_declined_text(symbol: str, *, side: str, why: str) -> str:
    """The owner message for an exit the desk decided on and did not place.

    Two marks, not three: the position still has whatever protection it had
    a moment ago, so this is not the unbounded-loss tier. It is above a
    warning because the desk's own decision to get out did not happen.
    """
    return (
        "🛑🛑 EXIT NOT PLACED\n"
        f"The desk decided to {side.upper()} {symbol} and did not submit "
        f"the order, because {why}.\n"
        "Nothing was sold, resized or cancelled, and any protective stop "
        "that was already resting is still resting. The desk re-attempts on "
        "its next scheduled pass; if you want out now, place the order at "
        "the broker by hand.\n"
        "THIS ALERT is sent at most once per symbol per trading day."
    )


def unreadable_stop_text(rows: Iterable[UnreadableStop]) -> str:
    """The owner message for stops that could not be READ. Board item 172.

    Says the unknown as an unknown. It does not claim the positions are
    naked and it does not reassure that they are covered, because the whole
    point is that neither was established. Severity in the leading words,
    never colour alone (`src/notifier.py` convention).
    """
    rows = list(rows)
    detail = "\n".join(
        f"  {r.symbol}: holding {r.held_qty:.4f}{' (short)' if r.is_short else ''} — {r.reason}" for r in rows
    )
    return (
        "🛑🛑 PROTECTIVE STOP UNREADABLE\n"
        f"The broker could not be asked whether {len(rows)} held "
        "position(s) have a protective stop. This is NOT a report that they "
        "are unprotected — it is a report that the desk does not know, and "
        "cannot find out, which of the two is true.\n"
        f"{detail}\n"
        "Per-position stops are the desk's only loss protection, so an "
        "unanswerable question about one is worth a look now: check the "
        "position's open orders at the broker directly and place a stop by "
        "hand if none is standing. Nothing has been sold, resized or "
        "cancelled. THIS ALERT is sent at most once per symbol per trading "
        "day; the condition itself keeps showing in the session messages "
        "for as long as it lasts, the same way a missing stop does."
    )


def alert_text(status: CoverageStatus) -> str:
    """Severity in the leading word, never colour alone (`src/notifier.py`
    convention)."""
    lines = []
    for g in status.gaps:
        dollars = f"${g.unprotected_value:,.2f}" if g.unprotected_value else "value unknown"
        lines.append(
            f"  {g.symbol}: holding {g.held_qty:.4f}, stop covers {g.covered_qty:.4f}, "
            f"{g.uncovered_qty:.4f} share(s) with NO stop ({dollars})"
        )
    reason = (
        "the database could not be read, so a session cannot be proven"
        if status.session_ran is None
        else "no scheduled session completed during that session"
    )
    if status.market_open:
        what_happens = (
            "The market is OPEN and the automatic re-placement did NOT close "
            f"this gap ({status.market_reason}). Nothing was sold, resized or "
            "cancelled — the only action this check can take is adding a "
            "protective stop, and it could not.\n\n"
        )
    else:
        what_happens = (
            f"The market is shut right now ({status.market_reason}), so no "
            "stop can be placed at this moment. The coverage sweep will put "
            "a DAY stop back over the remainder at the open, automatically, "
            "whether or not the desk is switched on.\n\n"
            "THE PART THAT IS NOT FIXED, AND CANNOT BE: a sub-share remainder "
            "is unprotected OVERNIGHT no matter what. This broker accepts "
            "fractional orders only as DAY orders, so every stop over a "
            "remainder stops existing at 16:00 ET. Re-placing it each session "
            "restores intraday protection and does nothing at all for a gap "
            "down before the open. The only ways to remove that exposure are "
            "to hold whole shares or not to hold the remainder.\n\n"
        )
    return (
        "🔴 UNPROTECTED SHARES, AND THE DESK IS NOT RUNNING\n"
        f"{len(status.gaps)} position(s) at the broker have protective-stop "
        "coverage short of what is held, and the session sweep that is "
        f"supposed to re-place it did not run on {status.trading_day} "
        f"({reason}).\n" + "\n".join(lines) + "\n"
        f"Total with no stop: ${status.unprotected_total:,.2f}.\n\n"
        + what_happens
        + "Your options: resume the desk, place the missing stop by hand "
        "(fractional stops must be DAY orders), or close the uncovered "
        "remainder. Nothing has been sold, resized or cancelled. This "
        "message repeats at most once per trading day while the condition "
        "holds."
    )


def repair_failure_text(status: CoverageStatus) -> str:
    """The alarm for a placement that was attempted and did NOT land.

    Separate from the exposure alert and on its own once-a-day marker: a
    failure to put the stop back is the state item 53 exists to make
    impossible to miss, and it must not be swallowed by an earlier report
    that merely described the same shares as uncovered.
    """
    lines = [f"  {r.symbol}: {r.qty:.4f} share(s) still with no stop — {r.detail}" for r in status.repair_failures]
    return (
        "🔴 COULD NOT PUT THE PROTECTIVE STOP BACK\n"
        f"The coverage sweep found {len(status.repair_failures)} position(s) "
        "short of stop coverage during OPEN market hours and tried to place "
        "the missing protective stop. It did not land.\n" + "\n".join(lines) + "\n\n"
        "These shares are unprotected right now, during the session, which "
        "is not the expected overnight lapse. Nothing was sold, resized or "
        "cancelled. Place the stop by hand or close the position. This "
        "message repeats at most once per trading day."
    )


def repair_resolution_text(symbols: Iterable[str]) -> str:
    """The retraction of `repair_failure_text`, in one place so the alarm
    and its all-clear cannot describe the same event two ways.

    Shared with the live session's coverage reconcile
    (`TradingPipeline._alert_owner_repair_resolved`), which can find the
    repair in a process this unit knows nothing about, exactly as the alarm
    itself is shared.
    """
    names = ", ".join(sorted(str(s).strip().upper() for s in symbols if str(s).strip()))
    count = len([s for s in symbols if str(s).strip()])
    return (
        "✅ THE PROTECTIVE STOP IS BACK\n"
        f"Earlier today the desk told you it could not put the protective "
        f"stop back on {count} position(s) and asked you to place it by "
        f"hand. It has now placed that stop itself: {names}.\n"
        "Nothing needs doing. If you already placed one by hand there will "
        "be two stops on that position — check the broker and cancel the "
        "duplicate. This clears the earlier red alert for these positions "
        "only."
    )


def repair_performed_text(status: CoverageStatus) -> str:
    """The owner message for a stop this run PUT BACK unprompted."""
    told = {str(s).strip().upper() for s in status.resolution_notice_symbols}
    rows = [r for r in status.repaired if str(r.symbol).strip().upper() not in told]
    detail = "\n".join(f"  {r.symbol}: {r.qty:.4f} share(s) had NO stop; one is now placed" for r in rows)
    return (
        "🛑🛑 A MISSING STOP WAS PUT BACK\n"
        f"The coverage sweep found {len(rows)} position(s) holding shares "
        "the broker was not watching, and placed the protective stop "
        "itself. Nothing needs doing about the stop.\n"
        f"{detail}\n"
        "Why you are being told: the sweep is the LAST line of defence. "
        "For it to find a gap at all, something earlier — an entry, a "
        "trailing ratchet or a re-protect after a partial exit — failed "
        "without saying so."
    )


def status_line(status: CoverageStatus) -> str:
    """One journal line for the heartbeat unit."""
    if status.broker_error:
        return f"coverage_watchdog: could NOT check the broker ({status.broker_error})"
    placed = ""
    if status.repaired:
        placed = (
            "; RE-PLACED "
            + ", ".join(f"{r.symbol} {r.qty:.4f}" for r in status.repaired)
            + " (DAY over any sub-share part — lapses at the close again)"
        )
    if status.repair_failures:
        placed += "; FAILED to place " + ", ".join(f"{r.symbol} {r.qty:.4f}" for r in status.repair_failures)
    # Board item 172. Appended to EVERY branch below, including the clean
    # one: a pass that could not read one symbol's stops has not checked
    # every held position, and a line saying it has would be false.
    if status.unreadable:
        placed += "; COULD NOT READ the stops of " + ", ".join(r.symbol for r in status.unreadable)
    if not status.gaps:
        if status.unreadable:
            return (
                f"coverage_watchdog: {len(status.unreadable)} position(s) "
                "UNREADABLE; every position that could be read is fully "
                "stop-covered" + placed
            )
        return "coverage_watchdog: OK — every held position is fully stop-covered" + placed
    if status.session_ran:
        return (
            f"coverage_watchdog: {len(status.gaps)} position(s) short-covered "
            f"(${status.unprotected_total:,.2f}) but a session ran on "
            f"{status.trading_day}; the sweep owns it, not alerting" + placed
        )
    if status.already_alerted_for_day:
        return (
            f"coverage_watchdog: STILL EXPOSED (${status.unprotected_total:,.2f}) "
            f"with no session on {status.trading_day}; already alerted for "
            "that day" + placed
        )
    return (
        f"coverage_watchdog: EXPOSED — {len(status.gaps)} position(s), "
        f"${status.unprotected_total:,.2f} with no stop and no session on "
        f"{status.trading_day} [{status.market_reason}]" + placed
    )


#: `agent_name` on the evidence row — distinct from 'pipeline' so the
#: dashboard's per-session feed never mistakes a sweep for a session.
SWEEP_AGENT_NAME = "coverage_sweep"


def sweep_summary(
    status: CoverageStatus | None,
    *,
    entry: str,
    run_id: str,
    alerts: Iterable[str] = (),
    error: str | None = None,
) -> dict[str, Any]:
    """Everything one run did, as one flat dict. `status` None means the run
    could not get as far as a check (`error` says why)."""
    if status is None:
        return {
            "stage": "coverage_sweep",
            "outcome": "could_not_run",
            "reason": error or "",
            "entry": entry,
            "run_id": run_id,
        }
    if status.broker_error:
        outcome = "could_not_check"
    elif status.repair_failures:
        outcome = "repair_failed"
    # Board item 172, ranked ABOVE 'repaired', 'clean' and 'gaps_left'. A
    # pass that could not read a position's stops did not establish that
    # position's coverage, and 'clean' is the one word that must never
    # describe a run holding an unanswered question about loss protection.
    elif status.unreadable:
        outcome = "unreadable_stops"
    elif status.repaired:
        outcome = "repaired"
    elif not status.gaps:
        outcome = "clean"
    elif status.repair_deferred:
        outcome = "repair_deferred"
    else:
        outcome = "gaps_left"
    return {
        "stage": "coverage_sweep",
        "outcome": outcome,
        "reason": status.repair_deferred or status.market_reason or "",
        "entry": entry,
        "run_id": run_id,
        "trading_day": status.trading_day,
        "session_ran": status.session_ran,
        "market_open": status.market_open,
        "positions_checked": status.positions_checked,
        # What the run FOUND, not what is left after it repaired them.
        "gaps_found": (len(status.gaps) if status.gaps_detected is None else status.gaps_detected),
        "gaps_remaining": len(status.gaps),
        "gap_symbols": [g.symbol for g in status.gaps],
        "unprotected_usd": status.unprotected_total,
        "repairs_attempted": len(status.repairs),
        "repairs_succeeded": len(status.repaired),
        "repairs_failed": len(status.repair_failures),
        "repaired": [{"symbol": r.symbol, "qty": r.qty} for r in status.repaired],
        "failed": [{"symbol": r.symbol, "qty": r.qty, "detail": r.detail} for r in status.repair_failures],
        "repair_deferred": status.repair_deferred,
        "broker_error": status.broker_error,
        "db_error": status.db_error,
        # Board item 172. `positions_checked` counts positions the sweep
        # LOOKED at, which includes the ones it could not read, so the two
        # numbers together say how much of the book was actually settled.
        "unreadable_count": len(status.unreadable),
        "unreadable_symbols": [r.symbol for r in status.unreadable],
        # Board item 193. A skipped symbol used to be absent from this
        # record entirely, so "is anything unguarded right now" had no
        # answer anywhere. These four keys are that answer, and they are
        # observability: nothing decides on them.
        "unguarded_count": len(status.unguarded),
        "unguarded": [
            {
                "symbol": r.symbol,
                "held_qty": r.held_qty,
                "is_short": r.is_short,
                "since_utc": r.since_utc,
                "approx_seconds_open": r.seconds_open,
                "measured_bound_seconds": r.bound_seconds,
                "bound_observations": r.bound_observations,
                "over_measured_bound": r.over_bound,
            }
            for r in status.unguarded
        ],
        "unguarded_over_bound": [r.symbol for r in status.unguarded_over_bound],
        "alerts": list(alerts),
    }


def sweep_log_line(summary: dict[str, Any]) -> str:
    """The one greppable line per run."""
    if summary.get("outcome") == "could_not_run":
        return (
            f"{SWEEP_LOG_NAME} {summary.get('run_id')} ({summary.get('entry')}): "
            f"could_not_run — {summary.get('reason')}"
        )
    alerts = summary.get("alerts") or []
    return (
        f"{SWEEP_LOG_NAME} {summary.get('run_id')} ({summary.get('entry')}): "
        f"{summary.get('outcome')} — positions checked "
        f"{summary.get('positions_checked')}, gaps "
        f"{summary.get('gaps_found')} found / "
        f"{summary.get('gaps_remaining', summary.get('gaps_found'))} still "
        f"open, "
        f"repairs attempted {summary.get('repairs_attempted')} / succeeded "
        f"{summary.get('repairs_succeeded')} / failed "
        f"{summary.get('repairs_failed')}, alert "
        f"{'; '.join(alerts) if alerts else 'none sent'}"
        + (f", deferred: {summary['repair_deferred']}" if summary.get("repair_deferred") else "")
        + (f", broker error: {summary['broker_error']}" if summary.get("broker_error") else "")
        + (
            ", UNREADABLE stops: " + ", ".join(summary.get("unreadable_symbols") or [])
            if summary.get("unreadable_count")
            else ""
        )
        # Board item 193: printed on EVERY run that has one, not only when
        # it is over the bound. A deliberate unguarded window that never
        # appears in the line is the silence this item was filed about.
        + (
            ", DELIBERATELY UNGUARDED (mid scale-in): "
            + ", ".join(
                f"{row.get('symbol')} ~"
                + (
                    f"{float(row['approx_seconds_open']):.0f}s"
                    if row.get("approx_seconds_open") is not None
                    else "age unknown"
                )
                + (" OVER LONGEST MEASURED" if row.get("over_measured_bound") else "")
                for row in (summary.get("unguarded") or [])
            )
            if summary.get("unguarded_count")
            else ""
        )
    )
