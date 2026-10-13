"""Trading-calendar module — single source of truth for US-trading-day semantics.

Everything that encodes "which trading day?" or "are we in session window X?"
goes through here. Prior to this module the same questions were answered in
five different places (util/time, scheduler, broker, pipeline, wrapper.sh)
and drifted — this consolidates them.

What belongs here:
- Timezone primitives (ET / UTC).
- "What trading day are we in?" via ET wall clock.
- "Is this a weekday?" — cheap, no network.
- Session-window definitions (morning/midday/evening/…) and window checks.
- The session-date key string used by daily_pnl and insights rows.

What does NOT belong here:
- Alpaca-calendar queries for *market holidays*. Holiday detection needs a
  live broker connection, so `is_trading_day()` stays on `AlpacaBroker`.
  Callers that only need a weekday heuristic use `is_weekday()` here.

KNOWN, UNFIXED, AND LARGER THAN IT LOOKS — the session windows are wall
clock only. `SESSION_WINDOWS` below pins every phase to a fixed minute of
the ET day, and `scripts/run_if_et_window.sh` carries a second hand-copied
table of the same numbers (pinned to this one by
`tests/test_trading_calendar.py::test_wrapper_windows_match_session_windows`).
Neither reads a calendar; the wrapper's only calendar awareness is a
weekday short-circuit. On an early-close day (13:00 ET) the consequence is
not a stale bar, it is that the phases run against a shut market: `midday`
(13:00-14:30) and `close` (15:30-16:00) fire entirely AFTER the close, so
the act-on-trigger end-of-day position review never happens while the
desk can still trade, and `intra_check` keeps firing its paid ticks until
16:00. On a full market holiday the wrapper fires every weekday phase.

The fix is to express each phase relative to the session's real open and
close, resolved per day from the broker calendar that `src/market_session.py`
already reads, and to delete the wrapper's copy of the table rather than
keep two. That is deliberately NOT attempted piecemeal: the fixed minute
bounds are also read by `src/scheduler.py` (intra_check trigger), both
watchdogs (`src/silence_watchdog.py`, `src/coverage_watchdog.py`, which
derive expected-tick counts from them) and `src/config/llm_cost.py` (the
paid-session budget), and every one of those becomes session-dependent at
the same moment. Sized at roughly 400-600 changed lines across those six
modules plus the wrapper and the pinning test. Nothing here detects the
condition, because a detector is not the fix.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

SessionMode = Literal["earnings_preprocess", "morning", "intra_check", "midday", "close", "evening"]

# Session windows as (start_minute_of_day, end_minute_of_day) in ET.
# These are the authoritative source. `scripts/run_if_et_window.sh` has the
# same table hardcoded for zero-dep launchd gating; `test_trading_calendar.py`
# asserts the two stay in sync.
SESSION_WINDOWS: dict[str, tuple[int, int]] = {
    "earnings_preprocess": (480, 555),  # 08:00 - 09:15 ET
    "morning": (570, 720),  # 09:30 - 12:00 ET
    # "no LLM" here was FALSE and is corrected 2026-09-23: intra_check is the
    # desk's largest paid cost centre, not a deterministic tick. Measured on
    # the production DB (llm_budget_sessions): 13-14 paid sessions a day,
    # $0.5672 of the day's $0.7883 on 2026-09-22 (72%) and $2.3839 of $2.6478
    # on 2026-09-21 (90%). Two separate derivations were built on the wrong
    # label before anyone checked. Whether it SHOULD be spending that much is
    # a live question and not settled here.
    "intra_check": (
        570,
        960,
    ),  # 09:30 - 16:00 ET  (P&L circuit-breaker + on-trigger paid review, every 30min tick; NOT subject to once-per-day guard)
    "midday": (780, 870),  # 13:00 - 14:30 ET  (position reviewer, patient)
    "close": (
        930,
        960,
    ),  # 15:30 - 16:00 ET  (position reviewer, act-on-trigger; 30min width guarantees a 30-min launchd tick lands inside regardless of phase)
    "evening": (1200, 1320),  # 20:00 - 22:00 ET  (reporting only)
}


# US equities regular (core) trading session, minutes of day in ET:
# 09:30-16:00. Source: NYSE "Hours & Calendars" (core trading session
# 9:30 a.m.-4:00 p.m. ET), the same bounds `SESSION_WINDOWS` already uses
# for the intra_check window. Early-close days (13:00 ET) are NOT modelled
# here — the holiday/early-close calendar needs a broker connection (see
# module docstring); for THIS constant's own users that errs toward "bar not
# yet complete" (stale-but-labelled), never toward treating a partial bar as
# complete, but read the docstring before concluding the early close is
# handled: the session-window table above has a worse exposure than this one.
REGULAR_SESSION_OPEN_MIN = 570  # 09:30 ET
REGULAR_SESSION_CLOSE_MIN = 960  # 16:00 ET


def et_now() -> datetime:
    """Current instant in US/Eastern: ``utc_now`` converted, never a second read."""
    return utc_now().astimezone(ET)


def utc_now() -> datetime:
    """Current instant as a timezone-aware datetime in UTC: the same clock as ``et_now``.

    THE desk clock -- the ONE OS read. ``et_now`` converts it. Every row stamp, prune cutoff and day boundary is read
    from here (or ``et_now``), never from SQLite's ``datetime('now')`` and
    never from a bare ``datetime.now`` elsewhere -- ``scripts/one_clock_guard.py``
    refuses both outside this module.
    """
    return datetime.now(UTC)


SQL_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def sql_utc_stamp(when: datetime | None = None) -> str:
    """``when`` (default: now) in the exact text SQLite's ``datetime('now')`` stores.

    Naive-UTC, second precision, ``'YYYY-MM-DD HH:MM:SS'``. Bind it as a
    parameter wherever SQL used to read its own clock, so a row written and a
    row compared are dated by the one Python clock -- the one a test can freeze.
    A naive ``when`` is taken as UTC, the way stored stamps are read back.
    """
    if when is None:
        when = utc_now()
    elif when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime(SQL_STAMP_FORMAT)


def sql_utc_stamp_ago(*, days: float = 0.0, hours: float = 0.0, minutes: float = 0.0) -> str:
    """A cutoff this far before now, in SQLite's stored format (replaces ``datetime('now', '-N days')``)."""
    return sql_utc_stamp(utc_now() - timedelta(days=days, hours=hours, minutes=minutes))


