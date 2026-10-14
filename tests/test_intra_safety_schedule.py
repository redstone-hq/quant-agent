"""Board item 177: the FREE intra-preamble safety work has its own schedule.

Until 2026-10-01 fill reconcile, stop-out reconcile, the protection-restore
drain and the repeg drain existed only as the opening block of the PAID
intraday tick, so the only way to cut paid intraday spend was to cut
loss-protection latency with it. These tests pin the decoupling and, just as
importantly, pin that the paid tick still runs the same work through the same
single implementation -- the change is additive, never a removal.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from src import config as cfg
from src.intraday.safety import IntradaySafety
from src.intraday.session import IntradaySession
from src.pipeline import TradingPipeline

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "scripts" / "systemd"
WRAPPER = ROOT / "scripts" / "run_if_et_window.sh"

SAFETY_JOBS = (
    "_drain_pending_protection_restores",
    "_drain_pending_repegs",
    "_reconcile_stop_coverage",
    "_reconcile_fills",
    "_reconcile_stop_out_fills",
)


def test_the_free_safety_work_has_a_standalone_entry_point():
    assert callable(getattr(TradingPipeline, "run_intra_safety", None)), (
        "the free safety preamble must be runnable without the paid tick"
    )


@pytest.mark.parametrize("job", SAFETY_JOBS)
def test_each_safety_job_lives_in_the_shared_preamble_not_the_paid_body(job):
    """One implementation, two callers -- never a forked copy."""
    preamble = inspect.getsource(IntradaySafety._run_intra_safety_preamble)
    paid = inspect.getsource(IntradaySession._run_intra_check_body)
    assert f"self.{job}(" in preamble, f"{job} must run in the shared preamble"
    assert f"self.{job}(" not in paid, (
        f"{job} is duplicated into the paid tick body -- the two callers must "
        "share one implementation so they can never drift apart"
    )


def test_the_paid_tick_still_runs_the_preamble():
    """Decoupling must be ADDITIVE: the paid tick loses nothing."""
    paid = inspect.getsource(IntradaySession._run_intra_check_body)
    assert "self._run_intra_safety_preamble(run_id)" in paid
    assert callable(TradingPipeline._run_intra_safety_preamble) and callable(TradingPipeline._run_intra_check_body)


def test_both_callers_take_the_same_broker_write_lock():
    """Two units firing together cannot race (board item 127)."""
    preamble = inspect.getsource(IntradaySafety._run_intra_safety_preamble)
    assert "self._intraday_scan_process_lock()" in preamble
    assert "self._blocking_owner_session()" in preamble


def test_standalone_mode_is_wired_through_main():
    main_src = (ROOT / "main.py").read_text()
    assert '"intra_safety"' in main_src
    assert "pipeline.run_intra_safety()" in main_src


def test_wrapper_gives_intra_safety_the_same_window_as_intra_check(capsys):
    """The wrapper holds no minutes; the command it calls answers for both modes.
    Same work, same hours: the free half's window is intra_check's by construction."""
    from src.session_calendar import main

    answers = {}
    for mode in ("intra_check", "intra_safety"):
        assert main([mode, "2026-11-27", "--normal-day"]) == 0
        answers[mode] = capsys.readouterr().out.strip()
    assert answers["intra_safety"] == answers["intra_check"] != "", (
        "the free half runs over exactly the hours the work already ran over"
    )
    assert "intra_safety" in WRAPPER.read_text(), "the wrapper must still accept the mode"


def test_wrapper_exempts_intra_safety_from_the_once_per_day_and_session_locks():
    text = WRAPPER.read_text()
    assert '"$MODE" != "intra_safety"' in text, "must fire on every tick, not once a day"
    assert '"$MODE" == "intra_safety"' in text, "must be exempt from the cross-mode lock"


def _oncalendar(unit: Path) -> list[str]:
    return [line.split("=", 1)[1].strip() for line in unit.read_text().splitlines() if line.startswith("OnCalendar=")]


def test_safety_timer_exists_and_runs_the_safety_mode():
    service = UNITS / "quant-agent-intra_safety.service"
    assert service.exists()
    assert "run_if_et_window.sh intra_safety" in service.read_text()
    assert (UNITS / "quant-agent-intra_safety.timer").exists()


def test_safety_cadence_is_the_operator_set_interval_not_a_new_number():
    """The interval must be INTRA_CHECK_TICK_MINUTES, which is ledgered."""
    spec = _oncalendar(UNITS / "quant-agent-intra_safety.timer")
    assert len(spec) == 1
    minutes = re.search(r":(\d+),(\d+)", spec[0])
    assert minutes, spec[0]
    first, second = int(minutes.group(1)), int(minutes.group(2))
    assert second - first == cfg.INTRA_CHECK_TICK_MINUTES, (
        "the safety cadence must equal the operator-set intraday tick interval; "
        "any other spacing would be an invented number"
    )


def test_safety_timer_shares_a_second_with_no_other_unit():
    """2026-09-17: two units on the same second is how the stop race happened."""
    ours = _oncalendar(UNITS / "quant-agent-intra_safety.timer")[0]
    assert ours.endswith(":30"), "the phase carries an explicit seconds field"
    for unit in sorted(UNITS.glob("*.timer")):
        if unit.name == "quant-agent-intra_safety.timer":
            continue
        assert ours not in _oncalendar(unit), f"collides with {unit.name}"
