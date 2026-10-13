"""The no-stop-at-all page as its own message (PR #978 defects 1 and D)."""

from __future__ import annotations


def uncovered_stop_gaps(result: dict | None) -> list[dict]:
    """The positions in a session result with NOTHING protecting them.

    The same partition `format_session_result` applies to
    `stop_coverage_gaps`: an expected fractional lapse is not a fault, and
    an UNREADABLE row asserts nothing about coverage (board item 172) so it
    must not be counted as naked.
    """
    from src.notifier import (
        _gap_is_expected_fractional,
        _gap_is_uncovered,
        _gap_is_unreadable,
    )

    if not isinstance(result, dict):
        return []
    gaps = [g for g in (result.get("stop_coverage_gaps") or []) if isinstance(g, dict)]
    return [
        g for g in gaps if not _gap_is_expected_fractional(g) and not _gap_is_unreadable(g) and _gap_is_uncovered(g)
    ]


def stop_coverage_was_audited(result: dict | None) -> bool:
    """Did this session actually answer "is anything unprotected right now"?

    `stop_coverage_gaps` is written by the broker-truth stop audit. A
    session that died (`_run_safe`'s except leaves `result=None`) or
    returned early (`evidence_gate_skip`, `broker_error`, `no_data` in
    src/pipeline.py) never ran that audit and the key is simply absent --
    which is NOT the same fact as "the audit ran and found nothing".
    """

    return isinstance(result, dict) and "stop_coverage_gaps" in result


def protection_undetermined_alert(result: dict | None, mode: str | None = None) -> str | None:
    """The page for a session that cannot say whether anything is naked.

    PR #978 defect D. The session that DIED is exactly the session where
    protection is doubtful, so the one thing that must never come out of it
    is silence -- and the one thing that must never be invented is a
    default of "everything is fine". This states the true thing: the
    question was not answered, go and look.

    `mode` is the run mode / scheduler job name. The page is only true for
    a mode whose session DECLARES that it audits stops at the broker
    (`AUDITS_BROKER_STOPS` on the session class, resolved by
    src/sessions/stop_audit.py): earnings_preprocess and the daily CSV
    export never run the audit, so their silence is not a missing answer.
    A dead auditing session (result None) still pages. A caller that cannot
    name its mode is treated as auditing -- fail closed, page.
    """

    if stop_coverage_was_audited(result):
        return None
    from src.sessions.stop_audit import mode_audits_broker_stops

    if not mode_audits_broker_stops(mode):
        return None
    # Plain words only: no status token, no run id. A raw state string is
    # not something the owner can read, and the machine reason is unchanged
    # in the result dict, the event rows and the log line.
    reason = " (it ended early)" if isinstance(result, dict) else " (it failed before it finished)"
    return (
        "⚠️ PROTECTION UNDETERMINED: this session ended without auditing "
        f"stops at the broker{reason}, so the desk CANNOT say whether any "
        "position is currently unprotected.\n"
        "This is not an all-clear. Check open positions and their stops by "
        "hand at the broker."
    )


def naked_position_alert(result: dict | None) -> str | None:
    """The NO-STOP-AT-ALL page, as its own message. None when nothing is naked.

    WHY THIS EXISTS (PR #978, adversary defect 1). This banner's only
    unconditional carrier was the session summary, and the per-category
    mute classifies that summary as operational. Its other carrier,
    `TradingPipeline._alert_owner_no_stop`, claims the symbol for the
    trading DAY, so the second and third session in which a position is
    still naked raise nothing at all — the loudest alarm the desk has,
    structurally silent from midday onwards.

    So: no daily claim and no category. No claim because a position still
    naked at 16:00 is a NEW true statement about money at risk, not a
    repeat of the morning's; no category because an unclassified kind
    resolves to `risk` and therefore survives the filter, which is the
    fail-closed default this design rests on.
    """
    if not stop_coverage_was_audited(result):
        return None
    uncovered = uncovered_stop_gaps(result)
    if not uncovered:
        return None
    names = ", ".join(str(g.get("symbol", "?")) for g in uncovered[:8])
    return (
        f"🛑🛑🛑 NO STOP AT ALL: {len(uncovered)} position(s) with nothing "
        f"protecting them — {names}\n"
        "There is nothing standing watch on these. Place a protective stop "
        "by hand or close the position."
    )


def send_naked_position_alert(notifier, result: dict | None, owner=None, mode: str | None = None) -> bool:
    """Raise `naked_position_alert` on `notifier`. Never raises. True if sent.

    Called once per session, by every entry point that sends a session
    summary (main.py, src/scheduler.py), BEFORE the summary itself.

    Silence is never the output of an AUDITING session: one that never ran
    the stop audit (died, or returned early) raises
    `protection_undetermined_alert` instead -- see PR #978 defect D. A mode
    whose session declares no broker stop audit is silent here by design;
    `mode` is how the caller says which it was (None = unknown = page).

    `owner` is whatever the caller holds that reaches the ledger (the pipeline);
    when given, a failure here also leaves a counted row. Omitted, it is logged
    with a traceback only.
    """
    try:
        text = naked_position_alert(result) or protection_undetermined_alert(result, mode=mode)
        if not text:
            return False
        symbols = [str(g.get("symbol")) for g in uncovered_stop_gaps(result) if g.get("symbol")]
        run_id = result.get("run_id") if isinstance(result, dict) else None
        from src.notifier.owner_alert import send_owner_alert_with_outcome

        return send_owner_alert_with_outcome(
            text,
            notifier=notifier,
            symbols=symbols,
            kind="no_stop_at_all",
            run_id=run_id,
        )[0]
    except Exception as exc:  # noqa: BLE001
        import logging

        from src.sentinel.guarded import record_guarded_pass

        record_guarded_pass(
            owner,
            "trader_feed.naked_position_alert",
            exc,
            log=logging.getLogger(__name__),
        )
        return False
