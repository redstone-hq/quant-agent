"""Deterministic trailing stops — spec Phase 3.7.

Trailing is arithmetic. It belongs in Python, not in an LLM's discretion.

Until this landed, every stop movement came from the Position Reviewer
emitting a `TRAIL_STOP` action with a price it chose, clamped after the fact
by a ratchet cooldown and a 1.25x ATR noise band. Those clamps exist because
the discretionary version marched stops into the daily-noise band in three or
four sessions — GE was ratcheted 325 -> 350 in eight sessions on one flag.
Clamping a bad generator is not the same as having a good one.

The rule depends on how the position is MANAGED, which the Technical Analyst
decides at entry and which is pinned to the trade row alongside the horizon:

**Type A — `range`.** There is structure on both sides. The structural trail
USED TO BE gated behind the recorded take-profit target, on the reasoning
that a position which trails on every wiggle gets stopped out inside the very
range it was bought to traverse. That gate was REMOVED on 2026-10-01 (item
212, see `docs/INCIDENT_HISTORY.md`): the target is an unsourced number that
never reaches the broker, and the structural trail now runs from entry on
Type A exactly as it does on Type B. What the gate was really protecting
against is now carried by the invariants every leg must clear — the ATR
noise band, the minimum ratchet, and the minimum bar count below — not by a
target. The paragraphs that follow describe the R-multiple ratchets, which
are UNCHANGED; where they say the structural trail does not run below the
target, that sentence is superseded by this one.

2026-09-04 audit fix #3: "no trailing until the target is exceeded" used to
mean a Type A trade got ZERO profit protection for its entire life until it
had captured 100% of the planned move — the single largest un-backtested,
asymmetric-downside rule found in the exit-management audit, and Type A is
this desk's most common setup by real observed frequency. A trade could
travel 90%+ of the way to its target and give back all of it with nothing
in place. Standard, widely cited practice (Van Tharp's R-multiple framework;
Elder's "Triple Screen"; the same R-multiple convention this codebase's own
docs already use elsewhere) is to move the stop to breakeven once a trade
has banked a defensible fraction of its planned risk — commonly +1R (one
initial-risk-unit of profit). `compute_trailing_stop` now does exactly that
for Type A specifically, ADDITIVE to the structural trail (which, since item
212, runs from entry on Type A too): once price reaches entry +/- 1R (see
`RANGE_BREAKEVEN_R_MULTIPLE`), the stop ratchets to breakeven if it hasn't
already reached breakeven or better; once price then goes on to exceed the
full target, the pre-existing structural/chandelier trail below takes back
over exactly as before. Type B's trail-from-entry behaviour is untouched —
it already rides the position from day one and has no equivalent gap.

2026-09-25, item 142 (owner-ratified): breakeven alone still gave back
everything a range trade earned BETWEEN breakeven and the target on a
reversal. A SECOND ratchet now stacks on top of the breakeven step and stays
below the target: once price reaches +2R (see `RANGE_SECOND_RATCHET_TRIGGER_R`)
the stop moves up to +1R (see `RANGE_SECOND_RATCHET_LOCK_R`), locking one
initial-risk-unit of gain. This REDUCES the give-back to a +1R floor once +2R
is tagged; it does NOT close the give-back gap — between +1R (the lock) and the
target the stop is pinned at +1R and no structural trail runs, so a run to +5R
then a reversal still gives back to +1R. The residual give-back between +1R and
target is unchanged. Both steps are Type A only, both fire only below the
target, and both ratchet the stop UP only — never down. Breakouts are
untouched: they use the structural/chandelier trail from entry and never reach
either R-multiple step.

**Type B — `breakout`.** There is no overhead structure and the target is a
measured-move reference, not a level. Progress and pace are meaningless here
(see `pipeline._build_position_facts`), so trailing IS the management: ride it
and let structure decide when it is over. Trail from entry, under each
successive higher low, with a chandelier stop where structure is unclear.
(Short mirror: trail from entry, above each successive lower high, chandelier
above the lowest low since entry where structure is unclear.)

Invariants, all of them enforced below:
  - **Ratchet toward less risk only.** Up for a long, down for a short. A
    stop never moves the wrong way. Ever.
  - A move must clear the existing stop by `MIN_RATCHET_TICKS` venue ticks —
    the smallest price increment the venue will accept — to be a different
    stop at all. Anything smaller is the same price after quantization.
  - A new stop is never placed inside `NOISE_BAND_ATR_MULTIPLE` ATRs of
    current price. That is the same floor the discretionary path already
    clamps to, applied at the source instead of after the fact.
  - Missing data yields no proposal, never a guess.

**Stage 2 of short selling (shorts-safe).** Every rule above was written and
tested against a long-only book, where "trail" only ever means "raise the
stop". A short's protective stop is a BUY stop ABOVE the market, and every
one of these rules mirrors through a price-axis flip: ratchet DOWN instead of
UP, trail under successive LOWER highs instead of higher lows, chandelier off
the LOWEST low instead of the highest high, noise band and ratchet-minimum
measured on the other side of the stop. `qty` supplies only the SIDE:
negative is a short. No order path
in this repo can open a short yet, so `qty` defaults to +1.0 and every
existing call site — which only ever knows about longs — is unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "TrailProposal",
    "TrailEvaluation",
    "compute_trailing_stop",
    "evaluate_trailing_stop",
    "MIN_RATCHET_TICKS",
    "venue_tick",
    "min_ratchet_floor",
    "CHANDELIER_ATR_MULTIPLE",
    "NOISE_BAND_ATR_MULTIPLE",
    "PIVOT_WINDOW",
    "RANGE_BREAKEVEN_R_MULTIPLE",
    "RANGE_SECOND_RATCHET_TRIGGER_R",
    "RANGE_SECOND_RATCHET_LOCK_R",
]


#: Chandelier distance below the highest high since entry, used only where
#: structure is unclear. 3x ATR is the conventional setting and is deliberately
#: loose: a fallback that strangles the trade defeats the purpose of Type B.
CHANDELIER_ATR_MULTIPLE = 3.0

#: A stop closer than this many ATRs to current price sits inside one ordinary
#: day's range. Same value the TRAIL_STOP clamp in `pipeline.py` uses.
NOISE_BAND_ATR_MULTIPLE = 1.25

#: Bars either side of a candidate swing low, for THIS module only.
#:
#: **This is a convention with no derivation, and it is deliberately not
#: reconciled with `src/data/levels.py`.** Until 2026-09-13 the comment here
#: claimed it "matches `src/data/levels.py`'s pivot detection so 'a higher
#: low' means the same thing in both places". That was false the day it was
#: written: `src/data/levels.py::PIVOT_WINDOW` is 5, this is 3, and they have
#: never been equal.
#:
#: What the desk adopted is the ARCHETYPE, not anyone's constant: a swing
#: point is a bar that strictly dominates N bars on BOTH sides, and the
#: right-hand arm means the verdict arrives N bars late. That is the Williams
#: fractal / pivot-high-low construction as implemented by TA-Lib's FRACTAL
#: (`optInLeftBars` / `optInRightBars`, https://ta-lib.org/functions/fractal.html)
#: and by Pine's `ta.pivothigh` / `ta.pivotlow`.
#:
#: The NUMBER is not adopted, because no source derives one:
#:   * TA-Lib's FRACTAL defaults both arms to 2 and states no rationale; the
#:     page only notes "Bill Williams' original is the symmetric five-candle
#:     case" — i.e. 2 either side, which is neither 3 nor 5.
#:   * MetaTrader 5's own fractal documentation defines the pattern as "at
#:     least five successive bars ... and two lower HIGHs on both sides" and
#:     gives no reason for the count
#:     (https://www.metatrader5.com/en/terminal/help/indicators/bw_indicators/fractals).
#:   * fxssi records that Williams did NOT require five, and that five became
#:     standard "due to its inclusion in the list of standard indicators of
#:     MetaTrader 4 trading terminal" (https://fxssi.com/bill-williams-fractals)
#:     — i.e. the popular number is a default that shipped, not a measurement.
#:   * LuxAlgo's swing high/low reference states outright: "There is no
#:     universally best setting ... different settings produce genuinely
#:     different structure from the same chart"
#:     (https://www.luxalgo.com/library/concept/swing-high-low/).
#: Reviewed 2026-09-13; ~all of this literature is assertion rather than
#: measurement, and no source fetched offered a derivation for any window.
#:
#: Second pass the same day (docs/WORK.md item 55) added the ACADEMIC source
#: the vendor docs above are not: Tsinaslanidis, "Technical Trading
#: Strategies, Pattern Recognition and Financial Risk Management" (PhD
#: thesis, University of Macedonia, 2012, §4.3). It defines the identical
#: symmetric construction and, uniquely, MEASURES its sensitivity — but at
#: rolling windows of 50/100/150 days total, i.e. 25/50/75 bars either side,
#: reporting the results "robust to any different parameterization" over
#: that range. That range does not contain 3, so it neither supports nor
#: refutes this constant. It is evidence that the object is insensitive at
#: swing scale, and silence at the scale a stop is actually placed on.
#:
#: So 3 stays, labelled honestly, rather than being changed to a number with
#: no better claim on being right. Changing it would be picking a number.
#:
#: Sole consumers: `_swing_lows` / `_swing_highs` in this file, reached only
#: through `compute_trailing_stop`. Callers of that are
#: `src/pipeline.py::_trail_open_positions` and
#: `src/backtest/engine.py`. Nothing downstream compares a pivot found here
#: against a level from `src/data/levels.py`, so the two windows disagreeing
#: is currently harmless — see `src/data/levels.py::PIVOT_WINDOW` for the
#: other half of this note and `tests/test_pivot_window_independence.py` for
#: the test that pins it.
PIVOT_WINDOW = 3
#: The fewest bars SINCE ENTRY the STRUCTURE leg needs for a reading. Not a
#: chosen number: `_swing_lows` needs `window` bars on both sides of a low
#: before it will confirm one, so this is exactly that window. It gates the
#: structure leg ONLY. The chandelier is not held to it (2026-10-13): it needs
#: ONE completed daily bar since entry and reads the highest completed high
#: (never the fill or a forming bar) - 3 x ATR. Gating it at 7 meant it first
#: read in session 8.
MIN_BARS_FOR_A_READING = PIVOT_WINDOW * 2 + 1

#: How many initial-risk-units (R) of profit a Type A / range trade must
#: bank before its stop ratchets to breakeven. 1.0 is the standard,
#: widely-cited default (Van Tharp's R-multiple framework; Elder's Triple
#: Screen) — not a backtested or desk-specific tuning, a conventional
#: starting point for "this trade has proven itself enough to stop risking
#: the full original bet." R itself is `abs(entry - initial_stop)`, i.e. the
#: risk actually taken at entry, never the (possibly already-ratcheted)
#: live stop.
RANGE_BREAKEVEN_R_MULTIPLE = 1.0

#: Item 142 — the SECOND range ratchet, stacked ON TOP of the +1R breakeven
#: step above and BELOW the target. Once a range / Type A trade reaches +2R of
#: open profit, its stop is moved up to +1R, locking in one initial-risk-unit
#: of gain. This REDUCES the give-back to a +1R floor once +2R is tagged; it
#: does NOT close the give-back gap — between +1R (the lock) and the target the
#: stop is pinned at +1R and no structural trail runs, so a run to +5R then a
#: reversal still gives back to +1R. The residual give-back between +1R and
#: target is unchanged. Both multiples are OWNER APPETITE, ratified by Rex on
#: 2026-09-25 for item 142 ("let's try your recommendation"): they are CHOSEN
#: appetite multiples — 2R is not computed from the +1R breakeven unit by any
#: formula, and the 1R lock equals the breakeven unit only by coincidence of
#: appetite — so both are recorded as `arbitrary`, not `derived` and not
#: sourced. Ratified is not sourced: it records WHO chose the number, not a
#: measurement behind it. R itself is `abs(entry - initial_stop)`, the same
#: denominator the breakeven ratchet uses; never the live (already ratcheted)
#: stop.
RANGE_SECOND_RATCHET_TRIGGER_R = 2.0
RANGE_SECOND_RATCHET_LOCK_R = 1.0


def _finite(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class TrailProposal:
    """A deterministic proposal to raise one position's stop."""

    symbol: str
    new_stop: float
    previous_stop: float
    source: str  # "structure" | "chandelier"
    reason: str


