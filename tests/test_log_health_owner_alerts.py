"""An owner report is not a fault: the funnel's CRITICAL echo is never counted."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import log_health as L

UTC = timezone.utc
START = datetime(2026, 10, 16, 19, 0, tzinfo=UTC)
END = datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
TS = datetime(2026, 10, 16, 20, 30, tzinfo=UTC)

ROUTINE = [
    "OWNER ALERT [safety_check]\nSafety check: nothing to report.",
    "OWNER ALERT [safety_check]\nSafety check deferred.",
    "OWNER ALERT [midday_review]\nMidday review: all positions fine.",
]


@pytest.mark.parametrize("source", ["src.notifier", "src.notifier.base", "src.notifier.owner_alert_funnel"])
@pytest.mark.parametrize("body", ROUTINE)
def test_routine_owner_report_is_not_a_fault(source, body):
    report = L.analyse([L.LogRecord(TS, "CRITICAL", source, body)], START, END)
    assert report.reported == []


def test_a_real_unrecognised_error_elsewhere_still_counts():
    records = [
        L.LogRecord(TS, "CRITICAL", "src.notifier.owner_alert_funnel", ROUTINE[0]),
        L.LogRecord(TS, "ERROR", "src.pipeline", "Something nobody has written a family for yet."),
    ]
    report = L.analyse(records, START, END)
    assert [f.family.key for f in report.reported] == [L.UNRECOGNISED_KEY]
    assert report.reported[0].count == 1


def test_lookalike_source_is_not_skipped():
    report = L.analyse([L.LogRecord(TS, "CRITICAL", "src.notifierx", ROUTINE[0])], START, END)
    assert [f.family.key for f in report.reported] == [L.UNRECOGNISED_KEY]
