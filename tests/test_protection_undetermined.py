"""The PROTECTION UNDETERMINED page fires only for a mode whose session audits broker stops.

Measured defect: the owner was paged "PROTECTION UNDETERMINED (it ended
early)" every weekday at 08:03 ET (earnings_preprocess) and 09:00 ET
(--mode daily) because the page treated ANY result without
`stop_coverage_gaps` as a session that failed to audit. Those modes have no
audit in their contract. The fix: src/sessions/stop_audit.py (imports
nothing, so the api reach guard stays intact) declares which session each
mode runs and which sessions audit; each session class repeats the fact as
`AUDITS_BROKER_STOPS` beside its audit. This test is what ties the three
together -- class attribute, table, and the audit code -- so none can drift.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

from src.intraday.safety import IntradaySafety
from src.intraday.session import IntradaySession
from src.sessions.earnings_preprocess_session import EarningsPreprocessSession
from src.sessions.evening_session import EveningSession
from src.sessions.morning_session import MorningSession
from src.sessions.position_review_session import PositionReviewSession
from src.sessions.quarterly_meta_session import QuarterlyMetaReflectionSession
from src.sessions.stop_audit import (
    MODE_SESSION,
    SESSION_AUDITS_BROKER_STOPS,
    STOP_COVERAGE_KEY,
    mode_audits_broker_stops,
    session_name_for_mode,
)
from src.trader_feed.naked import (
    naked_position_alert,
    protection_undetermined_alert,
    send_naked_position_alert,
)

ROOT = Path(__file__).resolve().parents[1]
SESSION_CLASSES = {
    c.__name__: c
    for c in (
        IntradaySafety,
        IntradaySession,
        EarningsPreprocessSession,
        EveningSession,
        MorningSession,
        PositionReviewSession,
        QuarterlyMetaReflectionSession,
    )
}


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
    name = session_name_for_mode(mode)  # KeyError = unregistered mode
    if name is None:
        return
    cls = SESSION_CLASSES[name]  # KeyError = table names a class this test does not know
    on_class = bool(getattr(cls, "AUDITS_BROKER_STOPS", False))
    in_table = SESSION_AUDITS_BROKER_STOPS[name]
    assert on_class == in_table, f"{name}: class says {on_class}, stop_audit table says {in_table}"
    writes = _module_writes_audit(cls)
    assert in_table == writes, (
        f"{mode} -> {name}: declared AUDITS_BROKER_STOPS={in_table} but its module "
        f"{'writes' if writes else 'never writes'} {STOP_COVERAGE_KEY}"
    )


@pytest.mark.parametrize("mode", sorted(_every_mode()))
def test_every_mode_is_registered_and_declaration_matches_the_audit(mode):
    _check_declaration_matches_audit(mode)


def test_every_table_entry_names_a_real_session_class():
    assert set(SESSION_AUDITS_BROKER_STOPS) == set(SESSION_CLASSES)
    assert {n for n in MODE_SESSION.values() if n is not None} <= set(SESSION_CLASSES)


def test_the_enumeration_check_can_fail_in_every_direction(monkeypatch):
    monkeypatch.setattr(MorningSession, "AUDITS_BROKER_STOPS", False)
    with pytest.raises(AssertionError, match="class says False"):
        _check_declaration_matches_audit("morning")
    monkeypatch.undo()

    monkeypatch.setitem(SESSION_AUDITS_BROKER_STOPS, "MorningSession", False)
    monkeypatch.setattr(MorningSession, "AUDITS_BROKER_STOPS", False)
    with pytest.raises(AssertionError, match="writes stop_coverage_gaps"):
        _check_declaration_matches_audit("morning")
    monkeypatch.undo()

    monkeypatch.setitem(SESSION_AUDITS_BROKER_STOPS, "EarningsPreprocessSession", True)
    monkeypatch.setattr(EarningsPreprocessSession, "AUDITS_BROKER_STOPS", True)
    with pytest.raises(AssertionError, match="never writes"):
        _check_declaration_matches_audit("earnings_preprocess")


def test_unregistered_mode_fails_the_enumeration():
    with pytest.raises(KeyError):
        session_name_for_mode("some_new_mode_nobody_registered")


def test_stop_audit_module_imports_nothing_from_src():
    """src/api reaches src/trader_feed, which reads this module: it must not open a route to session code."""
    src = inspect.getsource(sys.modules["src.sessions.stop_audit"])
    assert not re.search(r"^\s*(from|import)\s+src", src, re.M)


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
