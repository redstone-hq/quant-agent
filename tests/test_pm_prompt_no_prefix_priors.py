"""The PM prompt must not teach lessons drawn from pre-2026-10-12 trades.

Owner ruling 2026-10-09: trades before the Monday 2026-10-12 open are not
evidence. Owner ruling 2026-10-16: the momentum-leader starter sleeve, the last
paragraph built on those trades, is removed; fast movers come through the
velocity filter instead.
"""

from pathlib import Path

PROMPTS = Path(__file__).resolve().parent.parent / "config" / "prompts"
PROMPT = PROMPTS / "portfolio_manager.md"
BANNED = (
    "Apr–Jul",
    "predecessor account",
    "+3.8%",
    "single largest P&L drag",
    "≥20 closed trades",
    "Where the behavioural priors",
    "the account's own record wins",
)


def test_no_prefix_history_in_the_pm_prompt() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    found = [b for b in BANNED if b in text]
    assert not found, found


def test_starter_sleeve_is_gone_from_every_prompt() -> None:
    texts = {p.name: p.read_text(encoding="utf-8").lower() for p in PROMPTS.glob("*.md")}
    hits = [name for name, text in texts.items() if "starter sleeve" in text or "starter-sleeve" in text]
    assert hits == []
