"""Intraday, intra_check and hourly desk-check messages.

Moved verbatim from src/trader_feed.py; see src/trader_feed/__init__.py.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.intraday.no_room_text import no_room_sentence
from src.intraday_scan_outcome import scan_failure_banner
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
from src.trading_calendar import SESSION_WINDOWS, et_now, in_session_window, sql_utc_stamp_ago, to_et
from src.trader_feed.common import (
    _DB_PATH,
    _INTRADAY_SILENT_STATUSES,
    _SWEEP_SYMBOLS,
    _blocked_rows,
    _budgeted_sections,
    _done_rows,
    _fault_count,
    _fill_state_plain,
    _looked_at_rows,
    _machine_detail,
    _number,
    _outcome_word,
    _pnl_section_lines,
    _profiles,
    _read_run,
    _ticker_co,
    _traded_word,
    _wrap_details,
    logger,
)
from src.trader_feed.decision import (
    _append_blocked,
    _append_coverage_gaps,
    _append_done,
    _append_footer,
    _append_gate_and_execution,
    _append_intraday_evidence_freshness,
    _append_pm,
    _append_risk,
    _append_rotation,
    _append_signals,
    _signal_row_line,
)


def _format_intraday(outer: dict, nested: dict, elapsed: float) -> str:
    run_id = nested.get("run_id") or outer.get("run_id")
    snap = _read_run(run_id)
    status = str(nested.get("status", "unknown"))

    candidates = [str(symbol).upper() for symbol in (nested.get("candidates") or []) if symbol]
    done_rows = _done_rows(snap)
    blocked_rows = _blocked_rows(nested, snap)
    acted = {row["symbol"] for row in done_rows} | {row["symbol"] for row in blocked_rows}
    looked_at_rows = _looked_at_rows(snap, candidates, acted)
    profiles = _profiles(done_rows, blocked_rows, looked_at_rows)

    outcome = _outcome_word(
        status,
        len(done_rows),
        len(blocked_rows),
        done_rows,
        fault_count=_fault_count(blocked_rows),
    )
    lines = [f"⚡ INTRADAY OPPORTUNITY · {fmt_time_12h(et_now())} · {outcome}"]

    def _render_status_banner(lines: list[str]) -> None:
        if nested.get("pm_call_skipped"):
            # Owner ruling 2026-10-12: a deliberate skip, never a PM failure.
            lines.append(f"🔵 {no_room_sentence(nested)}")
        if status == "paid_analysis_suspended":
            lines.append(
                "🛑 SUSPENDED: paid opportunity discovery is suspended by the "
                "cost circuit; the deterministic intraday loss check completed "
                "normally."
            )
            if nested.get("error"):
                lines.append(_machine_detail(nested.get("error")))
        elif status == "intraday_analysis_error":
            # Board item 89 defect 5: `failure_status` is an internal token
            # ("pm_output_unparseable") and was printed in the middle of the
            # plain sentence. The sentence says what happened; the token
            # itself carries nothing the owner can act on and is dropped.
            lines.append(
                "🛑 FAILED: the Portfolio Manager's analysis did not "
                "complete, so nothing was decided. This was not a "
                "deliberate no-trade decision."
            )
            if nested.get("error") or nested.get("failure_status"):
                lines.append(
                    _machine_detail(
                        " ".join(str(part) for part in (nested.get("failure_status"), nested.get("error")) if part)
                    )
                )
        elif status == "evidence_gate_skip":
            # Was: one prose sentence, then `nested["reason"]` printed raw
            # underneath it — so the owner read the same explanation twice,
            # once in English and once in machine ("1 seat(s) ... "
            # "smart_money=expired ... docs/WORK.md item 20"). The stored
            # reason is unchanged; it simply no longer renders. Title line
            # plus bullets, shared with the standalone alert via
            # `describe_skipped_decision`.
            skip_lines = describe_skipped_decision(
                nested.get("lost_seats"),
                nested.get("data_status"),
                # This IS the tick's own message; "the next scheduled
                # decision tries again" belongs on the standalone alert,
                # not repeated inside every tick.
                include_next_pass=False,
            )
            lines.append(f"🟡 {skip_lines[0]}")
            lines.extend(skip_lines[1:])
        elif status in ("intraday_scan_crashed", "intraday_scan_out_of_credit"):
            # Operator-honesty fix: this used to be indistinguishable from a
            # healthy tick that ran and found nothing — the scan raised, the
            # caller swallowed the exception and set scan_result to None, and no
            # `intraday_scan` key ever reached this formatter. Now the crash
            # attaches a dict with this status, so it renders through the same
            # nested path `paid_analysis_suspended` / `intraday_analysis_error`
            # already use, instead of silently reading as "Status: ok".
            lines.append(scan_failure_banner(status))
            # The exception TYPE is not thrown away — it moves out of the
            # owner's sentence and into the labelled machine line with the
            # message, where it belongs and where it stays greppable.
            if nested.get("error") or nested.get("error_type"):
                lines.append(
                    _machine_detail(
                        " ".join(str(part) for part in (nested.get("error_type"), nested.get("error")) if part)
                    )
                )

    # P&L FIRST, directly under the heading — owner, 2026-09-18. It used to
    # sit BELOW the status banner here, which is exactly the drift he is
    # correcting: the banner is about this tick, the P&L is about his money.
    _new_section(lines, *_pnl_section_lines(outer))

    _new_block(lines, _render_status_banner)

    _new_block(lines, _append_done, done_rows, snap, profiles)
    _new_block(lines, _append_blocked, blocked_rows, profiles)
    # Same pruning-pass report the morning message carries (board item 219).
    _new_block(lines, _append_rotation, snap)
    # Held back and spliced in here — see `_budgeted_sections`.
    looked_at_slot = len(lines)

    detail_lines: list[str] = []
    # How much of this tick's evidence was actually read on this tick.
    # Owner mandate 2026-09-18 made every seat but the chart research
    # advisory, so an intraday decision can now stand on one fresh read plus
    # this morning's book — and every carried seat reports clean. It sits
    # inside the details block rather than the headline: it belongs to every
    # tick, and a line on every tick at the top is how a banner stops being
    # read. Disclosure only; it states a count and never judges one.
    _new_block(
        detail_lines,
        _append_intraday_evidence_freshness,
        outer,
        nested,
    )
    # Reasoning first, the per-candidate enumeration last — same ordering
    # and same reason as `_format_decision_session`. Risk/Execution are
    # split into `protected_lines` and given their own reserved budget in
    # `_wrap_details` (2026-09-25 reasoning-visibility fix) so they survive
    # even when the PM narrative alone is long enough to eat the budget.
    _new_block(detail_lines, _append_pm, snap, may_glue=True)
    _new_block(detail_lines, _append_signals, snap, candidates=candidates)
    protected_lines: list[str] = []
    _new_block(protected_lines, _append_risk, snap)
    # `nested`, not `outer`: on the intraday path the traded-order evidence
    # (and the run_id it's keyed by) lives in the `intraday_scan` sub-dict.
    _new_block(protected_lines, _append_gate_and_execution, nested, snap)
    _budgeted_sections(
        lines,
        looked_at_slot,
        looked_at_rows,
        profiles,
        snap,
        detail_lines,
        protected_lines,
    )

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


# === intra_check cadence (2026-09-17) ===
#
# Owner requirement: "I want messages every hour. I want oversight and
# transparency of what is being done." Ticks still run every 30 minutes,
# unchanged — but a tick with nothing actionable (no order, trade, skip,
# fill, stop/coverage problem, or error) sends NO message at all, same as
# before AND now including the ":30 INTRADAY OPPORTUNITY ... NO TRADE"
# message, which used to always send. Silence on a quiet half-hour is fine;
# silence for a whole clock hour is not. The top-of-hour tick (":00", plus
# the 9:30 session-open tick, which is the first tick of the day and has no
# ":00" before it) is the guaranteed pulse: it ALWAYS sends one message,
# built from DB records covering the last ~hour — including the :30 tick's
# analysis, which ran under a different run_id than this tick's own — so
# the owner never goes more than a clock hour without a message, even on a
# fully quiet stretch. Anything actionable still sends immediately at any
# tick, exactly as before; if that already happened this hour, the
# top-of-hour tick still sends its own summary, as a second message body.
_HOUR_WINDOW_MINUTES = 70  # 60 minutes + a buffer for scheduler jitter

# 2026-09-17 regression, fixed same night: PR #454 moved intra_check's own
# systemd timer off the shared `*:0/30` tick onto `*:15,45` (to stop it
# firing in the same wall-clock second as morning/midday/close/evening —
# see scripts/systemd/quant-agent-intra_check.timer for the full story),
# but `_is_hourly_checkpoint` below still hard-coded `minute == 0` as "the
# top of the hour". intra_check is the ONLY session that runs this check
# (it is the one that ticks all day, 09:30-16:00 ET) and it no longer ever
# lands on minute 0 — so the owner's guaranteed once-per-hour message
# silently stopped firing on a quiet day. Never caught because nothing
# tied the checkpoint minute to the timer's actual cadence.
#
# Fix: the "top of the hour" is not minute 0, it is whatever minute
# intra_check's OWN timer first fires on each hour — read from the unit
# file itself (scripts/systemd/quant-agent-intra_check.timer) rather than
# duplicated here as a second hard-coded number, so the next cadence move
# carries the guarantee with it instead of quietly breaking it again. See
# tests/test_trader_feed.py::test_hourly_checkpoint_minute_matches_intra_check_timer_cadence
# for the guard that fails the build if this ever falls out of sync with
# the unit file.
_INTRA_CHECK_TIMER_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "systemd" / "quant-agent-intra_check.timer"
)

# A bare `*:MM` or `*:MM,MM,...` OnCalendar spec — the only form intra_check's
# timer has ever used and the only form this parses. Anything else (a day
# restriction, a `/` step syntax, a full calendar expression, ...) is a
# cadence change this code cannot safely interpret, and it must say so
# loudly rather than guess.
_ONCALENDAR_PER_HOUR_RE = re.compile(r"\*:(\d{1,2}(?:,\d{1,2})*)")


def _intra_check_tick_minutes() -> tuple[int, ...]:
    """The minutes-past-the-hour intra_check actually fires on, read from
    its own systemd timer unit. Raises rather than guessing if the file is
    missing or its `OnCalendar=` line isn't the simple per-hour form this
    understands — a silent fallback here is exactly the class of bug this
    function exists to prevent.
    """
    try:
        text = _INTRA_CHECK_TIMER_PATH.read_text()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read intra_check's own timer unit at "
            f"{_INTRA_CHECK_TIMER_PATH} to derive the hourly-checkpoint "
            f"minute: {exc}"
        ) from exc

    spec = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("OnCalendar="):
            spec = line[len("OnCalendar=") :].strip()
            break
    if spec is None:
        raise RuntimeError(
            f"{_INTRA_CHECK_TIMER_PATH} has no OnCalendar= line; cannot derive the hourly-checkpoint minute"
        )

    match = _ONCALENDAR_PER_HOUR_RE.fullmatch(spec)
    if not match:
        raise RuntimeError(
            f"{_INTRA_CHECK_TIMER_PATH}: OnCalendar={spec!r} is not a "
            "simple '*:MM' / '*:MM,MM' per-hour cadence — "
            "_is_hourly_checkpoint doesn't know how to read this and won't "
            "guess. Update the parsing deliberately if the cadence syntax "
            "has genuinely changed."
        )
    return tuple(sorted({int(m) for m in match.group(1).split(",")}))


def _hourly_checkpoint_minute() -> int:
    """The minute-past-the-hour that owes the clock hour its guaranteed
    message: the FIRST tick intra_check makes each hour, under whatever
    cadence its timer is actually running."""
    return _intra_check_tick_minutes()[0]


def _is_hourly_checkpoint(now=None) -> bool:
    """True at the tick that owes this trading hour its guaranteed message.

    The first tick of each hour (see `_hourly_checkpoint_minute`) owns
    that hour. The 9:30 session-open tick is the exception: it is the
    first tick of the day, so there is no earlier tick this hour to have
    covered the 9:00-9:30 gap (during which the market was closed anyway)
    — 9:30 stands in for it.
    """
    now = now or et_now()
    return now.minute == _hourly_checkpoint_minute() or (now.hour == 9 and now.minute == 30)


def _intraday_tick_actionable(result: dict, nested: dict | None, snap: dict[str, Any]) -> bool:
    """Whether THIS intra_check tick has something the owner needs to see
    now, rather than folded into the next top-of-hour summary — an order,
    a trade, an execution skip, a stop-coverage problem, or a genuine
    error/interruption in paid analysis. Read-only classification; a
    mis-read here costs at most one message's timing, never a trading
    decision.

    `skips` is read from `snap` (the same `_read_run` DB snapshot every
    other renderer here uses), not from `nested["execution_skips"]` — the
    intraday scan's own result dict never sets that key (checked against
    `_intraday_opportunity_scan_body` in src/pipeline.py: it returns only
    status/candidates/orders/run_id), so a skip would otherwise never be
    seen as actionable here.
    """
    if _actionable_coverage_gaps(result.get("stop_coverage_gaps")):
        return True
    if not isinstance(nested, dict):
        return False
    status = str(nested.get("status") or "")
    if status in (
        "intraday_executed",
        "intraday_analysis_error",
        "intraday_scan_crashed",
        "intraday_scan_out_of_credit",
        "paid_analysis_suspended",
        "evidence_gate_skip",
    ):
        return True
    if status == "intraday_no_trades":
        # PM/risk reached a real decision point with nothing to execute —
        # actionable only if execution actually had something to skip
        # (insufficient cash, etc.); a clean "nothing to do" is quiet.
        return bool(snap.get("skips"))
    return False  # disabled / lock_contended / no_opportunity — quiet


def _midday_collision_tick_minute() -> int | None:
    """Minute-of-ET-day of the ONE intra_check tick that collides with the
    midday position review, or None if the two never meet.

    `midday` runs once per ET date (`scripts/run_if_et_window.sh` writes a
    last-run marker for every mode but `intra_check`), so its single report
    lands on the first scheduler tick inside `SESSION_WINDOWS["midday"]`.
    The colliding intra_check tick is therefore the first intra_check tick
    at or after that window opens — derived from the window constant and
    intra_check's own timer unit rather than restated as a third number.
    """
    lo, hi = SESSION_WINDOWS["midday"]
    ticks = _intra_check_tick_minutes()
    for hour in range(lo // 60, hi // 60 + 1):
        for minute in ticks:
            candidate = hour * 60 + minute
            if lo <= candidate <= hi:
                return candidate
    return None


def _is_midday_collision_tick(now=None) -> bool:
    """True only at that single tick, and only on a session weekday."""
    now = now or et_now()
    if not in_session_window("midday", when=now):
        return False
    collision = _midday_collision_tick_minute()
    if collision is None:
        return False
    now_et = to_et(now)
    return now_et.hour * 60 + now_et.minute == collision


def _format_intra_check(result: dict, elapsed_seconds: float) -> str | None:
    nested = result.get("intraday_scan")
    run_id = (nested.get("run_id") if isinstance(nested, dict) else None) or result.get("run_id")
    snap = _read_run(run_id)
    actionable = _intraday_tick_actionable(result, nested, snap)

    own_message: str | None = None
    if actionable:
        nested_status = str(nested.get("status") or "") if isinstance(nested, dict) else ""
        if isinstance(nested, dict) and nested_status not in _INTRADAY_SILENT_STATUSES:
            own_message = _format_intraday(result, nested, elapsed_seconds)
        else:
            # Actionable for a reason `_format_intraday` doesn't render
            # (an outer-level stop-coverage gap while the scan itself was
            # disabled/contended/found nothing) — the base formatter's own
            # `_append_intra_check_body` already renders that banner.
            own_message = _base_format_session_result(
                "intra_check",
                result,
                elapsed_seconds,
                error=None,
            )

    # Owner decision, 2026-09-17: the midday position review and the routine
    # half-hourly intra_check "nothing to report" ping must not fire minutes
    # apart. The ratified fix is to suppress that routine ping ONCE — at the
    # single intra_check tick that actually collides with the midday report —
    # not to silence intra_check for the whole 90-minute window.
    #
    # `_midday_collision_tick_minute()` derives that one tick from the two
    # existing authoritative sources (`SESSION_WINDOWS["midday"]` and
    # intra_check's own timer unit), so no new number is introduced and a
    # future cadence or window move carries the rule with it.
    #
    # Why only one tick and not the window: `midday` runs once per ET date,
    # not once per tick — `scripts/run_if_et_window.sh` writes a last-run
    # marker for every mode except `intra_check` and exits early for the rest
    # of the day. So the window holds exactly ONE midday report (its first
    # tick, 13:00 ET), and suppressing every quiet intra_check tick in the
    # window would instead swallow the 14:00 hour's guaranteed pulse (14:15
    # under #462's derived checkpoint minute) and leave a genuinely quiet day
    # with no message at all between 13:00 and 15:15 — breaking the owner's
    # standing "never a full clock hour without a message" requirement in
    # order to fix a two-messages-at-once complaint.
    #
    # Ordering matters: this runs BEFORE `_is_hourly_checkpoint` so that the
    # 13:00 hour's guaranteed pulse (13:15) does not re-send what the midday
    # report just said. Anything actionable (an order placed/filled/cancelled/
    # refused, a stop-coverage gap, a scan crash, `paid_analysis_suspended`,
    # a degraded-fill or coverage finding, ...) already set `own_message`
    # above and is never reached by this branch, in or out of the window.
    if own_message is None and _is_midday_collision_tick():
        return None

    if not _is_hourly_checkpoint():
        return own_message  # None here means: quiet tick, no send.

    summary = _format_hourly_desk_check(result, nested, elapsed_seconds)
    if own_message:
        return own_message + "\n\n" + summary
    return summary


def _movers_scanned_text(nested: dict | None) -> str:
    """Honest one-line account of whether the paid intraday scan even ran
    this tick — 'no arbitrary numbers': there is no mover COUNT to report
    for the common no-opportunity/disabled/contended outcomes (the
    pipeline's own status dicts don't carry one), so this says what is
    actually known instead of fabricating a figure.
    """
    if not isinstance(nested, dict):
        return "not run this tick"
    status = str(nested.get("status") or "")
    if status == "intraday_scan_no_opportunity":
        return "ran, no qualifying movers"
    if status == "intraday_scan_disabled":
        return "disabled"
    if status == "intraday_scan_lock_contended":
        return "skipped (lock contended)"
    if nested.get("pm_call_skipped"):
        return f"ran this tick. {no_room_sentence(nested)}"
    return "ran this tick"


def _read_hour_evidence(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> list[dict]:
    """Every tech-analyst analysis logged in roughly the last hour, read by
    TIME rather than by this tick's own `run_id` — the top-of-hour summary
    has to include the :30 tick's analysis too, and that ran under a
    different run_id. One row per symbol (most recent), so a symbol
    analyzed at both ticks (cooldown normally prevents this) is not shown
    twice. Read-only, fail-soft: any DB problem yields an empty list rather
    than blocking the guaranteed hourly message.
    """
    rows: dict[str, dict] = {}
    if not Path(_DB_PATH).exists():
        return []
    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        for row in conn.execute(
            "SELECT symbol, evidence_json FROM specialist_evidence "
            "WHERE agent_name = 'tech_analyst' AND kind = 'analysis' "
            "AND timestamp >= ? ORDER BY timestamp",
            (sql_utc_stamp_ago(minutes=hour_window_minutes),),
        ).fetchall():
            try:
                data = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            symbol = str(data.get("symbol") or row["symbol"] or "").upper()
            if symbol:
                rows[symbol] = data
    except Exception as exc:  # noqa: BLE001
        logger.error("hourly: evidence read failed", exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return list(rows.values())


def _read_hour_trades(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> list[dict]:
    """Every real (non-HOLD) broker trade row in roughly the last hour —
    read-only, fail-soft: any DB problem reads as an empty list rather than
    blocking the guaranteed hourly message (a real trade still sent its own
    immediate message regardless). The rows carry symbol, action, quantity,
    price and fill state so the hourly message can NAME what it counts
    (board item 89: a count is not information)."""
    if not Path(_DB_PATH).exists():
        return []
    conn = None
    try:
        uri = f"file:{Path(_DB_PATH).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        rows = conn.execute(
            "SELECT symbol, action, qty, price, fill_status, fill_qty, "
            "fill_price FROM trades WHERE timestamp >= ? "
            "AND UPPER(action) != 'HOLD' ORDER BY id",
            (sql_utc_stamp_ago(minutes=hour_window_minutes),),
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("hourly: trade read failed", exc_info=True)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _read_hour_trade_count(hour_window_minutes: int = _HOUR_WINDOW_MINUTES) -> int:
    """Count of real (non-HOLD) broker trades in roughly the last hour —
    see `_read_hour_trades`; a DB problem reads as 0."""
    return len(_read_hour_trades(hour_window_minutes))


def _format_hourly_desk_check(
    result: dict,
    nested: dict | None,
    elapsed: float,
    *,
    snap: dict[str, Any] | None = None,
    trade_count: int | None = None,
    trade_rows: list[dict] | None = None,
    period_line: str = "Covering the last hour",
    coverage_text: str | None = None,
    scanned_text: str | None = None,
) -> str:
    """The guaranteed top-of-hour message: what happened across BOTH ticks
    of the last hour, in plain English, even when neither one had anything
    that needed the owner's attention on its own.

    Every keyword argument defaults to exactly what the scheduled
    top-of-hour path has always done; they exist so the on-demand desk
    status (`format_desk_status` below, `scripts/desk_status.py`) can drive
    this same formatter from live broker truth and — crucially — say
    "not available" for the two fields an on-demand read genuinely cannot
    know, rather than borrowing a scheduled tick's answer or printing a
    reassuring zero.
    """
    run_id = result.get("run_id")
    if snap is None:
        snap = _read_run(run_id)
    if trade_count is None and trade_rows is None:
        # The scheduled path: read the hour's trades once and name them.
        trade_rows = _read_hour_trades()
    if trade_count is None:
        trade_count = len(trade_rows or [])
    trade_rows = [row for row in (trade_rows or []) if isinstance(row, dict)]
    outcome = _traded_word(trade_rows) if trade_count else "NO CHANGE"
    lines = [
        f"🕐 DESK CHECK · {fmt_time_12h(et_now())} · {outcome}",
        period_line,
    ]

    # P&L FIRST, directly under the heading — owner, 2026-09-18. It used to
    # sit below the hour's order list, four sections down.
    _new_section(lines, *_pnl_section_lines(result))

    positions = [row for row in (snap.get("positions") or []) if isinstance(row, dict)]
    risk_positions = [row for row in positions if str(row.get("symbol", "")).upper() not in _SWEEP_SYMBOLS]
    hour_rows = _read_hour_evidence()
    hour_symbols = [str(row.get("symbol", "")).upper() for row in hour_rows if row.get("symbol")]
    profiles = _profiles(hour_symbols, trade_rows, risk_positions)

    if trade_count == 0:
        _new_section(lines, "⏸️ No action this hour")
    elif trade_rows:
        # Board item 89: "N order(s) this hour" named nothing. Each order,
        # with its company and true fill state, from the same rows the
        # count came from — so the count can never exceed the list.
        trade_lines = [f"⚡ {trade_count} order(s) this hour — each already alerted on its own"]
        for row in trade_rows:
            action = str(row.get("action", "?")).upper()
            symbol = str(row.get("symbol", "?")).upper()
            qty = _number(row.get("fill_qty")) or _number(row.get("qty"))
            price = _number(row.get("fill_price")) or _number(row.get("price"))
            qty_text = f" {qty:g}" if qty is not None else ""
            price_text = f" @ ${price:,.2f}" if price is not None and price > 0 else ""
            trade_lines.append(
                f"   • {action} {_ticker_co(symbol, profiles)}{qty_text}{price_text} — "
                f"{_fill_state_plain(row.get('fill_status'))}"
            )
        _new_section(lines, *trade_lines)
    else:
        _new_section(
            lines,
            f"⚡ {trade_count} order(s) this hour — see the alert(s) already sent",
        )

    # Board item 89: "Positions held: N" named none of them. Every holding,
    # company alongside, with its own open profit or loss where recorded.
    position_lines = [f"💼 Positions held: {len(risk_positions)}"]
    for row in risk_positions:
        symbol = str(row.get("symbol", "?")).upper()
        pnl = _number(row.get("unrealized_pnl"))
        pnl_text = f" · {_fmt_signed_money(pnl)} open" if pnl is not None else ""
        position_lines.append(f"   • {_ticker_co(symbol, profiles)}{pnl_text}")
    _new_section(lines, *position_lines)

    _cov_start = len(lines)
    _append_coverage_gaps(lines, result)
    _cov_added = len(lines) > _cov_start
    _seal_section(lines, _cov_start)
    if not _cov_added:
        _new_section(lines, coverage_text or "🛡️ Stop coverage: OK")

    def _render_hour_signals(lines: list[str]) -> None:
        lines.append(f"🔎 Signals this hour: {len(hour_rows)} analyzed")
        priority = {"strong_buy": 0, "strong_sell": 0, "buy": 1, "sell": 1, "neutral": 2}
        ordered = sorted(
            hour_rows,
            key=lambda row: (
                priority.get(str(row.get("rating", "")).lower(), 3),
                str(row.get("symbol", "")),
            ),
        )
        # Uncapped, same reasoning as `_signal_rows`: the header just above
        # states the real count — the bullets below it must match, not
        # silently truncate to a smaller number. Company name inline
        # (`profiles`) since this is the only per-symbol listing in this
        # message — there is no separate DONE/LOOKED-AT section to have
        # already introduced it.
        for row in ordered:
            lines.append(_signal_row_line(row, profiles=profiles))

    detail_lines: list[str] = []
    if hour_rows:
        _new_block(detail_lines, _render_hour_signals)
    else:
        _new_section(
            detail_lines,
            f"🔎 Movers scanned: {scanned_text or _movers_scanned_text(nested)}",
        )
    _wrap_details(lines, detail_lines)

    _new_block(lines, _append_footer, snap, elapsed)
    return "\n".join(lines)


# === on-demand desk status (2026-09-17) ===
#
# Owner request: "send me the desk status now", at any time, without
# running a trading session. This deliberately holds NO message format of
# its own — it assembles read-only inputs and hands them to
# `_format_hourly_desk_check` above, the same function the scheduled
# top-of-hour message uses. Every future change to that formatter
# (wording, P&L line, sections) reaches this command with no edit here.
#
# Read-only, in the strong sense: it places and cancels nothing, calls no
# model, writes nothing to the database, and takes no session lock or
# once-per-day stamp. The scheduled sessions are unaffected by it.
_ON_DEMAND_PERIOD_LINE = "Covering the last hour · status requested on demand"
# Stop coverage is audited (and auto-repaired) by the scheduled sessions,
# which is a WRITING action — this command must not run it. So the answer
# is genuinely unknown here and says so, rather than printing the
# reassuring "OK" that the scheduled path prints when it has actually
# looked.
_ON_DEMAND_COVERAGE_TEXT = (
    "🛡️ Stop coverage: not available — the coverage audit runs with the scheduled sessions, not on demand"
)
_ON_DEMAND_SCANNED_TEXT = "not available — the movers scan runs with the scheduled sessions, not on demand"


def _position_rows_from_broker(positions: Any) -> list[dict[str, Any]]:
    """Broker `Position` objects (or plain dicts) → the same row shape
    `_read_run` puts in `snapshot["positions"]`, so the shared formatter
    cannot tell the difference. Shorts (negative qty) are kept, matching
    that query's `qty != 0`."""
    rows: list[dict[str, Any]] = []
    for position in positions or []:

        def _get(field: str) -> Any:
            if isinstance(position, dict):
                return position.get(field)
            return getattr(position, field, None)

        qty = _number(_get("qty"))
        if qty is None or qty == 0:
            continue
        rows.append(
            {
                "symbol": str(_get("symbol") or "").upper(),
                "qty": qty,
                "avg_entry": _number(_get("avg_entry")),
                "current_price": _number(_get("current_price")),
                "market_value": _number(_get("market_value")),
                "unrealized_pnl": _number(_get("unrealized_pnl")),
            }
        )
    rows.sort(key=lambda row: abs(row.get("market_value") or 0.0), reverse=True)
    return rows
