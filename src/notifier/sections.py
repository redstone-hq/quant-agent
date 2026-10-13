"""Raw-error redaction, section/block builders, status and mode labels, money and time formatting.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

import re

from src.notifier.base import (
    logger,
)
from src.time_format import fmt_time_12h

_RAW_ERROR_MARKER = "[internal error — the desk logged the details]"

#: A Python traceback dump, start to end of string. Tracebacks are always
#: printed as the tail of whatever text produced them, so consuming to the
#: end of the string (DOTALL) is correct and cannot eat legitimate prose
#: that would have to come AFTER a traceback, which never happens.
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):.*", re.DOTALL)

#: A `File "path", line N[, in func]` traceback frame on its own.
_TRACE_FRAME_RE = re.compile(r'File "[^"\n]*", line \d+(?:, in \S+)?')

#: A raw exception class name — optionally module-qualified
#: (`sqlite3.OperationalError`), always CamelCase ending in `Error` or
#: `Exception` — plus whatever `str(exc)`/`repr(exc)` tail follows it
#: (`: message text` or `(message text)`) up to the end of the line.
#: Deliberately narrow: it requires the CamelCase class shape, so ordinary
#: owner prose ("a data error occurred", "tracking error") never matches —
#: only an actual exception-class token does.
_EXCEPTION_TOKEN_RE = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b"
    r"(?:\s*:\s*[^\n]*|\s*\([^)\n]*\))?"
)


def _redact_raw_exception_text(text: str) -> str:
    """Replace any raw exception / traceback text in `text` with a neutral,
    owner-appropriate marker, so a genuine `str(exc)`/`repr(exc)` (a
    `ConnectionError`, a `sqlite3.OperationalError`, a traceback frame, ...)
    can never reach the owner as-is — see board item 89 defect 5: internal
    STATUS CODES were made plain English, but raw internal-error TEXT
    interpolated into a reason string was not.

    Same shape and same rationale as `_redact_malformed_numbers` right
    above — replace only the offending span, log the original in full so
    nothing is lost for debugging, and fail open (never block a real
    alert) if the guard itself breaks.
    """
    try:
        if not text:
            return text
        original = text
        redacted = _TRACEBACK_RE.sub(_RAW_ERROR_MARKER, text)
        redacted = _TRACE_FRAME_RE.sub(_RAW_ERROR_MARKER, redacted)
        redacted = _EXCEPTION_TOKEN_RE.sub(_RAW_ERROR_MARKER, redacted)
        if redacted != original:
            logger.warning(
                "notifier: redacted raw exception/internal-error text before "
                "sending to the owner — original text for diagnosis: %r",
                original,
            )
        return redacted
    except Exception:  # noqa: BLE001
        logger.exception("notifier: raw-exception guard itself failed; sending text unredacted")
        return text


def _seal_section(lines: list[str], start: int, *, may_glue: bool = False) -> None:
    """Insert one blank line before `lines[start:]` to set it apart from
    whatever precedes it — unless there is nothing to separate from yet
    (`start == 0`, still the first content), the new block turned out to be
    empty, or (`may_glue=True`) its first line is an indented continuation
    (bullets and sub-lines like "   * ..." / "   View: ..." throughout this
    module and src/trader_feed.py).

    `may_glue` defaults False — a new block always gets its own section —
    because most callers' output never starts indented, so the default is
    "always separate", not "guess from the text". Pass `may_glue=True` only
    for a block whose SHAPE depends on the data: `src/trader_feed.py`'s PM
    section sometimes renders its own "🧠 PM/Constructor:" heading and
    sometimes, when there is nothing to decide, renders only a "   View:"
    line continuing whatever came before it (normally the signals list) —
    that one has to be told to glue when it turns out to be a continuation.

    The one place both this module and src/trader_feed.py insert a section
    break, replacing what used to be ad-hoc `lines.append("")` calls
    scattered through each formatter — and the reason a message never ends
    up with a double blank line (a block that added nothing never gets a
    break inserted before it) or a leading one (nothing precedes the first
    section, so `start == 0` short-circuits).
    """
    if len(lines) <= start or start == 0:
        return
    if may_glue and lines[start].startswith("   "):
        return
    lines.insert(start, "")


def _new_section(lines: list[str], *new_lines: str, may_glue: bool = False) -> None:
    """Append `new_lines` as their own visual section — see `_seal_section`
    for the exact rule. No-op when `new_lines` is empty, so a caller can
    always call this unconditionally instead of guarding with `if text:`."""
    if not new_lines:
        return
    start = len(lines)
    lines.extend(new_lines)
    _seal_section(lines, start, may_glue=may_glue)


def _new_block(lines: list[str], render, *args, may_glue: bool = False, **kwargs) -> None:
    """Call `render(lines, *args, **kwargs)` — which appends its own block
    of zero or more lines in place via loops/conditionals rather than
    returning a ready list — then seal it off from whatever precedes it;
    see `_seal_section`. For the many `_append_*` helpers below and in
    src/trader_feed.py built that way.
    """
    start = len(lines)
    render(lines, *args, **kwargs)
    _seal_section(lines, start, may_glue=may_glue)


#: An internal status code -> the OUTCOME in plain words, written to read
#: naturally after a session name: "Pre-market filings — nothing new
#: was filed". Board item 89 clarity defect "internal status codes shown
#: as-is": a code with its underscores taken out is still a code, so these
#: are phrases rather than title-cased identifiers.
_STATUS_LABELS: dict[str, str] = {
    "ok": "nothing to report",
    "executed": "traded",
    "intraday_executed": "traded",
    "analyzed": "done",
    "reviewed": "reviewed, nothing to change",
    "preprocessed": "done",
    "reflected": "done",
    "sent": "sent",
    "no_trades": "no trade taken",
    "intraday_no_trades": "no trade taken",
    "no_data": "no data to work from",
    "nothing_new": "nothing new was filed",
    "market_holiday": "the market was shut",
    "early_close": "the market closed early",
    "rejected": "the risk check turned the plan down",
    "hard_risk_block": "blocked by the risk rules",
    "symbol_block": "blocked by the risk rules",
    "buys_unfunded": "there was not enough cash to fund the trade",
    "failed": "it did not finish",
    "error": "it did not finish",
    "analysis_error": "the thinking step failed",
    "intraday_analysis_error": "the thinking step failed",
    "broker_error": "the broker could not be reached",
    "fetch_error": "the data could not be fetched",
    "emergency_sold": "an emergency sale was made (historical)",
    # Kept, like `emergency_sold`, so a run stored before 2026-09-20 still
    # renders as words. The whole account-level loss alarm was removed that
    # day on the owner's instruction (retired item 32); nothing emits this.
    "daily_loss_halted": "stopped for the day after losses (historical)",  # retired-ok
    "kill_switch_halted": "stopped by the manual kill switch",
    "paid_analysis_suspended": "paid thinking is suspended",
    "evening_commentary_skipped": (
        "report delivered in full; only the paid AI commentary was skipped (AI cost cap reached)"
    ),
    "evidence_gate_skip": "skipped — the data was incomplete",
    "intraday_scan_crashed": "the scan for movers crashed",
    "intraday_scan_out_of_credit": "the scan for movers stopped: the research account is out of credit",
    "intraday_scan_disabled": "the scan for movers is switched off",
    "intraday_scan_lock_contended": "the scan for movers was delayed (busy)",
    "intraday_scan_no_opportunity": "no movers worth looking at",
    "digest_only": "only partly completed",
    "skipped": "skipped",
    "scheduler_exited": "the scheduler stopped",
    "unknown": "outcome not recorded",
}


def headline_status(mode: str, result: dict) -> str:
    """The status code the HEADLINE reports for a session result.

    An evening report that carries the day's figures (`total_value`) was
    delivered; a suspended paid circuit then only cost it the AI commentary,
    so it is headed as that, never as the red FAILED of a run that did not
    finish. Every other mode/status is returned unchanged.
    """
    status = str(result.get("status", "unknown"))
    if mode == "evening" and status == "paid_analysis_suspended" and result.get("total_value") is not None:
        return "evening_commentary_skipped"
    return status


def humanize_status(status: str) -> str:
    """Plain-English rendering of an internal status code for the owner-
    facing header line — e.g. "intraday_no_trades" -> "no trade taken".

    A status with no entry in the table above is reported as exactly that:
    an outcome nobody has plain wording for. It is never paraphrased into
    something the wording cannot support, and the raw code is never passed
    off as a sentence written for the reader. It is not dropped either: it
    is the only record of what happened.
    """
    status = str(status or "")
    label = _STATUS_LABELS.get(status)
    if label:
        return label
    if not status.strip():
        return "outcome not recorded"
    return (
        "an outcome the desk has no plain wording for (its own code for it, "
        f"kept for the record, is \u201c{status}\u201d)"
    )


#: Internal session name -> the name the owner would use for it. The mode
#: string is a scheduler identifier ("earnings_preprocess", "intra_check");
#: putting it in a message is the same defect as printing a status code.
#: Owner review, 2026-09-18, extending the evening report's ratified
#: standard (PR #471) to every other message.
_MODE_LABELS: dict[str, str] = {
    "morning": "Morning session",
    "midday": "Midday review",
    "close": "Closing review",
    "evening": "Evening report",
    "once": "One-off session",
    "intra_check": "Half-hourly check",
    "earnings_preprocess": "Pre-market filings",
    "meta": "Quarterly self-review",
    "daily": "Daily performance export",
}


def mode_label(mode: str) -> str:
    """The owner-facing name of a session, never the scheduler identifier."""
    raw = str(mode or "")
    label = _MODE_LABELS.get(raw)
    if label:
        return label
    text = raw.replace("_", " ").strip()
    return (text[:1].upper() + text[1:]) if text else "Session"


def format_settled_money(value: float | None) -> str:
    """A settled dollar amount for owner-facing text, spelled so it can
    never be the malformed shape `_redact_malformed_numbers` exists to
    catch.

    2026-09-29 log: `src/cost_circuit.py`'s owner-facing alerts printed
    settled cost with `f"${value:.4f}"` (four decimal places, to show
    sub-cent amounts like $0.0049 without rounding them away) and the
    notifier's own numeric-token guard redacted every one of them --
    "dollar amount without exactly 2 decimal places" -- five separate
    times in one afternoon. The guard was working exactly as designed; the
    generator was wrong to produce a `$` token with any decimal length
    but 2. This is the one honest way to show a true sub-cent figure
    without inventing a decimal convention the rest of the desk doesn't
    use: round to cents when that loses nothing, and say so in words
    when it would round a genuine nonzero charge down to "$0.00" --
    exactly the silent-zero failure this desk treats as a fabricated
    number (see `describe_ai_cost` above, same rule, same day of review).

    `None` renders as "unavailable", never as a guessed number.
    """
    if value is None:
        return "unavailable"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "unavailable"
    if amount < 0:
        amount = 0.0
    if 0 < amount < 0.01:
        return "under a cent"
    return f"${amount:,.2f}"


def describe_ai_cost(
    cost: float | None,
    label: str = "AI cost for this run",
    free_note: str = "this run used only free models",
) -> str:
    """The AI spend for a run, in words.

    Owner review of the live 17 September evening message: "$0.0000" read as
    broken. It is not — it is true. `src/cost_table.py` pins the
    free-tier seats at $0.00 as a deliberate price, not as an unpriced
    placeholder. So the honest rendering is a sentence, not four decimal
    places.

    This is the SINGLE implementation of that wording. The evening
    formatter's `src.trader_feed._evening_cost_line` now calls it rather
    than keeping its own copy, so the two can never drift; it lives here
    because src/trader_feed.py imports src/notifier.py and not the reverse.

    `None` means "the desk could not read what this cost" and says exactly
    that — it never renders as zero. Inventing a number in an
    owner-facing message is the worst failure mode on this desk.
    """
    if cost is None:
        return f"{label}: not available"
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return f"{label}: not available"
    if value <= 0:
        return f"{label}: none — {free_note}"
    if value < 0.01:
        return f"{label}: under one cent"
    return f"{label}: ${value:,.2f}"


def _fmt_signed_money(value: float) -> str:
    """'+$12.34' / '−$12.34' — sign BEFORE the '$', and a true minus
    sign (U+2212) for negative amounts rather than Python's default
    '$+12.34' / '$-12.34', which reads oddly on a phone."""
    sign = "+" if value >= 0 else "−"
    return f"{sign}${abs(value):,.2f}"
