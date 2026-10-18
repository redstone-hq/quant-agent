"""The planning AI (PM) is no longer shown the risk AI's recent verdicts."""

from pathlib import Path
from unittest.mock import patch

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.prompt_facts.decisions import PromptDecisions
from tests.pipeline_factory import build_pipeline

_PROMPT = Path(__file__).resolve().parent.parent / "config" / "prompts" / "portfolio_manager.md"


def _message(**extra) -> str:
    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="test", model="claude-opus-4-6")
        return agent.build_user_message(
            analyses=[],
            positions=[],
            macro_analysis=None,
            cash_balance=5000.0,
            total_value=10000.0,
            **extra,
        )


def test_pm_message_has_no_risk_verdict_block():
    msg = _message(rm_recent_verdicts="- 2026-04-16: APPROVED [cat=oversized; scale_all_buys=0.50] ZZZA")
    assert "Risk Manager Verdicts" not in msg
    assert "self-calibrate" not in msg
    assert "cat=oversized" not in msg
    assert "## Your Recent Decisions" not in msg


def test_feed_builder_is_gone():
    assert not hasattr(PromptDecisions, "_build_rm_recent_verdicts")
    assert not hasattr(build_pipeline(), "_build_rm_recent_verdicts")


def test_pm_prompt_does_not_tell_it_to_resize_on_risk_history():
    text = _PROMPT.read_text()
    assert "RM history self-calibration" not in text
    assert "L4 Risk Manager Verdicts" not in text
