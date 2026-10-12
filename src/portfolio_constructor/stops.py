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
    stated_soft_exit,
)
from src.risk.constants import (
    reward_risk_floor_applies,
    risk_budget_allocation_pct,
)

from src.portfolio_constructor.config import logger  # the package logger, named as before the split
from src.portfolio_constructor.config import (
    STOP_RULE_LEVEL_HONOURED,
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_ATR_BAND,
    STOP_RULE_OUTSIDE_BAND,
    STOP_REFUSAL_WRONG_SIDE,
    STOP_RULE_SIGNAL_BAR,
    STOP_RULE_STRUCTURAL_NO_ATR,
    STOP_RULE_PRIOR_BAR_NO_ATR,
    STOP_REFUSAL_STOP_NOT_FINITE,
    STOP_REFUSAL_ENTRY_NOT_FINITE,
    STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
    STOP_REFUSAL_NO_VALID_STOP,
    STOP_REFUSAL_NO_STRUCTURAL_TARGET,
)


from src.portfolio_constructor.entry_stop.resolver import EntryStopResolver
import src.portfolio_constructor.level_touch_record as touch_gate
from src.portfolio_constructor.stop_width import stop_atr_multiple
from src.portfolio_constructor import no_atr_stop_record as no_atr_rows
from src.portfolio_constructor.reward_risk_at import reward_risk_at


