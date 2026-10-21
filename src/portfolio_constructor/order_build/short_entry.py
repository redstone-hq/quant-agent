"""Short entry builder — the `_build_short` leg of the Portfolio Constructor.

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
from src.portfolio_constructor.config import STOP_REFUSAL_SIZED_TO_ZERO, STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY, RiskPlan


class ShortEntryBuilder:
    """`_build_short` (lifted verbatim) built from explicit collaborators."""

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

    def _build_short(
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
        """The BUY-side mirror (Stage 3, D1): open or add to a short.

        `current_pct` and `target_pct` are both SIGNED and <= 0 (the
        `construct_orders` dispatch only reaches this builder when the
        position is flat-or-short and the signed target does not cross zero
        to the long side).
        """
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

        # Take-profit: COMPUTED from structure and BELOW entry for a short
        # (price must FALL for a short to profit) — the exact mirror of
        # _build_buy, using the same derivation. The direction inversion
        # lives inside `derive_structural_target`: it draws from levels below
        # the entry and projects a measured move downward, so nothing here
        # has to know which way the trade points beyond passing
        # `target.direction` through.
        derivation = self._derive_target(
            target.symbol,
            analysis,
            entry_price,
            target.direction,
        )
        # A `no_target` derivation (measured chart, no level — owner rule
        # 2026-10-09) passes with `take_profit=None`: never invented.
        if derivation.refused or (derivation.price is not None and derivation.price >= entry_price):
            # Mirror of `_build_buy`: a data fault is already recorded and
            # logged as UNMEASURABLE; only a real refusal is a rejection.
            if not derivation.fault:
                # Board item 10 (2026-09-14, second pass) — mirror of
                # `_build_buy`. See the comment there.
                self._note_refusal(
                    target.symbol,
                    target.direction,
                    derivation.refusal or STOP_REFUSAL_TARGET_NOT_BELOW_ENTRY,
                    f"no take-profit could be computed below the "
                    f"${entry_price:,.2f} entry for this short"
                    + (f": {derivation.detail}" if derivation.detail else f" (computed {derivation.price})"),
                )
            return None
        take_profit = float(derivation.price) if derivation.price is not None else None

        from src.risk.rules import _gross_multiplier

        gross_mul = _gross_multiplier(target.symbol)
        # Both current_pct and target_pct are signed and <= 0 here; moving
        # FURTHER from zero (more negative) is what grows the short, so the
        # raw notional delta is `current - target` (positive when growing).
        allocation_pct = (current_pct - target_pct) / gross_mul

        # D4: unsigned risk-per-share (stop sits ABOVE entry for a short).
        risk_per_share = abs(entry_price - stop_loss)
        # Owner ruling 2026-10-04: no short-side haircut. A short runs the
        # SAME arithmetic as a long from here on.
        cap_note = ""
        if risk_per_share > 0:
            # Same ONE definition the long leg and the preview call (item
            # 221), with identical arguments: same math for both sides.
            alloc_cap_by_risk = risk_budget_allocation_pct(
                entry_price=entry_price,
                stop_price=stop_loss,
                total_value=total_value,
                risk_budget_pct=self.cfg.risk_budget_pct,
            )
            if alloc_cap_by_risk is not None and allocation_pct > alloc_cap_by_risk:
                logger.info(
                    "Constructor: SHORT %s alloc capped by risk budget (delta %.2f%% → %.2f%% at %.1f%% risk budget)",
                    target.symbol,
                    allocation_pct,
                    alloc_cap_by_risk,
                    self.cfg.risk_budget_pct,
                )
                cap_note = (
                    f" [constructor: PM target delta {allocation_pct:.2f}% "
                    f"capped to {alloc_cap_by_risk:.2f}% by the "
                    f"{self.cfg.risk_budget_pct:.1f}% risk budget "
                    f"— the size difference vs PM's stated weight is "
                    f"deterministic, not PM inconsistency]"
                )
                allocation_pct = alloc_cap_by_risk

        # Single-name ceiling — the SAME `max_position_pct` a long uses
        # (owner decision 2026-09-17: shorts carry the same limits as longs).
        # Mirrors _build_buy's max_position_pct clamp so the constructor
        # sizes UNDER the risk engine's hard block instead of proposing an
        # order the engine will drop outright.
        current_short_gross_pct = abs(current_pct)  # already gross-scaled, <= 0
        name_headroom_pct = (self.cfg.max_position_pct - current_short_gross_pct) / gross_mul
        if allocation_pct > name_headroom_pct:
            logger.info(
                "Constructor: SHORT %s alloc capped by the single-name "
                "ceiling (delta %.2f%% → %.2f%%; %.1f%% max position, %.2f%% "
                "already held)",
                target.symbol,
                allocation_pct,
                max(0.0, name_headroom_pct),
                self.cfg.max_position_pct,
                current_short_gross_pct,
            )
            cap_note += (
                f" [constructor: size capped to {max(0.0, name_headroom_pct):.2f}% "
                f"by the {self.cfg.max_position_pct:.0f}% single-name "
                f"ceiling (the same one a long uses). "
                f"Deterministic, not PM inconsistency]"
            )
            # Round DOWN when the cap bound: round() could lift the size up to
            # 0.005 points over the cap and the risk gate refuses that outright.
            allocation_pct = math.floor(max(0.0, name_headroom_pct) * 100 + 1e-9) / 100

        allocation_pct = max(0.0, round(allocation_pct, 2))
        if allocation_pct <= 0:
            # Board item 10 (2026-09-14) — see the identical comment in
            # `_build_buy`. The risk-budget-per-trade cap and the single-
            # short ceiling above both LOG when they shrink a request
            # without saying rejected/refused/skipped, so nothing here would
            # otherwise reach `_DropReasonCapture` or `last_refusals`.
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
            action="SHORT",
            symbol=target.symbol,
            allocation_pct=allocation_pct,
            entry_price=entry_price,
            stop_loss=stop_loss,  # already rounded + validated above
            take_profit=take_profit,
            reasoning=reasoning
            + cap_note
            + (f" {plan.note}" if plan is not None and plan.note else "")
            + self._target_note(derivation),
            # Conviction ledger (spec §7.2) — mirrors _build_buy's entry
            # pinning; see its comment for what each field means.
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
            # TradeDecision.structural_ceiling and the identical comment in
            # `_build_buy`.
            structural_ceiling=(derivation.level_used is not None),
            # Real, untruncated field alongside the embedded-in-reasoning
            # text above — see TradeDecision.thesis_invalid_if.
            thesis_invalid_if=stated_soft_exit(target.thesis_invalid_if) or None,
        )
