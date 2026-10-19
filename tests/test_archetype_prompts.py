"""Desk archetype step D: what the AI seats are told matches the archetype.

Authority: docs/OUTCOME.md, "The desk archetype: specialists, weighed, then
one head trader" (owner, 2026-10-19). These checks read the prompts exactly
as each seat receives them (placeholders rendered from the live settings)
and pin the sentences that carry the archetype, plus the absence of the
25-Sep wording. The retired-phrase registry (config/retired_mechanisms.yaml)
is the build-wide guard; this file pins the per-seat wording.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.prompt_limits import load_risk_config_from_settings, render_prompt_limits
from src.agents.risk_manager import SETTINGS_PATH

PROMPTS = Path(__file__).resolve().parent.parent / "config" / "prompts"

SPECIALISTS = ("tech_analyst", "news_analyst", "earnings_analyst", "macro_analyst", "smart_money_analyst")
ALL_SEATS = (*SPECIALISTS, "portfolio_manager", "position_reviewer", "risk_manager")


def _rendered(name: str) -> str:
    text = (PROMPTS / f"{name}.md").read_text()
    if name == "tech_analyst":
        from src.agents.tech_analyst import render_tech_placeholders

        text = render_tech_placeholders(text)
    return " ".join(render_prompt_limits(text, load_risk_config_from_settings(SETTINGS_PATH)).split())


#: How each specialist is told its view counts (item 3 of step D).
HOW_IT_COUNTS = (
    "Views are weighed, not counted as votes",
    "For a days-to-weeks hold the chart weighs most, and nothing is bought without it.",
    "a fresh catalyst is required for an entry only, and a quiet news seat never sells a holding",
    "Macro is the backdrop: it tilts size and direction, rarely blocks a name, and is not counted in the name ranking.",
    "The name ranking is the count of seats agreeing in the trade's direction.",
    "Risk is independent",
    "a short is judged exactly like a long",
)


@pytest.mark.parametrize("seat", (*SPECIALISTS, "portfolio_manager"))
def test_each_seat_is_told_how_its_view_counts(seat):
    text = _rendered(seat)
    for sentence in HOW_IT_COUNTS:
        assert sentence in text, f"{seat}: missing {sentence!r}"


@pytest.mark.parametrize("seat", ("portfolio_manager", "position_reviewer"))
def test_deciding_seats_are_told_the_objection_answer_field(seat):
    text = _rendered(seat)
    assert "`objection_answers`" in text
    for key in ('"seat"', '"objection_direction"', '"answer_text"', '"decision"', '"overrule" | "accept"'):
        assert key in text, f"{seat}: field part {key} not described"
    assert "it does not mean sell" in text


def test_the_field_named_in_the_prompts_is_the_real_one():
    from src.models.portfolio import TargetPosition
    from src.models.positions import ObjectionAnswer, PositionAction

    assert "objection_answers" in TargetPosition.model_fields
    assert "objection_answers" in PositionAction.model_fields
    assert set(ObjectionAnswer.model_fields) == {"seat", "objection_direction", "answer_text", "decision"}


def test_the_head_trader_answers_the_strongest_dissent():
    text = _rendered("portfolio_manager")
    assert "Answer the strongest dissent in writing." in text
    assert "a strong chart plus a fresh catalyst is enough even when other seats are lukewarm" in text


def test_risk_sizes_and_never_picks_stocks():
    text = _rendered("risk_manager")
    assert "You may veto (drop) or shrink an individual new trade; you never pick stocks." in text
    assert "Whether the thesis holds is the specialists' and the PM's call, not yours." in text


#: The 25-Sep wording (item 4). Lowercased substring match, as the registry does.
RETIRED = (
    "role-based bar",
    "timing veto",
    "all five seats right",
    "all five seats are right",
    "no single seat",
    "green-light",
    "±1",
    "self-declared conviction",
    "multi-seat conviction",
    "hard sell trigger",
    "every buy rule except timing",
    "aligned with your direction minus",
    "a net of zero or below",
    "you have no veto",
    "macro event inside the window",
    "senior macro strategist and intelligence analyst",
)


@pytest.mark.parametrize("seat", ALL_SEATS)
def test_no_seat_is_told_the_25_sep_wording(seat):
    text = _rendered(seat).lower()
    found = [p for p in RETIRED if p in text]
    assert not found, f"{seat}: retired wording still present: {found}"
