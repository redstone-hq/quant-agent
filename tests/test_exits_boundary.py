"""Boundary witnesses: the lifted exit-engine pieces build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so each
class is built from stubs alone (clause 5 of tests/boundary_harness.py).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from src.exits.alignment_exit import AlignmentExit
from src.exits.exit_substantiation import ExitSubstantiation
from src.exits.holding_discipline import HoldingDiscipline
from src.exits.structural_protection import StructuralProtection
from src.exits.target_revision import TargetRevision
from src.trading_calendar import et_today


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


class _Db:
    """A db whose every query returns one fixed row."""

    def __init__(self, row):
        self.row = row

    def __getattr__(self, name):
        return lambda *a, **k: self.row


@pytest.mark.parametrize(
    "cls",
    [
        AlignmentExit,
        ExitSubstantiation,
        HoldingDiscipline,
        StructuralProtection,
        TargetRevision,
    ],
)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    obj = _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert len(params) > 0


def test_holding_discipline_ignores_a_non_exit_action():
    structural = MagicMock(name="structural_protection_for_holding")
    hd = _build(HoldingDiscipline, structural_protection_for_holding=structural)
    hd._holding_discipline_check_for_exit(
        symbol="AAPL",
        action="HOLD",
        reason="nothing to do",
        positions=[],
        run_id="r1",
    )
    structural.assert_not_called()


def test_position_opened_today_reads_the_recorded_timestamp():
    assert AlignmentExit is not None
    ae_none = _build(AlignmentExit, db=_Db(None))
    assert ae_none._position_opened_today("AAPL", is_short=False) is False
    ae_today = _build(AlignmentExit, db=_Db({"timestamp": f"{et_today()}T14:00:00"}))
    assert ae_today._position_opened_today("AAPL", is_short=False) is True


def test_target_revision_with_nothing_flagged_and_nothing_held_adjudicates_nothing():
    file_row = MagicMock(name="file_target_revision")
    tr = _build(TargetRevision, file_target_revision=file_row)
    out = tr._adjudicate_target_revision_flags(
        MagicMock(target_revision_flags=[]),
        [],
        run_id="r1",
        seat="technical",
    )
    assert out == []
    file_row.assert_not_called()


# --- The second lift: the exit records, built and run with no pipeline behind them.
# (The trails and the AI risk review stay on the mixin: the broker-seam importer
# freeze and the import-cycle guard both refuse them as standalone modules.)

from datetime import datetime, timezone  # noqa: E402

from src.exits.exit_records import ExitRecords  # noqa: E402
from tests.boundary_harness import check_boundary  # noqa: E402

SECOND_LIFT = [ExitRecords]
SECOND_LIFT_MODULES = ["src.exits.exit_records"]


@pytest.mark.parametrize("cls", SECOND_LIFT)
def test_second_lift_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert params and all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", SECOND_LIFT_MODULES)
def test_second_lift_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_trail_cooldown_reads_only_the_db_collaborator():
    fresh = datetime.now(timezone.utc).isoformat()
    tightened = _build(ExitRecords, db=_Db([{"action": "TRAIL_STOP", "timestamp": fresh}]))
    assert tightened._trail_tightened_recently("AAPL") is True
    quiet = _build(ExitRecords, db=_Db([]))
    assert quiet._trail_tightened_recently("AAPL") is False


def test_exit_review_approvals_record_only_the_unvetoed_symbols():
    record = MagicMock(name="record_exit_refusal")
    rec = _build(ExitRecords, record_exit_refusal=record)
    decisions = [MagicMock(symbol="AAA", action="SELL"), MagicMock(symbol="BBB", action="REDUCE")]
    rec._record_exit_review_approvals(
        decisions,
        {"BBB"},
        MagicMock(reason_category="x", reasoning="fine"),
        run_id="r1",
        original_action_by_symbol={},
    )
    assert record.call_count == 1
    assert record.call_args.kwargs["symbol"] == "AAA"
    assert record.call_args.kwargs["dropped"] is False
