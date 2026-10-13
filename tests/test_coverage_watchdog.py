"""Stop-coverage watchdog for a paused desk — docs/INCIDENT_HISTORY.md 2026-09-12.

THE REAL CASE, reproduced exactly: ORCL 5.3089 shares held, one GTC stop
for 5.0 at the broker, the 0.3089-share DAY leg lapsed at the close on
2026-09-02, and no session ran on any trading day afterwards because the
timers were paused. Every record on the box called that "expected
overnight"; nobody was told for six sessions.

THE LOAD-BEARING TESTS ARE THE NEGATIVE ONES, as in the sibling watchdog
suites: the ordinary healthy morning (remainder lapsed, session ran
yesterday) MUST stay silent — the owner ratified that a nightly lapse does
not page — and the alert must fire exactly when the design's own
precondition ("the next session re-places it") has been false for a whole
trading session.

Nothing here touches the network, the broker, or a real Telegram chat.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import alert_watchdog, coverage_watchdog
from src.trading_calendar import ET

# 2026-09-11 was a Friday; the check runs 06:15 ET the next morning
# (Saturday) and must judge Friday's session. Saturday is deliberate: the
# most-recent-trading-day walk has to skip nothing here, and the Monday
# case below covers the weekend skip.
_SAT_0615 = datetime(2026, 9, 12, 6, 15, tzinfo=ET).astimezone(timezone.utc)
_MON_0615 = datetime(2026, 9, 14, 6, 15, tzinfo=ET).astimezone(timezone.utc)
_FRI = datetime(2026, 9, 11, tzinfo=ET)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "quant_agent.db"
    monkeypatch.setattr(alert_watchdog, "DB_PATH", path)
    monkeypatch.setattr(coverage_watchdog, "DB_PATH", path)
    return path


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "alerting" / "coverage_heartbeat.json"
    monkeypatch.setattr(coverage_watchdog, "STATE_PATH", path)
    return path


def _seed_session(db_path, *, source: str, when: datetime) -> None:
    alert_watchdog.record_check(
        ok=True,
        stage="delivered",
        detail="",
        source=source,
        db_path=db_path,
        now=when,
    )


def _orcl_broker(held=5.3089, stops=(5.0,), price=150.28, trading_days=True):
    """The live 2026-09-12 broker state, read-only: 5.3089 ORCL held, one
    GTC stop-limit for 5.0, plus the stopless SGOV cash sweep."""
    broker = MagicMock()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="ORCL", qty=held, current_price=price),
        SimpleNamespace(symbol="SGOV", qty=89.0, current_price=100.52),
    ]

    def _snapshot(symbol, side="sell"):
        if symbol == "ORCL":
            return True, [{"id": f"stop-{i}", "qty": q, "stop_price": 137.53} for i, q in enumerate(stops)]
        return True, []

    broker.snapshot_protective_stops.side_effect = _snapshot
    broker.is_trading_day.return_value = trading_days

    # The exchange calendar the placement gate reads. A MagicMock's default
    # answer here would be a Mock object, and comparing one to a datetime
    # raises — which the gate treats as "shut", so the default is already
    # safe. Wiring real edges anyway makes the market-hours tests say what
    # they mean instead of relying on a TypeError.
    def _edge(hour, minute):
        def _get(on_date=None):
            if not trading_days or on_date is None:
                return None
            return datetime(
                on_date.year,
                on_date.month,
                on_date.day,
                hour,
                minute,
                tzinfo=ET,
            )

        return _get

    broker.get_session_open.side_effect = _edge(9, 30)
    broker.get_session_close.side_effect = _edge(16, 0)
    return broker


# ===========================================================================
# 1. The real incident: fires
# ===========================================================================


def test_the_orcl_case_alerts_when_no_session_ran_on_the_last_trading_day(db, state_path):
    """ORCL 5.3089 held, 5.0 covered, timers paused: no session on Friday.
    This is the state the box was in on 2026-09-12 and nothing said so.
    The last real session was the 2026-09-02 evening run (00:03 UTC 09-03)."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _orcl_broker()
    status = coverage_watchdog.check_coverage(
        broker,
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"
    assert status.session_ran is False
    assert [g.symbol for g in status.gaps] == ["ORCL"]
    gap = status.gaps[0]
    assert gap.held_qty == pytest.approx(5.3089)
    assert gap.covered_qty == pytest.approx(5.0)
    assert gap.uncovered_qty == pytest.approx(0.3089)
    assert gap.unprotected_value == pytest.approx(46.42, abs=0.01)  # 0.3089 x $150.28
    assert status.should_alert is True
    text = coverage_watchdog.alert_text(status)
    assert "UNPROTECTED" in text and "ORCL" in text and "$46.42" in text
    assert "Nothing has been sold, resized or cancelled" in text
    # 06:15 ET is hours before the bell: the alert must say the stop goes
    # back at the open, and must not pretend the overnight gap is solved.
    assert "The market is shut right now" in text
    assert "unprotected OVERNIGHT no matter what" in text
    assert status.repairs == []
    # Read-only against the broker, always.
    assert not broker.submit_order.called
    assert not broker.cancel_order_by_id.called


def test_the_cash_sweep_vehicle_is_never_a_gap(db, state_path):
    """SGOV deliberately carries no stop. Flagging it would page every day."""
    broker = _orcl_broker(stops=(5.0, 0.3089))
    status = coverage_watchdog.check_coverage(
        broker,
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.gaps == []
    assert status.should_alert is False


# ===========================================================================
# 2. The healthy morning: silent (owner-ratified)
# ===========================================================================


def test_a_lapsed_remainder_after_a_normal_session_day_stays_silent(db, state_path):
    """The DAY leg lapses every night on a healthy desk. A session ran during
    Friday's cash session, so Monday's sweep owns the re-placement. NOT an
    alert — this is the case the owner said must never page."""
    _seed_session(db, source="intra_check", when=_FRI.replace(hour=12, minute=1).astimezone(timezone.utc))
    broker = _orcl_broker()
    status = coverage_watchdog.check_coverage(
        broker,
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.gaps and status.session_ran is True
    assert status.should_alert is False
    assert "not alerting" in coverage_watchdog.status_line(status)


def test_only_a_session_inside_cash_hours_counts_as_evidence(db, state_path):
    """The evening run sees a shut market and places nothing (by design), so
    an evening row alone does not prove the remainder was re-covered."""
    _seed_session(db, source="evening", when=_FRI.replace(hour=20, minute=1).astimezone(timezone.utc))
    _seed_session(db, source="earnings_preprocess", when=_FRI.replace(hour=8, minute=1).astimezone(timezone.utc))
    status = coverage_watchdog.check_coverage(
        _orcl_broker(),
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.session_ran is False
    assert status.should_alert is True


def test_the_heartbeat_probe_itself_is_not_a_session(db, state_path):
    _seed_session(db, source="heartbeat_timer", when=_FRI.replace(hour=12).astimezone(timezone.utc))
    status = coverage_watchdog.check_coverage(
        _orcl_broker(),
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.session_ran is False


def test_fully_covered_book_is_clean(db, state_path):
    broker = _orcl_broker(stops=(5.0, 0.3089))
    status = coverage_watchdog.check_coverage(
        broker,
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.gaps == [] and status.should_alert is False
    assert coverage_watchdog.status_line(status).startswith("coverage_watchdog: OK")


# ===========================================================================
# 3. Which day is judged
# ===========================================================================


def test_monday_morning_judges_friday_not_the_weekend(db, state_path):
    status = coverage_watchdog.check_coverage(
        _orcl_broker(),
        now=_MON_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"


def test_run_after_the_close_judges_today_not_yesterday(db, state_path):
    """Run by hand at 23:50 ET on Friday (this is exactly how the live probe
    on 2026-09-11 first ran, and it judged Thursday): Friday's session is
    over, so Friday is the day that can have failed to re-place a stop."""
    fri_late = datetime(2026, 9, 11, 23, 50, tzinfo=ET).astimezone(timezone.utc)
    status = coverage_watchdog.check_coverage(
        _orcl_broker(),
        now=fri_late,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"


def test_run_during_the_session_judges_yesterday(db, state_path):
    """At 12:00 ET on Friday the session is live and its sweep still has
    hours to run; the finished day to judge is Thursday."""
    fri_noon = datetime(2026, 9, 11, 12, 0, tzinfo=ET).astimezone(timezone.utc)
    status = coverage_watchdog.check_coverage(
        _orcl_broker(),
        now=fri_noon,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.trading_day == "2026-09-10"


def test_a_market_holiday_is_skipped_via_the_broker_calendar(db, state_path):
    """2026-09-07 (Labor Day) is a weekday the exchange was shut. Tuesday
    06:15 must judge the preceding Friday, not the holiday."""
    broker = _orcl_broker()
    broker.is_trading_day.side_effect = lambda d: d.isoformat() != "2026-09-07"
    tue = datetime(2026, 9, 8, 6, 15, tzinfo=ET).astimezone(timezone.utc)
    status = coverage_watchdog.check_coverage(
        broker,
        now=tue,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.trading_day == "2026-09-04"


def test_a_dead_calendar_falls_back_to_the_weekday_and_still_alerts(db, state_path):
    """A pathological all-False calendar must not walk past every real day
    and suppress the alert; provider exceptions now propagate distinctly."""
    status = coverage_watchdog.check_coverage(
        _orcl_broker(trading_days=False),
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.trading_day == "2026-09-11"
    assert status.should_alert is True


# ===========================================================================
# 4. Once per trading day, and failures stay on the alerting side
# ===========================================================================


def test_alerts_once_per_trading_day_then_again_the_next_day(db, state_path):
    broker = _orcl_broker()
    first = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path
    )
    again = coverage_watchdog.check_coverage(
        broker, now=_SAT_0615 + timedelta(hours=1), sweep_symbol="SGOV", db_path=db, state_path=state_path
    )
    monday = coverage_watchdog.check_coverage(
        broker, now=_MON_0615, sweep_symbol="SGOV", db_path=db, state_path=state_path
    )
    tuesday = coverage_watchdog.check_coverage(
        broker, now=_MON_0615 + timedelta(days=1), sweep_symbol="SGOV", db_path=db, state_path=state_path
    )
    assert first.should_alert is True
    assert again.should_alert is False and again.already_alerted_for_day is True
    assert monday.should_alert is False  # still judging Friday
    assert tuesday.trading_day == "2026-09-14" and tuesday.should_alert is True


def test_an_unreadable_database_cannot_prove_a_session_and_alerts(state_path, tmp_path):
    status = coverage_watchdog.check_coverage(
        _orcl_broker(),
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=tmp_path / "does-not-exist.db",
        state_path=state_path,
    )
    assert status.session_ran is None and status.db_error
    assert status.should_alert is True
    assert "could not be read" in coverage_watchdog.alert_text(status)


def test_a_broker_failure_reports_could_not_check_never_clean(db, state_path):
    broker = _orcl_broker()
    broker.get_positions.side_effect = RuntimeError("401")
    status = coverage_watchdog.check_coverage(
        broker,
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert status.gaps == [] and status.broker_error
    assert status.should_alert is False
    assert "could NOT check" in coverage_watchdog.status_line(status)


def test_a_short_is_checked_against_buy_stops(db, state_path):
    broker = MagicMock()
    broker.get_positions.return_value = [SimpleNamespace(symbol="XYZ", qty=-10.0, current_price=20.0)]
    broker.snapshot_protective_stops.return_value = (True, [{"qty": 10.0}])
    broker.is_trading_day.return_value = True
    status = coverage_watchdog.check_coverage(
        broker,
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
    )
    assert broker.snapshot_protective_stops.call_args.kwargs["side"] == "buy"
    assert status.gaps == []


# ===========================================================================
# 5. Wired into the daily heartbeat, and it cannot change the probe's verdict
# ===========================================================================


def test_heartbeat_runs_the_coverage_check_after_the_probe_and_keeps_its_own_exit_code(monkeypatch, capsys):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "run_probe", lambda: (0, "alert_heartbeat: ok"))
    calls = []

    def fake_coverage():
        calls.append(1)
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(hb, "run_coverage_check", fake_coverage)
    assert hb.main([]) == 0
    assert calls == [1]
    out = capsys.readouterr()
    assert "coverage_watchdog" in out.out + out.err


def test_heartbeat_status_mode_does_not_touch_the_broker(monkeypatch):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "run_status", lambda: (0, "status"))
    called = MagicMock()
    monkeypatch.setattr(hb, "run_coverage_check", called)
    assert hb.main(["--status"]) == 0
    assert not called.called


def test_run_coverage_check_sends_the_owner_alert_exactly_when_exposed(monkeypatch, db, state_path):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "_build_broker", lambda: _orcl_broker())
    monkeypatch.setattr(hb, "_cash_sweep_symbol", lambda: "SGOV")
    sent = []
    with patch("src.notifier.send_owner_alert", side_effect=lambda text, **kw: sent.append(text) or True):
        line = hb.run_coverage_check(now=_SAT_0615)
    assert len(sent) == 1 and "ORCL" in sent[0]
    assert "EXPOSED" in line and "delivered" in line


# ===========================================================================
# 6. It now PUTS THE STOP BACK — item 53, closed 2026-09-14
#
# The load-bearing tests here are the refusals: it must not place into a shut
# market, must not place a second stop over shares a live order already
# covers, and must never reach a path that sells anything. Placing is the
# easy half.
# ===========================================================================

_FRI_1005 = datetime(2026, 9, 11, 10, 5, tzinfo=ET).astimezone(timezone.utc)


def _repairable_broker(**kw):
    """`_orcl_broker` plus the two things a placement needs: a live price and
    the retrying submit. The recorded BUY stop is $137.53, below the price,
    so no guard blocks the repair for the wrong reason."""
    broker = _orcl_broker(**kw)
    broker.get_latest_price.return_value = 150.28
    broker._submit_protective_stop_retrying.return_value = {
        "id": "new-day-stop",
        "uncovered_qty": 0.0,
        "day_qty": 0.3089,
    }
    return broker


def _last_buy(_symbol, action="BUY"):
    if action == "SHORT":
        return {"stop_loss": 220.0}
    return {"stop_loss": 137.53}


def test_pre_open_run_places_nothing_and_says_the_market_is_shut(db, state_path):
    """06:15 ET, three hours before the bell. The unit this rides on fires
    then, and a fractional DAY stop submitted into a shut market is a
    rejection at best. It must report, not hope."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    status = coverage_watchdog.check_coverage(
        broker,
        now=_SAT_0615,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert status.market_open is False
    assert "has not opened yet" in status.market_reason
    assert status.repairs == []
    assert not broker._submit_protective_stop_retrying.called
    # and the exposure is still reported
    assert status.should_alert is True


def test_inside_the_session_it_re_places_the_missing_day_stop(db, state_path):
    """Friday 10:05 ET, desk paused, ORCL's 0.3089 remainder bare. The stop
    goes back through the same repair the in-session sweep uses."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    covered = {"n": 0}

    def _snapshot(symbol, side="sell"):
        if symbol != "ORCL":
            return True, []
        if covered["n"] == 0:
            return True, [{"id": "gtc", "qty": 5.0, "stop_price": 137.53}]
        return True, [
            {"id": "gtc", "qty": 5.0, "stop_price": 137.53},
            {"id": "new-day-stop", "qty": 0.3089, "stop_price": 137.53},
        ]

    broker.snapshot_protective_stops.side_effect = _snapshot

    def _place(**kwargs):
        covered["n"] = 1
        return {"id": "new-day-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place

    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert status.market_open is True
    assert [r.symbol for r in status.repaired] == ["ORCL"]
    assert status.repaired[0].qty == pytest.approx(0.3089)
    # The re-read after placing shows the position whole again, so there is
    # no exposure left to alert about.
    assert status.gaps == []
    assert status.should_alert is False
    assert "RE-PLACED" in coverage_watchdog.status_line(status)
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "ORCL"
    assert kwargs["qty"] == pytest.approx(0.3089)
    assert kwargs["stop_price"] == pytest.approx(137.53)
    assert kwargs["side"] == "sell"


# ===========================================================================
# 2b. THE 2026-09-17 DEFECT: this repair must defer to a live session
# ===========================================================================
#
# All six session timers shared one tick and, on 2026-09-17, a session's own
# stop-coverage reconcile and this standalone unit's reconcile ran ~90ms
# apart. Nothing needed repairing that day; the same timing with a real gap
# present is how a repair placing a stop collides with a session cancelling
# one to sell. This unit is not a party to run_if_et_window.sh's cross-mode
# session lock, so it must check it explicitly and defer — never skip the
# alert, only the ADD — whenever a session holds it.


def _hold_session_lock(monkeypatch, tmp_path, mode="midday"):
    """Simulate run_if_et_window.sh's acquire_session_lock having the
    named mode's lock currently held, the same directory
    `src.execution.held_qty.trading_session_lock_held` reads."""
    from src.execution import held_qty

    lock_dir = tmp_path / "active-session.lock"
    lock_dir.mkdir()
    (lock_dir / "owner").write_text(f"{mode} 2026-09-17 1000 12345")
    monkeypatch.setattr(held_qty, "_SESSION_LOCK_DIR", lock_dir)
    return lock_dir


def test_repair_defers_while_a_session_holds_the_trading_lock(
    db,
    state_path,
    tmp_path,
    monkeypatch,
):
    """THE INCIDENT, reproduced: a session (midday, that day) holds the
    lock. The standalone coverage-sweep tick must not place its own stop —
    that session already runs the identical repair itself — but the
    exposure must still be read and still reported, not silently dropped.
    """
    _hold_session_lock(monkeypatch, tmp_path, mode="midday")
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()

    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )

    assert not broker._submit_protective_stop_retrying.called, (
        "the repair must not fire while a session holds the trading lock"
    )
    assert status.repairs == []
    assert status.market_open is True
    assert "defer" in status.market_reason.lower()
    # The gap is real and unresolved this tick — it must still be visible,
    # not swallowed by the defer.
    assert status.gaps and status.gaps[0].symbol == "ORCL"
    assert status.should_alert is True


def test_repair_proceeds_once_the_session_lock_is_released(db, state_path):
    """No lock directory at all (the ordinary case, and the case
    immediately after a session finishes) — the repair runs exactly as
    before this change."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()

    def _place(**kwargs):
        return {"id": "new-day-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place

    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert broker._submit_protective_stop_retrying.called
    assert [r.symbol for r in status.repaired] == ["ORCL"]


def test_repair_defer_does_not_apply_to_intra_check(
    db,
    state_path,
    tmp_path,
    monkeypatch,
):
    """intra_check is deliberately exempt from run_if_et_window.sh's
    session lock, so it never appears as the lock owner here — this proves
    `trading_session_lock_held()` cannot and does not treat intra_check as
    a blocker. (The intra_check-vs-coverage-sweep pairing is instead closed
    by the schedule move in scripts/systemd/quant-agent-intra_check.timer —
    see tests/test_systemd_units.py — since intra_check never takes this
    lock in the first place.)
    """
    from src.execution import held_qty

    lock_dir = tmp_path / "active-session.lock"
    monkeypatch.setattr(held_qty, "_SESSION_LOCK_DIR", lock_dir)
    assert not lock_dir.is_dir()
    assert held_qty.trading_session_lock_held() is False

    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()

    def _place(**kwargs):
        return {"id": "new-day-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place

    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert broker._submit_protective_stop_retrying.called


def test_it_cannot_double_cover_a_remainder_a_live_order_already_holds(db, state_path):
    """The gap list is a snapshot; the broker is re-read immediately before
    placing. `snapshot_protective_stops` filters Alpaca's OPEN set, which
    includes an order still in flight from an earlier tick — so a remainder
    already covered shrinks the shortfall to zero and zero is not placed."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    reads = {"n": 0}

    def _snapshot(symbol, side="sell"):
        if symbol != "ORCL":
            return True, []
        reads["n"] += 1
        if reads["n"] == 1:  # the survey pass: a gap is real here
            return True, [{"id": "gtc", "qty": 5.0, "stop_price": 137.53}]
        # by the time we go to place, a DAY stop from an earlier tick is live
        return True, [
            {"id": "gtc", "qty": 5.0, "stop_price": 137.53},
            {"id": "in-flight-day", "qty": 0.3089, "stop_price": 137.53},
        ]

    broker.snapshot_protective_stops.side_effect = _snapshot
    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert reads["n"] >= 2, "the broker must be re-read right before placing"
    assert not broker._submit_protective_stop_retrying.called
    assert status.repairs == []


def test_a_failed_placement_alerts_and_is_not_swallowed(db, state_path, monkeypatch):
    """A placement that does not land is its own alarm, on its own
    once-a-day marker — the exposure report must not absorb it."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    broker._submit_protective_stop_retrying.return_value = None
    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert [r.symbol for r in status.repair_failures] == ["ORCL"]
    assert status.should_alert_repair_failure is True
    text = coverage_watchdog.repair_failure_text(status)
    assert "COULD NOT PUT THE PROTECTIVE STOP BACK" in text and "ORCL" in text
    assert "FAILED to place" in coverage_watchdog.status_line(status)
    # second run the same trading day: still exposed, but not paged twice
    again = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert again.repair_failures and again.should_alert_repair_failure is False


def test_a_placement_error_is_reported_not_swallowed(db, state_path):
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    broker._submit_protective_stop_retrying.side_effect = RuntimeError("429")
    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert status.repair_failures and "429" in status.repair_failures[0].detail


def test_it_never_sells_resizes_or_cancels_anything(db, state_path):
    """A 0% target is read by this desk as 'sell it'. Nothing in this path
    may reach a close, a cancel or an order that is not a protective stop."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert not broker.close_position.called
    assert not broker.submit_order.called
    assert not broker.cancel_snapshotted_stops.called
    assert not broker.cancel_open_orders.called
    assert not broker.replace_stop_loss.called


def test_the_cash_sweep_vehicle_is_never_repaired(db, state_path):
    """SGOV is deliberately stopless. It is skipped from the survey, and the
    repair pass skips it a second time."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    placed = [c.kwargs.get("symbol") for c in broker._submit_protective_stop_retrying.call_args_list]
    assert "SGOV" not in placed


def test_a_naked_short_is_repaired_with_a_buy_stop(db, state_path):
    """Uncovered short → BUY stop at the SHORT row's recorded level.
    Dropping is_short here would take the long path: a SELL stop at the
    BUY row's $137.53 sits below the $200 tape, so the price-side guard
    would pass and the gap would look repaired. Assert side=buy and the
    SHORT row's $220."""
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="TSLA", qty=-3.0, current_price=200.0),
    ]
    broker.get_latest_price.return_value = 200.0
    covered = {"n": 0}

    def _snapshot(symbol, side="sell"):
        if symbol != "TSLA":
            return True, []
        assert side == "buy", "a short's protective stops are BUY-side"
        if covered["n"] == 0:
            return True, []
        return True, [{"id": "new-buy-stop", "qty": 3.0, "stop_price": 220.0}]

    broker.snapshot_protective_stops.side_effect = _snapshot

    def _place(**kwargs):
        covered["n"] = 1
        return {"id": "new-buy-stop", "uncovered_qty": 0.0}

    broker._submit_protective_stop_retrying.side_effect = _place
    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=_last_buy,
    )
    assert status.market_open is True
    assert [r.symbol for r in status.repaired] == ["TSLA"]
    assert status.repaired[0].qty == pytest.approx(3.0)
    assert status.gaps == []
    kwargs = broker._submit_protective_stop_retrying.call_args.kwargs
    assert kwargs["symbol"] == "TSLA"
    assert kwargs["qty"] == pytest.approx(3.0)
    assert kwargs["stop_price"] == pytest.approx(220.0)
    assert kwargs["side"] == "buy"
    assert kwargs.get("limit_price") is None  # stop-MARKET: no limit


def test_heartbeat_last_buy_reader_forwards_short_action(tmp_path, monkeypatch):
    """The 30-minute sweep's production lookup must pass action=SHORT.
    A reader that only queries BUY would leave this SHORT row invisible
    and the watchdog-suite doubles would not catch it."""
    import scripts.alert_heartbeat as hb
    from src.storage.db import Database

    db_path = tmp_path / "quant_agent.db"
    db = Database(str(db_path))
    db.initialize()
    db.insert_trade(
        symbol="TSLA",
        action="SHORT",
        qty=3,
        price=200.0,
        reasoning="opened short",
        run_id="r1",
        stop_loss=220.0,
        fill_status="filled",
    )
    monkeypatch.setattr("src.api.deps.get_db_path", lambda: str(db_path))
    _db, reader = hb._coverage_db_and_last_buy()
    assert reader is not None
    row = reader("TSLA", action="SHORT")
    assert row is not None and row["stop_loss"] == 220.0
    assert reader("TSLA") is None  # default BUY must not see the SHORT row


def test_no_recorded_stop_lookup_means_it_stays_a_pure_reader(db, state_path):
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    status = coverage_watchdog.check_coverage(
        broker,
        now=_FRI_1005,
        sweep_symbol="SGOV",
        db_path=db,
        state_path=state_path,
        last_buy=None,
    )
    assert not broker._submit_protective_stop_retrying.called
    assert "nothing was placed" in status.market_reason


# ---- the coverage-only entry point ----


def test_coverage_only_runs_the_check_and_never_the_channel_probe(monkeypatch):
    import scripts.alert_heartbeat as hb

    probe = MagicMock()
    monkeypatch.setattr(hb, "run_probe", probe)
    monkeypatch.setattr(hb, "run_coverage_check", lambda: "coverage_watchdog: OK")
    assert hb.main(["--coverage-only"]) == 0
    assert not probe.called


def test_coverage_only_reports_a_broker_it_cannot_build(monkeypatch, capsys):
    import scripts.alert_heartbeat as hb

    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(hb, "run_coverage_check", boom)
    assert hb.main(["--coverage-only"]) == 1
    assert "no credentials" in capsys.readouterr().err


# ===========================================================================
# 7. Every run leaves a durable, identifiable record — board item 131
#
# Production, read-only, 2026-09-19: the unit had run 235 times since
# 2026-09-14 with every exit 0, and had placed stops at four market opens —
# but all of it lived only in the systemd journal of that one unit. Zero
# lines reached quant_agent.log and zero rows reached the database, so the
# board could not tell whether it had ever run.
# ===========================================================================


def _sweep_rows(database):
    return [
        json.loads(r["evidence_json"])
        for r in database.conn.execute(
            "SELECT evidence_json FROM specialist_evidence "
            "WHERE agent_name = ? AND kind = 'pipeline_event' AND scope = 'run'",
            ("coverage_sweep",),
        ).fetchall()
    ]


@pytest.fixture
def desk_db(tmp_path):
    from src.storage.db import Database

    database = Database(str(tmp_path / "desk.db"))
    database.initialize()
    yield database
    database.close()


def test_a_repairing_sweep_run_leaves_a_log_line_and_an_event_row(
    monkeypatch,
    db,
    state_path,
    desk_db,
    caplog,
):
    import logging

    import scripts.alert_heartbeat as hb

    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))
    broker = _repairable_broker()
    covered = {"n": 0}

    def _snapshot(symbol, side="sell"):
        if symbol != "ORCL":
            return True, []
        stops = [{"id": "gtc", "qty": 5.0, "stop_price": 137.53}]
        if covered["n"]:
            stops.append({"id": "new-day-stop", "qty": 0.3089, "stop_price": 137.53})
        return True, stops

    def _place(**kwargs):
        covered["n"] = 1
        return {"id": "new-day-stop", "uncovered_qty": 0.0}

    broker.snapshot_protective_stops.side_effect = _snapshot
    broker._submit_protective_stop_retrying.side_effect = _place
    monkeypatch.setattr(hb, "_build_broker", lambda: broker)
    monkeypatch.setattr(hb, "_cash_sweep_symbol", lambda: "SGOV")
    monkeypatch.setattr(hb, "_coverage_db_and_last_buy", lambda: (desk_db, _last_buy))
    monkeypatch.setattr(hb, "_RUN_ENTRY", "coverage_sweep", raising=False)

    with caplog.at_level(logging.INFO, logger="src.coverage_watchdog"):
        hb.run_coverage_check(now=_FRI_1005)

    lines = [r.getMessage() for r in caplog.records if "COVERAGE SWEEP" in r.getMessage()]
    assert len(lines) == 2
    assert lines[0].endswith("(coverage_sweep): started")
    assert "repaired" in lines[1] and "positions checked 1" in lines[1]
    # Defect 5b, 2026-09-30: a repairing run used to assert "alert none
    # sent". It now pages, so the assertion is inverted deliberately.
    assert "succeeded 1" in lines[1] and "alert none sent" not in lines[1]
    assert "stop-repaired alert" in lines[1]

    rows = _sweep_rows(desk_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "repaired"
    assert row["entry"] == "coverage_sweep"
    assert row["positions_checked"] == 1  # SGOV is excluded
    assert row["repairs_attempted"] == 1
    assert row["repairs_succeeded"] == 1
    assert row["repairs_failed"] == 0
    assert row["repaired"][0]["symbol"] == "ORCL"
    # Defect 5a: `gaps_found` is what the run FOUND. It read 0 here
    # because the post-repair re-read rebound the same variable, which is
    # how a repaired gap was reported as no gap at all.
    assert row["gaps_found"] == 1
    assert row["gaps_remaining"] == 0  # re-read after placing
    # Defect 5b: a repair now pages. (Not delivered in this test — no
    # notifier is configured — which the line reports honestly.)
    assert row["alerts"] == ["stop-repaired alert could NOT be delivered"]
    assert row["run_id"] in lines[0]


def test_a_clean_sweep_run_is_recorded_as_clean(monkeypatch, db, state_path, desk_db):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "_build_broker", lambda: _orcl_broker(stops=(5.0, 0.3089)))
    monkeypatch.setattr(hb, "_cash_sweep_symbol", lambda: "SGOV")
    monkeypatch.setattr(hb, "_coverage_db_and_last_buy", lambda: (desk_db, _last_buy))

    hb.run_coverage_check(now=_SAT_0615)

    [row] = _sweep_rows(desk_db)
    assert row["outcome"] == "clean"
    assert row["positions_checked"] == 1 and row["gaps_found"] == 0
    assert row["repairs_attempted"] == 0 and row["alerts"] == []


def test_an_alerting_sweep_run_records_that_the_alert_went_out(
    monkeypatch,
    db,
    state_path,
    desk_db,
):
    import scripts.alert_heartbeat as hb

    monkeypatch.setattr(hb, "_build_broker", lambda: _orcl_broker())
    monkeypatch.setattr(hb, "_cash_sweep_symbol", lambda: "SGOV")
    monkeypatch.setattr(hb, "_coverage_db_and_last_buy", lambda: (desk_db, None))
    with patch("src.notifier.send_owner_alert", return_value=True):
        hb.run_coverage_check(now=_SAT_0615)

    [row] = _sweep_rows(desk_db)
    assert row["outcome"] == "gaps_left"
    assert row["gap_symbols"] == ["ORCL"]
    assert row["alerts"] == ["exposure alert delivered"]


def test_a_sweep_that_cannot_build_its_broker_still_leaves_a_record(
    monkeypatch,
    db,
    state_path,
    desk_db,
):
    import scripts.alert_heartbeat as hb

    def _boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(hb, "_build_broker", _boom)
    monkeypatch.setattr(hb, "_coverage_db_and_last_buy", lambda: (desk_db, _last_buy))
    with pytest.raises(RuntimeError):
        hb.run_coverage_check(now=_SAT_0615)

    [row] = _sweep_rows(desk_db)
    assert row["outcome"] == "could_not_run"
    assert "no credentials" in row["reason"]


def test_the_sweep_process_writes_to_the_desk_log_file(tmp_path):
    """The unit's own process must reach `quant_agent.log`, the log the desk
    actually writes, not only its systemd journal."""
    import logging

    import scripts.alert_heartbeat as hb

    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    try:
        hb._attach_desk_log(tmp_path)
        logging.getLogger("src.coverage_watchdog").info(
            "COVERAGE SWEEP coverage_sweep-test (coverage_sweep): started",
        )
    finally:
        for h in root.handlers[:]:
            if h not in before_handlers:
                h.close()
                root.removeHandler(h)
        root.setLevel(before_level)
    text = (tmp_path / "quant_agent.log").read_text()
    assert "[INFO] src.coverage_watchdog: COVERAGE SWEEP coverage_sweep-test" in text


# ---------------------------------------------------------------------------
# DEFECT 5 (2026-09-30): the sweep repaired AAPL and reported
# "gaps 0, ... repairs attempted 1 ... alert none sent".
# ---------------------------------------------------------------------------


def _repaired_status(**kw):
    from src.coverage_watchdog import CoverageStatus, RepairOutcome

    base = dict(
        trading_day="2026-09-30",
        session_ran=True,
        gaps=[],  # the POST-repair re-read: nothing left
        gaps_detected=1,  # what the run actually found
        repairs=[RepairOutcome(symbol="AAPL", qty=7.33, placed=True)],
        positions_checked=11,
    )
    base.update(kw)
    return CoverageStatus(**base)


def test_summary_reports_the_gap_it_found_not_the_one_left():
    """5a. `gaps` is rebound to the post-repair re-read, so the count the
    operator scans read 0 while a repair was attempted. Both numbers now."""
    from src.coverage_watchdog import sweep_log_line, sweep_summary

    summary = sweep_summary(
        _repaired_status(),
        entry="coverage_sweep",
        run_id="r1",
    )
    assert summary["gaps_found"] == 1
    assert summary["gaps_remaining"] == 0
    assert summary["repairs_attempted"] == 1
    line = sweep_log_line(summary)
    assert "gaps 1 found / 0 still open" in line
    assert "gaps 0," not in line


def test_a_repair_pages_the_owner():
    """5b. A repair means something upstream failed silently; the last
    line of defence must not fix it and say nothing."""
    from src.coverage_watchdog import repair_performed_text

    status = _repaired_status()
    assert status.should_alert_repair_performed is True
    text = repair_performed_text(status)
    assert "AAPL" in text and "PUT BACK" in text


def test_a_clean_sweep_still_says_nothing():
    from src.coverage_watchdog import CoverageStatus

    clean = CoverageStatus(
        trading_day="2026-09-30",
        session_ran=True,
        gaps=[],
        gaps_detected=0,
        repairs=[],
        positions_checked=11,
    )
    assert clean.should_alert_repair_performed is False


def test_a_repair_already_all_cleared_does_not_page_twice():
    status = _repaired_status(resolution_notice_symbols=("aapl",))
    assert status.should_alert_repair_performed is False


# ---------------------------------------------------------------------------
# board item 193 — a deliberately unguarded position is REPORTED, not hidden
# ---------------------------------------------------------------------------


def _item193_db(tmp_path, *, created_at, windows=()):
    import json as _json
    import sqlite3 as _sq

    path = tmp_path / "item193.db"
    conn = _sq.connect(path)
    conn.execute(
        "CREATE TABLE pending_protection_restores ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, sell_order_id TEXT,"
        " position_qty_before_sell REAL, specs_json TEXT, created_at TEXT,"
        " run_id TEXT, side TEXT)"
    )
    from src.execution.scale_in import WAL_SCALE_IN_SENTINEL

    conn.execute(
        "INSERT INTO pending_protection_restores "
        "(symbol, sell_order_id, position_qty_before_sell, specs_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("AAPL", WAL_SCALE_IN_SENTINEL, 12.0, "[]", created_at),
    )
    conn.execute(
        "CREATE TABLE specialist_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, evidence_json TEXT)"
    )
    for secs in windows:
        conn.execute(
            "INSERT INTO specialist_evidence (kind, evidence_json) VALUES (?,?)",
            (
                "pipeline_event",
                _json.dumps(
                    {
                        "stage": "scale_in",
                        "outcome": "unprotected_window_closed",
                        "window_seconds": secs,
                    }
                ),
            ),
        )
    conn.commit()
    conn.close()
    return path


def test_item193_skipped_symbol_is_reported_with_its_age(tmp_path):
    """The skip stays; the symbol is no longer silently absent."""
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw

    db = _item193_db(tmp_path, created_at="2026-09-30 14:00:00", windows=[2.5, 9.0])
    now = datetime(2026, 9, 30, 14, 1, 0, tzinfo=timezone.utc)
    rows = cw.deliberately_unguarded(
        object(),
        db,
        skip_symbols={"AAPL"},
        now=now,
    )
    assert [r.symbol for r in rows] == ["AAPL"]
    row = rows[0]
    assert row.held_qty == 12.0 and row.is_short is False
    assert row.seconds_open == 60.0
    # the bound is MEASURED (the longest window the desk ever closed), not chosen
    assert row.bound_seconds == 9.0 and row.bound_observations == 2
    assert row.over_bound is True


def test_item193_no_measured_history_means_nothing_is_called_overdue(tmp_path):
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw

    db = _item193_db(tmp_path, created_at="2026-09-30 14:00:00")
    rows = cw.deliberately_unguarded(
        object(),
        db,
        skip_symbols={"AAPL"},
        now=datetime(2026, 9, 30, 14, 5, 0, tzinfo=timezone.utc),
    )
    assert rows and rows[0].bound_seconds is None
    assert rows[0].over_bound is False
    status = cw.CoverageStatus(trading_day="2026-09-30", session_ran=True, unguarded=rows)
    assert status.unguarded_over_bound == []
    assert status.should_alert_unguarded is False


def test_item193_only_actually_skipped_symbols_are_reported(tmp_path):
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw

    db = _item193_db(tmp_path, created_at="2026-09-30 14:00:00")
    assert (
        cw.deliberately_unguarded(
            object(),
            db,
            skip_symbols=set(),
            now=datetime(2026, 9, 30, 14, 5, tzinfo=timezone.utc),
        )
        == []
    )


def test_item193_summary_and_log_line_name_the_unguarded_position():
    import src.coverage_watchdog as cw

    row = cw.UnguardedWindow(
        symbol="AAPL",
        held_qty=12.0,
        is_short=False,
        since_utc="2026-09-30 14:00:00",
        seconds_open=60.0,
        bound_seconds=9.0,
        bound_observations=2,
    )
    status = cw.CoverageStatus(
        trading_day="2026-09-30",
        session_ran=True,
        unguarded=[row],
        unguarded_fresh=[row],
    )
    summary = cw.sweep_summary(status, entry="unit", run_id="r1")
    assert summary["unguarded_count"] == 1
    assert summary["unguarded_over_bound"] == ["AAPL"]
    assert summary["unguarded"][0]["measured_bound_seconds"] == 9.0
    line = cw.sweep_log_line(summary)
    assert "DELIBERATELY UNGUARDED" in line and "AAPL ~60s" in line
    assert "OVER LONGEST MEASURED" in line
    text = cw.unguarded_text([row])
    assert "AAPL" in text and "9.0s" in text
    assert status.should_alert_unguarded is True


# ---------------------------------------------------------------------------
# board item 193 — the session-lock skip is BOUNDED by measured history
# ---------------------------------------------------------------------------


class _EntryBroker:
    """Broker double whose only job is to say what entry orders are working."""

    def __init__(self, working):
        self.working = list(working)

    def get_orders(self, *a, **k):  # pragma: no cover - shape varies
        return list(self.working)


def _lock_held(monkeypatch, held):
    import src.execution.scale_in as si

    monkeypatch.setattr(si, "trading_session_lock_held", lambda: held)


def test_item193_lock_held_within_measured_bound_still_skips(tmp_path, monkeypatch):
    """A normal live window is untouched: the skip is the correct behaviour."""
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw

    _lock_held(monkeypatch, True)
    db = _item193_db(tmp_path, created_at="2026-09-30 14:00:00", windows=[2.5, 9.0])
    now = datetime(2026, 9, 30, 14, 0, 5, tzinfo=timezone.utc)  # 5s < 9s measured
    assert cw._scale_in_skip(_EntryBroker([]), db, now=now) == {"AAPL"}


def test_item193_lock_held_past_measured_bound_hands_symbol_to_the_sweep(
    tmp_path,
    monkeypatch,
):
    """Past every window ever measured, the lock is no longer reason to look
    away: with no entry order working there is nothing to collide with, so the
    position becomes repairable instead of staying naked indefinitely."""
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw

    _lock_held(monkeypatch, True)
    import src.execution.scale_in as si

    monkeypatch.setattr(si, "list_open_entry_ids", lambda b, s: [])
    db = _item193_db(tmp_path, created_at="2026-09-30 14:00:00", windows=[2.5, 9.0])
    now = datetime(2026, 9, 30, 14, 5, 0, tzinfo=timezone.utc)  # 300s > 9s
    assert cw._scale_in_skip(_EntryBroker([]), db, now=now) == set()


def test_item193_past_bound_but_entry_still_working_keeps_the_skip(
    tmp_path,
    monkeypatch,
):
    """The collision is real — a stop would be blocked — so the skip stands."""
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw
    import src.execution.scale_in as si

    _lock_held(monkeypatch, True)
    monkeypatch.setattr(si, "list_open_entry_ids", lambda b, s: ["entry-1"])
    db = _item193_db(tmp_path, created_at="2026-09-30 14:00:00", windows=[9.0])
    now = datetime(2026, 9, 30, 14, 5, 0, tzinfo=timezone.utc)
    assert cw._scale_in_skip(_EntryBroker(["entry-1"]), db, now=now) == {"AAPL"}


def test_item193_no_measured_history_leaves_the_old_behaviour_exactly(
    tmp_path,
    monkeypatch,
):
    """With nothing measured there is no bound, so nothing is called overdue
    and the lock-held skip behaves exactly as it did before."""
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw
    import src.execution.scale_in as si

    _lock_held(monkeypatch, True)
    monkeypatch.setattr(si, "list_open_entry_ids", lambda b, s: [])
    db = _item193_db(tmp_path, created_at="2026-09-30 14:00:00")  # no windows
    now = datetime(2026, 9, 30, 18, 0, 0, tzinfo=timezone.utc)
    assert cw._scale_in_skip(_EntryBroker([]), db, now=now) == {"AAPL"}


def test_item193_unreadable_write_time_is_never_treated_as_overdue(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import src.coverage_watchdog as cw
    import src.execution.scale_in as si

    _lock_held(monkeypatch, True)
    monkeypatch.setattr(si, "list_open_entry_ids", lambda b, s: [])
    db = _item193_db(tmp_path, created_at="not-a-timestamp", windows=[9.0])
    now = datetime(2026, 9, 30, 18, 0, 0, tzinfo=timezone.utc)
    assert cw._scale_in_skip(_EntryBroker([]), db, now=now) == {"AAPL"}


# ============================================================================
# Item 211 (2026-09-30): the generic per-TYPE alert claim still SPEAKS UP
# ============================================================================


def test_typed_alert_claim_is_per_type_and_records_what_it_held_back(tmp_path):
    """The owner-alert path must not get quieter than it already is.

    A first finding of any type still returns its keys, so the caller still
    pages. Only an exact repeat of the SAME finding on the SAME ET day is
    held, and a different alert type is never silenced by a noisy one --
    that is the failure a global throttle would introduce. What was held
    back is written down rather than dropped.
    """
    from src.coverage_watchdog import claim_typed_alert, load_state

    path = tmp_path / "state.json"

    # It fires.
    assert claim_typed_alert("deploy_drift", ["MAIN@ABC"], path=path) == ["MAIN@ABC"]
    # The same finding again is one finding, not two pages.
    assert claim_typed_alert("deploy_drift", ["MAIN@ABC"], path=path) == []
    # A different deployed SHA is a different finding, and it fires.
    assert claim_typed_alert("deploy_drift", ["MAIN@DEF"], path=path) == ["MAIN@DEF"]
    # A DIFFERENT alert type is never silenced by a noisy one.
    assert claim_typed_alert("pricing_cache", ["MAIN@ABC"], path=path) == ["MAIN@ABC"]

    # Nothing was silently dropped.
    suppressed = load_state(path)["suppressed_alerts"]["deploy_drift"]
    assert suppressed["count"] == 1
    assert suppressed["events"][-1]["key"] == "MAIN@ABC"


# ===========================================================================
# Item 211 defect 2 — the per-DAY unprotected marker silenced a second name
# ===========================================================================


def _two_name_broker(stopless: tuple[str, ...]):
    """Two held longs; the ones named in `stopless` have no protective stop."""
    broker = MagicMock()
    broker.get_positions.return_value = [
        SimpleNamespace(symbol="AAA", qty=10.0, current_price=100.0),
        SimpleNamespace(symbol="BBB", qty=10.0, current_price=100.0),
    ]

    def _snapshot(symbol, side="sell"):
        if symbol in stopless:
            return True, []
        return True, [{"id": f"stop-{symbol}", "qty": 10.0, "stop_price": 90.0}]

    broker.snapshot_protective_stops.side_effect = _snapshot
    broker.is_trading_day.return_value = True
    broker.get_session_open.side_effect = lambda on_date=None: None
    broker.get_session_close.side_effect = lambda on_date=None: None
    return broker


def test_a_second_name_going_unprotected_the_same_day_still_alerts(db, state_path):
    """Item 211 defect 2, a live-risk hole. The "position is unprotected"
    alert was deduped on a bare per-DAY marker while stop-repair-failure and
    unreadable-stop both dedupe per SYMBOL per day, so a SECOND name going
    naked later the same day was silenced completely. Two different symbols
    going unprotected on the same day must produce two alerts.

    Fails on the old behaviour: the first call wrote `alerted_for_day` and
    the second returned `should_alert is False` with BBB naked and nothing
    said about it anywhere.
    """
    _seed_session(db, source="evening", when=datetime(2026, 9, 3, 0, 3, tzinfo=timezone.utc))

    first = coverage_watchdog.check_coverage(
        _two_name_broker(("AAA",)),
        now=_SAT_0615,
        db_path=db,
        state_path=state_path,
    )
    assert [g.symbol for g in first.gaps] == ["AAA"]
    assert first.should_alert is True

    # Same trading day, same state file. BBB's stop is now gone too.
    second = coverage_watchdog.check_coverage(
        _two_name_broker(("AAA", "BBB")),
        now=_SAT_0615,
        db_path=db,
        state_path=state_path,
    )
    assert {g.symbol for g in second.gaps} == {"AAA", "BBB"}
    assert second.should_alert is True, "a second name going unprotected the same day must page the owner"
    assert "BBB" in coverage_watchdog.alert_text(second)

    # And a repeat of the SAME name alone is still only said once a day.
    third = coverage_watchdog.check_coverage(
        _two_name_broker(("AAA", "BBB")),
        now=_SAT_0615,
        db_path=db,
        state_path=state_path,
    )
    assert third.should_alert is False
