"""Rotation and pruning lines show ranking blocks in plain words, never R-codes."""

from __future__ import annotations

from src.agents.portfolio_manager.candidate_ranking import HELD_NEUTRAL_R2
from src.rotation_parts.reporting import ROTATION_HOLDING_BELOW_BAR
from src.rotation_parts.reporting_lines import owner_precheck_lines, pruning_pass_lines

PLAIN_R2 = "the chart reading was neutral — no buy or sell signal"


def _below_bar(reasons: str) -> str:
    record = {
        "outcome": ROTATION_HOLDING_BELOW_BAR,
        "headroom_pct": 1.0,
        "ceiling_pct": 8.0,
        "floor_pct": 1.0,
        "binding": "risk_budget",
        "held_symbol": "ZZZA",
        "held_reasons": reasons,
    }
    return " ".join(owner_precheck_lines(record))


def _pruning(reasons: str) -> str:
    record = {
        "outcome": ROTATION_HOLDING_BELOW_BAR,
        "held_examined": "ZZZA,ZZZB",
        "held_examined_count": 2,
        "held_below_entry_bar": "ZZZA",
        "held_reasons": reasons,
        "held_symbol": "ZZZA",
        "tier": "ineligible_hold",
    }
    return " ".join(pruning_pass_lines(record))


def _clean(text: str) -> None:
    assert PLAIN_R2 in text
    assert "R2" not in text
    assert "R3-R6" not in text
    assert ";  " not in text


def test_rotation_line_for_held_r2_uses_plain_words():
    _clean(_below_bar(HELD_NEUTRAL_R2))


def test_pruning_line_for_held_r2_uses_plain_words():
    _clean(_pruning(HELD_NEUTRAL_R2))


def test_other_ranking_codes_are_worded_too():
    text = _below_bar("R3 not on the buy list,R7 own-bar reason")
    assert "R3" not in text and "R7" not in text
    assert "allowed to buy today" in text
