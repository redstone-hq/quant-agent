"""Portfolio Constructor — turns PM target-state into concrete orders.

Phase 2 of the architecture work. Previously the LLM (Portfolio Manager)
emitted TradeDecision objects directly, including entry_price / stop_loss /
take_profit. That put the LLM dangerously close to the execution layer:
- fat-finger-protection patches
- vol-adjusted sizing patches
- stop-limit buffer patches
- sub-penny quantize patches
...were all band-aids for "LLM output an execution detail it shouldn't own."

Now PM emits TargetPosition (target_weight_pct, conviction, thesis,
invalid_if) and this module derives the actual orders from:
- Target state
- Current positions (broker truth)
- TA's ATR + suggested stop (for stop distance)
- Broker's live price (for entry price)
- Total equity + cash (for sizing)

The constructor is deterministic and unit-testable. LLM creativity is
confined to intent; math is code.
"""

from __future__ import annotations

import json
import math
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.data.levels import (
    describe_stop_level_basis,
    COVERAGE_UNKNOWN,
    FAULT_NO_ANALYSIS,
    FAULT_NO_ENTRY,
    FAULT_NO_PRICE,
    TargetDerivation,
    derive_structural_target,
    level_zone_halfwidth,
    stop_rests_on_level,
    touch_probability,
)
from src.data.technical import LONGEST_INDICATOR_WINDOW
from src.models import (
    Position,
    TargetPosition,
    TechAnalysisResult,
    TradeDecision,
    reward_to_risk,
    stated_soft_exit,
)
from src.risk.constants import (
    reward_risk_floor_applies,
    risk_budget_allocation_pct,
)

logger = logging.getLogger(
    __name__.rpartition(".")[0]
)  # "src.portfolio_constructor": the same logger as before the split

#: BOARD ITEM 222 — the one sentence that answers "how much of one stock may
#: this desk hold?". Three limits used to claim to answer it; only TWO are
#: independent, and this constant is the canonical written form of which one
#: binds. The portfolio manager's sector-preview prompt points here.
#:
#: THE THIRD LIMIT IS NOT A LIMIT. `alloc_cap_by_risk` in `_build_buy` /
#: `_build_short` below reads as a separate notional cap, but it is computed
#: as `risk_budget_pct x entry / |entry - stop|` — it is the SAME 5%
#: per-position risk envelope, re-expressed in notional units so it can be
#: compared against the notional ceiling. It cannot bind on an order the
#: envelope would not; it is a unit conversion, not a bound.
#:
#: So two bounds remain, both in the same unit (percent of total account
#: equity, raw notional, before the gross multiplier) against the same
#: denominator (`total_value`), and they cross exactly once — at a stop
#: distance of `risk_budget_pct / max_position_pct x 100` percent of entry
#: price. Wider stop: the risk envelope binds. Tighter stop: the ceiling
#: binds. `single_name_crossover_stop_pct()` below computes that crossover
#: from the live config rather than restating it.
#:
#: MEASURED against the production order record, 2026-09-02..2026-09-30
#: (38 constructor entry orders carrying a stop; 8 cash-sweep ETF buys are
#: not constructor-sized and carry no stop, so they are unattributable):
#: the single-name notional ceiling bound 4 of 38, every one of them at a
#: stop distance tighter than the crossover. The risk envelope bound 0 of 38
#: — the largest risk any order requested was 3.0%, under the 5% envelope —
#: and its notional image bound 0 of 38. The `trade_refusals` table is
#: EMPTY, so no refusal could be attributed to any limit at all.
SINGLE_NAME_BINDING_SENTENCE = (
    "The most the desk may hold in one name is 65% of total account equity "
    "in notional terms (raw position value / equity, before the gross "
    "multiplier, less whatever is already held in that name); the 5% "
    "per-position risk envelope and the constructor's notional risk cap are "
    "one limit expressed in two units, not two limits, and that limit binds "
    "before the 65% ceiling only when the stop sits further than 7.69% of "
    "entry price away."
)


def single_name_crossover_stop_pct(risk_budget_pct: float, max_position_pct: float) -> float:
    """Stop distance, in percent of entry price, where the two bounds cross.

    Below it the notional ceiling binds; above it the risk envelope binds.
    Derived, not chosen: `risk_budget x entry/|entry-stop| == max_position`
    rearranges to exactly this. Item 222.
    """
    if max_position_pct <= 0:
        raise ValueError("max_position_pct must be positive")
    return risk_budget_pct / max_position_pct * 100.0


# Python-stamped named trigger for a funding-trim / size-down. Checkable
# from the same live-book weight (and risk, when passed) the constructor
# used. PM thesis free text explains; it cannot create the sell. Do NOT
# add this phrase to pipeline._HARD_TRIGGER_KEYWORDS — a midday LLM
# could emit it with nothing behind it (correlation-breach lesson,
# 2026-09-13). Descriptive categorisation in storage is separate.
MECHANICAL_SIZE_DOWN_TRIGGER = "mechanical size-down vs live book"


def format_mechanical_size_down_reason(
    *,
    current_weight_pct: float,
    target_weight_pct: float,
    current_risk_pct: float | None = None,
    target_risk_pct: float | None = None,
) -> str:
    """Named trigger stamped from live-book numbers, never from PM prose."""
    parts = [f"{MECHANICAL_SIZE_DOWN_TRIGGER}: weight {current_weight_pct:.2f}% → {target_weight_pct:.2f}%"]
    if (
        current_risk_pct is not None
        and target_risk_pct is not None
        and math.isfinite(current_risk_pct)
        and math.isfinite(target_risk_pct)
    ):
        parts.append(f"risk {current_risk_pct:.2f}% → {target_risk_pct:.2f}%")
    return "; ".join(parts)


def _size_down_checkable(
    current_pct: float,
    target_pct: float,
    *,
    long_side: bool,
) -> bool:
    if not math.isfinite(current_pct) or not math.isfinite(target_pct):
        return False
    if long_side:
        return current_pct > 0 and target_pct < current_pct
    return current_pct < 0 and target_pct > current_pct


