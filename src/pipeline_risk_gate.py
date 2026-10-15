"""RiskGate — the deterministic application of risk verdicts to sizes. MONEY.

Conversion step 7 (docs/ARCHITECTURE.md §4). The hard-block filter that drops
a decision the risk engine refused, the durable record of that block, the
application of the risk seat's size/stop/target modifications, the minimum-
risk floor breach, the reconciliation of a modified size back to the risk
budget, the research pre-filter, and the refusal of a queued BUY whose filing
is unread. Every body here is the former `RiskGateMixin` body, byte for byte;
only the collaborators changed from inherited attributes to constructor
parameters. `TradingPipeline` reaches it through the thin delegating
`RiskGateMixin` in `src/pipeline_risk_gate_mixin.py`.

`compute_indicators` and `_get_sector` resolve against THIS module (patch
them here). Nothing here may import `src.pipeline` (boundary clause 3).
"""

import logging
import math

from pydantic import ValidationError

from src.data.technical import compute_indicators
from src.sector_reference import _get_sector
from src.models import TechnicalIndicators, TradeDecision
from src.pipeline_context import RunContext
from src.pipeline_delever import _optional_risk_number
from src.pipeline_earnings_refusal import refuse_queued_earnings_buys
from src.risk.rules import HARD_BLOCK_RULES
from src.sentinel.guarded import NO_LEDGER, record_guarded_pass

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class RiskGate:
    """Risk-verdict application, built alone from its three collaborators."""

    def __init__(self, *, risk_engine, db, sweeper, config, atr_for_symbol=None) -> None:
        # risk_engine.check refuses; db.insert_agent_log records a hard block (not
        # on the EventJournal port); sweeper() = `TradingPipeline._sweeper`; config.
        self.risk_engine = risk_engine
        self.db = db
        self._sweeper = sweeper
        self.config = config
        # The host's ATR(14) reader; sizes the `deployment_gap` threshold
        # (the book's own one-session drift). None = drift unmeasured.
        self._atr_for_symbol = atr_for_symbol

    def _filter_hard_risk_decisions(
        self,
        decisions: list[TradeDecision],
        positions,
        total_value: float,
        invested_target_pct: float | None = None,
        correlation_matrix: dict[str, dict[str, float]] | None = None,
        cash: float | None = None,
        # Spec §11.2. The ladder-resolved gross-exposure ceiling for this
        # session. None falls back to the configured cap inside the engine —
        # a caller that forgets it still gets a ceiling, never none.
        gross_ceiling=None,
    ) -> tuple[list[TradeDecision], list, list[str]]:
        allowed_decisions: list[TradeDecision] = []
        remaining_violations = []
        blocked_reasons: list[str] = []
        pending_investment = 0.0
        # Spec §12.2 — keyed by `(sector, side)`. A pending SHORT must not eat
        # the same sector's LONG budget, and vice versa.
        pending_sector_investment: dict[tuple[str, str], float] = {}
        pending_symbol_investment: dict[str, float] = {}
        pending_cash_outflow = 0.0
        # Spec §11.2: running total of GROSS notional (direction-agnostic,
        # leverage-adjusted) already allowed earlier in this batch. Without
        # it two entries in one run would each be measured against only the
        # pre-existing book and never see each other — the same gap
        # `pending_investment` closes for net exposure.
        pending_gross_investment = 0.0
        # Raw (unsigned, UN-leveraged) notional already approved this batch.
        # This is the pending leg of `book_exposure`'s `deployed` measure —
        # capital committed, which is what the invested target
        # (`DESK_INVESTED_TARGET_PCT`) is defined against. Distinct from `pending_cash_outflow` (BUYs only,
        # a funding question) and from `pending_gross_investment` (leverage
        # multiplied, a ceiling question).
        pending_raw_investment = 0.0

        # Cash-sweep view: the parked T-bill vehicle is cash-equivalent —
        # exclude it from the position list so net-exposure / cluster math
        # doesn't count parked cash as market exposure.
        #
        # This gate does NOT credit the parked vehicle's value into the cash
        # budget. Callers pass `ctx.deployable_cash`, which already includes
        # it (raw `cash` + convertible sweep value — see
        # `_compute_deployable_cash`). Crediting it a second time here would
        # double-count the same dollars and approve BUYs execution cannot
        # fund. The `sell_proceeds` credit just below is unrelated and
        # unchanged: it only credits proceeds of REAL position SELLs this
        # same run, which ExecutionStage always executes and waits for
        # before any BUY submits.
        sweeper = self._sweeper()
        if sweeper is not None:
            positions, _parked = sweeper.split_positions(positions)

        # Pre-pass: sum the cash SELLs in this session will return. The
        # execution stage always runs SELLs before BUYs and waits for fills,
        # so by the time a BUY submits, `cash + sell_proceeds` is available.
        # Without this the cash-only rule would block legitimate SELL→BUY
        # rotations that never actually draw on margin.
        sell_proceeds = 0.0
        if cash is not None:
            for d in decisions:
                if d.action != "SELL":
                    continue
                held = next((p for p in positions if p.symbol == d.symbol), None)
                if held is None or held.qty <= 0:
                    continue
                # CLAUDE.md convention: allocation_pct=0 means SKIP (not full sell).
                # Execution stage skips the order; filter must match or we'd
                # credit phantom SELL proceeds to the BUY cash budget, allowing
                # a BUY that actually draws margin at execution time.
                if d.allocation_pct <= 0:
                    continue
                # Alpaca occasionally returns NaN market_value during market-open
                # glitches or for assets with missing prices. Without this guard
                # `sell_proceeds += NaN * frac` poisons effective_cash to NaN,
                # which silently passes every subsequent BUY hard-rule check
                # (`NaN > limit` is False in Python comparisons). Skip the
                # SELL from the pre-sum — its proceeds aren't safely
                # knowable, so the BUY cash budget shouldn't pre-credit them.
                if not math.isfinite(held.market_value):
                    logger.warning(
                        "SELL pre-sum: skipping %s — broker returned non-finite "
                        "market_value=%s; cash budget will be conservative",
                        d.symbol,
                        held.market_value,
                    )
                    continue
                # Mirror ExecutionStage's exact share rounding so the cash
                # budget credits the proceeds the SELL will *actually* realize.
                # ExecutionStage (pipeline_stages.py) rounds a partial alloc to
                # whole shares for integer-qty positions via
                # `max(1.0, int(qty*frac))`, then promotes to a full sell when
                # the rounded qty meets/exceeds the position. The naive
                # `market_value * (alloc/100)` diverges from that both ways:
                #   - under-credits (e.g. 40% of a 1-share lot rounds UP to a
                #     full sell → 100% proceeds) → false-blocks a legit BUY;
                #   - over-credits (e.g. 99% of a 10-share lot rounds DOWN to 9
                #     shares = 90% proceeds) → phantom cash a BUY could borrow.
                # Crediting `eff_qty / held.qty` closes both gaps.
                if d.allocation_pct >= 100:
                    proceeds_frac = 1.0
                else:
                    eff_qty = held.qty * (d.allocation_pct / 100.0)
                    if float(held.qty).is_integer():
                        eff_qty = max(1.0, float(int(eff_qty)))
                    if eff_qty >= held.qty:
                        eff_qty = held.qty  # rounds up to a full exit
                    proceeds_frac = eff_qty / held.qty if held.qty > 0 else 0.0
                sell_proceeds += held.market_value * proceeds_frac
        effective_cash = None if cash is None else cash + sell_proceeds

        for decision in decisions:
            # Stage 3: a SHORT opens/adds new risk exactly as a BUY does, so
            # it must clear the same hard-block gate (D9's short caps live
            # inside `risk_engine.check`). SELL and COVER bypass this gate
            # entirely and fall straight through to `allowed_decisions` —
            # for COVER that is deliberate (D10: a cover can never be
            # blocked), for SELL it always has been.
            if decision.action not in ("BUY", "SHORT"):
                allowed_decisions.append(decision)
                continue

            violations = self.risk_engine.check(
                decision=decision,
                positions=positions,
                total_value=total_value,
                pending_investment=pending_investment,
                pending_sector_investment=pending_sector_investment,
                pending_symbol_investment=pending_symbol_investment,
                correlation_matrix=correlation_matrix,
                cash=effective_cash,
                pending_cash_outflow=pending_cash_outflow,
                # Spec §11.2 — the execution half of the gross ceiling. The
                # sweep vehicle has already been split out of `positions`
                # above, so `cash_park_symbol` here is belt-and-braces for
                # any future caller that has not.
                gross_ceiling=gross_ceiling,
                pending_gross_investment=pending_gross_investment,
                cash_park_symbol=(sweeper.symbol if sweeper is not None else None),
            )
            hard_violations = [v for v in violations if v.rule in HARD_BLOCK_RULES]
            if hard_violations:
                messages = [v.message for v in hard_violations]
                blocked_reasons.extend(messages)
                logger.warning("Hard risk block for %s %s: %s", decision.action, decision.symbol, "; ".join(messages))
                # sector_unresolved_* is advisory (never in HARD_BLOCK_RULES)
                # but must stay visible even when THIS decision is blocked
                # for a different reason (e.g. the single-name or gross
                # cap) — the whole point is
                # that an unresolved sector must never go quiet, and the
                # loop `continue`s past the ordinary remaining_violations
                # .extend below for a blocked decision.
                remaining_violations.extend(v for v in violations if v.rule.startswith("sector_unresolved"))
                continue

            remaining_violations.extend(violations)
            allowed_decisions.append(decision)

            from src.risk.rules import _effective_multiplier, _gross_multiplier

            raw_investment = total_value * (decision.allocation_pct / 100)
            is_short = decision.action == "SHORT"
            # Total exposure accumulates SIGNED contribution (hedges net
            # out). A SHORT moves it the OPPOSITE way a BUY of the same
            # symbol would — the matching flip lives in
            # RiskRuleEngine.check.
            signed_investment = raw_investment * _effective_multiplier(decision.symbol) * (-1.0 if is_short else 1.0)
            # Sector exposure accumulates GROSS (direction-agnostic magnitude).
            gross_investment = raw_investment * _gross_multiplier(decision.symbol)
            pending_investment += signed_investment
            # Deployment accumulates RAW notional for BUY *and* SHORT: both
            # commit capital, and neither leverage nor direction changes how
            # much of the book stops being idle cash.
            pending_raw_investment += raw_investment
            if not is_short:
                # Cash outflow is raw $ notional — leverage/direction don't
                # change the brokerage cash the BUY consumes. Inverse/
                # leveraged ETFs still cost their sticker price in cash.
                # A SHORT of any symbol never
                # spends this settled-cash pool (RiskRuleEngine.check), a
                # BUY of any symbol always does.
                pending_cash_outflow += raw_investment
            # Spec §11.2: gross is direction-agnostic — a BUY and a SHORT of
            # the same size consume the same ceiling. `gross_investment` is
            # already the leverage-adjusted unsigned magnitude.
            pending_gross_investment += gross_investment
            pending_symbol_investment[decision.symbol] = (
                pending_symbol_investment.get(decision.symbol, 0.0) + raw_investment
            )
            # Spec §12.2 — books into the `(sector, side)` bucket this order
            # would actually land in, so a pending SHORT never consumes the
            # long budget the next BUY in that sector is measured against.
            from src.risk.rules import accumulate_pending_sector

            accumulate_pending_sector(
                pending_sector_investment,
                _get_sector(decision.symbol),
                decision.action,
                gross_investment,
            )

        # Advisory check: projected capital at work vs the invested target
        # (`DESK_INVESTED_TARGET_PCT`, fixed at 100% by the owner mandate of
        # 2026-09-17 — macro no longer sets it). Does NOT block trades; emits
        # a non-hard violation so RiskManager sees the gap. It reports
        # UNDER-deployment only: a book at or above the target (margin is
        # enabled) is not a reason to scale anything down — leverage is
        # already capped, and enforced, by the §11.2 gross ceiling.
        if invested_target_pct is not None and total_value > 0:
            from src.risk.rules import (
                book_exposure,
                book_one_session_drift_pp,
                RiskViolation,
            )

            # Read through `book_exposure` — the SAME function that produces
            # PM's `invested_pct`. Before this, the two seats were judged
            # against one target using two definitions with opposite signs
            # (see the measured example on `book_exposure`), and the RM's leg
            # additionally `abs()`-ed a signed net, so a net-SHORT book read
            # as positively invested and was indistinguishable from the
            # equivalent long. `projected` is the book AFTER this batch:
            # deployment counts every approved order's raw notional (a SHORT
            # commits capital too), direction counts them signed.
            projected = book_exposure(
                positions,
                total_value,
                pending_deployed_usd=pending_raw_investment,
                pending_net_usd=pending_investment,
            )
            projected_invested_pct = projected.deployed_pct
            deviation = projected_invested_pct - invested_target_pct
            # The threshold is today's book's own one-session drift (sum of
            # position weight x ATR(14)/price) — see `book_one_session_drift_pp`
            # — not a typed band. An UNDER-deployed book beyond that drift is
            # the idle cash this advisory exists to surface; when the drift is
            # unmeasured (no ATR for the book) the advisory is withheld rather
            # than judged against a fallback number. The OVER branch that told
            # RM to "consider scale_all_buys" was deleted with the mandate —
            # there is no macro target left to be above, and scaling entries
            # down leaves exactly the idle cash the owner ruled out.
            drift = book_one_session_drift_pp(positions, total_value, getattr(self, "_atr_for_symbol", None))
            if drift is not None and -deviation > drift:
                remaining_violations.append(
                    RiskViolation(
                        rule="deployment_gap",
                        message=(
                            f"Projected invested {projected_invested_pct:.0f}% (capital at "
                            f"work; net direction {projected.net_pct:+.0f}%) is "
                            f"{-deviation:.1f}pp UNDER the fully-invested mandate "
                            f"({invested_target_pct:.0f}%), more than the book's one-session "
                            f"drift of {drift:.1f}pp (advisory — do NOT scale "
                            f"down BUYs or SHORTs for exposure reasons; idle cash is "
                            f"the cost here. If cutting anything, name a risk "
                            f"specific to the trade, not the gap.)"
                        ),
                        value=projected_invested_pct,
                        limit=invested_target_pct,
                    )
                )

        return allowed_decisions, remaining_violations, blocked_reasons

    def _persist_hard_risk_block(self, ctx: RunContext, reasons: str, *, stage: str) -> None:
        """Forensic record for a run where the deterministic hard-risk gate
        blocks EVERY candidate before `risk_manager` is ever called
        (Stage 2 Checkpoint C reconstruction gap).

        Before this, `RiskStage.run()` returned early with an in-memory
        `{"status": "hard_risk_block", "reason": ...}` dict above the
        `pipeline.risk_manager.review(...)` call — the reason reached a log
        line and a Telegram push, but no row in any table recorded which
        rule fired. This reuses the existing `agent_logs` table via the
        existing `insert_agent_log` mechanism: additive only, no schema
        change, no second risk system, no change to what gets blocked or
        why.

        `agent_name="risk_gate"` is a deliberately distinct sentinel from
        the real `"risk_manager"` LLM agent name so this can never be
        confused with an actual LLM call: `scripts/replay_decision.py`
        selects rows to replay by exact `agent_name` match and would
        otherwise try to replay an empty prompt; per-agent cost/roster
        views (`AGENT_NAMES`-driven) and `Database.agent_names_logged_on`'s
        dead-man's-switch check are unaffected since neither iterates
        unknown agent_names. `cost_usd`/`tokens_used` are 0 (known-zero,
        not unknown — no LLM call happened), not None, so
        `Database.sum_session_cost`'s any-null-means-unknown convention
        doesn't corrupt this run's otherwise-known research/PM cost total.

        Never raises — a persistence failure here must never affect the
        early-return risk decision itself, which has already been made by
        the time this is called.
        """
        try:
            self.db.insert_agent_log(
                agent_name="risk_gate",
                run_id=ctx.run_id,
                input_summary=f"deterministic hard-risk gate blocked all candidates ({stage})",
                input_message="",
                output_summary=f"HARD_RISK_BLOCK: {reasons}",
                full_response=reasons,
                model="deterministic",
                tokens_used=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                provider_requests=0,
                decision_id=ctx.decision_id,
                status="hard_risk_block",
            )
            record_guarded_pass(self, "risk_gate.persist_hard_risk_block", log=logger)
        except Exception as exc:
            record_guarded_pass(self, "risk_gate.persist_hard_risk_block", exc, log=logger)
            logger.warning(
                "hard_risk_block: failed to persist forensic record for run %s: %s",
                ctx.run_id,
                exc,
            )

    _FIELD_ALIASES = {
        "target": "take_profit",
        "tp": "take_profit",
        "stop": "stop_loss",
        "sl": "stop_loss",
        "price": "entry_price",
        "alloc": "allocation_pct",
    }

    def _apply_risk_modifications(
        self,
        decisions: list[TradeDecision],
        modifications,
        symbols_bars: dict | None = None,
        unapplied: list[dict] | None = None,
    ) -> tuple[list[TradeDecision], list[dict]]:
        """Apply RM-proposed field modifications to decisions.

        When a mod fails Pydantic validation, the decision is **dropped** rather
        than left at its original (un-tightened) value. RM's job is to be more
        protective; if their proposed change can't be applied, we cannot assume
        the un-modified decision is safe — the safest invariant is "RM tried to
        change this, we couldn't, so don't execute it". Previously a break left
        the original decision in place, silently dropping RM's protective intent.

        Two further guards (2026-09-03 audit), both because a field-valid
        `TradeDecision` is not the same thing as a MORE PROTECTIVE one, and
        this function's whole job is the latter:

        1. **An exit can never be silently cancelled by an edit.** A SELL or
           COVER's `allocation_pct` reaching 0 through an RM modification
           reads as "skip" at execution (see `pipeline_stages.py`'s
           `RiskStage.run`, "CLAUDE.md convention: allocation_pct=0 means
           SKIP") — a real exit vanishes with no distinguishable trace.
           Observed live 2026-08-24 on two symbols. If the RM genuinely
           believes an exit should not happen, it already has a real
           mechanism for that — `RiskVerdict.rejected_symbols`
           (`SymbolRejection`, handled in `RiskStage.run` before this method
           ever runs) — a REFUSAL, distinguishable from an edit. A
           modification is not that mechanism, so this method refuses the
           EDIT (keeps the exit at its pre-modification size) rather than
           refusing the trade itself: reverting is the closer match to "RM
           tried to protect this and couldn't", the same invariant already
           governing the validation-failure branch below, and it does not
           require inventing a new rejection channel for something the
           schema already has one for.
        1b. **An entry's `allocation_pct` may only be reduced — BUY and
           SHORT alike.** This seat exists to be MORE protective than the
           constructor, and nothing enforced that:
           `RiskModification.new_value` is unbounded. On an entry that adds
           to a held name the field is an INCREMENT on top of the existing
           weight, so an upward edit grows the position by more than the
           number reads (2026-09-18: an edit believed to cut a name to 30%
           left it at 50.8%). An increase is reverted and recorded in
           `rejected_mods` — same posture as guard 1, the trade still ships at
           the constructor's size. Checked AFTER schema validation, unlike
           guard 1: an out-of-range value (allocation_pct > 100) must keep
           hitting the validation branch below and DROP the decision, which is
           stricter still. This guard governs only the values Pydantic accepts.
        2. **A stop/target edit cannot bypass the checks a fresh decision
           would have to clear.** The constructor measures reward:risk on a
           range setup (refusing only an UNMEASURABLE ratio — a computed
           ratio is a ranking input, not a gate, and a breakout
           is never measured) and enforces a noise-band stop distance before
           a decision ever reaches the Risk Manager; both checks compared a
           modified decision only against itself, so an RM edit that widened
           a stop or pulled in a target could ship a BUY/SHORT whose
           reward:risk the constructor could not have measured, or a stop
           resting inside the ATR noise band. Invented numeric reward:risk
           floors are retired. This reuses the SAME arithmetic (`TradeDecision.reward_risk`,
           which is `models.reward_to_risk` — the one ratio definition every
           other gate in this codebase already shares) and the SAME
           configured floor (`RiskConfig.absolute_min_stop_atr_multiple`) the
           constructor uses, rather than re-deriving either. The noise-band
           half only runs when `symbols_bars` is supplied and yields a usable
           ATR reading; when it can't be computed the edit is refused rather
           than guessed at ("reject outright if it can't be safely
           re-verified" — the same posture as the noise-band check itself,
           which does not invent a stop distance it cannot measure).

        Returns `(decisions, rejected_mods)`. `rejected_mods` records every
        modification this method refused to apply — as opposed to a decision
        DROPPED outright by a validation failure — so the caller can persist
        a visible pipeline event for each one instead of the edit just
        disappearing.

        `unapplied` (board item 164, 2026-09-19) is an optional sink for the
        three outcomes `rejected_mods` deliberately does NOT carry, each of
        which used to reach the log only: a decision DROPPED because the
        edit failed schema validation (`outcome="dropped"`), an edit naming
        a field this method cannot modify, and an edit naming a symbol with
        no decision in the plan (both `outcome="modification_not_applied"`).
        Each entry names the symbol, the gate, the value asked for and the
        seat's own reason. Recording only — nothing here changes what is
        applied, reverted or dropped.
        """
        updated_decisions: list[TradeDecision | None] = list(decisions)
        modifiable_fields = {"allocation_pct", "entry_price", "stop_loss", "take_profit"}
        rejected_mods: list[dict] = []

        for mod in modifications:
            field = self._FIELD_ALIASES.get(mod.field, mod.field)
            if field != mod.field:
                logger.info("Risk mod field alias: '%s' -> '%s'", mod.field, field)
                mod = type(mod)(**{**mod.model_dump(), "field": field})
            if mod.field not in modifiable_fields:
                logger.warning("Risk mod ignored: unknown field '%s'", mod.field)
                if unapplied is not None:
                    unapplied.append(
                        {
                            "symbol": mod.symbol,
                            "field": mod.field,
                            "outcome": "modification_not_applied",
                            "gate": "rm_modification_unknown_field",
                            "requested": mod.new_value,
                            "seat_reason": mod.reason,
                            "reason": (
                                f"RM modification NOT APPLIED: {mod.symbol}.{mod.field} "
                                f"-> {mod.new_value} names a field the desk cannot "
                                f"modify (modifiable: "
                                f"{', '.join(sorted(modifiable_fields))}). The "
                                f"decision is unchanged. RM reason given: "
                                f"{mod.reason!r}"
                            ),
                        }
                    )
                continue

            for idx, decision in enumerate(updated_decisions):
                if decision is None or (decision.symbol.strip().upper() != mod.symbol.strip().upper()):
                    continue

                # Guard 1 — the seat may NEVER shrink a protective exit.
                # Owner ruling 2026-09-24 (final): the risk seat can never
                # block OR reduce a protective exit (SELL/REDUCE/COVER). It
                # used to revert only an edit that drove the exit's
                # allocation_pct to <= 0 (a silent cancel); an edit from
                # 100% -> 50% sailed through and cut how much the desk sold to
                # reduce risk. Now ANY downward allocation_pct edit on an exit
                # is reverted — the exit keeps its intended size. An UPWARD
                # edit (selling more) is left alone; it only reduces risk. This
                # is checked BEFORE the candidate is built: a valid smaller
                # allocation_pct would otherwise sail straight through Pydantic.
                if (
                    decision.action in ("SELL", "REDUCE", "COVER")
                    and mod.field == "allocation_pct"
                    and decision.allocation_pct > 0
                    and float(mod.new_value) < decision.allocation_pct
                ):
                    reason = (
                        f"RM modification would REDUCE {mod.symbol}'s exit "
                        f"allocation_pct ({decision.allocation_pct:.2f} -> "
                        f"{mod.new_value:.2f}), shrinking a {decision.action} the "
                        f"desk is using to reduce risk. Reverted — the seat may "
                        f"never block or reduce a protective exit; it stays at "
                        f"its intended size. RM reason given: {mod.reason!r}"
                    )
                    logger.warning("Risk mod REJECTED for %s: %s", mod.symbol, reason)
                    rejected_mods.append(
                        {
                            "symbol": mod.symbol,
                            "field": mod.field,
                            "reason": reason,
                        }
                    )
                    # updated_decisions[idx] already holds the unmodified
                    # decision — nothing to change, the exit still ships.
                    break

                candidate = decision.model_dump()
                candidate[mod.field] = mod.new_value
                try:
                    updated_decision = TradeDecision(**candidate)
                except ValidationError as exc:
                    logger.warning(
                        "Risk mod rejected for %s.%s %.4f -> %.4f: %s — "
                        "DROPPING decision (RM intended a protection we cannot apply)",
                        mod.symbol,
                        mod.field,
                        mod.original_value,
                        mod.new_value,
                        exc,
                    )
                    if unapplied is not None:
                        errors = "; ".join(
                            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
                            for err in exc.errors()
                        )
                        unapplied.append(
                            {
                                "symbol": decision.symbol,
                                "field": mod.field,
                                "outcome": "dropped",
                                "gate": "rm_modification_schema_invalid",
                                "action": decision.action,
                                "before": getattr(decision, mod.field, None),
                                "requested": mod.new_value,
                                "seat_reason": mod.reason,
                                "reason": (
                                    f"{decision.action} {decision.symbol} DROPPED: "
                                    f"the RM edit {mod.field} "
                                    f"{getattr(decision, mod.field, None)} -> "
                                    f"{mod.new_value} fails the order schema "
                                    f"({errors}), and a protection the seat asked "
                                    f"for that cannot be applied is not assumed "
                                    f"safe to skip. RM reason given: {mod.reason!r}"
                                ),
                            }
                        )
                    updated_decisions[idx] = None
                    break

                # Guard 1b — an `allocation_pct` edit on an ENTRY (BUY *or*
                # SHORT) may only REDUCE. The Risk Manager's stated job at
                # this seat is to be MORE protective than the constructor;
                # nothing in the schema enforced that for this field
                # (`RiskModification.new_value` is unbounded and
                # `TradeDecision.allocation_pct` only clamps 0-100), so a
                # larger number sailed through as a "protection". Compounding
                # it, on an entry that ADDS to a name already held the field
                # is an INCREMENT on top of the existing position, so an
                # upward edit grows it by more than the number suggests —
                # observed 2026-09-18, where an edit the seat believed cut a
                # name to 30% left it at 50.8%. The prompt now states the
                # increment and the resulting weight; this guard is the part
                # that holds regardless of what the model reasons.
                #
                # Board item 155 (2026-09-26): SHORT was folded in HERE. It
                # used to be policed one layer out, by
                # `_revert_entry_size_increases` in `src/pipeline_stages.py`,
                # only because this file was locked by another workstream on
                # 2026-09-18 — never because two enforcement points for one
                # rule were the right shape. The outer sweep is deleted. This
                # is now the SINGLE enforcement point, and
                # `tests/test_pipeline_stages.py` fails the build if a second
                # one reappears. A short is sized by the explicit mirror of
                # the long clamp and opens new risk exactly as a BUY does, so
                # one condition covers both sides.
                if (
                    decision.action in ("BUY", "SHORT")
                    and mod.field == "allocation_pct"
                    and float(mod.new_value) > decision.allocation_pct
                ):
                    reason = (
                        f"RM modification would INCREASE {mod.symbol}'s "
                        f"{decision.action} allocation_pct "
                        f"({decision.allocation_pct:.2f} -> "
                        f"{mod.new_value:.2f}). Reverted — the risk seat may "
                        f"only reduce an entry's size, never enlarge it; on "
                        f"an add this field is an increment, so an upward "
                        f"edit grows the position by more than the number "
                        f"reads. RM reason given: {mod.reason!r}"
                    )
                    logger.warning("Risk mod REJECTED for %s: %s", mod.symbol, reason)
                    rejected_mods.append(
                        {
                            "symbol": mod.symbol,
                            "field": mod.field,
                            "reason": reason,
                        }
                    )
                    # updated_decisions[idx] already holds the unmodified
                    # decision — the BUY ships at the constructor's size.
                    break

                # Guard 2 — a stop/target edit on a BUY/SHORT must not ship
                # a reward:risk the constructor would have refused, or (when
                # verifiable) a stop inside the ATR noise band.
                if decision.action in ("BUY", "SHORT") and mod.field in (
                    "stop_loss",
                    "take_profit",
                ):
                    floor_reason = self._risk_mod_floor_breach(
                        decision,
                        updated_decision,
                        mod,
                        symbols_bars,
                    )
                    if floor_reason is not None:
                        logger.warning(
                            "Risk mod REJECTED for %s: %s",
                            mod.symbol,
                            floor_reason,
                        )
                        rejected_mods.append(
                            {
                                "symbol": mod.symbol,
                                "field": mod.field,
                                "reason": floor_reason,
                            }
                        )
                        break

                # Guard 3 (board item 134) — a `stop_loss` or `entry_price`
                # edit on a BUY/SHORT must be reconciled back to the position
                # SIZE. The constructor sized the position for the ORIGINAL
                # stop distance: `shares = equity*risk_pct / |entry - stop|`,
                # so `allocation_pct` and the stop distance are two halves of
                # one granted dollar-risk budget. Guards 1b and 2 police
                # `allocation_pct` and the stop's noise band, but NOTHING
                # recomputed the size after
                # a stop/entry edit — so widening the stop (larger
                # |entry - stop|) while `allocation_pct` stayed fixed shipped a
                # position whose real dollar risk (shares x new stop distance)
                # EXCEEDED the granted budget, unflagged. This reconciles the
                # size so a wider stop shrinks the position and can never
                # enlarge dollar risk beyond what the pre-edit ticket carried
                # (desk doctrine: "wider stop -> smaller position, never larger
                # dollar risk"). A TIGHTER stop is deliberately NOT allowed to
                # auto-enlarge the position — the seat's remit is to be more
                # protective, and every sibling guard here fails toward the
                # smaller size — so the reconciliation takes the SMALLER of the
                # original and the recomputed allocation.
                if decision.action in ("BUY", "SHORT") and mod.field in (
                    "stop_loss",
                    "entry_price",
                ):
                    reconciled_alloc = self._reconcile_size_to_risk_budget(
                        decision,
                        updated_decision,
                    )
                    if reconciled_alloc is not None and reconciled_alloc < updated_decision.allocation_pct:
                        logger.info(
                            "Risk mod size reconciled for %s: %s edit widened "
                            "risk-per-share, allocation_pct %.2f -> %.2f to hold "
                            "dollar risk within the granted budget",
                            mod.symbol,
                            mod.field,
                            updated_decision.allocation_pct,
                            reconciled_alloc,
                        )
                        updated_decision = updated_decision.model_copy(
                            update={"allocation_pct": reconciled_alloc},
                        )

                logger.info(
                    "Risk mod applied: %s.%s %.4f -> %.4f (%s)",
                    mod.symbol,
                    mod.field,
                    mod.original_value,
                    mod.new_value,
                    mod.reason,
                )
                updated_decisions[idx] = updated_decision
                break
            else:
                logger.warning("Risk mod ignored: no matching decision for '%s'", mod.symbol)
                if unapplied is not None:
                    unapplied.append(
                        {
                            "symbol": mod.symbol,
                            "field": mod.field,
                            "outcome": "modification_not_applied",
                            "gate": "rm_modification_no_matching_decision",
                            "requested": mod.new_value,
                            "seat_reason": mod.reason,
                            "reason": (
                                f"RM modification NOT APPLIED: {mod.symbol} has no "
                                f"decision left in the plan to edit ({mod.field} -> "
                                f"{mod.new_value}), so nothing changed. RM reason "
                                f"given: {mod.reason!r}"
                            ),
                        }
                    )

        return [d for d in updated_decisions if d is not None], rejected_mods

    def _risk_mod_floor_breach(
        self,
        original: TradeDecision,
        modified: TradeDecision,
        mod,
        symbols_bars: dict | None,
    ) -> str | None:
        """Return a refusal reason if `modified` breaches a constructor
        risk-side floor, else None.

        **2026-09-17.** Invented reward:risk floors are retired. A computed
        or missing ratio does not refuse an RM edit. What still refuses the
        *edit* (not the ticket) is a stop pulled inside the ATR noise band.
        """
        if mod.field != "stop_loss" or not symbols_bars:
            return None

        # Noise-band check — only attempted when bars are available to
        # compute a real ATR reading. `RiskConfig.absolute_min_stop_atr_multiple`
        # is the same configured floor `PortfolioConstructor._widen_stop_past_noise`
        # enforces; this does not invent a new number.
        bars = symbols_bars.get(original.symbol)
        if not bars or len(bars) < 15:
            return None
        try:
            atr14 = compute_indicators(original.symbol, bars).atr_14
            record_guarded_pass(self, "risk_gate.risk_mod_floor_atr", log=logger)
        except Exception as exc:
            record_guarded_pass(self, "risk_gate.risk_mod_floor_atr", exc, log=logger)
            logger.warning(
                "Risk mod noise-band check skipped for %s: ATR unavailable (%s)",
                original.symbol,
                exc,
            )
            return None
        if atr14 is None or not math.isfinite(atr14) or atr14 <= 0:
            return None

        # Same defensive posture as `_optional_risk_number` above: tests
        # build this pipeline against `TradingPipeline.__new__`, which never
        # ran `__init__` and carries no `self.config` at all. Absence of a
        # real config means the floor cannot be verified — skip rather than
        # crash or guess at a multiple nobody configured.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        floor_multiple = _optional_risk_number(getattr(risk_cfg, "absolute_min_stop_atr_multiple", None))
        if floor_multiple is None:
            return None

        is_short = modified.action == "SHORT"
        entry = modified.entry_price
        new_stop = modified.stop_loss
        distance = (entry - new_stop) if not is_short else (new_stop - entry)
        band_edge = floor_multiple * atr14
        if distance < band_edge:
            return (
                f"modified stop ${new_stop:.2f} sits {distance:.2f} from "
                f"entry ${entry:.2f} — inside the {floor_multiple}x ATR14 "
                f"(${atr14:.2f}) noise band (${band_edge:.2f} minimum) the "
                f"constructor enforces. RM reason given: {mod.reason!r}"
            )
        return None

    @staticmethod
    def _reconcile_size_to_risk_budget(
        original: TradeDecision,
        modified: TradeDecision,
    ) -> float | None:
        """The `allocation_pct` that keeps `modified`'s dollar risk at or below
        the dollar risk the pre-edit `original` ticket carried, or None when it
        cannot be measured.

        Board item 134. The constructor sizes a position so the number of
        shares put its stop distance's worth of loss at exactly the granted
        risk budget: `shares = equity*risk_pct / |entry - stop|`, and
        downstream execution spends the resulting `allocation_pct` as
        `qty = equity * allocation_pct/100 / entry`. Substituting, the fraction
        of equity a ticket risks is

            dollar_risk / equity = allocation_pct/100 * |entry - stop| / entry

        — it depends only on the ticket's own fields, not on the book value.
        So the pre-edit ticket's own risk fraction is the budget to preserve
        (it is already the constructor's granted risk after every single-name,
        portfolio and sector clamp, so it never over-states what was granted).
        Solving that identity for the allocation that reproduces the SAME
        fraction under the edited entry/stop gives the reconciled size:

            reconciled = original_alloc * (|e0 - s0|/e0) / (|e1 - s1|/e1)

        The short-side gap-risk haircut the constructor applies to
        risk-per-share cancels in this ratio, so shorts need no special case.
        Returns the reconciled allocation only; the caller takes the smaller of
        it and the current allocation so a tighter stop can never auto-enlarge
        the position. None when either ticket is geometrically degenerate
        (non-finite or non-positive entry, or a zero pre/post risk-per-share),
        in which case the caller leaves the size untouched.

        Precision of the preserved budget:

        - For a STOP edit the reconciliation is EXACT: the entry is unchanged,
          so `allocation_pct` and stop distance are the only moving parts and
          the identity holds against whatever entry execution ultimately sizes
          off.
        - For an ENTRY edit it is exact ONLY when execution's sizing
          denominator equals the edited entry. Execution actually sizes off
          `sizing_price = max(today_print, entry)` for a long / `min(...)` for
          a short (`_place_buy_with_sizing` in `pipeline_stages.py`), so under
          market drift the denominator differs and the preserved budget is
          APPROXIMATE. It is bounded on the high side by the execution-time 5%
          `_qty_by_risk_budget` ceiling and this reconciliation only ever
          REDUCES the allocation, so the approximation can under-risk but never
          over-risk.

        Scope: this guarantee covers the RM EDIT only. An execution-time ATR
        stop-widen applied AFTER this stage is reconciled solely against that
        same 5% `_qty_by_risk_budget` ceiling (pre-existing behaviour, not
        introduced here) — this method does not and cannot re-run for it.
        """
        e0, s0 = original.entry_price, original.stop_loss
        e1, s1 = modified.entry_price, modified.stop_loss
        alloc0 = original.allocation_pct
        rps0 = abs(e0 - s0)
        rps1 = abs(e1 - s1)
        values = (e0, e1, rps0, rps1, alloc0)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            return None
        if e0 <= 0 or e1 <= 0 or rps0 <= 0 or rps1 <= 0:
            return None
        original_risk_fraction = alloc0 * (rps0 / e0)
        new_risk_per_alloc = rps1 / e1
        reconciled = original_risk_fraction / new_risk_per_alloc
        if reconciled >= alloc0:
            # A tighter stop (or unchanged risk-per-share) — never auto-enlarge;
            # keep the ticket's own already-valid size untouched.
            return alloc0
        # FLOOR to 2 dp rather than round: rounding could nudge the size back UP
        # a hundredth of a percent and, with it, dollar risk a hair over the
        # pre-edit budget. Flooring guarantees the reconciled size never exceeds
        # the exact budget-preserving allocation.
        return math.floor(reconciled * 100) / 100

    @staticmethod
    def _has_actionable_signal_fn(
        indicators,
        symbol: str,
        bars,
        positions,
        live_price: float | None = None,
    ) -> bool:
        """Pre-filter: only send symbols with interesting signals to the LLM.

        Lifted from a nested function in run_morning so MorningResearchStage
        can inject it as a dependency. Takes positions explicitly rather than
        closing over an outer scope.

        `live_price` (2026-09-14): during market hours the price-vs-band
        proximity check uses the live price, not the last completed close;
        the bands themselves stay on completed bars.
        """
        held_symbols = {p.symbol for p in positions}
        if symbol in held_symbols:
            return True
        if not isinstance(indicators, TechnicalIndicators):
            return True  # can't filter unknown types, pass through
        if indicators.rsi_14 is not None and (indicators.rsi_14 < 35 or indicators.rsi_14 > 65):
            return True
        if indicators.bb_upper and indicators.bb_lower and bars:
            last_close = live_price if isinstance(live_price, (int, float)) and live_price > 0 else bars[-1].close
            band_width = indicators.bb_upper - indicators.bb_lower
            if band_width > 0:
                if abs(last_close - indicators.bb_upper) / band_width < 0.1:
                    return True
                if abs(last_close - indicators.bb_lower) / band_width < 0.1:
                    return True
        if indicators.macd_hist is not None and len(bars) >= 27:
            # A MACD histogram merely being small is common, not a signal.
            # The original prefilter intended to catch a histogram changing
            # sign, but implemented only "near zero"; in production that
            # admitted most of the universe (36/75 sampled names on 2026-08-26
            # qualified solely through this clause).  Recompute the prior
            # completed bar and require an actual zero-line crossover.
            try:
                previous_hist = compute_indicators(symbol, bars[:-1]).macd_hist
                record_guarded_pass(NO_LEDGER, "risk_gate.signal_prefilter_prior_macd", log=logger)
            except Exception as exc:
                record_guarded_pass(NO_LEDGER, "risk_gate.signal_prefilter_prior_macd", exc, log=logger)
                previous_hist = None
            if previous_hist is not None and (
                (previous_hist < 0 < indicators.macd_hist) or (previous_hist > 0 > indicators.macd_hist)
            ):
                return True
        if indicators.volume_change_pct is not None and abs(indicators.volume_change_pct) > 50:
            return True
        if indicators.ma_20 and indicators.ma_50:
            spread = abs(indicators.ma_20 - indicators.ma_50)
            if indicators.atr_14 and indicators.atr_14 > 0:
                if spread < 0.5 * indicators.atr_14:
                    return True
            else:
                if spread / indicators.ma_50 < 0.02:
                    return True
        return False

    _refuse_queued_earnings_buys = staticmethod(refuse_queued_earnings_buys)
