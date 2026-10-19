"""A routine owner report is not a fault; an unplaced or risk alert still is."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import log_health as L
from src.notifier import owner_alert_funnel as funnel

UTC = timezone.utc
START = datetime(2026, 10, 16, 19, 0, tzinfo=UTC)
END = datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
TS = datetime(2026, 10, 16, 20, 30, tzinfo=UTC)

SOURCES = ["src.notifier", "src.notifier.base", "src.notifier.owner_alert_funnel"]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("kind", ["intra_check", "morning", "midday", "close"])
def test_operational_echo_is_not_a_fault(source, kind):
    msg = f"OWNER ALERT [{kind}|operational]\nSafety check: nothing to report."
    report = L.analyse([L.LogRecord(TS, "CRITICAL", source, msg)], START, END)
    assert report.reported == []


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("tag", ["owner_alert|risk", "mystery|risk", "owner_alert"])
def test_unclassified_or_risk_echo_still_surfaces(source, tag):
    msg = f"OWNER ALERT [{tag}]\nSomething nobody has written a family for yet."
    report = L.analyse([L.LogRecord(TS, "CRITICAL", source, msg)], START, END)
    assert [f.family.key for f in report.reported] == [L.UNRECOGNISED_KEY]


def test_a_real_unrecognised_error_elsewhere_still_counts():
    records = [
        L.LogRecord(TS, "CRITICAL", "src.notifier.owner_alert_funnel", "OWNER ALERT [midday|operational]\nok"),
        L.LogRecord(TS, "ERROR", "src.pipeline", "Something nobody has written a family for yet."),
    ]
    report = L.analyse(records, START, END)
    assert [f.family.key for f in report.reported] == [L.UNRECOGNISED_KEY]
    assert report.reported[0].count == 1


def test_lookalike_source_is_not_skipped():
    msg = "OWNER ALERT [midday|operational]\nok"
    report = L.analyse([L.LogRecord(TS, "CRITICAL", "src.notifierx", msg)], START, END)
    assert [f.family.key for f in report.reported] == [L.UNRECOGNISED_KEY]


def test_funnel_stamps_resolved_category(caplog):
    class _N:
        pass

    caplog.set_level("CRITICAL")
    funnel.send_owner_alert_with_outcome("hello", notifier=_N(), kind="midday", max_attempts=1)
    funnel.send_owner_alert_with_outcome("hello", notifier=_N(), kind="owner_alert", max_attempts=1)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("OWNER ALERT")]
    assert any(m.startswith("OWNER ALERT [midday|operational]") for m in lines)
    assert any(m.startswith("OWNER ALERT [owner_alert|risk]") for m in lines)
