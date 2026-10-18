"""thesis_invalid_if checker, lifted verbatim from exit_guard.py."""

from __future__ import annotations

from typing import Literal
from collections.abc import Mapping
import re
from dataclasses import dataclass
from src.risk.exit_guard_deltas import _finite
from src.risk.exit_guard_thesis_macro import (
    _clean_number,
    _macro_threshold_in_series_unit,
    _MACRO_SERIES_RE,
    _MACRO_SERIES_UNITS,
)


# ---------------------------------------------------------------------------
# thesis_invalid_if checker — measured, not assumed
# ---------------------------------------------------------------------------
#
# `thesis_invalid_if` is free text an analyst writes at entry (e.g. "closes
# below the 50-day average", "loses the $142 level", "RSI drops below 30").
# Until now NOTHING checked whether it had actually become true when a SELL
# was later proposed citing it — pure honour system.
#
# General-purpose parsing of arbitrary English into an executable check was
# floated as too large/unreliable to attempt. Rather than accept that as a
# guess, it was measured against real recorded text: 20 real executed BUYs
# carrying an `(invalid if: ...)` / `(thesis_invalid_if: ...)` marker in
# `trades.reasoning`, plus 1,028 unique real, un-truncated
# `thesis_invalid_if` values recorded in `specialist_evidence` (tech-analyst
# candidates, 1,203 non-null occurrences before de-duplication) — see
# `docs/WORK.md` for the query and full bucket counts. Every one of those
# real values fell into exactly two checkable shapes:
#
#   - a moving-average reference ("closes below MA20", "the 50-day average")
#     — 984 of 1,028 unique specialist_evidence values (95.7%); every one of
#     the 20 real executed-trade examples that referenced an MA used MA20 or
#     MA50, never MA200 or any other period.
#   - a bare price level ("closes below the $218.51 support level", "loses
#     the $142 level") — 38 of 1,028 (3.7%).
#
# Everything else — a single Bollinger Band mention and a handful of
# qualitative conditions ("guidance pulled", "AI accelerator rollout faces
# delays") — was too rare (6 of 1,028, 0.6%) and, for the qualitative cases,
# inherently unparseable, to justify building or trusting a checker for. No
# RSI-threshold example was found in either real source at all, despite RSI
# thresholds being a plausible category in principle.
#
# So this checker covers ONLY the two shapes that are both common and safe:
# a bare numeric price level, and a moving-average reference where the
# caller already has the corresponding computed MA (20/50/200, matching
# `src/data/technical.py::compute_indicators` — no new indicator is computed
# here). It NEVER computes or fetches its own market data; the caller passes
# in whatever is already on hand from the same pipeline that would otherwise
# just trust the honour system.
#
# A compound condition ("closes below MA50 OR breaks $180 support") is
# deliberately refused (UNPARSEABLE) rather than partially evaluated: only
# unambiguous single conditions are checked. Same posture as the rest of
# this module — fail closed, only ever assert a fact that is provably true
# or provably false, never guess.
#
# NOT WIRED IN. This is a standalone, tested function. Wiring it into
# `RiskStage` or anywhere in the live pipeline is the next step, and is
# blocked on a parallel piece of work giving `thesis_invalid_if` its own
# dedicated, un-truncated field on `TradeDecision` — reading today's
# `reasoning`-embedded text here would mean trusting the same lossy,
# truncated string this whole effort exists to stop trusting.

#: Words that mean the condition fires when price goes DOWN through a level.
_DOWN_WORDS_RE = re.compile(r"\b(?:below|under|loses)\b", re.IGNORECASE)

#: Words that mean the condition fires when price goes UP through a level.
_UP_WORDS_RE = re.compile(r"\b(?:above|over|reclaims|clears)\b", re.IGNORECASE)

#: MA reference: "MA20", "MA 50", "SMA200", "50-day moving average",
#: "50-day MA". Captures the period so it can be matched to the caller's
#: ma_20 / ma_50 / ma_200 — only those three periods are ever computed
#: elsewhere in this pipeline, so anything else is UNPARSEABLE rather than
#: guessed at.
_MA_REF_RE = re.compile(
    r"\b(?:MA|SMA|EMA)[\s-]?(\d{2,3})\b|(\d{2,3})-day\s+(?:moving\s+)?(?:average|MA|SMA|EMA)\b",
    re.IGNORECASE,
)

#: A dollar-prefixed number anywhere in the text — the clearest possible
#: signal of a fixed price level, integer prices included ("$1000").
_DOLLAR_PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

#: A decimal number immediately next to a level/support/resistance word.
#: Decimal is required (no dollar sign) so this cannot mis-fire on an
#: unrelated integer such as an RSI threshold or a day count.
_LEVEL_WORD_PRICE_RE = re.compile(
    r"([\d,]+\.\d+)\s*(?:level|support|resistance)\b"
    r"|(?:support|resistance|level)\D{0,15}?([\d,]+\.\d+)",
    re.IGNORECASE,
)

#: A decimal number close after a direction word ("below 85.50", "above the
#: 108.69"). Decimal required for the same false-positive reason as above.
_DIRECTION_PRICE_RE = re.compile(
    r"\b(?:above|below|over|under)\b\D{0,12}?([\d,]+\.\d+)",
    re.IGNORECASE,
)

#: MA periods this checker will compare against — must match what
#: `src/data/technical.py::compute_indicators` actually computes.
_SUPPORTED_MA_PERIODS = (20, 50, 200)


