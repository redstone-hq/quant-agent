"""The below-entry-bar sale is its own step and does not wait on the PM.

Owner rulings: 2026-10-01 (a holding that fails the desk's own fresh-entry
bar is SOLD, unconditionally, every run) and 2026-10-12 (that sale needs no
paid portfolio-manager call). Before this change the sale was proposed only
inside a run whose PM call returned a decision: a run with no PM answer
returned early and the below-bar holding was kept.

Hermetic: the PM is a mock that returns NO decision. Its deterministic prompt
assembly (`build_user_message`) is the one place the below-bar set is
computed, and is stubbed to leave the pre-check a real assembly would.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.models import Position
from src.pipeline_stages import DecisionStage, RunContext
from src.risk.exit_guard import StructuralProtectionCheck
from src.rotation import RotationOpportunity, RotationPrecheck
from tests.pipeline_factory import build_pipeline

REASONS = ("R7 conviction bar: technical seat opposed",)


def _below_bar_precheck() -> RotationPrecheck:
    return RotationPrecheck(
        opportunity=RotationOpportunity(
            new_symbol=None,
            new_score=None,
            held_symbol="OLD",
            held_score=None,
            tier="ineligible_hold",
            reasons=REASONS,
            ineligible_candidates=(("OLD", REASONS),),
        ),
        headroom_pct=5.0,
        ceiling_pct=25.0,
        floor_pct=0.5,
        held_below_entry_bar=("OLD",),
        held_examined=("OLD", "KEEP"),
    )


def _nothing_below_bar_precheck() -> RotationPrecheck:
    return RotationPrecheck(
        opportunity=None,
        headroom_pct=5.0,
        ceiling_pct=25.0,
        floor_pct=0.5,
        held_examined=("KEEP",),
    )


def _pos(symbol: str) -> Position:
    return Position(
        symbol=symbol,
        qty=10.0,
        avg_entry=100.0,
        current_price=95.0,
        market_value=950.0,
        unrealized_pnl=-50.0,
        sector="Technology",
    )


def _pipeline(precheck: RotationPrecheck):
    p = build_pipeline(db=MagicMock())
    p.db.get_latest_insights.return_value = None
    p.db.get_trades.return_value = []
    p.db.get_pending_protection_restores.return_value = []
    p.db.get_pending_repegs.return_value = []
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
    ):
        setattr(p, name, MagicMock(return_value=""))
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p._require_paid_analysis = MagicMock(return_value=None)
    p._record_heal = MagicMock(return_value=None)
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p.config.execution = SimpleNamespace(rotation_enabled=True, rotation_ranked_margin_enabled=False)
    p._last_symbol_sectors = {}
    p.portfolio_constructor = MagicMock()
    p.portfolio_constructor.real_reward_risk_preview.return_value = {}
    p.portfolio_constructor.last_refusals = {}
    p.broker = MagicMock()
    p._structural_protection_for_holding = MagicMock(
        return_value=StructuralProtectionCheck(protected=True, basis="structural_level_intact", detail="intact")
    )
    pm = MagicMock()
    pm.last_rotation_precheck = None
    # The PM gives NO decision this run. Its prompt assembly is deterministic
    # and is the one place the below-bar set is computed.
    pm.decide.return_value = (
        None,
        MagicMock(
            user_message="",
            raw_text="",
            tokens_used=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            model="none",
            semantic_status="pm_not_answered",
            semantic_error="no PM answer this run",
        ),
    )

    def _assemble(**_kwargs):
        pm.last_rotation_precheck = precheck
        return "prompt"

    pm.build_user_message.side_effect = _assemble
    p.portfolio_manager = pm
    return p


def _run(pipeline):
    ctx = RunContext.start("intra_check")
    ctx.positions = [_pos("OLD"), _pos("KEEP")]
    ctx.analyses = []
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()
    with patch("src.pipeline_stages._persist_evidence", MagicMock()):
        try:
            DecisionStage(pipeline=pipeline).run(ctx)
        except Exception:  # noqa: BLE001
            # Construction onward is not stood up here; the sale decision
            # has already been made by then.
            pass
    return ctx


def _constructed_symbols(pipeline) -> list[str]:
    calls = pipeline.portfolio_constructor.construct_orders.call_args_list
    return [t.symbol for c in calls for t in c.kwargs.get("targets", [])]


def test_a_below_bar_holding_is_sold_with_no_pm_answer():
    pipeline = _pipeline(_below_bar_precheck())
    ctx = _run(pipeline)
    assert ctx.rotation is not None, "no PM answer must not stop the below-entry-bar sale"
    assert ctx.rotation["held_symbol"] == "OLD"
    assert ctx.rotation["tier"] == "ineligible_hold"
    assert _constructed_symbols(pipeline) == ["OLD"], "the close goes through the ordinary constructor"
    # The structurally protected holding is still sold on the categorical
    # tier, exactly as with a PM answer (protection measured, never a veto).
    assert pipeline._structural_protection_for_holding.called
    # Only the original PM call: the step itself pays for nothing.
    assert pipeline.portfolio_manager.decide.call_count == 1


def test_a_holding_above_the_bar_is_not_sold():
    pipeline = _pipeline(_nothing_below_bar_precheck())
    ctx = _run(pipeline)
    assert ctx.rotation is None
    assert ctx.portfolio_decision is None, "no sale and no PM answer ends as a no-decision run"
    assert pipeline.portfolio_constructor.construct_orders.call_count == 0


def test_a_per_holding_guard_still_refuses_the_sale_with_no_pm_answer():
    pipeline = _pipeline(_below_bar_precheck())
    pipeline.db.get_trades.return_value = [{"action": "BUY", "fill_status": "filled"}]
    ctx = _run(pipeline)
    assert ctx.rotation is None, "bought today: the existing guard still refuses"
    assert ctx.portfolio_decision is None
    assert pipeline.portfolio_constructor.construct_orders.call_count == 0


def test_switched_off_rotation_keeps_todays_no_decision_behaviour():
    pipeline = _pipeline(_below_bar_precheck())
    pipeline.config.execution = SimpleNamespace(rotation_enabled=False)
    ctx = _run(pipeline)
    assert ctx.rotation is None
    assert ctx.portfolio_decision is None
    assert pipeline.portfolio_manager.build_user_message.call_count == 0


# ---------------------------------------------------------------------------
# Unknown is not fail: a seat outage must not fake a below-bar reading
# ---------------------------------------------------------------------------

R5_ONLY = ("R5 net evidence +0 if long — no rung",)


def _r5_only_precheck() -> RotationPrecheck:
    return RotationPrecheck(
        opportunity=RotationOpportunity(
            new_symbol=None,
            new_score=None,
            held_symbol="OLD",
            held_score=None,
            tier="ineligible_hold",
            reasons=R5_ONLY,
            ineligible_candidates=(("OLD", R5_ONLY),),
        ),
        headroom_pct=5.0,
        ceiling_pct=25.0,
        floor_pct=0.5,
        held_below_entry_bar=("OLD",),
        held_examined=("OLD", "KEEP"),
    )


def _run_recorded(pipeline, data_status=None, pm_skip_reason=None):
    ctx = RunContext.start("intra_check")
    ctx.data_status = dict(data_status or {})
    if pm_skip_reason:
        ctx.pm_skip_reason = pm_skip_reason
    ctx.positions = [_pos("OLD"), _pos("KEEP")]
    ctx.analyses = []
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()
    events: list[tuple] = []

    def _rec(_pipeline, _ctx, symbol, stage, outcome, reason, **details):
        events.append((symbol, stage, outcome, reason, details))

    with (
        patch("src.pipeline_stages._persist_evidence", MagicMock()),
        patch("src.pipeline_stages._record_pipeline_event", _rec),
    ):
        try:
            DecisionStage(pipeline=pipeline).run(ctx)
        except Exception:  # noqa: BLE001
            pass
    return ctx, events


def test_a_failed_supporting_seat_does_not_sell_on_net_evidence_alone():
    pipeline = _pipeline(_r5_only_precheck())
    ctx, events = _run_recorded(pipeline, {"tech": "ok", "news": "failed", "macro": "ok"})
    assert ctx.rotation is None, "a net-evidence reading with a failed seat is unknown, not a fail"
    skips = [e for e in events if e[1] == "rotation" and e[2] == "skipped"]
    assert [(s[0], s[3]) for s in skips] == [("OLD", "evidence_incomplete_seat_failed")]
    assert skips[0][4]["failed_seats"] == "news"
    checked = [e for e in events if e[1] == "rotation" and e[2] == "holdings_checked"]
    assert checked and "OLD=not_judged_seat_failed:news" in checked[0][4]["held_outcomes"]


def _run_with_registry(pipeline, data_status, registry):
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch.object(PortfolioManagerAgent, "build_evidence_registry", MagicMock(return_value=registry)):
        return _run_recorded(pipeline, data_status)


def test_a_partial_news_seat_with_the_holdings_own_news_read_still_sells():
    """Desk archetype (2026-10-19): a holding whose own news seat turned
    against it is no longer a mechanical sale (R5 net evidence is retired and
    the bearish news is an objection for the position reviewer), so the
    half-hour check does not sell it; the seat outage is not the reason."""
    pipeline = _pipeline(_r5_only_precheck())
    ctx, events = _run_with_registry(
        pipeline,
        {"tech": "ok", "news": "partial", "macro": "low_confidence", "smart_money": "degraded"},
        {"OLD": {"technical": "neutral", "news": "bearish", "macro": "neutral", "smart_money": "neutral"}},
    )
    assert ctx.rotation is None
    assert not [e for e in events if e[1] == "rotation" and e[3] == "evidence_incomplete_seat_failed"]


def test_a_partial_news_seat_missing_the_holdings_own_news_is_not_judged():
    pipeline = _pipeline(_r5_only_precheck())
    ctx, events = _run_with_registry(
        pipeline,
        {"tech": "ok", "news": "partial"},
        {"OLD": {"technical": "neutral"}},
    )
    assert ctx.rotation is None
    skips = [e for e in events if e[1] == "rotation" and e[3] == "evidence_incomplete_seat_failed"]
    assert skips and skips[0][4]["failed_seats"] == "news"


def test_net_evidence_below_bar_still_sells_when_every_seat_read():
    """Desk archetype (2026-10-19): R5 is retired and never sells, even with
    every seat read — a quiet news seat never sells a holding."""
    pipeline = _pipeline(_r5_only_precheck())
    ctx, _ = _run_recorded(pipeline, {"tech": "ok", "news": "ok", "macro": "ok"})
    assert ctx.rotation is None


def test_an_active_opposition_still_sells_through_a_seat_failure():
    pipeline = _pipeline(_below_bar_precheck())  # R7: a seat actively opposed
    ctx, _ = _run_recorded(pipeline, {"news": "failed"})
    assert ctx.rotation is not None and ctx.rotation["held_symbol"] == "OLD"


# ---------------------------------------------------------------------------
# Deliberate PM skip: no PM call, recorded as a skip, the sale still runs
# ---------------------------------------------------------------------------


def test_a_deliberate_pm_skip_sells_without_calling_the_pm_and_says_skipped():
    pipeline = _pipeline(_below_bar_precheck())
    ctx, events = _run_recorded(pipeline, pm_skip_reason="intraday_no_movers")
    assert pipeline.portfolio_manager.decide.call_count == 0
    assert ctx.rotation is not None and ctx.rotation["held_symbol"] == "OLD"
    pm_rows = [(e[2], e[3]) for e in events if e[1] == "portfolio_manager"]
    assert pm_rows == [("skipped", "intraday_no_movers")]
    checked = [e for e in events if e[1] == "rotation" and e[2] == "holdings_checked"]
    assert checked[0][3] == "skipped:intraday_no_movers"
    assert checked[0][4]["held_outcomes"] == "OLD=sale_proposed|KEEP=meets_bar"


# ---------------------------------------------------------------------------
# Intraday: a check with zero movers still re-reads holds and can sell
# ---------------------------------------------------------------------------


@patch("src.pipeline_intraday.compute_indicators")
def test_an_intraday_check_with_zero_movers_still_sells_a_below_bar_holding(mock_ci):
    from tests.test_intraday_scan import _held_position, _intraday_pipeline, _ta_result

    mock_ci.return_value = MagicMock()
    p = _intraday_pipeline(universe=["OLD"], atr={})
    p.broker.get_intraday_snapshots.return_value = {}  # no movers at all
    p.tech_analyst.analyze_batch.return_value = (
        {"OLD": _ta_result("OLD", rating="neutral")},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    seen = {}

    def _decide(ctx):
        seen["pm_skip_reason"] = getattr(ctx, "pm_skip_reason", None)
        ctx.portfolio_decision = SimpleNamespace(decisions=[SimpleNamespace(action="SELL", symbol="OLD")])

    p.decision_stage.run.side_effect = _decide
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "SELL", "symbol": "OLD"}]
    ctx = RunContext.start("intra_check")
    ctx.positions = [_held_position("OLD")]
    result = p._run_intraday_opportunity_scan(ctx)
    assert [s["symbol"] for s in p.tech_analyst.analyze_batch.call_args.args[0]] == ["OLD"]
    assert seen["pm_skip_reason"] == "intraday_no_movers"
    assert result["status"] == "intraday_executed"
    p.risk_stage.run.assert_called_once_with(ctx)
