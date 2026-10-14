"""Telegram: every internal code the owner sees goes through ONE code-to-plain-words table.

The owner reads on a phone and holds no labels in his head. The decision line
used to print `scale_all_buys 80%` and the close-review block printed
`Profit factor` and `p-value`. Display only: no AI prompt is touched, so what
the seats are told is unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from src.trader_feed import decision, trade_block

RAW_SCALE = "scale_all_buys"


def _risk_snapshot(**risk) -> dict:
    base = {"approved": True, "reason_category": "other", "reasoning": "Book looks fine.", "rejected_symbols": []}
    base.update(risk)
    return {"risk": base, "risk_mods": []}


def _risk_text(**risk) -> str:
    lines: list[str] = []
    decision._append_risk(lines, _risk_snapshot(**risk))
    return "\n".join(lines)


# (a) the decision line ------------------------------------------------------


def test_decision_line_says_the_cut_in_plain_words():
    text = _risk_text(scale_all_buys=0.8)
    assert RAW_SCALE not in text
    from src.status_plain_extra import owner_words

    assert owner_words("risk_field", RAW_SCALE) in text
    assert "80% of plan" in text
    assert "advisory only, entries were NOT resized" in text


def test_ai_prose_naming_the_field_is_translated_by_the_display_layer():
    text = _risk_text(scale_all_buys=1.0, reasoning="Heavy tech tilt, so scale_all_buys 0.8 until it eases.")
    assert RAW_SCALE not in text
    from src.status_plain_extra import owner_words

    assert owner_words("risk_field", RAW_SCALE) in text
    assert "80% of plan" in text


def test_whole_session_message_carries_no_raw_field_name(monkeypatch):
    snap = decision._empty_snapshot()
    snap.update(_risk_snapshot(scale_all_buys=0.8, reasoning="Trim: scale_all_buys 0.8, rejected_symbols none."))
    monkeypatch.setattr(decision, "_read_run", lambda run_id: snap)
    monkeypatch.setattr(decision, "_with_guard_verdicts", lambda s, r: s)
    msg = decision._format_decision_session("morning", {"status": "executed", "run_id": "r1"}, 1.0)
    assert RAW_SCALE not in msg
    assert "rejected_symbols" not in msg
    assert "80% of plan" in msg


# (b) the close-review trade block -------------------------------------------


def _summary(**over) -> dict:
    base = {
        "n": 2,
        "wins": 1,
        "losses": 1,
        "win_rate_pct": 50.0,
        "total_usd": 150.0,
        "average_usd": 75.0,
        "best": {"symbol": "AAA", "usd": 200.0},
        "worst": {"symbol": "BBB", "usd": -50.0},
        "not_counted": [],
        "profit_factor": 4.0,
        "quality_n": 2,
        "p_value": 0.0123,
    }
    base.update(over)
    return base


@pytest.mark.parametrize("profit_factor", [4.0, "no losses", "n/a", None])
def test_trade_block_has_no_raw_statistics_labels(profit_factor):
    text = "\n".join(trade_block.trade_block_lines(_summary(profit_factor=profit_factor)))
    assert "profit factor" not in text.lower()
    assert "p-value" not in text.lower()
    assert "no losses" not in text  # the stored token, not words
    from src.status_plain_extra import owner_words

    assert owner_words("trade_review", "profit_factor") in text
    assert owner_words("trade_review", "p_value") in text


def test_trade_block_unknown_luck_check_still_says_unknown():
    text = "\n".join(trade_block.trade_block_lines(_summary(p_value=None, quality_n=0)))
    assert "p-value" not in text.lower()
    assert "unknown" in text.split("\n")[-1]


# (c) every code these two renderers can show has a table entry ---------------


def _trade_review_keys_read_by_the_renderer() -> set[str]:
    """Every string key the block subscripts on `summary`, read from its own source."""
    tree = ast.parse(Path(trade_block.__file__).read_text())
    keys = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "summary"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


def _profit_factor_tokens() -> set[str]:
    """The strings `trade_review.profit_factor` returns instead of a number, from its own source."""
    import inspect

    from src.storage.analytics import trade_review

    tree = ast.parse(inspect.getsource(trade_review.profit_factor))
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }


def _shown_codes() -> set[tuple[str, str]]:
    from src.models import RiskReasonCategory, RiskVerdict
    from src.models.portfolio import RiskModification
    from src.models.risk_verdicts import ExitRiskReasoningChain, ExitRiskVerdict, RiskReasoningChain

    codes = {("risk_category", value) for value in get_args(RiskReasonCategory)}
    # The risk seat's own output field names (snake_case ones): the decision line
    # prints its prose, and prose quotes the fields it fills in.
    for model in (RiskVerdict, ExitRiskVerdict, RiskReasoningChain, ExitRiskReasoningChain, RiskModification):
        codes |= {("risk_field", name) for name in model.model_fields if "_" in name}
    codes |= {("trade_review", key) for key in _trade_review_keys_read_by_the_renderer()}
    codes |= {("profit_factor_value", token) for token in _profit_factor_tokens()}
    return codes


def test_the_enumeration_finds_codes_in_every_group():
    kinds = {kind for kind, _ in _shown_codes()}
    assert kinds == {"risk_category", "risk_field", "trade_review", "profit_factor_value"}
    assert _profit_factor_tokens() == {"n/a", "no losses"}
    assert {"profit_factor", "p_value"} <= _trade_review_keys_read_by_the_renderer()


def test_every_shown_code_has_a_table_entry():
    from src.status_plain_extra import OWNER_WORDS

    missing = sorted(f"{kind}:{code}" for kind, code in _shown_codes() if f"{kind}:{code}" not in OWNER_WORDS)
    assert not missing, f"codes the owner can see with no plain wording: {missing}"


def test_no_entry_repeats_its_own_code_back_to_the_owner():
    from src.status_plain_extra import OWNER_WORDS

    for key, words in OWNER_WORDS.items():
        code = key.split(":", 1)[1]
        if "_" not in code:
            continue  # plain single words are their own wording
        assert code not in words, key
        assert words.strip(), key


def test_prose_translation_covers_every_risk_field_name():
    from src.status_plain_extra import plain_prose

    for kind, code in sorted(_shown_codes()):
        if kind == "risk_field":
            assert code not in plain_prose(f"the {code} was low"), code
