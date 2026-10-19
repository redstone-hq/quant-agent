"""All-zero agreement rows are omitted from the PM briefing and COUNTED.

Split out of test_pm_input_shape.py so that file does not grow.
"""

from __future__ import annotations

import importlib
import re

from src.agents.portfolio_manager import PortfolioManagerAgent

scenarios = importlib.import_module("ops.model_policy.scenarios")

_LEVEL_LESS = scenarios.load_frozen_selection(scenarios.LEVEL_LESS_SELECTION_FIXTURE)


def _section(text: str, heading: str) -> str:
    parts = re.split(r"\n(?=## )", text)
    for part in parts:
        if part.startswith("## " + heading):
            return part
    raise AssertionError(f"section not found: {heading}")


# --- Cost: the all-zero agreement rows ------------------------------------
#
# MEASURED on a recorded production briefing (2026-10-04): 38 of the block's
# 82 rows read `0 aligned / 0 opposed` on BOTH sides, 6,768 of 87,234
# characters (7.8%), at the seat that is 91% of all model spend. A row that
# states no source took either side states nothing. It is omitted and
# COUNTED, so the seat can still tell "no coverage either way" from "this
# symbol is not in the registry". The deterministic refusal is untouched by
# this: `PortfolioConstructor` re-derives `signed_source_score` from the
# registry object, never from the text rendered here.
# Re-MEASURED 2026-10-04 on the same recorded briefing: every one of the 38
# all-zero rows carries the identical "macro stance broadcast" boilerplate,
# so dropping only caveat-free rows would have dropped NOTHING. An all-zero
# row goes whatever caveat it carries; what the caveats conveyed is carried
# by the breakdown on the single summary line.
_EMPTY_ROW_REGISTRY = {
    "AAA": {"technical": "buy", "news": "bearish"},
    "BBB": {"news": "neutral"},
    "CCC": {"technical": "neutral", "news": "mixed"},
    "DDD": {"smart_money": "bullish"},
    "EEE": {"macro": "neutral"},
    "FFF": {"earnings": "buy"},
}
# EEE's only stance is a broadcast macro one with no direction, FFF's only
# stance is stale. A broadcast DIRECTIONAL stance still counts AGAINST the
# opposite side, so such a row is not all-zero and is never dropped.
_EMPTY_ROW_BROADCAST = {"EEE": {"macro"}}
_EMPTY_ROW_STALE = {"FFF": {"earnings"}}
_INFORMATIVE_ROWS = (
    "- AAA: if long 1 seat(s) for / 1 against, entry backstop +0; "
    "if short 1 seat(s) for / 1 against, entry backstop +0 "
    "(of 2 source(s) with current coverage)",
    "- DDD: if long 1 seat(s) for / 0 against, entry backstop +1; "
    "if short 0 seat(s) for / 1 against, entry backstop -1 "
    "(of 1 source(s) with current coverage)",
)


def _render_with_registry(monkeypatch, registry, *, broadcast=None, stale=None) -> str:
    monkeypatch.setattr(
        PortfolioManagerAgent,
        "build_evidence_registry",
        lambda self, **kwargs: dict(registry),
    )
    monkeypatch.setattr(
        PortfolioManagerAgent,
        "broadcast_macro_sources",
        lambda self, **kwargs: dict(broadcast or {}),
    )
    monkeypatch.setattr(
        PortfolioManagerAgent,
        "stale_evidence_sources",
        lambda self, **kwargs: dict(stale or {}),
    )
    sel = _LEVEL_LESS.raw
    account = sel["account"]
    memory = sel["memory"]
    agent = PortfolioManagerAgent.__new__(PortfolioManagerAgent)
    return agent.build_user_message(
        analyses=_LEVEL_LESS.analyses,
        positions=_LEVEL_LESS.positions,
        macro_analysis=sel["macro_analysis"],
        cash_balance=account["cash_balance"],
        reserve_balance=account["reserve_balance"],
        total_value=account["total_value"],
        news_intel=_LEVEL_LESS.news,
        earnings_analyses=sel["earnings_analyses"],
        recent_performance=sel["recent_performance"],
        position_history=sel["position_history"],
        yesterday_insights=sel["yesterday_insights"],
        weekly_narrative=memory["weekly_narrative"],
        macro_trajectory=memory["macro_trajectory"],
        active_state_changes=memory["active_state_changes"],
        projected_portfolio=memory["projected_portfolio"],
        calibration_note=memory["calibration_note"],
        recent_missed_lessons=memory["recent_missed_lessons"],
        recent_loss_pits=memory["recent_loss_pits"],
        allow_margin=account["allow_margin"],
        session_type=account["session_type"],
        allowed_buy_symbols=set(sel["allowed_buy_symbols"]),
        transient_admitted_symbols=set(sel["transient_admitted_symbols"]),
    )


def test_rows_with_no_source_on_either_side_are_omitted_and_counted(monkeypatch):
    """Both informative rows survive byte-for-byte, both all-zero rows are
    gone, and one line states how many were omitted and why."""
    agreement = _section(
        _render_with_registry(
            monkeypatch,
            _EMPTY_ROW_REGISTRY,
            broadcast=_EMPTY_ROW_BROADCAST,
            stale=_EMPTY_ROW_STALE,
        ),
        "Independent Source Agreement",
    )
    for row in _INFORMATIVE_ROWS:
        assert row in agreement
    for dropped in ("BBB", "CCC", "EEE", "FFF"):
        assert f"- {dropped}:" not in agreement
    assert "0 seat(s) for / 0 against" not in agreement
    # A caveat-bearing all-zero row goes too, and its caveat survives as a
    # count. This is the whole fix: filtering on "no caveat" dropped 0 rows.
    assert "stance broadcast — one-sided" not in agreement
    assert (
        "- (4 further symbol(s) are present in the registry above but have "
        "no aligned and no opposed source on either side — net +0 long and "
        "net +0 short — so their rows are omitted here; omitted does NOT "
        "mean absent. 1 of them have only a one-sided broadcast macro "
        "stance, which cannot count FOR a trade — see the note below. "
        "1 of them have only a stale stance, counted neither way.)"
    ) in agreement


def test_no_omission_line_when_every_row_says_something(monkeypatch):
    """The count line must not appear when nothing was dropped."""
    agreement = _section(
        _render_with_registry(
            monkeypatch,
            {k: v for k, v in _EMPTY_ROW_REGISTRY.items() if k in ("AAA", "DDD")},
        ),
        "Independent Source Agreement",
    )
    assert "further symbol(s)" not in agreement
    for row in _INFORMATIVE_ROWS:
        assert row in agreement
