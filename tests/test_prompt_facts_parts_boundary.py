"""Boundary witnesses: the lifted prompt-facts signal helpers build and run with no pipeline behind them.

Every collaborator is an explicit keyword-only constructor argument, so the class is
built from stubs alone (clause 5 of tests/boundary_harness.py).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.prompt_facts.missed_ops_signals import MissedOpsSignals
from tests.boundary_harness import check_boundary


def _build(cls, **overrides):
    params = inspect.signature(cls).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return cls(**kwargs)


LIFTED = [MissedOpsSignals]


@pytest.mark.parametrize("cls", LIFTED)
def test_every_lifted_piece_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", ["src.prompt_facts.missed_ops_signals"])
def test_every_lifted_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_held_set_reads_only_the_db_collaborator():
    db = MagicMock(name="db")
    db.get_positions_snapshot = MagicMock(return_value=[])
    sig = _build(MissedOpsSignals, db=db)
    assert sig.db is db and sig._parse_logged_agent_response is not None


def test_macro_sector_map_tolerates_an_empty_macro_store():
    macro_store = MagicMock(name="macro_store")
    macro_store.latest = MagicMock(return_value=None)
    macro_store.get_latest = MagicMock(return_value=None)
    sig = _build(MissedOpsSignals, macro_store=macro_store)
    out = sig._missed_ops_macro_sector_map()
    assert isinstance(out, dict)


# --- The trade-review parts (src/prompt_facts/review/): built and run with no pipeline behind them.

from src.prompt_facts.review.blocked import ReviewBlocked  # noqa: E402
from src.prompt_facts.review.calibration import ReviewCalibration  # noqa: E402
from src.prompt_facts.review.exits import ReviewExits  # noqa: E402
from src.prompt_facts.review.grading import ReviewGrading  # noqa: E402
from src.prompt_facts.review.replay import ReviewReplay  # noqa: E402

REVIEW_PARTS = [ReviewGrading, ReviewExits, ReviewCalibration, ReviewBlocked, ReviewReplay]
REVIEW_MODULES = [f"src.prompt_facts.review.{m}" for m in ("grading", "exits", "calibration", "blocked", "replay")]


@pytest.mark.parametrize("cls", REVIEW_PARTS)
def test_every_review_part_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", REVIEW_MODULES)
def test_every_review_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_recent_performance_runs_against_a_stub_db():
    """Exercised, not just built: an empty daily-pnl history yields a dict and reads only the db."""
    db = MagicMock(name="db")
    db.get_daily_pnl = MagicMock(return_value=[])
    part = _build(ReviewCalibration, db=db)
    out = part._compute_recent_performance(current_equity=100_000.0)
    assert isinstance(out, dict)
    db.get_daily_pnl.assert_called()


def test_exits_part_reads_the_sweeper_live():
    """The sweeper is a callable read per use, never snapshotted at construction."""
    calls = []
    part = _build(ReviewExits, sweeper=lambda: calls.append("read") or None)
    assert part._sweeper() is None and calls == ["read"]


# --- The parent prompt-facts parts (src/prompt_facts/*.py): built and run with no pipeline behind them.

from src.prompt_facts.decisions import PromptDecisions  # noqa: E402
from src.pipeline_prompt_facts import PromptExposure  # noqa: E402
from src.prompt_facts.heat import PromptHeat  # noqa: E402
from src.pipeline_prompt_facts import PromptHistory  # noqa: E402
from src.prompt_facts.pm_facts import PromptPMFacts  # noqa: E402
from src.pipeline_prompt_facts import PromptPositionFacts  # noqa: E402
from src.prompt_facts.projected import PromptProjected  # noqa: E402
from src.prompt_facts.watchlist import PromptWatchlist  # noqa: E402

FACT_PARTS = [
    PromptHistory,
    PromptDecisions,
    PromptProjected,
    PromptWatchlist,
    PromptExposure,
    PromptHeat,
    PromptPMFacts,
    PromptPositionFacts,
]
FACT_MODULES = [f"src.prompt_facts.{m}" for m in ("decisions", "projected", "watchlist", "heat", "pm_facts")]


@pytest.mark.parametrize("cls", FACT_PARTS)
def test_every_fact_part_is_constructible_from_stubs(cls):
    _build(cls)
    params = inspect.signature(cls).parameters
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@pytest.mark.parametrize("module", FACT_MODULES)
def test_every_fact_module_passes_the_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_heat_part_is_handed_the_stop_map_not_owning_it():
    """Portfolio heat reads the stop map through the host's shim; the heat part never defines it, and the sweeper is read per use."""
    assert not hasattr(PromptHeat, "_build_stop_map") and hasattr(PromptExposure, "_build_stop_map")
    reads = []
    part = _build(
        PromptHeat,
        sweeper=lambda: reads.append("sweeper") or None,
        build_stop_map=lambda positions: reads.append("stop_map") or ({}, {}, set()),
    )
    part._build_portfolio_heat([MagicMock(name="pos", symbol="AAA")], 100_000.0)
    assert reads[:2] == ["sweeper", "stop_map"]


