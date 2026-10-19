"""Owner-message number filter: one-decimal dollar figures are real (19 Oct 2026)."""

import pytest

from src.notifier.base import _find_malformed_numeric_tokens, _redact_malformed_numbers


@pytest.mark.parametrize(
    "text",
    [
        "ZZZ revenue was $450.0 million",
        "ZZZ closed at $341.9",
        "ZZZ lost -$10.9 today",
        "ZZZ position worth $1,234.5",
    ],
)
def test_one_decimal_dollar_figures_pass_unchanged(text):
    assert _find_malformed_numeric_tokens(text) == []
    assert _redact_malformed_numbers(text) == text


@pytest.mark.parametrize("text", ["ZZZ at $1,23,4.00", "ZZZ ratio 1.2.3"])
def test_genuinely_malformed_tokens_still_removed(text):
    assert _find_malformed_numeric_tokens(text)
    assert "[number garbled" in _redact_malformed_numbers(text)
