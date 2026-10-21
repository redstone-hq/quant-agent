"""The midday/close position reviewer judges a proposed sale on the SAME
buy-rule evidence the morning path uses (owner 14 Oct 2026): carried insider
findings and R2-R6 from the manager's deterministic prompt assembly, built by
the one manager-input builder (`build_pm_decide_inputs`). No model is called.
A failed assembly holds the sale and records why."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models.positions import PositionAction, PositionReasoningChain, PositionReview
from src.pipeline_stages import RunContext
from src.models import Position
from src.sessions.position_review_session import PositionReviewSession
from tests.pipeline_factory import build_pipeline

INSIDER = SimpleNamespace(symbol="ACME", note="carried Form 4")


def _v(seat, symbol, direction):
    return SimpleNamespace(
        seat=seat, symbol=symbol, direction=direction, invalidation="closes below the level", evidence=[object()]
    )


def _pipeline(assemble):
    p = build_pipeline(db=MagicMock())
    p.db.get_latest_insights.return_value = None
    p._sweeper = MagicMock(return_value=None)
    for name in ("_compute_recent_performance", "_build_position_history"):
        setattr(p, name, MagicMock(return_value={}))
    for name in (
        "_build_weekly_narrative",
        "_build_macro_trajectory",
        "_build_active_state_changes",
        "_build_projected_portfolio",
        "_build_calibration_note",
        "_build_macro_tech_alignment",
        "_build_recent_loss_pits",
        "_build_blocked_proposals",
    ):
        setattr(p, name, MagicMock(return_value=""))
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = ["ACME"]
    p.config.execution = SimpleNamespace(rotation_enabled=False, rotation_ranked_margin_enabled=False)
    p.portfolio_constructor = MagicMock()
    p.portfolio_constructor.entry_stop_preview.return_value = None
    p.portfolio_constructor.real_reward_risk_preview.return_value = None
    p.portfolio_constructor.last_refusals = {}
    p._carry_forward_insider = MagicMock(return_value=SimpleNamespace(payload=[INSIDER], status="carried"))
    pm = MagicMock()
    pm.last_blocked = None
    pm.last_rotation_precheck = None
    pm.build_user_message.side_effect = lambda **kw: assemble(pm, kw)
    p.portfolio_manager = pm
    return p


def _ctx():
    ctx = RunContext.start("midday")
    ctx.positions = [
        Position(
            symbol="ACME",
            qty=10.0,
            avg_entry=100.0,
            current_price=95.0,
            market_value=950.0,
            unrealized_pnl=-50.0,
            sector="Technology",
        )
    ]
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    return ctx


def _review():
    chain = PositionReasoningChain(**{f: "x" for f in PositionReasoningChain.model_fields})
    return PositionReview(
        reasoning_chain=chain,
        overall_assessment="test",
        risk_level="low",
        actions=[PositionAction(action="SELL", symbol="ACME", reason="losing, no chart level")],
    )


class _DB:
    def __init__(self):
        self.evaluations = []

    def get_trades(self, symbol=None, limit=50, today_only=False):
        return []

    def record_intraday_evaluation(self, **kw):
        self.evaluations.append(kw)


def _gate(pipeline, rating, *, wired=True):
    """One reviewer SELL of a long ACME losing with no chart level under its stop."""
    reads = [SimpleNamespace(symbol="ACME", rating=rating)]
    fake = SimpleNamespace(
        _db=_DB(),
        _pipeline=pipeline,
        _todays_technical_reads=lambda positions: reads,
        _structural_protection_for_holding=lambda **kw: SimpleNamespace(
            protected=False, basis="no_chart_level", detail="losing, no level"
        ),
        _build_position_history=lambda positions: {"ACME": {"entry_price": 100.0, "stop_loss": 90.0}},
    )
    ctx = _ctx()
    kwargs = dict(news_intel=None, news_ok=True, macro_analysis=None, earnings_analyses=[])
    if wired:
        kwargs["ctx"] = ctx
    seats = [_v(s, "ACME", "bullish") for s in ("technical", "news", "earnings", "smart_money", "macro")]
    with patch.object(PortfolioManagerAgent, "_collect_seat_verdicts", staticmethod(lambda **kw: seats)):
        return PositionReviewSession._gate_reviewer_sales(fake, _review(), ctx.positions, run_id="r1", **kwargs)


def _assembled(blocked):
    def assemble(pm, kw):
        pm.seen = kw
        pm.last_blocked = dict(blocked)
        return "prompt"

    return assemble


def test_a_no_level_loser_that_fails_r2_on_the_reviewer_path_is_sold():
    pipeline = _pipeline(_assembled({}))
    gated = _gate(pipeline, "sell")
    assert gated.actions[0].action == "SELL"
    pm = pipeline.portfolio_manager
    assert pm.build_user_message.call_count == 1 and pm.decide.call_count == 0  # no model call
    assert pm.seen["smart_money_findings"] == [INSIDER]
    assert "ACME" in pm.seen["allowed_buy_symbols"]


def test_a_no_level_loser_that_passes_the_assembled_bar_is_held_on_passes_bar():
    gated = _gate(_pipeline(_assembled({})), "buy")
    assert gated.actions[0].action == "HOLD"
    assert "still passes the keep rule" in gated.actions[0].reason


def test_without_the_wiring_r2_is_unread_and_the_failing_loser_is_held():
    gated = _gate(_pipeline(_assembled({})), "sell", wired=False)
    assert gated.actions[0].action == "HOLD"


def _raise(pm, kw):
    raise RuntimeError("assembly broke")


def _no_record(pm, kw):
    return "prompt"  # leaves last_blocked None: never read as a pass


@pytest.mark.parametrize("assemble", [_raise, _no_record])
def test_a_failed_assembly_holds_the_sale_and_records_why(assemble):
    with patch("src.sentinel.guarded_site.record_site") as record:
        gated = _gate(_pipeline(assemble), "sell")
    assert gated.actions[0].action == "HOLD"
    assert "R2-R6 not evaluated" in gated.actions[0].reason
    sites = [(c.args[1], c.kwargs.get("context")) for c in record.call_args_list]
    assert ("same_rules_gate.reviewer_entry_rules", {"symbols": ["ACME"]}) in sites
