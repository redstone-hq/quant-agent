"""Owner ruling q23 (2026-10-21): the evening AI reports to the owner, it does
not steer the buying AI. Nothing the evening AI writes may reach the portfolio
manager's input — not the insights block, not the evening prose inside the
7-day narrative, not via the decision stage's call."""

import json
from unittest.mock import MagicMock, patch

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.pipeline_context import RunContext
from src.pipeline_stages import DecisionStage
from tests.pipeline_factory import build_pipeline

# Unique strings so a hit can only mean evening text leaked through.
EVENING_ROW = {
    "date": "2026-10-20",
    "timestamp": "2026-10-20T20:05:00",
    "tomorrow_outlook": "EVE_OUTLOOK_MARKER defensive tomorrow",
    "lessons": "EVE_LESSONS_MARKER do not chase",
    "risk_rating": "elevated",
    "suggested_actions": json.dumps(["EVE_ACTION_MARKER buy more semis"]),
    "tomorrow_bias": "bearish",
    "tomorrow_conviction": "high",
    "tomorrow_key_risks": json.dumps(["EVE_RISK_MARKER FOMC"]),
    "sell_decisions_assessment": "EVE_SELLGRADE_MARKER premature",
    "thesis_updates_json": json.dumps(["EVE_THESIS_MARKER"]),
    "selection_rules_json": json.dumps(["EVE_SELECTION_MARKER"]),
    "discipline_notes_json": json.dumps(["EVE_DISCIPLINE_MARKER"]),
}
MARKERS = [
    v
    for v in (
        "EVE_OUTLOOK_MARKER",
        "EVE_LESSONS_MARKER",
        "EVE_ACTION_MARKER",
        "EVE_RISK_MARKER",
        "EVE_SELLGRADE_MARKER",
        "EVE_THESIS_MARKER",
        "EVE_SELECTION_MARKER",
        "EVE_DISCIPLINE_MARKER",
    )
]


def _assert_no_evening_text(text: str) -> None:
    for m in MARKERS:
        assert m not in text, f"evening text {m} reached the PM input"
    assert "Prior Evening Insights" not in text
    assert "bias=bearish" not in text
    assert "SELL discipline grade" not in text


def test_pm_user_message_drops_evening_insights_even_if_passed():
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            yesterday_insights=dict(EVENING_ROW),
        )
    _assert_no_evening_text(msg)


def test_pm_narrative_keeps_desk_returns_and_drops_evening_prose():
    db = MagicMock()
    db.get_recent_insights.return_value = [dict(EVENING_ROW)]
    db.get_daily_pnl.return_value = [{"date": "2026-10-20", "daily_return_pct": 0.42}]
    pipeline = build_pipeline(db=db, broker=MagicMock())

    pm_narrative = pipeline._build_weekly_narrative(include_evening_text=False)
    _assert_no_evening_text(pm_narrative)
    assert "elevated" not in pm_narrative  # risk_rating is evening-authored
    assert "2026-10-20: +0.42%" in pm_narrative  # the desk's own measured return stays

    # The evening and reviewer reads are unchanged.
    assert "EVE_OUTLOOK_MARKER" in pipeline._build_weekly_narrative()

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            weekly_narrative=pm_narrative,
        )
    _assert_no_evening_text(msg)
    assert "+0.42%" in msg


def test_decision_stage_sends_pm_no_evening_insights():
    p = build_pipeline(db=MagicMock(), portfolio_manager=MagicMock())
    p.db.get_latest_insights.return_value = dict(EVENING_ROW)
    p._sweeper = MagicMock(return_value=None)
    p._compute_recent_performance = MagicMock(return_value={})
    p._build_position_history = MagicMock(return_value={})
    p._build_weekly_narrative = MagicMock(return_value="")
    for name in (
        "_build_macro_trajectory",
        "_build_active_state_changes",
        "_build_projected_portfolio",
        "_build_calibration_note",
        "_build_macro_tech_alignment",
        "_build_recent_loss_pits",
    ):
        setattr(p, name, MagicMock(return_value=""))
    p._build_pm_facts = MagicMock(return_value=MagicMock())
    p._ensure_correlation_matrix = MagicMock(return_value={})
    p.config = MagicMock()
    p.config.risk.allow_margin = False
    p.config.trading.universe = []
    p._last_symbol_sectors = {}
    p.portfolio_manager.decide.return_value = (
        None,
        MagicMock(
            user_message="m",
            raw_text="{}",
            tokens_used=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            model="test-model",
        ),
    )
    ctx = RunContext.start("intra_check")
    ctx.positions = []
    ctx.analyses = []
    ctx.macro_analysis = None
    ctx.total_value = 100_000.0
    ctx.last_equity = 100_000.0
    ctx.cash = 50_000.0
    ctx.deployable_cash = 50_000.0
    ctx.admitted_symbols = set()

    DecisionStage(pipeline=p).run(ctx)

    kwargs = p.portfolio_manager.decide.call_args.kwargs
    assert "yesterday_insights" not in kwargs
    _assert_no_evening_text(json.dumps(kwargs, default=str))
    p._build_weekly_narrative.assert_called_once_with(include_evening_text=False)
