"""The planning AI no longer sees a risk-AI discipline block, and neither AI sees a
"recent decisions" diary. Entry reason and days held for a held name still show."""

from unittest.mock import patch

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.position_reviewer import PositionReviewerAgent
from src.pipeline_pm_facts import PMFacts
from src.models import Position
from src.prompt_facts.decisions import PromptDecisions
from tests.pipeline_factory import build_pipeline

_POS = Position(
    symbol="ZZZA", qty=10, avg_entry=10, current_price=11, market_value=110, unrealized_pnl=10, sector="Technology"
)
_HISTORY = {
    "ZZZA": {
        "entry_date": "2026-04-15",
        "entry_price": 10.0,
        "entry_reasoning": "ZZZ entry thesis marker",
        "days_held": 6,
        "tech_history": [],
    }
}


def test_pm_facts_render_has_no_rm_discipline():
    assert "RM Discipline" not in PMFacts().render()
    assert not hasattr(PMFacts(), "rm_verdicts_seen")


def test_diary_builders_are_gone():
    for name in ("_build_pm_recent_decisions", "_build_own_recent_decisions"):
        assert not hasattr(PromptDecisions, name)
        assert not hasattr(build_pipeline(), name)


def test_planning_input_has_no_decision_diary_but_keeps_entry_reason_and_days_held():
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            analyses=[],
            positions=[_POS],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            position_history=_HISTORY,
            facts=PMFacts(),
        )
    assert "Recent Decisions" not in msg
    assert "RM Discipline" not in msg
    assert "ZZZ entry thesis marker" in msg
    assert "held 6d" in msg


def test_review_input_has_no_decision_diary():
    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(api_key="test", model="claude-opus-4-6")
        msg = agent.build_user_message(
            positions=[_POS],
            analyses=[],
            macro_summary={},
            cash_balance=5000.0,
            total_value=10000.0,
            session_type="midday",
            own_recent_decisions="- 2026-04-16: ZZZA:TRIM",
        )
    assert "Recent Decisions" not in msg
    assert "ZZZA:TRIM" not in msg
