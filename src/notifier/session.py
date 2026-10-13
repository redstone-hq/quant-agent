"""format_session_result: the top-level session message assembler.

Moved verbatim from the former src/notifier.py; the package re-exports it.
"""

from __future__ import annotations

from src.notifier.base import (
    _clip_text,
    logger,
)
from src.notifier.sections import (
    _new_block,
    _new_section,
    fmt_time_12h,
    headline_status,
    humanize_status,
    mode_label,
)
from src.notifier.markup import (
    _fmt_elapsed,
    _status_emoji,
)
from src.notifier.wording import (
    _append_universe_changes,
)
from src.notifier.alerts import (
    _append_evidence_freshness,
)
from src.notifier.gaps import (
    _actionable_coverage_gaps,
)
from src.notifier.snapshots import (
    _append_earnings_body,
    _append_intra_check_body,
    _append_meta_body,
)
from src.notifier.trade_body import (
    _append_trade_session_body,
)
from src.notifier.evening import (
    _append_evening_body,
    _evening_pnl_block,
)
from src.llm_balance_runway import balance_line
from src.notifier.costs import (
    _day_cost_line,
    _margin_interest_lines,
    _session_cost_line,
)

# === Session result formatting ===
# Built as a free function (not a TelegramNotifier method) so it's
# easy to unit-test without the network stub and so main.py can
# compute the message before deciding to send.