class StopRules:
    """Stop and reward-to-risk rules, HELD by `PortfolioConstructor` (bodies moved verbatim).

    Constructible alone. `read_cfg` is a zero-argument callable returning the
    owner's CURRENT `ConstructorConfig` -- handed in live, never snapshotted,
    because callers reassign the owner's `cfg` after construction.
    `entry_stop_resolver` is a zero-argument callable building the standalone
    `EntryStopResolver` PER CALL from the owner's live collaborators; the three
    resolver-backed names below are thin shims over it. Wiring lives in
    `src/portfolio_constructor/assembly.py`.
    """

    def __init__(self, *, read_cfg, entry_stop_resolver):
        self._read_cfg = read_cfg
        self._entry_stop_resolver = entry_stop_resolver

    @property
    def cfg(self):
        """The owner's current config, read on every access."""
        return self._read_cfg()

    def _resolve_entry_and_stop(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/entry_stop/resolver.py."""
        return self._entry_stop_resolver()._resolve_entry_and_stop(*args, **kwargs)

    def _stop_atr_multiple(self) -> float:
        """How many ATRs of room an unbacked stop gets: the owner-ruled base
        (2.5, ruling 2026-10-04). The ATR already carries the stock's own
        behaviour; no setup or market-mood scaler applies."""
        return stop_atr_multiple(self.cfg)

    def _level_backing_stop(
        self,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float,
        is_short: bool,
    ) -> float | None:
        """The computed structural level this stop sits at, or None.

        Takes no ATR. It used to, because the match tolerance was an ATR
        multiple; since 2026-09-13 (docs/WORK.md item 46) "at this level"
        is decided against the level's own zone, whose width is a
        percentage of price, so the name's volatility is not an input to
        this question at all. It is still very much an input to whether
        the resulting stop is honoured — see `_widen_stop_past_noise`.

        Spec §12.1. "Verified" means the price came out of
        `src/data/levels.py::find_structural_levels` and was attached to the
        analysis IN PYTHON (`TechAnalysisResult.computed_levels`, set by
        `TechAnalystAgent` after parsing and never emitted by the model). A
        model asserting a level in `support_levels` / `resistance_levels`
        earns nothing here — if it could, it would only have to name a level
        beside its stop to buy itself an exemption from the noise floor,
        and the verification would be worthless.

        Side is judged against THIS ENTRY, not against the last close.
        `find_structural_levels` labels a level support or resistance
        relative to the last close, and its own docstring says a ceiling
        price has broken through becomes a floor. The trade is entered at a
        live price that may sit on the other side of a level, so what
        matters is only whether the level is below entry (it can hold a
        long up) or above it (it can cap a short). This is the same
        re-partition `derive_structural_target` performs for the target,
        for the same reason — see the `levels` note in its docstring.

        Returns the CLOSEST matching level so the log names the one the
        stop is actually sitting on. A level with fewer than
        `risk.min_level_touches_for_stop_honor` prior touches is skipped
        entirely here — it is real enough to register in `computed_levels`
        and to anchor a target, but not (per docs/RESEARCH_FINDINGS.md §7's
        measured table) trusted enough to earn the tight-stop exemption. A
        stop resting on it falls through to the ATR floors below, same as a
        stop with nothing computed under it at all.
        """
        raw_levels = getattr(analysis, "computed_levels", None) or []
        touches_by_price = getattr(analysis, "computed_level_touches", None) or {}
        bars_by_price = getattr(analysis, "computed_level_bars", None) or {}
        min_touches = self.cfg.min_level_touches_for_stop_honor

        best: float | None = None
        best_gap = float("inf")
        for raw in raw_levels:
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            # A long is held up by structure at or below its entry; a short
            # is capped by structure at or above its entry. A "support"
            # above a long's entry cannot be what its stop is resting on.
            if is_short and price < entry_price:
                continue
            if not is_short and price > entry_price:
                continue
            if not touch_gate.level_clears_touch_bar(
                touches_by_price.get(price),
                min_touches,
                site=touch_gate.SITE_TIGHT_STOP_EXEMPTION,
            ):
                continue
            # "At" this level means ON ONE OF ITS BARS, not merely inside
            # its band — docs/WORK.md item 215. Item 55 made the zone the
            # MEASURED combined span of the bars that formed the level
            # (`computed_level_zones`), which is honest about how wide the
            # structure is but is exactly why "inside the zone" cannot earn
            # the exemption: that span reaches a fifth of the price on some
            # names, and a stop at one end of it can be taken out with the
            # level itself never broken. The stop must instead lie inside
            # the traded high-low range of at least one bar that DREW the
            # level, carried here on `computed_level_bars`. No tolerance and
            # no width cap is introduced: the bound is the instrument's own
            # smallest statement that structure traded at that price.
            #
            # Fail closed: a level with no bar ranges recorded (older stored
            # analysis, fixture) is NOT backing, and the stop falls through
            # to the ordinary ATR floor, exactly as an unmatched stop does.
            # The measured zone stays on the level for reporting; it is no
            # longer what decides the exemption.
            # ...AND the level must be more precise than the trade it is
            # backing: its measured zone strictly narrower than the stop
            # distance. Bar membership alone put NO ceiling on how far the
            # stop could sit from the level (the zone edges are bar extremes,
            # so the furthest passing stop is the halfwidth exactly — median
            # 3.33% of price and up to 36.07% on this desk's own 704-level
            # set), while the break check reads the LEVEL price. No number is
            # introduced: the ceiling is this trade's own risk.
            if not stop_rests_on_level(
                stop_loss,
                bars_by_price.get(price),
                stop_distance=abs(entry_price - stop_loss),
            ):
                continue
            gap = abs(stop_loss - price)
            if gap < best_gap:
                best, best_gap = price, gap
        return best

    def _derive_structural_stop_no_atr(
        self,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        is_short: bool,
    ) -> tuple[float | None, float, str] | None:
        """Read a protective stop from price STRUCTURE when ATR is absent.

        Owner ruling 2026-09-25 (board item 80): a missing volatility reading
        is never a reason to skip protection or to size off an unverifiable
        typed number. ATR is gone and no raw bars survive into the constructor
        (it is a pure decision over an already-computed `TechAnalysisResult`),
        but the STRUCTURE those bars produced does survive on the analysis
        object. Tried in order of how defensible the level is:

          1. The NEAREST verified computed structural level on the protective
             side of entry -- support at/below a long, resistance at/above a
             short -- with at least `risk.min_level_touches_for_stop_honor`
             prior touches, the SAME trust bar `_level_backing_stop` applies
             before it will honour a tight typed stop. This is a swing-low /
             structure stop (IG, LuxAlgo). The shipping stop is placed one
             buffer BEYOND the level (the losing side).
          2. The signal (last completed) bar's low for a long / high for a
             short -- a prior-bar low stop (Ticker Daily), which is also what a
             Donchian channel-low collapses to when only the last bar is
             trustworthy. The shipping stop is placed one buffer beyond it.

        Returns (matched_level_or_None, stop_price, rule) or None when neither
        tier yields a usable level on the protective side of entry (a monotonic
        move, no structure, or zero bars -- the ruling's genuine skip case).
        The buffer is `ConstructorConfig.structural_stop_buffer_pct` --
        owner-appetite, ledgered with an open question, not doctrine.
        """
        buffer_pct = self.cfg.structural_stop_buffer_pct

        def _beyond(level_price: float) -> float:
            # Just past the level on the losing side: below it for a long,
            # above it for a short.
            return level_price * (1.0 + buffer_pct) if is_short else level_price * (1.0 - buffer_pct)

        def _usable(stop_price: float) -> bool:
            return (
                math.isfinite(stop_price)
                and stop_price > 0
                and (stop_price > entry_price if is_short else stop_price < entry_price)
            )

        # ---- Tier 1: nearest verified computed structural level ----------
        raw_levels = getattr(analysis, "computed_levels", None) or []
        touches_by_price = getattr(analysis, "computed_level_touches", None) or {}
        min_touches = self.cfg.min_level_touches_for_stop_honor
        best_level: float | None = None
        best_gap = float("inf")
        candidates = 0
        row = no_atr_rows.recorder(
            analysis,
            is_short,
            entry_price,
            buffer_pct,
            min_touches,
        )
        for raw in raw_levels:
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            # Protective side only, judged against THIS entry (not last close),
            # same re-partition `_level_backing_stop` and target derivation do.
            if is_short and price <= entry_price:
                continue
            if not is_short and price >= entry_price:
                continue
            if not touch_gate.level_clears_touch_bar(
                touches_by_price.get(price),
                min_touches,
                site=touch_gate.SITE_NO_ATR_STRUCTURAL_ANCHOR,
            ):
                continue
            candidates += 1
            gap = abs(entry_price - price)
            if gap < best_gap:
                best_level, best_gap = price, gap
        if best_level is not None:
            stop = _beyond(best_level)
            if _usable(stop):
                # Counted, never altered: `row` returns its argument.
                return row(
                    no_atr_rows.OUTCOME_LEVEL,
                    (best_level, stop, STOP_RULE_STRUCTURAL_NO_ATR),
                    touches_by_price.get(best_level),
                    candidates,
                )

        # ---- Tier 2: the signal (prior) bar's far edge -------------------
        bar_edge = getattr(
            analysis,
            "signal_bar_high" if is_short else "signal_bar_low",
            None,
        )
        try:
            bar_edge = float(bar_edge) if bar_edge is not None else None
        except (TypeError, ValueError):
            bar_edge = None
        if (
            bar_edge is not None
            and math.isfinite(bar_edge)
            and bar_edge > 0
            and (bar_edge > entry_price if is_short else bar_edge < entry_price)
        ):
            stop = _beyond(bar_edge)
            if _usable(stop):
                return row(
                    no_atr_rows.OUTCOME_PRIOR_BAR,
                    (bar_edge, stop, STOP_RULE_PRIOR_BAR_NO_ATR),
                    None,
                    candidates,
                )

        # Ran and found nothing. The row keeps that distinguishable from
        # "never reached", which is no row at all. Returns None regardless.
        return row(no_atr_rows.OUTCOME_NONE, None, None, candidates)

    def _reward_risk_at(self, entry_price, stop_price, target_price, is_short):
        """Reward:risk at the stop that will ship. See `reward_risk_at`."""
        return reward_risk_at(entry_price, stop_price, target_price, is_short)

    def real_reward_risk_preview(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/entry_stop/resolver.py."""
        return self._entry_stop_resolver().real_reward_risk_preview(*args, **kwargs)

    def entry_stop_preview(self, *args, **kwargs):
        """Thin shim: body in src/portfolio_constructor/entry_stop/resolver.py."""
        return self._entry_stop_resolver().entry_stop_preview(*args, **kwargs)

    def _widen_stop_past_noise(self, *args, **kwargs):
        """Thin shim: body moved to src/portfolio_constructor/entry_stop/resolver.py."""
        return self._entry_stop_resolver()._widen_stop_past_noise(*args, **kwargs)

    def shipped_stop_rule(
        self,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float,
        direction: str = "long",
    ) -> str | None:
        """Which rule the SHIPPING stop answers to, for `TradeDecision`.

        Asks `_level_backing_stop` — the same function, the same
        `computed_levels`, no second data path — whether the final rounded
        stop sits at a level the system computed. That is deliberately the
        question about the stop as it ships, not about the stop as it
        arrived: what the execution stage needs to know is whether the
        number it is holding is defended by structure.

        Returns `STOP_RULE_LEVEL_HONOURED` when it is, None when it is not
        or cannot be judged. Fails to the SAFE side: None means the
        execution-time ATR floor in `src/pipeline_stages.py` still applies,
        which is the pre-existing behaviour.
        """
        # The ATR check below is NOT the match tolerance any more (item 46
        # made that a percentage of the level's own price). It is kept as a
        # precondition because what this function REPORTS is an exemption
        # from an ATR floor: with no usable ATR there is no floor to be
        # exempt from, and claiming the exemption would be answering a
        # question nobody can evaluate. Unchanged behaviour, different
        # reason.
        atr = getattr(analysis, "atr_14", None) if analysis else None
        try:
            atr = float(atr) if atr is not None else None
        except (TypeError, ValueError):
            return None
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return None
        if not math.isfinite(entry_price) or entry_price <= 0 or not math.isfinite(stop_loss) or stop_loss <= 0:
            return None
        level = self._level_backing_stop(
            analysis,
            entry_price,
            stop_loss,
            direction == "short",
        )
        return STOP_RULE_LEVEL_HONOURED if level is not None else None

    def shipped_stop_level_basis(
        self,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float,
        direction: str = "long",
    ) -> str | None:
        """WHAT the shipping stop was based on, as a JSON record. Item 55.

        RECORDING ONLY, FALSIFICATION ONLY, and it changes nothing. It asks
        `_level_backing_stop` the SAME question `shipped_stop_rule` above
        already asks, with the same arguments and the same `computed_levels`,
        and writes down the answer's ingredients: which level, how many times
        price turned there, how many bars confirm a swing point, how wide the
        level's zone was, and how far the stop sat from it. No caller reads
        the result back into a decision — it goes to the `trades` row and
        stops there.

        The point is that board item 55 ("what IS a structural level") has
        been argued three times and measured three times to three different
        answers, because the desk never recorded what its own stops were
        standing on. This records it. See the falsification-only limit on
        `src.data.levels.describe_stop_level_basis`: this data may show the
        current definition is WRONG and may NEVER be swept for a better bar
        count or zone width.

        Returns None only when the inputs cannot produce an honest record
        (no analysis, or a non-finite entry/stop) — never a substituted one.
        A trade whose stop had NO level behind it still gets a record, with
        `level_backed` false, because that is the control the falsification
        needs.
        """
        if analysis is None:
            return None
        try:
            entry_f = float(entry_price)
            stop_f = float(stop_loss)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(entry_f) or entry_f <= 0 or not math.isfinite(stop_f) or stop_f <= 0:
            return None
        is_short = direction == "short"
        level = self._level_backing_stop(analysis, entry_f, stop_f, is_short)
        record = describe_stop_level_basis(
            level_price=level,
            stop_loss=stop_f,
            entry_price=entry_f,
            computed_levels=getattr(analysis, "computed_levels", None),
            computed_level_touches=getattr(analysis, "computed_level_touches", None),
            is_short=is_short,
        )
        # The ATR precondition `shipped_stop_rule` applies is an EXEMPTION
        # question, not a structure question, so it is recorded rather than
        # allowed to suppress the record: without it a later reader cannot
        # tell "no level" from "level, but no ATR to be exempt from".
        record["shipped_stop_rule"] = self.shipped_stop_rule(
            analysis,
            entry_f,
            stop_f,
            direction,
        )
        record["levels_coverage"] = getattr(analysis, "levels_coverage", None)
        try:
            return json.dumps(record, sort_keys=True)
        except (TypeError, ValueError):
            return None

    def _resolve_stop(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        entry_price: float,
    ) -> float | None:
        """Priority: target's suggested stop → the technical analyst's stop →
        None, which `_widen_stop_past_noise` then READS FROM THE INSTRUMENT.

        Until 2026-09-12 a None here was a refusal ("no synthesized
        fallback"), on the argument that a stop nobody derived from the
        chart cannot be risk-sized honestly. Item 54 (docs/WORK.md) replaced
        that: the stop is always derivable — the wider of the ATR noise band
        and the signal bar's far edge, both read from the bars — and the
        thing that cannot be sized honestly is a stop WIDER than the
        instrument's own range -- which item 54 answered with a refusal
        (`STOP_REFUSAL_WIDER_THAN_REACH`) that was itself DELETED on
        2026-09-26 under board item 56 route (c), the ratified answer being
        to size down instead. No width refusal remains anywhere in this
        module. The old flat `entry * 0.95` fallback stays gone; nothing
        here is a percentage.
        """
        if target.suggested_stop_price and target.suggested_stop_price > 0:
            return float(target.suggested_stop_price)
        if analysis and analysis.stop_loss and analysis.stop_loss > 0:
            return float(analysis.stop_loss)
        logger.info(
            "Constructor: %s has no typed stop (suggested_stop_price and "
            "analysis.stop_loss both absent) — it will be read from the "
            "instrument (item 54).",
            target.symbol,
        )
        return None