def _lookup_existing_risk(
    existing_risk_pct: dict[str, float] | None,
    symbol: str,
) -> float | None:
    if not existing_risk_pct:
        return None
    if symbol in existing_risk_pct:
        val = existing_risk_pct[symbol]
    else:
        val = existing_risk_pct.get(str(symbol).upper())
    if val is None:
        return None
    try:
        parsed = float(val)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _named_reduction_trigger(
    target: TargetPosition,
    current_pct: float,
    target_pct: float,
    *,
    long_side: bool,
    current_risk_pct: float | None = None,
) -> tuple[str, str] | None:
    """(reasoning, falsifier) for a SELL/COVER, or None if no desk warrant.

    A stated thesis_invalid_if is a checkable soft-exit warrant; reasoning
    then keeps the existing thesis + parenthetical so a Python-authored
    close reason already on the target is preserved. With a blank
    falsifier, PM thesis cannot create the sell: only a live-book
    size-down (weight, and risk when known) is stamped as the named
    trigger. Never invents a falsifier.
    """
    falsifier = stated_soft_exit(target.thesis_invalid_if)
    thesis = (target.thesis or "").strip()
    if falsifier:
        reasoning = thesis
        reasoning += f" (thesis_invalid_if: {falsifier})"
        return reasoning, falsifier
    if not _size_down_checkable(current_pct, target_pct, long_side=long_side):
        logger.warning(
            "Constructor: %s %s skipped — blank thesis_invalid_if and "
            "size-down vs live book is not checkable (current_pct=%s "
            "target_pct=%s); PM thesis cannot create the sell",
            "SELL" if long_side else "COVER",
            target.symbol,
            current_pct,
            target_pct,
        )
        return None
    trigger = format_mechanical_size_down_reason(
        current_weight_pct=current_pct,
        target_weight_pct=target_pct,
        current_risk_pct=current_risk_pct,
        target_risk_pct=target.risk_allocation_pct,
    )
    if thesis:
        trigger = f"{trigger} (PM explanation: {thesis[:200]})"
    return trigger, ""


