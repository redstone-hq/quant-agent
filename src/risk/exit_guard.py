"""Deterministic guard on exit reasoning — spec Phase 3.2, audit §1.5.

The Position Reviewer had no memory of its own prior review. It rebuilt its
view of every position from scratch twice a day, so it could report a position
deteriorating while every number it had itself recorded six hours earlier had
improved. On 2026-08-26 it sold EPD for "not progressing" when thesis progress
had risen 16% -> 20% and distance-to-stop had improved since its own midday
read. The evening reviewer then graded that exit **premature**, and the same
thing happened to MRVL.

This module is the deterministic half of the fix. It does not decide anything
about the market; it only refuses to let a **deterioration claim** stand when
the deterioration did not happen. The reviewer keeps full authority to exit on
new information — adverse news, an earnings miss, a regime shift, a
triggered `thesis_invalid_if`. Those are judgments about
the world. "It is stalling" is a claim about numbers, and the numbers are
right here.

Directionality matters and is the whole point:
  - `thesis_progress_pct` rising is improvement.
  - `distance_to_stop_pct` rising is improvement ONLY when the PRICE moved
    away from the stop, on EITHER side — see `veto_contradicted_exit`'s
    docstring for the 2026-09-18 fix that made the underlying metric
    actually side-correct for a short. It is also a function of the stop
    as much as of the price, so widening the stop also makes it rise —
    which is the desk removing its own protection, not the position
    getting better. Verified on real 2026-09-01 snapshots for V, CMCSA and
    DIS, all three of which were deteriorating at the time. Decomposed
    since 2026-09-18; see `_STOP_DEPENDENT_METRIC` and
    `MetricDeltas.stop_driven`, both side-aware since the same date.
  - `r_multiple` rising is improvement.
  - `pace` rising is improvement.
A verdict may not call a position stalled while its own measured deltas are
positive.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal
from src.risk.exit_guard_structural import (  # noqa: F401 -- lifted verbatim, re-exported
    _consecutive_prior_break_count,
    _structural_level_backing_stop,
)
from src.risk.exit_guard_claims import (  # noqa: F401 -- re-exported, lifted verbatim
    _REGIME_FLIP_CLAIM_RE,
    _BEARISH_STATE_CHANGE_CLAIM_RE,
    TRUSTED_MACRO_STATUSES,
    _THESIS_INVALIDATION_CLAIM_RE,
    _NEGATION_CUE_RE,
    _NEGATION_LOOKBACK_CHARS,
    _is_negated,
    claims_regime_flip,
    claims_bearish_state_change,
    claims_thesis_invalidation,
    HoldingDisciplineClaimCheck,
    holding_discipline_claim_check,
    holding_discipline_false_claim,
)

from src.risk.exit_guard_deltas import (  # noqa: F401 -- lifted verbatim, re-exported
    DETERIORATION_PATTERNS,
    _DETERIORATION_RE,
    _HIGHER_IS_BETTER,
    _STOP_DEPENDENT_METRIC,
    _PROVENANCE_KEYS,
    _finite,
    MetricDeltas,
    compute_deltas,
    is_deterioration_claim,
    veto_contradicted_exit,
)
from src.risk.exit_guard_trend import (  # noqa: F401 -- lifted verbatim, re-exported
    TREND_CONFIRMING_CLOSES,
    ADX_STRONG_TREND_THRESHOLD,
    REGIME_STRONG_WITH_TREND,
    REGIME_DEFAULT,
    classify_trend_context,
    _break_confirmation_settings,
    _prior_structural_level_broken,
    _trend_clause,
    _compose_owner_break_reason,
    render_owner_break_message,
)
from src.risk.exit_guard_thesis_macro import (  # noqa: F401 -- lifted verbatim, re-exported
    _MACRO_SERIES_UNITS,
    _MACRO_SERIES_RE,
    _MACRO_NUMBER_RE,
    _macro_threshold_in_series_unit,
    _clean_number,
)
from src.risk.exit_guard_thesis import (  # noqa: F401 -- lifted verbatim, re-exported
    _DOWN_WORDS_RE,
    _UP_WORDS_RE,
    _MA_REF_RE,
    _DOLLAR_PRICE_RE,
    _LEVEL_WORD_PRICE_RE,
    _DIRECTION_PRICE_RE,
    _SUPPORTED_MA_PERIODS,
    ThesisInvalidationCheck,
    _extract_price_threshold,
    check_thesis_invalid_if,
)

__all__ = [
    "MetricDeltas",
    "compute_deltas",
    "is_deterioration_claim",
    "veto_contradicted_exit",
    "DETERIORATION_PATTERNS",
    "BREAK_CONFIRMATION_ATR_MULTIPLE",
    "ThesisInvalidationCheck",
    "check_thesis_invalid_if",
    "TRUSTED_MACRO_STATUSES",
    "claims_regime_flip",
    "claims_bearish_state_change",
    "claims_thesis_invalidation",
    "holding_discipline_false_claim",
    "HoldingDisciplineClaimCheck",
    "holding_discipline_claim_check",
    "StructuralProtectionCheck",
    "check_structural_protection",
    "structural_protection_broken",
]


#: BOARD ITEM 70, THE SPLIT (2026-09-26). Until this date ONE literal `1.0`
#: did TWO different jobs in the exit path: the noise band (removed 2026-10-09; how far an
#: adverse move must travel from ENTRY before it stops being ordinary daily
#: wobble) and the BREAK MARGIN below (how far a daily CLOSE must sit beyond a
#: structural LEVEL before that level counts as broken). They are not the same
#: quantity — different reference points, different units in the literature,
#: and nothing ties their magnitudes — so a shared constant made each one
#: impossible to source without moving the other. They are now two names.
#:
#: NO BEHAVIOUR CHANGES IN THIS SPLIT. Both are 1.0 today, exactly as before;
#: the split exists so each can be answered on its own evidence. Neither value
#: was retuned, which item 70 explicitly forbids in the same pass.
#:
#: WHAT THE RESEARCH FOUND for THIS one (docs/INCIDENT_HISTORY.md, 2026-09-26).
#: The published literature answers "how far beyond a level is a real break" in
#: PERCENT and level-dependently, never in ATRs: Edwards & Magee use ~3% for a
#: major level and ~1% for a short-term one; Bulkowski's answer is essentially
#: "a decisive close", i.e. zero distance, with confirmation carried by the
#: close count and the throwback behaviour instead. There is therefore NO
#: published ATR basis for this quantity at all — it is not merely unsourced,
#: it is unidentifiable in the units it is expressed in. The confirmation RULE
#: around it IS sourced (`TREND_CONFIRMING_CLOSES`, Edwards & Magee's two
#: consecutive closes); only this margin is not. Kept at 1.0 and kept
#: `arbitrary` in config/number_ledger.yaml rather than dressed up as sourced.
BREAK_CONFIRMATION_ATR_MULTIPLE = 1.0

# ---------------------------------------------------------------------------
# Structural (data-driven) holding protection — spec item 25, 2026-09-03
# ---------------------------------------------------------------------------
#
# WHAT THIS REPLACES. `holding_discipline_false_claim` used to treat every
# position with `days_held < 5` as "protected" from a plain SELL/REDUCE/COVER,
# full stop — a flat day-count with no backtest behind it, traced to an April
# 2026 commit that stated a philosophy ("give a thesis room to work") and
# never measured one. The owner rejected the day count as arbitrary and
# approved this replacement: a position is protected from a plain
# no-real-trigger exit UNLESS the structural level actually backing its
# thesis has been broken by price. Nothing here is time-bound; a position
# held 30 days with an intact level is exactly as protected as one held
# zero days with the same level intact, and a position whose level breaks
# on day zero has no protection at all.
#
# WHAT "backing its thesis" MEANS, in priority order:
#   1. The trade's own `thesis_invalid_if` (the analyst's stated falsifier,
#      `TradeDecision.thesis_invalid_if` — PR #250), checked for real against
#      today's price/MA data via `check_thesis_invalid_if` above. TRIGGERED
#      means broken; NOT_TRIGGERED means intact; UNPARSEABLE falls through
#      to (2) rather than being treated as either — an unparseable condition
#      is not evidence either way, and a fallback the desk already trusts
#      elsewhere is a better answer than a coin flip.
#   2. Absent a stated condition (or given one `check_thesis_invalid_if`
#      cannot read), the nearest VERIFIED structural level backing the
#      position's actual stop — the exact machinery
#      `PortfolioConstructor._level_backing_stop` already uses to decide
#      whether a stop earns an exemption from the ATR noise floor:
#      `computed_levels` / `computed_level_touches` (real levels, attached
#      to the analysis in Python by `TechAnalystAgent`, never asserted by
#      the model — see that method's docstring), at any touch count
#      (the touch bar was deleted 2026-10-14: a real level beats none at
#      the same distance and extra touches add nothing), matched to the
#      stop by whether the stop falls inside that level's own zone
#      (`level_cluster_tolerance_pct` of the level price —
#      `src.data.levels.CLUSTER_TOLERANCE_PCT`, the constant that built
#      the zone). No new constant is introduced here — the bar is the one
#      `_level_backing_stop` already uses, reused rather than duplicated.
#      A long's level is "broken" when price is at or through
#      it by more than one ATR noise band (a different question in a
#      different unit — see `check_structural_protection`'s docstring);
#      the mirror for a short is price at or through a resistance level
#      from above.
#   3. Neither (1) nor (2) resolves — no stated condition (or an
#      unparseable one) AND no qualifying structural level under the stop.
#      This is an INTENTIONAL, owner-flagged behaviour change: a thesis
#      with nothing concrete backing it is not entitled to an automatic
#      pass just because it is young. The caller must log this case
#      visibly (see `holding_discipline_false_claim` below) rather than
#      silently letting the position fall through as either protected or
#      not.
#
# This module never fetches data itself — every value is a plain number,
# string or mapping the caller already has lying around from the same
# machinery `PortfolioConstructor` uses (bars → `compute_indicators` →
# atr/MAs, `find_structural_levels` → computed_levels/touches). No LLM call
# anywhere in this path.


def level_zone_span_phrase(
    level: float,
    computed_level_zones: dict | None = None,
    computed_level_bars: dict | None = None,
) -> str:
    """How wide the level is, in words, for every claim that it is BACKING.

    docs/WORK.md item 215. A position could be reported to the owner as still
    protected by a level whose measured zone runs a fifth of the price wide,
    and nothing in the sentence said so: "the level is intact" and "the price
    where the stop rests is intact" are different statements inside a wide
    zone. Every owner-facing sentence that says a level is backing the stop
    now carries the level's MEASURED span, so the owner can see how precise
    the claim is without anyone inventing a "wide"/"tight" cutoff.

    The span is read, in order, off the measured zone (`computed_level_zones`,
    ``[low, high]``) or off the bars that drew the level
    (`computed_level_bars`). Neither present means the span is UNKNOWN and
    this says so — it never substitutes a percentage of price for a
    measurement the caller did not supply.
    """
    low = high = None
    zone = (computed_level_zones or {}).get(level)
    if zone is not None:
        try:
            low, high = float(zone[0]), float(zone[1])
        except (TypeError, ValueError, IndexError, KeyError):
            low = high = None
    if low is None or high is None:
        lows, highs = [], []
        for rng in (computed_level_bars or {}).get(level) or ():
            try:
                b_low, b_high = float(rng[0]), float(rng[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (math.isfinite(b_low) and math.isfinite(b_high)) or b_low > b_high:
                continue
            lows.append(b_low)
            highs.append(b_high)
        if lows and highs:
            low, high = min(lows), max(highs)
    if low is None or high is None or not (math.isfinite(low) and math.isfinite(high)) or high < low:
        return "measured zone span NOT RECORDED for this level"
    span = high - low
    pct = f", {span / level * 100:.2f}% of the level price" if math.isfinite(level) and level > 0 else ""
    return f"measured zone {low:.4g}-{high:.4g}, span {span:.4g}{pct}"


@dataclass(frozen=True)
class StructuralProtectionCheck:
    """Whether a position's thesis-backing level is still intact.

    `protected` is the one field callers gate on. `basis` names which of
    the three cases above decided it, and `detail` is a human-readable
    reason for the audit trail / log line — see the module note above for
    why the no-basis case in particular must never be silent.
    """

    protected: bool
    basis: Literal[
        "thesis_invalid_if_triggered",
        "thesis_invalid_if_pending_confirmation",
        "thesis_invalid_if_intact",
        "structural_level_broken",
        "structural_level_pending_confirmation",
        "structural_level_intact",
        # No chart level backs the thesis (or no data to read one): never
        # protected. Replaced the four ATR noise-band fallback bases 2026-10-09.
        "no_chart_level",
        # No chart level, but the position is flat or in profit versus entry:
        # protected (owner mandate: take profit early only in sideways
        # markets, so a rotation may not sell a winner on this ground).
        "no_adverse_move_from_entry",
    ]
    detail: str
    #: The structural level price this read found CONFIRMED broken, on the
    #: `structural_level_broken` basis only; None on every other basis.
    #: Added 2026-09-30 so a caller can name WHICH level broke instead of
    #: guessing one by proximity to the close (the alignment exit did
    #: exactly that and could admit an overhead level that never broke).
    broken_level: float | None = None
    #: True when TODAY's close (independent of the confirmation gate below)
    #: found the thesis/level basis broken. Callers must persist this value
    #: keyed by symbol AND today's close date, so it can be fed back in as
    #: `break_seen_prior_close` on the NEXT TRADING DAY's read — that is the
    #: only state this module needs to implement confirmation, and it holds
    #: none of it itself (pure function in, pure value out).
    raw_broken: bool = False
    #: The confirmation REGIME this read selected — one of
    #: `classify_trend_context`'s labels (against_or_weak / with_trend_moderate
    #: / with_trend_strong / insufficient_context), or "" on a basis where it
    #: does not apply (a non-broken level, no chart level). Recorded so
    #: the audit trail and the owner message can say WHICH regime the desk read.
    trend_context: str = ""
    #: How many consecutive confirming daily closes this regime needs before it
    #: lifts protection (1 for against/weak, 2 for a with-trend break), and how
    #: many have confirmed so far (today's close plus the prior consecutive
    #: streak, capped at needed). 0 on a basis where confirmation does not apply.
    confirming_closes_needed: int = 0
    confirming_closes_seen: int = 0
    #: PLAIN-LANGUAGE, owner-facing reason for a DECISIVE break outcome — a
    #: confirmed break that lifts protection ("real breakdown"), or a break
    #: held pending confirmation ("possible shakeout, waiting"). Empty on every
    #: non-decisive basis (intact level, thesis intact, no chart level).
    #: Carries the trigger, the trend context and the confirmation state in
    #: words, no bare numbers standing alone. `render_owner_break_message`
    #: composes the symbol and action verb around it for the Telegram/board
    #: surfaces; this field is the reusable clause.
    owner_reason: str = ""

    @property
    def confirmed_chart_break(self) -> bool:
        """True only when a CONFIRMED chart break (thesis level or structural
        level, past the confirmation gate) backs a cut. A record, not a gate:
        a cut without one still proceeds, bounded by the protective stop."""
        return self.basis in ("thesis_invalid_if_triggered", "structural_level_broken")


def check_structural_protection(
    *,
    thesis_invalid_if: str | None,
    current_price: float | None,
    entry_price: float | None,
    stop_loss: float | None,
    atr: float | None,
    is_short: bool = False,
    computed_levels: list | None = None,
    computed_level_touches: dict | None = None,
    computed_level_zones: dict | None = None,
    computed_level_bars: dict | None = None,
    level_cluster_tolerance_pct: float,
    ma_20: float | None = None,
    ma_50: float | None = None,
    ma_200: float | None = None,
    ma_200_prior: float | None = None,
    adx: float | None = None,
    break_seen_prior_close: bool = False,
    prior_break_streak: int | None = None,
    prior_break_records: list | None = None,
    prior_session_dates: list | None = None,
) -> StructuralProtectionCheck:
    """Decide whether a position's thesis-backing level is still intact.

    Pure function — every input is a plain value or mapping the caller
    already has; nothing here calls an LLM, a broker, or a market-data
    endpoint. See the module note above for the three-case priority order.

    TREND-SCALED CONFIRMATION (owner mandate 2026-09-24; citation-backed spec).
    `adx`, the 50/200 MA stack and the 200-MA SLOPE (`ma_200`/`ma_200_prior`)
    and the close are passed to `classify_trend_context`, which selects one of
    two sourced regimes (see the module note above): the DEFAULT regime
    (against/weak/tangled, or no trend data) uses the ratified two-consecutive-
    close floor with reclaim reset; the STRONG-WITH-TREND regime (ADX>=25 with a
    rising-200/50>200 up-structure) additionally requires the prior structural
    swing-low to also break before lifting protection. There is NO single-close
    path — nothing exits faster than the two-close floor. The break MARGIN is
    ALWAYS `BREAK_CONFIRMATION_ATR_MULTIPLE` (1.0 ATR) — the regime changes only whether
    the prior-low condition applies, never the margin or the close count. The
    decisive outcomes (a confirmed break, or a break held pending confirmation)
    fill `owner_reason` with a plain-language sentence for the owner surfaces.

    CONFIRMATION STATE carried across days. Preferred inputs are
    `prior_break_records` — one record per prior completed session for this
    position (each `{bar_date, raw_broken, close}`) — and `prior_session_dates`
    — the exact completed trading sessions strictly before today's close,
    most-recent first, taken from the position's own daily bars (the
    authoritative calendar, weekends/holidays already removed). From those the
    count of CONSECUTIVE confirming prior closes is reconstructed here, so a
    skipped/gap session resets the streak (#3) and a prior close that only
    cleared a LOOSER margin than today's classification demands does not count
    (#4). When those richer inputs are absent, the legacy shorthands apply:
    `prior_break_streak` (an int count) or `break_seen_prior_close` (a bool,
    a prior streak of exactly one).

    `current_price` MUST be the latest completed DAILY CLOSE for the
    thesis/level basis below — never a live/intraday quote. Real trading
    practice (and this codebase's own noise-band reasoning elsewhere) is
    explicit that a level "breaks" on a decisive close beyond it, not on a
    wick that pierces it and closes back inside; a same-day intrabar dip
    through a level must never register as a break at all, closed or not.
    `ma_20`/`ma_50`/`ma_200` must be computed off the same close.

    CONFIRMATION GATE (owner refinement, 2026-09-04, corrected same day
    after review against real technical-analysis practice). A thesis-break
    or a structural-level break must not lift protection off a single
    day's close — a "spring" (a level briefly breaking then reclaiming,
    often itself a BULLISH signal) is a well-documented pattern, not a
    real breakdown, and can take a day or two to resolve. A break lifts
    protection only once the SAME break condition holds on the close of
    TWO CONSECUTIVE TRADING DAYS. `break_seen_prior_close` carries that
    state IN — true when the immediately preceding TRADING DAY's close
    (not merely the last time this ran — several same-day pipeline cycles
    must not double-count one close), for this same position, already
    came back `raw_broken=True`. This read only lifts protection
    (`protected=False`) when it is ALSO broken today, i.e. two consecutive
    confirming closes; a single broken close returns `protected=True` with
    a `*_pending_confirmation` basis, and a reclaim the next day resets —
    it does NOT carry forward toward a future confirmation. The caller is
    responsible for persisting `raw_broken` keyed by symbol AND the close's
    own date, and feeding the prior TRADING DAY's value back in as
    `break_seen_prior_close`; this module holds no state of its own and
    does not know what a "day" or a "cycle" is. This gate applies ONLY to
    the thesis/level basis below — the no-level case (never protected), and
    the two independent regime-flip / bearish-state-change triggers in
    `holding_discipline_false_claim`, all lift protection immediately,
    unaffected by this gate.

    The margin for "beyond the level" is `BREAK_CONFIRMATION_ATR_MULTIPLE`
    (1.0), NOT the level-zone tolerance used to MATCH a level to a stop's
    placement. Until 2026-09-26 it shared the noise-band constant and this
    docstring called that 1.0 "already ratified" — it never was, on either
    job (board item 70); the two jobs now have two names and two ledger
    entries, both still open. The two are not
    even the same kind of quantity: matching is an identity question about
    a zone defined as a percentage of price, breaking is a question about
    whether a move exceeded the name's own noise, which is an ATR
    question. See docs/WORK.md item 46 for why conflating the two units
    was the defect here in the first place.
    """
    # Trend regime — classify ONCE up front so the thesis and structural-level
    # branches below share the same read. `needed_closes` and whether the prior
    # structural swing-low must also break come from the sourced regime; the
    # break margin is always `BREAK_CONFIRMATION_ATR_MULTIPLE`.
    trend_context = classify_trend_context(
        is_short=is_short,
        current_price=current_price,
        ma_50=ma_50,
        ma_200=ma_200,
        ma_200_prior=ma_200_prior,
        adx=adx,
    )
    needed_closes, requires_prior_low = _break_confirmation_settings(
        trend_context,
    )

    def _prior_streak(clears) -> int:
        """Consecutive prior confirming closes under `clears`. Prefers the
        adjacency/margin-aware record walk; falls back to the legacy int/bool
        shorthands when no records were supplied."""
        if prior_break_records and prior_session_dates:
            return _consecutive_prior_break_count(
                prior_break_records,
                prior_session_dates,
                clears=clears,
            )
        if prior_break_streak is not None:
            return max(0, int(prior_break_streak))
        return 1 if break_seen_prior_close else 0

    # Thesis-branch base streak: adjacency only (a thesis_invalid_if break has
    # no ATR margin to conflate, so every adjacent broken close counts). The
    # structural-level branch recomputes this with a margin-conflation guard
    # once its own break margin is known.
    prior_streak = _prior_streak(clears=lambda r: True)
    # Today's own broken close is the first confirming close; the prior streak
    # supplies the rest. Capped for display so "seen" never exceeds "needed".
    closes_seen = min(prior_streak + 1, needed_closes)
    confirmed = prior_streak + 1 >= needed_closes

    text = (thesis_invalid_if or "").strip()
    if text:
        check = check_thesis_invalid_if(
            text,
            current_price,
            ma_20=ma_20,
            ma_50=ma_50,
            ma_200=ma_200,
        )
        if check.status == "TRIGGERED":
            if confirmed:
                return StructuralProtectionCheck(
                    protected=False,
                    basis="thesis_invalid_if_triggered",
                    detail=(
                        f"thesis_invalid_if triggered on {closes_seen} "
                        f"confirming trading-day close(s) "
                        f"(trend context: {trend_context}): {check.detail}"
                    ),
                    raw_broken=True,
                    trend_context=trend_context,
                    confirming_closes_needed=needed_closes,
                    confirming_closes_seen=closes_seen,
                    owner_reason=_compose_owner_break_reason(
                        is_short=is_short,
                        trigger_desc="its stated exit condition was met on the close",
                        trend_context=trend_context,
                        confirmed=True,
                    ),
                )
            return StructuralProtectionCheck(
                protected=True,
                basis="thesis_invalid_if_pending_confirmation",
                detail=(
                    f"thesis_invalid_if triggered on today's close "
                    f"({closes_seen} of {needed_closes} confirming closes, "
                    f"trend context: {trend_context}) — still protected "
                    f"pending confirmation (guards against a one-day "
                    f"spring/false-breakdown): {check.detail}"
                ),
                raw_broken=True,
                trend_context=trend_context,
                confirming_closes_needed=needed_closes,
                confirming_closes_seen=closes_seen,
                owner_reason=_compose_owner_break_reason(
                    is_short=is_short,
                    trigger_desc="its stated exit condition was met on the close",
                    trend_context=trend_context,
                    confirmed=False,
                ),
            )
        if check.status == "NOT_TRIGGERED":
            return StructuralProtectionCheck(
                protected=True,
                basis="thesis_invalid_if_intact",
                detail=f"thesis_invalid_if not triggered: {check.detail}",
                raw_broken=False,
            )
        # UNPARSEABLE — falls through to the structural-level check below
        # rather than being treated as protected or broken by default.

    ent = _finite(entry_price)
    stop = _finite(stop_loss)
    atr_f = _finite(atr)
    if ent is not None and stop is not None and atr_f is not None and atr_f > 0:
        level = _structural_level_backing_stop(
            entry_price=ent,
            stop_loss=stop,
            is_short=is_short,
            computed_levels=computed_levels,
            computed_level_zones=computed_level_zones,
            computed_level_bars=computed_level_bars,
            level_cluster_tolerance_pct=level_cluster_tolerance_pct,
        )
        if level is not None:
            cur = _finite(current_price)
            # NOTE: matching WHICH level backs the stop (above, via
            # `_structural_level_backing_stop`) asks an IDENTITY question
            # and is answered by whether the stop rests on a BAR that drew
            # that level, with the level's measured zone required to be
            # narrower than the trade's own risk (items 55 and 215). No
            # percentage of price is involved on that side any more.
            # Deciding whether that level has since BROKEN is a different
            # question — it is about whether an adverse move is real, which
            # IS a volatility question — so it uses a wider, decisive
            # ATR-based margin, `BREAK_CONFIRMATION_ATR_MULTIPLE` (see this
            # function's docstring). Two questions, two units, on purpose.
            # The break margin is ALWAYS that constant (owner mandate
            # 2026-09-24, trend-scaled exit): the regime changes how many closes
            # and the prior-low condition, NEVER the margin.
            break_margin = BREAK_CONFIRMATION_ATR_MULTIPLE * atr_f

            # MARGIN-CONSISTENCY GUARD (#4). Now that this branch's break margin
            # is known, recompute the prior streak counting a prior close only
            # if it cleared THIS margin — a close that only cleared a looser
            # margin (a wider ATR band on a lower-ATR day) does not confirm a
            # break under the margin now in force.
            # Legacy break rows written before the stored-close change carry no
            # "close", so `_finite` returns None and they cannot confirm a break;
            # this self-heals within ~one session as fresh rows (with close) are
            # written each cycle.
            def _cleared_current_margin(r) -> bool:
                rc = _finite(r.get("close"))
                if rc is None:
                    return False
                return (rc >= level + break_margin) if is_short else (rc <= level - break_margin)

            level_prior_streak = _prior_streak(clears=_cleared_current_margin)
            closes_seen = min(level_prior_streak + 1, needed_closes)
            streak_confirmed = level_prior_streak + 1 >= needed_closes
            # A long's support is broken when the CLOSE has fallen to/through
            # it by at least the noise-band margin; a short's resistance is
            # broken when the close has risen to/through it by the same
            # margin from below.
            if cur is None:
                # No close to judge against — cannot say the level has
                # broken, so the level stays trusted (fail toward
                # protection, same "fail closed on the side that does not
                # ship a false 'safe to sell'" posture as
                # `_level_backing_stop` itself uses for missing bar ranges).
                return StructuralProtectionCheck(
                    protected=True,
                    basis="structural_level_intact",
                    detail=(
                        f"structural level {level} backs the stop but no "
                        f"current_price (closing price) supplied — treated "
                        f"as intact ("
                        + level_zone_span_phrase(
                            level,
                            computed_level_zones,
                            computed_level_bars,
                        )
                        + ")"
                    ),
                    raw_broken=False,
                )
            # BOARD ITEM 70, THE SETTLEMENT RECORDING (2026-10-01). The break
            # margin is `arbitrary` and, worse, UNIDENTIFIABLE in its own
            # units: every published answer to "how far beyond a level is a
            # real break" is a PERCENTAGE of price scaled by how important the
            # level is (Edwards & Magee ~3% major / ~1% short-term), never an
            # ATR multiple. Nothing in the desk's record said what 1.0 ATR
            # actually amounted to in those units at the moment of a decision,
            # so the number could never be compared against the only
            # literature that measures the same quantity. Every break
            # evaluation now records the margin in BOTH units, plus the touch
            # count that is the desk's only level-importance signal. This is a
            # RECORDING ONLY — `break_margin` above is unchanged and nothing
            # about when the desk sells moves. It accrues the observations in
            # the literature's units that would let this constant be settled
            # (or replaced) on evidence rather than re-searched a third time.
            _margin_pct = (break_margin / cur * 100.0) if cur > 0 else float("nan")
            _level_touches = None
            if computed_level_touches:
                _level_touches = computed_level_touches.get(level)
            break_margin_payload = (
                f"rule=break_confirmation_margin "
                f"margin_atr_multiple={BREAK_CONFIRMATION_ATR_MULTIPLE:g} "
                f"atr14={atr_f:.4g} margin_price={break_margin:.4g} "
                f"margin_pct_of_close={_margin_pct:.3g} "
                f"level={level:g} level_touches={_level_touches} "
                f"regime={trend_context} | "
            )
            if is_short:
                broken = cur >= level + break_margin
            else:
                broken = cur <= level - break_margin
            if broken:
                level_desc = f"its {level:g} resistance" if is_short else f"its {level:g} support"
                # REGIME-3 STRUCTURAL PATIENCE. For a strong-with-trend break the
                # close streak alone is not enough: the PRIOR structural
                # swing-low (long) / swing-high (short) must ALSO have closed
                # broken before protection lifts. When there is no prior
                # structural level, the gate is vacuous and regime 3 falls back
                # to the plain two-consecutive-close rule. (No volume in the exit
                # path, so this is a structural, not a Wyckoff-volume, read.)
                awaiting_prior_low = False
                if requires_prior_low and streak_confirmed:
                    prior_low_broken = _prior_structural_level_broken(
                        computed_levels,
                        level,
                        cur,
                        break_margin,
                        is_short=is_short,
                    )
                    confirmed = prior_low_broken
                    awaiting_prior_low = not prior_low_broken
                else:
                    confirmed = streak_confirmed
                if confirmed:
                    return StructuralProtectionCheck(
                        protected=False,
                        basis="structural_level_broken",
                        broken_level=_finite(level),
                        detail=(
                            break_margin_payload + f"structural level {level} backing the stop has "
                            f"closed beyond it on {closes_seen} confirming "
                            f"trading-day close(s) (regime: {trend_context}"
                            f"{'; prior swing-low also broken' if requires_prior_low else ''}): "
                            f"close {cur} vs level {level} "
                            f"(break margin {break_margin:.4g})"
                        ),
                        raw_broken=True,
                        trend_context=trend_context,
                        confirming_closes_needed=needed_closes,
                        confirming_closes_seen=closes_seen,
                        owner_reason=_compose_owner_break_reason(
                            is_short=is_short,
                            trigger_desc=(f"closed decisively {'above' if is_short else 'below'} {level_desc}"),
                            trend_context=trend_context,
                            confirmed=True,
                        ),
                    )
                pending_reason = (
                    "awaiting prior swing-low break"
                    if awaiting_prior_low
                    else f"{closes_seen} of {needed_closes} confirming closes"
                )
                return StructuralProtectionCheck(
                    protected=True,
                    basis="structural_level_pending_confirmation",
                    detail=(
                        break_margin_payload + f"structural level {level} backing the stop closed "
                        f"beyond it today ({pending_reason}, regime: "
                        f"{trend_context}) — still protected pending "
                        f"confirmation (guards against a one-day "
                        f"spring/false-breakdown): close {cur} vs level "
                        f"{level} (break margin {break_margin:.4g})"
                    ),
                    raw_broken=True,
                    trend_context=trend_context,
                    confirming_closes_needed=needed_closes,
                    confirming_closes_seen=closes_seen,
                    owner_reason=_compose_owner_break_reason(
                        is_short=is_short,
                        trigger_desc=(f"{'rose above' if is_short else 'dipped below'} {level_desc}"),
                        trend_context=trend_context,
                        confirmed=False,
                        awaiting_prior_low=awaiting_prior_low,
                    ),
                )
            return StructuralProtectionCheck(
                protected=True,
                basis="structural_level_intact",
                detail=(
                    break_margin_payload + f"structural level {level} backing the stop is intact: "
                    f"close {cur} vs level {level} (break margin "
                    f"{break_margin:.4g}; "
                    + level_zone_span_phrase(
                        level,
                        computed_level_zones,
                        computed_level_bars,
                    )
                    + ")"
                ),
                raw_broken=False,
                trend_context=trend_context,
            )

    # Neither a checkable thesis_invalid_if nor a qualifying structural
    # level under the stop, or the data to read either is missing. NOT
    # protected (owner mandate 2026-10-09: cut losers fast; the protective
    # stop bounds every loss). No chart level backing the thesis is never a
    # reason to refuse a cut, and missing data never manufactures protection.
    # An ATR "noise band" fallback used to stand here and keep such a holding
    # protected until its adverse move cleared 1.0 x ATR; it was removed whole.
    #
    # The ONE exception is a holding that is NOT a loser: flat or in profit
    # versus entry stays protected, because the mandate cuts LOSERS fast and
    # takes profit early only in sideways markets — a rotation must not sell
    # a winner merely because no chart level backs it.
    ent = _finite(entry_price)
    cur = _finite(current_price)
    if ent is not None and cur is not None and ent > 0:
        adverse = (cur - ent) if is_short else (ent - cur)
        if adverse <= 0:
            return StructuralProtectionCheck(
                protected=True,
                basis="no_adverse_move_from_entry",
                detail=(
                    "no thesis_invalid_if and no verified structural level "
                    "under the stop, but price is flat/favourable versus "
                    "entry — protected (not a loser; no chart break)"
                ),
                raw_broken=False,
            )
    return StructuralProtectionCheck(
        protected=False,
        basis="no_chart_level",
        detail=(
            "no thesis_invalid_if and no verified structural level under the "
            "stop (or no data to read one) — not protected; no confirmed "
            "chart break backs a cut here"
        ),
        raw_broken=False,
    )


def structural_protection_broken(
    *,
    thesis_invalid_if: str | None,
    current_price: float | None,
    entry_price: float | None,
    stop_loss: float | None,
    atr: float | None,
    is_short: bool = False,
    computed_levels: list | None = None,
    computed_level_touches: dict | None = None,
    computed_level_zones: dict | None = None,
    computed_level_bars: dict | None = None,
    level_cluster_tolerance_pct: float,
    ma_20: float | None = None,
    ma_50: float | None = None,
    ma_200: float | None = None,
    ma_200_prior: float | None = None,
    adx: float | None = None,
    break_seen_prior_close: bool = False,
    prior_break_streak: int | None = None,
    prior_break_records: list | None = None,
    prior_session_dates: list | None = None,
) -> bool:
    """True when the position's thesis-backing level has broken and that
    break is CONFIRMED (no protection); False when it is still intact or
    the break is only pending confirmation (protected).

    Thin bool wrapper over `check_structural_protection` — see that
    function and the module note above for the full priority order and the
    confirmation gate. Kept as a separate function because most callers
    only need the yes/no answer; the caller that needs to log WHY, or that
    needs `raw_broken` to persist for the next cycle's
    `break_seen_prior_close` (see `holding_discipline_false_claim`), should
    call `check_structural_protection` directly.
    """
    return not check_structural_protection(
        thesis_invalid_if=thesis_invalid_if,
        current_price=current_price,
        entry_price=entry_price,
        stop_loss=stop_loss,
        atr=atr,
        is_short=is_short,
        computed_levels=computed_levels,
        computed_level_touches=computed_level_touches,
        computed_level_zones=computed_level_zones,
        computed_level_bars=computed_level_bars,
        level_cluster_tolerance_pct=level_cluster_tolerance_pct,
        ma_20=ma_20,
        ma_50=ma_50,
        ma_200=ma_200,
        ma_200_prior=ma_200_prior,
        adx=adx,
        break_seen_prior_close=break_seen_prior_close,
        prior_break_streak=prior_break_streak,
        prior_break_records=prior_break_records,
        prior_session_dates=prior_session_dates,
    ).protected
