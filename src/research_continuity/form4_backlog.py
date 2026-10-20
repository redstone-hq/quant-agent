"""src.research_continuity.form4_backlog -- the pre-market insider-filing record and its alert.

Bodies moved verbatim from src/pipeline_research_continuity.py (originally
src/pipeline.py), item 210 step 9, second research-continuity instalment.
The Form 4 backlog and congressional refresh records and the
before-the-open backlog alert. Writes the journal only; reads no storage.

Every collaborator is an explicit keyword-only constructor argument; nothing
here imports src.pipeline. Storage and the evidence journal (anything with
`EventJournal.persist_evidence`, src/ports/event_journal.py) are handed in,
never reached for through a host.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _edgar_unverified_clause(record: dict) -> str:
    """Owner-facing words for an unverified EDGAR count; codes go to the log only."""
    from src.data.smart_money_edgar_coverage import UNVERIFIED_EDGAR_REASONS

    raw_reasons = [str(r) for r in (record.get("reasons") or [])]
    logger.error("PRE-OPEN: Form 4 coverage unverified, reasons=%s", raw_reasons)
    # Only the UNVERIFIED reasons are faults; days_not_queried and
    # scan_cap_reached are the scan's own bounded choices.
    faults = [r for r in raw_reasons if r in UNVERIFIED_EDGAR_REASONS]
    if not raw_reasons:
        return "no coverage was recorded at all"
    tail = ", so a quiet day and a failed read cannot be told apart"
    if "edgar_total_unreadable" in faults:
        days_read = int(record.get("days_queried") or 0)
        unreadable = max(days_read - int(record.get("days_with_total") or 0), 0)
        return f"the filing service gave no usable count for {unreadable} of the {days_read} days read{tail}"
    if faults:
        return f"the filing service's answers could not be fully checked{tail}"
    return ""


def _alert_text(watched_all_read: bool, names: int, why: list[str], coverage_line: str) -> str:
    """Assemble the owner alert; lead with good news when every watched name was read."""
    if watched_all_read:
        lead = f"All {names} companies the desk watches had every insider filing read"
        lead += (": " + "; ".join(why)) if why else ""
        tail = ". Only the market-wide scan for companies outside the watched list is short."
    else:
        lead = "Insider-filing check did not finish this morning: " + "; ".join(why)
        tail = (
            ". Until the check is complete, the desk still makes its trading decisions "
            "but without complete insider evidence, and each decision records that."
        )
    # Carried whatever the reason for the alert, not only when coverage itself
    # is the complaint — the counts are the context for every other line.
    return (
        lead
        + tail
        + " Existing positions and their stops are unaffected."
        + (f" {coverage_line}" if coverage_line else "")
    )


class Form4BacklogRecorder:
    """Pre-market Form 4 backlog / congressional refresh records and the backlog alert."""

    def __init__(self, *, journal) -> None:
        self.journal = journal

    def _record_form4_backlog(self, run_id: str, refresh: dict) -> None:
        """Persist the pre-market Form 4 backlog and coverage. Never raises."""
        if not isinstance(refresh, dict):
            return
        import json as _json

        keys = (
            "status",
            "pending_filings",
            "watched_pending_filings",
            "discovery_cap_reached",
            "watched_read_through",
            "watched_names",
            "watched_names_read_through",
            "watched_names_unread",
            "watched_unchecked_names",
            "watched_drain_ran",
            "watched_drain_read",
            "watched_drain_deadline_hit",
            "edgar_coverage",
            "error",
        )
        self.journal.persist_evidence(
            run_id=run_id,
            agent_name="smart_money_refresh",
            kind="form4_backlog",
            scope="run",
            evidence_json=_json.dumps(
                {k: refresh.get(k) for k in keys},
                sort_keys=True,
                default=str,
            ),
        )

    def _record_congressional_refresh(self, run_id: str, refresh: dict) -> None:
        """Persist the congressional refresh's counts. Never raises.

        Same record as the Form 4 backlog above: per source fetched, already
        seen, processed, new, dropped by reason, watermark before/after,
        duration, and how old the newest disclosure and each source's copy
        are. Nothing is written when the congressional feed is switched off.
        """
        summary = refresh.get("congressional") if isinstance(refresh, dict) else None
        if not isinstance(summary, dict):
            return
        import json as _json

        self.journal.persist_evidence(
            run_id=run_id,
            agent_name="smart_money_refresh",
            kind="congressional_refresh",
            scope="run",
            evidence_json=_json.dumps(summary, sort_keys=True, default=str),
        )

    def _alert_form4_backlog_before_open(self, refresh: dict) -> None:
        """Say BEFORE the open that today's insider evidence is incomplete.

        `refresh` has always computed the backlog numbers and the pipeline
        had only ever logged them. A returned value nobody catches is a
        check that does not exist — on 2026-09-18 the cap bound at the
        pre-market refresh, and the first anyone knew of it was six lost
        decision windows later.

        The condition is coverage: every watched name read through today,
        nothing unread, nothing unchecked. Anything else means the insider
        seat cannot be current on every tick today. Since PR #535 that seat
        is advisory — it no longer stops the desk — so the alert says the
        desk decides WITHOUT complete insider evidence, not that it refuses.
        """
        if not isinstance(refresh, dict):
            return
        from src.util.time import et_today

        read_through = str(refresh.get("watched_read_through") or "").strip()[:10]
        today = et_today().isoformat()
        watched_pending = int(refresh.get("watched_pending_filings") or 0)
        unchecked = list(refresh.get("watched_unchecked_names") or [])
        cap_reached = bool(refresh.get("discovery_cap_reached"))
        names = int(refresh.get("watched_names") or 0)
        names_read = int(refresh.get("watched_names_read_through") or 0)
        # Board item 126. EDGAR publishes its own count of the Form 4s filed
        # on a day. When the morning read could not obtain that count, it
        # cannot tell "nobody filed anything" from "our read of the filings
        # service came back broken" — and the second case used to reach this
        # desk looking exactly like the first.
        #
        # Fail CLOSED on a missing record, matching the morning seat in
        # src/pipeline_stages.py: a refresh that ran a Form 4 pass and
        # recorded no coverage answered the question not at all, which is
        # not the same as answering it well. A refresh that carries the
        # Form 4 drain keys is held to this, and so is one that reports an
        # error — a sub-provider that raised outright produces neither the
        # drain keys nor a coverage record, and that is the LOUDEST case,
        # not an exemption. A wrapper with neither is not asked to answer
        # for coverage it never had; it is NOT thereby let off the alert,
        # because an empty `watched_read_through` still trips the ordinary
        # did-not-finish clause below.
        edgar = refresh.get("edgar_coverage")
        form4_answered = "watched_drain_ran" in refresh or bool(refresh.get("error"))
        edgar_unverified = form4_answered and not (isinstance(edgar, dict) and edgar.get("verified"))
        record = edgar if isinstance(edgar, dict) else {}
        # Reported whether or not anything is wrong. The market-wide scan is
        # bounded by its own deadline and in production reaches a minority
        # of the lookback window, so "how much of the window did we check"
        # is a fact the owner needs on an ORDINARY morning — rendering it
        # only on the failure branch would have shown him the honest number
        # exactly when it was least representative.
        #
        # Gated on whether coverage was RECORDED, not merely present. A
        # blank record is all zeros, and "read 0 of 0 filings across 0 of 0
        # days" reads to a human as nothing to worry about when it means
        # the opposite — the same trap `ratio` already avoids by answering
        # None to nought-of-nought rather than 1.0.
        if record.get("known"):
            coverage_line = (
                "Insider-filing coverage this morning: read "
                f"{record.get('enumerated', 0)} of {record.get('edgar_total', 0)} "
                "filings the service reported, covering the "
                f"{record.get('days_queried', 0)} most recent days of a "
                f"{record.get('days_in_window', 0)}-day look-back window."
            )
        elif record:
            coverage_line = (
                "Insider-filing coverage this morning: NOT KNOWN — the "
                "morning read did not record how much of the filing service "
                "it covered."
            )
        else:
            coverage_line = ""
        if coverage_line:
            logger.info("PRE-OPEN: %s", coverage_line)
        if read_through == today and not watched_pending and not unchecked and not edgar_unverified:
            return
        why: list[str] = []
        deadline_hit = bool(refresh.get("watched_drain_deadline_hit"))
        # Every watched company read through today: the shortfall, if any, is
        # in the market-wide scan only, so the alert must not say the check
        # "did not finish".
        watched_all_read = (
            names > 0
            and names_read >= names
            and read_through == today
            and not watched_pending
            and not unchecked
            and not deadline_hit
        )
        if edgar_unverified:
            clause = _edgar_unverified_clause(record)
            if clause:
                why.append(clause)
        if names and not watched_all_read:
            why.append(
                f"{names_read} of our {names} companies have every insider filing read",
            )
        if watched_pending:
            why.append(
                f"{watched_pending} company filing(s) on names we hold are still unread",
            )
        if unchecked:
            why.append(
                f"{len(unchecked)} of our own companies could not be checked at all",
            )
        if deadline_hit:
            why.append(
                "the morning read of our own companies ran out of time; it resumes where it stopped tomorrow morning",
            )
        if cap_reached:
            why.append(
                "the market-wide scan stopped at its own limit"
                if watched_all_read
                else "the morning read stopped at its own limit before finishing",
            )
        if not why:
            why.append(
                "the morning read did not confirm it finished"
                + (f" (last confirmed {read_through})" if read_through else ""),
            )
        text = _alert_text(watched_all_read, names, why, coverage_line)
        logger.error("PRE-OPEN: %s", text)
        try:
            from src.notifier import CATEGORY_OPERATIONAL, send_owner_alert

            send_owner_alert(text, category=CATEGORY_OPERATIONAL)
        except Exception as exc:  # noqa: BLE001
            logger.error("Form 4 backlog pre-open alert failed to send: %s", exc)
