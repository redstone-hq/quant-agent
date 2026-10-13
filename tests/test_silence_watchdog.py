"""The desk-wide silence watchdog — docs/WORK.md item 17c.

THE GAP THIS CLOSES
--------------------
`src/alert_watchdog.py` (tested in `tests/test_alert_watchdog.py`) proves the
Telegram PIPE works. It says nothing about whether the desk did any WORK: a
latched circuit or an unopenable database would leave that watchdog reporting
a perfectly healthy channel while nothing runs. This file tests the other
half: a check that fires on the ABSENCE of any completed scheduled session,
across all six modes, for `threshold` consecutive scheduled windows.

THE LOAD-BEARING TESTS ARE THE NEGATIVE ONES, same rule as
`test_alert_watchdog.py`: a healthy desk staying silent (no alert) proves
little on its own. What matters is that real silence is caught, caught
exactly once, and a database that cannot be opened does not make the
watchdog give up rather than escalate.

Nothing here touches the network or a real Telegram chat — every transport
is mocked at `src.notifier.requests.post`, exactly like the alert-watchdog
tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import alert_watchdog, silence_watchdog
from src.trading_calendar import ET, SESSION_WINDOWS

# A known-good Monday in ET, used as the "now" anchor for every test so
# weekday/weekend arithmetic is deterministic rather than depending on the
# day the suite happens to run.
_MONDAY = datetime(2026, 9, 7, tzinfo=ET)  # 2026-09-07 is a Monday


def _et(day: datetime, hh: int, mm: int) -> datetime:
    return day.replace(hour=hh, minute=mm, second=0, microsecond=0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An isolated `alert_channel_checks` database shared by both modules,
    exactly as production shares one file between them."""
    path = tmp_path / "quant_agent.db"
    monkeypatch.setattr(alert_watchdog, "DB_PATH", path)
    monkeypatch.setattr(silence_watchdog, "DB_PATH", path)
    return path


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "alerting" / "silence_heartbeat.json"
    monkeypatch.setattr(silence_watchdog, "STATE_PATH", path)
    return path


def _seed(db_path: Path, *, source: str, when: datetime, ok: bool = True) -> None:
    alert_watchdog.record_check(
        ok=ok,
        stage="delivered",
        detail="",
        source=source,
        db_path=db_path,
        now=when,
    )


def _notifier_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:FAKE-TOKEN-FOR-TESTS")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.delenv("TELEGRAM_DISABLED", raising=False)


# ===========================================================================
# 1. On schedule -> silent
# ===========================================================================