def et_today() -> date:
    """Current trading-day date in US/Eastern.

    Example: host in SGT at 2026-04-18 09:00 SGT → ET is 2026-04-17 21:00 →
    this returns date(2026, 4, 17) — the trading day that just ended.
    """
    return et_now().date()


def to_et(when: datetime) -> datetime:
    """Convert any datetime (naive-UTC or aware) into US/Eastern-aware.

    Naive datetimes are assumed to be UTC — that's how SQLite stores
    `datetime('now')` and how most log timestamps land.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(ET)


def session_date_key(when: datetime | None = None) -> str:
    """ET-trading-day key as 'YYYY-MM-DD'.

    The shared string key for all per-day tables: daily_pnl, insights,
    snapshot directories. Using this everywhere ensures a host in SGT and
    a host in NYC index the same trading session under the same key.
    """
    d = to_et(when).date() if when is not None else et_today()
    return d.isoformat()


def is_weekday(d: date | None = None) -> bool:
    """True when `d` (defaults to today in ET) is Mon-Fri.

    This is a CHEAP weekday check — it does NOT know about market holidays.
    For authoritative "will the exchange be open?" use `broker.is_trading_day()`,
    which queries Alpaca's official calendar.
    """
    target = d if d is not None else et_today()
    return target.weekday() < 5  # Mon=0 .. Sun=6


def trading_sessions_held(start: date, end: date) -> int:
    """Weekend-aware count of trading sessions between two calendar dates.

    Counts weekdays (Mon-Fri) strictly AFTER `start` up to and including
    `end` — i.e. how many new sessions' worth of price action have occurred
    since `start`. A same-day round trip is 0; consecutive weekdays give the
    same answer as a plain calendar-day count; a Friday-to-Monday hold gives
    1, not the 2-3 calendar days that elapsed, because only one session's
    worth of real price action happened in between.

    2026-09-04 audit follow-up: `exit_guard.noise_band_atr` scales its band
    by `sqrt(sessions)`, citing `levels.py::derive_structural_target`'s
    `sqrt(expected_horizon_sessions)` as precedent — but that precedent
    counts TRADING SESSIONS, and the caller in `pipeline.py` was passing
    calendar days (`(today - entry_date).days`), which include weekends.
    A Friday-to-Monday hold was getting `sqrt(3)` instead of `sqrt(1)`,
    over-widening the band after every weekend/holiday. This function is
    the fix: callers that want the levels.py-style scaling should count
    SESSIONS with this, not calendar days.

    This is a CHEAP approximation — Mon-Fri only, no US market-holiday
    calendar (per the module docstring, holiday detection needs a live
    broker connection via `AlpacaBroker.is_trading_day`). A market holiday
    inside the range is silently counted as a session, so this can still
    overstate the true session count by one per holiday crossed — a small
    residual gap versus the pure weekend case, which it fixes exactly.

    Returns 0 if `end` is not after `start`.
    """
    from datetime import timedelta

    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


_QUARTER_END_MONTHS = (3, 6, 9, 12)


def quarter_of(d: date | None = None) -> int:
    """Return the calendar quarter (1-4) that `d` (default today-ET) falls in."""
    target = d if d is not None else et_today()
    return (target.month - 1) // 3 + 1


def quarter_label(d: date | None = None) -> str:
    """'YYYY-QN' label — the key used by data/evolution/ subdirectories."""
    target = d if d is not None else et_today()
    return f"{target.year}-Q{quarter_of(target)}"


def _minute_of_day(when: datetime) -> int:
    et = to_et(when)
    return et.hour * 60 + et.minute


def in_session_window(mode: SessionMode, when: datetime | None = None) -> bool:
    """True when `when` (defaults to now) falls inside this mode's ET window.

    Inclusive of both endpoints — matches wrapper.sh's `-lt`/`-gt` semantics.
    Weekend short-circuits to False, mirroring the wrapper.
    """
    now = when if when is not None else et_now()
    if not is_weekday(to_et(now).date()):
        return False
    window = SESSION_WINDOWS.get(mode)
    if window is None:
        raise ValueError(f"unknown session mode: {mode}")
    lo, hi = window
    minute = _minute_of_day(now)
    return lo <= minute <= hi


def in_regular_session(when: datetime | None = None) -> bool:
    """True while the regular US equities session is in progress.

    [09:30, 16:00) ET on a weekday. While this is True, TODAY's daily bar is
    still forming: any price-vs-level comparison must use a live price, and
    the completed-bar series ends at the previous session. Holidays are not
    known here (weekday heuristic); a holiday reads as "in session" but the
    live-price freshness check (`live_price_is_today`) then marks the price
    stale because no trade prints today.
    """
    now = when if when is not None else et_now()
    if not is_weekday(to_et(now).date()):
        return False
    minute = _minute_of_day(now)
    return REGULAR_SESSION_OPEN_MIN <= minute < REGULAR_SESSION_CLOSE_MIN


def last_completed_bar_date(when: datetime | None = None) -> date:
    """Latest calendar date whose DAILY bar can be complete at `when`.

    On a weekday at/after 16:00 ET that is today; at any other time it is
    the previous calendar day. The result is a filter bound (`bar.date <=
    this`), not a claim that a bar exists on that date — weekends and
    holidays simply have no bar, so the bound needs no holiday calendar.
    """
    from datetime import timedelta

    now = when if when is not None else et_now()
    d = to_et(now).date()
    if is_weekday(d) and _minute_of_day(now) >= REGULAR_SESSION_CLOSE_MIN:
        return d
    return d - timedelta(days=1)


def live_price_is_today(last_trade_at, when: datetime | None = None) -> bool:
    """True when a provider trade timestamp falls on the current ET date.

    A snapshot's last trade from a prior session (holiday, halt, feed gap)
    is NOT a live price for today and must be labelled stale, never shown as
    current. A missing or naive timestamp is treated as not-today (unknown
    freshness fails visible rather than passing as live).
    """
    if last_trade_at is None or not isinstance(last_trade_at, datetime):
        return False
    if last_trade_at.tzinfo is None:
        return False
    now = when if when is not None else et_now()
    return to_et(last_trade_at).date() == to_et(now).date()


def format_window(mode: SessionMode) -> str:
    """Human-friendly 'HH:MM-HH:MM ET' rendering — for logs and tests."""
    lo, hi = SESSION_WINDOWS[mode]
    return f"{time(lo // 60, lo % 60).strftime('%H:%M')}-{time(hi // 60, hi % 60).strftime('%H:%M')} ET"
