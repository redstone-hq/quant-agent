"""The fixed trading universe keeps SH and PSQ and drops the leveraged inverse
funds SDS and SQQQ (owner decision, Rex 19 Oct 2026, checklist q19).

Reason [measured, 2 years of daily prices]: worst built-in shortfall over
5-21 trading days was SDS 5.0% and SQQQ 13-17%, against SH 1.3% and PSQ 2.1%.
"""

from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"


@pytest.fixture
def universe(monkeypatch) -> set[str]:
    # load_config validates API keys from the environment, as in
    # tests/test_benchmark_budget.py; placeholders satisfy it.
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "FRED_API_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.setenv(key, "placeholder-for-test")
    from src.config import load_config

    return set(load_config(SETTINGS).trading.universe)


def test_universe_keeps_unleveraged_and_mild_inverse_funds(universe):
    assert "SH" in universe
    assert "PSQ" in universe


def test_universe_drops_leveraged_inverse_funds(universe):
    assert "SDS" not in universe
    assert "SQQQ" not in universe
