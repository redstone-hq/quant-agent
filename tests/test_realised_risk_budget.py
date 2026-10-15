"""Board item 186 — the realised total at-risk, one durable row per run.

WHY. `max_portfolio_risk_pct` (25) is owner-ratified appetite with no
measurement behind it, and the owner's
2026-09-30 ruling ("risk is never a global dial") bars picking a replacement
value. What is buildable without inventing a number is the OBSERVATION: write
down what the book's realised concentration actually was, session by session.

RECORDING ONLY. These tests assert the row's shape, its unit and its NULL
discipline. Nothing here derives, tunes or proposes a ceiling, and nothing in
the product reads these rows back into a sizing, ordering or refusal decision.
"""

import json

import pytest

from src.risk.budget import RiskRequest, allocate_risk_budget
from src.storage.db import Database
from src.storage.risk_budget_record import (
    ensure_table,
    record_realised_risk_budget,
)


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    ensure_table(d.conn)
    return d


def _rows(db):
    cur = db.conn.execute("SELECT * FROM realised_risk_budget")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _allocation():
    """A real allocator run, not a stub: the recording must describe the
    object that actually rations orders in production."""
    return allocate_risk_budget(
        [RiskRequest("AAA", 5.0), RiskRequest("BBB", 5.0), RiskRequest("CCC", 5.0)],
        ceiling_pct=25.0,
        floor_pct=0.5,
    )


def test_a_real_allocator_run_is_recorded(db):
    alloc = _allocation()
    assert (
        record_realised_risk_budget(
            db,
            allocation=alloc,
            equity=100_000.0,
            run_id="run-1",
        )
        is True
    )
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["allocator_ran"] == 1
    assert row["equity"] == 100_000.0
    assert row["ceiling_pct"] == 25.0
    # At-risk carried by held names no request touched, read off the
    # allocator's own "held + granted" arithmetic — not a second estimate.
    granted = sum(g.granted_pct for g in alloc.grants.values())
    assert row["held_only_pct"] == pytest.approx(alloc.committed_pct - granted)
    assert row["committed_pct"] == pytest.approx(alloc.committed_pct)
    # The cluster columns stay in the live table but are no longer written.
    assert row["cluster_share_pct"] is None
    assert row["cluster_shares_json"] is None
    grants = json.loads(row["grants_json"])
    assert {g["symbol"] for g in grants} == {"AAA", "BBB", "CCC"}
    assert row["rationed_names"] == sum(1 for g in grants if g["limited_by"])


def test_an_unknown_book_records_unknown_and_not_zero(db):
    """The allocator does not run when the held book's risk is unreadable.

    That is the state the ceilings go UNENFORCED in, so it must be visible as
    unknown — a row of zeros would read as a book with no concentration.
    """
    assert (
        record_realised_risk_budget(
            db,
            allocation=None,
            equity=100_000.0,
            run_id="run-2",
        )
        is True
    )
    row = _rows(db)[0]
    assert row["allocator_ran"] == 0
    assert row["committed_pct"] is None
    assert row["held_only_pct"] is None
    assert row["rationed_names"] is None
    assert row["equity"] == 100_000.0


def test_the_recording_is_idempotent_per_run(db):
    for _ in range(2):
        record_realised_risk_budget(
            db,
            allocation=_allocation(),
            equity=1.0,
            run_id="run-3",
        )
    assert len(_rows(db)) == 1


def test_a_fully_granted_book_carries_no_untouched_held_risk(db):
    """Every name in this run was requested, so nothing is held-only: the
    figure must be 0.0 and not silently absent."""
    record_realised_risk_budget(
        db,
        allocation=_allocation(),
        equity=1.0,
        run_id="run-4",
    )
    assert _rows(db)[0]["held_only_pct"] == pytest.approx(0.0)


def test_the_decision_stage_calls_the_recorder():
    """The column existing is not the check; something reaching it is.

    Three settlement recordings shipped in 2026 with live columns and no
    reachable write (`tests/test_settlement_recording_writes.py`), so this
    asserts the production call site exists rather than trusting the method.
    """
    import inspect

    from src import pipeline_risk_budget_recording, stage_decision

    src = inspect.getsource(stage_decision)
    both = inspect.getsource(pipeline_risk_budget_recording)
    assert "_record_realised_sector_weights(" in both
    assert "_record_realised_risk_budget(pipeline, ctx, total_value)" in both
    assert "_record_realised_concentration(" in src