def _pnl_lines_for(result: dict | None, mode: str = "") -> list[str]:
    """`trader_feed._pnl_section_lines` for whatever this message knows.

    One renderer for the owner's P&L block across BOTH message modules, so
    the figure and its wording can never differ between two messages sent
    minutes apart. Never raises: a P&L-rendering fault must not be able to
    stop the message it leads — it degrades to the same honest
    "not available" wording the normal path uses for a missing figure.
    """
    if mode == "evening" and isinstance(result, dict):
        # Evening has its own, richer and 4pm-close-correct block — see
        # `_evening_pnl_block`. The shared renderer would show the
        # real-time (after-hours-contaminated) figure instead.
        try:
            return _evening_pnl_block(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evening P&L block could not be rendered: %s", exc)
    try:
        from src.trader_feed import _pnl_section_lines

        return _pnl_section_lines(result if isinstance(result, dict) else {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("P&L block could not be rendered: %s", exc)
        return [
            "\U0001f4c8 Today's P&L: not available",
            "   The figure could not be read while this message was built.",
        ]


def format_session_result(
    mode: str,
    result: dict | None,
    elapsed_seconds: float,
    error: BaseException | None = None,
) -> str | None:
    """Build the human-readable message body for one completed (or
    failed) session.

    Returns None when the session shouldn't generate a notification
    per the per-mode noise policy (intra_check OK,
    earnings_preprocess nothing_new, meta skipped). Caller treats
    None as "do nothing".
    """
    from src.trading_calendar import et_now

    _now = et_now()
    timestamp = fmt_time_12h(_now)
    elapsed_str = _fmt_elapsed(elapsed_seconds)

    if error is not None:
        # Errors always notify — operator wants to see crashes loudly.
        err_type = type(error).__name__
        # 1500 chars, not the old 500 — a Python traceback's exception
        # message (chained cause, validation error detail) routinely runs
        # long, and this is a single line in a 4000-char budget; see
        # _clip_text for why it clips on a boundary instead of mid-word.
        err_msg = _clip_text(str(error), 1500) or "(no message)"
        return (
            f"\U0001f6d1 FAILED: {mode_label(mode)} did not finish  "
            f"({timestamp})\n"
            f"error: {err_type}: {err_msg}\n"
            f"\U0001f9fe took {elapsed_str}"
        )

    if not isinstance(result, dict):
        return (
            f"\u26aa {mode_label(mode)} finished but reported nothing the "
            f"desk could read ({timestamp})\n"
            f"\U0001f9fe took {elapsed_str}"
        )

    status = headline_status(mode, result)

    # === Per-mode noise policy ===
    if mode == "intra_check" and status in ("ok", "market_holiday"):
        # Silent — would otherwise be 14 pings/day. UNLESS the 30-minute
        # sweep found a stop-coverage gap (spec §11.1 guard 3): "no
        # deterministic breach fired" is not the same as "nothing is wrong",
        # and an unprotected position found at 12:30 was previously reported
        # to a log file and nowhere else, because this is the one mode whose
        # normal tick sends no message.
        #
        # Spec §11.1 hybrid fractional stops: a gap the sweep itself already
        # closed ('fractional_replaced'), or one the design expects
        # ('fractional_overnight'), is NOT a reason to break that silence.
        # This mode ticks 14 times a day; if the routine re-placement of a
        # sub-share DAY stop pinged the owner, the fractional feature would
        # turn a deliberately-quiet channel into a noisy one, and the guard
        # that is supposed to interrupt him would arrive as ping 15.
        if not _actionable_coverage_gaps(result.get("stop_coverage_gaps")):
            return None
    if mode == "earnings_preprocess" and status in (
        "market_holiday",
        "nothing_new",
        "fetch_error",
    ):
        # nothing_new is the common case (most pre-market days have
        # no fresh 10-Q to analyze). fetch_error suppresses occasional
        # SEC transients. analysis_error still notifies (real LLM bug).
        if status == "fetch_error":
            return None
        if status == "nothing_new":
            return None
        if status == "market_holiday":
            return None
    if mode == "meta" and status == "skipped":
        return None  # quarter-end check fires daily; silent on non-Q-end
    if mode == "daily" and status == "sent":
        # The CSV document push (with its self-describing caption) IS
        # the delivery confirmation — a second status text every weekday
        # would be pure noise. error / skipped still notify below.
        return None

    run_id = result.get("run_id", "?")
    emoji = _status_emoji(status)
    # Colour-blind-safe (item 21b): a 🛑 circle and a 🟢/🟡/⚪ circle must not
    # differ by hue alone. The shape itself (🛑 vs the others) already
    # breaks that tie, but the header's first word states it in text too,
    # so the line is still correct with zero emoji rendering — "status:
    # {status}" a line below is not the FIRST word of the message.
    severity_prefix = "FAILED: " if emoji == "\U0001f6d1" else ""
    # The outcome goes IN the title line, in words. Two lines that used to
    # sit under it are gone (owner review, 2026-09-18, extending PR #471's
    # ratified evening standard to every message):
    #   - "run_id: earnings_preprocess-a745ceda" — a run identifier
    #     means nothing to him and he does not need it. Board item 89
    #     clarity defect "run identifiers", previously fixed in the evening
    #     message only. `run_id` is still read below for the cost lookup;
    #     it is simply never shown.
    #   - "status: Preprocessed" — an internal status code with its
    #     underscores taken out is still an internal status code. Board
    #     item 89 clarity defect "internal status codes shown as-is".
    lines: list[str] = [
        f"{emoji} {severity_prefix}{mode_label(mode)} — {humanize_status(status)}  ({timestamp})",
    ]

    # P&L FIRST, directly under the heading — owner, 2026-09-18, verbatim:
    # "all the P&L information has to go at the very top of every telegram
    # alert, right after the first line, which is really the heading." A
    # REPEAT correction: it kept drifting below whatever block was added
    # next, so the tests that go with this change assert the POSITION, not
    # the presence.
    #
    # Rendered by the same `trader_feed._pnl_section_lines` every other
    # message uses, so two messages can never state his P&L differently.
    # Imported lazily because `trader_feed` imports this module. A mode
    # that carries no account figures (the pre-open filing reader, a crash
    # report) renders "not available" plus one sentence saying why — never
    # a dropped block and never a fabricated zero.
    _new_section(lines, *_pnl_lines_for(result, mode))

    # Per-session LLM cost (looked up from agent_logs by run_id), the
    # day-to-date spend, and (morning/once only) the prepaid balance and
    # margin-interest lines — one "cost info" section, kept tight against
    # itself and separated from the header above and the body below.
    #
    # Per-session cost: shows for every mode that ran agents — operator
    # wants to see the dollar spend alongside the orders. Returns None
    # silently if no DB or no rows; the line is omitted rather than render
    # "$?.??" mid success-message noise.
    #
    # Day-to-date: shown on every session that spent money, because that is
    # when "how close am I" to the self-imposed brake is actually being
    # asked. Distinct from the balance line below, which is real money.
    #
    # OpenRouter balance (morning/once only): owner request 2026-08-31 — he
    # wants to see the balance falling rather than discover it empty.
    # OpenRouter is PREPAID; on 2026-08-31 the account was down to $7.10,
    # about seven clean trading days, and nothing in the system said so.
    # Morning-only because it changes slowly and repeating it on every
    # session would train the operator to skim past it.
    cost_block: list[str] = []
    cost_line = _session_cost_line(run_id)
    if cost_line:
        cost_block.append(cost_line)
    day_line = _day_cost_line()
    if day_line and cost_line:
        cost_block.append(day_line)
    if mode in ("morning", "once"):
        cost_block.append(balance_line())
    _new_section(lines, *cost_block)

    # Margin interest gets its OWN section, not a berth in the cost block
    # above (owner, 2026-09-18). It sat there since 2026-09-01 and he never
    # found it: model spend and the prepaid OpenRouter balance are what it
    # costs to RUN the desk, while this is the price of money the desk
    # borrowed — a different kind of number, and filing it under running
    # costs is what made it invisible.
    if mode in ("morning", "once"):
        _new_section(lines, *_margin_interest_lines())

    # === Mode-specific body ===
    if mode in ("morning", "midday", "close", "once"):
        _new_block(lines, _append_trade_session_body, result)
        _append_evidence_freshness(lines, result)
        if mode in ("morning", "once"):
            _append_universe_changes(lines, result)
    elif mode == "evening":
        _new_block(lines, _append_evening_body, result)
    elif mode == "earnings_preprocess":
        _new_block(lines, _append_earnings_body, result)
    elif mode == "intra_check":
        _new_block(lines, _append_intra_check_body, result)
        _append_evidence_freshness(lines, result)
    elif mode == "meta":
        _new_block(lines, _append_meta_body, result)
    elif mode == "daily":
        # Only error / skipped reach here ("sent" is silenced above).
        # Surface the failure reason — a bare '🛑 FAILED: status error' is
        # undebuggable from a phone.
        daily_block: list[str] = []
        filename = result.get("filename", "")
        if filename:
            daily_block.append(f"📊 {result.get('rows', '?')} rows → {filename}")
        err = result.get("error")
        if err:
            daily_block.append(f"error: {err}")
        _new_section(lines, *daily_block)

    # "elapsed: 3m 5s" — a raw label the owner called noise. Kept,
    # because a session that suddenly takes four times as long is worth
    # seeing, but as the evening message already renders it.
    _new_section(lines, f"\U0001f9fe took {elapsed_str}")
    return "\n".join(lines)
