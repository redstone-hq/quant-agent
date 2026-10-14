"""Plain wording for the desk states that had none (filled 2026-10-02).

Split out of `ops/rehearsal/report.py`, which is tracked by the shrink-only
file-size baseline and may not grow; the report merges `EXTRA_PLAIN` into
`STATUS_PLAIN`. Each entry says what happened and whether the fault is in the
desk or outside it. Where one token has several producers, the wording says
so rather than picking one meaning.

It is also the ONE table behind the owner's Telegram wording (2026-10-14): the
codes the decision line and the close-review block can print, each as plain
words, under `OWNER_WORDS` below. Display only; no AI prompt reads it.
"""

from __future__ import annotations

import re

from src.intraday_scan_outcome import OUT_OF_CREDIT_PLAIN

UNWORDED_PLAIN = {
    # Produced by the pipeline's kill-switch check (src/pipeline.py).
    "kill_switch_halted": (
        "The desk stopped itself on purpose: the kill-switch file exists, so "
        "the run halted before touching the broker or any paid analysis. "
        "This is not a fault. Someone placed that file; the desk stays "
        "stopped until it is removed. Positions already held keep their "
        "protective stops; only new orders are refused."
    ),
    # Produced by the pre-market data refresh (earnings preprocess session),
    # as the `smart_money_refresh` sub-result, when the switch is off.
    "disabled": (
        "The pre-market refresh of insider and congressional trading data "
        "is switched off in the desk's settings, so it did not run. This is "
        "a setting, not a fault, but the research seats get no fresh "
        "smart-money data while it stays off."
    ),
    # Produced by the same refresh, and by the congressional-trading source,
    # when a data provider call raised an exception.
    "provider_error": (
        "The pre-market refresh of insider and congressional trading data "
        "failed because a data source (the SEC filings feed or the "
        "congressional-trades feed) could not be read. The cause is outside "
        "the desk, so smart-money research runs on older data until the "
        "next successful refresh."
    ),
    # Produced by two different background jobs, not by a trading session:
    # the daily account export and the stock-universe screening pass.
    "error": (
        "A background job failed. This token comes from two different jobs, "
        "so the reported reason below says which: the daily account export "
        "(no portfolio data, or the Telegram upload failed) or the "
        "stock-universe screening pass (the desk's own code raised). "
        "No trading session was involved and no order was affected."
    ),
}

EXTRA_PLAIN = {**OUT_OF_CREDIT_PLAIN, **UNWORDED_PLAIN}


# ---------------------------------------------------------------------------
# Owner Telegram wording: one table, keyed "<kind>:<code>".
# ---------------------------------------------------------------------------
#
# The owner reads on a phone and holds no labels in his head. Every internal
# code `trader_feed.decision` and `trader_feed.trade_block` can print, and the
# raw field names the risk seat's prose quotes, are worded here and nowhere
# else. `tests/test_owner_text_plain_words.py` enumerates those codes from the
# renderers' own source and the models, and fails on any code with no entry.

# `models.RiskReasonCategory`: what the code MEANS, as a clause.
_RISK_CATEGORY = {
    "clean": "no changes asked for",
    "oversized": "sizing too aggressive for the conviction",
    "rr_fail": "reward too thin for the risk",
    "concentration": "too much in one sector or one name",
    "correlation_risk": "too many positions moving together",
    "event_risk": "an event (earnings, Fed, macro) too close",
    "macro_misalign": "plan runs against the market backdrop",
    "data_degraded": "several research inputs failed",
    "signal_fidelity": "PM contradicted the chart research without saying why",
    "other": "a reason outside the usual categories",
}

# The risk seat's own output field names, as the SHORTEST noun phrase a
# non-specialist understands, for when its prose quotes one. The owner reads on
# a phone: length is a correctness problem. `scale_all_buys` is the one that
# takes a value ("new buys at 80% of plan", see `plain_prose`).
_RISK_FIELD = {
    "scale_all_buys": "new buys",
    "rejected_symbols": "refused names",
    "reason_category": "reason type",
    "reasoning_chain": "reasoning steps",
    "rr_audit": "reward-vs-risk check",
    "signal_fidelity": "PM-vs-charts check",
    "correlation_check": "move-together check",
    "event_risk": "nearby-event check",
    "sizing_sanity": "size check",
    "original_value": "old value",
    "new_value": "new value",
}

# `trade_review.close_review_summary` keys the close-review block prints.
_TRADE_REVIEW = {
    "n": "Closed today",
    "wins": "wins",
    "losses": "losses",
    "win_rate_pct": "Win rate",
    "total_r": "total",
    "average_r": "average",
    "best": "Best",
    "worst": "worst",
    "profit_factor": "Won per $1 lost",
    "quality_n": "trades with known result",
    "p_value": "odds it was luck",
}

# The words `trade_review.profit_factor` returns in place of a number.
_PROFIT_FACTOR_VALUE = {
    "n/a": "no trades",
    "no losses": "no losing trades",
}

OWNER_WORDS: dict[str, str] = {
    **{f"profit_factor_value:{code}": words for code, words in _PROFIT_FACTOR_VALUE.items()},
    **{f"risk_category:{code}": words for code, words in _RISK_CATEGORY.items()},
    **{f"risk_field:{code}": words for code, words in _RISK_FIELD.items()},
    **{f"trade_review:{code}": words for code, words in _TRADE_REVIEW.items()},
}


def owner_words(kind: str, code: object) -> str:
    """The plain words for one code. KeyError for a code with no entry (a test fails first)."""
    return OWNER_WORDS[f"{kind}:{str(code).strip().lower()}"]


def words_for(kind: str) -> dict[str, str]:
    """Every `{code: words}` of one kind."""
    prefix = f"{kind}:"
    return {key[len(prefix) :]: words for key, words in OWNER_WORDS.items() if key.startswith(prefix)}


_NOT_IN_A_WORD = r"(?<![A-Za-z0-9_])({})(?![A-Za-z0-9_])"
_FIELD_NAMES = sorted(words_for("risk_field"), key=len, reverse=True)
_FIELD_TOKEN = re.compile(_NOT_IN_A_WORD.format("|".join(map(re.escape, _FIELD_NAMES))), re.IGNORECASE)
_CUT_WITH_VALUE = re.compile(
    r"(?<![A-Za-z0-9_])scale_all_buys(?:\s*(?:=|:|to|of|at)\s*|\s+)(\d+(?:\.\d+)?|\.\d+)(%?)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _cut_with_value(match: re.Match) -> str:
    value = float(match.group(1))
    percent = value if match.group(2) else value * 100
    if not 0 <= percent <= 100:
        return match.group(0)
    return f"{owner_words('risk_field', 'scale_all_buys')} at {percent:.0f}% of plan"


def plain_prose(text: str) -> str:
    """AI prose with any raw risk-seat field name replaced by its plain words.

    Only the snake_case names in the table are touched, so ordinary words and
    tickers pass through unchanged. `scale_all_buys 0.8` becomes "new buys at
    80% of plan".
    """
    if not text or "_" not in text:
        return text
    text = _CUT_WITH_VALUE.sub(_cut_with_value, text)
    return _FIELD_TOKEN.sub(lambda m: owner_words("risk_field", m.group(1)), text)
