"""Entry price + stop resolution for the position builder (bodies moved verbatim
from src/portfolio_constructor/stops.py). Standalone: every collaborator is an
explicit keyword-only argument, no TradingPipeline and no host object behind it."""

from __future__ import annotations

import math
import re
from src.data.levels import FAULT_NO_ENTRY, FAULT_NO_PRICE, derive_structural_target, touch_probability
from src.models import TargetPosition, TechAnalysisResult, TradeDecision
from src.risk.constants import reward_risk_floor_applies
import src.portfolio_constructor.absolute_floor_record as absolute_floor_record
from src.portfolio_constructor.config import logger
from src.portfolio_constructor.config import (
    STOP_RULE_LEVEL_HONOURED,
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_ATR_BAND,
    STOP_RULE_OUTSIDE_BAND,
    STOP_REFUSAL_WRONG_SIDE,
    STOP_RULE_SIGNAL_BAR,
    STOP_REFUSAL_STOP_NOT_FINITE,
    STOP_REFUSAL_ENTRY_NOT_FINITE,
    STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
    STOP_REFUSAL_NO_VALID_STOP,
    STOP_REFUSAL_NO_STRUCTURAL_TARGET,
)


class EntryStopResolver:
    def __init__(
        self,
        *,
        cfg,
        derive_target,
        note_data_fault,
        note_refusal,
        resolve_stop,
        unpriceable_symbols,
        reward_risk_at,
        derive_structural_stop_no_atr,
        level_backing_stop,
        stop_atr_multiple,
        widen_stop_past_noise=None,
    ):
        self.cfg = cfg
        self._derive_target = derive_target
        self._note_data_fault = note_data_fault
        self._note_refusal = note_refusal
        self._resolve_stop = resolve_stop
        self._unpriceable_symbols = unpriceable_symbols
        self._reward_risk_at = reward_risk_at
        self._derive_structural_stop_no_atr = derive_structural_stop_no_atr
        self._level_backing_stop = level_backing_stop
        self._stop_atr_multiple = stop_atr_multiple
        if widen_stop_past_noise is not None:
            self._widen_stop_past_noise = widen_stop_past_noise  # only a non-shim override

    def _resolve_entry_and_stop(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        market_price: float | None,
        regime: str | None = None,
    ) -> tuple[float | None, float | None]:
        """Entry and a validated stop, or (None, None).

        Direction-aware (D4, Stage 3): for a long the stop must sit strictly
        BELOW entry (existing behaviour, unchanged); for a short it must sit
        strictly ABOVE entry — a short's stop protects against the price
        RISING, so `stop_loss <= entry_price` is rejected instead of
        `stop_loss >= entry_price`.

        Extracted from `_build_buy` because risk-based sizing needs the stop
        distance one step earlier — the position's weight is not knowable until
        the stop is. `_build_buy`/`_build_short` call this too, so there is
        exactly one definition of what a tradeable entry/stop pair is.
        """
        is_short = target.direction == "short"

        # Item 120: a new name the caller could not price to a fresh today
        # print is refused HERE, before the TA-entry fallback below can size
        # it off a stale analyst number. The freshness resolver
        # (`src.data.live_price.resolve_live_price`) already rejected a quote
        # mid and a prior-session trade for this name — it returns a real
        # print or today's forming session bar, never a quote mid — so an
        # entry here means no usable today price of any kind. Falling back to
        # the TA entry would reintroduce exactly the bad-price sizing the
        # resolver exists to prevent. Filed under its own SIZING fault code
        # (FAULT_NO_PRICE / FAULT_STALE_PRICE, carried from the caller) so it
        # routes through the existing unmeasurable-drop path
        # (`_record_constructor_drops` / `_alert_unmeasurable_symbols`) and
        # is never counted as a trade the desk judged.
        fault_code = self._unpriceable_symbols.get(target.symbol.strip().upper())
        if fault_code is not None:
            self._note_data_fault(
                target.symbol,
                target.direction,
                fault_code,
                "DATA FAULT: no fresh today print to size this new name this "
                "session (a quote mid or a prior-session price is not a "
                "sizing reference) — the buy cannot be sized without "
                "inventing a price, so the name is refused as unmeasurable "
                "rather than mis-sized",
            )
            return (None, None)

        entry_price = 0.0
        if market_price and market_price > 0:
            entry_price = float(market_price)
        elif analysis and analysis.entry_price:
            entry_price = float(analysis.entry_price)
            logger.info(
                "Constructor: no live market_price for %s, using TA entry $%.2f",
                target.symbol,
                entry_price,
            )
        if entry_price <= 0:
            # A listed instrument always has a price. No live quote AND no
            # analyst entry is a data fault on this desk's side, the same
            # class `derive_structural_target` would name one line later
            # (FAULT_NO_ENTRY) — recorded and alerted as such, never as a
            # trade the constructor judged. Still no trade.
            self._note_data_fault(
                target.symbol,
                target.direction,
                FAULT_NO_ENTRY,
                "DATA FAULT: no live market price and no analyst entry "
                "price — the symbol cannot be priced, let alone measured",
            )
            return (None, None)

        # Round FIRST, then validate: the TradeDecision ships
        # round(stop_loss, 2), so validating the unrounded value let a stop
        # that rounds UP to exactly the entry price through the
        # `stop_loss < entry_price` check (e.g. entry $10.00, stop $9.999 →
        # ships $10.00 == entry → risk_per_share = 0, and a stop at the entry
        # fires on the first tick down). 2026-07-16 audit.
        #
        # No young-listing count gate here any more (board item 180, owner
        # ruling 2026-09-25): a short history is NOT refused on a bar count.
        # Indicators degrade to the bars that exist upstream (`ma_200` = None
        # under 200 sessions), and a name too young to read ANY stop from is
        # caught below by the stop-readability rule (`_derive_structural_stop_
        # no_atr` / `STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY`), on what
        # the trade needs rather than on a calendar count.

        # The target is derived BEFORE the stop is finalised, because the
        # reward:risk check inside `_widen_stop_past_noise` needs a real
        # target to measure against. It depends only on entry, direction and
        # the chart — never on the stop — so there is no circularity.
        derivation = self._derive_target(
            target.symbol,
            analysis,
            entry_price,
            target.direction,
        )
        if derivation.refused:
            # A data fault has already been recorded and logged as
            # UNMEASURABLE by `_derive_target`; only a genuine refusal is
            # logged here as a rejection. Both fail closed. A `no_target`
            # derivation (measured chart, no level — owner rule 2026-10-09)
            # is NOT refused and continues to the stop with no take-profit.
            if not derivation.fault:
                # Board item 10 (2026-09-14, second pass): this used to be a
                # bare `logger.warning`. It DOES match the capture's regex,
                # so the drop was never invisible — but it reached the
                # database as a generic `constructor_dropped` row carrying a
                # sentence, indistinguishable by `reason` from six other
                # causes. `derivation.refusal` is already a machine code
                # (`src/data/levels.py`), so file that one rather than mint
                # a synonym for it.
                self._note_refusal(
                    target.symbol,
                    target.direction,
                    derivation.refusal or STOP_REFUSAL_NO_STRUCTURAL_TARGET,
                    f"no take-profit could be computed from structure at the "
                    f"${entry_price:,.2f} entry: {derivation.detail}",
                )
            return (None, None)

        stop_loss = self._resolve_stop(target, analysis, entry_price)
        stop_loss = self._widen_stop_past_noise(
            target.symbol,
            analysis,
            entry_price,
            stop_loss,
            regime=regime,
            direction=target.direction,
            target_price=derivation.price,
            # The PM's sub-floor catalyst gate has already run by the time a
            # target reaches here; this is where its verdict is honoured
            # rather than silently re-litigated. `getattr` because
            # `_resolve_entry_and_stop` is also called with hand-built
            # targets from the backtest shim and older tests.
            subfloor_catalyst_exception=bool(getattr(target, "subfloor_catalyst_verified", False)),
            # The MEASURED half of the trend-trade test (2026-09-11, funnel
            # item 6 + item 1(d)): `_derive_target` has just run the desk's
            # own level computation for this exact trade, and
            # `level_used is None` means it found nothing in the trade's
            # direction — a computed absence of a ceiling, not a label.
            # `src.risk.constants.is_trend_trade` is the one definition both
            # this and `derive_structural_target` consume.
            structural_ceiling=(derivation.level_used is not None),
        )
        if stop_loss is not None:
            stop_loss = round(stop_loss, 2)
        if is_short:
            invalid = stop_loss is None or stop_loss <= 0 or stop_loss <= entry_price
        else:
            invalid = stop_loss is None or stop_loss <= 0 or stop_loss >= entry_price
        if invalid:
            # Board item 10 (2026-09-14, second pass). THE BACKSTOP. Every
            # named stop refusal in `_widen_stop_past_noise` arrives here as
            # a plain `None`, so this line is the last thing many drops say
            # — and it said it only in prose. Filed as a code now, but
            # `only_if_unrecorded` so the precise upstream reason (wrong
            # side, wider than reach, no volatility reading, ...) is never
            # overwritten by this generic one. What reaches here with
            # nothing already recorded is a stop that is genuinely just on
            # the wrong side of, or equal to, the entry.
            self._note_refusal(
                target.symbol,
                target.direction,
                STOP_REFUSAL_NO_VALID_STOP,
                f"no valid stop {'above' if is_short else 'below'} the "
                f"${entry_price:,.2f} entry (stop={stop_loss}). A stop that "
                f"does not sit on the protective side of the entry protects "
                f"nothing, so the trade is refused rather than shipped.",
                only_if_unrecorded=True,
            )
            logger.warning(
                "Constructor: %s %s rejected — no valid stop %s entry (entry=$%.2f, stop=%s)",
                "SHORT" if is_short else "BUY",
                target.symbol,
                "above" if is_short else "below",
                entry_price,
                stop_loss,
            )
            return (None, None)
        return (entry_price, stop_loss)

    def real_reward_risk_preview(
        self,
        analysis: TechAnalysisResult | None,
        direction: str,
        regime: str | None = None,
    ) -> float | None:
        """Reward:risk at the entry and stop `entry_stop_preview` resolves."""
        preview = self.entry_stop_preview(analysis, direction, regime=regime)
        if preview is None:
            return None
        entry_price, honoured_stop, target = preview
        ratio = self._reward_risk_at(entry_price, honoured_stop, target, direction == "short")
        return None if ratio is None else round(ratio, 2)

    def entry_stop_preview(
        self,
        analysis: TechAnalysisResult | None,
        direction: str,
        regime: str | None = None,
    ) -> tuple[float, float, float] | None:
        """The reward:risk this candidate would actually clear at
        construction time — same target derivation and stop-widening
        `construct_orders` applies — computed BEFORE a `TargetPosition`
        exists.

        Exists to close a gap found in a 2026-09-04 audit: the 2026-09-01
        decision that reward:risk must be "evidence, never arithmetic"
        (this class's own `_derive_target` / `_widen_stop_past_noise`) was
        applied to order construction but never reached
        `PortfolioManagerAgent.candidate_eligibility` /
        `_apply_subfloor_catalyst_rule` — the EARLIER gate that decides
        which candidates even reach construction. That gate kept reading
        `TechAnalysisResult.risk_reward`, which is real arithmetic but over
        the ANALYST's own guessed target, never checked against structure
        (see that field's docstring). On a measured real day the two gates
        passed disjoint sets — zero overlap — because the first gate
        screened out exactly the names the second would have allowed
        through, and vice versa. This method gives the earlier gate the
        SAME real number the later one uses, by calling the same
        `_derive_target` / `_widen_stop_past_noise` this class already
        runs, rather than a second copy of that logic.

        Two inputs are necessarily earlier-stage approximations, because
        nothing later exists yet at PM eligibility time:
          - entry is the analyst's SNAPSHOT `entry_price`, not the live
            market price `construct_orders` prices the real order at —
            that price does not exist until the PM has decided to trade
            the name.
          - the stop is `analysis.stop_loss`, mirroring `_resolve_stop`'s
            second-priority source. There is no `TargetPosition.
            suggested_stop_price` yet, because the PM has not proposed one.
        Both are re-resolved for real at construction time against the
        live price and (if the PM supplies one) its own suggested stop, so
        a name can still legitimately move between preview and shipped
        order — but it will no longer move because of TWO DIFFERENT
        DEFINITIONS of reward:risk, which is the defect this closes: the
        derived target and the noise-floor stop widening were never
        applied before the PM's gate at all.

        None means "cannot judge" — the same fail-closed contract as
        `_widen_stop_past_noise`, which this calls. As of 2026-09-11
        (docs/WORK.md item 1(d)) it no longer ALSO means "under this desk's
        floor": a real but weak range payoff returns its real number, and a
        breakout returns its number without any floor having been consulted.
        Callers must not read a returned number as "eligible" or a None as
        "sub-floor" — see `PortfolioManagerAgent.candidate_eligibility`.
        """
        if analysis is None:
            return None
        entry_price = getattr(analysis, "entry_price", None)
        if not entry_price or entry_price <= 0:
            return None
        entry_price = float(entry_price)
        is_short = direction == "short"

        # No young-listing count gate here (board item 180, owner ruling
        # 2026-09-25): the preview mirrors `_resolve_entry_and_stop`, which no
        # longer refuses on a bar count. A name too young to read any stop from
        # still previews None below when the stop-readability rule cannot place
        # a stop, so the PM is not shown a candidate the constructor would drop.

        derivation = self._derive_target(
            analysis.symbol,
            analysis,
            entry_price,
            direction,
        )
        if derivation.refused:
            # A `no_target` derivation continues: its ratio previews None,
            # which the PM gate reads as "no ratio", never as a block.
            return None

        raw_stop = getattr(analysis, "stop_loss", None)
        try:
            raw_stop = float(raw_stop) if raw_stop else None
        except (TypeError, ValueError):
            raw_stop = None
        if raw_stop is not None and raw_stop <= 0:
            raw_stop = None
        # A missing stop is no longer a None here: `_widen_stop_past_noise`
        # reads one from the instrument (item 54) exactly as it will at
        # construction time. A stop on the WRONG side is still refused there.
        if raw_stop is not None:
            # Geometry must already hold before any widening is attempted —
            # a stop on the wrong side of entry is not a candidate for the
            # noise floor, it is not a stop at all.
            if is_short:
                if raw_stop <= entry_price:
                    return None
            elif raw_stop >= entry_price:
                return None

        honoured_stop = self._widen_stop_past_noise(
            analysis.symbol,
            analysis,
            entry_price,
            raw_stop,
            regime=regime,
            direction=direction,
            target_price=derivation.price,
            structural_ceiling=(derivation.level_used is not None),
        )
        if honoured_stop is None:
            # Ungeometric, or (Type A only) an unmeasurable ratio against the
            # real target and the stop that would actually ship —
            # `_widen_stop_past_noise` fails closed on both. As of item 1(d)
            # it no longer returns None merely for a sub-floor ratio, so a
            # weak-but-real range payoff now yields a NUMBER here (which the
            # ranking consumes) rather than a None the gate reads as
            # "ineligible".
            return None
        return entry_price, float(honoured_stop), derivation.price

    def _widen_stop_past_noise(
        self,
        symbol: str,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        stop_loss: float | None,
        regime: str | None = None,
        direction: str = "long",
        target_price: float | None = None,
        subfloor_catalyst_exception: bool = False,
        structural_ceiling: bool | None = None,
    ) -> float | None:
        """Decide the stop that will actually ship, and say which rule did it.

        Spec §12.1 (2026-09-01). **A stop sitting at a level the system
        COMPUTED is honoured however tight; the ATR band applies only when
        nothing computed backs it.**

        What this replaced, and why. `min_stop_atr_multiple` used to
        overwrite the structural stop unconditionally whenever the level sat
        closer to entry than the band. After that the stop was not at
        anything real — and `min_reward_risk_after_widening` was then judged
        against that fabricated number, so the reward was being divided by a
        risk nobody would ever take. The arithmetic is unforgiving: over a
        ~15-session hold a stock travels ~3.9 ATR, so against a 3.0-ATR stop
        the best achievable ratio is ~1.29 against a 1.5 floor. Essentially
        no trade could pass, and on 2026-09-01 the desk reviewed 38 qualified
        signals and placed ZERO trades. Tuning the multiple down to 2.0 was
        considered and explicitly REJECTED by the owner: it keeps a floor
        that still cannot tell a level-backed tight stop from an arbitrary
        one. Neither threshold moved. What changed is WHICH stop the
        arithmetic is performed on.

        "Verified" is doing real work here — see `_level_backing_stop`. The
        level must have come out of `find_structural_levels` and been
        attached in Python. A stop the analyst merely placed close, with
        nothing computed under it, still gets the full band.

        **The 1x ATR floor below is an addition beyond the ratified §12.1
        wording, and it is deliberate.** §12.1 argues the exemption is safe
        because `config/prompts/tech_analyst.md` already forbids a stop
        inside 1*ATR of entry. That is a PROMPT — an instruction to a
        language model — and Invariant 2 of the spec requires deterministic
        Python protections to be the final authority and to fail closed. A
        prompt is not a deterministic guarantee. Meanwhile a genuine support
        level sitting 0.2 ATR under entry is real structure AND a guaranteed
        whipsaw; both things are true at once. So a level-backed stop is
        honoured however tight DOWN TO `absolute_min_stop_atr_multiple`
        ATRs, and inside that it is widened to exactly that floor — never to
        the full `min_stop_atr_multiple` band, which is the behaviour being
        removed.

        For an unbacked stop nothing has changed: it is pushed out to
        `min_stop_atr_multiple` ATRs, exactly as before. That corrects the
        case the evidence says was routine — stops a median 1.7 ATRs from
        entry, a coin flip on a normal day's range rather than a thesis
        invalidation, which then forced enormous positions to reach any
        meaningful risk. This never invents a stop where none exists, and
        never pulls a wide stop tighter.

        D5 (Stage 3): direction-aware. A long's stop is pushed DOWN, away
        from entry; a short's stop is pushed UP, away from entry, by the
        same number of ATRs — mirrored, not reflected through a different
        rule. A computed reward:risk on a widened short is ranking
        information, not a refusal, exactly as for a long.

        **2026-09-11, docs/WORK.md item 1 part (d) — this function no longer
        REFUSES anything on reward:risk.** Owner decision, by setup type:

          * **Type B / trend** — no reward:risk comparison runs at all.
            Nothing overhead is expected to stop the stock; the position is
            managed by a trailing stop with no fixed target
            (`src/risk/trailing.py`), so the only number available to put in
            the numerator is invented to make the ratio computable. Decided
            by `src.risk.constants.is_trend_trade`, which accepts EITHER the
            analyst's `setup_type="breakout"` label OR
            `structural_ceiling=False` — the desk's own level computation
            having found nothing in this trade's direction. The caller
            supplies that measured fact from the same `_derive_target`
            derivation it already ran, so this exemption and
            `derive_structural_target`'s measured-move projection turn on one
            shared definition rather than two that can drift.
          * **Type A / range** — the ratio is still computed from this
            trade's own real support (the shipping stop) and real resistance
            (`_derive_target`). A computed result is never a refusal and is
            never a size-cap (owner 2026-09-17). It reaches ranking as a
            real ordering signal (`real_reward_risk_preview` ->
            `src/verdicts.py::rank_verdicts`).

        What still refuses, unchanged: every RISK-side check above — a
        wrong-side stop, a non-finite stop or entry. An UNMEASURABLE ratio
        is recorded, not refused (owner 2026-09-17 overnight bind: honesty
        about a payoff without a number is a ranking hint, not a gate).
        `subfloor_catalyst_exception` is inert theater around a dead floor
        and does not admit, refuse, or resize anything.

        `target_price` (2026-09-01) is the DERIVED target — computed from
        structure by `_derive_target`. It has to be passed in rather than
        read off the analysis, because the whole point of the change is that
        `analysis.reference_target` is the language model's guess and this
        gate is arithmetic. When it is omitted there is no target: the old
        read of `analysis.reference_target` is gone (2026-10-09), so the
        backtest engine, which handed its own nearest level over on that
        shim (`src/backtest/engine.py`), now gets no take-profit and no
        reward:risk log line from this step.

        **2026-09-12, docs/WORK.md item 54 — the width gate, and a stop
        that is always derivable.** Sourced research (Bulkowski on gaps,
        George & Hwang 2004 on nearness to the 52-week high, Kullamägi's
        own stop discipline) falsified the one-day "no floor, no trade"
        rule: what published practice constrains is the stop's WIDTH, not
        whether a level exists under it. So: (1) an unbacked stop is placed
        at the wider of the noise band and the signal bar's far edge, both
        read from the instrument; (2) a missing stop is placed the same way
        rather than refused; (3) whatever placed the stop, a width past the
        instrument's own reach over the trade's horizon was REFUSED by code
        (`STOP_REFUSAL_WIDER_THAN_REACH`). **That refusal was DELETED on
        2026-09-26 (board item 56, route (c)) after refusing nothing in 648
        recorded sized stops** — this function no longer refuses on distance
        at all. Width is answered by `_plan_risk_targets` sizing down
        (§2.1); `STOP_RULE_OUTSIDE_BAND` still keeps a wide typed stop.

        **2026-09-02 — the reward:risk floor now runs on EVERY path, not
        only when this function moved the stop.** It previously lived
        inside the two widening branches, behind two unnamed early returns
        ("already outside the noise band", "no ATR reading"). A stop that
        was already wide enough therefore reached the broker with NO
        deterministic reward:risk check at all, and the name
        `min_reward_risk_after_widening` recorded that as if it were the
        design. It was not caught because it fails OPEN and quietly.

        Measured against the pre-reset production database
        (`data/resets/20260902T181859Z`), 2026-08-18 to 2026-09-02:
        **14 of the 49 constructed entry orders — 29% — shipped with a
        reward:risk below the 1.5 floor**, as low as 0.43 (XLB,
        2026-09-02) and 0.50 (NET, 2026-08-27). The floor's own refusal
        message appears ZERO times in the whole production log history
        before 2026-09-02. Two of the 14 reached the broker; XLE on
        2026-08-21 FILLED, 9 shares at $64.26, on a reward:risk of 0.81.
        Everything else was stopped downstream by the Risk Manager — a
        language model — or by the 1.2 execution-time belt in
        `src/pipeline_stages.py`. A deterministic floor was being enforced
        by an LLM's prose, which is exactly the inversion Invariant 2
        forbids.

        So the ratio is now measured against the stop that will ship,
        whichever rule placed it, and refused by a code naming that rule.
        The threshold did not move and no path became more permissive.
        """
        # Non-finite guards come FIRST, and they REFUSE rather than pass the
        # value through. `nan <= 0` is False, so a NaN price sails through
        # every ordering comparison in this function AND through the
        # caller's `stop_loss >= entry_price` validity check in
        # `_resolve_entry_and_stop` — a NaN stop was reaching
        # `TradeDecision` intact. The same NaN then makes `ratio < floor`
        # False at the gate below. Every one of those failures is silent and
        # every one of them is permissive, which is why this is a refusal
        # and not a passthrough.
        if stop_loss is not None and not math.isfinite(stop_loss):
            self._note_refusal(
                symbol,
                direction,
                STOP_REFUSAL_STOP_NOT_FINITE,
                f"the stop price supplied for this trade is not a finite "
                f"number ({stop_loss!r}), so no distance can be measured "
                f"from it. Refused rather than let through comparisons a "
                f"NaN silently passes.",
            )
            return None
        if not math.isfinite(entry_price):
            self._note_refusal(
                symbol,
                direction,
                STOP_REFUSAL_ENTRY_NOT_FINITE,
                f"the entry price is not a finite number ({entry_price!r}), "
                f"so no stop distance can be measured from it.",
            )
            return None
        # Unchanged legacy passthrough: a non-positive stop or entry is the
        # caller's to reject, and it does (`stop_loss <= 0` there is a real
        # comparison on a real number). A MISSING stop is no longer passed
        # through as None (item 54, 2026-09-12): it is read from the
        # instrument below, in the same branch that widens an unbacked one.
        if entry_price <= 0 or (stop_loss is not None and stop_loss <= 0):
            return stop_loss

        is_short = direction == "short"
        # A stop on the WRONG side of entry is refused here rather than
        # widened into validity. Found 2026-09-01: widening moves the stop to
        # `entry +/- multiple * ATR`, which is unconditionally on the correct
        # side, so a short handed a stop BELOW its entry came out of this
        # function with a valid-looking stop above it — silently repairing
        # exactly the nonsense the caller's side check exists to catch.
        # `test_short_stop_at_or_below_entry_is_rejected` only passed because
        # its fixture carried no ATR, which is not a state production reaches
        # (`atr_14` is Python-set on every analysis). Widening corrects a stop
        # that is too CLOSE; it must not invent one that is on the wrong side.
        if stop_loss is None:
            pass  # nothing typed: derived from the instrument below
        elif is_short and stop_loss <= entry_price:
            self._note_refusal(
                symbol,
                direction,
                STOP_REFUSAL_WRONG_SIDE,
                f"the stop ${stop_loss:,.2f} is at or below the "
                f"${entry_price:,.2f} entry on a SHORT, so it protects "
                f"nothing. Refused rather than widened into validity.",
            )
            return None
        elif not is_short and stop_loss >= entry_price:
            self._note_refusal(
                symbol,
                direction,
                STOP_REFUSAL_WRONG_SIDE,
                f"the stop ${stop_loss:,.2f} is at or above the "
                f"${entry_price:,.2f} entry on a BUY, so it protects "
                f"nothing. Refused rather than widened into validity.",
            )
            return None

        atr = getattr(analysis, "atr_14", None) if analysis else None
        try:
            atr = float(atr) if atr is not None else None
        except (TypeError, ValueError):
            atr = None
        if atr is not None and (not math.isfinite(atr) or atr <= 0):
            atr = None

        # The target is resolved BEFORE any branch, because the reward:risk
        # floor now applies on every one of them. It used to be resolved
        # after two early returns had already carried most stops past it.
        # Only the MEASURED target counts. The analyst's `reference_target`
        # is evidence, never a take-profit or a reward:risk input (owner
        # rule 2026-09-12: unverifiable numbers never rank or size): when
        # nothing is measured there is no target.
        had_target = target_price is not None
        try:
            target_price = float(target_price) if target_price else None
        except (TypeError, ValueError):
            target_price = None

        side_word = "above" if is_short else "below"
        side_label = "SHORT" if is_short else "BUY"

        # -------------------------------------------------------------
        # Decide the shipping stop, and name the rule that placed it.
        # Exactly one of these branches runs (no ATR; nothing typed, read
        # from the instrument; outside the band; level-honoured; absolute
        # floor; widened to the band or the signal bar). `honoured` is what
        # ships; `rule` is why. NO WIDTH GATE JUDGES THE RESULT ANY MORE:
        # `STOP_REFUSAL_WIDER_THAN_REACH` went on 2026-09-26 (board item 56
        # route (c)) and the no-ATR branch's
        # `STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR` went on 2026-09-30 (board
        # item 185). What judges the geometry now is the single reward:risk
        # gate at the bottom -- and that gate is OFF for a Type B /
        # breakout trade by design (`reward_risk_floor_applies`), so on a
        # breakout nothing judges stop WIDTH at all. Width is answered by
        # `_plan_risk_targets` sizing down (spec 2.1) and, at the extreme,
        # by `position_sized_to_zero`, which is the ratified answer and the
        # reason the refusals went.
        # -------------------------------------------------------------
        if atr is None:
            # No volatility reading -- but a missing ATR is NEVER a reason to
            # skip protection or to size off an unverifiable typed number
            # (owner ruling 2026-09-25, board item 80, reworked). ATR is
            # Python-set upstream from the same bars that produced this
            # analysis; when it is gone the raw OHLC series is gone with it,
            # BUT the price STRUCTURE those bars yielded survives on the
            # analysis object -- `computed_levels` (every level
            # `find_structural_levels` clustered over the full fetched history,
            # with `computed_level_touches`) and `signal_bar_low`/
            # `signal_bar_high` (the last completed bar). "There are always
            # levels, even from a few days ago." So DERIVE the protective stop
            # from that structure and HOLD; refuse this one name ONLY when
            # no structural level is readable at all. There is no longer a
            # second, width-based refusal on this branch: board item 185
            # deleted `STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR` on 2026-09-30
            # (see the deletion site below). Never a book-wide halt -- a
            # transient ATR loss drops only the names it actually hits.
            #
            # Published basis (cited, not re-derived here): a swing-low /
            # structure stop placed just beyond the nearest confirmed level
            # (IG, LuxAlgo), degrading to a prior-bar low stop when only the
            # last completed bar is trustworthy (Ticker Daily; a Donchian
            # channel-low collapses to the same thing as the window shrinks).
            # The buffer past the level is owner-appetite, not doctrine --
            # `ConstructorConfig.structural_stop_buffer_pct`, ledgered with an
            # open question. O'Neil's 7-8% percentage stop is deliberately NOT
            # used even as a floor: it is a fitted number, and inventing one
            # conflicts with desk doctrine (no arbitrary numbers) -- SKIP is
            # preferred over an invented stop when no level can be read.
            derived = self._derive_structural_stop_no_atr(
                analysis,
                entry_price,
                is_short,
            )
            if derived is None:
                # No verified structural level on the protective side of entry,
                # and no usable signal-bar edge either: a monotonic move with
                # no intervening low, a chart with no structure, or zero
                # completed bars. Nothing measurable to protect this trade
                # with, so refuse THIS one name -- the ruling's genuine
                # skip-correct case, not a view on the idea. Per-symbol,
                # durable, never a halt.
                typed = (
                    ""
                    if stop_loss is None
                    else (
                        f" A stop was typed at ${stop_loss:,.2f}, but with no ATR "
                        f"and no readable structural level there is nothing to "
                        f"verify or place a stop from."
                    )
                )
                self._note_refusal(
                    symbol,
                    direction,
                    STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
                    f"there is no ATR reading for this name and no structural "
                    f"level (a computed level with enough touches, or the "
                    f"signal-bar edge) sits on the protective side of the "
                    f"${entry_price:,.2f} entry, so no stop can be read from "
                    f"the instrument to hold it.{typed} Not a view on the "
                    f"idea -- nothing measurable was available to protect it.",
                )
                return None
            level, honoured, rule = derived
            # NO WIDTH REFUSAL HERE ANY MORE -- removed 2026-09-30, board
            # item 185. There used to be one: a structural stop further
            # from entry than `1 - STOP_SANITY_FLOOR_FRACTION` (50%) was
            # filed as `STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR`. It borrowed
            # that fraction from the midday TRAIL_STOP typo guard, which
            # item 185 has now replaced with a reading off the instrument;
            # there is no ATR on this branch by construction, so the
            # borrowed fraction could not follow it, and the only way to
            # keep the gate was to invent an independent flat width bound,
            # which doctrine bars.
            #
            # Deleting it rather than re-picking it follows the ruling
            # already made about the gate further down this same method:
            # board item 56 route (c) deleted `STOP_REFUSAL_WIDER_THAN_REACH`
            # on the reasoning that "a wide stop is answered by
            # `_plan_risk_targets` sizing down -- the ratified spec 2.1
            # invariant, and what published practice prescribes -- never by
            # a refusal here". This gate was the same shape, and its own
            # comment already conceded the point: per-trade risk stays
            # bounded by fixed-fractional sizing whatever the width, so all
            # it added was a stub-size objection -- and that objection was
            # itself ruled a non-reason by board item 183 (Alpaca charges no
            # stock commission and the desk trades fractional shares, so a
            # small position is not costly to hold). Measured before
            # deleting: across every retained production log (2026-08-31 to
            # 2026-09-30) and the whole live trade/report history in
            # `data/quant_agent.db`, this refusal fired ZERO times.
            # `STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR` stays DEFINED because
            # tests reference it, exactly as
            # `STOP_REFUSAL_SECTOR_BELOW_MIN_ORDER` was kept after item 183.
            # WHAT STILL JUDGES THIS STOP, STATED HONESTLY. For a Type A
            # / range trade, the reward:risk gate at the tail of this
            # method: it runs on the no-ATR branch too and measures the
            # stop against the trade's own derived target rather than
            # against a chosen fraction of entry. For a Type B / breakout
            # trade -- the commonest setup on this desk -- it does NOT
            # run: `reward_risk_floor_applies` returns False for a trend
            # trade and its own docstring says that is the whole rule
            # (owner decision 2026-09-11, docs/WORK.md item 1 part (d)).
            # So on a breakout with no ATR reading, NOTHING judges the
            # width of this stop after the deletion above. That is
            # deliberate and it is the ratified answer, not an oversight:
            # width is answered by `_plan_risk_targets` holding the risked
            # dollars constant and buying fewer shares (spec 2.1), and at
            # the extreme by `position_sized_to_zero`. It is recorded here
            # rather than glossed because an earlier version of this
            # comment claimed the reward:risk tail still judged the stop,
            # which was untrue for exactly the commonest case.
            logger.info(
                "Constructor: %s %s had no ATR reading; protective stop "
                "derived from price structure at $%.2f [%s]%s and HELD -- a "
                "missing volatility reading is not a reason to skip protection "
                "(owner 2026-09-25, board item 80).",
                side_label,
                symbol,
                honoured,
                rule,
                f" (structural level ${level:.2f})" if level is not None else "",
            )
            # honoured/rule set above; falls through to the single
            # reward:risk tail, exactly like the ATR path. There is no
            # width gate left on any branch (board items 56 and 185), and
            # the reward:risk tail does NOT run on a Type B / breakout
            # trade -- see the note where the branches are introduced.
        else:
            multiple = self._stop_atr_multiple()
            band_edge = entry_price + multiple * atr if is_short else entry_price - multiple * atr
            # The instrument's own fallback (item 54): the WIDER of the
            # noise band and the signal bar's far edge — Kullamägi's
            # "low of the day" placement, read from the last completed bar
            # the analyst judged. Python-set beside the levels; None on an
            # older row or a hand-built object, in which case the band
            # alone decides, as it did before.
            bar_edge = getattr(
                analysis,
                "signal_bar_high" if is_short else "signal_bar_low",
                None,
            )
            try:
                bar_edge = float(bar_edge) if bar_edge is not None else None
            except (TypeError, ValueError):
                bar_edge = None
            if bar_edge is not None and (
                not math.isfinite(bar_edge)
                or bar_edge <= 0
                or (bar_edge <= entry_price if is_short else bar_edge >= entry_price)
            ):
                bar_edge = None
            bar_wins = bar_edge is not None and (bar_edge > band_edge if is_short else bar_edge < band_edge)
            fallback_edge = bar_edge if bar_wins else band_edge
            fallback_rule = STOP_RULE_SIGNAL_BAR if bar_wins else STOP_RULE_ATR_BAND
            level = None
            if stop_loss is None:
                # Nothing typed by the PM or the analyst. Read the stop from
                # the instrument rather than refuse: a stop is always
                # derivable (item 54). Nothing judges its WIDTH below any
                # more -- the width gate was deleted (board item 56 route
                # (c)); sizing answers a wide stop.
                honoured, rule = fallback_edge, fallback_rule
                logger.info(
                    "Constructor: %s %s had no stop from the PM or the "
                    "analyst; placed at $%.2f from the instrument [%s] "
                    "(%.2f x ATR band $%.2f%s).",
                    side_label,
                    symbol,
                    honoured,
                    rule,
                    multiple,
                    band_edge,
                    f", signal bar edge ${bar_edge:.2f}" if bar_edge is not None else "",
                )
                placed = True
                stop_loss = honoured
            else:
                placed = False
                outside_band = stop_loss >= band_edge if is_short else (band_edge <= 0 or stop_loss <= band_edge)
            if placed:
                pass  # read from the instrument above; nothing to widen
            elif outside_band:
                # Already further from entry than the noise band asks for.
                # Nothing to widen — but this is the path that used to
                # return before the floor ran, and it is the majority path.
                honoured, rule = stop_loss, STOP_RULE_OUTSIDE_BAND
            else:
                # ---------------------------------------------------------
                # §12.1 — is this stop sitting on something we COMPUTED?
                # ---------------------------------------------------------
                level = self._level_backing_stop(
                    analysis,
                    entry_price,
                    stop_loss,
                    is_short,
                )
                if level is not None:
                    honoured, rule = stop_loss, STOP_RULE_LEVEL_HONOURED
                    floor_multiple = self.cfg.absolute_min_stop_atr_multiple
                    hard_floor = entry_price + floor_multiple * atr if is_short else entry_price - floor_multiple * atr
                    inside_hard_floor = (
                        floor_multiple > 0
                        and hard_floor > 0
                        and (stop_loss < hard_floor if is_short else stop_loss > hard_floor)
                    )
                    honoured, rule = absolute_floor_record.noted(
                        inside_hard_floor=inside_hard_floor,
                        symbol=symbol,
                        side_label=side_label,
                        side_word=side_word,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        atr=atr,
                        level=level,
                        hard_floor=hard_floor,
                        floor_multiple=floor_multiple,
                        multiple=multiple,
                        band_edge=band_edge,
                    )
                else:
                    # Nothing computed backs this stop — widen it to the
                    # instrument's own fallback: the band, exactly as
                    # before §12.1, or the signal bar's far edge when that
                    # is wider (item 54).
                    honoured, rule = fallback_edge, fallback_rule
                    logger.info(
                        "Constructor: %s %s stop widened $%.2f → $%.2f "
                        "(%.1f%% %s entry) [%s] — no computed structural "
                        "level sits at it, and it was placed inside %.2f x "
                        "ATR of $%.2f (%s setup, %s tape). A stop nothing on "
                        "the chart defends does not earn the §12.1 exemption.%s",
                        side_label,
                        symbol,
                        stop_loss,
                        honoured,
                        100 * abs(entry_price - honoured) / entry_price,
                        side_word,
                        rule,
                        multiple,
                        atr,
                        getattr(analysis, "setup_type", None) or "unknown",
                        regime or "unknown",
                        (
                            f" The signal bar's {'high' if is_short else 'low'} "
                            f"${bar_edge:.2f} sits past the band's ${band_edge:.2f}, "
                            f"so the bar decides."
                        )
                        if bar_wins
                        else "",
                    )

        # -------------------------------------------------------------
        # The stop-width READING (item 56, 2026-09-13). NOT a gate.
        # -------------------------------------------------------------
        # There USED to be a refusal here: a stop wider than
        # `horizon_reach` (ATR x sqrt(horizon) x 1.5) was turned away as
        # "a stop price cannot reach". It was deleted on 2026-09-26 (board
        # item 56, route (c)) and the reasoning is at
        # `STOP_REFUSAL_WIDER_THAN_REACH` above: in 648 recorded sized
        # stops it refused nothing, it could not fire on the desk's own
        # fallback stop at any horizon it has ever been given, and the
        # threshold it turned on was a multiple nobody could source.
        # A wide stop is answered by `_plan_risk_targets` sizing down --
        # the ratified spec 2.1 invariant, and what published practice
        # prescribes -- never by a refusal here.
        #
        # What survives is the READING, and it is the whole point: the
        # probability this stop is TOUCHED inside the trade's own horizon.
        # Reflection principle + the range-to-sigma identity, both
        # published, no chosen constant -- `levels.touch_probability`
        # carries the citations. Recorded on EVERY stop the desk sizes,
        # because "how unlikely must a touch be before a stop is not a
        # stop" is still an open question and this is the evidence that
        # would answer it.
        if atr is not None:
            stated_horizon = getattr(
                analysis,
                "expected_horizon_sessions",
                None,
            )
            width = abs(entry_price - honoured)
            p_touch = touch_probability(
                width / atr,
                min(
                    int(stated_horizon or 0) or 1,
                    max(1, int(self.cfg.max_target_horizon_sessions)),
                ),
            )
            if p_touch is not None:
                logger.info(
                    "Constructor: %s stop width %.2f x ATR over a "
                    "%s-session horizon — touch probability %.1f%% "
                    "(item 56 reading; no width refusal exists — a wide "
                    "stop is answered by a smaller position).",
                    symbol,
                    width / atr,
                    stated_horizon,
                    100 * p_touch,
                )

        # -------------------------------------------------------------
        # ONE reward:risk gate, on the stop that will actually ship.
        # -------------------------------------------------------------
        # Both sides of this ratio are measured: the stop is whatever the
        # branch above decided, and the target is where structure says
        # price travels (`_derive_target`). The refusal is about GEOMETRY
        # and says so — this entry does not support this trade — not about
        # a model having guessed a poor target.
        setup_type = getattr(analysis, "setup_type", None) if analysis else None
        if not reward_risk_floor_applies(
            setup_type,
            structural_ceiling=structural_ceiling,
        ):
            # TYPE B / BREAKOUT — no reward-side refusal here at all
            # (owner 2026-09-11, restated 2026-09-17). Nothing overhead is
            # expected to stop this stock. The RISK side is unchanged and
            # has already run above.
            # The sentence used to end "There is no overhead level to
            # measure a reward against" UNCONDITIONALLY. That is a claim
            # about the chart, and on the label branch nothing had measured
            # it: `is_trend_trade` returns True on `setup_type="breakout"`
            # before it ever consults `structural_ceiling`. META,
            # 2026-09-21, is the measured case — the derivation had found a
            # real level and recorded `basis="structural_level"`, and this
            # line asserted in the same record that no such level existed.
            # Both cannot be true; the derivation was the true one. The log
            # now states which of the two grounds the exemption rests on
            # and never denies structure the desk itself computed.
            # `structural_ceiling`, NOT the target price. The two come
            # apart on exactly the branch this sentence is about: a real
            # `measured_move` derivation (the chart yielded levels, none
            # of them overhead) returns a finite positive PRICE with
            # `level_used=None`, so testing the price would assert a
            # level was found on the one branch where none was — swapping
            # one false sentence for another. Verified by construction:
            # entry 100, levels [85, 92], ATR 2, 25 sessions returns
            # price 110.0 and level_used None. `structural_ceiling` is
            # computed by all four call sites as
            # `derivation.level_used is not None`, which is the question
            # actually being asked here. `None` means the caller did not
            # measure it, so it is never reported as a finding either way.
            if structural_ceiling is True:
                why = (
                    "the desk's own level scan DID find a level overhead, "
                    "but a breakout is managed by trailing rather than to "
                    "that level"
                )
            elif structural_ceiling is False:
                why = "the desk's own level scan found nothing overhead to measure a reward against"
            else:
                why = (
                    "whether anything stands overhead was not measured at "
                    "this call site, so the exemption rests on the "
                    "analyst's setup label alone"
                )
            logger.info(
                "Constructor: %s %s stop $%.2f [%s] shipped with NO "
                "reward:risk check — breakout setup: %s, so approval rests "
                "on the risk side alone.",
                side_label,
                symbol,
                honoured,
                rule,
                why,
            )
            return honoured
        reward_risk = self._reward_risk_at(
            entry_price,
            honoured,
            target_price,
            is_short,
        )
        if reward_risk is None and had_target:
            # Recorded fact, not a refuse. Owner 2026-09-17: unmeasurable
            # payoff honesty may stay as ranking hint with zero refuse,
            # zero size floor, zero sub-floor branch.
            logger.info(
                "Constructor: %s %s stop $%.2f [%s] shipped with "
                "unmeasurable reward:risk — a target was supplied "
                "(%r) but payoff against this stop at $%.2f cannot be "
                "computed. Honesty about unknown geometry, not a floor.",
                side_label,
                symbol,
                honoured,
                rule,
                target_price,
                entry_price,
            )
        return honoured
