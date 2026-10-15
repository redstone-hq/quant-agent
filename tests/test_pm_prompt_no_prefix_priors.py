"""The PM prompt must not teach lessons drawn from pre-2026-10-12 trades.

Owner ruling 2026-10-09: trades before the Monday 2026-10-12 open are not
evidence. The momentum-leader starter sleeve paragraph is waiting on an owner
decision and is pinned byte-for-byte; every other line must be clean.
"""

import hashlib
from pathlib import Path

PROMPT = Path(__file__).resolve().parent.parent / "config" / "prompts" / "portfolio_manager.md"
SLEEVE_MARKER = "**Momentum-leader starter sleeve**"
SLEEVE_SHA256 = "aab7f2972a5e4974e0105d39063132ce5c6be3e7cafe2d23e6e14e8914ec8ceb"
BANNED = (
    "Apr–Jul",
    "predecessor account",
    "+3.8%",
    "single largest P&L drag",
    "≥20 closed trades",
    "Where the behavioural priors",
    "the account's own record wins",
)


def _split() -> tuple[str, str]:
    lines = PROMPT.read_text(encoding="utf-8").split("\n")
    sleeve = [ln for ln in lines if ln.startswith(SLEEVE_MARKER)]
    assert len(sleeve) == 1
    rest = "\n".join(ln for ln in lines if not ln.startswith(SLEEVE_MARKER))
    return sleeve[0], rest


def test_no_prefix_history_outside_the_sleeve_paragraph() -> None:
    _, rest = _split()
    found = [b for b in BANNED if b in rest]
    assert not found, found


def test_sleeve_paragraph_is_unchanged_but_for_the_review_note() -> None:
    sleeve, _ = _split()
    assert hashlib.sha256(sleeve.encode("utf-8")).hexdigest() == SLEEVE_SHA256