#: Why an evaluation ended the way it did. One code per exit of
#: `evaluate_trailing_stop`, so "why has this stop never trailed?" has an
#: answer on file (`src/execution/exit_path_records.py`). Names only — no
#: code path branches on them.
TRAIL_CODE_TRAILED = "trailed"
TRAIL_CODE_BAD_PRICE_INPUT = "no_usable_entry_or_price"
TRAIL_CODE_NO_LIVE_STOP = "no_live_stop"
TRAIL_CODE_RANGE_NO_INITIAL_STOP = "range_below_target_no_entry_stop_on_record"
TRAIL_CODE_RANGE_ZERO_RISK = "range_below_target_entry_equals_entry_stop"
TRAIL_CODE_RANGE_BELOW_1R = "range_below_target_not_yet_1r"
TRAIL_CODE_RANGE_ALREADY_BREAKEVEN = "range_below_target_stop_already_at_breakeven"
TRAIL_CODE_RANGE_BREAKEVEN_OFF_SIDE = "range_breakeven_not_between_stop_and_price"
TRAIL_CODE_RANGE_BELOW_2R = "range_below_target_not_yet_2r"
TRAIL_CODE_RANGE_SECOND_OFF_SIDE = "range_second_ratchet_lock_not_between_stop_and_price"
TRAIL_CODE_NO_CANDIDATE = "no_structure_and_no_usable_chandelier"
TRAIL_CODE_BELOW_MIN_RATCHET = "move_smaller_than_min_ratchet"
TRAIL_CODE_INSIDE_NOISE_BAND = "inside_noise_band"
TRAIL_CODE_ROUNDED_OFF_SIDE = "rounded_candidate_not_between_stop_and_price"
#: Too few bars since entry for the STRUCTURE leg to be read
#: (`MIN_BARS_FOR_A_READING`), and the ungated chandelier offered no usable
#: level either. Recorded only when no candidate exists at all.
TRAIL_CODE_TOO_FEW_BARS = "too_few_bars_since_entry"


