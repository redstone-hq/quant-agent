"""Position review (midday/close), evening report and the pre-market earnings message.

Moved verbatim from src/trader_feed.py; see src/trader_feed/__init__.py.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.notifier import (
    _actionable_coverage_gaps,
    _attr_or_key,
    _clip_text,
    _DB_PATH as _NOTIFIER_DB_PATH,
    _fmt_signed_money,
    _lookup_company_profiles,
    _margin_interest_lines,
    _new_block,
    _new_section,
    _seal_section,
    company_name,
    describe_ai_cost,
    describe_data_status,
    describe_skipped_decision,
    fmt_time_12h,
    humanize_status,
    format_session_result as _base_format_session_result,
    TelegramNotifier,
)
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, to_et
from src.trader_feed.common import (
    _SWEEP_SYMBOLS,
    _all_symbols,
    _b,
    _blocked_rows,
    _clip,
    _done_rows,
    _execution_rows,
    _fault_count,
    _fmt_elapsed,
    _fmt_pnl_line,
    _machine_detail,
    _number,
    _outcome_word,
    _pnl_section_lines,
    _profiles,
    _read_run,
    _status_emoji,
    _ticker_co,
    _wrap_details,
)
from src.trader_feed.decision import (
    _append_blocked,
    _append_book,
    _append_coverage_gaps,
    _append_done,
    _append_footer,
    _append_gate_and_execution,
    _append_held,
    _append_watch,
    _held_symbols,
    _watch_rows,
)


def _format_position_review(mode: str, result: dict, elapsed: float) -> str:
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    # See `_format_decision_session`'s identical comment: a supplied
    # `_positions` snapshot is how a stored midday/close report is
    # re-rendered without printing today's book under an old date.
    if isinstance(result.get("_positions"), list):
        snap = dict(snap)
        snap["positions"] = result["_positions"]
    status = str(result.get("status", "unknown"))
    review = result.get("review") if isinstance(result.get("review"), dict) else {}

    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(result, snap)
    held_symbols = _held_symbols(snap)
    watch_rows = _watch_rows(result)
    profiles = _profiles(done_rows, blocked_rows, held_symbols, watch_rows)

    outcome = _outcome_word(
        status,
        len(done_rows),
        len(blocked_rows),
        done_rows,
        fault_count=_fault_count(blocked_rows),
    )
    lines = [f"{_status_emoji(status)} {mode.upper()} REVIEW · {fmt_time_12h(et_now())} · {outcome}"]

    # P&L FIRST, directly under the heading (owner 2026-09-17, restated
    # 2026-09-18 as the standing rule for EVERY message). Always shown,
    # never silently dropped the way the old "Session P&L" line was on
    # every midday/close message.
    _new_section(lines, *_pnl_section_lines(result))

    _new_block(lines, _append_coverage_gaps, result)

    positions = result.get("positions")
    risk_level = review.get("risk_level")

    def _render_review_summary(lines: list[str]) -> None:
        # Board item 89 defect 2 — an assertion computed independently of
        # the list underneath it.
        #
        # This line used to print `result["positions"]`, which
        # `run_position_review` sets to `len(positions)` from the snapshot
        # taken at the START of the session, BEFORE the reviewer's own
        # exits ran and before the closing `_sync_positions_from_broker`.
        # The HELD block a few lines below reads the book AFTER all of
        # that. So on any session that actually sold something the header
        # claimed a number, and the list beneath it named a different set
        # — the owner had no way to tell which one was his book.
        #
        # The count is now taken from `held_symbols`, the very list that is
        # rendered below it, so the two cannot disagree. The pre-session
        # count is not discarded: when it differs, the message says the
        # book changed during the session rather than printing two numbers
        # and leaving the reader to reconcile them.
        if not held_symbols and not risk_level and not positions:
            return
        bits = [f"{len(held_symbols)} position(s) held now"]
        if isinstance(positions, int) and positions != len(held_symbols):
            bits.append(f"{positions} at the start of this session")
        if risk_level:
            # Board item 89 clarity defect — an unscaled risk rating. Same
            # scale treatment the evening message gives it.
            bits.append(f"risk {_risk_with_scale(risk_level)}")
        lines.append("📍 Review: " + " · ".join(bits))

    _new_block(lines, _render_review_summary)

    def _render_target_revisions(lines: list[str]) -> None:
        # ONE renderer, not a second telling (item 194): the owner feed and
        # the plain session message say the same words about a revision.
        from src.notifier import describe_target_revisions

        lines.extend(describe_target_revisions(result))

    _new_block(lines, _render_target_revisions)

    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    _new_block(lines, _append_held, held_symbols, profiles)
    _new_block(lines, _append_watch, watch_rows, profiles)
    _new_block(lines, _append_book, snap)

    actions = [row for row in (review.get("actions") or []) if isinstance(row, dict)]
    actionable = [row for row in actions if str(row.get("action", "")).upper() != "HOLD"]
    holds = [row for row in actions if str(row.get("action", "")).upper() == "HOLD"]

    detail_lines: list[str] = []
    overall = _clip(review.get("overall_assessment"), 650)
    if overall:
        detail_lines.append(f"🧠 Reviewer: {overall}")

    def _render_decisions(lines: list[str]) -> None:
        if not actions:
            return
        lines.append(f"🎯 Decisions: {len(actionable)} action(s) · {len(holds)} hold(s)")
        # Uncapped — same reasoning as `_signal_rows`: a header count must
        # never claim more than the bullets beneath it actually show.
        for row in actionable:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            stop = _number(row.get("new_stop_price"))
            stop_text = f" → stop ${stop:,.2f}" if stop is not None else ""
            reason = _clip(row.get("reason"), 420)
            text = f"   • {action} {symbol}{stop_text}"
            if reason:
                text += f" — {reason}"
            lines.append(text)
        for row in holds:
            symbol = str(row.get("symbol", "?")).upper()
            reason = _clip(row.get("reason"), 420)
            text = f"   • HOLD {symbol}"
            if reason:
                text += f" — {reason}"
            lines.append(text)

    _new_block(detail_lines, _render_decisions, may_glue=True)

    def _render_gate_and_outcome(lines: list[str]) -> None:
        _append_gate_and_execution(lines, result, snap, explain_no_trade=False)
        _, real = _execution_rows(snap)
        if not real:
            if actions and holds and not actionable:
                lines.append("⏸️ NO ACTION — reviewer explicitly held the book")
            elif not actions and positions == 0:
                lines.append("⏸️ NO ACTION — no market-risk positions required review")
            elif not actions and status == "reviewed":
                lines.append("⏸️ NO ACTION — review completed with no broker action")

    _new_block(detail_lines, _render_gate_and_outcome)
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


# === Evening report (2026-09-18 owner redesign) ===
#
# The evening message used to be rendered by the base notifier formatter,
# which predates the scan-first layout every other session message now uses.
# Owner review of the live 2026-09-17 message asked for six removals and
# three changes; each is implemented below and named where it happens:
#
#   * the run id and the provider-request count are gone (they are internal
#     identifiers with no action attached — same reasoning as `_append_footer`)
#   * "status: analyzed" is gone: it only ever meant "the evening review
#     produced a parseable answer" (src/pipeline.py `run_evening` sets
#     'analyzed' when `analysis is not None` and an error status otherwise),
#     so the normal case carried no information. The header's outcome word
#     says REVIEWED, and a failed review now says so in a sentence.
#   * the cost renders in words, never as "$0.0000"
#   * the overnight-fractional line is silent in the expected state
#     (see `_evening_fractional_line`)
#   * today's and the dated total P&L lead the message
#   * sections carry `<b>` headings, like every other feed message


def _evening_outcome(status: str, analysis: Any) -> str:
    """ONE plain word for the header — REVIEWED / REVIEW FAILED.

    This is what "status: analyzed" is translated into. `run_evening`'s
    status is a two-state fact: 'analyzed' when the evening analyst returned
    a parseable review, 'evening_analysis_error'/'evening_parse_error' when
    it did not. The owner cannot act on the success case, so it is carried
    by the header word alone; the failure case gets a sentence below.
    """
    if analysis:
        return "REVIEWED"
    if "error" in str(status or ""):
        return "REVIEW FAILED"
    return "REVIEWED"


def _evening_pnl_lines(result: dict) -> list[str]:
    """Today's P&L and the dated total, in that order, at the very top of
    the message (owner request, 2026-09-17).

    Today's figure prefers the true 4pm close-to-close P&L the evening run
    computes from broker portfolio history (`pnl_4pm`/`equity_close`) and
    falls back to the real-time prior-close→now difference, exactly as the
    base formatter did. The total is the same dated figure every other feed
    message shows (`_pnl_section_lines`), so the two can never disagree.
    A figure that is genuinely unavailable says so — never a rendered 0.
    The account's own value follows them, as it did before the redesign.
    """
    pnl_4pm = _number(result.get("pnl_4pm"))
    equity_close = _number(result.get("equity_close"))
    today_ret: float | None
    if pnl_4pm is not None and equity_close is not None:
        today_pnl = pnl_4pm
        baseline = equity_close - pnl_4pm
        today_ret = (pnl_4pm / baseline * 100) if baseline > 0 else None
        suffix = "  ·  at the 4pm close"
    else:
        today_pnl = _number(result.get("daily_pnl"))
        today_ret = _number(result.get("daily_return_pct"))
        suffix = ""
    total_pnl = _number(result.get("total_pnl"))
    total_ret = _number(result.get("total_return_pct"))
    since = result.get("total_pnl_since")
    total_label = f"📊 Total P&L since {since}:" if since else "📊 Total P&L:"
    lines = [
        _fmt_pnl_line("📈 Today's P&L:", today_pnl, today_ret) + suffix,
        _fmt_pnl_line(total_label, total_pnl, total_ret),
    ]
    # The account's own value, kept from the pre-redesign message: it is
    # the denominator both figures above are a change in, and the owner
    # never asked for it to go.
    equity = equity_close if equity_close is not None else _number(result.get("total_value"))
    if equity is not None:
        lines.append(f"   Account value: ${equity:,.2f}")
    return lines


def _evening_fractional_line(result: dict) -> str | None:
    """The overnight sub-share remainder line — but ONLY when the overnight
    state is not the one the design produces every night.

    Owner review, 2026-09-17: "overnight fractional unprotected by design"
    appeared on every single evening message and restated a design fact he
    had already ratified. A line that never varies is not information.

    What "abnormal" means here is read off the mechanism, not chosen: a
    fractional position's whole-share leg carries a GTC stop that does not
    expire, and the sub-share remainder carries a DAY stop that the broker
    expires at the close and the next session re-places. That is the
    expected state and it now says nothing. Two states are NOT expected and
    still speak:

      * a holding of less than one whole share — there is no whole-share
        leg, so `_classify_coverage_gap` calls it 'fractional' while the
        ENTIRE position is stopless overnight (the worst case is the whole
        position, not a remainder — corrected 2026-09-04);
      * a remainder the session's own sweep recorded as replaced but which
        did not in fact come back covered.

    Positions with no stop at all, and stops mis-sized on the whole-share
    leg, are not this line's business — they have their own banners above
    and are never suppressed.
    """
    from src.notifier import _gap_is_expected_fractional

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list):
        return None
    abnormal: list[dict] = []
    for gap in gaps:
        if not isinstance(gap, dict) or not _gap_is_expected_fractional(gap):
            continue
        coverage = str(gap.get("coverage", "")).strip().lower()
        covered = _number(gap.get("covered_qty"))
        if coverage == "fractional_overnight" and (covered is None or covered <= 0):
            abnormal.append(gap)
        elif coverage == "fractional_replaced" and not gap.get("repaired"):
            abnormal.append(gap)
    if not abnormal:
        return None
    total = 0.0
    for gap in abnormal:
        value = _number(gap.get("unprotected_value"))
        if value is not None:
            total += value
    names = ", ".join(str(gap.get("symbol", "?")) for gap in abnormal[:6])
    return (
        f"🛑 NO STOP OVERNIGHT: {len(abnormal)} holding(s) under one whole "
        f"share have nothing protecting them tonight — {names}" + (f" (${total:,.2f})" if total > 0 else "")
    )


def _evening_overnight_remainder_detail(result: dict) -> str | None:
    """The DOLLARS unprotected overnight on sub-share remainders, as a fact
    inside DETAILS — never a banner.

    TWO RATIFIED THINGS WERE IN CONFLICT AND ONE OF THEM HAD QUIETLY LOST.

    The owner accepted the hybrid fractional design (spec §11.1) on an
    explicit condition, recorded in `src/notifier.py`
    (`_append_fractional_overnight_line`): "a number he can look at beats a
    guarantee he has to trust." Then on 2026-09-17 he ruled the nightly
    BANNER off, because a banner that says the same sentence every night is
    not information (`_evening_fractional_line`). That ruling is right and
    is not touched here: the banner stays suppressed for the expected state.

    What went with it was the number. Measured 2026-09-30 against the live
    desk: the 2026-09-29 evening message (`notifier_sends` id 199) carried
    no figure at all while nine holdings sat with $2,264 of sub-share
    remainder unprotected overnight, and nothing in `src/api/` or
    `frontend/src/` renders stop coverage either — so the figure the owner
    made a condition of accepting the exposure was observable on no surface
    he reads. Only the 30-minute sweep's log line had it.

    So the number goes back, in the one place a never-varying sentence
    costs nothing: inside the expandable DETAILS block. No mark, no
    severity, nothing to act on — the whole-share leg is still standing
    watch and the DAY leg is re-placed at the next open, exactly as
    designed.

    Scope is deliberately the EXPECTED state only. A remainder whose
    position has no whole-share leg at all, or one the sweep recorded as
    replaced and did not get back, is abnormal, is the banner's business
    (`_evening_fractional_line`), and is excluded here so the same shares
    are never counted in two places.
    """
    from src.notifier import _gap_is_expected_fractional

    gaps = result.get("stop_coverage_gaps")
    if not isinstance(gaps, list):
        return None
    rows: list[dict] = []
    for gap in gaps:
        if not isinstance(gap, dict) or not _gap_is_expected_fractional(gap):
            continue
        if str(gap.get("coverage", "")).strip().lower() != "fractional_overnight":
            continue
        covered = _number(gap.get("covered_qty"))
        # covered <= 0 is the abnormal sub-one-share case the banner above
        # already reports. Excluded, not silently merged.
        if covered is None or covered <= 0:
            continue
        rows.append(gap)
    if not rows:
        return None
    total = 0.0
    for gap in rows:
        value = _number(gap.get("unprotected_value"))
        if value is not None:
            total += value
    # Every name, not the first few: this sits inside the expandable block,
    # which `_wrap_details` sizes against the message budget, and a count
    # of nine followed by six names is a sentence that does not add up.
    named = ", ".join(f"{gap.get('symbol', '?')} ${(_number(gap.get('unprotected_value')) or 0):,.2f}" for gap in rows)
    return (
        f"Unprotected overnight, as designed: ${total:,.2f} across "
        f"{len(rows)} holding(s) — the sub-share remainder only, whose DAY "
        f"stop the broker expires at the close and the desk re-places at "
        f"the next open. The whole shares are still covered by their GTC "
        f"stop. {named}."
    )


def _append_evening_banners(lines: list[str], result: dict) -> None:
    """Everything that must be read before the numbers. Same conditions the
    base formatter raised, in the same order, minus the nightly fractional
    line (see `_evening_fractional_line`)."""
    from src.notifier import _gap_is_expected_fractional, _gap_is_uncovered

    missing = result.get("missing_sessions")
    if isinstance(missing, list) and missing:
        hard = [m for m in missing if m == "morning" or str(m).startswith("morning (")]
        for entry in hard:
            detail = entry if entry != "morning" else ("no activity was logged this morning — check the scheduler")
            lines.append(f"🛑 MORNING SESSION DID NOT RUN — {detail}")
        soft = [m for m in missing if m not in hard]
        if soft:
            lines.append(f"⚠️ No activity logged today for: {', '.join(soft)}")

    from src.notifier import _gap_is_unreadable

    gaps = [g for g in (result.get("stop_coverage_gaps") or []) if isinstance(g, dict)]
    # Board item 172 — same partition the base formatter uses, for the same
    # reason: an unreadable row asserts nothing about coverage and must not
    # be counted into a banner that does.
    unreadable = [g for g in gaps if _gap_is_unreadable(g)]
    faults = [g for g in gaps if not _gap_is_expected_fractional(g) and not _gap_is_unreadable(g)]
    uncovered = [g for g in faults if _gap_is_uncovered(g)]
    partial = [g for g in faults if not _gap_is_uncovered(g)]
    if unreadable:
        names = ", ".join(str(g.get("symbol", "?")) for g in unreadable[:6])
        lines.append(
            f"🛑🛑 STOP UNREADABLE: {len(unreadable)} position(s) the broker "
            f"could not be asked about — coverage UNKNOWN — {names}"
        )
    if uncovered:
        names = ", ".join(str(g.get("symbol", "?")) for g in uncovered[:6])
        lines.append(f"🛑🛑🛑 NO STOP AT ALL: {len(uncovered)} position(s) with nothing protecting them — {names}")
    if partial:
        names = ", ".join(str(g.get("symbol", "?")) for g in partial[:6])
        lines.append(f"⚠️ STOP MIS-SIZED: {len(partial)} position(s) only partly protected — {names}")
    abnormal_fractional = _evening_fractional_line(result)
    if abnormal_fractional:
        lines.append(abnormal_fractional)

    analysis = result.get("analysis")
    if not analysis:
        lines.append(
            "🛑 The evening review did not complete, so there is no read on "
            "today or on tomorrow. The figures below are the broker's."
        )
    risk = _attr_or_key(analysis, "risk_rating")
    if isinstance(risk, str) and risk.lower() in ("elevated", "high"):
        lines.append(f"🚨 NEEDS YOUR ATTENTION — the desk graded today's risk {risk}")

    # A deterministic banner used to sit here, raised when the day's loss
    # reached 80% of the account-level loss limit. That
    # breaker was removed 2026-09-20 on the owner's instruction (retired
    # item 32, docs/INCIDENT_HISTORY.md).


def _append_evening_positions(lines: list[str], result: dict, profiles: dict) -> None:
    """The book, under a heading that looks like one (owner review item 8:
    the old "Positions: 9 invested $10,650" line was a section title that
    did not read as one). Winners and underwater rows are unchanged in
    content — he asked for those to be left alone — other than naming the
    company alongside the ticker, per the standing wording rule."""
    rows = [r for r in (result.get("_positions") or []) if isinstance(r, dict)]
    if not rows:
        return
    parked = sum(
        (_number(r.get("market_value")) or 0.0) for r in rows if str(r.get("symbol", "")).upper() in _SWEEP_SYMBOLS
    )
    rows = [r for r in rows if str(r.get("symbol", "")).upper() not in _SWEEP_SYMBOLS]
    if not rows:
        return
    invested = sum((_number(r.get("market_value")) or 0.0) for r in rows)
    total_value = _number(result.get("equity_close")) or _number(result.get("total_value"))
    lines.append(_b(f"POSITIONS ({len(rows)})"))
    summary = f"   ${invested:,.0f} invested"
    if total_value and total_value > 0:
        cash_pct = max(0.0, (total_value - invested) / total_value * 100)
        summary += f"  ·  {100 - cash_pct:.0f}% deployed, {cash_pct:.0f}% cash"
    if parked > 0:
        summary += f"  ·  ${parked:,.0f} parked in T-bills"
    lines.append(summary)

    def _row_line(row: dict) -> str:
        pnl = _number(row.get("unrealized_pnl")) or 0.0
        entry = _number(row.get("avg_entry"))
        price = _number(row.get("current_price"))
        pct = ((price / entry - 1) * 100) if (entry and price) else 0.0
        sign = "+" if pnl >= 0 else "−"
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        return f"   {name}  {sign}${abs(pnl):,.0f}  ({pct:+.1f}%)"

    ranked = sorted(
        (r for r in rows if _number(r.get("unrealized_pnl")) is not None),
        key=lambda r: _number(r.get("unrealized_pnl")) or 0.0,
        reverse=True,
    )
    winners = [r for r in ranked if (_number(r.get("unrealized_pnl")) or 0) > 0][:3]
    losers = [r for r in ranked if (_number(r.get("unrealized_pnl")) or 0) < 0][-3:][::-1]
    if winners:
        lines.append("📈 Top winners:")
        lines.extend(_row_line(r) for r in winners)
    if losers:
        lines.append("📉 Underwater:")
        lines.extend(_row_line(r) for r in losers)


def _append_evening_watchlist(lines: list[str], result: dict, profiles: dict) -> None:
    """Two things the desk knew at the end of the day and never said: which
    holdings sit within one ordinary day's move of their stop, and which
    report earnings imminently (owner question, 2026-09-17: "is there
    anything else that should be added?").

    Neither uses a threshold anyone picked. "Close to its stop" is measured
    against that symbol's own ATR(14) in `TradingPipeline._evening_stop_
    proximity`; "reports soon" reuses `EARNINGS_EVENT_WINDOW_SESSIONS`, the
    window every research seat already treats as imminent, and a date that
    could not be fetched is reported as not checked rather than as nothing
    due.
    """
    near = [r for r in (result.get("stop_proximity") or []) if isinstance(r, dict) and r.get("status") == "near"]
    through = [r for r in (result.get("stop_proximity") or []) if isinstance(r, dict) and r.get("status") == "through"]
    unknown_stop = [
        r for r in (result.get("stop_proximity") or []) if isinstance(r, dict) and r.get("status") == "unknown"
    ]
    earnings = [r for r in (result.get("earnings_proximity") or []) if isinstance(r, dict)]
    from src.data.event_calendar import EARNINGS_EVENT_WINDOW_SESSIONS

    soon = [
        r
        for r in earnings
        if isinstance(r.get("sessions_away"), int) and r["sessions_away"] <= EARNINGS_EVENT_WINDOW_SESSIONS
    ]
    if not near and not soon and not unknown_stop and not through:
        return
    lines.append(_b("WORTH KNOWING"))
    # Printed FIRST and separately from "near": a stop the tape has already
    # passed without the order filling is not a tight stop, it is shares
    # with nothing standing watch over them. The two used to render as the
    # same line.
    for row in sorted(
        through,
        key=lambda r: -(_number(r.get("through")) or 0.0),
    ):
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        stop = _number(row.get("stop"))
        past = _number(row.get("through"))
        stop_text = f" at ${stop:,.2f}" if stop else ""
        past_text = f" \u2014 price is ${past:,.2f} past it" if past else ""
        lines.append(
            f"   \U0001f534 {name}: the protective order{stop_text} fired "
            f"and did not fill{past_text}, so those shares have nothing "
            "standing watch over them"
        )
    for row in sorted(near, key=lambda r: _number(r.get("gap")) or 0.0):
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        stop = _number(row.get("stop"))
        stop_text = f" (stop ${stop:,.2f})" if stop else ""
        lines.append(f"   🎯 {name} is inside one ordinary day's move of its stop{stop_text}")
    if unknown_stop:
        names = ", ".join(_ticker_co(str(r.get("symbol", "?")), profiles) for r in unknown_stop[:6])
        lines.append(f"   ❔ Could not check the stop distance on: {names}")
    for row in sorted(soon, key=lambda r: r.get("sessions_away", 99)):
        name = _ticker_co(str(row.get("symbol", "?")), profiles)
        sessions = row["sessions_away"]
        when = "tomorrow" if sessions <= 1 else f"in about {sessions} trading days"
        lines.append(f"   📅 {name} reports earnings {when}")


_RISK_SCALE = ("low", "moderate", "elevated", "high")


def _risk_with_scale(risk: Any) -> str:
    """'moderate — step 2 of 4 (low · moderate · elevated · high)', or the
    word alone when it is not on the desk's own four-step scale."""
    text = str(risk or "").strip()
    if text.lower() in _RISK_SCALE:
        step = _RISK_SCALE.index(text.lower()) + 1
        return f"{text} — step {step} of {len(_RISK_SCALE)} ({' · '.join(_RISK_SCALE)})"
    return text


def _append_evening_tomorrow(lines: list[str], result: dict) -> None:
    """Tomorrow, with a scale and a consequence attached (owner review item
    9: "moderate" with no scale and "bullish" with no consequence both say
    nothing).

    The scale is the model's own enum (`models.EveningAnalysis.risk_rating`
    is `Literal['low','moderate','elevated','high']`) — printed rather than
    described, so the reader can see where tonight sits in it. The
    consequence is what the code actually does with the figure: tomorrow
    morning's Portfolio Manager and the position reviewer are both handed
    this bias and conviction as context for their decisions
    (`src/agents/portfolio_manager.py`, `src/agents/position_reviewer.py`).
    """
    analysis = result.get("analysis")
    risk = _attr_or_key(analysis, "risk_rating")
    bias = _attr_or_key(analysis, "tomorrow_bias")
    conviction = _attr_or_key(analysis, "tomorrow_conviction")
    outlook = _attr_or_key(analysis, "tomorrow_outlook") or ""
    if not (risk or bias or outlook):
        return
    lines.append(_b("TOMORROW"))
    if isinstance(risk, str) and risk.lower() in _RISK_SCALE:
        step = _RISK_SCALE.index(risk.lower()) + 1
        lines.append(f"   Risk: {risk} — step {step} of {len(_RISK_SCALE)} ({' · '.join(_RISK_SCALE)})")
    elif risk:
        lines.append(f"   Risk: {risk}")
    if bias:
        confidence = f", {conviction} confidence" if conviction else ""
        lines.append(f"   Leaning {bias}{confidence} — tomorrow morning's decisions start from this")
    if outlook:
        lines.append(f"   {_clip(outlook, 400)}")


def _evening_cost_line(snap: dict) -> str:
    """The AI spend for this session, in words.

    Owner review: "$0.0000" read as broken. It is not — it is true. Every
    seat the evening session runs (evening_analyst and news_analyst_evening)
    is on `gemini-3.5-flash-lite`, which `src/cost_table.py` pins at $0.00
    as a deliberate free-tier price, not as an unpriced placeholder; the one
    expensive seat, the portfolio manager, does not run in the evening at
    all. So the honest rendering is a word, not four decimal places.
    """
    # One implementation of this wording, in src/notifier.py, so the evening
    # message and every other owner-facing message cannot drift apart. The
    # words below are unchanged from the version the owner signed off.
    return describe_ai_cost(
        snap.get("cost"),
        label="AI cost tonight",
        free_note="the evening review runs on free models",
    )


def _evening_meta_line(auto_meta: Any) -> str | None:
    """The once-a-quarter self-review, in one line, or nothing.

    `run_evening` attaches `auto_meta` only on the last trading day of a
    quarter; `status='skipped'` on every other evening. The base formatter
    rendered five variants of this — kept, condensed, and moved into
    DETAILS, because on the 99% of nights it is absent nothing changes and
    on the night it is present the owner still needs to be told to look.
    """
    if not isinstance(auto_meta, dict):
        return None
    status = str(auto_meta.get("status", ""))
    period = auto_meta.get("period", "this quarter")
    if status == "skipped":
        return None
    if status == "auto_meta_error":
        return f"Quarterly self-review ({period}) failed — check the logs."
    if status == "digest_only":
        return (
            f"Quarterly self-review ({period}): the write-up was saved but the review itself failed — check the logs."
        )
    report = auto_meta.get("editor_report") or {}
    applied = len(report.get("applied") or [])
    rejected = report.get("rejected") or []
    staged = sum(1 for row in rejected if isinstance(row, dict) and "dry_run" in str(row.get("reason", "")))
    if applied:
        return f"Quarterly self-review ({period}): {applied} change(s) applied, {len(rejected)} rejected."
    if staged:
        return f"Quarterly self-review ({period}): {staged} proposed change(s) are staged for you to approve."
    if rejected:
        return f"Quarterly self-review ({period}): nothing applied, {len(rejected)} rejected."
    proposed = int(auto_meta.get("proposed_learnings_count") or 0)
    if proposed:
        return (
            f"Quarterly self-review ({period}): {proposed} proposal(s) generated "
            f"but the report is missing — check the logs."
        )
    return None


def _format_evening(result: dict, elapsed: float) -> str:
    status = str(result.get("status", "unknown"))
    analysis = result.get("analysis")
    run_id = result.get("run_id")
    snap = _read_run(run_id)
    # The base formatter read the positions table itself; reuse the
    # trader-feed snapshot read instead so there is one DB path, not two.
    #
    # A caller may supply `_positions` already — that is how a stored
    # report is re-rendered (`render_stored_evening`). `_read_run`'s
    # positions query is deliberately NOT run-scoped (it reads the book as
    # it stands right now), so replaying an older night through it would
    # print today's holdings under that night's date. A supplied snapshot
    # therefore wins; the live evening path never sets one and is
    # unaffected.
    result = dict(result)
    if not isinstance(result.get("_positions"), list):
        result["_positions"] = snap.get("positions") or []
    profiles = _lookup_company_profiles(
        _all_symbols(
            result["_positions"],
            result.get("stop_proximity") or [],
            result.get("earnings_proximity") or [],
        )
    )

    outcome = _evening_outcome(status, analysis)
    lines = [f"🌙 EVENING · {fmt_time_12h(et_now())} · {outcome}"]

    # P&L FIRST, directly under the heading — owner, 2026-09-18. It sat
    # below the evening banners, which is the same drift he is correcting
    # everywhere else in this pass: a banner is about the desk, the P&L is
    # about his money.
    _new_section(lines, *_evening_pnl_lines(result))
    _new_block(lines, _append_evening_banners, result)
    _new_block(lines, _append_evening_positions, result, profiles)
    _new_block(lines, _append_evening_watchlist, result, profiles)
    _new_block(lines, _append_evening_tomorrow, result)

    detail_lines: list[str] = []

    def _render_details(detail: list[str]) -> None:
        summary = _attr_or_key(analysis, "daily_summary")
        if summary:
            detail.append(_clip(summary, 700))
        risk_capital = _number(result.get("risk_capital_dollars"))
        pnl = _number(result.get("pnl_4pm"))
        if pnl is None:
            pnl = _number(result.get("daily_pnl"))
        if risk_capital is not None and risk_capital > 0 and pnl is not None:
            detail.append(
                f"Measured against the ${risk_capital:,.2f} actually at risk "
                f"today, that is {pnl / risk_capital * 100:+.2f}%."
            )
        key_risks = _attr_or_key(analysis, "tomorrow_key_risks")
        if isinstance(key_risks, list) and key_risks:
            named = "; ".join(_clip(r, 120) for r in key_risks[:3] if r)
            if named:
                detail.append(f"Watching tomorrow: {named}")
        actions = _attr_or_key(analysis, "suggested_actions")
        if isinstance(actions, list) and actions:
            detail.append("Suggested by the desk:")
            for action in actions[:5]:
                if isinstance(action, str) and action.strip():
                    detail.append(f"   • {_clip(action, 400)}")
        overnight = _evening_overnight_remainder_detail(result)
        if overnight:
            detail.append(overnight)
        meta = _evening_meta_line(result.get("auto_meta"))
        if meta:
            detail.append(meta)
        detail.append(_evening_cost_line(snap))

    _new_block(detail_lines, _render_details)
    _wrap_details(lines, detail_lines)

    _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
    return "\n".join(lines)


# === Pre-market earnings pass (2026-09-18) ===
#
# The owner's own words on the old message ("analyzed: 1 confirmed: 1
# failed: 0"): "which one? what's the symbol? what's the company?". The
# run now records each filing it handled (`result["filings"]`, see
# `TradingPipeline.run_earnings_preprocess`) and this names every one:
# ticker, company, which report, when it was filed, what the reader
# concluded, and what that changes for him. A result that predates the
# field says plainly that the companies were not recorded.

_FORM_WORDS: dict[str, str] = {
    "10-Q": "quarterly report (10-Q)",
    "10-K": "annual report (10-K)",
}


def _form_words(form_type: Any) -> str:
    text = str(form_type or "").strip()
    return _FORM_WORDS.get(text.upper(), text or "filing")


def _earnings_filing_line(row: dict, profiles: dict) -> str:
    name = _ticker_co(str(row.get("symbol", "?")), profiles)
    filed = row.get("filing_date") or "filing date not recorded"
    return f"{name} — {_form_words(row.get('form_type'))} filed {filed}"


def _format_earnings(result: dict, elapsed: float) -> str:
    status = str(result.get("status", "unknown"))
    filings = [f for f in (result.get("filings") or []) if isinstance(f, dict)]
    read = [f for f in filings if str(f.get("outcome", "")) == "analyzed"]
    failed = [f for f in filings if str(f.get("outcome", "")) != "analyzed"]
    profiles = _profiles(filings)

    if status == "analysis_error":
        outcome = "FAILED"
    elif read and failed:
        outcome = "PARTLY READ"
    elif read:
        outcome = f"{len(read)} FILING{'S' if len(read) != 1 else ''} READ"
    elif failed:
        outcome = "NOTHING READ"
    else:
        outcome = "RAN"
    lines = [f"📄 PRE-MARKET EARNINGS · {fmt_time_12h(et_now())} · {outcome}"]

    # P&L FIRST, directly under the heading — owner, 2026-09-18: EVERY
    # message, this one included. The pre-market filing reader runs before
    # the open and does no account read, so both figures are genuinely
    # unknown here; `_pnl_section_lines` renders that as "not available"
    # plus one sentence saying why, rather than dropping the block or
    # inventing a zero.
    _new_section(lines, *_pnl_section_lines(result))

    if status == "analysis_error":
        banner = [
            "🛑 The earnings reader stopped with a fault before it finished, "
            "so no filing below was read this morning. Nothing was bought or "
            "sold because of it. The desk tries again at its next pre-market "
            "pass; until a filing is read, the Portfolio Manager treats it as "
            "unread and caps any new buy of that company.",
        ]
        if result.get("error"):
            banner.append(_machine_detail(result.get("error")))
        _new_section(lines, *banner)
        if filings:
            waiting = [_b("WAITING TO BE READ")]
            waiting += [f"   • {_earnings_filing_line(f, profiles)}" for f in filings]
            _new_section(lines, *waiting)
        else:
            _new_section(
                lines,
                "The desk did not record which companies' filings were waiting.",
            )
        _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
        return "\n".join(lines)

    if not filings:
        # A result from before the run recorded its filings: say so, and
        # keep the only figures the run did record, unrounded.
        _new_section(
            lines,
            "The desk did not record which companies these were. What it "
            f"did record: {result.get('analyzed', 'not recorded')} read, "
            f"{result.get('confirmed', 'not recorded')} filed as read, "
            f"{result.get('failed', 'not recorded')} failed.",
        )
        _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
        return "\n".join(lines)

    if read:
        block = [_b("READ AND FILED")]
        for row in read:
            verdict_bits = [str(v) for v in (row.get("sentiment"), row.get("conviction")) if v]
            verdict = (
                f"{verdict_bits[0]}, {verdict_bits[1]} conviction"
                if len(verdict_bits) == 2
                else (verdict_bits[0] if verdict_bits else "the reader recorded no verdict")
            )
            block.append(f"   • {_earnings_filing_line(row, profiles)}: {verdict}")
            thesis = _clip(row.get("key_thesis"), 420)
            if thesis:
                block.append(f"      {thesis}")
        block.append(
            "   What changes: from the next session on, the Portfolio Manager "
            "and the position reviewer read this verdict when they look at "
            "the company. Nothing was bought or sold on it now."
        )
        _new_section(lines, *block)

    if failed:
        block = [_b("COULD NOT BE READ")]
        for row in failed:
            block.append(f"   • {_earnings_filing_line(row, profiles)}: the reader did not produce a usable analysis")
        block.append(
            "   What it means: nothing was traded on these. The desk tries "
            "again at its next pre-market pass, and gives up on a filing after "
            "repeated failures — this message does not record which try this "
            "was. Until it is read, the Portfolio Manager treats the filing as "
            "unread and caps any new buy of that company."
        )
        _new_section(lines, *block)

    _new_section(lines, f"🧾 {_fmt_elapsed(elapsed)}")
    return "\n".join(lines)