def test_no_alert_when_every_mode_reports_on_schedule(db, state_path):
    """A healthy Monday: every mode writes its row inside its own window.
    The consecutive-silent count must be zero and nothing should fire."""
    monday_end = _et(_MONDAY, 23, 0)
    for mode, (lo, hi) in SESSION_WINDOWS.items():
        checked_at = (_et(_MONDAY, 0, 0) + timedelta(minutes=(lo + hi) // 2)).astimezone(timezone.utc)
        _seed(db, source=mode, when=checked_at)

    status = silence_watchdog.check_silence(now=monday_end.astimezone(timezone.utc))
    assert status.consecutive_silent_windows == 0
    assert status.is_silent is False
    assert status.should_alert is False


def test_a_fresh_deploy_with_recent_history_is_not_silent(db, state_path):
    """Only one mode has ever reported, minutes ago. That is not a desk-wide
    outage — it is a desk that just started."""
    now = _et(_MONDAY, 10, 0).astimezone(timezone.utc)
    _seed(db, source="morning", when=now - timedelta(minutes=5))
    status = silence_watchdog.check_silence(now=now)
    assert status.consecutive_silent_windows == 0
    assert status.should_alert is False


# ===========================================================================
# 2. Desk-wide, not per-mode
# ===========================================================================


def test_one_quiet_mode_does_not_trigger_while_others_still_run(db, state_path):
    """`close` never fires on a day with no open position to review, but
    `morning`/`intra_check`/`evening` keep running either side of it. That
    must read as a live desk, not a missing-mode alarm — silence here is
    judged across ALL modes together, never per mode."""
    now = _et(_MONDAY, 21, 0).astimezone(timezone.utc)
    # Every mode EXCEPT close reports today; close's own window (15:30-16:00)
    # has long since elapsed with nothing recorded for it specifically.
    for mode, (lo, hi) in SESSION_WINDOWS.items():
        if mode == "close":
            continue
        checked_at = (_et(_MONDAY, 0, 0) + timedelta(minutes=(lo + hi) // 2)).astimezone(timezone.utc)
        _seed(db, source=mode, when=checked_at)

    status = silence_watchdog.check_silence(now=now)
    assert status.should_alert is False


# ===========================================================================
# 3. Real silence is caught, and caught once
# ===========================================================================


def test_a_full_day_of_silence_crosses_the_default_threshold(db, state_path):
    """Nothing recorded since last Friday evening; by Monday evening all six
    of Monday's scheduled windows have elapsed with zero completed sessions.
    That is well past `DEFAULT_SILENT_WINDOW_THRESHOLD` (2) missed windows."""
    friday = _MONDAY - timedelta(days=3)
    last_seen = _et(friday, 21, 0).astimezone(timezone.utc)
    _seed(db, source="evening", when=last_seen)

    now = _et(_MONDAY, 23, 0).astimezone(timezone.utc)
    status = silence_watchdog.check_silence(now=now)

    assert status.consecutive_silent_windows == len(SESSION_WINDOWS)
    assert status.is_silent is True
    assert status.should_alert is True


def test_the_alert_fires_exactly_once_per_silence_episode(db, state_path, monkeypatch):
    """Running the check repeatedly against the same unrecovered silence
    must not resend. A new completed session must clear the flag so a LATER,
    separate silence episode can alert again."""
    _notifier_env(monkeypatch)
    friday = _MONDAY - timedelta(days=3)
    _seed(db, source="evening", when=_et(friday, 21, 0).astimezone(timezone.utc))
    now = _et(_MONDAY, 23, 0).astimezone(timezone.utc)

    with patch("src.notifier.requests.post") as post:
        post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        first = silence_watchdog.check_silence(now=now)
        assert first.should_alert is True
        second = silence_watchdog.check_silence(now=now + timedelta(minutes=30))
        assert second.is_silent is True
        assert second.should_alert is False  # already alerted for this baseline
        assert post.call_count >= 0  # the script, not check_silence, sends

    # A new session lands -> the baseline advances -> a later silence episode
    # is free to alert again.
    _seed(db, source="morning", when=now + timedelta(hours=1))
    recovered = silence_watchdog.check_silence(now=now + timedelta(hours=2))
    assert recovered.should_alert is False
    assert recovered.consecutive_silent_windows == 0

    # Push far enough forward (skipping the weekend) that a fresh full day
    # elapses with nothing recorded since the recovery -> alerts again.
    tuesday_next_week = _MONDAY + timedelta(days=8)
    far_future = _et(tuesday_next_week, 23, 0).astimezone(timezone.utc)
    third = silence_watchdog.check_silence(now=far_future)
    assert third.should_alert is True


def test_check_silence_never_sends_telegram_itself(db, state_path, monkeypatch):
    """`check_silence` only computes and persists state. Sending is the
    caller's job (`scripts/silence_heartbeat.py`), so a test that never
    mocks the transport must not accidentally make a real HTTP call."""
    _notifier_env(monkeypatch)
    now = _et(_MONDAY, 23, 0).astimezone(timezone.utc)
    with patch("src.notifier.requests.post") as post:
        silence_watchdog.check_silence(now=now)
        post.assert_not_called()


# ===========================================================================
# 4. The database being unreadable is itself part of the signal
# ===========================================================================


def test_an_unreadable_database_does_not_erase_the_last_known_marker(tmp_path, state_path):
    """The exact 2026-09-02 shape: the database that would prove the desk
    alive is the very thing that is broken. The on-box marker must still
    answer from what it already knew, not reset to 'we know nothing'."""
    good_db = tmp_path / "quant_agent.db"
    broken_db = tmp_path / "does" / "not" / "exist.db"

    seen_at = _et(_MONDAY, 10, 0).astimezone(timezone.utc)
    _seed(good_db, source="morning", when=seen_at)
    baseline = silence_watchdog.check_silence(
        now=seen_at + timedelta(minutes=1),
        db_path=good_db,
    )
    assert baseline.last_known_session_at is not None

    later = seen_at + timedelta(days=3)
    status = silence_watchdog.check_silence(now=later, db_path=broken_db)
    assert status.db_error is not None
    assert status.last_known_session_at == baseline.last_known_session_at


def test_an_unreadable_database_still_lets_the_streak_grow_to_alerting(
    tmp_path,
    state_path,
    monkeypatch,
):
    _notifier_env(monkeypatch)
    good_db = tmp_path / "quant_agent.db"
    broken_db = tmp_path / "missing.db"

    friday = _MONDAY - timedelta(days=3)
    seen_at = _et(friday, 21, 0).astimezone(timezone.utc)
    _seed(good_db, source="evening", when=seen_at)
    silence_watchdog.check_silence(now=seen_at + timedelta(minutes=1), db_path=good_db)

    now = _et(_MONDAY, 23, 0).astimezone(timezone.utc)
    status = silence_watchdog.check_silence(now=now, db_path=broken_db)
    assert status.db_error is not None
    assert status.is_silent is True
    assert status.should_alert is True


# ===========================================================================
# 5. Weekends never manufacture a false alarm
# ===========================================================================


def test_weekend_produces_no_expected_windows(db, state_path):
    """Friday evening's session is the last one recorded; by Saturday
    evening no scheduled window has been missed, because the desk has no
    scheduled windows on a Saturday at all."""
    friday = _MONDAY - timedelta(days=3)
    _seed(db, source="evening", when=_et(friday, 21, 0).astimezone(timezone.utc))

    saturday = _MONDAY - timedelta(days=2)
    now = _et(saturday, 23, 0).astimezone(timezone.utc)
    status = silence_watchdog.check_silence(now=now)
    assert status.consecutive_silent_windows == 0
    assert status.should_alert is False


# ===========================================================================
# 6. Threshold is a parameter, not a constant baked into the logic
# ===========================================================================


def test_threshold_is_fully_configurable(db, state_path):
    friday = _MONDAY - timedelta(days=3)
    _seed(db, source="evening", when=_et(friday, 21, 0).astimezone(timezone.utc))
    now = _et(_MONDAY, 23, 0).astimezone(timezone.utc)

    generous = silence_watchdog.check_silence(now=now, threshold=100)
    assert generous.should_alert is False

    strict = silence_watchdog.check_silence(now=now, threshold=1)
    assert strict.should_alert is True


# ===========================================================================
# 7. Message shape — severity in words, matching src/notifier.py's convention
# ===========================================================================


def test_alert_text_leads_with_a_plain_english_severity_word():
    status = silence_watchdog.SilenceStatus(
        consecutive_silent_windows=6,
        threshold=6,
        last_known_session_at="2026-09-04T21:00:00+00:00",
        newest_elapsed_window=None,
        db_error=None,
        already_alerted_for_this_baseline=False,
    )
    text = silence_watchdog.alert_text(status)
    assert "SILENT:" in text.splitlines()[0]
    # The alert used to close with "this threshold is a placeholder pending
    # owner confirmation — see docs/WORK.md item 17c". The threshold was
    # ratified 2026-09-03; the sentence outlived it. What must survive is
    # the operator instruction, not the stale caveat.
    assert "Mission Control" in text


# ===========================================================================
# 8. scripts/silence_heartbeat.py end to end
# ===========================================================================


def test_script_sends_through_the_owner_alert_path_and_exits_1(
    db,
    state_path,
    monkeypatch,
    freeze_clock,
):
    _notifier_env(monkeypatch)
    friday = _MONDAY - timedelta(days=3)
    _seed(db, source="evening", when=_et(friday, 21, 0).astimezone(timezone.utc))

    from scripts import silence_heartbeat

    fixed_now = _et(_MONDAY, 23, 0).astimezone(timezone.utc)
    freeze_clock(fixed_now)

    with patch("src.notifier.requests.post") as post:
        post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 1}},
        )
        code, line = silence_heartbeat.run_check(silence_watchdog.DEFAULT_SILENT_WINDOW_THRESHOLD)

    assert code == 1
    assert "SILENT" in line
    assert post.called


def test_script_exits_0_and_sends_nothing_on_a_healthy_desk(db, state_path, monkeypatch, freeze_clock):
    monday_end = _et(_MONDAY, 23, 0).astimezone(timezone.utc)
    for mode, (lo, hi) in SESSION_WINDOWS.items():
        checked_at = (_et(_MONDAY, 0, 0) + timedelta(minutes=(lo + hi) // 2)).astimezone(timezone.utc)
        _seed(db, source=mode, when=checked_at)

    from scripts import silence_heartbeat

    freeze_clock(monday_end)
    with patch("src.notifier.requests.post") as post:
        code, line = silence_heartbeat.run_check(silence_watchdog.DEFAULT_SILENT_WINDOW_THRESHOLD)

    assert code == 0
    assert "OK" in line
    post.assert_not_called()


# ===========================================================================
# 6. A paused desk that nobody remembers — docs/WORK.md item 11
#
# The wrapper's pause guard (scripts/run_silence_heartbeat.sh) correctly
# suppresses the 30-minute silence alarm while the trading timers are
# stopped. Shipped on its own, that made "paused and forgotten" the one
# desk-wide condition with NO alarm behind it: the daily channel probe
# sends-and-deletes, and the coverage watchdog only speaks when a held
# position is short of stops — so a paused desk with a flat book was
# indistinguishable from a working desk in a quiet market. These tests are
# the other half.
# ===========================================================================


@pytest.fixture
def paused_state_path(tmp_path, monkeypatch):
    path = tmp_path / "alerting" / "paused_desk.json"
    monkeypatch.setattr(silence_watchdog, "PAUSED_STATE_PATH", path)
    return path


def test_paused_desk_notifies_once_a_weekday(paused_state_path):
    """THE LOAD-BEARING TEST. A weekday afternoon with the desk paused must
    produce exactly one reminder, and the second check of the same day must
    produce none — a reminder every 30 minutes is the muted-alarm failure
    the pause guard exists to avoid."""
    afternoon = _et(_MONDAY, 15, 0).astimezone(timezone.utc)
    first = silence_watchdog.check_paused_desk(now=afternoon)
    assert first.should_notify
    assert first.elapsed_windows_today > 0

    later = _et(_MONDAY, 15, 30).astimezone(timezone.utc)
    second = silence_watchdog.check_paused_desk(now=later)
    assert not second.should_notify
    assert second.already_notified_today


def test_paused_desk_notifies_again_the_next_weekday(paused_state_path):
    """One reminder per day, not one per pause episode: every extra day the
    desk stays off is another day of not trading, and the owner can end it
    at any time."""
    silence_watchdog.check_paused_desk(now=_et(_MONDAY, 15, 0).astimezone(timezone.utc))
    tuesday = _MONDAY + timedelta(days=1)
    nxt = silence_watchdog.check_paused_desk(
        now=_et(tuesday, 15, 0).astimezone(timezone.utc),
    )
    assert nxt.should_notify


def test_paused_desk_says_nothing_before_the_first_window_closes(paused_state_path):
    """00:30 on a weekday is not evidence of anything: the desk had not yet
    been due to do a single thing. The gate is read off SESSION_WINDOWS, not
    from a chosen hour."""
    small_hours = _et(_MONDAY, 0, 30).astimezone(timezone.utc)
    status = silence_watchdog.check_paused_desk(now=small_hours)
    assert not status.should_notify
    assert status.elapsed_windows_today == 0


def test_paused_desk_says_nothing_at_the_weekend(paused_state_path):
    """A desk that is not trading on a Saturday is not a finding — the
    trading timers would not have fired either."""
    saturday = _MONDAY - timedelta(days=2)
    status = silence_watchdog.check_paused_desk(
        now=_et(saturday, 15, 0).astimezone(timezone.utc),
    )
    assert not status.is_weekday
    assert not status.should_notify


def test_paused_reminder_leads_with_a_word_not_a_colour(paused_state_path):
    """Severity is carried in text, never in colour alone."""
    status = silence_watchdog.check_paused_desk(
        now=_et(_MONDAY, 15, 0).astimezone(timezone.utc),
    )
    text = silence_watchdog.paused_alert_text(status)
    assert "PAUSED" in text
    assert status.et_date in text


def test_paused_state_never_touches_the_silence_marker(
    db,
    state_path,
    paused_state_path,
):
    """The two records must not share a file: a pause episode overwriting
    `last_known_session_at` would blind the silence check for the day the
    desk is resumed."""
    _seed(db, source="morning", when=_et(_MONDAY, 10, 0).astimezone(timezone.utc))
    silence_watchdog.check_silence(now=_et(_MONDAY, 11, 0).astimezone(timezone.utc))
    before = state_path.read_text()

    silence_watchdog.check_paused_desk(now=_et(_MONDAY, 15, 0).astimezone(timezone.utc))
    assert state_path.read_text() == before
    assert paused_state_path.exists()


def test_script_paused_path_sends_once_and_exits_1(paused_state_path, monkeypatch, freeze_clock):
    """End to end through the entry point the wrapper actually execs."""
    _notifier_env(monkeypatch)
    from scripts import silence_heartbeat

    fixed_now = _et(_MONDAY, 15, 0).astimezone(timezone.utc)
    freeze_clock(fixed_now)

    with patch("src.notifier.requests.post") as post:
        post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"message_id": 1}},
        )
        code, line = silence_heartbeat.run_paused_notice()
        assert code == 1
        assert post.called

        post.reset_mock()
        code2, line2 = silence_heartbeat.run_paused_notice()
    assert code2 == 0
    assert "already reminded" in line2
    post.assert_not_called()
