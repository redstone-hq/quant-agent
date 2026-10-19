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

import math
import logging
import re
from collections.abc import Sequence

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
from src.portfolio_constructor.sector_weights import (
    _current_sector_weights,
    _note_reducing_order_sectors,
)
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

logger = logging.getLogger(__name__)

# Every public and private name the old single module defined, re-exported
# so existing imports and patch targets keep working unchanged.
from src.portfolio_constructor.config import (
    SINGLE_NAME_BINDING_SENTENCE,
    single_name_crossover_stop_pct,
    MECHANICAL_SIZE_DOWN_TRIGGER,
    format_mechanical_size_down_reason,
    _size_down_checkable,
    _lookup_existing_risk,
    _named_reduction_trigger,
    _DropReasonCapture,
    STOP_RULE_LEVEL_HONOURED,
    STOP_RULE_ABSOLUTE_FLOOR,
    STOP_RULE_ATR_BAND,
    STOP_RULE_OUTSIDE_BAND,
    STOP_RULE_NO_VOLATILITY,
    STOP_REFUSAL_WRONG_SIDE,
    STOP_RULE_SIGNAL_BAR,
    STOP_RULE_STRUCTURAL_NO_ATR,
    STOP_RULE_PRIOR_BAR_NO_ATR,
    STOP_REFUSAL_WIDER_THAN_REACH,
    STOP_REFUSAL_BUDGET_EXHAUSTED,
    STOP_REFUSAL_BOOK_RISK_UNKNOWN,
    STOP_REFUSAL_DOLLAR_TARGET_NO_STOP,
    STOP_REFUSAL_AGREEMENT_NET,
    STOP_REFUSAL_SIZED_TO_ZERO,
    STOP_REFUSAL_GROSS_EXPOSURE_CEILING,
    STOP_REFUSAL_BELOW_OWNER_MIN_RISK,
    CONSTRUCTOR_NO_ACTION_BELOW_MIN_DELTA,
    CONSTRUCTOR_TARGET_WEIGHT_ZERO_NOTHING_HELD,
    CONSTRUCTOR_SHORT_ALREADY_AT_TARGET,
    CONSTRUCTOR_HELD_PARTIAL_TRIM_REFUSED,
    _NO_REAL_WEIGHT_DELTA_PCT,
    TRIM_REFUSAL_NO_USABLE_LIVE_STOP,
    STOP_REFUSAL_STOP_NOT_FINITE,
    STOP_REFUSAL_ENTRY_NOT_FINITE,
    STOP_REFUSAL_NO_STOP_NO_VOLATILITY,
    STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY,
    STOP_REFUSAL_STRUCTURAL_STOP_TOO_FAR,
    STOP_REFUSAL_NO_VALID_STOP,
    STOP_REFUSAL_NO_STRUCTURAL_TARGET,
    STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY,
    STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY,
    STOP_REFUSAL_SECTOR_BELOW_MIN_ORDER,
    CONSTRUCTOR_REFUSED_EVENT_REASON,
    STOP_REFUSAL_GEOMETRY_AT_BAND,
    STOP_REFUSAL_GEOMETRY_AT_LEVEL,
    STOP_REFUSAL_GEOMETRY_AT_KEPT,
    STOP_REFUSAL_GEOMETRY_UNMEASURABLE,
    SUBFLOOR_RISK_OBSERVED,
    _SUBFLOOR_RISK_STAGE,
    STOP_PERMIT_SUBFLOOR_CATALYST,
    _GEOMETRY_REFUSAL_BY_RULE,
    LEVEL_BACKED_STOP_RULES,
    RiskPlan,
    ConstructorConfig,
    widest_reachable_stop_atr_multiple,
)
from src.portfolio_constructor.assembly import hold_parts, install_delegates
from src.portfolio_constructor.divergence_counter import log_divergence
from src.portfolio_constructor import refusal_log, sector_dial, target_derivation