@dataclass(frozen=True)
class TrailEvaluation:
    """The proposal (or None) AND the code naming why — so a caller can
    record a no-trail instead of discarding it."""

    proposal: TrailProposal | None
    code: str
    #: Item 212 follow-up: on a Type A (range) name BOTH legs can now have an
    #: opinion. When the R-ratchet leg supplies the answer, this carries the
    #: STRUCTURAL leg's own code so its refusal reason stays on the record
    #: instead of being silently discarded. `None` means the structural leg
    #: was not consulted (Type B, or an early return before the candidates).
    structural_code: str | None = None


#: Where each piece of the arithmetic lives now. `src/risk/trailing.py` keeps
#: the contract (the proposal/evaluation types, the TRAIL_CODE_* names and
#: every ratified number, each pinned here by the number ledger and by
#: `tests/test_pivot_window_independence.py`); the three parts below hold the
#: bodies, moved verbatim, each importable and exercisable on its own
#: (`tests/test_trailing_parts_boundary.py`). Resolved lazily so that a part
#: importing a constant from here never meets a half-initialised module, and
#: so `from src.risk.trailing import X` and `patch("src.risk.trailing.X")`
#: keep working for every existing caller. ONE mirror block, never two.
_PART_OF = {
    "MIN_RATCHET_TICKS": "src.risk.trail_tick",
    "venue_tick": "src.risk.trail_tick",
    "min_ratchet_floor": "src.risk.trail_tick",
    "_swing_lows": "src.risk.trail_structure",
    "_swing_highs": "src.risk.trail_structure",
    "_structural_pivot": "src.risk.trail_structure",
    "_range_breakeven_ratchet": "src.risk.trail_range_ratchet",
    "_range_second_ratchet": "src.risk.trail_range_ratchet",
    "compute_trailing_stop": "src.risk.trail_evaluate",
    "evaluate_trailing_stop": "src.risk.trail_evaluate",
}


def __getattr__(name: str):
    try:
        module = _PART_OF[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    return getattr(importlib.import_module(module), name)
