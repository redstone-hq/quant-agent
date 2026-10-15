"""The held-position exit engine: everything that decides whether an open
position is SOLD, and the one method that executes the resulting actions.

Step 4 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210), clusters K and L,
re-measured against the step-3 branch before anything was touched. Moved
verbatim out of `src/pipeline.py` as a mixin, so `TradingPipeline` keeps every
one of these as its own attribute and every test that patches or calls them is
untouched.

This is the live-money SELL path: target-revision adjudication, structural
protection for a holding and the voicing of its break, exit-trigger
substantiation, the holding-discipline fact-check, the deterministic trails,
the event-risk block, the alignment exit (scan, cache,
per-holding verdict and reading record), the AI risk review of exits, the
refusal and approval records, and `_midday_execute_llm_actions`.

The module-level exit-trigger vocabulary travels with it: the
`_HARD_TRIGGER_KEYWORDS` tuple and `_reason_cites_hard_trigger`, plus
`_actions_with_scan_fallback` and `_reason_claims_alignment_exit`, whose only
caller is `_midday_execute_llm_actions`. All are re-exported from `src.pipeline`
so `from src.pipeline import ...` keeps working -- but a test that PATCHES one
of them on `src.pipeline` no longer reaches this module's code and must patch it
here instead (plan S5, silent-behaviour risk 1).

`_atr_for_symbol` sits inside this cluster's range and deliberately did NOT
move: it is read by the base class's `_evening_stop_proximity` and by
`PromptFactsMixin`, so it stays in `src/pipeline.py` as a thin delegation,
which lends it back to this mixin. Its neighbour `_constructor_cfg_or_none`
is GONE (2026-10-05): the deleted admission shell was its only caller.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import functools
import json as _json
import logging
from datetime import datetime, timedelta

from src.exits_parts.midday_gates import midday_pre_gates
from src.exits_parts.midday_holding_discipline import midday_holding_discipline
from src.exits_parts.midday_spent_trigger import midday_spent_trigger
from src.exits_parts.midday_state import SKIP, MiddayLoop
from src.models import ReasoningChain, TradeDecision
from src.exit_quote import read_exit_quote
from src.sentinel.guarded_exit import record_exit_guard
from src.trading_calendar import et_today

#: Exit-refusal code for a reviewer REDUCE (a model-originated partial sell).
CODE_PARTIAL_SELL_REFUSED = "partial_sell_refused_whole_exits_only"

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


from src.pipeline_exit_vocabulary import (  # noqa: F401  (re-exported)
    _CANONICAL_TRIGGER_NAMES,
    _VERIFIED_ON_CHART,
    _canonical_prose_names,
    _CHART_VERIFIED_TRIGGER_NAMES,
    _HARD_TRIGGER_KEYWORDS,
    _reason_cites_hard_trigger,
    _actions_with_scan_fallback,
    _reason_claims_alignment_exit,
    _collab_of,
)


class ExitEngineMixin:
    """Cluster K + L of `docs/PIPELINE_SPLIT_PLAN.md`: the sell-side engine."""

    def _symbols_already_trimmed_today(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords

        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._symbols_already_trimmed_today(*args, **kwargs)

    # --- lifted to src/exits/target_revision.py (TargetRevision); thin shims follow ---
    def _adjudicate_target_revision_flags(self, *args, **kwargs):
        """Thin shim: builds the standalone TargetRevision and calls it (body moved to src/exits/target_revision.py)."""
        from src.exits.target_revision import TargetRevision

        return TargetRevision(
            file_target_revision=_collab_of(self, "_file_target_revision"),
            broker=_collab_of(self, "broker"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
            risk_engine=getattr(self, "risk_engine", None),
        )._adjudicate_target_revision_flags(*args, **kwargs)

    def _file_target_revision(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords

        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._file_target_revision(*args, **kwargs)

    # --- lifted to src/exits/structural_protection.py (StructuralProtection); thin shims follow ---
    def _structural_protection_for_holding(self, *args, **kwargs):
        """Thin shim: builds the standalone StructuralProtection and calls it (body moved to src/exits/structural_protection.py)."""
        from src.exits.structural_protection import StructuralProtection

        return StructuralProtection(
            voice_structural_protection_break=_collab_of(self, "_voice_structural_protection_break"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
            risk_engine=getattr(self, "risk_engine", None),
        )._structural_protection_for_holding(*args, **kwargs)

    def _voice_structural_protection_break(
        self,
        *,
        symbol: str,
        run_id: str,
        check,
    ) -> None:
        """Record a decisive structural-protection break's plain-language
        reason as a durable row, which the session message reads. Never raises.

        The guard's verdict is PROVISIONAL (the PM may still sell or hold), so
        it no longer pages the owner on its own mid-session: the row below is
        read into the one line the session message carries for the stock,
        next to the PM's decision and any risk change or pruning. Owner,
        2026-10-14: one line per stock, not the same stock several times with
        verdicts that may disagree.

        FAILURES STILL GO OUT ALONE AND AT ONCE (owner ruling 2026-09-02): if
        the row cannot be written, the session message has nothing to read, so
        the verdict is sent as its own alert through
        `notifier.send_owner_alert` instead.

        SILENT-ACTION GUARD (#5): the dedup slot is consumed only AFTER the why
        has reached a surface (the row, or the fallback alert). If BOTH fail,
        the slot is left free so a later cycle retries — the desk must never
        act on a break without the why reaching the owner.
        """
        from src.risk.exit_guard import render_owner_break_message

        message = render_owner_break_message(symbol, check)
        if not message:
            return
        symbol_u = (symbol or "").strip().upper()
        dedup_key = (run_id, symbol_u, check.basis)
        seen = getattr(self, "_voiced_structural_breaks", None)
        if seen is None:
            seen = set()
            self._voiced_structural_breaks = seen
        if dedup_key in seen:
            return

        any_surface_ok = False

        # Board / dashboard: a durable, machine-readable row carrying the SAME
        # sentence, on the specialist_evidence table the journal reads.
        try:
            self.db.insert_specialist_evidence(
                run_id=run_id,
                agent_name="risk_manager",
                kind="structural_break_trend_context",
                scope="symbol",
                symbol=symbol_u,
                evidence_json=_json.dumps(
                    {
                        "protected": bool(check.protected),
                        "basis": check.basis,
                        "trend_context": check.trend_context,
                        "confirming_closes_needed": check.confirming_closes_needed,
                        "confirming_closes_seen": check.confirming_closes_seen,
                        "owner_reason": message,
                        # The bare clause (no symbol, no lead verb): what the
                        # session message's one line for the stock quotes.
                        "reason": check.owner_reason,
                    }
                ),
            )
            any_surface_ok = True
            record_exit_guard(self, "structural.board_reason_write")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                self,
                "structural.board_reason_write",
                e,
                logger,
                symbol=symbol_u,
                effect="verdict sent as its own alert instead",
            )

        # Fallback only: the row was not written, so the session message cannot
        # carry this verdict. `send_owner_alert` does NOT raise on a failed
        # send — it RETURNS False — so the surface only counts as reached when
        # the return is truthy (and, as a backstop, when it does not raise).
        if not any_surface_ok:
            try:
                from src import notifier as _notifier

                ok = _notifier.send_owner_alert(
                    f"{message} (Not recorded for the session message, so sent on its own.)",
                    symbols=[symbol_u],
                )
                any_surface_ok |= bool(ok)
                record_exit_guard(self, "structural.owner_alert_send")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(
                    self,
                    "structural.owner_alert_send",
                    e,
                    logger,
                    symbol=symbol_u,
                    effect="owner not told; dedup slot stays free",
                )

        # Consume the dedup slot only if the why reached at least one surface;
        # otherwise leave it free so a later cycle retries rather than the desk
        # acting silently.
        if any_surface_ok:
            seen.add(dedup_key)

    # --- lifted to src/exits/exit_substantiation.py (ExitSubstantiation); thin shims follow ---
    def _substantiate_exit_triggers(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitSubstantiation and calls it (body moved to src/exits/exit_substantiation.py)."""
        from src.exits.exit_substantiation import ExitSubstantiation

        return ExitSubstantiation(
            record_heal=_collab_of(self, "_record_heal"),
            require_paid_analysis=_collab_of(self, "_require_paid_analysis"),
            db=_collab_of(self, "db"),
            position_reviewer=_collab_of(self, "position_reviewer"),
        )._substantiate_exit_triggers(*args, **kwargs)

    # --- lifted to src/exits/holding_discipline.py (HoldingDiscipline); thin shims follow ---
    def _holding_discipline_check_for_exit(self, *args, **kwargs):
        """Thin shim: builds the standalone HoldingDiscipline and calls it (body moved to src/exits/holding_discipline.py)."""
        from src.exits.holding_discipline import HoldingDiscipline

        return HoldingDiscipline(
            build_active_state_changes=_collab_of(self, "_build_active_state_changes"),
            carry_forward_macro=_collab_of(self, "_carry_forward_macro"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            db=_collab_of(self, "db"),
        )._holding_discipline_check_for_exit(*args, **kwargs)

    def _apply_deterministic_trails(self, *args, **kwargs):
        """Thin shim: body moved verbatim to src/exits_parts/trails.py."""
        from src.exits_parts.trails import _apply_deterministic_trails

        return _apply_deterministic_trails(self, *args, **kwargs)

    def _exit_event_risk_block(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords

        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._exit_event_risk_block(*args, **kwargs)

    #: Per-run memo for the alignment verdict, keyed
    #: (run_id, symbol, is_short). The scan below and the confirmer inside
    #: `_midday_execute_llm_actions` ask the SAME question about the same
    #: position in the same pass; the chart read behind it costs bars plus a
    #: structural-protection evaluation, so it is computed once. Same inputs,
    #: same deterministic answer — this changes no verdict, only the count of
    #: reads. Declared at class level so an instance built without __init__
    #: (tests do this) still reads a value rather than raising.
    _alignment_exit_memo: dict | None = None

    def _alignment_exit_cached(
        self,
        *,
        symbol: str,
        thesis_invalid_if: str | None,
        is_short: bool,
        entry_price: float | None,
        stop_loss: float | None,
        run_id: str,
    ):
        """`_alignment_exit_for_holding`, computed at most once per
        (run, symbol, side). Never raises: a memo failure just recomputes."""
        key = (run_id, symbol, bool(is_short))
        memo = self._alignment_exit_memo
        if not isinstance(memo, dict):
            memo = {}
            self._alignment_exit_memo = memo
        if key in memo:
            return memo[key]
        verdict = self._alignment_exit_for_holding(
            symbol=symbol,
            thesis_invalid_if=thesis_invalid_if,
            is_short=is_short,
            entry_price=entry_price,
            stop_loss=stop_loss,
            run_id=run_id,
        )
        memo[key] = verdict
        return verdict

    # --- lifted to src/exits/alignment_exit.py (AlignmentExit); thin shims follow ---
    def _alignment_exit_scan(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit

        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._alignment_exit_scan(*args, **kwargs)

    def _record_alignment_reading(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit

        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._record_alignment_reading(*args, **kwargs)

    def _position_opened_today(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit

        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._position_opened_today(*args, **kwargs)

    def _alignment_exit_for_holding(self, *args, **kwargs):
        """Thin shim: builds the standalone AlignmentExit and calls it (body moved to src/exits/alignment_exit.py)."""
        from src.exits.alignment_exit import AlignmentExit

        return AlignmentExit(
            alignment_exit_cached=_collab_of(self, "_alignment_exit_cached"),
            structural_protection_for_holding=_collab_of(self, "_structural_protection_for_holding"),
            config=_collab_of(self, "config"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._alignment_exit_for_holding(*args, **kwargs)

    def _record_exit_refusal(
        self,
        *,
        symbol: str,
        run_id: str,
        action: str,
        code: str,
        dropped: bool,
        detail: str,
        layer: str,
    ) -> None:
        """Append-only per-symbol refusal/uncertainty record. Never raises."""
        from src.risk.exit_refusal import record_exit_refusal

        record_exit_refusal(
            self.db,
            symbol=symbol,
            run_id=run_id,
            action=action,
            code=code,
            dropped=dropped,
            detail=detail,
            layer=layer,
        )

    def _risk_review_exits(self, *args, **kwargs):
        """Thin shim: body moved verbatim to src/exits_parts/risk_review.py."""
        from src.exits_parts.risk_review import _risk_review_exits

        return _risk_review_exits(self, *args, **kwargs)

    def _record_exit_review_approvals(self, *args, **kwargs):
        """Thin shim: builds the standalone ExitRecords and calls it (body moved to src/exits/exit_records.py)."""
        from src.exits.exit_records import ExitRecords

        return ExitRecords(
            trade_executed_or_pending=_collab_of(self, "_trade_executed_or_pending"),
            record_exit_refusal=_collab_of(self, "_record_exit_refusal"),
            db=_collab_of(self, "db"),
            market=_collab_of(self, "market"),
        )._record_exit_review_approvals(*args, **kwargs)

    def _midday_execute_llm_actions(
        self,
        positions,
        review,
        run_id: str,
        already_trimmed_today: set[str] | None = None,
        metric_deltas: dict | None = None,
        risk_vetoed_symbols: set[str] | None = None,
        position_facts: dict | None = None,
        same_rules_allowed: set[str] | None = None,
    ) -> list[dict]:
        """Dispatch LLM-recommended SELL / REDUCE / TRAIL_STOP / COVER actions
        to broker.

        Dedups same-symbol conflicting actions by priority (SELL/COVER >
        REDUCE > TRAIL_STOP > HOLD) to avoid the broker seeing two orders
        fighting each other on one position. (A `blocked_symbols` argument
        used to suppress LLM exits on a symbol whose midday auto-take-profit
        sell was still in flight; that rule was deleted 2026-09-12 and no
        other system sell runs ahead of the reviewer in the same session.)

        COVER is the short-side twin of SELL/REDUCE (Stage 3 shorts gap
        fix): it is the ONLY lever the reviewer has on a held short (never
        SELL — the executor requires the action to match the held side,
        see the qty-sign gate below) and it routes through every protection
        a SELL/REDUCE gets — the named-trigger phrase gate, the exit
        guard's metric-contradiction veto, the noise band, the same-day-trim
        discipline, and (further down `run_position_review`) the AI Risk
        routing via `_risk_review_exits`. It always executes as a FULL
        close (`_full_sell_qty`, mirroring SELL) — the schema
        (`PositionAction`) carries no allocation fraction for it, unlike the
        PM's `TradeDecision.allocation_pct`, so there is no partial-COVER
        signal for this path to act on.
        """
        orders: list[dict] = []
        _priority = {"SELL": 0, "COVER": 0, "TRAIL_STOP": 2, "HOLD": 3}
        best_by_symbol: dict[str, dict] = {}
        actions_raw = review.actions if review else []
        actions_list = [a.model_dump() for a in actions_raw]
        # Owner ruling 2026-10-09 (docs/OUTCOME.md): never sell PART of a
        # held position — kept whole or sold whole. REDUCE is no longer a
        # reviewer action; a legacy REDUCE in a model answer is lifted out at
        # parse (`PositionReview.refused_reduce`) and refused here with a
        # durable reason. It is never converted into a SELL.
        for ai in list(getattr(review, "refused_reduce", None) or []):
            logger.warning(
                "Position reviewer: REDUCE %s refused — a held position is kept whole or sold whole",
                ai.get("symbol"),
            )
            self._record_exit_refusal(
                symbol=str(ai.get("symbol") or ""),
                run_id=run_id,
                action="REDUCE",
                code=CODE_PARTIAL_SELL_REFUSED,
                dropped=True,
                detail=f"REDUCE: {str(ai.get('reason') or '')[:400]}",
                layer="whole_exits_only",
            )
        for ai in actions_list:
            sym = (ai.get("symbol") or "").strip().upper()
            if not sym:
                continue
            curr = best_by_symbol.get(sym)
            if curr is None or _priority.get(ai.get("action"), 99) < _priority.get(curr.get("action"), 99):
                best_by_symbol[sym] = ai
        if len(best_by_symbol) < len(actions_list):
            dropped = len(actions_list) - len(best_by_symbol)
            logger.info(
                "Midday: collapsed %d duplicate same-symbol actions (priority SELL/COVER>REDUCE>TRAIL_STOP>HOLD)",
                dropped,
            )

        # THE ALIGNMENT SCAN — every held position is read against the
        # owner-ratified alignment exit here, before the early return below,
        # because a review that proposed nothing at all is exactly the
        # session in which the chart must still be allowed to speak.
        _scan_displaced: dict[str, dict] = {}
        self._alignment_exit_scan(
            positions,
            best_by_symbol,
            run_id=run_id,
            position_facts=position_facts,
            priority=_priority,
            displaced=_scan_displaced,
        )

        if not best_by_symbol:
            return orders

        already_trimmed = {
            symbol.strip().upper() for symbol in (already_trimmed_today or set()) if symbol and symbol.strip()
        }
        # Board item 74 — what the desk has ALREADY acted on today, so a
        # trigger cannot authorise a second cut of the same name on the same
        # record. Read ONCE per execution pass and appended to in-process as
        # cuts submit, so two actions inside THIS pass cannot double-cut
        # either. `None` means the read failed: that is uncertainty and the
        # layer fails OPEN (src/risk/spent_trigger.py).
        from src.risk.spent_trigger import (
            SPENT_LAYER,
            acted_trigger_payload,
            keep_executed_acted_triggers,
            parse_acted_triggers,
            spent_trigger_check,
        )

        try:
            _raw_acted = self.db.get_acted_exit_triggers_today()
            acted_today = None if _raw_acted is None else parse_acted_triggers(_raw_acted)
            # A trigger is spent by a cut that actually REDUCED the position,
            # never by one merely submitted. The executed set is built from
            # the same `_trade_executed_or_pending` contract the sibling
            # same-day-trim gate uses, so the two gates cannot hold opposite
            # views of what a real fill is: a rejected / cancelled / expired
            # zero-fill cut spends nothing and the name is fair game again.
            _executed_order_ids: set[str] | None = {
                str(r.get("broker_order_id"))
                for r in (self.db.get_trades(today_only=True, limit=200) or [])
                if r.get("broker_order_id") and self._trade_executed_or_pending(r)
            }
            acted_today = keep_executed_acted_triggers(
                acted_today,
                executed_order_ids=_executed_order_ids,
            )
            record_exit_guard(self, "spent_trigger.acted_today_read")
        except Exception as _e:  # noqa: BLE001 — a failed read is uncertainty
            record_exit_guard(
                self,
                "spent_trigger.acted_today_read",
                _e,
                logger,
                effect="layer fails OPEN for this pass",
            )
            acted_today = None
        # Entry context (thesis_invalid_if / entry price / entry stop) for the
        # holding-discipline claim check below. Built ONCE and only if some
        # exit actually reaches that gate — a HOLD-only or TRAIL_STOP-only
        # review must not buy the DB reads.
        hd_position_history: dict | None = None
        # The per-symbol phases lifted into src/exits_parts/ read the values
        # that are the same for every symbol of this pass from one object.
        _loop = MiddayLoop(
            owner=self,
            positions=positions,
            run_id=run_id,
            metric_deltas=metric_deltas,
            risk_vetoed_symbols=risk_vetoed_symbols,
            already_trimmed=already_trimmed,
            acted_today=acted_today,
            same_rules_allowed=frozenset(str(s).strip().upper() for s in (same_rules_allowed or ())),
        )

        for action_item in _actions_with_scan_fallback(
            best_by_symbol.values(),
            _scan_displaced,
            orders,
        ):
            act = action_item.get("action")
            if act not in ("SELL", "TRAIL_STOP", "COVER"):
                continue
            symbol = action_item.get("symbol", "")
            if midday_pre_gates(_loop, action_item, act, symbol) is SKIP:
                continue

            # NO SALE IS REFUSED FOR THE PRICE THE DESK PAID (owner ruling,
            # 2026-10-09). An entry-anchored ATR "noise band" used to stand
            # here and drop a discretionary SELL/REDUCE/COVER of a losing
            # holding whose loss from entry sat inside 1.0 x ATR x
            # sqrt(sessions held). The desk's own 2026-10-04 measurement found
            # loss from entry never signalled a finished trend
            # (docs/RESEARCH_FINDINGS.md), so the gate was removed whole.
            if act in ("SELL", "REDUCE", "COVER"):
                held_now = next((p for p in positions if p.symbol == symbol), None)
                reason_for_band = action_item.get("reason", "")

                # THE ALIGNMENT EXIT (owner ruling 2026-09-30, "exit on
                # ALIGNMENT, never on a target") — the desk's only sanctioned
                # way to realise a GAIN. (The entry-anchored noise band this
                # used to be the one exception to was removed 2026-10-09.)
                #
                # A sale claiming the trend is over is now VERIFIED, not trusted:
                # only a chart that confirms the last mark has been given up by
                # more than the give-back tolerance gets through. An unconfirmed
                # or unreadable chart DROPS the sale — the opposite posture to
                # the fail-open gates below, and deliberately so, because this is
                # the one exit the desk takes with no external event behind it
                # and possibly with the other seats still positive.
                #
                # THE READING IS TAKEN ON EVERY EXIT OF A HELD POSITION,
                # not only on the ones whose prose happens to name it. A
                # sale the model wanted for some other reason still leaves
                # a durable record of what the chart said about that
                # position's trend at that moment; without it, a position
                # the desk exited has no alignment record at all and the
                # evening review cannot tell an unread chart from a chart
                # that said hold. Only a sale that CLAIMS the alignment
                # exit is GATED by the verdict.
                alignment_claimed = _reason_claims_alignment_exit(
                    reason_for_band,
                    action_item.get("exit_trigger"),
                )
                if held_now is not None:
                    facts = (position_facts or {}).get(symbol, {}) or {}
                    verdict = self._alignment_exit_cached(
                        symbol=symbol,
                        thesis_invalid_if=getattr(held_now, "thesis_invalid_if", None)
                        or facts.get("thesis_invalid_if"),
                        is_short=(act == "COVER"),
                        entry_price=getattr(held_now, "avg_entry", None),
                        # IDENTICAL to the scan's inputs, including the
                        # position-facts fallback. `stop_loss` decides
                        # whether a broken-level mark exists, and both
                        # callers key the SAME memo — resolving it
                        # differently would let one of them read a verdict
                        # built from a stop the other never passed.
                        stop_loss=getattr(held_now, "stop_loss", None) or facts.get("stop_loss"),
                        run_id=run_id,
                    )
                    # EVERY verdict leaves a durable, machine-readable, per-symbol
                    # record — INCLUDING the "could not read the chart" states.
                    # Without them the desk cannot tell a position it HELD from
                    # one it failed to read, and neither the other seats nor the
                    # owner can see that an exit was considered at all. The parsed
                    # thesis MA period and the prose it came from are recorded
                    # with it: that text is model-written and unversioned, so a
                    # reword silently changes which price decides a sale, and
                    # without pinning it the record would not say which average
                    # actually decided this one.
                    det = (
                        f"{act}: {verdict.status} "
                        f"claimed={alignment_claimed} "
                        f"ma={verdict.thesis_ma_kind}{verdict.thesis_ma_period} "
                        f"breach_atr={verdict.breach_atrs} "
                        f"tolerance_atr={verdict.band_atrs} "
                        f"sessions_since_mark_lost={verdict.sessions_since_mark_lost} "
                        f"thesis={verdict.thesis_text!r} "
                        f"| {verdict.reason}"
                    )[:1200]
                    self._record_exit_refusal(
                        symbol=symbol,
                        run_id=run_id,
                        action=act,
                        code=verdict.code,
                        dropped=alignment_claimed and not verdict.exit_cleared,
                        detail=det,
                        layer=("alignment_exit" if alignment_claimed else "alignment_exit_observed"),
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol,
                            run_id=run_id,
                            status=f"alignment_exit_{verdict.status.lower()}",
                            detail=det[:400],
                        )
                        record_exit_guard(self, "alignment_exit.audit_write")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(
                            self,
                            "alignment_exit.audit_write",
                            e,
                            logger,
                            symbol=symbol,
                            effect="audit row not written",
                        )
                    logger.info(
                        "Alignment exit %s %s: %s (claimed=%s) — %s",
                        act,
                        symbol,
                        verdict.status,
                        alignment_claimed,
                        verdict.reason,
                    )
                    if alignment_claimed:
                        # A sale the same-rules gate decided keeps this as an
                        # evidence row only (owner 2026-10-13: one standard).
                        if not verdict.exit_cleared and not _loop.gate_decided(symbol, act):
                            continue
                        # Carry the chart's own words into the order reason
                        # so the owner and the other seats read WHY, not
                        # just THAT.
                        if verdict.owner_reason:
                            action_item["reason"] = (f"{action_item.get('reason', '')} | {verdict.owner_reason}")[:2000]

            reason_text = action_item.get("reason", "")
            if act in ("SELL", "REDUCE", "COVER"):
                from src.risk.exit_refusal import (
                    CODE_HARD_TRIGGER_UNCERTAIN,
                    CODE_UNRECOGNIZED_TRIGGER,
                    classify_trigger_reason,
                )

                trigger_judgment = classify_trigger_reason(
                    reason_text,
                    cites=_reason_cites_hard_trigger,
                    trigger=action_item.get("exit_trigger"),
                    trigger_evidence=action_item.get("trigger_evidence"),
                )
                if trigger_judgment == "unnamed":
                    logger.warning(
                        "Position reviewer: blocking %s %s — the reason names no "
                        "recognised trigger. Exits require NEW INFORMATION "
                        "(thesis invalidation, adverse news, earnings, regime "
                        "shift, sector shock, stop hit); "
                        "price action and soft flags are not triggers. Reason "
                        "was: %r",
                        act,
                        symbol,
                        str(reason_text)[:200],
                    )
                    try:
                        self.db.record_intraday_evaluation(
                            symbol=symbol,
                            run_id=run_id,
                            status="exit_blocked_no_named_trigger",
                            detail=f"{act}: {str(reason_text)[:400]}",
                        )
                        record_exit_guard(self, "exit_gate.audit_write")
                    except Exception as e:  # noqa: BLE001
                        record_exit_guard(
                            self,
                            "exit_gate.audit_write",
                            e,
                            logger,
                            symbol=symbol,
                            effect="audit row not written",
                        )
                    self._record_exit_refusal(
                        symbol=symbol,
                        run_id=run_id,
                        action=act,
                        code=CODE_UNRECOGNIZED_TRIGGER,
                        dropped=not _loop.gate_decided(symbol, act),
                        detail=f"{act}: {str(reason_text)[:400]}",
                        layer="hard_trigger",
                    )
                    if not _loop.gate_decided(symbol, act):
                        continue
                if trigger_judgment == "uncertain":
                    logger.error(
                        "Position reviewer: hard-trigger recogniser raised "
                        "on %s %s — failing OPEN on that gate (agent "
                        "application of the 2026-08-27 dead-model posture, "
                        "not a new owner ratification). Reason was: %r",
                        act,
                        symbol,
                        str(reason_text)[:200],
                    )
                    self._record_exit_refusal(
                        symbol=symbol,
                        run_id=run_id,
                        action=act,
                        code=CODE_HARD_TRIGGER_UNCERTAIN,
                        dropped=False,
                        detail=f"{act}: {str(reason_text)[:400]}",
                        layer="hard_trigger",
                    )
                reason_text = reason_text if isinstance(reason_text, str) else str(reason_text or "")

            _hd_verdict, hd_position_history = midday_holding_discipline(
                _loop,
                action_item,
                act,
                symbol,
                reason_text,
                hd_position_history,
            )
            if _hd_verdict is SKIP:
                continue

            spent = midday_spent_trigger(_loop, action_item, act, symbol)
            if spent is SKIP:
                continue
            existing = [p for p in positions if p.symbol == symbol]
            # COVER only matches a held SHORT (qty < 0); SELL / REDUCE /
            # TRAIL_STOP only match a held LONG (qty > 0) — same "the order
            # must match the held side" rule ExecutionStage's COVER loop
            # enforces for the PM's decision path (mirrors it here, not a
            # new rule). A COVER proposed against a long/flat position, or
            # a SELL/REDUCE/TRAIL_STOP proposed against a short, is dropped.
            if act == "COVER":
                if not existing or existing[0].qty >= 0:
                    logger.warning(
                        "Midday: skipping COVER %s — no matching short position",
                        symbol,
                    )
                    continue
            elif not existing or existing[0].qty <= 0:
                logger.warning("Midday: skipping %s %s — no matching position", act, symbol)
                continue
            prot = None
            try:
                if act == "TRAIL_STOP":
                    try:
                        from src.execution.scale_in import pending_protection_symbols

                        if symbol in pending_protection_symbols(self.db):
                            logger.info(
                                "Midday: TRAIL_STOP %s skipped — a protection-restore WAL row is in flight",
                                symbol,
                            )
                            continue
                        record_exit_guard(self, "midday_trail.pending_protection_check")
                    except Exception as exc:  # noqa: BLE001
                        record_exit_guard(
                            self,
                            "midday_trail.pending_protection_check",
                            exc,
                            logger,
                            symbol=symbol,
                            effect="restore-in-flight check skipped",
                        )
                    try:
                        new_stop = float(action_item.get("new_stop_price") or 0)
                    except (TypeError, ValueError):
                        new_stop = 0.0
                    if new_stop <= 0:
                        logger.warning(
                            "Midday: TRAIL_STOP %s skipped — missing/invalid new_stop_price",
                            symbol,
                        )
                        continue
                    if new_stop >= existing[0].current_price:
                        logger.warning(
                            "Midday: TRAIL_STOP %s skipped — new_stop $%.2f >= current $%.2f",
                            symbol,
                            new_stop,
                            existing[0].current_price,
                        )
                        continue
                    # Minimum-ratchet floor: a raise must land on a
                    # DIFFERENT stop price than the one resting at the
                    # broker -- at least one venue tick above it. Until
                    # 2026-10-02 this was 2% (`MIN_RATCHET_PCT`), a picked
                    # churn-appetite number that existed only because a
                    # stop move meant cancel-then-resubmit. The in-place
                    # amend is on main, so the floor is now read off the
                    # instrument: see `src.risk.trailing.min_ratchet_floor`
                    # and the ledger entry `src.risk.trailing.MIN_RATCHET_TICKS`.
                    # A rejection here keeps the existing (valid, looser) stop
                    # in place: protection is never removed, only left as-is.
                    # Old stop is broker truth; if it is missing/unreadable the
                    # floor cannot be computed, so this establishes protection
                    # rather than blocking it (the RC1 clamps still apply).
                    from src.execution.stop_read import read_stop

                    _old_read = read_stop(self.broker, symbol, db=self.db, context="midday min-ratchet floor")
                    old_stop = _old_read.price if _old_read.found else None
                    if old_stop is not None and old_stop > 0:
                        from src.risk.trailing import (
                            min_ratchet_floor,
                            venue_tick,
                        )

                        min_new_stop = min_ratchet_floor(old_stop)
                        if new_stop < min_new_stop - venue_tick(old_stop) / 2.0:
                            logger.warning(
                                "Midday: TRAIL_STOP %s skipped — new_stop "
                                "$%.4f does not clear the live stop $%.4f by "
                                "one venue tick (floor $%.4f); it is the same "
                                "stop after quantization. Old stop kept.",
                                symbol,
                                new_stop,
                                old_stop,
                                min_new_stop,
                            )
                            continue
                    # WIDTH IS ANSWERED BY ADJUSTING THE STOP, NEVER BY
                    # PLACING NONE (board item 185, 2026-09-30; board item
                    # 80's ruling; board item 56 route (c)'s shape).
                    #
                    # What used to be here. A flat refusal: a proposed stop
                    # under 50% of current price was dropped as a model
                    # typo, and the routine moved on -- placing nothing.
                    # Nothing fixed the 50%; it was picked, and the
                    # universe screen then DERIVED its volatility ceiling
                    # from it, so each end of the pair was justified only
                    # by the other.
                    #
                    # Why a refusal is the wrong answer here whatever the
                    # bound is. This check can only bind where the live
                    # broker stop was unreadable or absent -- where the
                    # stop IS readable the min-ratchet floor above has
                    # already refused anything that does not clear it, so
                    # a typo far below price is long gone. "The live stop
                    # could not be read" is precisely the case where the
                    # position may be carrying NO protection at all, and a
                    # refusal there ends the loop with the name still
                    # naked. That is the owner's board-item-80 failure in
                    # a different costume -- its ruling, quoted at
                    # `portfolio_constructor.
                    # STOP_REFUSAL_NO_STOP_NO_VOLATILITY`, is that "a
                    # missing volatility reading is never a reason to skip
                    # protection", and the general shape of it is that the
                    # desk does not answer a stop it dislikes by placing
                    # nothing. It is also the same ruling board item 56
                    # route (c) made about stop WIDTH specifically: a wide
                    # stop is answered by adjusting the trade (there, by
                    # sizing down), never by a refusal. There is no sizing
                    # lever on this path, so the adjustment available is
                    # the stop price itself.
                    #
                    # What happens instead. A proposal further below price
                    # than any stop this desk's own rules can produce is
                    # CLAMPED to that widest legitimate stop and PLACED.
                    # The bound is read off the instrument, not chosen:
                    # the widest multiple `PortfolioConstructor.
                    # _stop_atr_multiple` can actually return (the base
                    # `min_stop_atr_multiple` times the largest setup and
                    # regime scalers, 3.00 at today's settings) against
                    # THIS name's live ATR14. The clamped price is below
                    # the 1.25 x ATR noise floor by construction, so the
                    # noise-band clamp below cannot then reject it. If the
                    # name is so volatile that even that widest stop lands
                    # at or below zero, there is no legitimate stop to
                    # clamp to, so the proposal stands -- the same
                    # "something beats nothing" direction, and the case
                    # the universe screen's ceiling exists to keep out.
                    #
                    # The desk, not the model, chose that price, so it is
                    # recorded per-symbol and durably rather than only
                    # logged (`dropped=False` -- nothing was dropped).
                    #
                    # `atr` is fetched once here and reused by the
                    # noise-band clamp below. Unreadable ATR degrades to no
                    # clamp, the same rule the noise band already used;
                    # the proposal then stands, because placing the model's
                    # stop still beats placing none.
                    atr = self._atr_for_symbol(symbol)
                    if (old_stop is None or old_stop <= 0) and atr is not None:
                        from src.portfolio_constructor import (
                            widest_reachable_stop_atr_multiple,
                        )

                        _cfg = self.portfolio_constructor.cfg
                        widest = widest_reachable_stop_atr_multiple(
                            _cfg.min_stop_atr_multiple,
                        )
                        widest_stop = existing[0].current_price - widest * atr
                        if widest_stop > 0 and new_stop < widest_stop:
                            from src.risk.exit_refusal import (
                                CODE_TRAIL_CLAMPED_TO_WIDEST,
                            )

                            detail = (
                                f"TRAIL_STOP {symbol}: no live stop was "
                                f"readable, and the proposed ${new_stop:,.2f} "
                                f"sits further below the "
                                f"${existing[0].current_price:,.2f} price than "
                                f"the widest stop this desk can place "
                                f"({widest:.2f} x ATR14 ${atr:,.2f} = "
                                f"${widest_stop:,.2f}). Read as a model typo "
                                f"and CLAMPED to ${widest_stop:,.2f} -- the "
                                f"position may be unprotected, so a stop is "
                                f"placed, never skipped (board item 80)."
                            )
                            logger.warning("Midday: %s", detail)
                            self._record_exit_refusal(
                                symbol=symbol,
                                run_id=run_id,
                                action=act,
                                code=CODE_TRAIL_CLAMPED_TO_WIDEST,
                                dropped=False,
                                detail=detail[:400],
                                layer="midday_trail_width",
                            )
                            new_stop = widest_stop
                    # Noise-band clamp (RC1 exit-quality forensics,
                    # 2026-07-16): a stop inside 1.25×ATR14 of the current
                    # price sits inside one day's normal range — it converts
                    # routine volatility into a realized exit, so the old
                    # stop is kept. The band is read off THIS stock's own
                    # ATR14, so a tighten that clears it is applied however
                    # recently the last one landed — there is no day-count
                    # lock. A hard-trigger citation in the reason bypasses
                    # the clamp, mirroring the SELL/REDUCE gate. `atr` was
                    # read above for the typo guard; the fetch is not
                    # repeated. A refused tighten is recorded per-symbol and
                    # durably, never only logged.
                    if atr is not None and not _reason_cites_hard_trigger(action_item.get("reason", "")):
                        noise_floor = existing[0].current_price - 1.25 * atr
                        if new_stop > noise_floor:
                            from src.risk.exit_refusal import CODE_NOISE_BAND

                            detail = (
                                f"TRAIL_STOP {symbol} refused: new stop "
                                f"${new_stop:,.2f} is inside the 1.25 x ATR "
                                f"noise band (floor ${noise_floor:,.2f}, ATR14 "
                                f"${atr:,.2f}); routine volatility would fill "
                                f"it. Old stop kept; cite a hard trigger to "
                                f"bypass."
                            )
                            logger.warning("Midday: %s", detail)
                            self._record_exit_refusal(
                                symbol=symbol,
                                run_id=run_id,
                                action=act,
                                code=CODE_NOISE_BAND,
                                dropped=True,
                                detail=detail[:400],
                                layer="midday_trail_noise_band",
                            )
                            continue
                    from src.execution.stop_records import (
                        accepted_stop_order,
                        replace_stop_and_record,
                    )

                    order = replace_stop_and_record(
                        self.broker,
                        self.db,
                        symbol,
                        new_stop,
                        run_id=run_id,
                        caller="midday_trail_stop",
                    )
                    if order and not (isinstance(order, dict) and not accepted_stop_order(order)):
                        if isinstance(order, dict):
                            order.setdefault("action", "TRAIL_STOP")  # audit F5
                        orders.append(order)
                        self.db.insert_trade(
                            symbol=symbol,
                            action="TRAIL_STOP",
                            qty=existing[0].qty,
                            price=new_stop,
                            reasoning=action_item.get("reason", "midday trailing stop"),
                            run_id=run_id,
                            stop_loss=new_stop,
                            broker_order_id=order.get("id"),
                            fill_status="submitted",
                        )
                        logger.info(
                            "Midday action: TRAIL_STOP %s → $%.2f — %s",
                            symbol,
                            new_stop,
                            action_item.get("reason"),
                        )
                    continue

                if act == "COVER":
                    # COVER is always a FULL close here — see the docstring
                    # for why (no allocation fraction on this schema).
                    # `existing[0].qty` is the NEGATIVE broker qty; every
                    # downstream qty (WAL specs, fill_qty, insert_trade) is
                    # an absolute magnitude, never the signed qty.
                    qty = self._full_sell_qty(abs(existing[0].qty))
                    if qty is None:
                        continue
                    # A plain DAY MARKET buy-to-cover — see
                    # src/exit_quote.py.
                    order_limit = None
                    position_qty = abs(existing[0].qty)
                    close_side = "buy"
                else:
                    qty = self._full_sell_qty(existing[0].qty)
                    if qty is None:
                        continue
                    # A plain DAY MARKET sell — see src/exit_quote.py.
                    order_limit = None
                    position_qty = existing[0].qty
                    close_side = "sell"
                # Measure what the market order costs; never blocks the exit.
                read_exit_quote(self.broker, symbol)
                # audit F1 review #1: snapshot -> persist WAL -> cancel.
                # One record, written when the cut is sent: now, or by the run's
                # re-check if the stop's cancel is unconfirmed (put off).
                book = functools.partial(
                    _book_midday_exit, self, symbol, act, existing[0].current_price, action_item, run_id, acted_today,
                    orders,
                )  # fmt: skip
                sale = self._submit_protected_sell(
                    symbol=symbol,
                    qty=qty,
                    limit_price=order_limit,
                    reference_price=existing[0].current_price,
                    position_qty_before_sell=position_qty,
                    label=act,
                    side=close_side,
                    book=book,
                )
                if sale is None:
                    continue
                order, prot = sale
                book(order, qty)
                record_exit_guard(self, "midday.order")
            except Exception as e:
                record_exit_guard(
                    self,
                    "midday.order",
                    e,
                    logger,
                    symbol=symbol,
                    effect="order failed; exit not placed",
                )
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops — the same per-name
            # discipline the de-lever loops got (docs/WORK.md item 111).
            # Finalizing the batch once after the loop left every earlier
            # symbol with no protective stop while later symbols were
            # cancelled, submitted and waited on. Runs even when the ledger
            # write above raised: the stops are off and the order is live.
            # Which names exit, how much and at what limit are unchanged.
            if prot is not None:
                self._finalize_pending_protections(
                    [prot],
                    context="Midday reviewer",
                )
        return orders


def _book_midday_exit(host, symbol, act, price, action_item, run_id, acted_today, orders, order, qty) -> None:
    """The record of one midday cut the broker accepted: its trade row and what authorised it.

    Written when the cut is sent: at once, or later by the deciding run's
    re-check of a cut put off on an unconfirmed stop cancel
    (src/protection/put_off_sales.py). `qty` is what was sent."""
    from src.risk.spent_trigger import acted_trigger_payload

    orders.append(order)
    host.db.insert_trade(
        symbol=symbol,
        action=act,
        qty=qty,
        price=price,
        reasoning=action_item.get("reason", "midday review"),
        run_id=run_id,
        broker_order_id=order.get("id"),
        fill_status="submitted",
    )
    # Board item 74 — record what authorised this cut. Written
    # at SUBMIT, the only moment the trigger and its evidence
    # are in hand; it does not by itself spend the trigger. The
    # reader believes this row only once the order is known to
    # have executed (see `keep_executed_acted_triggers`), so a
    # rejected or unfilled cut spends nothing. The in-process
    # list is appended too: a later action in THIS same pass
    # sees it without a second DB read, and inside one pass the
    # order is as live as it will get.
    _acted = acted_trigger_payload(
        symbol=symbol,
        trigger=action_item.get("exit_trigger"),
        evidence=action_item.get("trigger_evidence"),
        action=act,
        run_id=run_id,
        broker_order_id=str(order.get("id") or ""),
    )
    if _acted is not None:
        try:
            host.db.record_acted_exit_trigger(
                run_id=run_id,
                payload_json=_acted.to_json(),
                symbol=_acted.symbol,
            )
            record_exit_guard(host, "spent_trigger.record_acted")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                host,
                "spent_trigger.record_acted",
                e,
                logger,
                symbol=symbol,
                effect="a second cut today would not be caught",
            )
        if acted_today is not None:
            acted_today.append(_acted)
    logger.info(
        "Midday action: %s %s %s — %s",
        act,
        host._format_qty(qty),
        symbol,
        action_item.get("reason"),
    )
