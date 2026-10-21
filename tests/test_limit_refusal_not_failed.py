"""21 Oct 2026 live defect (Telegram, intra_check): a buy the desk's own
single-stock limit refused before any order existed was filed under
"FAILED" and the header said FAILED. A limit doing its job is a refusal,
not a breakage. Genuine failures (broker rejects) must still read FAILED."""

from unittest.mock import patch

from src import trader_feed
from tests.test_trader_feed import (
    _QUIET_TICK_TIME,
    _agent_log,
    _evidence,
    _make_db,
    _pin_clock,
    _trade,
)

LIMIT_SENTENCE = "RSG would go over the 65% limit for one stock (it would be 65.003%)"


def _render(tmp_path, monkeypatch, run, with_reject=False):
    db = _make_db(tmp_path, monkeypatch)
    _evidence(
        db,
        run,
        "pipeline",
        "pipeline_event",
        {"stage": "deterministic_gate", "outcome": "blocked", "reason": "hard_risk", "detail": LIMIT_SENTENCE},
        symbol="RSG",
    )
    # Same tick as the live message: a deliberate sizing skip made it actionable.
    _evidence(
        db,
        run,
        "execution",
        "execution_skip",
        {"symbol": "ZZZ", "reason": "qty_zero", "detail": "sizing rounds to zero"},
        symbol="ZZZ",
    )
    if with_reject:
        _trade(db, run, "ABC", "BUY", status="rejected")
    _agent_log(db, run, "portfolio_manager", "x")
    outer = {
        "status": "ok",
        "run_id": run,
        "daily_pnl": 0.0,
        "daily_return_pct": 0.0,
        "intraday_scan": {"status": "intraday_no_trades", "run_id": run, "candidates": ["RSG"], "orders": []},
    }
    _pin_clock(monkeypatch, _QUIET_TICK_TIME)
    with patch("src.data.company.CompanyProfileStore.get_many", lambda self, symbols, allow_fetch=True: {}):
        return trader_feed.format_session_result("intra_check", outer, 1.0)


def test_single_name_limit_refusal_is_not_taken_and_header_not_failed(tmp_path, monkeypatch):
    msg = _render(tmp_path, monkeypatch, "run-limit")
    assert msg is not None
    assert "<b>🚫 NOT TAKEN</b>" in msg
    assert LIMIT_SENTENCE in msg
    assert "FAILED" not in msg


def test_broker_rejection_still_reads_failed(tmp_path, monkeypatch):
    msg = _render(tmp_path, monkeypatch, "run-reject", with_reject=True)
    assert msg is not None
    assert "<b>❌ FAILED</b>" in msg
    assert "· FAILED" in msg.splitlines()[0]
    # the limit refusal beside it is still a refusal
    assert "<b>🚫 NOT TAKEN</b>" in msg
