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

#: The fewest bars SINCE ENTRY the STRUCTURE leg needs for a reading. Not a
#: chosen number: swing points come from `src.data.levels.turning_points`, the
#: one zigzag detector (owner ruling, Rex 2026-10-15 — a turning point comes
#: from the stock's own movement, not a bar count; the old `PIVOT_WINDOW = 3`
#: is deleted). Its reversal threshold is the stock's own ATR(14), which first
#: exists on bar 14, and a candidate there is confirmed at the earliest by the
#: next bar — so this is `src.data.levels.MIN_SCAN_BARS`, read, not copied.
#: It gates the structure leg ONLY. The chandelier is not held to it
#: (2026-10-13): it needs ONE completed daily bar dated after the entry day and
#: reads the highest such high (never the fill, the entry-day bar or a forming
#: bar) - 3 x the current 14-day ATR.
from src.data.levels import MIN_SCAN_BARS as MIN_BARS_FOR_A_READING  # noqa: E402

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
