"""Owner ruling 2026-10-13: the PAID portfolio-manager ("what to buy") call
runs at most once per hour in the intraday cycle, which checks every 30
minutes. The last call time is the durable agent_logs record of the last
intraday portfolio_manager row, so a restart cannot reset it. A check inside
the hour still runs the free held-book review; only the paid call is skipped.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.pipeline_context import RunContext
from src.storage.db import Database
from tests.test_intraday_no_paid_call_when_full import _EQUITY, _book, _ceiling, _ta_log
from tests.test_intraday_scan import _intraday_pipeline, _snapshot, _ta_result

_T0 = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)


def _check(db_path: str, now: datetime) -> dict:
    """One intraday check at `now` against the real agent_logs at `db_path`; returns what happened.

    A fresh pipeline and a fresh Database each call: every check is as if the
    process had just restarted, so only durable state carries over.
    """
    real_db = Database(db_path)
    real_db.initialize()
    names = ["M00", "M01", "M02"]
    p = _intraday_pipeline(universe=names + ["OLD", "BIG"], atr=2.0)
    p.db.execute = real_db.execute
    p.config.risk.allow_margin = True
    _ceiling(p)
    p.portfolio_constructor.entry_stop_preview = MagicMock(return_value=(100.0, 95.0, 2.0))
    p.broker.get_intraday_snapshots.return_value = {
        s: _snapshot(last=101.0 + i * 0.1, prev=100.0) for i, s in enumerate(names)
    }
    p.db.get_recent_intraday_evaluations.return_value = []
    p._recently_intraday_evaluated = MagicMock(return_value=False)
    p.tech_analyst.analyze_batch.side_effect = lambda data, **kw: (
        {d["symbol"]: _ta_result(d["symbol"]) for d in data},
        _ta_log(),
    )
    seen = {"pm_calls": 0, "held_book_reviews": 0}

    def _decide(ctx):
        seen["held_book_reviews"] += 1
        if not getattr(ctx, "pm_skip_reason", None):
            seen["pm_calls"] += 1
            # What the real portfolio_manager seat leaves behind: its agent_logs row.
            real_db.execute(
                "INSERT INTO agent_logs (agent_name, run_id, cost_usd, timestamp) VALUES (?, ?, ?, ?)",
                ("portfolio_manager", ctx.run_id, 0.16, now.strftime("%Y-%m-%d %H:%M:%S")),
            )
            real_db.conn.commit()
        ctx.portfolio_decision = SimpleNamespace(decisions=[])

    p.decision_stage.run.side_effect = _decide
    p.execution_stage.run.return_value = []
    ctx = RunContext.start("intra_check")
    ctx.positions = _book(0.5 * _EQUITY)
    ctx.total_value = _EQUITY
    ctx.cash = 0.0
    ctx.deployable_cash = 0.0
    with (
        patch("src.pipeline_intraday._utcnow", return_value=now),
        patch("src.pipeline_intraday.compute_indicators", return_value=MagicMock()),
        patch("src.pipeline_candidate_records._persist_evidence"),
    ):
        seen["result"] = p._run_intraday_opportunity_scan(ctx)
    real_db.conn.close()
    return seen


def test_paid_call_at_t_skipped_at_half_hour_and_back_at_the_hour_across_restarts():
    db_path = str(Path(tempfile.mkdtemp()) / "t.db")

    first = _check(db_path, _T0)
    assert first["pm_calls"] == 1

    # 30 minutes later, a new process: the paid call is skipped, the free held-book review still runs.
    half = _check(db_path, _T0 + timedelta(minutes=30))
    assert half["pm_calls"] == 0
    assert half["held_book_reviews"] == 1
    assert half["result"]["pm_call_skipped"] == "pm_cadence"

    hour = _check(db_path, _T0 + timedelta(minutes=60))
    assert hour["pm_calls"] == 1


def test_a_morning_portfolio_manager_call_does_not_count_against_the_intraday_hour():
    db_path = str(Path(tempfile.mkdtemp()) / "t.db")
    db = Database(db_path)
    db.initialize()
    db.execute(
        "INSERT INTO agent_logs (agent_name, run_id, timestamp) VALUES (?, ?, ?)",
        ("portfolio_manager", "morning-x", (_T0 - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.conn.commit()
    db.conn.close()
    assert _check(db_path, _T0)["pm_calls"] == 1
