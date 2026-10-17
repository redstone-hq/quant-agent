"""The technical seat must be told the bar window the code actually sends.

THE DEFECT THIS CLOSES (board item 98). `config/prompts/tech_analyst.md` told
the model "only the last 20 bars are attached here", leaned on "the 20 bars"
for pivots and gap detection, and named "the 20 bars you are shown" when
explaining why structural levels come from elsewhere — while
`_BARS_PER_SYMBOL` had been 40 and the slice had been sending 40. The
technical analyst is the ONLY seat allowed to halt the desk, so a seat
mis-briefed about its own inputs is the worst place in the system for this
class of drift. Two prior prompt-drift PRs (#464, #467) both shipped without
touching it, which is why this file exists: the fix is not a wording change,
it is a mechanical coupling plus a check that the coupling still holds.

WHY THESE CHECKS AND NOT AN ASSERTION ON "40". Asserting the sheet says 40
would pass today and rot the moment `_BARS_PER_SYMBOL` moves — the same shape
of defect, one number later. So nothing here names a bar count except the
data-sufficiency floor it reads back out of the sheet.

WHAT THE FIRST DRAFT OF THIS FILE MISSED, found by adversary review before it
shipped, and why each check below is shaped the way it is:

  * A placeholder written `{{ tech.bars_per_symbol }}` with spaces stopped
    substituting and every test still passed — literal template syntax would
    have shipped to the seat. The renderer is now a regex, and
    `test_rendered_prompt_leaves_no_template_syntax` scans for ANY surviving
    braces rather than for the one exact spelling.
  * Changing the slice to `bars[:_BARS_PER_SYMBOL]` — the OLDEST 40 bars, with
    the sheet still saying "most recent" — passed the AST check, because that
    check only asked whether the constant appeared in some slice somewhere.
    `test_user_message_sends_the_most_recent_window` now builds a real user
    message and reads which bars came out, which is a behaviour check and not
    a syntax one.
  * `docs/OUTCOME.md` records that NEITHER confirmed prompt-drift defect ever
    lived in a prompt FILE — both were Python-assembled strings. A file scanner
    would have caught neither, so `test_user_message_states_no_bar_count` scans
    the assembled user message too.
  * The regex missed every reworded form ("40-bar window", "forty bars", "40
    sessions", "40 trading days", "40 daily candles"). All are matched now,
    spelled-out numbers included.

THE ONE EXEMPT "20". The sheet's "< 20 bars of history" is a data-sufficiency
rule — the minimum history below which the seat must return `neutral` — not a
claim about what was attached. The board item says to leave it alone. It is NOT
independent of the window, though: the seat can only count bars it was given,
so the rule is meaningful only while the window is at least that large. That is
now asserted rather than assumed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.tech_analyst import (
    _BARS_PER_SYMBOL,
    _BARS_PLACEHOLDER,
    _BARS_PLACEHOLDER_RE,
    PROMPT_PATH,
    TechAnalystAgent,
    render_bars_per_symbol,
    render_tech_placeholders,
)
from src.models import OHLCV, TechnicalIndicators

#: Numbers a future editor might spell rather than type. Not exhaustive and
#: cannot be — the behaviour checks below are what cover the general case; this
#: only closes the cheapest way to restate the window in prose.
_SPELLED = "ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|a dozen|two dozen|dozen"

#: A claim about the size of the attached window, in any of the forms the sheet
#: could plausibly use. Digits or spelled; bars, sessions, trading days or
#: candles; "<n> bars" or "<n>-bar window". Plural/possessive shapes on purpose:
#: the sheet also says "a made-up 1.5/2.0 bar was missed" (a hurdle, not a
#: window) and "the entry bar's own volatility", neither of which is a claim
#: about the input.
_BAR_CLAIM_RE = re.compile(
    r"(\d+|" + _SPELLED + r")"
    r"[\s-]*"
    r"(?:daily\s+|completed\s+|trading\s+)*"
    r"(?:bars\b|bar[\s-]window\b|sessions\b|trading\s+days\b|candles\b)",
    re.IGNORECASE,
)

#: The one correct, independent "20 bars" in the sheet — the data-sufficiency
#: floor, NOT a statement about the attached window. Matched as a phrase so it
#: survives the line moving; if the wording changes, these tests fail loudly and
#: whoever changed it decides whether the exemption still applies.
_DATA_SUFFICIENCY_PHRASE = "< 20 bars of history"
_DATA_SUFFICIENCY_FLOOR = 20

#: `src/data/context.py` writes "MA change since prior session (in ATR)" into
#: the market context block. It names no session count; the pattern is kept so
#: a "<n> sessions" wording reappearing there would still be exempted by shape
#: and not mistaken for a window claim.
_SLOPE_PHRASE_RE = re.compile(r"MA change since prior session \(in ATR\)")

#: The rendered history-depth phrase (board item 168) says "about <n> weekday
#: sessions", which is a claim about the UPSTREAM history the indicators are
#: computed from, not about the bar window attached to the message. It is
#: rendered from `trading.lookback_days` and checked end-to-end by
#: `tests/test_tech_analyst_history_window.py`; exempted here by shape so the
#: setting may change without touching this file.
_HISTORY_PHRASE_RE = re.compile(
    r"up to \d+ calendar days of price history "
    r"\(about \d+ weekday sessions[^)]*\)"
)

#: The rendered longest-indicator window (`LONGEST_INDICATOR_WINDOW` in
#: src/data/technical.py) — how far the deepest INDICATOR reaches, not how many
#: bars are attached. Rendered from that constant and checked in
#: tests/test_tech_analyst_history_window.py; exempted here by shape.
_LONGEST_INDICATOR_PHRASE_RE = re.compile(r"(?:longest|deepest)(?:\s+of\s+them)?(?:\s+reach\w+)?\s+\d+\s+sessions")

#: Bar counts to render the sheet against in the coupling test. All sit above
#: the data-sufficiency floor on purpose: rendering a sheet that says "you are
#: shown 7 bars" and also "return neutral below 20 bars" is not a configuration
#: this desk should be asserting is fine.
_PRETEND_COUNTS = (25, 40, 41, 123, 400)


def _prompt_source() -> str:
    return PROMPT_PATH.read_text()


def _bar_claims(text: str) -> list[str]:
    """Every window claim except the data-sufficiency floor, as written."""
    stripped = text.replace(_DATA_SUFFICIENCY_PHRASE, "<data-sufficiency-floor>")
    assert _DATA_SUFFICIENCY_PHRASE not in stripped
    stripped = _SLOPE_PHRASE_RE.sub("<ma-slope-lookback>", stripped)
    stripped = _HISTORY_PHRASE_RE.sub("<history-window>", stripped)
    stripped = _LONGEST_INDICATOR_PHRASE_RE.sub("<longest-indicator-window>", stripped)
    return [m.group(1) for m in _BAR_CLAIM_RE.finditer(stripped)]


def _bars(count: int) -> list[OHLCV]:
    """`count` ascending daily bars, each identifiable by its close price.

    Close == index, so a test can read straight off the rendered message WHICH
    bars were sent, not merely how many.
    """
    start = date(2026, 1, 5)
    return [
        OHLCV(
            date=start + timedelta(days=i),
            open=float(i),
            high=float(i),
            low=float(i),
            close=float(i),
            volume=1_000_000,
        )
        for i in range(count)
    ]


def _indicators() -> TechnicalIndicators:
    return TechnicalIndicators(
        symbol="SPY",
        ma_20=505.0,
        ma_50=498.0,
        ma_200=450.0,
        rsi_14=58.0,
        macd=1.0,
        macd_signal=0.5,
        macd_hist=0.5,
        bb_upper=520.0,
        bb_middle=505.0,
        bb_lower=490.0,
        atr_14=8.5,
        volume_change_pct=15.0,
    )


def _user_message(bar_count: int) -> str:
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        return agent.build_user_message(
            symbols_data=[
                {"symbol": "SPY", "bars": _bars(bar_count), "indicators": _indicators()},
            ],
        )


# --------------------------------------------------------------------------
# The sheet: one home for the number
# --------------------------------------------------------------------------


def test_data_sufficiency_rule_is_still_present_and_untouched() -> None:
    """The exemption must describe something that actually exists.

    If this phrase disappears, the exemption above is silently protecting
    nothing and the other tests would be scanning a sheet whose sufficiency
    rule was rewritten — a review question, not a pass.
    """
    assert _DATA_SUFFICIENCY_PHRASE in _prompt_source(), (
        f"The technical seat's data-sufficiency rule {_DATA_SUFFICIENCY_PHRASE!r} "
        f"is gone from {PROMPT_PATH.name}. That rule is SEPARATE from the "
        f"attached-window count and board item 98 said to leave it alone. If it "
        f"was deliberately reworded, update _DATA_SUFFICIENCY_PHRASE here and say "
        f"in the PR why the minimum-history floor changed."
    )


def test_window_is_at_least_the_data_sufficiency_floor() -> None:
    """The window must be big enough for the sufficiency rule to mean anything.

    The seat can only count the bars it is attached. If `_BARS_PER_SYMBOL` ever
    drops below the floor the sheet states, every symbol satisfies "fewer than
    N bars of history" and the correct answer to every chart becomes `neutral`.
    Also what makes the exemption above honest: the floor is independent of the
    window only while this holds.
    """
    assert _BARS_PER_SYMBOL >= _DATA_SUFFICIENCY_FLOOR, (
        f"_BARS_PER_SYMBOL is {_BARS_PER_SYMBOL} but the sheet tells the seat to "
        f"return `neutral` below {_DATA_SUFFICIENCY_FLOOR} bars of history. Every "
        f"symbol would now trip that rule. Lower the floor in the sheet, or raise "
        f"the window."
    )


def test_prompt_source_carries_no_hand_typed_bar_count() -> None:
    """No literal window count may survive in the sheet.

    A literal is the second home for the number, and the second home is the
    whole defect. Every window claim must be the placeholder.
    """
    literals = _bar_claims(_prompt_source())
    assert not literals, (
        f"{PROMPT_PATH.name} states a hand-typed bar-window count {literals} "
        f"instead of {_BARS_PLACEHOLDER}. The count the seat is shown has ONE "
        f"home — `_BARS_PER_SYMBOL` in src/agents/tech_analyst.py — and the "
        f"sheet must render it, not restate it. This is board item 98 coming "
        f"back; it was missed by two prior prompt-drift PRs (#464, #467)."
    )


def test_prompt_source_actually_uses_the_placeholder() -> None:
    """The sheet must still tell the model the window size at all.

    Deleting every mention would pass the no-literals test while leaving the
    seat uninformed.
    """
    assert _BARS_PLACEHOLDER_RE.search(_prompt_source()), (
        f"{PROMPT_PATH.name} no longer mentions the OHLCV window at all. The "
        f"technical seat reads pivots, gaps and micro-structure off that window "
        f"and must be told how big it is, via {_BARS_PLACEHOLDER}."
    )


# --------------------------------------------------------------------------
# Rendering: the coupling is live, not a coincidence of today's value
# --------------------------------------------------------------------------


def test_rendered_prompt_states_the_count_the_code_sends() -> None:
    claims = _bar_claims(render_tech_placeholders(_prompt_source()))
    assert claims, "rendering produced no bar-window claim at all"
    wrong = sorted({c for c in claims if c != str(_BARS_PER_SYMBOL)})
    assert not wrong, (
        f"The technical seat's rendered sheet claims {wrong} while the code "
        f"sends {_BARS_PER_SYMBOL} (`_BARS_PER_SYMBOL`). The ONLY seat that can "
        f"halt the desk would be reading micro-structure off a window it has "
        f"been mis-told the size of."
    )


def test_rendered_prompt_leaves_no_template_syntax() -> None:
    """ANY surviving braces, not just the one exact placeholder spelling.

    The first draft checked for the exact string, so `{{ tech.bars_per_symbol }}`
    with spaces — or a typo in one of the five placeholders — rendered as literal
    template syntax to the seat with every test green.
    """
    rendered = render_tech_placeholders(_prompt_source())
    leftovers = re.findall(r"\{\{[^}]*\}\}", rendered)
    assert not leftovers, (
        f"unsubstituted template syntax survived rendering: {leftovers}. The "
        f"technical seat would be shown this verbatim. Check the spelling against "
        f"{_BARS_PLACEHOLDER} — note that `{{{{risk.*}}}}` placeholders do NOT "
        f"resolve on this sheet (see src/agents/prompt_limits.py)."
    )


@pytest.mark.parametrize("pretend_bars", _PRETEND_COUNTS)
def test_rendered_count_tracks_a_changed_constant(pretend_bars: int) -> None:
    """Renders the real sheet against counts the repo has never used.

    This is what makes the check durable rather than a re-assertion of today's
    number: a future edit that hard-codes any value — including the one that
    happens to be right today — fails here.
    """
    claims = _bar_claims(render_tech_placeholders(_prompt_source(), pretend_bars))
    assert claims, "rendering produced no bar-window claim at all"
    assert set(claims) == {str(pretend_bars)}, (
        f"With `_BARS_PER_SYMBOL` = {pretend_bars} the sheet still claims "
        f"{sorted(set(claims))}. Some window claim is hard-coded rather than "
        f"rendered from the constant, so prompt and code can diverge again."
    )


def test_agent_system_prompt_is_rendered() -> None:
    """The live path renders too — not just the helper the tests above call."""
    agent = TechAnalystAgent.__new__(TechAnalystAgent)
    prompt = agent.system_prompt
    assert not re.search(r"\{\{[^}]*\}\}", prompt), (
        "TechAnalystAgent.system_prompt is serving the sheet unrendered — the "
        "seat would receive literal template syntax"
    )
    assert set(_bar_claims(prompt)) == {str(_BARS_PER_SYMBOL)}, (
        "TechAnalystAgent.system_prompt does not state `_BARS_PER_SYMBOL` as the "
        "window size, so the rendering is not on the live path"
    )


# --------------------------------------------------------------------------
# The other half: what the code actually sends
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "available",
    [_BARS_PER_SYMBOL - 5, _BARS_PER_SYMBOL, _BARS_PER_SYMBOL + 60],
)
def test_user_message_sends_the_most_recent_window(available: int) -> None:
    """Behaviour, not syntax: how many bars go out, and WHICH ones.

    An AST check that the constant appears in some slice is not enough — the
    first draft of this file passed with `bars[:_BARS_PER_SYMBOL]`, i.e. the
    OLDEST 40 bars, under a sheet promising "the most recent". Each bar's close
    equals its index, so the rendered rows say exactly which were sent.
    """
    msg = _user_message(available)
    sent = [int(float(m)) for m in re.findall(r"C=([0-9.]+)", msg)]
    expected_count = min(available, _BARS_PER_SYMBOL)
    assert len(sent) == expected_count, (
        f"{available} bars available, {len(sent)} sent, but `_BARS_PER_SYMBOL` is "
        f"{_BARS_PER_SYMBOL} so the seat should have received {expected_count}. "
        f"The sheet states the window from that constant, so the code must honour "
        f"it — board item 98 is exactly this disagreement."
    )
    assert sent == list(range(available - expected_count, available)), (
        f"The seat was sent bars {sent[:3]}..{sent[-3:]} out of 0..{available - 1}. "
        f"The sheet promises the MOST RECENT window; these are not the last "
        f"{expected_count} bars."
    )


def test_user_message_states_no_bar_count() -> None:
    """The assembled user message must not restate the window size either.

    `docs/OUTCOME.md`: neither confirmed prompt-drift defect lived in a prompt
    FILE — both were Python-assembled strings, which a file scanner cannot see.
    If a count is ever stated here it becomes a second home for the number, so
    it must be rendered from the constant if it appears at all.
    """
    msg = _user_message(_BARS_PER_SYMBOL + 60)
    wrong = sorted({c for c in _bar_claims(msg) if c != str(_BARS_PER_SYMBOL)})
    assert not wrong, (
        f"The technical seat's USER MESSAGE claims a window of {wrong} while "
        f"`_BARS_PER_SYMBOL` is {_BARS_PER_SYMBOL}. A count written into an "
        f"assembled string is invisible to any prompt-file scan — the exact class "
        f"docs/OUTCOME.md records as the one that actually bit this desk twice."
    )