@dataclass(frozen=True)
class ThesisInvalidationCheck:
    """Result of checking one `thesis_invalid_if` string against real data.

    `status` is one of:
      - "TRIGGERED"     — the condition is provably true right now.
      - "NOT_TRIGGERED" — the condition is provably false right now.
      - "UNPARSEABLE"   — cannot say, for any reason (compound condition,
        unsupported reference, qualitative/news condition, or a required
        market data point the caller didn't supply). Never a guess.
    """

    status: Literal["TRIGGERED", "NOT_TRIGGERED", "UNPARSEABLE"]
    detail: str


def _extract_price_threshold(text: str) -> float | None:
    """The single fixed price named in `text`, or None if none is found.

    Tries, in order of confidence: a dollar-prefixed amount, a decimal
    number next to "level"/"support"/"resistance", a decimal number right
    after a direction word. Stops at the first match — this function is
    only reached once the caller has already established the condition is
    a plain price level (not an MA reference), so the first plausible
    number is the level.
    """
    for pattern in (_DOLLAR_PRICE_RE, _LEVEL_WORD_PRICE_RE, _DIRECTION_PRICE_RE):
        m = pattern.search(text)
        if m:
            group = next(g for g in m.groups() if g is not None)
            return _clean_number(group)
    return None


def check_thesis_invalid_if(
    thesis_invalid_if: str,
    current_price: float | None,
    *,
    ma_20: float | None = None,
    ma_50: float | None = None,
    ma_200: float | None = None,
    macro_levels: Mapping[str, float | None] | None = None,
) -> ThesisInvalidationCheck:
    """Check whether a `thesis_invalid_if` condition has become true.

    Covers exactly three shapes, chosen because they are the real cases the
    seats actually write (see the module-level notes above for the measured
    counts) and are checkable without guessing:

      - a bare numeric price level ("closes below the $218.51 support
        level", "loses the $142 level") — checked against `current_price`.
      - a moving-average reference ("closes below MA20", "the 50-day
        average") for period 20, 50 or 200 — checked against the matching
        `ma_20` / `ma_50` / `ma_200` the caller supplies.
      - a named macro series with a level ("HY OAS widens back above
        420bps", "VIX above 30") — checked against the matching entry in
        `macro_levels`, which the caller fills from the macro summary it
        already fetched. Same strict above/below test; bps<->% is the only
        conversion applied and an unlabelled spread number is refused.

    Everything else — a compound "A or B" condition, an unsupported MA
    period, an indicator threshold (RSI/MACD/Bollinger), a qualitative or
    news-based condition, or a required price/MA/macro value the caller left
    as None — returns UNPARSEABLE. This function never fetches or computes
    market data itself; it only compares numbers the caller already has.
    """
    text = (thesis_invalid_if or "").strip()
    if not text:
        return ThesisInvalidationCheck("UNPARSEABLE", "empty thesis_invalid_if")

    if re.search(r"\bor\b", text, re.IGNORECASE):
        return ThesisInvalidationCheck(
            "UNPARSEABLE",
            "compound condition ('...or...') — refusing to partially evaluate",
        )

    down = bool(_DOWN_WORDS_RE.search(text))
    up = bool(_UP_WORDS_RE.search(text))
    if down == up:  # neither found, or both found (ambiguous/contradictory)
        return ThesisInvalidationCheck(
            "UNPARSEABLE",
            "no unambiguous direction (above/below) found",
        )
    direction: Literal["down", "up"] = "down" if down else "up"

    cur = _finite(current_price)

    ma_match = _MA_REF_RE.search(text)
    if ma_match:
        period = int(ma_match.group(1) or ma_match.group(2))
        if period not in _SUPPORTED_MA_PERIODS:
            return ThesisInvalidationCheck(
                "UNPARSEABLE",
                f"MA{period} referenced but only MA20/MA50/MA200 are computed "
                "upstream — not guessing at an uncomputed indicator",
            )
        ma_value = _finite({20: ma_20, 50: ma_50, 200: ma_200}[period])
        if cur is None or ma_value is None:
            return ThesisInvalidationCheck(
                "UNPARSEABLE",
                f"MA{period} condition recognised but current_price or ma_{period} was not supplied",
            )
        fired = cur < ma_value if direction == "down" else cur > ma_value
        status = "TRIGGERED" if fired else "NOT_TRIGGERED"
        return ThesisInvalidationCheck(
            status,
            f"price {cur} vs MA{period} {ma_value:.2f}, condition was '{direction}'",
        )

    for key, pattern in _MACRO_SERIES_RE:
        series_match = pattern.search(text)
        if series_match is None:
            continue
        level = _finite((macro_levels or {}).get(key))
        if level is None:
            return ThesisInvalidationCheck(
                "UNPARSEABLE",
                f"'{key}' condition recognised but the caller supplied no current {key} level",
            )
        series_unit = _MACRO_SERIES_UNITS[key]
        macro_threshold, why = _macro_threshold_in_series_unit(
            text,
            series_unit,
            series_match.span(),
        )
        if macro_threshold is None:
            return ThesisInvalidationCheck("UNPARSEABLE", why)
        fired = level < macro_threshold if direction == "down" else level > macro_threshold
        return ThesisInvalidationCheck(
            "TRIGGERED" if fired else "NOT_TRIGGERED",
            f"{key} {level} vs level {macro_threshold} ({series_unit}), condition was '{direction}'",
        )

    threshold = _extract_price_threshold(text)
    if threshold is None:
        return ThesisInvalidationCheck(
            "UNPARSEABLE",
            "no MA reference, named macro series or numeric price level found in text",
        )
    if cur is None:
        return ThesisInvalidationCheck(
            "UNPARSEABLE",
            "price level condition recognised but current_price was not supplied",
        )
    fired = cur < threshold if direction == "down" else cur > threshold
    status = "TRIGGERED" if fired else "NOT_TRIGGERED"
    return ThesisInvalidationCheck(
        status,
        f"price {cur} vs level {threshold}, condition was '{direction}'",
    )
