"""The PROTECTION UNDETERMINED page fires only for a mode whose session audits broker stops.

Measured defect: the owner was paged "PROTECTION UNDETERMINED (it ended
early)" every weekday at 08:03 ET (earnings_preprocess) and 09:00 ET
(--mode daily) because the page treated ANY result without
`stop_coverage_gaps` as a session that failed to audit. Those modes have no
audit in their contract. The fix: each session class declares
`AUDITS_BROKER_STOPS`, and the page reads that declaration through
src/sessions/stop_audit.py. The enumeration test below keeps declaration
and audit from drifting apart in either direction.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

from src.sessions.stop_audit import (
    STOP_COVERAGE_KEY,
    mode_audits_broker_stops,
    session_class_for_mode,
)
from src.trader_feed.naked import (
    naked_position_alert,
    protection_undetermined_alert,
    send_naked_position_alert,
)

ROOT = Path(__file__).resolve().parents[1]


def _every_mode() -> set[str]:
    """Every mode main.py accepts plus every job name the scheduler runs."""
    main_src = (ROOT / "main.py").read_text()
    choices = re.search(r'"--mode",\s*choices=\[(.*?)\]', main_src, re.S)
    assert choices, "main.py --mode choices not found"
    modes = set(re.findall(r'"(\w+)"', choices.group(1)))
    sched_src = (ROOT / "src" / "scheduler.py").read_text()
    modes |= set(re.findall(r'args=\[self\.pipeline\.\w+,\s*"(\w+)"\]', sched_src))
    assert {"earnings_preprocess", "daily", "morning", "intra_check", "evening"} <= modes
    return modes


def _module_writes_audit(cls) -> bool:
    return STOP_COVERAGE_KEY in inspect.getsource(sys.modules[cls.__module__])


def _check_declaration_matches_audit(mode: str) -> None:
    cls = session_class_for_mode(mode)  # KeyError = unregistered mode
    if cls is None:
        return
    declared = bool(getattr(cls, "AUDITS_BROKER_STOPS", False))
    writes = _module_writes_audit(cls)
    assert declared == writes, (
        f"{mode} -> {cls.__name__}: AUDITS_BROKER_STOPS={declared} but its module "
        f"{'writes' if writes else 'never writes'} {STOP_COVERAGE_KEY}"
    )


@pytest.mark.parametrize("mode", sorted(_every_mode()))
def test_every_mode_is_registered_and_declaration_matches_the_audit(mode):
    _check_declaration_matches_audit(mode)


def test_the_enumeration_check_can_fail_in_both_directions(monkeypatch):
    morning = session_class_for_mode("morning")
    assert morning.AUDITS_BROKER_STOPS is True
    monkeypatch.setattr(morning, "AUDITS_BROKER_STOPS", False)
    with pytest.raises(AssertionError, match="writes stop_coverage_gaps"):
        _check_declaration_matches_audit("morning")

    earnings = session_class_for_mode("earnings_preprocess")
    assert earnings.AUDITS_BROKER_STOPS is False
    monkeypatch.setattr(earnings, "AUDITS_BROKER_STOPS", True)
    with pytest.raises(AssertionError, match="never writes"):
        _check_declaration_matches_audit("earnings_preprocess")


def test_unregistered_mode_fails_the_enumeration():
    with pytest.raises(KeyError):
        session_class_for_mode("some_new_mode_nobody_registered")


def test_the_two_measured_false_pages_are_now_silent():
    # 08:03 ET earnings_preprocess, 09:00 ET --mode daily: no audit in contract.
    assert protection_undetermined_alert({"status": "ok", "run_id": "x"}, mode="earnings_preprocess") is None
    assert protection_undetermined_alert({"status": "error", "error": "no data"}, mode="daily") is None
    assert protection_undetermined_alert({"status": "scheduler_exited"}, mode="live") is None
    assert protection_undetermined_alert(None, mode="meta") is None


@pytest.mark.parametrize("mode", ["morning", "once", "midday", "close", "evening", "intra_check", "intra_safety"])
def test_an_auditing_mode_that_ended_early_or_died_still_pages(mode):
    assert mode_audits_broker_stops(mode)
    early = protection_undetermined_alert({"status": "evidence_gate_skip", "run_id": "x"}, mode=mode)
    assert early and "PROTECTION UNDETERMINED" in early and "ended early" in early
    died = protection_undetermined_alert(None, mode=mode)
    assert died and "failed before it finished" in died
    assert protection_undetermined_alert({"stop_coverage_gaps": []}, mode=mode) is None


def test_unknown_or_unnamed_mode_fails_closed_and_pages():
    assert protection_undetermined_alert({"status": "ok"}) is not None
    assert protection_undetermined_alert({"status": "ok"}, mode="not_a_mode") is not None


def test_no_stop_at_all_page_is_unchanged_by_mode():
    naked = {"stop_coverage_gaps": [{"symbol": "AAPL", "qty": 10, "covered_qty": 0, "gap_qty": 10}]}
    text = naked_position_alert(naked)
    assert text and "NO STOP AT ALL" in text and "AAPL" in text


def test_send_is_silent_for_a_non_auditing_mode_and_loud_for_an_auditing_one():
    sent: list[str] = []

    class Sender:
        def send(self, text, **kw):
            sent.append(text)
            return True

    assert send_naked_position_alert(Sender(), {"status": "ok"}, mode="earnings_preprocess") is False
    assert sent == []
    send_naked_position_alert(Sender(), None, mode="morning")
    assert sent and "PROTECTION UNDETERMINED" in sent[0]
