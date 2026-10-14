"""Owner ruling 2026-10-14: one line per stock, not the same stock several times.

The exit stage used to page the owner with the guard's provisional verdict
mid-session, and then the session message said the same stock again, with a
verdict that could disagree. Now the exit stage only RECORDS its verdict, and
the session message carries one line per stock built from what every role
recorded. A failure still goes out alone and at once (owner ruling 2 Sep).
"""

from unittest.mock import patch

from src import trader_feed
from src.risk.exit_guard import StructuralProtectionCheck
from src.risk.exit_guard_trend import render_owner_break_message
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline
from tests.test_trader_feed import _QUIET_TICK_TIME, _evidence, _make_db, _pin_clock, _trade

_RUN = "run-one-line"

# The reason text the guard really records when a stated exit condition was
# met but the break is not yet confirmed (it opens with "its").
_KEPT = StructuralProtectionCheck(
    protected=True,
    basis="thesis_invalid_if_pending_confirmation",
    detail="pending confirmation",
    owner_reason=(
        "its stated exit condition was met on the close, but the break isn't "
        "confirmed yet; holding one more session to rule out a one-day false breakdown."
    ),
)


def _exit_stage(tmp_path, monkeypatch):
    """A real pipeline over the same DB file the session message reads."""
    path = _make_db(tmp_path, monkeypatch)
    db = Database(str(path))
    db.initialize()
    return path, build_pipeline(db=db)


def test_exit_stage_sends_nothing_and_session_line_names_both_verdicts(tmp_path, monkeypatch):
    path, pipeline = _exit_stage(tmp_path, monkeypatch)
    with patch("src.notifier.send_owner_alert", return_value=True) as alert:
        pipeline._voice_structural_protection_break(symbol="ETN", run_id=_RUN, check=_KEPT)
    assert alert.call_count == 0, "the exit stage must not page the owner about a provisional verdict"

    _trade(path, _RUN, "ETN", "SELL", qty=10, price=350, reasoning="The trend has broken. Exiting.")
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    msg = trader_feed.format_session_result(
        "morning",
        {"status": "executed", "run_id": _RUN, "orders": [{"symbol": "ETN"}]},
        30.0,
    )
    # Every line that carries a verdict about ETN: exactly one, naming both.
    etn_lines = [line for line in msg.splitlines() if "ETN" in line and ("Guard" in line or "guard" in line)]
    assert len(etn_lines) == 1, msg
    line = etn_lines[0]
    assert "SELL ETN" in line and "Guard said keep" in line and "PM sold" in line, line
    assert "hold-protection" not in msg  # the alert's wording is not repeated as a second line


def test_guard_verdict_row_alone_gives_one_line_for_a_stock_with_no_order(tmp_path, monkeypatch):
    path = _make_db(tmp_path, monkeypatch)
    _evidence(
        path,
        _RUN,
        "risk_manager",
        "structural_break_trend_context",
        {"protected": True, "basis": "x", "reason": _KEPT.owner_reason},
        symbol="ETN",
    )
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    msg = trader_feed.format_session_result("morning", {"status": "no_trades", "run_id": _RUN, "orders": []}, 30.0)
    etn_lines = [line for line in msg.splitlines() if "ETN" in line and "Guard" in line]
    assert len(etn_lines) == 1, msg
    assert "no order this session" in etn_lines[0]


def test_reason_starting_its_gives_no_it_its():
    msg = render_owner_break_message("ETN", _KEPT)
    assert msg is not None
    assert "it its" not in msg.lower()
    assert "ETN: hold-protection kept — its stated exit condition" in msg
    # A reason without its own subject keeps the "it" the template adds.
    plain = StructuralProtectionCheck(
        protected=False,
        basis="structural_level_broken",
        detail="d",
        owner_reason="closed decisively below the 90 support, confirmed over two closes.",
    )
    assert "— it closed decisively below" in render_owner_break_message("ETN", plain)


def test_a_failure_still_sends_at_once_and_alone(tmp_path, monkeypatch):
    # The verdict row cannot be written, so the session message would have
    # nothing to read: the verdict goes out on its own, immediately, once,
    # for that stock only, and says why it is on its own.
    _path, pipeline = _exit_stage(tmp_path, monkeypatch)
    with (
        patch.object(pipeline.db, "insert_specialist_evidence", side_effect=RuntimeError("db down")),
        patch("src.notifier.send_owner_alert", return_value=True) as alert,
    ):
        pipeline._voice_structural_protection_break(symbol="ETN", run_id=_RUN, check=_KEPT)
    assert alert.call_count == 1
    text = alert.call_args.args[0]
    assert alert.call_args.kwargs["symbols"] == ["ETN"]
    assert text.startswith("ETN:") and "sent on its own" in text
    assert ("run-one-line", "ETN", "thesis_invalid_if_pending_confirmation") in pipeline._voiced_structural_breaks
