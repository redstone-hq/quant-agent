"""Item 99(b): the technical seat's prompt must name every data block it is
actually sent, and must claim none it is not.

The enforcement is exact-match only: a label the renderer emits, or a label the
message builder writes into its f-string, either appears in the registry below
with a phrase that is present in the prompt, or this fails. There is no
similarity score and no threshold to tune — the desk rejected a blanket scan of
prompt prose on this item, and this is the opposite of one: it keys off the
strings the code itself produces.
"""

from __future__ import annotations

from dataclasses import replace

import inspect
from pathlib import Path

from src.data.context import Gap, MarketContext, format_context_block

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = REPO_ROOT / "config" / "prompts" / "tech_analyst.md"


def _prompt_text() -> str:
    return PROMPT.read_text()


# Every line `format_context_block` can emit, keyed by the literal label the
# model sees, mapped to the words the prompt must use for it. Add a line to the
# renderer and this test fails until the prompt is told about it.
CONTEXT_LABELS: dict[str, str] = {
    "Returns:": "returns (1w/1m/3m/6m/12m)",
    "Relative strength vs": "relative strength vs the index ETF",
    "52w range:": "52-week range position",
    "Volatility: ATR": "ATR percentile",
    "MA change since prior session": "MA change since prior session",
    "CONSOLIDATING:": "CONSOLIDATING",
    "Not consolidating:": "CONSOLIDATING",
    "Liquidity:": "average daily dollar volume",
    "Up/down volume (20d):": "up/down volume ratio",
    "Unfilled gap": "unfilled gaps",
    "Next earnings in": "days to earnings",
    "⚠️ Next earnings in": "days to earnings",
}

# Labels the per-symbol section of the user message writes directly.
BUILDER_LABELS: dict[str, str] = {
    "Prior rating (context):": "**Prior rating (context)**",
    "Valuation:": "**Valuation**",
    "Price (last": "**OHLCV**",
    "Indicators:": "**Pre-computed indicators**",
    "Last completed close:": "**Current price**",
}


def _fully_populated_context() -> MarketContext:
    """Every optional field present, so every renderable line is emitted."""
    return MarketContext(
        last_close=100.0,
        return_1w=1.0,
        return_1m=2.0,
        return_3m=3.0,
        return_6m=4.0,
        return_12m=5.0,
        rel_strength_1m=1.5,
        rel_strength_3m=2.5,
        benchmark_symbol="SPY",
        high_52w=120.0,
        low_52w=80.0,
        pct_from_52w_high=-16.0,
        pct_from_52w_low=25.0,
        range_position_pct=50.0,
        atr_pct=2.0,
        atr_percentile_1y=60.0,
        volatility_state="normal",
        ma20_change_atr=0.5,
        ma50_change_atr=0.4,
        ma200_change_atr=0.3,
        is_consolidating=True,
        consolidation_high=105.0,
        consolidation_low=95.0,
        consolidation_range_pct=10.0,
        consolidation_range_atr=2.0,
        sessions_in_range=12,
        avg_dollar_volume_20d=5_000_000.0,
        up_down_volume_ratio=1.4,
        unfilled_gaps=[
            Gap(date="2026-09-01", from_price=90.0, to_price=95.0, direction="up", sessions_ago=8, size_atr=1.5),
        ],
    )


def _emitted_labels() -> set[str]:
    """Labels the renderer actually produces, both consolidation branches."""
    ctx = _fully_populated_context()
    text = format_context_block(ctx, days_to_earnings=7)
    quiet = replace(_fully_populated_context(), is_consolidating=False)
    text += "\n" + format_context_block(quiet, days_to_earnings=None)
    return {line.strip() for line in text.splitlines() if line.strip()}


def test_every_context_line_the_code_emits_is_named_in_the_prompt():
    prompt = _prompt_text()
    for line in _emitted_labels():
        if line.startswith("Market context"):
            continue
        matches = [lab for lab in CONTEXT_LABELS if line.startswith(lab)]
        assert matches, (
            f"format_context_block emits a line the prompt contract does not "
            f"know about: {line!r}. Register it in CONTEXT_LABELS and name it "
            f"in the Input section of {PROMPT.name}."
        )
        phrase = CONTEXT_LABELS[matches[0]]
        assert phrase in prompt, (
            f"the market-context line {matches[0]!r} is sent to the technical "
            f"seat but {PROMPT.name} does not say so (expected the phrase "
            f"{phrase!r} in its Input section)."
        )


def test_every_per_symbol_label_the_builder_writes_is_named_in_the_prompt():
    from src.agents.tech_analyst import TechAnalystAgent

    source = inspect.getsource(TechAnalystAgent.build_user_message)
    prompt = _prompt_text()
    for label, phrase in BUILDER_LABELS.items():
        assert label in source, (
            f"{label!r} is no longer written by build_user_message; the prompt "
            f"claim {phrase!r} has become untrue — remove it or re-point this row."
        )
        assert phrase in prompt, (
            f"{label!r} is sent to the technical seat but {PROMPT.name} does not name it (expected {phrase!r})."
        )


def test_the_input_section_claims_nothing_the_seat_is_not_sent():
    """Each Input bullet is backed by a string the code demonstrably produces."""
    from src.agents import tech_analyst as ta

    builder = inspect.getsource(ta.TechAnalystAgent.build_user_message)
    context_src = inspect.getsource(format_context_block)
    levels_src = (REPO_ROOT / "src" / "data" / "levels.py").read_text()
    haystack = builder + context_src + levels_src + inspect.getsource(ta)
    claims = {
        "**OHLCV**": "COMPLETED daily bars",
        "**Pre-computed indicators**": "Indicators: MA20=",
        "**Current price**": "Last completed close:",
        "**Market context**": "Market context (computed, not estimated):",
        "**Structural levels**": "format_levels_block",
        "**Current session (TODAY, INCOMPLETE)**": "CURRENT SESSION (TODAY, INCOMPLETE",
        "**Macro context**": "## Macro Context",
        "**Valuation**": "Valuation: trailing PE",
        "**Prior rating (context)**": "Prior rating (context):",
    }
    prompt = _prompt_text()
    for claim, evidence in claims.items():
        assert claim in prompt, f"{PROMPT.name} no longer lists {claim}"
        assert evidence in haystack, (
            f"{PROMPT.name} tells the technical seat it receives {claim}, but "
            f"nothing in the code produces {evidence!r} any more — an untrue "
            f"prompt sentence, not a wording problem."
        )


def test_the_input_section_exists_where_this_test_expects_it():
    prompt = _prompt_text()
    assert "For each symbol you receive:" in prompt