def test_pm_facts_part_is_handed_heat_and_history_not_owning_them():
    assert not hasattr(PromptPMFacts, "_build_portfolio_heat") and not hasattr(PromptPMFacts, "_build_position_history")
    part = _build(PromptPMFacts, build_portfolio_heat=lambda *a, **k: "HEAT", build_position_history=lambda *a, **k: {})
    assert part._build_portfolio_heat() == "HEAT" and part._build_position_history() == {}
    assert "config" in inspect.signature(PromptPMFacts).parameters, (
        "the body reads getattr(self, 'config') by string; it must be handed in"
    )


def test_projected_part_holds_no_sector_cache_and_writes_onto_the_run():
    """The sector map belongs to ONE run: the part keeps none, the host
    keeps none, and the write lands on the run context handed in."""
    part = _build(PromptProjected)
    assert not hasattr(part, "_last_symbol_sectors") and not hasattr(part, "_sector_cache_owner")
    assert "sector_cache_owner" not in inspect.signature(PromptProjected).parameters
    assert "run" in inspect.signature(PromptProjected._build_projected_portfolio).parameters


def test_decisions_part_runs_against_a_stub_db():
    """Exercised, not just built: a db that returns nothing yields a string and reads only the db."""
    db = MagicMock(name="db")
    part = _build(PromptDecisions, db=db)
    assert isinstance(part._build_pm_recent_decisions(), str)
    assert db.method_calls, "the body never read the db collaborator"


# --- The trade-review SEAT (src/prompt_facts/review/held.py): built alone from its collaborators
# ONLY -- no host object exists anywhere in these tests. The pipeline holds one, rebuilt per call
# from its own collaborators by `review_of`, and inherits nothing.

from src.prompt_facts.review.held import COLLABORATORS, PromptFactsReview  # noqa: E402


def test_review_seat_is_constructible_with_no_host_at_all_and_passes_the_boundary_check():
    """Built from NOTHING: no host, no namespace standing in for one, no keyword named host."""
    part = PromptFactsReview()
    params = list(inspect.signature(PromptFactsReview).parameters.values())
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)
    assert tuple(p.name for p in params) == COLLABORATORS and len(COLLABORATORS) == 6
    assert "host" not in COLLABORATORS and not hasattr(part, "_host")
    assert all(getattr(part, name) is None for name in COLLABORATORS)  # bare: nothing raises
    source = inspect.getsource(PromptFactsReview)  # the class body never names a host or reaches one
    assert "_host" not in source and "host=" not in source and "getattr(" not in source
    verdict = check_boundary("src.prompt_facts.review.held")
    assert verdict.passed, verdict.failures


def test_review_seat_runs_a_body_from_an_explicit_db_collaborator_and_nothing_else():
    """Exercised, not just built; the db it was handed is the one that runs, and only that one."""
    db = MagicMock(name="db")
    db.get_daily_pnl.return_value = []
    part = PromptFactsReview(db=db)
    assert isinstance(part._compute_recent_performance(current_equity=100_000.0), dict)
    db.get_daily_pnl.assert_called_once()
    assert part._review_calibration().db is db and part._review_blocked().db is db


def test_review_seat_hands_each_family_part_exactly_the_collaborator_it_was_built_with():
    """`sweeper` and `exit_audit_actions` the
    exits part, the operator logger the calibration part -- as passed, with no host in between."""
    marks = {name: object() for name in COLLABORATORS}
    marks["sweeper"] = lambda: "SWEPT"
    part = PromptFactsReview(**marks)
    assert part._review_grading().broker is marks["broker"] and part._review_grading().market is marks["market"]
    assert part._review_exits()._sweeper() == "SWEPT"
    assert part._review_exits()._EXIT_AUDIT_ACTIONS is marks["exit_audit_actions"]
    logger = marks["log_conviction_outcome_for_operator"]
    assert part._review_calibration()._log_conviction_outcome_for_operator is logger