class _DropReasonCapture(logging.Handler):
    """Funnel-queue item 2 (2026-09-03): the census
    (`scripts/blocked_proposals_census.py`) found ~19 PM targets per
    reproduction window that never became a `proposed_order` row and
    carried NO other evidence either — classified `no_order_built`. Root
    cause, confirmed against `data/resets/20260902T181859Z/quant_agent.db`
    plus the production `quant_agent.log*` files: the constructor's OWN
    reason for dropping a target (`_resolve_entry_and_stop`,
    `_derive_target`, the reward:risk floor, the sector-crowding refusal,
    ~20 distinct call sites below) has ALWAYS been logger-only — the
    module docstring on `blocked_proposals_census.py` already documents
    this as something "only ever logger.info/logger.warning text — never
    persisted to a table." Every one of the reproduced cases DID have a
    real log line explaining it (in journalctl for a systemd-timer run, or
    in the rotated `quant_agent.log*` file for a manually-triggered one) —
    so "13 with no record anywhere" overstated it; the record existed, just
    not in the database the census script reads and not always in the one
    log sink an operator thinks to check first.

    Rather than thread a reason string through every one of those ~20
    return points (a much larger, riskier change to a stateless,
    heavily-tested sizing function), this captures the SAME log lines the
    module already emits, via a handler scoped to exactly one
    `construct_orders` call, and exposes them on
    `PortfolioConstructor.last_drop_reasons` so the caller
    (`pipeline_stages.DecisionStage`) can persist a terminal per-symbol
    evidence row for every constructor-dropped target — never nothing —
    using the constructor's own real words instead of a generic label.
    """

    _SYMBOL = re.compile(
        r"Constructor:\s*(?:BUY|SHORT|SELL|COVER)?\s*"
        r"([A-Za-z][A-Za-z0-9.\-]{0,9})\s+"
        r"(?:rejected|refused|skipped)\b"
    )

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.reasons: dict[str, list[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — a capture side-channel must never raise
            return
        m = self._SYMBOL.search(msg)
        if not m:
            return
        symbol = m.group(1).strip().upper()
        self.reasons.setdefault(symbol, []).append(msg)


# ---------------------------------------------------------------------------
# Named outcomes for the stop rule (spec §12.1, 2026-09-01)
# ---------------------------------------------------------------------------
# Same discipline `src/data/levels.py::derive_structural_target` established
# in §10.4: every path that MOVES or REFUSES a stop says which rule fired, by
# name. "No trade" without a reason is what let the original defect — the ATR
# floor silently overwriting a real structural level — survive unnoticed
# through 38 signals and zero trades on 2026-09-01. The codes are for logs and
# tests; the message beside each one is written for a reader who does not know
# the code.
STOP_RULE_LEVEL_HONOURED = "stop_honoured_at_computed_level"
STOP_RULE_ABSOLUTE_FLOOR = "stop_widened_to_absolute_atr_floor"
STOP_RULE_ATR_BAND = "stop_widened_to_atr_noise_band"
# The two paths that leave the stop exactly where structure put it. Both
# existed before 2026-09-02 as unnamed early returns, and that anonymity is
# precisely how they escaped the reward:risk floor for two weeks — see
# `_widen_stop_past_noise`. Named now, for the same reason every other
# outcome is named.
STOP_RULE_OUTSIDE_BAND = "stop_kept_already_outside_atr_band"
#: RETIRED as a shipping rule (board item 80, 2026-09-25, reworked). This named
#: the one branch that honoured a typed stop with NO ATR to verify it against,
#: letting an unverifiable distance size a position. That branch now DERIVES the
#: stop from price structure and HOLDS (`STOP_RULE_STRUCTURAL_NO_ATR` /
#: `STOP_RULE_PRIOR_BAR_NO_ATR`), refusing only when no structure is readable,
#: so no shipping stop carries this rule any more. Kept defined only so the
#: `_GEOMETRY_REFUSAL_BY_RULE` lookup and any historical record referencing the
#: string still resolve.
STOP_RULE_NO_VOLATILITY = "stop_kept_no_atr_reading"
STOP_REFUSAL_WRONG_SIDE = "stop_on_wrong_side_of_entry"
#: The unbacked-stop fallback placed the stop under the SIGNAL BAR's low
#: (above its high for a short) because that sat further from entry than
#: the ATR noise band did. Kristjan Kullamägi's own placement for a stop
#: with no level under it — "the low of the day" — read from the last
#: completed bar the analyst judged (`TechAnalysisResult.signal_bar_low` /
#: `signal_bar_high`, Python-set from the same bars as the levels). Rare by
#: construction: it only wins when the signal bar itself spans more than
#: the noise band, i.e. a climactic bar.
STOP_RULE_SIGNAL_BAR = "stop_placed_past_signal_bar"
#: No-ATR structural fallback (owner ruling 2026-09-25, board item 80).
#: The protective stop was READ from price structure because there was no
#: volatility reading to place one from: `STRUCTURAL_NO_ATR` sits one
#: buffer beyond the nearest VERIFIED computed level on the protective
#: side of entry (a swing-low / structure stop); `PRIOR_BAR_NO_ATR` sits
#: one buffer beyond the last completed bar's far edge (a prior-bar low
#: stop) when no computed level qualifies. Both HOLD the position -- a
#: missing ATR is never a reason to skip protection.
STOP_RULE_STRUCTURAL_NO_ATR = "stop_read_from_structure_no_atr"
STOP_RULE_PRIOR_BAR_NO_ATR = "stop_read_from_prior_bar_no_atr"
#: RETIRED as a live refusal (board item 56, route (c), 2026-09-26). This
#: named the stop-WIDTH gate: whatever placed the stop, a width past
#: `horizon_reach` (ATR x sqrt(horizon) x a 1.5 multiple nothing derived)
#: refused the trade outright. It is DELETED, and the deletion is measured,
#: not argued: across 648 sized stops recorded in production between
#: 2026-09-13 and 2026-09-26 (`quant_agent.log`, the touch-probability
#: reading below) it refused ZERO trades, and the widest stop it ever saw
#: sat at 1.29 x ATR x sqrt(H) against its 1.5 cap. It could not have fired
#: on the desk's own fallback stop at any horizon of three sessions or more
#: (2.5 ATR vs 1.5 x sqrt(3) = 2.60 ATR), and every horizon the desk has
#: ever stated was 6 sessions or longer. What it claimed to protect --
#: "a size computed off a distance price will not travel is fiction" -- is
#: already answered, in the direction published practice prescribes, by the
#: ratified sizing invariant (§2.1, `_plan_risk_targets`: same dollars of
#: risk, wider stop, fewer shares) and, at the extreme, by
#: `STOP_REFUSAL_SIZED_TO_ZERO`. The threshold it needed was never read off
#: anything: no published work fixes the touch probability below which a
#: stop stops being a stop. The READING survives the gate and is stamped on
#: every sized stop (`src.data.levels.touch_probability`), because that is
#: the evidence that could one day answer the question.
#:
#: Kept defined as a greppable key: it appears in pre-deletion production
#: records and in docs/INCIDENT_HISTORY.md, and the blocked-proposal census
#: (`scripts/blocked_proposals_census.py`) must still be able to name it.
#: NOTHING EMITS IT ANY MORE, and `tests/test_stop_width_gate.py` fails if
#: anything starts to.
STOP_REFUSAL_WIDER_THAN_REACH = "stop_wider_than_instrument_reach"
#: RETIRED as a constructor refusal (board item 180, owner ruling 2026-09-25).
#: This named the young-listing refusal that fired on a bar COUNT: fewer
#: completed sessions than `LONGEST_INDICATOR_WINDOW` (200) and the trade was
#: turned away, however readable its stop and levels were. The owner dropped
#: the count gate: a young listing is TRADEABLE when a defensible protective
#: stop is readable from whatever bars exist and the seats clear it, and is
#: refused ONLY when no stop can be read from the instrument -- the existing
#: stop-readability rule (`_derive_structural_stop_no_atr` /
#: `STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY`, board item 80) is now the
#: real gate, catching same-day-IPO / near-zero-bar names on what the trade
#: needs rather than on a calendar count. Indicators already degrade to the
#: bars that exist (`compute_indicators`), so a short history simply leaves
#: `ma_200` = None, which the exit guard treats as UNPARSEABLE for any
#: `thesis_invalid_if` that references it. The string is deliberately NOT
#: reused here; the universe screen and the transient-admission lane keep
#: their own separate `insufficient_history` gates (out of this item's scope).
#: retired board item 49 (`docs/INCIDENT_HISTORY.md`, 2026-09-14). The portfolio risk budget was spent
#: before this candidate's turn came round. NOT a judgement about the idea:
#: it passed every gate, and on a day with fewer competing names it would
#: have been bought. The only reason it produced no order is that better-
#: ranked names took the 25% ceiling first.
#:
#: This exists because the budget denial was the ONE constructor drop path
#: with no durable per-symbol reason at all: it logged
#: "Constructor: X produces no order — risk budget granted 0% ...", which
#: `_DropReasonCapture._SYMBOL` does not match (it requires
#: rejected|refused|skipped after the symbol), so every budget-rationed name
#: reached `_record_constructor_drops` as the generic `constructor_dropped`
#: with detail "no matching constructor log line captured". Once the budget
#: actually binds on a normal day — which is what item 49 is about — that is
#: the largest silent bucket on the sheet. Verified on this branch before the
#: fix by running the capture's own regex against the real message.
STOP_REFUSAL_BUDGET_EXHAUSTED = "risk_budget_exhausted"
#: Defect 2026-10-09: some holding's risk is UNKNOWN (no usable price, or
#: the book could not be read at all), so the 25% total-risk ceiling cannot
#: be checked. Every risk-ADDING order is refused under this code until the
#: holding is read; closes, exits and stops are never blocked by it.
STOP_REFUSAL_BOOK_RISK_UNKNOWN = "book_risk_unknown"
#: Defect 2026-10-09: a dollar-only (weight, no risk %) target that would
#: add exposure, for which no stop could be resolved, so it cannot be
#: converted to risk and charged against the 25% ceiling. Refused by name.
STOP_REFUSAL_DOLLAR_TARGET_NO_STOP = "dollar_target_no_stop"
#: Board item 10 (2026-09-14): the same defect item 49 fixed for the
#: PORTFOLIO-level budget allocator, found again by statically running
#: `_DropReasonCapture._SYMBOL` against every other constructor drop message
#: in the module (no live data needed — the regex is deterministic and the
#: messages are compile-time strings). §9.4's OTHER refusal path — the net
#: independent source score landing at or below zero — used
#: the identical "Constructor: X produces no order — ..." phrasing item 49
#: already found the regex does not match (it requires rejected|refused|
#: skipped directly after the symbol; "produces" is neither), and reached
#: `_record_constructor_drops` the same generic way. Fixed the same way:
#: `_note_refusal` files the code as data instead of relying on the log
#: scrape ever catching up to a sentence.
STOP_REFUSAL_AGREEMENT_NET = "agreement_net_at_or_below_zero"
#: Board item 10 (2026-09-14). `_build_buy`/`_build_short` already LOG when
#: the risk-budget-per-trade cap or the single-name ceiling
#: shrinks a request ("alloc capped by risk budget" / "... by the single-
#: name ceiling"), but neither message contains rejected/refused/skipped, so
#: the regex never matches them. When one of those caps (or the sector dial
#: above them) leaves nothing to round to above zero, the order silently
#: never ships — and unlike the ATR/no-stop path a few lines up, there is no
#: SECOND log line for the same symbol that happens to match: `_build_buy`
#: just returns `None`. Filed directly from `cap_note`, which already
#: carries the deterministic provenance of whichever cap(s) bound.
STOP_REFUSAL_SIZED_TO_ZERO = "position_sized_to_zero"
#: Board item 10 (2026-09-14). `apply_gross_ceiling` (`src/risk/rules.py`)
#: refuses a BUY/SHORT outright — equity unusable, headroom below the
#: minimum-order floor, or a granted slice that rounds to nothing — and logs
#: through its OWN `Constructor: %s`-wrapped note (`portfolio_constructor.py`
#: relays `outcome.notes` verbatim at `logger.warning("Constructor: %s",
#: note)`). Every one of those notes reads "Constructor: max_gross_exposure:
#: SYMBOL refused — ..." — the rule name sits BETWEEN "Constructor:" and the
#: symbol, which `_DropReasonCapture._SYMBOL` requires to follow immediately
#: (optionally through one of BUY/SHORT/SELL/COVER only). Every gross-ceiling
#: block was therefore invisible to the regex. `GrossCeilingOutcome.
#: blocked_detail` now carries the per-symbol reason text out of
#: `apply_gross_ceiling` so the constructor can file it with `_note_refusal`
#: directly, the same precedent as every other code in this block.
STOP_REFUSAL_GROSS_EXPOSURE_CEILING = "gross_exposure_ceiling_refused"
#: Owner rule 2026-08-27 (owner-ruled): risk per new position is 0.5-5% of
#: equity, and below 0.5% the desk does not trade. The allocator already
#: denies a GRANT under the floor (`src/risk/budget.py`), but every shrink
#: that runs after it -- the single-name ceiling, the gross-exposure ceiling
#: -- could cut an entry below it and still let it out. This code names the
#: refusal the constructor files when it re-checks the FINISHED order list:
#: any BUY/SHORT whose resulting position would risk less than
#: `min_risk_pct` of equity at its own stop is dropped. Closes, trims and
#: covers are never subject to it.
STOP_REFUSAL_BELOW_OWNER_MIN_RISK = "below_owner_min_risk"
#: RETIRED (owner ruling 2026-09-30, board item 183). This named the delta
#: loop's churn filter: `min_trade_weight_delta`, a picked 0.5%-of-book
#: floor, silently `continue`d past a delta below it — a brand-new position
#: too small to bother with left no `TradeDecision` row and no log line at
#: all, and an existing one got a HOLD instead of the trade it asked for.
#: The owner ruled the desk gets autonomy to nudge a position whenever its
#: own reasoning calls for it; a flat unsourced percentage that silently
#: overrides that judgement is gone, not resized. See
#: `config/number_ledger.yaml`'s now-deleted entry for the measurement that
#: already showed the cost side could not justify a floor this size. Kept
#: defined as a greppable key only because it appears in
#: `tests/test_owner_message_cannot_mislead.py` / `tests/test_trader_feed.py`
#: fixture data exercising the generic refusal-reporting path, which is
#: unaffected by this constant's retirement — NOTHING EMITS IT ANY MORE.
CONSTRUCTOR_NO_ACTION_BELOW_MIN_DELTA = "delta_below_min_trade_weight"
#: The two reasons that REPLACE it — both read off the request itself, and
#: neither is a threshold. Deleting `min_trade_weight_delta` deleted the only
#: reason this loop ever filed, and two things still reach its no-op arm
#: without producing an order. `tests/test_every_drop_path_files_a_reason.py`
#: fails if either goes back to being silent.
#:
#: 1. The desk named a candidate and asked for a weight of ZERO on a name it
#:    holds none of. Measured in the production archive this repo already
#:    ships (`tests/fixtures/constructor_drop_paths_archive.json`): all 8 of
#:    the candidates left anonymous by the floor's removal are this exact
#:    case — `target_weight_pct == 0.0` against a zero holding, not a small
#:    request. The old message called these "smaller than the desk's
#:    minimum", which was never true of a zero. Nothing was judged wrong
#:    with the idea; the desk asked for no position.
CONSTRUCTOR_TARGET_WEIGHT_ZERO_NOTHING_HELD = "target_weight_zero_nothing_held"
#: 2. A held SHORT whose target equals what is already short. The long side
#:    of this arm emits a HOLD row and the symbol survives; the short side
#:    emits none, because HOLD's audit bookkeeping in this stage is long-only
#:    (pre-existing, unchanged here), so without this the position silently
#:    produced nothing. This is "already where the desk wants it", NOT a
#:    refusal of the idea — the prose says so.
CONSTRUCTOR_SHORT_ALREADY_AT_TARGET = "short_already_at_target_weight"
#: 3. Owner ruling 2026-10-09 (docs/OUTCOME.md): a HELD position is kept at
#:    its size or exited whole — never partly sold by the planner, whether
#:    the desk's own risk number came down or the budget granted less. Such
#:    a cut is refused by this code and the position is held unchanged. Only
#:    the gross-ceiling de-lever (its own order path) partly cuts a holding.
CONSTRUCTOR_HELD_PARTIAL_TRIM_REFUSED = "held_partial_trim_refused"
#: NOT a replacement for `min_trade_weight_delta`. `signed_target` and
#: `current_pct` are both floats built from independent divisions (a live
#: price against total_value vs. a model-typed percent), so a delta the
#: desk did not actually ask for — "hold exactly what is held" — can land a
#: few ULPs off zero rather than bit-exact. This is the same role
#: `_FRACTIONAL_QTY_EPSILON` already plays for share quantities elsewhere in
#: this codebase (`src/execution/broker.py`): it recognises floating-point
#: representation noise as "no request", never as a size the desk judged
#: too small. Nine orders of magnitude below the finest weight either a
#: model or a human types, so it is not a floor on any real nudge.
_NO_REAL_WEIGHT_DELTA_PCT = 1e-9
#: 2026-09-17 (intra_check-44594a05). A risk-based TRIM of a held name this
#: session did not analyse is sized from the position's live broker stop
#: (see `_held_trim_entry_and_stop`). When there is no usable live stop the
#: trim cannot be sized, and the position is left unchanged. That is NOT a
#: market-data fault — the price feed is fine — so it is filed under its own
#: name instead of `analysis_missing`, which pages the owner to check the feed.
TRIM_REFUSAL_NO_USABLE_LIVE_STOP = "trim_without_analysis_has_no_usable_live_stop"
#: Board item 10, second pass (2026-09-14). The five fixes above closed every
#: path that reached `_record_constructor_drops` as the literal "no matching
#: constructor log line captured". They did NOT make every drop path
#: MACHINE-READABLE: eight more sites still ended a candidate with nothing but
#: a sentence in a log line, recovered by regex into a generic
#: `constructor_dropped` row whose `reason` column is the same for all of
#: them. One of those eight — no typed stop AND no ATR to read one from — the
#: regex misses outright ("Constructor: BUY SYM has no stop ...", and the
#: pattern requires rejected|refused|skipped straight after the symbol), so
#: the "five is the complete set" claim was wrong on its own terms. Codes
#: below, one per site, filed with `_note_refusal` exactly like the rest. The
#: rule this whole item is about, stated once: a candidate is never dropped
#: without a durable, per-symbol, machine-readable reason.
#:
#: The stop the analyst typed, or the entry it is measured from, is not a
#: finite number. Not a trade judgement and not a data fault either — an
#: input arrived and is nonsense, so it is refused rather than compared.
STOP_REFUSAL_STOP_NOT_FINITE = "stop_price_not_finite"
STOP_REFUSAL_ENTRY_NOT_FINITE = "entry_price_not_finite"
#: RETIRED as an emitted refusal (board item 80, 2026-09-25, reworked): nothing
#: typed and no ATR no longer refuses on sight -- the branch first tries to
#: DERIVE a stop from price structure and only refuses (with
#: `STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY`) when none is readable. Kept
#: defined as a greppable key: it appears in pre-rework production logs and in
#: docs/INCIDENT_HISTORY.md, and nothing emits it any more.
STOP_REFUSAL_NO_STOP_NO_VOLATILITY = "no_stop_and_no_volatility_reading"
#: No-ATR structural-stop fallback refusals (owner ruling 2026-09-25,
#: board item 80, reworked). The branch used to REFUSE a typed stop with
#: no ATR outright (`typed_stop_but_no_volatility_reading`); that was
#: wrong per the ruling -- a missing volatility reading is never a reason
#: to skip protection. The branch now derives the stop from price
#: structure and HOLDS, and only ONE refusal remains:
#:  * NO_STRUCTURAL_STOP_NO_VOLATILITY -- no ATR AND no structural level
#:    (computed level with enough touches, or signal-bar edge) on the
#:    protective side of entry: a monotonic move, no structure, or zero
#:    bars. The ruling's genuine skip-correct case. Per-symbol, durable,
#:    never a book-wide halt.
#:
#: STRUCTURAL_STOP_TOO_FAR is still DEFINED but is no longer raised by
#: anything: board item 185 deleted the width refusal behind it on
#: 2026-09-30. It refused a readable structural level sitting further from
#: entry than `1 - STOP_SANITY_FLOOR_FRACTION` (50%), borrowing that
#: fraction from the midday TRAIL_STOP typo guard -- which item 185
#: replaced with a reading off the instrument that this no-ATR branch
#: cannot follow, there being no volatility reading here by construction.
#: The reasoning for deleting rather than re-picking it is at the deletion
#: site in `_widen_stop_past_noise`; in short it is board item 56 route
#: (c)'s ruling applied to the same shape of gate, and the refusal had
#: fired zero times in production. What is NOT true, and was claimed in an
#: earlier version of this note, is that the reward:risk tail still judges
#: the resulting stop in every case: that tail is off for a Type B /
#: breakout trade by design, so on a breakout with no ATR reading nothing
#: judges stop width at all and sizing is the only answer. The name is kept because tests
#: reference it, exactly as STOP_REFUSAL_SECTOR_BELOW_MIN_ORDER was kept
#: after board item 183.
STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY = "no_structural_stop_and_no_volatility_reading"
STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR = "structural_stop_past_sanity_bound"
#: `_resolve_entry_and_stop`'s terminal side check: whatever the stop rules
#: above produced is absent, non-positive, or on the wrong side of entry.
#: Filed only when no more specific refusal was already recorded for this
#: symbol this call — the specific reason always wins over the generic one.
STOP_REFUSAL_NO_VALID_STOP = "no_valid_stop_against_entry"
#: `derive_structural_target` refused (not a data fault) and the trade has no
#: take-profit to measure against. The code filed is the derivation's OWN
#: `refusal` (`no_structural_levels`, `no_level_in_direction`,
#: `no_expected_horizon`, `projection_implausible` — `src/data/levels.py`),
#: because it is already a machine code and inventing a second name for it
#: here would just be a synonym to keep in sync. This constant is the
#: fallback for the impossible case of a refusal with no code on it.
STOP_REFUSAL_NO_STRUCTURAL_TARGET = "no_structural_target"
#: The derivation produced a price, but on the wrong side of the entry — a
#: long whose computed target sits at or below entry, a short whose sits at
#: or above it. Distinct from the above: structure was measurable, and what
#: it measured does not pay for this direction.
STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY = "target_not_above_entry"
STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY = "target_not_below_entry"
#: Retired sector-crowding refusal; never raised since 2026-09-24 and the dial
#: itself was deleted 2026-10-09. Kept so historical log rows still decode.
STOP_REFUSAL_SECTOR_BELOW_MIN_ORDER = "sector_crowding_leaves_below_min_order"
#: The `pipeline_event` reason under which `pipeline_stages.DecisionStage`
#: files a structured constructor refusal (`refusal=<code>` beside it).
#: Distinct from `constructor_dropped`, whose detail is recovered by regex
#: over log text and misses several messages; this one is written from
#: `last_refusals` directly, so the funnel and
#: `scripts/blocked_proposals_census.py` see the code, not a sentence.
CONSTRUCTOR_REFUSED_EVENT_REASON = "constructor_refused"
# DEAD AS OF 2026-09-11 (docs/WORK.md item 1(d)) — the three
# below-floor refusal codes and the PERMIT code below them. Nothing in this
# module refuses a trade on a reward:risk floor any more: a breakout is not
# measured against one at all, and a range trade's real ratio became a
# ranking input instead of a cutoff. Kept, not deleted, because they are
# greppable keys that appear in historical production logs and in
# `docs/INCIDENT_HISTORY.md`, and because whether the sub-floor catalyst
# machinery is retired is an owner call that has not been made. Flagged
# rather than quietly removed.
STOP_REFUSAL_GEOMETRY_AT_BAND = "reward_risk_below_floor_at_widened_stop"
STOP_REFUSAL_GEOMETRY_AT_LEVEL = "reward_risk_below_floor_at_honoured_stop"
STOP_REFUSAL_GEOMETRY_AT_KEPT = "reward_risk_below_floor_at_kept_stop"
STOP_REFUSAL_GEOMETRY_UNMEASURABLE = "reward_risk_not_measurable"

#: NOT a refusal and NOT a resize — a recording, and the only thing board
#: item 223 changed. Ruled on the RISK ROUTE 2026-10-01 on the adversary's
#: measurement -- NOT an owner ruling, which the item itself permits ("the
#: owner OR the risk route"); a reader must stay free to re-open this on new
#: evidence. A portfolio-manager target
#: whose `risk_allocation_pct` is positive but below
#: `RiskConfig.min_position_risk_pct` is sized and shipped exactly as asked,
#: because measured over 142 portfolio-manager logs (2026-08-17 to
#: 2026-09-30) 115 targets carried a risk allocation and ZERO were
#: positive-but-below-floor — a deterministic refusal would have fired zero
#: times. The floor is the seat's instruction; this code only makes a breach
#: of it VISIBLE, with the symbol, the risk asked for, the floor in force and
#: what the desk then did. The desk already had a hard sub-floor refusal and
#: retired it on 2026-09-11 for being the wrong shape; this is deliberately
#: not that. Rows carry stage `_SUBFLOOR_RISK_STAGE`, never a refusal stage.
SUBFLOOR_RISK_OBSERVED = "pm_target_below_min_risk_floor_observed"
#: The outcome column for the row above, in one greppable token: the target
#: was accepted unchanged and continued down the normal path.
_SUBFLOOR_RISK_STAGE = "observed_not_refused"
#: Not a refusal — the one PERMIT code in this block. A below-floor ratio let
#: through because the PM's sub-floor catalyst gate verified the citation and
#: capped the size (docs/WORK.md item 1, parts (b)+(c)). Greppable so "how
#: often does the exception actually fire?" is answerable from logs, which is
#: the number part (d) will need before it replaces the hard floor at all.
STOP_PERMIT_SUBFLOOR_CATALYST = "reward_risk_below_floor_catalyst_verified"

#: Which refusal code names the failure, given the rule that placed the stop.
#: One refusal per stop rule, so a log line says both what the stop IS and
#: why the ratio against it did not clear.
_GEOMETRY_REFUSAL_BY_RULE = {
    STOP_RULE_LEVEL_HONOURED: STOP_REFUSAL_GEOMETRY_AT_LEVEL,
    STOP_RULE_ABSOLUTE_FLOOR: STOP_REFUSAL_GEOMETRY_AT_LEVEL,
    STOP_RULE_ATR_BAND: STOP_REFUSAL_GEOMETRY_AT_BAND,
    STOP_RULE_OUTSIDE_BAND: STOP_REFUSAL_GEOMETRY_AT_KEPT,
    STOP_RULE_NO_VOLATILITY: STOP_REFUSAL_GEOMETRY_AT_KEPT,
}

#: Stop rules whose stop sits on a level `find_structural_levels` COMPUTED.
#: `src/pipeline_stages.py` reads this off `TradeDecision.stop_rule` to know
#: it must not re-apply its own execution-time ATR floor to that stop.
LEVEL_BACKED_STOP_RULES = frozenset(
    {
        STOP_RULE_LEVEL_HONOURED,
        STOP_RULE_ABSOLUTE_FLOOR,
    }
)


@dataclass(frozen=True)
class RiskPlan:
    """A risk-based target resolved into the units the order path speaks.

    `risk_pct` is what the budget actually granted, which may be less than the
    PM asked for; `target_weight_pct` is that risk converted through the stop
    distance into a gross-leverage weight. `note` explains any cut, and is
    carried into the order's reasoning so the AI Risk Manager reads a
    deterministic reduction as arithmetic rather than as the PM contradicting
    itself.
    """

    symbol: str
    risk_pct: float
    target_weight_pct: float
    entry_price: float | None
    stop_price: float | None
    note: str = ""
    #: True when this plan is a trim of a held, unanalysed name sized from its
    #: live broker stop. Such a plan may only REDUCE the position.
    sized_from_live_stop: bool = False


@dataclass
class ConstructorConfig:
    """Tunables for how the constructor sizes and prices orders."""

    # Ceiling on any SINGLE position's risk, and the fallback sizing basis for
    # a legacy notional target. Owner-ratified at 5% (2026-08-27); the prior
    # 0.5% was a constructor default nobody chose. Under risk-based sizing
    # (spec §2.1) this caps `TargetPosition.risk_allocation_pct` rather than
    # driving it — conviction sets the size, this bounds it.
    risk_budget_pct: float = 5.0
    # Below this, an idea is not worth trading: a token position still
    # consumes a book slot and needs its own stop and ongoing attention for
    # an immaterial payoff, and its spread/slippage cost is a large share of
    # the whole position (Alpaca charges no stock commission — this is not a
    # commission floor). A request rationed under the floor is denied
    # outright rather than shrunk.
    min_risk_pct: float = 0.5
    # Spec §2.2. Total at-risk ceiling across the book, and the share of it any
    # one correlated cluster may take. Enforced only when the caller supplies
    # `existing_risk_pct` / `clusters` — without those the constructor has no
    # view of the book's risk and must not invent one.
    max_portfolio_risk_pct: float = 25.0
    max_cluster_risk_share_pct: float = 40.0
    # The risk engine's single-name GROSS notional ceiling, mirrored here so
    # the constructor sizes UNDER it instead of proposing an order the engine
    # will hard-block. `max_position_pct` is in HARD_BLOCK_RULES, so without
    # this clamp a BUY over the ceiling is dropped entirely rather than
    # trimmed.
    #
    # 20 -> 100 on 2026-09-04 (real-data audit): at 20 this bound on nearly
    # every trade — notional = risk_pct x entry/(entry - stop) — so delivered
    # risk collapsed to ~1% regardless of stated conviction (6 of 13 real
    # proposed orders pinned at exactly 20%). 100 removed that clipping.
    #
    # 100 -> 33 on 2026-09-11. The 2026-09-04 justification for 100 rested on
    # `allow_margin` being false, which had ALREADY been flipped to true two
    # days earlier (2026-09-02) — so 100 was a live single-name ceiling, not
    # the unreachable documentation it was described as (a separate pass
    # this same day independently caught and recorded the same drift before
    # this fix landed — see docs/INCIDENT_HISTORY.md). 33 was derived from
    # this desk's own -20% `GROSS_LADDER` rung against a -60% median real,
    # dated single-session idiosyncratic collapse — see `risk.max_position_pct`
    # in config/settings.yaml for the full derivation.
    #
    # 33 -> 65 the same day, owner override. Reviewed the derivation directly
    # and set his own risk-appetite number rather than the ladder-consistent
    # one — recorded honestly, not re-derived to fit. At 65 the SAME median
    # disaster (-60%) now costs ~39% of equity, past the -20% alert rung
    # rather than under it; only the mildest of the five reference events
    # stays under that line. The ladder still de-levers what remains — this
    # number no longer prevents that rung from being reached by one name
    # alone, the way 33 was built to. In exchange, 65 mostly does not bind
    # on ordinary trades at current stop distances (33 bound knowingly; 65
    # is mostly a pure backstop against the no-stop-fills case, not a live
    # tax on everyday sizing). This is the ONLY parameter bounding a loss
    # when the stop does not fill at all (gap, halt, fraud, regulatory
    # action) — every other risk number on the desk is stop-conditional.
    # SURVIVAL against single-name tail risk, NOT diversification, variance
    # reduction or risk parity, all of which are rejected for this desk.
    # Keep in sync with `risk.max_position_pct` — pipeline.py wires them from
    # the same setting.
    max_position_pct: float = 65.0
    # NO LONGER used to reject a sector-crowded trade for being small (fixed
    # 2026-09-24: a genuine ~$295 / 2.95%-of-equity MRVL trade was refused
    # here as "under the $500 minimum order ... pays full commission" — the
    # $500 was an arbitrary round number (config/number_ledger.yaml) with no
    # broker minimum behind it, and Alpaca charges NO stock commission, so
    # the refusal was a bad decision justified by a false reason.
    # `_apply_sector_crowding_scale` now lets a sector-crowded trade through
    # at whatever size crowding leaves, however small, rather than refusing
    # it outright.
    #
    # DELETED 2026-09-26, board item 183. The sentence that used to stand here
    # said the field was "kept (not deleted) because `apply_gross_ceiling`
    # still reads it as the notional floor" — that had been false since
    # 2026-09-24, when the same fix turned that parameter into an explicitly
    # accepted-and-ignored argument (`src/risk/rules.py`, the comment on its
    # signature). Nothing in the constructor read this field, and the one
    # place it was passed to discarded it, so the constructor no longer passes
    # anything: no order size, refusal or gate changes. The sweep's own
    # `cash_sweep.min_order_usd` is a different field and is untouched.
    # Minimum stop distance, in ATRs. A stop inside ordinary volatility is not
    # a thesis invalidation, it is a coin flip on noise — Phase 3 already
    # established 1.25 ATR as one ordinary day's range for a TRAILING stop,
    # and an entry stop has to survive the whole expected hold, not one
    # session. Measured 2026-08-27: the book's stops sat a median 4.3% below
    # entry against a median ATR of 2.56% of price — about 1.7 ATRs, barely
    # more than a single day. Structure still places the stop; this only
    # pushes it out when structure put it inside the noise.
    # This is a BASE, not a constant. `_stop_atr_multiple` adjusts it per
    # trade — a range setup invalidates inside a defined band and does not
    # need the room a breakout does, and a risk-off tape chops harder than a
    # trending one. ATR itself already adapts the distance to each stock and
    # each session; these adjust how many ATRs that stock's setup deserves.
    #
    # 3.0 -> 1.5 -> 2.5 (2026-09-10). This ONLY applies when no real level
    # backs the stop (see `absolute_min_stop_atr_multiple` below for that
    # case) — the owner's standing rule is that a REAL level is always judged
    # on its own honest distance, never overwritten by this number. This is
    # purely the fallback for trades that have no such level.
    #
    # The 1.5 this replaces was measured (Sweeney MAE) against this desk's
    # own ~2-week trade history — the SAME history whose signals were later
    # found to include seats that misreported confidence and data quality
    # (the "content-honesty" fixes, 2026-09-04/05). That window is too short,
    # too clean a regime (no risk-off), and now of suspect provenance to be
    # the sole basis for a risk-of-ruin number. Not necessarily wrong, just
    # no longer trustworthy as the ONLY input.
    #
    # 2.5 instead comes from published doctrine that does not depend on this
    # desk's own data at all: general swing-trading stop-placement guidance
    # puts a FIXED entry stop at 2.5-3.0x ATR (vs. 1.0x scalping, 1.5-2.0x
    # intraday momentum) for a multi-day hold. QAMC's prompt describes itself
    # as exactly that kind of book (days-to-weeks holds), so 2.5 sits inside
    # that bracket rather than at either edge.
    #
    # Caveat, stated honestly: Chuck LeBeau's Chandelier Exit and Van Tharp's
    # volatility-stop work (also cited in this debate, also 2.0-3.0x) are
    # TRAILING-stop mechanisms — the stop recalculates off each new high, it
    # is not a fixed distance from a static entry. This settings file already
    # flagged, correctly, that applying a trailing-stop multiple to a fixed
    # entry stop plus a hard reward:risk floor is a different, more binding
    # combination than the literature's trailing-stop use case. 2.5 is
    # grounded in the general entry-stop consensus above, not in Chandelier/
    # Tharp specifically — noted so the two aren't conflated later.
    #
    # Known tension, disclosed rather than hidden: `min_reward_risk_after_
    # widening` (1.5) requires roughly `sqrt(H) >= 1.5 x 2.5` to clear
    # (H = hold in sessions), i.e. H >= ~14 sessions. Re-measure once honest
    # post-fix trade history exists.
    #
    # ONE WIDTH FOR EVERY UNBACKED STOP (owner ruling 2026-10-04; mandate
    # 2026-10-09). A stock's stop width comes from that stock's own
    # behaviour -- its ATR -- times this 2.5, and nothing else. The setup
    # scaler (range 0.90) and the macro-regime scalers (risk-off 1.20 /
    # transitional 1.10 / risk-on 0.95) were removed 2026-10-09: none was
    # measured, and market mood is one weighted input to the decision
    # elsewhere, never a stop-width scaler.
    min_stop_atr_multiple: float = 2.5
    # --- Level-backed stops (spec §12.1, 2026-09-01) --------------------
    # NO `level_match_atr_tolerance` HERE ANY MORE — removed 2026-09-13,
    # docs/WORK.md item 46, along with the `risk.*` setting it mirrored.
    # It said "within 0.25 ATR of a computed level counts as sitting AT it"
    # and justified 0.25 as being at least as wide as the 1%-of-price zone
    # `find_structural_levels` clusters pivots into. Different units: the
    # claim only held where ATR >= 4% of price, and at this desk's quoted
    # 2.56% median ATR the tolerance was 0.64% — 1.56x narrower than the
    # zone. `_level_backing_stop` now reads the bound off the zone itself
    # via `level_zone_halfwidth`, so there is one number, in one unit, in
    # one file. The ATR question ("is the stop far enough out to survive
    # this name's noise") is unchanged and still lives in
    # `min_stop_atr_multiple` / `absolute_min_stop_atr_multiple`.
    # The deterministic floor UNDER the exemption above, applying to
    # level-backed stops too. See `_widen_stop_past_noise` for the reasoning;
    # in short, §12.1 argues the exemption is safe because
    # `config/prompts/tech_analyst.md` forbids a sub-1-ATR stop, but that is
    # a prompt and Invariant 2 requires the deterministic layer to be the
    # final authority and to fail closed. A level-backed stop is honoured
    # however tight down to this many ATRs; inside it, it is widened to
    # exactly this floor — NOT to the `min_stop_atr_multiple` band.
    # 0 disables the floor entirely. Kept in sync with
    # `risk.absolute_min_stop_atr_multiple`.
    absolute_min_stop_atr_multiple: float = 1.0
    # How many prior touches a computed level needs before `_level_backing_stop`
    # treats a stop resting on it as verified enough for the exemption above
    # (Phase 12.1, 2026-09-03). `find_structural_levels` already requires 2
    # touches to register a level at all, but that was never a quality bar
    # for trusting a TIGHT stop on it — see docs/RESEARCH_FINDINGS.md §7's
    # measured table. Real-vs-shuffled bounce probability only separates with
    # non-overlapping 95% CIs at 5+ touches (real 0.644 [0.590, 0.696] vs
    # shuffled 0.505 [0.470, 0.539]); every lower bucket's CIs overlap or
    # nearly touch, i.e. could be noise. A level below this bar is not
    # "verified" here and the stop falls back to the ATR floors, exactly as
    # an unbacked stop does. Kept in sync with
    # `risk.min_level_touches_for_stop_honor`.
    min_level_touches_for_stop_honor: int = 5
    # How far BELOW a structural level (ABOVE it for a short) the
    # protective stop sits when it must be READ from price structure
    # because there is no ATR reading -- `_derive_structural_stop_no_atr`,
    # owner ruling 2026-09-25 (board item 80). A fraction of the level
    # price, so a wick that just tags the level does not trigger the stop.
    # OWNER-APPETITE, not doctrine: the published methods (swing-low,
    # prior-bar low, Donchian channel-low) agree a buffer is needed but
    # none fixes its size. Ledgered with an OPEN QUESTION in
    # config/number_ledger.yaml. Default 0.5% is a modest slack,
    # deliberately smaller than the 1.0% level-cluster zone
    # (`levels.CLUSTER_TOLERANCE_PCT`) so the stop sits just past the zone
    # the level was matched within, never inside it.
    structural_stop_buffer_pct: float = 0.005
    # --- Target derivation (2026-09-01) ---------------------------------
    # The stop has been computed from measured volatility since 2026-08-27;
    # the target was still the language model's `reference_target`, so the
    # reward:risk gate above was dividing a measurement by an opinion. These
    # tune `src/data/levels.py::derive_structural_target`, which computes the
    # target from the same bars the stop comes from. See that module's
    # target-derivation section for the rule and the evidence; the defaults
    # here deliberately mirror its module-level constants.
    min_target_atr_multiple: float = 1.0
    breakout_projection_atr_multiple: float = 1.0
    max_target_reach_atr_multiple: float = 1.5
    # NO `max_stop_width_reach_atr_multiple` HERE ANY MORE -- the stop-width
    # refusal was deleted 2026-09-26 (board item 56, route (c)); see
    # `STOP_REFUSAL_WIDER_THAN_REACH` above for the measurement that settled
    # it. `max_target_reach_atr_multiple` on the line above is a DIFFERENT
    # number doing the target-estimation job and is unchanged.
    max_target_horizon_sessions: int = 60
    # The model's target is not thrown away — it becomes evidence. Above this
    # absolute percentage gap between the computed target and the model's
    # guess, the disagreement is logged at WARNING rather than INFO, because
    # a model that is consistently far from the chart is a finding about the
    # model, not about the trade.
    target_divergence_warn_pct: float = 25.0
    # NO `min_trade_weight_delta` HERE ANY MORE (owner ruling 2026-09-30,
    # board item 183) -- the flat 0.5%-of-book churn floor is DELETED, not
    # resized. The desk's own delta loop now attempts every nonzero
    # rebalance its reasoning asks for; see `CONSTRUCTOR_NO_ACTION_BELOW_
    # MIN_DELTA` above and `_NO_REAL_WEIGHT_DELTA` below for what is kept
    # in its place (a floating-point-noise guard, not an appetite choice).
    # --- Spec §11.2 — the gross-exposure ceiling (2026-09-01) ------------
    # The SIZING half of the ceiling. `max_gross_exposure` is in
    # HARD_BLOCK_RULES, so without this clamp an entry that breaches the
    # ceiling is DROPPED at the execution gate rather than taken smaller —
    # the same relationship `max_position_pct` already has with its clamp
    # here, and the same reason: a ceiling that only refuses produces
    # no-trade sessions instead of right-sized ones.
    #
    # This is the STANDING cap. The ladder-resolved ceiling for the session
    # is passed to `construct_orders` per run, because it depends on live
    # drawdown and a config default cannot know it. Kept in sync with
    # `risk.max_gross_exposure_x` — pipeline.py wires them from the same
    # setting.
    max_gross_exposure_x: float = 2.0
    # The cash-park vehicle (`cash_sweep.symbol`, SGOV by default), which is
    # parked cash and NOT exposure. Taken from config rather than hardcoded;
    # None when sweeping is off.
    cash_park_symbol: str | None = None
    # NOTE (2026-08-27): the naive-percent stop fallback (`entry * 0.95`)
    # was REMOVED and stays removed. The ATR-multiple fallback came back on
    # 2026-09-12 (docs/WORK.md item 54) in a different form: not a silent
    # `entry - 2*ATR` that became the norm because the analyst typed
    # nothing, but the desk's own noise band (`min_stop_atr_multiple`, per
    # setup and tape) or the signal bar's edge, whichever is wider, applied
    # ONLY when nothing computed backs the typed stop — and the result is
    # then gated on width. The analyst schema requires a stop on every
    # actionable rating, so "nothing typed" is the rare case, not the norm.


def widest_reachable_stop_atr_multiple(base_multiple: float | None = None) -> float:
    """The widest stop, in ATRs, `_stop_atr_multiple` can return.

    NOT a new number: it is the base (`risk.min_stop_atr_multiple`, owner
    ruling 2026-10-04: 2.5 ATR) and nothing else, because the setup and
    macro-regime scalers were removed 2026-10-09 -- a stock's stop width
    comes from its own ATR only. It exists so the midday TRAIL_STOP clamp
    and the universe screen's volatility ceiling compute "the widest stop
    this desk can place" from the constant that governs stops instead of
    carrying their own copy (board item 185). `tests/test_universe_screen.py`
    pins that both callers agree.
    """
    if base_multiple is None:
        base_multiple = ConstructorConfig.min_stop_atr_multiple
    return float(base_multiple)