@install_delegates
class PortfolioConstructor:
    """Stateless translator: target state → concrete orders."""

    def __init__(self, config: ConstructorConfig | None = None, recorder=None):
        self.cfg = config or ConstructorConfig()
        hold_parts(self, delegate_owner=PortfolioConstructor)  # parts HELD, not inherited; wiring in assembly.py
        # `recorder` is optional so every existing caller and every test
        # still constructs this class with no arguments; when the
        # composition root supplies one (built around the db), refusals and
        # observations are written to the `trade_refusals` table with the
        # numbers in their OWN columns, never as a sentence.
        self.refusal_recorder = recorder
        # Populated fresh by every `construct_orders` call — see
        # `_DropReasonCapture`. {symbol: "Constructor: ... rejected/refused
        # ..."} for every target dropped THIS call. Empty, never absent, so
        # a caller can always `getattr(..., "last_drop_reasons", {})` — or
        # just read the attribute — without a first-call special case.
        self.last_drop_reasons: dict[str, str] = {}
        # DATA FAULTS (2026-09-12): {SYMBOL: {"fault", "detail", "direction"}}
        # for every symbol `_derive_target` (or the no-entry-price branch of
        # `_resolve_entry_and_stop`) found UNMEASURABLE — an input a real
        # market always has (price, volatility, usable bars) that this desk
        # failed to obtain. Never a trade judgement, and deliberately NOT
        # mixed into `last_drop_reasons`' classification: the caller
        # (`pipeline_stages.DecisionStage`) records these as `data_fault`
        # rows, not `constructor_dropped`, and pages the owner.
        #
        # Accumulates across `real_reward_risk_preview` (the PM-eligibility
        # pass over every analysed symbol, which runs BEFORE the PM and is
        # where a symbol silently becomes unanalysable) and
        # `construct_orders`, because both run on this one instance in one
        # session. `drain_data_faults()` hands them over and clears; it is
        # the caller's job to drain once per session.
        self.last_data_faults: dict[str, dict[str, str]] = {}

        # New names the caller could not price to a fresh today print (item
        # 120), mapping SYMBOL -> the fault code to file (FAULT_NO_PRICE or
        # FAULT_STALE_PRICE). Populated per `construct_orders` call from its
        # `unpriceable_symbols` argument; consulted by
        # `_resolve_entry_and_stop` so such a name is refused as a data fault
        # rather than sized on a stale/mid price. Initialised here so the
        # backtest shim and older tests that call `_resolve_entry_and_stop`
        # directly never hit an unset attribute.
        self._unpriceable_symbols: dict[str, str] = {}

        # STRUCTURED refusals (2026-09-12): {SYMBOL: {"refusal", "detail",
        # "direction"}} for every trade this instance refused BY NAME —
        # e.g. STOP_REFUSAL_WIDER_THAN_REACH and
        # STOP_REFUSAL_NO_STRUCTURAL_STOP_NO_VOLATILITY. Written directly, never
        # recovered from log text: `last_drop_reasons`
        # is a regex over the constructor's own log lines and several
        # messages miss its pattern, so a refusal that mattered could reach
        # the record as "no matching constructor log line captured".
        #
        # Accumulates across `real_reward_risk_preview` (the PM-eligibility
        # pass over every analysed symbol, which runs BEFORE the PM) and
        # `construct_orders`, because both run on this one instance in one
        # session. `drain_refusals()` hands them over and clears; the caller
        # (`pipeline_stages.DecisionStage`) drains once per session.
        self.last_refusals: dict[str, dict[str, str]] = {}

        # SIDE FLIPS (board item 164, 2026-09-19): {SYMBOL: {...}} for every
        # target this `construct_orders` call collapsed from "flip the side"
        # to "close only" (rule D3 below). The symbol is NOT dropped — it
        # still gets its closing leg — so neither `last_drop_reasons` nor
        # `last_refusals` ever saw it, and the only trace was a log line.
        # Reset per call; `pipeline_stages.DecisionStage` persists it.
        self.last_side_flips: dict[str, dict] = {}

        # REALISED SECTOR of every entry order this call built (board item
        # 224, 2026-10-01): {SYMBOL: sector-or-None}. Filled by
        # `_accrue_sector`, which already resolves the sector while sizing,
        # so this capture buys NO extra data — `_get_sector` is a live
        # network lookup for an uncached name and a RECORDING must never
        # pay for itself. A name whose sector could not be determined is
        # stored as None (NULL in the row), NEVER as "other" or "Unknown":
        # "the desk could not tell" and "the desk placed it in a bucket"
        # are different facts. Reset per call; `pipeline_stages` persists
        # the realised weights from the FINISHED order list.
        self.last_order_sectors: dict[str, str | None] = {}

    def drain_data_faults(self) -> dict[str, dict[str, str]]:
        """Thin shim: body lives in src/portfolio_constructor/refusal_log.py."""
        return refusal_log.drain_data_faults(self)

    def _note_data_fault(
        self,
        symbol: str,
        direction: str,
        fault: str,
        detail: str,
    ) -> None:
        """Thin shim: body lives in src/portfolio_constructor/refusal_log.py."""
        refusal_log._note_data_fault(self, symbol, direction, fault, detail)

    def drain_refusals(self) -> dict[str, dict[str, str]]:
        """Thin shim: body lives in src/portfolio_constructor/refusal_log.py."""
        return refusal_log.drain_refusals(self)

    def _record_subfloor_risk_target(
        self,
        symbol: str,
        direction: str | None,
        requested_pct: float,
    ) -> None:
        """Thin shim: body lives in src/portfolio_constructor/refusal_log.py."""
        refusal_log._record_subfloor_risk_target(self, symbol, direction, requested_pct)

    def _note_refusal(
        self,
        symbol: str,
        direction: str,
        refusal: str,
        detail: str,
        *,
        only_if_unrecorded: bool = False,
        action: str | None = None,
    ) -> None:
        """Thin shim: body lives in src/portfolio_constructor/refusal_log.py."""
        refusal_log._note_refusal(
            self,
            symbol,
            direction,
            refusal,
            detail,
            only_if_unrecorded=only_if_unrecorded,
            action=action,
        )

    def construct_orders(self, *args, **kwargs) -> list[TradeDecision]:
        """Same contract as `_construct_orders_impl` — see its docstring for
        every parameter. Wraps it only to capture, via `_DropReasonCapture`,
        the real reason each dropped target's own log line already states,
        onto `self.last_drop_reasons` (funnel-queue item 2, 2026-09-03: see
        `_DropReasonCapture`'s docstring for why this exists instead of
        threading a reason string through ~20 return points).
        """
        capture = _DropReasonCapture()
        logger.addHandler(capture)
        self.last_side_flips = {}
        self.last_order_sectors = {}
        try:
            orders = self._construct_orders_impl(*args, **kwargs)
            self._note_reducing_order_sectors(orders)
            return orders
        finally:
            logger.removeHandler(capture)
            self.last_drop_reasons = {sym: " | ".join(msgs) for sym, msgs in capture.reasons.items()}

    _note_reducing_order_sectors = _note_reducing_order_sectors
    _current_sector_weights = staticmethod(_current_sector_weights)

    def _construct_orders_impl(
        self,
        targets: list[TargetPosition],
        positions: list[Position],
        analyses: list[TechAnalysisResult],
        total_value: float,
        price_map: dict[str, float] | None = None,
        existing_risk_pct: dict[str, float] | None = None,
        regime: str | None = None,
        evidence_registry: dict[str, dict[str, str]] | None = None,
        stale_sources: dict[str, frozenset[str]] | None = None,
        non_corroborating_sources: dict[str, frozenset[str]] | None = None,
        gross_ceiling=None,
        ranking: Sequence[str] | None = None,
        live_stops: dict[str, float] | None = None,
        unpriceable_symbols: dict[str, str] | set[str] | None = None,
    ) -> list[TradeDecision]:
        """Produce the order list that moves the book from current → target state.

        Orders are returned in a canonical order: exits (SELL/COVER, partials
        and full closes) first, then entries (BUY/SHORT). Execution layer is
        free to re-order, but this matches the existing pipeline assumption
        (exits free up capacity first).

        `price_map`: optional {symbol: live_price} — required for BUYs so
        the constructor can sanity-check TA's entry. If absent for a BUY
        symbol, we fall back to TA's entry_price.

        `unpriceable_symbols`: new names (docs/WORK.md item 120) for which
        the caller's freshness resolver (`src.data.live_price`) could NOT
        obtain a fresh today print this session — a thin name with no print,
        or a feed that returned only a stale print. A mapping SYMBOL ->
        fault code (`FAULT_NO_PRICE` / `FAULT_STALE_PRICE`); a bare set is
        also accepted and defaults every entry to `FAULT_NO_PRICE`. Sizing a
        buy off a stale price (or a quote mid) mis-sizes it proportionally,
        so each of these is refused as a DATA FAULT and dropped rather than
        sized on a bad price OR on the TA entry fallback. It never affects a
        held name: the caller only lists new targets it tried and failed to
        price. `resolve_live_price` returns a real print or today's forming
        session bar and never a quote mid, so an absent value here means no
        usable today price of any kind — not merely "no last trade".

        `existing_risk_pct`: spec §2.2. The book's current
        per-symbol budget risk (`src/risk/metrics.py`). Supplied, it turns the 25% at-risk ceiling from a figure
        the PM was shown into a gate it cannot exceed. Omitted, the portfolio-level ceilings are not
        enforced — the constructor has no view of the book's risk and must not
        invent one — though per-position sizing and the 5% single-name ceiling
        still apply.

        `evidence_registry`: spec §9.4. {symbol: {source: stance}} — the same
        canonical registry `PortfolioManagerAgent.build_evidence_registry`
        built for the PM's own prompt this session (the caller recomputes it
        from the identical inputs; it is a pure function of them, so this is
        guaranteed to agree with what the PM was shown). Drives the agreement
        ceiling in `_plan_risk_targets`. Omitted, that ceiling is not enforced
        — same "no view, don't invent one" posture as `existing_risk_pct`.

        `stale_sources`: spec §9.4 freshness. {symbol: {source}} — registry
        entries that are real coverage but too old to earn size, from
        `PortfolioManagerAgent.stale_evidence_sources` (the same pure function
        the PM's own prompt used, recomputed by the caller from identical
        inputs). Removed from BOTH sides of the agreement tally (a stance too
        stale to corroborate is too stale to dissent), so it can lower a
        ceiling and never raise one. Omitted, nothing is gated — a caller with
        no freshness view must not invent one, exactly as above.

        `non_corroborating_sources`: board item 109. {symbol: {source}} —
        today only a macro stance broadcast onto a name whose sector the
        macro read never mentioned, from
        `PortfolioManagerAgent.broadcast_macro_sources`. A DIFFERENT fact
        from staleness, kept in a DIFFERENT parameter because it has a
        different consequence: it is removed from the ALIGNED side only, so
        it can never manufacture agreement and can never drop a dissent the
        desk should hear. Merging the two mappings would raise the net on
        any name a broad bearish read opposed, admitting trades that are
        refused today — and would file "too stale" as the durable reason for
        a stance that is perfectly current. Omitted, nothing is gated.

        `gross_ceiling`: spec §11.2. The de-levering ladder's resolved
        `GrossCeiling` for this session — the standing cap, stepped down by
        measured peak-to-trough drawdown. Entries are shrunk to fit it, and
        refused outright when what remains is below `min_order_usd`. Omitted,
        the constructor falls back to the standing cap with no drawdown
        applied, so a caller that forgets it still sizes under A ceiling
        rather than none. **This function never trims the held book** — the
        gross ceiling's de-lever is authored in the session preamble, before
        any agent runs, so it cannot depend on a model returning a book.

        `live_stops`: {symbol: live broker stop} for held positions — the
        same stops the heat roll-up behind `existing_risk_pct` is computed
        from. Used ONLY to size a risk-based trim of a held name that has no
        analysis this session (`_held_trim_entry_and_stop`). Omitted, such a
        trim is refused by name and the position is left unchanged.

        `ranking`: retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14). The
        session's candidate order, BEST FIRST — the caller passes the symbols
        of `PortfolioManagerAgent.last_candidate_ranking`, i.e. exactly the
        `rank_verdicts` order the PM itself was shown. When the risk budget
        binds it is spent down this order rather than in whatever order the
        allocator happened to iterate. Omitted, the allocator's pre-decision
        ordering applies — no ranking is invented here or there.
        """
        if total_value <= 0:
            return []
        price_map = price_map or {}
        # New names the caller could not price to a fresh today print (item
        # 120), keyed upper-cased so the lookup in `_resolve_entry_and_stop`
        # cannot miss on case/whitespace drift; the value is the fault code
        # to file. A bare set defaults every entry to FAULT_NO_PRICE. Reset
        # every call — per-run state, like `price_map` itself.
        if isinstance(unpriceable_symbols, dict):
            self._unpriceable_symbols = {
                str(s).strip().upper(): str(code or FAULT_NO_PRICE) for s, code in unpriceable_symbols.items()
            }
        else:
            self._unpriceable_symbols = {str(s).strip().upper(): FAULT_NO_PRICE for s in (unpriceable_symbols or ())}
        current_weights = self._current_weights(positions, total_value)
        analyses_by_sym = {a.symbol: a for a in analyses}
        positions_by_sym = {p.symbol: p for p in positions}

        # Spec §2.1/§2.2. Resolve each risk-based target's implied notional
        # weight BEFORE the delta loop, because that weight is what every
        # downstream step — the churn filter, the close test, the partial-sell
        # fraction — already speaks in. Conviction arrives as risk; the stop
        # converts it to a size; the budget rations it across the book.
        risk_plan = self._plan_risk_targets(
            targets,
            analyses_by_sym=analyses_by_sym,
            price_map=price_map,
            current_weights=current_weights,
            existing_risk_pct=existing_risk_pct,
            regime=regime,
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            non_corroborating_sources=non_corroborating_sources,
            ranking=ranking,
            live_stops=live_stops,
        )
        # Dollar-only targets that ADD exposure were converted to risk through
        # their stop and charged against the 25% ceiling inside
        # `_plan_risk_targets` (defect 2026-10-09: they used to bypass it
        # entirely). One refused there produces no order; the rest are sized
        # the old way, exactly as before.
        # Direct read: `_plan_risk_targets` (called unconditionally above, no
        # early return before it sets this) resets it every run, so a missing
        # set is a wiring fault and raises here.
        refused_dollar = self._dollar_targets_refused
        if refused_dollar:
            targets = [t for t in targets if t.symbol not in refused_dollar]

        # Spec §10.3. Held GROSS exposure per sector, carried through the
        # loop and updated as each entry is built, so the second and third
        # targets in one crowded sector are sized against a book that already
        # contains the first.
        sector_weights = self._current_sector_weights(positions, total_value)

        sells: list[TradeDecision] = []
        buys: list[TradeDecision] = []

        for target in targets:
            sym = target.symbol
            current_pct = current_weights.get(sym, 0.0)
            is_short_target = target.direction == "short"
            if target.risk_allocation_pct is not None:
                plan = risk_plan.get(sym)
                if plan is None:
                    # No stop, no entry, or the budget refused it outright.
                    # drop-reason: delegated — `_plan_risk_targets` files the
                    # named refusal (agreement net, budget exhausted) or
                    # `_resolve_entry_and_stop` filed the fault/refusal that
                    # left this symbol without a plan in the first place.
                    continue
                target_mag = plan.target_weight_pct  # unsigned magnitude
            else:
                target_mag = target.target_weight_pct or 0.0

            # D1 (Stage 3): signed target. `current_pct` is already signed
            # (Stage 1) — negative means a held short. Everything below
            # operates on SIGNED weights, so the sign of the delta IS the
            # side of the order: positive is buy-side (BUY to open/add a
            # long, or COVER to reduce a short); negative is sell-side
            # (SELL to reduce a long, or SHORT to open/add a short).
            signed_target = -target_mag if is_short_target else target_mag

            # D3: sign-crossing is refused. A single order that flips a
            # position from long to short (or back) is unprotected for the
            # instant between legs, and the broker treats a sell LARGER
            # than the held quantity differently again (it opens a short
            # rather than just closing). Emit ONLY the closing leg this
            # session — flatten to zero — and let the position open on the
            # other side next session once the book is actually flat.
            if (current_pct > 0 and signed_target < 0) or (current_pct < 0 and signed_target > 0):
                logger.warning(
                    "Constructor: %s target flips side (held %.2f%%, signed "
                    "target %.2f%%) — refusing the flip. Emitting only the "
                    "flattening leg this session; the other side may open "
                    "next session once the book is actually flat.",
                    sym,
                    current_pct,
                    signed_target,
                )
                try:
                    self.last_side_flips[str(sym).strip().upper()] = {
                        "held_weight_pct": current_pct,
                        "requested_weight_pct": signed_target,
                        "emitted_weight_pct": 0.0,
                    }
                except Exception:  # noqa: BLE001 — a record side-channel must never raise
                    pass
                signed_target = 0.0

            plan_for_sym = risk_plan.get(sym) if target.risk_allocation_pct is not None else None
            if sym in self._reduce_only and abs(signed_target) > abs(current_pct):
                # Book risk is UNKNOWN: this request was let through only
                # because it asked for no more risk than the name carries, so
                # it may shrink the position but never grow it.
                logger.info("Constructor: %s held at its size — book risk unknown, reductions only", sym)
                signed_target = current_pct
            if plan_for_sym is not None and plan_for_sym.sized_from_live_stop and abs(signed_target) > abs(current_pct):
                # A trim sized from the live stop may only reduce. At its live
                # stop this position already carries no more than the risk
                # asked for, so there is nothing to sell — and with no
                # analysis there is no basis to buy. Hold it as it is.
                logger.info(
                    "Constructor: %s trim needs no order — at its live stop "
                    "($%.2f) the position already risks no more than the "
                    "%.2f%% asked for; left unchanged.",
                    sym,
                    plan_for_sym.stop_price or 0.0,
                    plan_for_sym.risk_pct,
                )
                signed_target = current_pct

            # Owner ruling 2026-10-09: never sell PART of a held position.
            # A same-side target smaller than what is held is kept at its
            # size; a full close (signed_target == 0) is untouched. The
            # planner never partly cuts a holding — not even when the risk
            # budget granted less than asked, because the budget funds
            # better-ranked ideas first and would sell part of a held winner
            # to pay for a new one. The only partial cut of a holding is the
            # gross-ceiling de-lever, which has its own order path.
            shrinks_held = (
                signed_target != 0
                and (current_pct > 0) == (signed_target > 0)
                and abs(current_pct) - abs(signed_target) >= _NO_REAL_WEIGHT_DELTA_PCT
            )
            if current_pct != 0 and shrinks_held:
                self._note_refusal(
                    sym,
                    target.direction,
                    CONSTRUCTOR_HELD_PARTIAL_TRIM_REFUSED,
                    "the desk asked for a smaller position in a stock it "
                    "already holds, but a held position is either kept "
                    "whole or sold whole (owner ruling 2026-10-09). The "
                    "position was kept at its current size; nothing was sold.",
                )
                if current_pct > 0:
                    buys.append(self._hold_decision(target))
                continue

            delta_pct = signed_target - current_pct

            # signed_target == 0 is PM saying "CLOSE this position" (long or
            # short), not "rebalance toward ~0". The churn filter must not
            # swallow it: a 0.4%-weight dreg with an explicit close target
            # was silently converted into a HOLD, so a position PM had
            # decided to exit sat in the book indefinitely (2026-07-16
            # audit). Anything held with target 0 goes to the SELL/COVER
            # builder, which emits a full exit.
            closing = signed_target == 0 and current_pct != 0
            # Owner ruling 2026-09-30 (board item 183): the picked
            # `min_trade_weight_delta` churn floor is GONE. Any delta the
            # desk's own reasoning asked for is attempted below, however
            # small — the mechanical bounds a real order can still hit
            # (a broker minimum notional/quantity, sub-penny pricing, a
            # non-fractionable instrument) are read from the broker itself
            # (`AlpacaBroker.get_fractionability`, `_quantize_price`,
            # `_is_terminal_broker_rejection` in `submit_order`), not chosen
            # here. Only a delta that is not really there at all — floating-
            # point noise below `_NO_REAL_WEIGHT_DELTA_PCT` — is a no-op.
            if not closing and abs(delta_pct) < _NO_REAL_WEIGHT_DELTA_PCT:
                # No action. Three arms, one per thing that actually gets
                # here — and every one of them leaves a record, because
                # deleting `min_trade_weight_delta` deleted the only reason
                # this loop used to file and board item 10 does not allow a
                # candidate to end anonymously.
                if current_pct > 0:
                    # Held LONG already at its target: emit HOLD for audit
                    # continuity so the desk's intent to keep this position
                    # at its current level is recorded. The symbol survives,
                    # so this is not a drop.
                    buys.append(self._hold_decision(target))
                elif current_pct < 0:
                    # Held SHORT already at its target. HOLD's audit
                    # bookkeeping stays long-only for this stage, so unlike
                    # the arm above this one produces nothing and must say
                    # why in its own words.
                    self._note_refusal(
                        sym,
                        target.direction,
                        CONSTRUCTOR_SHORT_ALREADY_AT_TARGET,
                        "this short is already the size the desk wants it, "
                        "so there was nothing to buy or sell. Nothing was "
                        "judged wrong with the position and nothing about "
                        "it was changed.",
                    )
                else:
                    # Nothing held, and the weight the desk asked for is
                    # zero. Not a size judgement and not a refusal of the
                    # idea: there is no position to place.
                    self._note_refusal(
                        sym,
                        target.direction,
                        CONSTRUCTOR_TARGET_WEIGHT_ZERO_NOTHING_HELD,
                        "the desk named this stock but asked for a position "
                        "of zero, and nothing is held in it, so there was no "
                        "trade to place. Nothing was judged wrong with the "
                        "idea and nothing already held was touched.",
                    )
                continue

            if delta_pct < 0:
                if current_pct > 0:
                    # Trim or close a LONG.
                    sell_decision = self._build_sell(
                        target,
                        positions_by_sym.get(sym),
                        current_pct,
                        signed_target,
                        current_risk_pct=_lookup_existing_risk(
                            existing_risk_pct,
                            sym,
                        ),
                    )
                    if sell_decision is not None:
                        sells.append(sell_decision)
                else:
                    # Open or add to a SHORT (current_pct <= 0).
                    short_decision = self._build_short(
                        target,
                        plan=risk_plan.get(sym),
                        analysis=analyses_by_sym.get(sym),
                        current_pct=current_pct,
                        target_pct=signed_target,
                        total_value=total_value,
                        market_price=price_map.get(sym),
                        regime=regime,
                    )
                    if short_decision is not None:
                        self._accrue_sector(sector_weights, short_decision)
                        sells.append(short_decision)
            else:
                if current_pct < 0:
                    # Cover (reduce/close) a SHORT.
                    cover_decision = self._build_cover(
                        target,
                        positions_by_sym.get(sym),
                        current_pct,
                        signed_target,
                        current_risk_pct=_lookup_existing_risk(
                            existing_risk_pct,
                            sym,
                        ),
                    )
                    if cover_decision is not None:
                        buys.append(cover_decision)
                else:
                    # Open or add a LONG.
                    buy_decision = self._build_buy(
                        target,
                        plan=risk_plan.get(sym),
                        analysis=analyses_by_sym.get(sym),
                        current_pct=current_pct,
                        target_pct=signed_target,
                        total_value=total_value,
                        market_price=price_map.get(sym),
                        regime=regime,
                    )
                    if buy_decision is not None:
                        self._accrue_sector(sector_weights, buy_decision)
                        buys.append(buy_decision)

        # Canonical ordering: SELLs first (free up cash), then BUYs.
        # Among SELLs: full closes before partials. Among BUYs: by target
        # weight descending (largest commitments first so cash rationing
        # in a tight-cash session prioritizes highest conviction).
        sells.sort(key=lambda d: 0 if d.allocation_pct >= 100 else 1)
        buys.sort(key=lambda d: d.allocation_pct, reverse=True)
        orders = sells + buys

        # Spec §11.2 — the SIZING half of the gross-exposure ceiling. Runs
        # last, on the finished order list, because it is the only ceiling
        # here that is a property of the WHOLE book rather than of one name:
        # the exits above have already reduced what will be held, and every
        # entry has to be rationed against the same headroom.
        #
        # `emit_trims=False` on purpose. Shrinking an order it is about to
        # propose is this class's job; authoring a de-lever of the held book
        # is not. That has exactly one owner — the session preamble, which
        # runs before any agent and therefore keeps working on a run where
        # the Portfolio Manager returns nothing at all.
        from src.risk.rules import (
            GrossCeiling,
            apply_gross_ceiling,
            resolve_gross_ceiling,
        )

        # isinstance, not truthiness: a caller (or a Mock pipeline in a test)
        # that hands over something ceiling-shaped-but-not-a-ceiling must fall
        # back to the standing cap rather than silently size against a
        # comparison that raises.
        ceiling = (
            gross_ceiling
            if isinstance(gross_ceiling, GrossCeiling)
            else resolve_gross_ceiling(
                None,
                base_x=self.cfg.max_gross_exposure_x,
            )
        )
        outcome = apply_gross_ceiling(
            orders,
            positions,
            total_value,
            ceiling,
            cash_park_symbol=self.cfg.cash_park_symbol,
            # No `min_order_usd`: board item 183 deleted the constructor's
            # copy of the flat $500 floor. `apply_gross_ceiling` accepts the
            # argument only as an ignored legacy parameter and falls back to
            # its own default, which it never reads either.
            emit_trims=False,
        )
        for note in outcome.notes:
            logger.warning("Constructor: %s", note)
        # Board item 10 (2026-09-14): every note above is relayed through
        # THIS logger as "Constructor: max_gross_exposure: SYMBOL refused —
        # ...", which `_DropReasonCapture._SYMBOL` never matches — the rule
        # name sits between "Constructor:" and the symbol. File the
        # structured refusal directly from `outcome.blocked_detail` instead
        # of leaning on the log scrape. Direction is read off the ORIGINAL
        # decision (before the filter below drops it) so a blocked SHORT is
        # recorded as a short refusal, not defaulted to BUY.
        if outcome.blocked:
            action_by_symbol = {d.symbol: d.action for d in orders}
            for sym in outcome.blocked:
                direction = "short" if action_by_symbol.get(sym) == "SHORT" else "long"
                self._note_refusal(
                    sym,
                    direction,
                    STOP_REFUSAL_GROSS_EXPOSURE_CEILING,
                    outcome.blocked_detail.get(
                        sym,
                        "the §11.2 gross-exposure ceiling refused this entry outright; no further detail was recorded",
                    ),
                )
        # An entry rationed to nothing is dropped rather than emitted as a
        # zero-allocation order — `allocation_pct == 0` means SKIP to the
        # execution stage, and leaving it in the list would show the operator
        # a trade that was never going to happen.
        orders = [d for d in orders if d.action not in ("BUY", "SHORT") or d.allocation_pct > 0]
        # Owner rule 2026-08-27: below `min_risk_pct` the desk does not
        # trade. Re-checked HERE, on the finished list, because every shrink
        # above (single-name ceiling, gross ceiling) runs after the allocator
        # applied the same floor to the grant.
        return self._refuse_below_min_risk(orders, risk_plan, current_weights)

    def _refuse_below_min_risk(
        self,
        orders: list[TradeDecision],
        risk_plan: dict[str, RiskPlan],
        current_weights: dict[str, float],
    ) -> list[TradeDecision]:
        """Drop every risk-adding order whose resulting position risks less
        than `cfg.min_risk_pct` of equity at its own entry and stop.

        Only BUY (a long open/add) and SHORT (a short open/add) add risk;
        SELL and COVER pass untouched. The position's risk is
        `(held same-side weight + this order's weight) / gross multiplier x
        |entry - stop| / entry` -- the inverse of the §2.1 weight formula in
        `_plan_risk_targets`, at the order's own entry and stop, judged by
        `src.risk.min_risk.min_risk_shortfall`. An order with no readable
        entry or stop has unknown risk and is refused by name, never passed.
        """
        from src.risk.min_risk import min_risk_shortfall
        from src.risk.rules import _gross_multiplier

        floor = float(self.cfg.min_risk_pct)
        if not floor > 0:
            return orders
        kept: list[TradeDecision] = []
        for decision in orders:
            plan = risk_plan.get(decision.symbol)
            if decision.action not in ("BUY", "SHORT"):
                kept.append(decision)
                continue
            # The order's own entry and stop; the plan's only when the order
            # carries none.
            entry = decision.entry_price or (plan.entry_price if plan else None)
            stop = decision.stop_loss if decision.stop_loss is not None else (plan.stop_price if plan else None)
            held = current_weights.get(decision.symbol, 0.0)
            held_same = max(0.0, -held if decision.action == "SHORT" else held)
            gross = _gross_multiplier(decision.symbol)
            # Order weights are rounded to 2dp (`_build_buy`/`_build_short`),
            # so a request sized at exactly the floor can land a hair under
            # it. Half of that 0.01pp step is forgiven -- rounding, not a cut.
            rounding_pct = 0.0
            if isinstance(entry, (int, float)) and isinstance(stop, (int, float)) and entry > 0:
                rounding_pct = 0.005 / gross * abs(entry - stop) / entry
            # The 0.5% floor is owner rule 2026-08-27; judging it on the WHOLE
            # position after the order (held same-side weight + this order),
            # and refusing rather than sizing up, is owner ruling 2026-10-12.
            # Weights are percent of book, so equity is 100.
            check = min_risk_shortfall(
                position_notional=(held_same + float(decision.allocation_pct)) / gross,
                entry=entry,
                stop=stop,
                equity=100.0,
                floor_pct=floor,
                tolerance_pct=rounding_pct,
            )
            if not check.refused:
                kept.append(decision)
                continue
            if not check.readable:
                detail = (
                    f"its entry ({entry}) or stop ({stop}) is unreadable, so the "
                    f"position's risk at the stop is unknown and cannot be shown "
                    f"to meet the owner's {floor:.2f}% minimum risk per position "
                    f"(owner rule 2026-08-27)."
                )
            else:
                detail = (
                    f"after every sizing cut this position would risk {check.risk_pct:.2f}% "
                    f"of equity at its stop, under the owner's {floor:.2f}% minimum "
                    f"risk per position (owner rule 2026-08-27): below it the desk "
                    f"does not trade."
                )
            logger.warning("Constructor: %s refused — %s", decision.symbol, detail)
            self._note_refusal(
                decision.symbol,
                "short" if decision.action == "SHORT" else "long",
                STOP_REFUSAL_BELOW_OWNER_MIN_RISK,
                detail,
            )
        return kept

    def _plan_risk_targets(
        self,
        targets: list[TargetPosition],
        *,
        analyses_by_sym: dict,
        price_map: dict[str, float],
        current_weights: dict[str, float],
        existing_risk_pct: dict[str, float] | None,
        regime: str | None = None,
        evidence_registry: dict[str, dict[str, str]] | None = None,
        stale_sources: dict[str, frozenset[str]] | None = None,
        non_corroborating_sources: dict[str, frozenset[str]] | None = None,
        ranking: Sequence[str] | None = None,
        live_stops: dict[str, float] | None = None,
    ) -> dict[str, RiskPlan]:
        """Turn risk-based targets into notional weights, under the budget.

        Spec §2.1: `shares = (equity x risk_pct) / |entry - stop|`, which as a
        notional weight is `risk_pct x entry / (entry - stop)`. The equity term
        cancels, so this needs no book value — only the stop distance. A wider
        stop yields a SMALLER position rather than a rejected trade, which is
        what eliminates the "stops too tight" failure class: risk is never
        controlled by squeezing the stop.

        Spec §2.2: the requested risks are rationed against the total
        ceiling before any of them is converted to a size, so the
        book is bounded by construction rather than by a later veto.

        Spec §9.4, as of 2026-09-14, narrowed 2026-10-19 (desk archetype):
        the net-evidence floor (`evidence_registry` + `entry_floor_score`:
        seats for other than macro, minus the chart when it is against;
        opposing name-level seats are objections, not subtractions) is a
        REFUSAL GATE and nothing more. A score at or below zero refuses the
        request entirely, so the target produces no order at all; a held
        position is left exactly where it is, because refusing to BUY is not
        a decision to SELL. A score of 1 or more imposes no size restriction:
        the graduated ceiling that used to scale risk by sqrt(net / 5 seats)
        is retired, because that law prices INDEPENDENT estimates and these
        seats read overlapping evidence. See
        `src/risk/rules.py::agreement_refuses_trade`. Agreement still ORDERS
        which candidates get funded first, through `rank_verdicts` and the
        allocator's `priority`.
        """
        from src.risk.budget import RiskRequest, allocate_risk_budget
        from src.risk.rules import (
            _gross_multiplier,
            agreement_refuses_trade,
            count_aligned_sources,
            count_opposing_sources,
            entry_floor_score,
        )
        from src.risk.size_override import SizeOverride

        priced: dict[str, tuple[float, float]] = {}  # symbol -> (entry, stop)
        # Stage 3: direction is tracked alongside the priced entry/stop so
        # the weight formula below can pick the right (unsigned) risk-per-
        # share denominator and apply the short sizing haircut. The RESULT
        # (`target_weight_pct`) stays an unsigned magnitude either way — the
        # delta loop in `construct_orders` applies the sign from
        # `target.direction`.
        directions: dict[str, str] = {}
        live_stop_trims: set[str] = set()
        requests: list[RiskRequest] = []
        closes: set[str] = set()
        # §9.4 dissent. Since 2026-09-02 it IS subtracted (the refusal reads
        # the signed score); this note records the split that produced the
        # score, so an order backed by 3-for/1-against evidence says so
        # rather than looking like a clean 2-source idea.
        dissent_notes: dict[str, str] = {}

        # Defect 2026-10-09: the 25% ceiling used to be SKIPPED when any
        # holding's risk could not be read. `unknown` is carried on the
        # book map itself (`BookRiskPct`); while it is non-empty every
        # risk-adding request is refused by name. A close is never blocked.
        book_unknown = sorted(getattr(existing_risk_pct, "unknown", ()) or ())
        self._dollar_targets_refused: set[str] = set()
        #: Held same-side names whose risk request is at or below what they
        #: already carry: a reduction, allowed while the book is unknown, and
        #: never allowed to GROW the position in the delta loop.
        self._reduce_only: set[str] = set()
        dollar_risk: dict[str, tuple[str, float]] = {}  # symbol -> (direction, risk %)

        for target in targets:
            if target.risk_allocation_pct is None:
                converted = self._dollar_target_as_risk(
                    target,
                    analyses_by_sym=analyses_by_sym,
                    price_map=price_map,
                    current_weights=current_weights,
                    regime=regime,
                    book_unknown=book_unknown,
                    gross_multiplier=_gross_multiplier,
                )
                if converted is None:
                    self._dollar_targets_refused.add(target.symbol)
                    continue  # drop-reason: delegated — refused by name in `_dollar_target_as_risk`
                if converted > 0:
                    dollar_risk[target.symbol] = (target.direction, converted)
                # drop-reason: NOT a drop. A dollar-only target gets no
                # RiskPlan; it is charged against the ceiling below and the
                # delta loop sizes it the old way.
                continue
            sym = target.symbol
            if target.risk_allocation_pct == 0.0:
                # A close needs no price, no stop and no budget. Routing it
                # through the pricing checks below would let a missing quote
                # silently cancel an exit PM had decided on.
                closes.add(sym)
                # 2026-09-12: the close IS handed to the allocator, as the
                # zero request `allocate_risk_budget` already documents
                # ("a zero request is PM closing the name. It consumes no
                # budget"). Before this, a full close never reached the
                # allocator at all, so the closed name's EXISTING risk kept
                # counting as committed and a same-session "close X, open
                # Y" plan had Y rationed against a book that still held X —
                # denied for "no room" the sale was about to create.
                # Measured: OLD at 24.8% of a 25% ceiling, NEW asking 2%:
                # without this, NEW granted 0.00% (below_floor); with it,
                # 2.00%. Partial trims never had this problem because they
                # are requests already. Whether the sale then FILLS is
                # ExecutionStage's question, answered there (it sells,
                # waits for terminal, and re-reads the account before any
                # BUY); a granted size on an unfilled close is the same
                # exposure a trim-then-add plan has always carried.
                requests.append(RiskRequest(sym, 0.0))
                # drop-reason: NOT a drop. This is PM asking to CLOSE the
                # name; it goes to the exit builder, not to nowhere.
                continue
            if book_unknown:
                held_pct = current_weights.get(sym, 0.0)
                held_same = held_pct < 0 if target.direction == "short" else held_pct > 0
                held_risk = (existing_risk_pct or {}).get(sym.upper(), (existing_risk_pct or {}).get(sym))
                if held_same and held_risk is not None and target.risk_allocation_pct <= held_risk:
                    # Reduces risk on a held name: never blocked by an
                    # unknown book, and pinned so it can only shrink.
                    self._reduce_only.add(sym)
                else:
                    self._refuse_book_unknown(sym, target.direction, book_unknown)
                    # drop-reason: delegated — `_refuse_book_unknown` files
                    # STOP_REFUSAL_BOOK_RISK_UNKNOWN and logs the symbols.
                    continue
            if 0.0 < target.risk_allocation_pct < self.cfg.min_risk_pct:
                # Board item 223 — RECORDING ONLY. The target is not
                # refused, not resized and not reordered; it falls through
                # to exactly the path it would have taken had this block
                # not existed. A zero request never reaches here (it is the
                # CLOSE branch above), which is why the comparison is
                # strictly positive.
                self._record_subfloor_risk_target(
                    sym,
                    target.direction,
                    target.risk_allocation_pct,
                )
            analysis = analyses_by_sym.get(sym)
            held_pct = current_weights.get(sym, 0.0)
            held_same_side = held_pct < 0 if target.direction == "short" else held_pct > 0
            if analysis is None and held_same_side:
                # A held name this session still has no Technical for (Tech
                # unresolved after retry, or a path that never asked). The
                # intraday scan now produces Technical for holds; this
                # branch is the remainder, not the product. There is
                # nothing to derive a new stop from, but the position
                # already HAS one at the broker: size the trim from that,
                # and never let it grow the position.
                entry, stop = self._held_trim_entry_and_stop(
                    target,
                    price_map.get(sym),
                    (live_stops or {}).get(sym.upper()),
                )
                if entry is None or stop is None:
                    # drop-reason: delegated — `_held_trim_entry_and_stop`
                    # files TRIM_REFUSAL_NO_USABLE_LIVE_STOP before returning
                    # (None, None). The position is left unchanged.
                    continue
                live_stop_trims.add(sym)
            else:
                entry, stop = self._resolve_entry_and_stop(
                    target,
                    analysis,
                    price_map.get(sym),
                    regime=regime,
                )
            if entry is None or stop is None:
                # drop-reason: delegated. `_resolve_entry_and_stop` has
                # already filed the fault or the named refusal for this
                # symbol — every one of its own exits does.
                continue  # already logged; no stop means no honest size
            priced[sym] = (entry, stop)
            directions[sym] = target.direction

            # The single-name envelope binds before the portfolio one. A PM
            # asking for more than the ratified envelope is clamped rather
            # than refused — the idea is sound, the size is not.
            envelope_capped = min(target.risk_allocation_pct, self.cfg.risk_budget_pct)
            # docs/WORK.md item 13: this cap is always a plain multiplier —
            # the envelope can shrink a request, it never refuses one — so it
            # is never anything but SizeOverride.sized(). The refusal case
            # lives entirely in the agreement refusal below.
            envelope_override = SizeOverride.sized(envelope_capped)
            # §9.4: then the agreement refusal, computed from THIS session's
            # canonical registry (not from target.provenance — see
            # `count_aligned_sources`), before the request ever reaches the
            # portfolio-level budget allocator. Same "no view, don't invent
            # one" posture as `existing_risk_pct` above: when the
            # caller has no registry to offer, the ceiling is UNENFORCED
            # (infinite), never silently treated as zero agreement — a
            # missing registry is not evidence of disagreement.
            agreement_count: int | None = None
            opposing_count: int | None = None
            source_score: int | None = None
            if evidence_registry is not None:
                sources = evidence_registry.get(sym.upper(), {})
                # §9.4 freshness: a stance the caller has judged too old is
                # dropped from the TALLY only. It stays in `sources`, so it is
                # still coverage `validate_grounding` recognises — it can
                # only ever pull the net DOWN, never up. One gate, both sides: a
                # stance too stale to corroborate is too stale to dissent.
                ignored = (stale_sources or {}).get(sym.upper())
                # Item 109: a broadcast macro stance comes off the ALIGNED
                # side only. It cannot corroborate a name the macro read
                # never looked at; it can still dissent.
                non_corroborating = (non_corroborating_sources or {}).get(sym.upper())
                aligned_ignored = frozenset(ignored or ()) | frozenset(non_corroborating or ())
                agreement_count = count_aligned_sources(
                    sym,
                    sources,
                    target.direction,
                    ignored_sources=aligned_ignored,
                )
                opposing_count = count_opposing_sources(
                    sym,
                    sources,
                    target.direction,
                    ignored_sources=ignored,
                )
                # 2026-10-19 (desk archetype): the floor no longer nets off
                # an opposing name-level seat (that is an objection the
                # planning AI must overrule in writing, enforced per target
                # as `objection_unanswered` before this stage) or macro
                # (it tilts size only). `entry_floor_score` says what is
                # left: chart for + name-level seats for - chart against.
                # `agreement_count` / `opposing_count` above stay the full
                # displayed split for the dissent note.
                source_score = entry_floor_score(
                    sym,
                    sources,
                    target.direction,
                    ignored_sources=ignored,
                )
                # 2026-09-14: agreement is a REFUSAL, not a ceiling. Net at
                # or below zero drops the target; anything above it imposes
                # no size restriction of its own — the ratified per-trade
                # envelope and the budget allocator are the only bounds. See
                # `agreement_refuses_trade` for why the graduated ladder was
                # retired. `SizeOverride.no_trading()` carries the refusal
                # rather than a bare 0.0, which downstream could not tell
                # apart from an intentional zero-weight close (item 13).
                agreement_override = (
                    SizeOverride.no_trading()
                    if agreement_refuses_trade(source_score)
                    else SizeOverride.sized(float("inf"))
                )
                # Two gates, two REASONS. One merged line filed "too stale"
                # against a broadcast macro stance that is perfectly current
                # — a wrong, persistent, machine-readable reason on the path
                # that drops a candidate. Each is now logged as what it is.
                if ignored:
                    gated = sorted(s for s in ignored if s in sources)
                    if gated:
                        logger.info(
                            "Constructor: %s — %s stance(s) present but too "
                            "stale to count toward agreement (%d aligned "
                            "after the freshness gate)",
                            sym,
                            ", ".join(gated),
                            agreement_count,
                        )
                if non_corroborating:
                    broad = sorted(s for s in non_corroborating if s in sources)
                    if broad:
                        logger.info(
                            "Constructor: %s — %s stance(s) present and "
                            "current, but market-wide rather than a read on "
                            "this name's sector, so they cannot count FOR "
                            "the trade (macro is never in the entry floor; "
                            "it tilts size only); %d aligned, %d opposed",
                            sym,
                            ", ".join(broad),
                            agreement_count,
                            opposing_count,
                        )
                logger.info(
                    "Constructor: %s agreement %d aligned / %d opposed; "
                    "entry floor %+d (direction=%s, %d source(s) with coverage)",
                    sym,
                    agreement_count,
                    opposing_count,
                    source_score,
                    target.direction,
                    len(sources),
                )
            else:
                # No registry to check dissent against — same "no view, don't
                # invent one" posture as everywhere else in this method: an
                # unbounded multiplier, never a refusal.
                agreement_override = SizeOverride.sized(float("inf"))
            # docs/WORK.md item 13 / the pysystemtrade override algebra: the
            # combined override is always the MORE RESTRICTIVE of the two —
            # `no_trading` absorbs a multiplier no matter how large, so this
            # can never be diluted back into "trade a bit". This is the exact
            # generalization of the 2026-09-02 signed-dissent workaround: that
            # patch dropped a target whose combined float landed at or below
            # zero; this makes "combined float at or below zero" and "the
            # combination IS a refusal" the same statement, structurally,
            # rather than two things a future caller could let drift apart.
            combined_override = envelope_override.combine(agreement_override)
            if combined_override.is_refusal:
                # Drop the target entirely rather than sizing it at zero — a
                # zero-weight RiskPlan reads to the delta loop as "PM wants
                # this position CLOSED", so letting a 0% request through would
                # turn a refusal to open into a forced liquidation of whatever
                # is already held. Refusing to BUY is not a decision to SELL.
                # The two dels are load-bearing, not tidiness: the sizing loop
                # below iterates `priced` and looks each symbol up in
                # `requests`, which this target never joins.
                #
                # `_note_refusal` files the CODE as data (board item 10,
                # 2026-09-14) instead of a `logger.warning` whose "produces
                # no order" phrasing `_DropReasonCapture._SYMBOL` does not
                # match — the same defect item 49 already fixed for the
                # portfolio-level budget allocator below, found again here by
                # running the regex against this message.
                self._note_refusal(
                    sym,
                    target.direction,
                    STOP_REFUSAL_AGREEMENT_NET,
                    # Board item 89 defect 4 — a rule cited by NUMBER whose
                    # text says something else. This read "§9.4 refuses a
                    # net at or below zero". `docs/QAMC_REMEDIATION_SPEC.md`
                    # §9.4 is titled "Conviction is agreement, not a
                    # technical rating" and is about agreement EARNING
                    # SIZE; it states no refusal at all, and the sizing
                    # half it does state was RETIRED on 2026-09-14 — which
                    # the sibling dissent note twenty lines below already
                    # says out loud, in the same function. So the owner was
                    # pointed at a section that contradicted the reason he
                    # was given for losing the trade.
                    #
                    # The rule is now STATED rather than cited. That is the
                    # real fix: a section number in owner-facing prose is a
                    # promise that a separate document still says a
                    # particular thing, and nothing in this repo checks
                    # that promise — so it can go stale again the moment
                    # the spec is renumbered or amended, silently and
                    # without touching this file. Nothing about the gate,
                    # the threshold or the decision changes here; only what
                    # the owner is told about it.
                    f"{agreement_count} seat(s) for / {opposing_count} against this "
                    f"{target.direction}, entry backstop {source_score:+d}. The desk "
                    f"does not open a position nobody backs: the chart and the "
                    f"news, earnings and smart-money seats do not back it, or "
                    f"the chart is against it. Any existing position is left "
                    f"untouched.",
                )
                del priced[sym]
                del directions[sym]
                continue
            # `.value` is safe here — `combined_override` is a `multiplier`
            # override by construction whenever it is not a refusal (the only
            # other kinds this method ever produces are `no_trading`, handled
            # above, so nothing else reaches this line).
            requested_pct = combined_override.value
            if opposing_count:
                # Carried into the order's reasoning (see `RiskPlan.note`) so
                # the AI Risk Manager reads it and it lands in the persisted
                # proposed_order evidence. The SPLIT, not just the net: an
                # idea backed 3-for/1-against is a different idea from one
                # with a flat 2 aligned and no dissent, and the note is the
                # only place that survives.
                dissent_notes[sym] = (
                    f"[constructor: {sym} — {opposing_count} independent "
                    f"source(s) took the OPPOSITE side of this "
                    f"{target.direction} ({agreement_count} aligned, net "
                    f"{source_score:+d}). §9.4 nets the SIGNED sum since "
                    "2026-09-02; the net stayed above zero, so the trade was "
                    "not refused. Agreement no longer sizes anything "
                    "(retired 2026-09-14) — read this as evidence quality]"
                )
            requests.append(RiskRequest(sym, requested_pct))

        # `existing_risk_pct is None` means the CALLER supplied no book at
        # all (unit tests, the backtest engine's own budget). The live path
        # never passes None: `_book_risk_inputs` returns a `BookRiskPct`,
        # marked UNKNOWN when the book cannot be read, and an unknown book
        # refuses every risk-adding request above (defect 2026-10-09).
        allocation = self.last_risk_allocation = (
            allocate_risk_budget(
                requests,
                existing_pct=existing_risk_pct,
                ceiling_pct=self.cfg.max_portfolio_risk_pct,
                floor_pct=self.cfg.min_risk_pct,
                # retired board item 49 — best-ranked first. `ranking` is the PM's
                # own `rank_verdicts` order, threaded through unchanged; the
                # allocator scores nothing and this module scores nothing.
                priority=ranking,
            )
            if existing_risk_pct is not None
            else None
        )

        if allocation is not None and dollar_risk:
            # A close of either kind (risk 0.0, or a dollar-only 0% weight)
            # frees its existing risk, exactly as `closes` does for the allocator.
            closing = {c.upper() for c in closes} | {
                t.symbol.upper() for t in targets if t.risk_allocation_pct is None and not (t.target_weight_pct or 0.0)
            }
            committed = sum(v for k, v in existing_risk_pct.items() if k.upper() not in closing)
            committed += sum(g.granted_pct for g in allocation.grants.values())
            for dsym, (ddir, drisk) in dollar_risk.items():
                ceiling = self.cfg.max_portfolio_risk_pct
                if committed + drisk > ceiling + 1e-9:
                    self._dollar_targets_refused.add(dsym)
                    self._note_refusal(
                        dsym,
                        ddir,
                        STOP_REFUSAL_BUDGET_EXHAUSTED,
                        f"the portfolio risk budget has {max(0.0, ceiling - committed):.2f}% left of the "
                        f"{ceiling:.2f}% ceiling; this dollar-only target, converted through its "
                        f"stop, would risk {drisk:.2f}%. Refused (defect 2026-10-09: dollar-only "
                        f"targets used to bypass the ceiling).",
                    )
                    continue
                committed += drisk

        plans: dict[str, RiskPlan] = {}
        for sym in closes:
            plans[sym] = RiskPlan(
                symbol=sym,
                risk_pct=0.0,
                target_weight_pct=0.0,
                entry_price=None,
                stop_price=None,
                note="",
            )

        for sym, (entry, stop) in priced.items():
            requested = min(
                next(r.requested_pct for r in requests if r.symbol == sym),
                self.cfg.risk_budget_pct,
            )
            note_parts = []
            if sym in dissent_notes:
                note_parts.append(dissent_notes[sym])
            if allocation is not None:
                grant = allocation.grants.get(sym.upper())
                granted = grant.granted_pct if grant else 0.0
                if grant and grant.note:
                    note_parts.append(grant.note)
                if granted <= 0:
                    # retired board item 49 (`docs/INCIDENT_HISTORY.md`, 2026-09-14). This used to be a
                    # bare `logger.info` whose wording the drop-reason
                    # capture's regex does not match, so a budget-rationed
                    # name reached the database as a generic
                    # `constructor_dropped` with "no matching constructor log
                    # line captured". `_note_refusal` files the CODE as data
                    # instead, per symbol, exactly like every other named
                    # constructor refusal.
                    #
                    # The target is DROPPED, not zeroed. A 0% risk target is
                    # read downstream as "sell it"; refusing to open a
                    # position is not a decision to close one, and this
                    # `continue` leaves `plans[sym]` absent so the delta loop
                    # skips the symbol entirely. `closes` is populated on a
                    # different path (an explicit PM 0.0 request) and is
                    # untouched here.
                    limited_by = grant.limited_by if grant else "no grant"
                    self._note_refusal(
                        sym,
                        directions.get(sym, ""),
                        STOP_REFUSAL_BUDGET_EXHAUSTED,
                        f"the portfolio risk budget granted 0.00% of the "
                        f"{requested:.2f}% risk this idea asked for "
                        f"({limited_by}). Better-ranked candidates took the "
                        f"{self.cfg.max_portfolio_risk_pct:.2f}% ceiling "
                        f"first (owner decision 2026-09-12, retired board "
                        f"item 49). Nothing is wrong with the idea — it "
                        f"passed every gate and lost only the queue.",
                    )
                    continue
            else:
                granted = requested
            note = " ".join(note_parts)

            # risk_pct x entry / risk_per_share: the §2.1 formula as a
            # weight. risk_per_share is UNSIGNED — `entry - stop` is
            # negative for a short (whose stop sits ABOVE entry), so a bare
            # `entry - stop` would corrupt the weight's sign; `abs()` keeps
            # this an unsigned magnitude exactly like the long case (D4).
            risk_per_share = abs(entry - stop)
            # Owner ruling 2026-10-04: no short-side haircut — a short and
            # a long with the same stop distance get the same weight.
            raw_weight = granted * entry / risk_per_share
            plans[sym] = RiskPlan(
                symbol=sym,
                risk_pct=granted,
                # Stored in GROSS-leverage terms, the units _current_weights
                # and the delta loop speak. _build_buy divides back out.
                target_weight_pct=raw_weight * _gross_multiplier(sym),
                entry_price=entry,
                stop_price=stop,
                note=note,
                sized_from_live_stop=sym in live_stop_trims,
            )
        return plans

    def _refuse_book_unknown(self, sym: str, direction: str, book_unknown: list[str]) -> None:
        """Refuse one risk-adding request because the book's risk is UNKNOWN."""
        self._note_refusal(
            sym,
            direction,
            STOP_REFUSAL_BOOK_RISK_UNKNOWN,
            f"the risk of held {', '.join(book_unknown)} could not be measured "
            f"(no usable price, or the book could not be read), so the "
            f"{self.cfg.max_portfolio_risk_pct:.2f}% total-risk ceiling cannot be "
            f"checked. No risk is added until it can; exits and stops are unaffected.",
        )
        logger.warning(
            "Constructor: %s refused — book risk UNKNOWN for %s; new buys wait until it is read",
            sym,
            ", ".join(book_unknown),
        )

    def _dollar_target_as_risk(
        self,
        target: TargetPosition,
        *,
        analyses_by_sym: dict,
        price_map: dict[str, float],
        current_weights: dict[str, float],
        regime: str | None,
        book_unknown: list[str],
        gross_multiplier,
    ) -> float | None:
        """A dollar-only target that ADDS exposure, as a risk request — or refused by name.

        Defect 2026-10-09: a target carrying only `target_weight_pct` got no
        RiskPlan and was sized "the old way", never charged against the 25%
        total-risk ceiling. Returns 0.0 for a close, a side flip (flattened
        by the delta loop) or a same-side hold at or below the held weight:
        nothing is added. Otherwise the weight converted through its own
        stop, `risk = weight / gross x |entry - stop| / entry`, which the
        caller charges against the ceiling — or None when it is refused by
        name (book risk unknown, or no stop to convert through).
        """
        sym = target.symbol
        weight = float(target.target_weight_pct or 0.0)
        held = current_weights.get(sym, 0.0)
        held_same_side = -held if target.direction == "short" else held
        if weight <= 0 or held_same_side < 0 or weight <= held_same_side:
            return 0.0
        if book_unknown:
            self._refuse_book_unknown(sym, target.direction, book_unknown)
            return None  # drop-reason: delegated — `_refuse_book_unknown` files STOP_REFUSAL_BOOK_RISK_UNKNOWN
        entry, stop = self._resolve_entry_and_stop(
            target,
            analyses_by_sym.get(sym),
            price_map.get(sym),
            regime=regime,
        )
        if entry is None or stop is None or entry <= 0 or entry == stop:
            self._note_refusal(
                sym,
                target.direction,
                STOP_REFUSAL_DOLLAR_TARGET_NO_STOP,
                f"a {weight:.2f}% dollar-only target with no usable stop cannot be "
                f"converted to risk, so it cannot be charged against the "
                f"{self.cfg.max_portfolio_risk_pct:.2f}% total-risk ceiling. Refused.",
                only_if_unrecorded=True,
            )
            logger.warning("Constructor: %s dollar-only target refused — no stop to convert it to risk", sym)
            return None
        risk_pct = weight / gross_multiplier(sym) * abs(entry - stop) / entry
        logger.info(
            "Constructor: %s dollar-only target %.2f%% converted to %.3f%% risk through its stop $%.2f",
            sym,
            weight,
            risk_pct,
            stop,
        )
        return risk_pct

    def _held_trim_entry_and_stop(
        self,
        target: TargetPosition,
        market_price: float | None,
        live_stop: float | None,
    ) -> tuple[float | None, float | None]:
        """Thin shim: body lives in src/portfolio_constructor/target_derivation.py."""
        return target_derivation._held_trim_entry_and_stop(
            self._note_refusal,
            target,
            market_price,
            live_stop,
        )

    def _derive_target(
        self,
        symbol: str,
        analysis: TechAnalysisResult | None,
        entry_price: float,
        direction: str,
    ) -> TargetDerivation:
        """Thin shim: body lives in src/portfolio_constructor/target_derivation.py."""
        return target_derivation._derive_target(
            self.cfg,
            self._note_data_fault,
            self._log_target_divergence,
            symbol,
            analysis,
            entry_price,
            direction,
        )

    def _log_target_divergence(
        self,
        symbol: str,
        derivation: TargetDerivation,
    ) -> None:
        """Thin shim: body lives in src/portfolio_constructor/target_derivation.py."""
        target_derivation._log_target_divergence(
            self.cfg,
            self.refusal_recorder,
            symbol,
            derivation,
        )

    @staticmethod
    def _target_note(derivation: TargetDerivation) -> str:
        """Thin shim: body lives in src/portfolio_constructor/target_derivation.py."""
        return target_derivation._target_note(derivation)

    @staticmethod
    def _current_weights(
        positions: list[Position],
        total_value: float,
    ) -> dict[str, float]:
        """Thin shim: body lives in src/portfolio_constructor/sector_dial.py."""
        return sector_dial._current_weights(positions, total_value)

    def _accrue_sector(
        self,
        sector_weights: dict[tuple[str, str], float],
        decision: TradeDecision,
    ) -> None:
        """Thin shim: body lives in src/portfolio_constructor/sector_dial.py."""
        sector_dial._accrue_sector(self.last_order_sectors, sector_weights, decision)
