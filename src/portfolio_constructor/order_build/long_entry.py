"""Long entry builder — the `_build_buy` leg of the Portfolio Constructor.

Standalone piece: every collaborator is an explicit keyword-only constructor
argument, so it builds and runs without a `PortfolioConstructor` or a
`TradingPipeline` behind it (clauses 1-5 of tests/boundary_harness.py).
Method bodies moved VERBATIM from src/portfolio_constructor/orders.py; the
same-named thin shims there build this object per call."""

from __future__ import annotations
import math
from src.models import TargetPosition, TechAnalysisResult, TradeDecision, stated_soft_exit
from src.risk.constants import risk_budget_allocation_pct
from src.portfolio_constructor.config import logger
from src.portfolio_constructor.target_derivation import log_no_target
from src.portfolio_constructor.config import STOP_REFUSAL_SIZED_TO_ZERO, STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY, RiskPlan


class LongEntryBuilder:
    """`_build_buy` (lifted verbatim) built from explicit collaborators."""

    def __init__(
        self,
        *,
        cfg,
        derive_target,
        resolve_entry_and_stop,
        note_refusal,
        shipped_stop_rule,
        shipped_stop_level_basis,
        target_note,
    ):
        self.cfg = cfg
        self._derive_target = derive_target
        self._resolve_entry_and_stop = resolve_entry_and_stop
        self._note_refusal = note_refusal
        self.shipped_stop_rule = shipped_stop_rule
        self.shipped_stop_level_basis = shipped_stop_level_basis
        self._target_note = target_note

    def _build_buy(
        self,
        target: TargetPosition,
        analysis: TechAnalysisResult | None,
        current_pct: float,
        target_pct: float,
        total_value: float,
        market_price: float | None,
        plan: RiskPlan | None = None,
        regime: str | None = None,
    ) -> TradeDecision | None:
        # A risk-based target already resolved its entry and stop in
        # _plan_risk_targets — reusing them keeps the size the budget granted
        # consistent with the level the order actually ships, which a second
        # resolution against a moved quote would not.
        if plan is not None and plan.entry_price is not None and plan.stop_price is not None:
            entry_price, stop_loss = plan.entry_price, plan.stop_price
        else:
            entry_price, stop_loss = self._resolve_entry_and_stop(
                target,
                analysis,
                market_price,
                regime=regime,
            )
            if entry_price is None or stop_loss is None:
                # drop-reason: delegated — every exit from
                # `_resolve_entry_and_stop` files a fault or a named refusal.
                return None

        # Take-profit is COMPUTED from structure (2026-09-01), not read from
        # the analyst's `reference_target`. Two earlier stages of the same
        # argument: `entry * (1 + 2*stop_gap_pct)` manufactured a target when
        # the analyst omitted one, and was deleted; then the analyst's own
        # number was made mandatory, which removed the fabrication but left
        # the reward:risk gate dividing a measured stop by a guessed target.
        # Now both sides of that ratio come from the bars. The model's guess
        # survives on `analysis.reference_target` as evidence and is logged
        # against the computed level by `_derive_target`.
        derivation = self._derive_target(
            target.symbol,
            analysis,
            entry_price,
            target.direction,
        )
        # A `no_target` derivation (measured chart, no level — owner rule
        # 2026-10-09) passes with `take_profit=None`: never invented.
        if derivation.refused or (derivation.price is not None and derivation.price <= entry_price):
            # A data fault is already recorded/logged as UNMEASURABLE by
            # `_derive_target`; only a real refusal is a rejection here.
            if not derivation.fault:
                # Board item 10 (2026-09-14, second pass) — same treatment as
                # the identical check in `_resolve_entry_and_stop`. The
                # derivation's own refusal code wins when it has one; the
                # fallback names the case where a price WAS computed and it
                # simply does not sit above the entry.
                self._note_refusal(
                    target.symbol,
                    target.direction,
                    derivation.refusal or STOP_REFUSAL_TARGET_NOT_ABOVE_ENTRY,
                    f"no take-profit could be computed above the "
                    f"${entry_price:,.2f} entry for this long"
                    + (f": {derivation.detail}" if derivation.detail else f" (computed {derivation.price})"),
                )
            return None
        take_profit = float(derivation.price) if derivation.price is not None else None

        # `target_pct` and `current_pct` are GROSS-leverage weights (see
        # _current_weights), but every consumer of `allocation_pct` spends it
        # as RAW notional: risk/rules.py does `total_value * alloc/100` and
        # THEN applies the gross multiplier itself, and ExecutionStage sizes
        # `qty = total_value * alloc/100 / price`. Emitting the gross delta
        # raw therefore over-deployed leveraged/inverse ETFs by their
        # multiplier (2026-07-16 audit: a PM target of 6% gross on SQQQ (3x)
        # deployed $6k raw = 18% gross of a $100k book — and the NEXT session
        # saw current_pct=18 vs target 6 and emitted SELL 67% of the hedge PM
        # wanted held, repeating until raw ≈ 2%). Convert once, here, so the
        # delta and every downstream consumer speak the same units. No-op for
        # the ~99% of the universe with multiplier 1.0.
        from src.risk.rules import _gross_multiplier

        allocation_pct = (target_pct - current_pct) / _gross_multiplier(target.symbol)
        # Pull in vol-adj sizing in a uniform way: ensure qty (computed
        # downstream) doesn't put more than risk_budget_pct of equity at risk.
        # NOTE: alloc_cap_by_risk below is computed in RAW notional terms, so
        # this conversion must happen BEFORE the comparison.
        # D4: unsigned everywhere — a plain `entry - stop` is negative for a
        # short (whose stop sits above entry), which would corrupt this cap
        # instead of tightening it.
        # ITEM 222: this is NOT a third, independent notional cap. It is
        # `risk_budget_pct x entry / |entry - stop|` — the per-position RISK
        # envelope converted into notional units so it can be compared with
        # the notional ceiling below. See `SINGLE_NAME_BINDING_SENTENCE`.
        risk_per_share = abs(entry_price - stop_loss)
        # qty_by_risk = risk_dollars_allowed / risk_per_share
        # position_$ = qty_by_risk * entry_price
        # allocation_by_risk_pct = position_$ / total_value * 100
        #                        = (risk_dollars_allowed / risk_per_share) * entry_price / total_value * 100
        cap_note = ""
        if risk_per_share > 0:
            # ONE definition of stop-derived size (board item 221): the
            # PM-facing projected-portfolio preview calls this same helper,
            # so the sector mix the PM self-corrects against is the mix this
            # constructor would actually build. The arithmetic is unchanged.
            alloc_cap_by_risk = risk_budget_allocation_pct(
                entry_price=entry_price,
                stop_price=stop_loss,
                total_value=total_value,
                risk_budget_pct=self.cfg.risk_budget_pct,
            )
            if alloc_cap_by_risk is not None and allocation_pct > alloc_cap_by_risk:
                logger.info(
                    "Constructor: %s alloc capped by risk budget (delta %.2f%% → %.2f%% at %.1f%% risk budget)",
                    target.symbol,
                    allocation_pct,
                    alloc_cap_by_risk,
                    self.cfg.risk_budget_pct,
                )
                # Provenance for the AI Risk Manager: it audits the
                # CONSTRUCTED order against PM's prose. Without this note
                # a capped allocation reads as PM claiming one size and
                # proposing another — on 2026-08-20 the RM called exactly
                # that mismatch (PM 15% vs constructed 10.65%) "plan
                # inconsistency", scored the reasoning chain incoherent
                # and issued a full-plan veto over deterministic math.
                cap_note = (
                    f" [constructor: PM target delta {allocation_pct:.2f}% "
                    f"capped to {alloc_cap_by_risk:.2f}% by the "
                    f"{self.cfg.risk_budget_pct:.1f}% risk budget — the size "
                    f"difference vs PM's stated weight is deterministic, "
                    f"not PM inconsistency]"
                )
                allocation_pct = alloc_cap_by_risk

        # Single-name notional ceiling. The risk engine treats
        # `max_position_pct` as a HARD BLOCK, not a trim, so an order above it
        # is not "reduced" downstream — it is dropped and the trade never
        # happens. Under risk-based sizing that is the common case rather than
        # the edge: risk_pct x entry/(entry - stop) exceeds 20% of equity for
        # any conviction above ~1% at this book's real stop distances. Clamp
        # to what the engine will actually accept, and say so, rather than
        # shipping an order built to be rejected.
        #
        # The resulting position therefore risks LESS than the PM allocated
        # whenever this binds. That is the honest outcome of the two ceilings
        # meeting, and the note carries it into the audit trail — silently
        # delivering under-sized risk is exactly the kind of gap this pass
        # exists to close.
        gross_mul = _gross_multiplier(target.symbol)
        name_headroom_pct = (self.cfg.max_position_pct - current_pct) / gross_mul
        if allocation_pct > name_headroom_pct:
            logger.info(
                "Constructor: %s alloc capped by the single-name ceiling "
                "(delta %.2f%% → %.2f%%; %.1f%% max position, %.2f%% already held)",
                target.symbol,
                allocation_pct,
                max(0.0, name_headroom_pct),
                self.cfg.max_position_pct,
                current_pct,
            )
            cap_note += (
                f" [constructor: size capped to {max(0.0, name_headroom_pct):.2f}% "
                f"by the {self.cfg.max_position_pct:.0f}% single-name ceiling — "
                f"the stop is close enough that the requested risk would need a "
                f"larger position than one name may hold, so this trade carries "
                f"less risk than allocated. Deterministic, not PM inconsistency]"
            )
            # Round DOWN when the cap bound: round() could lift the size up to
            # 0.005 points over the cap and the risk gate refuses that outright.
            allocation_pct = math.floor(max(0.0, name_headroom_pct) * 100 + 1e-9) / 100

        allocation_pct = max(0.0, round(allocation_pct, 2))
        if allocation_pct <= 0:
            # Board item 10 (2026-09-14): the risk-budget-per-trade cap and
            # the single-name ceiling above both LOG when they shrink a
            # request, but neither message says rejected/refused/skipped, so
            # `_DropReasonCapture` never sees them — and unlike the ATR/no-
            # stop path in `_resolve_entry_and_stop`, nothing else logs for
            # this symbol afterward to compensate. `cap_note` already carries
            # the deterministic provenance of whichever cap(s) bound (it is
            # the same text appended to a surviving order's `reasoning`), so
            # it is reused verbatim as the refusal detail rather than
            # re-deriving which cap fired.
            self._note_refusal(
                target.symbol,
                target.direction,
                STOP_REFUSAL_SIZED_TO_ZERO,
                (
                    cap_note.strip()
                    or (
                        "the position sizing chain (risk budget, single-name "
                        "ceiling, sector crowding) left nothing to round to "
                        "above zero"
                    )
                ),
            )
            return None

        reasoning = target.thesis
        falsifier = stated_soft_exit(target.thesis_invalid_if)
        catalyst = stated_soft_exit(target.catalyst)
        if falsifier:
            reasoning += f" (invalid if: {falsifier})"
        if catalyst:
            reasoning += f" (catalyst: {catalyst})"

        log_no_target(target.symbol, target.direction, entry_price, derivation)
        return TradeDecision(
            action="BUY",
            symbol=target.symbol,
            allocation_pct=allocation_pct,
            entry_price=entry_price,
            stop_loss=stop_loss,  # already rounded + validated above
            take_profit=take_profit,
            # Cap note appended AFTER the truncation so provenance never
            # gets sliced off by a long thesis. The budget note (spec §2.2)
            # rides alongside it for the same reason: a portfolio-level cut
            # the AI Risk Manager cannot see the arithmetic behind reads as
            # the PM contradicting itself.
            reasoning=reasoning
            + cap_note
            + (f" {plan.note}" if plan is not None and plan.note else "")
            + self._target_note(derivation),
            # Conviction ledger (spec §7.2) — pinned at entry, never
            # recomputed. `plan` is None for a legacy notional target, so
            # `allocated_risk_pct` stays None rather than a fabricated
            # figure the budget never actually granted.
            conviction=target.conviction,
            requested_risk_pct=target.risk_allocation_pct,
            allocated_risk_pct=plan.risk_pct if plan is not None else None,
            # Carried so the execution stage does not re-widen a stop this
            # constructor deliberately honoured at a computed level.
            stop_rule=self.shipped_stop_rule(
                analysis,
                entry_price,
                stop_loss,
                target.direction,
            ),
            # Item 55 RECORDING, no behaviour: the same answer as stop_rule
            # above, with the ingredients that produced it, so the desk can
            # later ask what its levels actually did. See
            # TradeDecision.stop_level_basis.
            stop_level_basis=self.shipped_stop_level_basis(
                analysis,
                entry_price,
                stop_loss,
                target.direction,
            ),
            # Carried for the SAME reason as stop_rule: so the execution
            # stage's own reward:risk belt does not kill an order that was
            # deliberately permitted below the floor. See
            # TradeDecision.subfloor_catalyst_exception.
            subfloor_catalyst_exception=bool(getattr(target, "subfloor_catalyst_verified", False)),
            # Carried for the SAME reason as stop_rule: how this position is
            # MANAGED decides whether a reward:risk figure means anything at
            # all downstream. See TradeDecision.setup_type.
            setup_type=getattr(analysis, "setup_type", None),
            # The MEASURED half of the same verdict (item 82) — see
            # TradeDecision.structural_ceiling. Same expression the reward:
            # risk gate above (inside `_widen_stop_past_noise`, via
            # `_resolve_entry_and_stop`) used against THIS `derivation`, so
            # the Risk Manager sees the identical verdict construction saw.
            structural_ceiling=(derivation.level_used is not None),
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            thesis_invalid_if=stated_soft_exit(target.thesis_invalid_if) or None,
        )
