"""Regression tests for the 2026-08-17/20 PM parse-annihilation incident.

Production forensics (trading-utility recovery): `AgentResult.parse_json`
scored the PM plan's inner `targets` array (5 pts/symbol) above the full
PortfolioDecision object, because `_EXPECTED_AGENT_KEY_WEIGHTS` still
anchored on the pre-constructor-refactor key `decisions` while PM emits
`targets`. Any plan with >= 8 targets therefore lost the candidate
selection to a fragment of itself, `PortfolioDecision(**list)` raised, and
the session collapsed to "no trades" — 10 of the 13 decision runs between
2026-08-14 and 2026-08-20 died this way, each retried at full research
cost on the next 30-min tick.

Fixtures are the VERBATIM recorded `full_response` payloads from the two
killed production runs (run-cbf2adbd 17 targets, run-5dc7a354 11 targets).
"""

from pathlib import Path

import pytest

from src.agents.base import AgentResult
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import PortfolioDecision

FIXTURES = Path(__file__).parent / "fixtures"


def _result(raw: str) -> AgentResult:
    return AgentResult(raw_text=raw, tokens_used=0, model="test")


def test_pm_decide_treats_non_dict_parse_as_failure(monkeypatch):
    """A list reaching decide() must return (None, result) — a loud parse
    failure the pipeline retries — never a silent 'no trades' hold."""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    fake = _result('[{"symbol": "XOM", "target_weight_pct": 5.0}]')
    monkeypatch.setattr(
        PortfolioManagerAgent,
        "run",
        lambda self, **kw: fake,
        raising=False,
    )
    decision, result = agent.decide(analyses=[], positions=[])
    assert decision is None
    assert result is fake


def test_pm_decide_treats_all_malformed_targets_as_failure(monkeypatch):
    raw = """{
      "reasoning_chain": {
        "macro_filter":"m", "news_check":"n", "earnings_check":"e",
        "signal_conflicts":"s", "sizing_logic":"z",
        "portfolio_balance":"b", "cash_target":"c"
      },
      "targets":[{"symbol":"AAPL","target_weight_pct":99,"thesis":"bad"}],
      "portfolio_view":"actionable target was attempted"
    }"""
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    fake = _result(raw)
    monkeypatch.setattr(
        PortfolioManagerAgent,
        "run",
        lambda self, **kw: fake,
        raising=False,
    )
    decision, result = agent.decide(analyses=[], positions=[])
    assert decision is None
    assert result is fake
