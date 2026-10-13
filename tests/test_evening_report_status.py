"""A suspended paid circuit must not make a delivered evening report read FAILED.

12 Oct: the daily AI cost cap tripped, the $0 evening news step was refused
because its preflight label "evening_news" named no agent (so it was treated as
paid), and the complete report went out headed "FAILED: paid thinking is
suspended".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.cost_circuit import PaidAnalysisSuspended
from src.notifier.session import format_session_result
from tests.pipeline_factory import build_pipeline
from tests.test_cost_circuit_free_route import FREE, PM, _agent, _circuit, _trip_daily_cap


def test_evening_run_under_daily_cap_reaches_its_free_news_step(tmp_path):
    circuit, _ = _circuit(tmp_path)
    circuit.activate_session("evening-x", "evening")
    _trip_daily_cap(circuit)
    pipeline = build_pipeline(
        broker=MagicMock(),
        db=MagicMock(),
        _drain_pending_protection_restores=MagicMock(),
        _reconcile_orphan_pending_submits=MagicMock(),
        _reconcile_stop_coverage=MagicMock(return_value=[]),
        _reconcile_fills=MagicMock(),
        _run_news_update=MagicMock(side_effect=RuntimeError("reached the news step")),
        cost_circuit=circuit,
    )
    pipeline.news_analyst = _agent(FREE)
    pipeline.evening_analyst = _agent(FREE)
    pipeline.portfolio_manager = _agent(PM)
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_account.return_value = {"portfolio_value": 10_200.0, "last_equity": 10_000.0}
    pipeline.broker.get_positions.return_value = []
    pipeline.broker.get_recent_daily_closes.return_value = []

    with pytest.raises(RuntimeError, match="reached the news step"):
        pipeline.run_evening()  # old code returned paid_analysis_suspended before this step
    # Paid calls stay refused while the cap is tripped.
    with pytest.raises(PaidAnalysisSuspended):
        pipeline._require_paid_analysis("portfolio_manager")


def test_delivered_evening_report_is_not_headed_failed_when_circuit_suspended():
    result = {
        "status": "paid_analysis_suspended",
        "paid_analysis_suspended": True,
        "run_id": "evening-1",
        "analysis": None,
        "total_value": 10_200.0,
        "daily_pnl": 200.0,
        "daily_return_pct": 2.0,
    }
    head = format_session_result("evening", result, 5.0).splitlines()[0]
    assert "FAILED" not in head
    assert "\U0001f6d1" not in head
    assert "report delivered in full" in head


def test_suspended_evening_without_figures_and_other_modes_still_fail():
    bare = {"status": "paid_analysis_suspended", "run_id": "e", "analysis": None}
    assert "FAILED" in format_session_result("evening", bare, 1.0).splitlines()[0]
    full = dict(bare, total_value=1.0)
    assert "FAILED" in format_session_result("morning", full, 1.0).splitlines()[0]
