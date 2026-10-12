"""Money paths read their parts directly, so a missing part raises at the read (part 2).

Each site below used to read its part with ``getattr(obj, "_x", None)`` and
silently skip the rule when the part was gone (no dollar-target refusal, no
reduce-only clamp, no cash-park split). Every part is either set on every run
before the read (``_plan_risk_targets`` resets both constructor sets at its
top, with no early return before) or is a method on the owner's class
(``TradingPipeline._sweeper``), so a missing one is a wiring fault.

The AI cost limit needs no runtime alarm: an unset limit falls back to the
settings defaults ($0.90 per session, $1.50 per day), and settings loading
refuses a zero, negative or infinite limit. Both are pinned below.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.config.llm_cost import LLMCostCircuitConfig
from src.models.portfolio import TargetPosition
from src.models.positions import Position
from src.portfolio_constructor import PortfolioConstructor
from src.stage_decision import DecisionStage
from src.stage_risk import RiskStage
from tests.pipeline_factory import build_pipeline


def _target() -> TargetPosition:
    return TargetPosition(symbol="AAPL", target_weight_pct=3.0, conviction="medium", thesis="add")


@pytest.fixture
def constructor(monkeypatch):
    """A constructor whose risk planner returns no plan and sets nothing, so
    each test controls exactly which per-run set exists."""
    c = PortfolioConstructor()
    monkeypatch.setattr(c, "_plan_risk_targets", lambda *a, **k: {})
    return c


def test_constructor_raises_without_refused_dollar_set(constructor):
    with pytest.raises(AttributeError, match="_dollar_targets_refused"):
        constructor.construct_orders([_target()], [], [], 100_000.0)


def test_constructor_raises_without_reduce_only_set(constructor):
    constructor._dollar_targets_refused = set()
    with pytest.raises(AttributeError, match="_reduce_only"):
        constructor.construct_orders([_target()], [], [], 100_000.0)


def test_constructor_unchanged_when_sets_present(constructor):
    constructor._dollar_targets_refused = set()
    constructor._reduce_only = set()
    orders = constructor.construct_orders([_target()], [], [], 100_000.0)
    assert isinstance(orders, list)


@pytest.fixture
def pipe_without_sweeper(monkeypatch):
    pipe = build_pipeline()
    monkeypatch.delattr(type(pipe), "_sweeper")
    return pipe


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="r-1",
        positions=[],
        analyses=[],
        news_intel=None,
        earnings_results=None,
        macro_analysis=None,
        total_value=100_000.0,
        deployable_cash=100_000.0,
        last_equity=100_000.0,
        portfolio_decision=SimpleNamespace(decisions=[]),
        data_status=None,
    )


def test_decision_stage_raises_without_sweeper(pipe_without_sweeper):
    pipe = pipe_without_sweeper
    with pytest.raises(AttributeError, match="_sweeper"):
        DecisionStage(pipeline=pipe).run(_ctx())


def test_risk_stage_raises_without_sweeper(pipe_without_sweeper, monkeypatch):
    monkeypatch.setattr("src.stage_risk._isolate_empty_soft_exit_entries", lambda *a: False)
    pipe = pipe_without_sweeper
    with pytest.raises(AttributeError, match="_sweeper"):
        RiskStage(pipeline=pipe)._run_review(_ctx())


def test_real_planner_resets_both_sets_before_a_close_reads_them():
    """No stub: the real risk planner runs inside the order builder, sets both
    per-run sets before they are read, and a full close of a held name is
    emitted without AttributeError."""
    c = PortfolioConstructor()
    held = Position(
        symbol="AAPL",
        qty=10,
        avg_entry=100.0,
        current_price=100.0,
        market_value=1_000.0,
        unrealized_pnl=0.0,
        sector="Technology",
    )
    close = TargetPosition(symbol="AAPL", target_weight_pct=0.0, conviction="medium", thesis="exit")
    assert not hasattr(c, "_dollar_targets_refused")
    assert not hasattr(c, "_reduce_only")
    orders = c.construct_orders([close], [held], [], 100_000.0, price_map={"AAPL": 100.0})
    assert c._dollar_targets_refused == set()
    assert c._reduce_only == set()
    assert [(o.symbol, o.action) for o in orders] == [("AAPL", "SELL")]


def test_unset_ai_cost_limit_falls_back_to_defaults():
    cfg = LLMCostCircuitConfig()
    assert cfg.session_cost_limit_usd == 0.90
    assert cfg.daily_cost_limit_usd == 1.50


@pytest.mark.parametrize("field", ["session_cost_limit_usd", "daily_cost_limit_usd"])
@pytest.mark.parametrize("value", [0, -1.0, float("inf")])
def test_settings_refuse_zero_negative_or_infinite_ai_cost_limit(field, value):
    with pytest.raises(ValidationError):
        LLMCostCircuitConfig(**{field: value})
