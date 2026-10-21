"""The portfolio manager is no longer nudged with recurring missed themes.

Owner ruling (2026-10-21): misses are an owner report (evening review,
dashboard, digest), not an instruction to the buying seat. The PM prompt
must carry neither the section heading nor its chase-it-today steering.
"""

from unittest.mock import patch


def _pm_message(**extra) -> str:
    from src.agents.portfolio_manager import PortfolioManagerAgent

    with patch("anthropic.Anthropic"):
        agent = PortfolioManagerAgent(api_key="k", model="claude-opus-4-6")
    return agent.build_user_message(
        analyses=[],
        positions=[],
        macro_analysis=None,
        cash_balance=10_000,
        total_value=100_000,
        **extra,
    )


def test_pm_prompt_has_no_missed_themes_section_by_default():
    msg = _pm_message()
    assert "Recurring Missed Themes" not in msg
    assert "runs further away" not in msg


def test_pm_prompt_ignores_legacy_missed_lessons_kwarg():
    """Even a caller still passing the old slot gets no nudge rendered."""
    msg = _pm_message(recent_missed_lessons="- nuclear/power: 3 days (symbols: VST, OKLO)")
    assert "Recurring Missed Themes" not in msg
    assert "runs further away" not in msg
    assert "nuclear/power" not in msg
