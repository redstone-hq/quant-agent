"""The owner's health report states what the log shows, and no more.

Three untruths in the 2026-10-13 reports (owner-facing audit, F11 and F12):

* 4:30 PM said "Still true: faults this health check does not recognise".
  All 162 of them were logged before 11:15 AM and none after. The report gave
  no time on that line, so a fault that had stopped five hours earlier read as
  current. Every line now carries its last time and, when the log ran on past
  it, "none since" with the time the log was read to. No threshold decides
  anything: both times come from the log.
* 8:55 AM said "1 holding" went through the night without a stop on its
  part-share. The coverage sweep, every 30 minutes all night, found 6 of 6.
  The report counted LOG LINES (one evening line) as holdings. The count now
  comes from the sweep's own run line.
* A standing exposure the owner accepted on 2 Sep headed the report as HURT
  every morning. It now reads "known, accepted" and never sets HURT.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from src import log_health as L
from src.coverage_watchdog import sweep_log_line

ET = timezone(timedelta(hours=-4))  # US Eastern daylight time, October 2026


def _et(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ET).astimezone(L.LOG_TZ)


def _rec(ts: datetime, level: str, source: str, message: str) -> L.LogRecord:
    return L.LogRecord(ts, level, source, message)


def _text(report: L.Report) -> str:
    return "\n".join(L.render(report))


# --- "none since", measured from the log ------------------------------------

_UNPLACED = "owner stop gate: synthetic credential refusal number {i}"


def _stopped_fault_report(previous=None) -> L.Report:
    start, end = _et(13, 8, 55), _et(13, 16, 30)
    first = _et(13, 9, 30)
    faults = [
        _rec(first + timedelta(seconds=39 * i), "ERROR", "src.stop_gate", _UNPLACED.format(i=i)) for i in range(161)
    ]
    faults.append(_rec(_et(13, 11, 15), "ERROR", "src.stop_gate", _UNPLACED.format(i=161)))
    later = [
        _rec(_et(13, 14, 0), "INFO", "src.pipeline", "session tick"),
        _rec(_et(13, 16, 29), "INFO", "src.pipeline", "session tick"),
    ]
    records = faults + later
    return L.analyse(records, start, end, previous=previous)


def test_the_fixture_faults_are_unrecognised_and_end_at_eleven_fifteen():
    assert L.classify(_UNPLACED.format(i=0), "ERROR") is None
    report = _stopped_fault_report()
    finding = next(f for f in report.reported if f.family.key == L.UNRECOGNISED_KEY)
    assert finding.count == 162
    assert finding.last_seen == _et(13, 11, 15)


def test_a_fault_that_stopped_says_none_since_and_where_the_log_was_read_to():
    text = _text(_stopped_fault_report())
    line = next(l for l in text.splitlines() if "never seen before" in l)
    assert re.search(
        r"last at \d{4}-\d{2}-\d{2} 11:15 AM ET, none since, log read to \d{4}-\d{2}-\d{2} 4:29 PM ET", line
    ), line


def test_a_still_true_line_carries_the_same_fact():
    text = _text(_stopped_fault_report(previous={L.UNRECOGNISED_KEY: 162}))
    line = next(l for l in text.splitlines() if l.startswith("• Still true:") and "does not recognise" in l)
    assert "11:15 AM ET, none since, log read to" in line, line
    assert "4:29 PM ET" in line, line


def test_a_fault_still_firing_at_the_end_of_the_log_claims_nothing_more():
    start, end = _et(13, 8, 55), _et(13, 16, 30)
    last = _et(13, 16, 29)
    report = L.analyse([_rec(last, "ERROR", "src.stop_gate", _UNPLACED.format(i=0))], start, end)
    line = next(l for l in _text(report).splitlines() if "never seen before" in l)
    assert "none since" not in line
    assert "(last at " in line


# --- the overnight count comes from the coverage sweep ----------------------

_EXPOSURE = (
    "OVERNIGHT FRACTIONAL EXPOSURE: 6 position(s) carrying a sub-share remainder with no live stop "
    "until the next session — $1834.00 total at risk. Expected and bounded by design; the whole-share "
    "part of each is still covered by its GTC stop."
)


def _sweep(ts: datetime, still_open: int, checked: int = 6) -> L.LogRecord:
    summary = {
        "outcome": "repair_deferred",
        "run_id": f"coverage_sweep-{ts:%H%M}",
        "entry": "coverage_sweep",
        "positions_checked": checked,
        "gaps_found": still_open,
        "gaps_remaining": still_open,
        "repairs_attempted": 0,
        "repairs_succeeded": 0,
        "repairs_failed": 0,
        "repair_deferred": "market closed",
        "alerts": [],
    }
    return _rec(ts, "INFO", "src.coverage_watchdog", sweep_log_line(summary))


def _overnight_report(last_open: int = 6) -> L.Report:
    start, end = _et(12, 16, 30), _et(13, 8, 55)
    records = [_rec(_et(12, 20, 0), "WARNING", "src.pipeline", _EXPOSURE)]
    moment = _et(12, 20, 30)
    while moment < _et(13, 8, 30):
        records.append(_sweep(moment, 6))
        moment += timedelta(minutes=30)
    records.append(_sweep(_et(13, 8, 30), last_open))
    return L.analyse(records, start, end)


def test_six_of_six_all_night_is_never_reported_as_one_holding():
    text = _text(_overnight_report())
    assert "1 holding " not in text, text
    assert "6 holdings" in text, text


def test_the_latest_sweep_run_sets_the_count():
    text = _text(_overnight_report(last_open=5))
    assert "5 holdings" in text, text
    assert "1 holding " not in text, text


# --- an accepted exposure is known, not hurt --------------------------------


def test_an_accepted_family_reads_known_accepted_and_never_hurt():
    text = _text(_overnight_report())
    assert "known, accepted" in text, text
    assert "HURT" not in text, text
