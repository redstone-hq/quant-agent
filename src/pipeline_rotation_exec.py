"""Rotation EXECUTION — the order-placing half of opportunity-cost rotation.

Split out of ``src/pipeline_stages.py`` (pipeline split, step 12). Every
function below is the VERBATIM body from that file; nothing was renamed,
reordered or changed. ``src.pipeline_stages`` re-exports each name lazily,
so every existing import path and every ``src.pipeline_stages.<name>``
patch target keeps working.
"""

from __future__ import annotations

from src.sentinel.guarded import record_guarded_pass
from src.exit_quote import read_exit_quote
from src.exits.evidence_seats import failed_evidence_seats
from src.sizing_refusal import sizing_price_or_refusal
from src.stage_execution_parts.entry_order_pricing import no_price_detail, quote_side_paid
from src.stage_execution_parts.entry_order_type import entry_orders_are_market
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    _IN_FLIGHT_FILL_STATUSES,
    _entry_deployment_budget,
    _fractional_sizing_allowed,
    _min_position_risk_pct,
    _live_fill_price,
    _qty_by_risk_budget,
    _record_execution_skip,
    _record_pipeline_event,
    _single_name_execution_cap,
    _size_shares,
    _today_sizing_price,
    logger,
    replace,
)

from src.rotation_projection import (  # noqa: E402,F401  lifted, re-exported
    _projected_entry_cost,
    _projected_post_sale_book,
    _projected_post_sale_cash,
    _projected_sale_qty,
    _scaled_position,
    ProjectionRefused,
)
from src.rotation_buy_leg_post import (  # noqa: E402,F401  lifted, re-exported
    _alert_rotation_buy_leg_missing,
    _alert_rotation_executed,
    _drop_buys_sold_today_below_bar,
    _drop_rotation_buy_if_room_not_freed,
    _record_rotation_buy_leg_outcome,
)

#: Sentinel returned by `_rotation_ranked_margin_sell_reason` when the SELL
#: must NOT be submitted. Distinct from `None`, which means "not a
#: ranked-margin rotation close — carry on as before".
_ROTATION_SELL_REFUSED = object()


def _rotation_execution_enabled(pipeline) -> bool:
    """Phase 14b — is automatic opportunity-cost rotation ON?

    True for an explicit `execution.rotation_enabled is True` and nothing
    else, the same convention as `_repeg_settings` below and for the same
    reason: many tests build the pipeline with a MagicMock config whose
    auto-attributes are truthy, and a MagicMock must never read as "yes,
    close a real position on your own".
    """
    execution_cfg = getattr(getattr(pipeline, "config", None), "execution", None)
    return getattr(execution_cfg, "rotation_enabled", None) is True


def _rotation_ranked_margin_enabled(pipeline) -> bool:
    """Board item 39 — is the RANKED-MARGIN rotation tier executable?

    Requires `execution.rotation_ranked_margin_enabled is True` IN ADDITION
    to `_rotation_execution_enabled`, and the same `is True` convention for
    the same reason (a MagicMock config must never read as "yes, close a
    real position on your own").

    This flag decides whether the tier is ASKED about. It does not decide
    whether a sale can be built: `src/rotation.py::rotation_sell_reason`
    raises unconditionally without a `RotationClearance`, which only
    `_rotation_buy_leg_projected_refusal` below mints, and only after the
    replacement BUY has survived every gate in `REQUIRED_BUY_LEG_GATES`
    against PROJECTED POST-SALE state.
    """
    if not _rotation_execution_enabled(pipeline):
        return False
    execution_cfg = getattr(getattr(pipeline, "config", None), "execution", None)
    return getattr(execution_cfg, "rotation_ranked_margin_enabled", None) is True


#: The one entry-bar reason that was a SUM over seat evidence (R5, net
#: independent source score), retired 2026-10-19 (desk archetype): a record
#: written before then may still carry it, and it never sells. R2 (neutral
#: technical), R3 (not BUY-eligible), R6 (constructor refusal) and R7 (the
#: chart against the holding) are each a positive reading that a missing seat
#: cannot manufacture.
_NET_EVIDENCE_REASON_PREFIX = "R5 "


def below_bar_only_on_incomplete_evidence(reasons, failed_seats: tuple[str, ...]) -> bool:
    """Unknown is not fail: True when the ONLY grounds a holding fails the
    entry bar on is the evidence sum, and a seat that feeds that sum failed
    this run. Such a holding is not judged below the bar this run."""
    if not failed_seats:
        return False
    texts = [str(r) for r in (reasons or ())]
    return bool(texts) and all(t.startswith(_NET_EVIDENCE_REASON_PREFIX) for t in texts)


def _rotation_skip(pipeline, ctx, opportunity, reason: str, **details) -> None:
    """One durable `rotation` / `skipped` audit row. Every refusal to act
    lands here so the evening review can see WHY a surfaced comparison did
    not become a trade, rather than inferring it from a log line."""
    logger.info(
        "Rotation: not acting on %s -> %s (%s)",
        opportunity.held_symbol,
        opportunity.new_symbol,
        reason,
    )
    _record_pipeline_event(
        pipeline,
        ctx,
        opportunity.held_symbol,
        "rotation",
        "skipped",
        reason,
        new_symbol=opportunity.new_symbol,
        tier=opportunity.tier,
        **details,
    )


def _record_rotation_precheck(pipeline, ctx) -> None:
    """One durable `rotation` / `precheck` row per session, whatever the
    pre-check concluded — including when it concluded nothing.

    The gap this closes: `_apply_rotation_execution` returns silently when
    `precheck.opportunity is None`, and that silent return is the desk's
    COMMONEST rotation outcome — the book is full, every candidate was
    still ranked against what is held, and none of them won. It left no log
    line, no durable row and nothing in the owner's report, so a session
    that did the comparison looked identical to one that never made it.

    Runs regardless of `execution.rotation_enabled`: the comparison happens
    in the PM's own prompt either way, and whether the desk may ACT on it is
    a separate fact this row records rather than a reason to stay silent.
    Never raises — bookkeeping must not take a live session with it.
    """
    from src.rotation import RotationPrecheck, precheck_record

    try:
        precheck = getattr(
            getattr(pipeline, "portfolio_manager", None),
            "last_rotation_precheck",
            None,
        )
        if not isinstance(precheck, RotationPrecheck):
            return
        record = precheck_record(
            precheck,
            execute_enabled=_rotation_execution_enabled(pipeline),
            ranked_margin_enabled=_rotation_ranked_margin_enabled(pipeline),
        )
        logger.info(
            "Rotation pre-check: %s (headroom %.2f%% of a %.2f%% ceiling, binding [%s], %s vs %s at ratio %s%s)",
            record["outcome"],
            record["headroom_pct"],
            record["ceiling_pct"],
            record.get("binding", ""),
            record.get("held_symbol"),
            record.get("new_symbol"),
            record.get("ratio"),
            f", refused at {record['refusal_point']}" if record.get("refusal_point") else "",
        )
        # RUN-scoped, with the symbols in the payload. Scoping it to the
        # holding was tried and reverted on adversary review: this repo has
        # ruled three times (`src/execution/exit_path_records.py`, board
        # item 164, and the plan-edit rows in this file) that
        # `src/refusal_signature.py` reads EVERY symbol-scoped
        # `pipeline_event` as "this session considered that stock as a new
        # idea". A weakest HOLDING is not such a candidate, and this row
        # fires every session — it would have broken the monomorphic-refusal
        # streak on essentially every run and silently disarmed the jam
        # alarm. The near-miss fields are just as queryable in the payload.
        _record_pipeline_event(
            pipeline,
            ctx,
            None,
            "rotation",
            "precheck",
            record["outcome"],
            **{k: v for k, v in record.items() if k != "outcome"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rotation pre-check record failed: %s", exc)


def _same_rules_opportunity(pipeline, ctx, surfaced, positions):
    """The half-hour check's cull set under the 2026-10-13 same-rules ruling.

    The ranked-margin tier is returned unchanged (it is OFF by config, and a
    still-eligible name it proposed would be refused by the same gate in
    `RiskStage`). Otherwise every held LONG is judged by
    `src.exits.same_rules_gate.judge_holding`; those that fail every buy rule
    but timing (owner Option A, 14 Oct) form the categorical cull set,
    worst-first (most failing rules, then
    alphabetical). A holding whose bar cannot be evaluated, or bought today,
    is never in it. Returns `None` when nothing fails.

    ONE SALE PER CHECK STAYS: the walk below closes the first sellable name
    and `ctx.rotation` is a single slot that `ExecutionStage` and
    `_rotation_sell_gate` read for exactly one held symbol. Any other failing
    holding is sold at the next half-hour check, and a PM SELL of it this
    run passes the same gate with no per-run limit.
    """
    from src.exits.same_rules_gate import judge_holding, views_from_pipeline
    from src.rotation_parts.types import RotationOpportunity

    if surfaced is not None and surfaced.tier == "ranked_margin":
        return surfaced

    def _opportunity(sym, reasons, candidates):
        return RotationOpportunity(
            new_symbol=getattr(surfaced, "new_symbol", None),
            new_score=getattr(surfaced, "new_score", None),
            held_symbol=sym,
            held_score=None,
            tier="ineligible_hold",
            reasons=tuple(reasons),
            ineligible_candidates=tuple(candidates),
        )

    record = getattr(ctx, "held_bar_record", None) or {}
    cull = []
    for p in positions or []:
        sym = (p.symbol or "").strip().upper()
        if not sym or p.qty <= 0:
            continue
        views = views_from_pipeline(pipeline, ctx, sym, is_short=False)
        verdict = judge_holding(views)
        logger.info("Same-rules gate (half-hour check): %s %s — %s", sym, verdict.basis, verdict.detail)
        if verdict.sell_allowed:
            cull.append((sym, tuple(verdict.reasons)))
        elif below_bar_only_on_incomplete_evidence(record.get(sym, ()), views.unknown_seats):
            # Unknown is not fail: the durable row says which seat failed.
            _rotation_skip(
                pipeline,
                ctx,
                _opportunity(sym, record[sym], ()),
                "evidence_incomplete_seat_failed",
                failed_seats=",".join(views.unknown_seats),
                held_reasons=list(record[sym]),
            )
    if not cull:
        return None
    cull.sort(key=lambda c: (-len(c[1]), c[0]))
    return _opportunity(cull[0][0], cull[0][1], cull)


def _apply_rotation_execution(pipeline, ctx, portfolio_decision, positions, position_history: dict | None) -> None:
    """Phase 14b — turn the CATEGORICAL rotation comparison into an ordinary
    zero-size PM target, when the desk's own data says it is safe to.

    Runs after the PM's plan is parsed and grounded and BEFORE
    `PortfolioConstructor.construct_orders`, so the close it proposes is
    built, risk-checked, reviewed and executed by exactly the machinery a
    PM-decided close goes through — `_build_sell`, the hard risk rules, the
    AI Risk Manager (which can refuse it by symbol), the holding-discipline
    claim check in `RiskStage`, and `_submit_protected_sell` in
    `ExecutionStage`. This function adds a target to the plan and records
    why; it never places an order and never removes anything the PM asked
    for. See `src/rotation.py` for the doctrine and the owner request.

    Every guard below fails CLOSED — an unanswerable question means no
    sale — and every refusal is recorded as a `rotation`/`skipped` event:

      1. `execution.rotation_enabled` must be explicitly True (else this
         function is a no-op and the run is byte-for-byte Phase 14).
      2. The PM's own prompt must have surfaced a comparison this session,
         and it must be the categorical tier. The ranked-margin tier is
         information only (see `src/rotation.py`).
      3. The held name must be a LONG the broker actually shows. KEPT, and
         re-examined under the 2026-10-01 ruling rather than inherited: this
         function only appends a zero-size `TargetPosition`, which
         `_build_sell` turns into a SELL, and the SELL execution loop
         hard-refuses a SELL on a short (`existing[0].qty <= 0: continue`).
         A short needs the separate COVER path, whose caps, protective
         BUY-stop cancel/replace and partial-fill restore have never been
         exercised from here. Removing the restriction today would therefore
         either do nothing or put an unverified order shape on live capital
         with the remainder's protection unproven — which the one-way
         protection rule forbids. It is a REPORTED GAP, not a silent one:
         below-bar SHORTS are recorded every session by
         `holdings_below_entry_bar` and skipped under their own audited
         reason, and automating the cover leg is its own piece of work.
      4. RANKED-MARGIN TIER ONLY: the PM must itself have targeted the new
         candidate with a real size this session. The CATEGORICAL tier no
         longer requires it (owner ruling 2026-10-01) — a holding below the
         desk's own entry bar is closed whether or not the book is full and
         whether or not anything is queued to replace it.
      5. The PM must not already be targeting the held symbol (closing it
         itself, or adding to it). Either way the model has spoken and
         this function does not override a decision the PM made.
      6. Nothing may be in flight on the held symbol: no trades row for
         it today still `submitted`/`pending_submit`, no BUY of it today at
         all (a day-zero exit "never given a single day's normal range to
         breathe" is exactly what the exit noise band exists to prevent —
         OKLO 2026-08-26), no pending protection-restore WAL row (a sell
         already mid-flight), no pending re-peg row (an entry mid-chase).
      7. RANKED-MARGIN TIER ONLY: its structural protection must ALREADY be
         broken under the item-25 holding-discipline check. That tier sells
         a still-ELIGIBLE name for opportunity cost, which is not one of the
         three real triggers. On the CATEGORICAL tier the holding is by
         definition no longer eligible, and the owner ruled on 2026-10-01
         that failing the fresh-entry bar IS the real trigger, so protection
         is measured and reported but does not veto.
    """
    if not _rotation_execution_enabled(pipeline):
        return
    from src.models import TargetPosition
    from src.rotation import RotationPrecheck, rotation_proposal_reason

    precheck = getattr(
        getattr(pipeline, "portfolio_manager", None),
        "last_rotation_precheck",
        None,
    )
    if not isinstance(precheck, RotationPrecheck):
        return  # no precheck this session — nothing to act on
    if getattr(ctx, "held_bar_record", None) is None:
        # This run's deterministic assembly did not produce a record (it
        # failed): no judgement sale (unknown never sells).
        logger.info("Half-hour check: no held-bar record this run — no judgement sale")
        return
    # OWNER RULING 2026-10-13, "same rules as buying". The categorical cull
    # set is no longer the 25 Sep opposition-only STAY subset the PM's
    # ranking surfaced: it is every held long that FAILS every-buy-rule-but-
    # timing (Option A, 14 Oct: a neutral chart never culls) under the one
    # same-rules gate, with missing evidence never selling.
    opportunity = _same_rules_opportunity(pipeline, ctx, precheck.opportunity, positions)
    if opportunity is None:
        return  # no holding fails the entry bar this run — nothing to act on
    held_symbol = opportunity.held_symbol.strip().upper()
    # OWNER RULING 2026-10-01. The categorical tier surfaces a below-bar
    # holding with NO replacement candidate, so `new_symbol` is None on
    # exactly the case that ruling created. Normalised to "" here: the
    # only reader below is the RANKED-MARGIN buy-leg precondition, which
    # matches it against target symbols, and "" matches none of them.
    new_symbol = (opportunity.new_symbol or "").strip().upper()

    if opportunity.tier == "ranked_margin":
        # Board item 39. Executable only behind its own second switch, and
        # even then this only PROPOSES the close: the sale is withdrawn
        # again in `ExecutionStage._run_session` unless the replacement BUY
        # clears every gate in `REQUIRED_BUY_LEG_GATES` against projected
        # post-sale state. Nothing here can put it on the wire.
        if not _rotation_ranked_margin_enabled(pipeline):
            _rotation_skip(
                pipeline,
                ctx,
                opportunity,
                "ranked_margin_tier_not_enabled",
            )
            return
    elif opportunity.tier != "ineligible_hold":
        _rotation_skip(
            pipeline,
            ctx,
            opportunity,
            "unknown_rotation_tier",
            tier_seen=str(opportunity.tier),
        )
        return

    targets = list(getattr(portfolio_decision, "targets", None) or [])
    # OWNER RULING 2026-10-01. The buy leg is NO LONGER a precondition for a
    # categorical sale. MEASURED: this tier fired 8 times (24-25 Sep) and
    # died 8 of 8 right here, because the tier only ran on a full book and
    # the prompt then told the model there was no room to buy — so the model
    # never wrote the buy and the sell was never proposed. A holding below
    # the entry bar is now closed on its own merits. The RANKED-MARGIN tier
    # keeps the precondition: it sells a still-eligible name purely to fund a
    # replacement, so without the replacement it has no reason at all.
    if opportunity.tier == "ranked_margin":
        new_targeted = any(t.symbol.upper() == new_symbol and not t.is_close for t in targets)
        if not new_targeted:
            _rotation_skip(
                pipeline,
                ctx,
                opportunity,
                "pm_did_not_target_new_candidate",
            )
            return

    def _sellable_this_run(cand_symbol: str, cand_reasons):
        """Every per-holding sell guard for ONE below-bar candidate.

        Returns `(held_position, protection, history, cand_opportunity)` when
        this name may be closed this run, or `None` after recording exactly
        why it may not — so the caller advances to the next-worst below-bar
        holding instead of abandoning the rotation (board item 39). Every
        guard here is a fact about THIS name only; the buy-leg precondition
        is checked once, above, because it does not depend on which held name
        makes the room.
        """
        cand_opp = replace(
            opportunity,
            held_symbol=cand_symbol,
            reasons=tuple(cand_reasons),
        )
        held_pos = next(
            (p for p in (positions or []) if (p.symbol or "").upper() == cand_symbol),
            None,
        )
        if held_pos is None or held_pos.qty <= 0:
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "held_symbol_is_not_a_long_position",
                qty=getattr(held_pos, "qty", None),
            )
            return None
        failed_seats = failed_evidence_seats(ctx, cand_symbol)
        if cand_opp.tier == "ineligible_hold" and below_bar_only_on_incomplete_evidence(cand_opp.reasons, failed_seats):
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "evidence_incomplete_seat_failed",
                failed_seats=",".join(failed_seats),
                held_reasons=list(cand_opp.reasons),
            )
            return None
        if any(t.symbol.upper() == cand_symbol for t in targets):
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "pm_already_targets_held_symbol",
            )
            return None

        # In flight? Read from the desk's own durable state machine. Any
        # failure to answer is a refusal to act on THIS name, never an
        # assumption of "clear".
        try:
            today_rows = pipeline.db.get_trades(
                symbol=cand_symbol,
                limit=50,
                today_only=True,
            )
            bought_today = any(str(r.get("action") or "").upper() == "BUY" for r in today_rows)
            in_flight_rows = [
                r
                for r in today_rows
                if str(r.get("fill_status") or "").lower() in _IN_FLIGHT_FILL_STATUSES
                and str(r.get("action") or "").upper() != "HOLD"
            ]
            pending_restores = [
                r
                for r in pipeline.db.get_pending_protection_restores()
                if str(r.get("symbol") or "").upper() == cand_symbol
            ]
            pending_repegs = [
                r for r in pipeline.db.get_pending_repegs() if str(r.get("symbol") or "").upper() == cand_symbol
            ]
            record_guarded_pass(pipeline, "rotation_exec.in_flight_check")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(pipeline, "rotation_exec.in_flight_check", exc, log=logger)
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "in_flight_check_failed",
                detail=str(exc),
            )
            return None
        if bought_today:
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "held_symbol_bought_today",
            )
            return None
        if in_flight_rows:
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "order_in_flight_on_held_symbol",
                detail="; ".join(
                    f"{r.get('action')}:{r.get('fill_status')}:{r.get('broker_order_id')}" for r in in_flight_rows
                )[:400],
            )
            return None
        if pending_restores:
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "sell_already_in_flight_wal_row",
                detail=str(pending_restores[0].get("sell_order_id")),
            )
            return None
        if pending_repegs:
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "entry_repeg_in_flight",
                detail=str(pending_repegs[0].get("old_order_id")),
            )
            return None

        # Item-25 holding discipline: is the position still structurally
        # protected? Same method, same inputs `RiskStage` uses. A protected
        # (thesis-intact) name is NEVER sold — the walk passes OVER it to the
        # next below-bar name; it never overrides the discipline.
        cand_hist = (
            (position_history or {}).get(cand_symbol)
            or (position_history or {}).get(getattr(held_pos, "symbol", cand_symbol))
            or {}
        )
        try:
            cand_protection = pipeline._structural_protection_for_holding(
                symbol=cand_symbol,
                thesis_invalid_if=cand_hist.get("thesis_invalid_if"),
                entry_price=cand_hist.get("entry_price"),
                stop_loss=cand_hist.get("stop_loss"),
                is_short=False,
                run_id=ctx.run_id,
            )
            record_guarded_pass(pipeline, "rotation_exec.protection_check")
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(pipeline, "rotation_exec.protection_check", exc, log=logger)
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "protection_check_failed",
                detail=str(exc),
            )
            return None
        if cand_protection.protected and cand_opp.tier == "ranked_margin":
            # RANKED-MARGIN ONLY. That tier sells a name that still PASSES the
            # desk's own entry gates, purely to fund something else, and
            # opportunity cost is not one of item 25's real triggers — so a
            # thesis-intact name is surfaced there, never sold.
            #
            # CATEGORICAL tier, owner ruling 2026-10-01: item 25 protects a
            # "still-eligible, thesis-intact" position. A holding in `blocked`
            # is NOT still-eligible — it fails the identical bar a fresh buy
            # must clear — and the owner has now ruled that failing that bar
            # IS a real trigger ("it has to earn its right to be there").
            # Keeping this conjunct would have meant a name could fail the
            # entry bar indefinitely with its stop intact and never be sold,
            # which is the state the ruling exists to end. The protection
            # state is still measured and is still stated in the sale's
            # reason and on the owner-facing surfaces; it no longer vetoes.
            # Downstream is consistent: `holding_discipline_claim_check`
            # blocks only a regime-flip or bearish-state-change CLAIM proven
            # false, and a rotation reason claims neither.
            _rotation_skip(
                pipeline,
                ctx,
                cand_opp,
                "held_symbol_structurally_protected",
                protection_basis=cand_protection.basis,
                protection_detail=str(cand_protection.detail)[:400],
            )
            return None
        return held_pos, cand_protection, cand_hist, cand_opp

    # Board item 39. Walk the below-bar cull set worst-first and close the
    # FIRST name that clears every per-holding guard. The rotation is
    # abandoned only when EVERY below-bar holding is unsellable this run —
    # not, as before, when the single worst name happened to be structurally
    # protected. `ineligible_candidates` is empty on the ranked-margin tier
    # and on a directly-constructed opportunity, so both fall back to the one
    # `held_symbol` and behave exactly as before.
    cull_set = (
        opportunity.ineligible_candidates
        if (opportunity.tier == "ineligible_hold" and opportunity.ineligible_candidates)
        else ((held_symbol, opportunity.reasons),)
    )
    chosen = None
    for cand_symbol, cand_reasons in cull_set:
        chosen = _sellable_this_run(
            str(cand_symbol).strip().upper(),
            cand_reasons,
        )
        if chosen is not None:
            break
    if chosen is None:
        # Every below-bar holding was unsellable this run; each was recorded
        # under its own reason above.
        return
    _held_pos, protection, hist, opportunity = chosen
    held_symbol = opportunity.held_symbol

    # A PROPOSAL, not an authorisation — see `rotation_proposal_reason`.
    # For the categorical tier this is byte-for-byte the string
    # `rotation_sell_reason` produced before; for the ranked-margin tier it
    # says in the Risk Manager's own input that the close is contingent on
    # the replacement BUY clearing its execution gates.
    reason = rotation_proposal_reason(
        opportunity,
        protection_basis=protection.basis,
        protection_detail=str(protection.detail),
        headroom_pct=precheck.headroom_pct,
        ceiling_pct=precheck.ceiling_pct,
        floor_pct=precheck.floor_pct,
        # 2026-09-23: so the clause naming why there was no room states the
        # limit that actually bound. Without this the sale's own reason
        # claims 14.50% is "under the 0.50% minimum" on a funding-bound
        # rotation — false, on the record the Risk Manager reads.
        binding=tuple(precheck.binding or ()),
        entry_budget_usd=precheck.entry_budget_usd,
        min_entry_usd=precheck.min_entry_usd,
    )
    # A zero-size target IS this desk's "close it" instruction
    # (`TargetPosition.is_close`; `_build_sell` turns it into a full SELL).
    # `thesis_invalid_if` is carried from the position's own entry record
    # so the built order's reasoning shows the condition the desk was
    # holding it against, exactly as a PM-authored close would.
    portfolio_decision.targets.append(
        TargetPosition(
            symbol=held_symbol,
            direction="long",
            risk_allocation_pct=0.0,
            conviction="high",
            thesis=reason,
            thesis_invalid_if=str(hist.get("thesis_invalid_if") or ""),
        )
    )
    # `None` when the ruling sold a below-bar holding with nothing queued to
    # replace it. Never a placeholder score — see `RotationOpportunity`.
    new_symbol = opportunity.new_symbol or None
    new_score = float(opportunity.new_score) if opportunity.new_score is not None else None
    ctx.rotation = {
        "held_symbol": held_symbol,
        "new_symbol": new_symbol,
        # Board item 39: the execution stage branches on this. A rotation
        # dict without it is treated as the categorical tier, which is what
        # every pre-item-39 caller meant.
        "tier": opportunity.tier,
        # Minted (or not) by `_rotation_sell_gate`, immediately before
        # the close is submitted. `None` here is not a
        # default that decays open: the SELL loop refuses a ranked-margin
        # rotation sale outright unless a real `RotationClearance` is
        # sitting in this slot.
        "clearance": None,
        "opportunity": opportunity,
        "protection_basis_text": protection.basis,
        "protection_detail_text": str(protection.detail),
        "floor_pct": float(precheck.floor_pct),
        # Carried onto the context so the SELL built at the wire states the
        # same binding constraint the PROPOSAL did — the two must not
        # disagree about why the room was gone.
        "binding": tuple(precheck.binding or ()),
        "entry_budget_usd": precheck.entry_budget_usd,
        "min_entry_usd": precheck.min_entry_usd,
        "new_score": new_score,
        "held_reasons": list(opportunity.reasons),
        "protection_basis": protection.basis,
        "protection_detail": str(protection.detail),
        "headroom_pct": float(precheck.headroom_pct),
        "ceiling_pct": float(precheck.ceiling_pct),
        "reason": reason,
    }
    logger.warning(
        "Rotation: proposing a full close of %s (below the desk's own entry bar; replacement candidate: %s) — %s",
        held_symbol,
        new_symbol or "none",
        reason,
    )
    _record_pipeline_event(
        pipeline,
        ctx,
        held_symbol,
        "rotation",
        "proposed",
        reason,
        new_symbol=new_symbol,
        new_score=new_score,
        held_reasons=list(opportunity.reasons),
        protection_basis=protection.basis,
        protection_detail=str(protection.detail)[:400],
        headroom_pct=float(precheck.headroom_pct),
        ceiling_pct=float(precheck.ceiling_pct),
        tier=opportunity.tier,
    )


def _rotation_buy_leg_projected_refusal(
    pipeline,
    ctx,
    *,
    rotation,
    buy_decision,
    positions,
    total_value: float,
    rotation_sell,
    cash: float = 0.0,
    buy_decisions_before: list | None = None,
):
    """Would the rotation's replacement BUY be refused downstream, judged
    against the book as it will be AFTER this session's exits?

    Returns `(clearance, reason, detail)`: exactly one of `clearance` (a
    `src.rotation.RotationClearance`) and `reason` is not None.

    Board item 39. Every gate below is the SAME computation the execution
    stage runs later, fed projected post-sale inputs — not a second
    implementation of it.

    The list below is FOUR long and so is `REQUIRED_BUY_LEG_GATES`; a
    `gate_coverage_incomplete` refusal at the bottom of this function is
    what keeps the two from drifting, and this paragraph is the third copy
    of the count, so change all three together. It was six until
    2026-09-23, when the owner's removal of the account-level loss halt
    (PR #584) deleted the `daily_loss_recheck` refusal (retired-ok) the
    first gate anticipated, and five until 2026-09-24, when `below_min_notional`
    (retired-ok) was deleted the same way: the flat $500 `min_order_usd` (deleted)
    notional floor it named was arbitrary, not a broker minimum, and Alpaca
    charges no stock commission, so `_prevent_rotation_naked_sale` no
    longer refuses a rotation's replacement buy for re-sizing small but
    nonzero — only a genuine zero still refuses, via `insufficient_cash`.
    Nothing replaced either deleted gate: the gross exposure the loss halt
    shared with the §11.2 ladder is still gated, by `insufficient_cash`.

      * `no_price` / `stale_entry` — `_live_fill_price` and the same 5%
        deviation test the preflight applies. Neither depends on the sale
        at all, so these are exact now, not projected.
      * `qty_zero` — the preflight's own sizing helpers (`_size_shares`,
        `_qty_by_risk_budget`, `_fractional_sizing_allowed`) at the
        projected equity.
      * `insufficient_cash` — `_entry_deployment_budget` over the
        projected post-sale positions, equity and settled cash, drained by
        every entry earlier in the same session exactly as the submit loop
        drains it. This is what still carries the §11.2 gross ladder: the
        budget's ladder-backed branch measures the headroom the sale
        frees, so a rotation that would breach gross is refused here even
        though the account-level alarm is gone.

    **What this does NOT close, stated plainly.** The BUY submit loop can
    still refuse an entry for reasons no pre-check can evaluate in advance:
    the latency window (`latency_window`), the borrow gate on a SHORT, and
    any broker rejection. (Board item 183, 2026-09-30: a displayed quote
    through the entry ceiling is no longer one of them — it is recorded as
    `venue_quote_through_ceiling` and the order is still sent, because the
    limit is its own protection.) Those are not knowable before the sale, and the tape
    can also move between this projection and the real check. This gate
    removes the deterministic, knowable refusals — the ones that made the
    naked-sale outcome reproducible — and the residual is recorded in the
    PR and in `docs/INCIDENT_HISTORY.md` rather than papered over.
    """
    from src.rotation import REQUIRED_BUY_LEG_GATES, RotationClearance

    symbol = str(getattr(buy_decision, "symbol", "") or "").strip().upper()
    checked: list[str] = []

    # ONE projection, over the rotation's own close and nothing else.
    #
    # An earlier version projected the other exits this session too, and
    # then tried to bound the uncertainty by evaluating two books — "all
    # exits fill" and "only the rotation's fills". That is not a bound: the
    # worst case is per-position (an exit carrying an intraday LOSS fails
    # to fill while one carrying a GAIN fills), and that mixed book is
    # neither of the two. Sampling two points of 2^N and calling it
    # conservative is the same mistake as attempt 2, one level up.
    #
    # It is also unnecessary. This gate now runs from inside the SELL loop,
    # immediately before the rotation's own close is submitted, and the
    # rotation's close is ordered LAST among this session's exits. By the
    # time it is reached every other exit has a terminal status and the
    # account has been refreshed, so the other exits are a MEASUREMENT in
    # `positions`, not an assumption. The only thing left to project is the
    # one sale that has not happened yet — which is the thing a projection
    # is actually good for.
    # `positions` is a broker read taken after every other SELL this
    # session reached a terminal status, so everything else the session did
    # is already in it. A session with a COVER still pending never reaches
    # this function at all (`_pending_cover_symbols`), so the only thing
    # left to project is the one sale that has not happened yet.
    # The sale is a market order, so it fills at the LIVE bid; that is what
    # it is projected to bring in. No bid, no projection: the rotation is
    # refused with the reason rather than priced off a guessed fill.
    exit_quotes = {}
    if rotation_sell is not None:
        sell_symbol = str(getattr(rotation_sell, "symbol", "") or "").strip().upper()
        exit_quotes[sell_symbol] = read_exit_quote(pipeline.broker, sell_symbol)
    try:
        projected_positions, equity_for_weights = _projected_post_sale_book(
            positions,
            total_value,
            [rotation_sell] if rotation_sell is not None else [],
            [],
            quotes=exit_quotes,
        )
    except ProjectionRefused as exc:
        return None, "no_price", str(exc)

    # --- gates 1/2: price and entry staleness, exact now -----------------
    checked.append("no_price")
    market_price = _live_fill_price(pipeline, symbol)
    if (
        not isinstance(market_price, (int, float))
        or isinstance(
            market_price,
            bool,
        )
        or market_price <= 0
    ):
        return (
            None,
            "no_price",
            ("no verifiable live price for the replacement buy (daily bar close is not a fill reference)"),
        )
    market_price = float(market_price)
    # docs/WORK.md item 120: the SHARE COUNT divides the dollar allocation by
    # the sizing price, so it must be a real TODAY PRINT, never a quote mid
    # or a prior-session trade. `market_price` above (the fill reference) may
    # be a quote mid by design; the sizing divisor may not. Folded into the
    # `no_price` gate so `REQUIRED_BUY_LEG_GATES` coverage is unchanged.
    # An UNREADABLE price is refused too, its detail naming
    # `sizing_price_unreadable` so it never reads as a measured absence.
    sizing_print, _why, detail = sizing_price_or_refusal(
        _today_sizing_price,
        pipeline,
        symbol,
        "replacement buy",
    )
    if sizing_print is None:
        return None, "no_price", detail

    checked.append("stale_entry")
    try:
        entry_price = float(getattr(buy_decision, "entry_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        entry_price = 0.0
    if entry_price > 0:
        deviation = abs(entry_price - market_price) / market_price
        if deviation > 0.05:
            return (
                None,
                "stale_entry",
                (f"entry ${entry_price:.2f} is {deviation * 100:.1f}% from market ${market_price:.2f} (threshold 5%)"),
            )

    # --- gate 3: does the replacement round to a tradeable size? ---------
    checked.append("qty_zero")
    # Size off the TODAY PRINT (item 120), bounded conservatively by the
    # already-approved entry — never off the fill-reference mid.
    sizing_price = max(sizing_print, entry_price or 0.0)
    is_short = getattr(buy_decision, "action", "BUY") == "SHORT"
    # The same divisor the submit loop uses (src/stage_execution_parts/
    # entry_order_pricing.py): a market entry's risk budget divides by the
    # quote side it pays — ask for a BUY, bid for a SHORT — and a missing
    # side refuses the leg, never falls back to the print. The rotation
    # projection sized off the print before (it never received the
    # 2026-10-04 divisor fix), so it cleared buys the submit loop then sized
    # smaller or refused.
    risk_sizing_price = sizing_price
    if entry_orders_are_market(pipeline):
        quote = read_exit_quote(pipeline.broker, symbol)
        paid = quote_side_paid(is_short, quote["bid"], quote["ask"])
        if paid is None:
            return None, "no_price", no_price_detail("replacement " + ("SHORT" if is_short else "BUY"), is_short)
        risk_sizing_price = paid
        if not is_short:
            sizing_price = max(sizing_price, paid)
    fractional = _fractional_sizing_allowed(
        pipeline,
        symbol,
        is_short=is_short,
    )
    try:
        allocation_pct = float(
            getattr(buy_decision, "allocation_pct", 0.0) or 0.0,
        )
    except (TypeError, ValueError):
        allocation_pct = 0.0
    qty = _size_shares(
        pipeline,
        (float(equity_for_weights) * allocation_pct / 100.0) / sizing_price,
        fractional=fractional,
    )
    if qty <= 0:
        return None, "qty_zero", (f"allocation {allocation_pct:.2f}% at ${sizing_price:.2f} rounds to zero shares")
    risk_qty = _qty_by_risk_budget(
        pipeline,
        total_value=float(equity_for_weights),
        sizing_price=risk_sizing_price,
        stop_price=getattr(buy_decision, "stop_loss", 0.0),
        is_short=is_short,
        fractional=fractional,
    )
    if risk_qty is not None and risk_qty < qty:
        qty = risk_qty
    if qty <= 0:
        return (
            None,
            "qty_zero",
            (
                f"risk budget at ${risk_sizing_price:.2f} entry / "
                f"${getattr(buy_decision, 'stop_loss', 0.0)} stop rounds to zero "
                f"shares"
            ),
        )

    # --- gates 4/5: can the post-sale book actually FUND the replacement? -
    # Added after adversary review of attempt 3. These are the refusals a
    # rotation is MOST likely to hit, because a rotation only surfaces when
    # the risk headroom is already under the floor — and risk-based sizing
    # can ask for more notional than the sale frees whenever the new name's
    # stop is tighter than the old one's. Both are deterministic functions
    # of the post-sale book, so both belong here rather than in the
    # "unknowable" residual.
    #
    # The budget is deliberately a LOWER BOUND: it is measured on the
    # projected book with the projected cash and WITHOUT the cash sweep's
    # help. The sweep can only add deployable cash, never remove it, so a
    # replacement that fits here fits the real budget too. Being wrong in
    # this direction refuses a rotation that would have worked, which costs
    # an opportunity; being wrong the other way sells a position to fund an
    # order that is then refused, which costs the position.
    checked.append("insufficient_cash")
    projected_cash = _projected_post_sale_cash(
        cash,
        positions,
        [rotation_sell] if rotation_sell is not None else [],
        [],
        quotes=exit_quotes,
    )
    try:
        entry_budget, ladder_backed, budget_note = _entry_deployment_budget(
            pipeline,
            ctx,
            projected_positions,
            float(equity_for_weights),
            projected_cash,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            None,
            "insufficient_cash",
            (f"the post-sale entry budget could not be measured ({exc}), so the replacement buy cannot be cleared"),
        )
    # Earlier entries in the same session drain the pool before this one
    # reaches it — the submit loop subtracts each order's cost as it goes,
    # so the projection walks the same order.
    for earlier in buy_decisions_before or []:
        entry_budget -= _projected_entry_cost(
            earlier,
            float(equity_for_weights),
            budget_is_gross=bool(ladder_backed),
        )
    single_name_cap = _single_name_execution_cap(
        pipeline,
        float(equity_for_weights),
    )
    order_ceiling = min(entry_budget, single_name_cap)
    estimated_cost = qty * sizing_price
    if not is_short and estimated_cost > order_ceiling:
        affordable_qty = _size_shares(
            pipeline,
            order_ceiling / sizing_price,
            fractional=fractional,
        )
        if affordable_qty <= 0:
            return (
                None,
                "insufficient_cash",
                (
                    f"estimated cost ${estimated_cost:.2f} exceeds the "
                    f"${order_ceiling:.2f} deployable on the post-sale book "
                    f"({budget_note})"
                ),
            )
        # Fixed 2026-09-24 (retired the `below_min_notional` gate outright,
        # see `REQUIRED_BUY_LEG_GATES`): this used to refuse the rotation
        # whenever re-sizing to the post-sale budget landed under the flat
        # `min_order_usd` floor (deleted) — an arbitrary $500 with no broker minimum
        # behind it, and Alpaca charges no stock commission. A rotation
        # whose replacement buy re-sizes small but nonzero
        # (`affordable_qty > 0`, already checked above) is cleared, not
        # refused; the real "no shares fit" case is `insufficient_cash`
        # above.
        qty = affordable_qty

    # Owner rule 2026-08-27, the WHOLE position after the order at least
    # the floor, at the candidate's real stop and through the one shared
    # function execution itself uses (`src/risk/min_risk.py`). Without this
    # gate the held name could be sold and the replacement buy then refused
    # for `below_owner_min_risk`, stranding the cash.
    checked.append("below_owner_min_risk")
    from src.risk.min_risk import min_risk_shortfall

    # Broker-signed qty of the symbol on the projected book; read inline so
    # this module does not widen the set reaching the broker seam.
    held_signed = next(
        (
            float(getattr(p, "qty", 0) or 0)
            for p in projected_positions
            if str(getattr(p, "symbol", "") or "").upper() == str(symbol or "").upper()
        ),
        0.0,
    )
    held_same = max(0.0, -held_signed if is_short else held_signed)
    floor_pct = _min_position_risk_pct(pipeline)
    buy_stop = getattr(buy_decision, "stop_loss", None)
    floor_check = min_risk_shortfall(
        position_notional=(held_same + qty) * sizing_price,
        entry=sizing_price,
        stop=buy_stop,
        equity=float(equity_for_weights),
        floor_pct=floor_pct,
    )
    if floor_check.refused:
        return (
            None,
            "below_owner_min_risk",
            (
                f"on the post-sale book the replacement position would risk "
                f"{floor_check.risk_pct:.2f}% of equity at its ${buy_stop} stop, under the "
                f"owner's {floor_pct:.2f}% minimum risk per position"
                if floor_check.readable
                else f"the replacement buy's stop ({buy_stop}) is unreadable, so its risk is unknown"
            ),
        )

    missing = [g for g in REQUIRED_BUY_LEG_GATES if g not in checked]
    if missing:
        # Unreachable while this function and `REQUIRED_BUY_LEG_GATES` agree.
        # It is here so that they cannot silently stop agreeing: a gate added
        # to the list and not to this function refuses the sale rather than
        # clearing it on a check that was never run.
        return None, "gate_coverage_incomplete", (f"gates not evaluated: {', '.join(missing)}")
    clearance = RotationClearance(
        held_symbol=str(rotation.get("held_symbol") or ""),
        new_symbol=symbol,
        gates_checked=tuple(checked),
        projected_entry_budget=float(order_ceiling),
        # Truncated: this string reaches `rotation_sell_reason`, whose text
        # is cut at `ROTATION_REASON_MAX_CHARS` with the clearance clause
        # LAST, so an over-long note would delete the very thing it
        # documents.
        projected_budget_basis=str(budget_note)[:60],
        projected_positions=tuple(str(getattr(p, "symbol", "") or "").strip().upper() for p in projected_positions),
        projected_equity=float(equity_for_weights),
    )
    return clearance, None, None


def _pending_cover_symbols(cover_decisions, positions) -> tuple[str, ...]:
    """The symbols this session will actually COVER when the rotation's
    close is gated, in the order they will be covered.

    **A cover that cannot move the book does not count.** The COVER loop
    refuses two classes outright — a COVER against a symbol that is not
    held short (`existing[0].qty >= 0`), and one with
    `allocation_pct == 0` — and both take zero shares off the book, change
    no intraday P&L and move no gross. Counting them would refuse the
    rotation for a cause with no effect, every session the PM keeps
    proposing that dead cover, which is a statement about the desk rather
    than about the market. `_projected_sale_qty` applies the same sign and
    allocation agreement the COVER loop itself applies, so the filter here
    and the loop there cannot drift apart.

    Board item 39. The COVER loop runs AFTER the SELL loop, so at gate time
    no cover has happened and none can be measured — unlike the other
    SELLs, which the reorder makes measurable.

    **This refusal has outlived the argument that produced it, and that is
    recorded here rather than papered over (adversary review, 2026-09-23).**
    It was adopted because one projected book could not be the worst case
    for three disagreeing consumers: the daily-loss numerator, the
    volatility-relative threshold, and gross exposure. The owner's removal
    of the account-level loss halt (PR #584) deleted the first two. The
    survivor, `_entry_deployment_budget`, turns out to be cheaply boundable
    in both of its regimes: with `allow_margin: true` — what ships — it
    returns `ceiling_x * equity - held_gross` and never reads cash, a COVER
    lowers held gross, so simply NOT applying any cover is already the
    minimum headroom; with margin off it returns `min(headroom, cash)` and
    both terms are monotone in the set of covers assumed, so the lower
    bound over every fill outcome is `min(headroom with no covers, cash
    with all covers)` — two scalars, no enumeration.

    The refusal is kept anyway, and on one ground only: it is strictly the
    more conservative posture, `execution.rotation_ranked_margin_enabled`
    ships FALSE, and loosening a live-selling gate is not something to do
    in the same change that resolves a merge. Whoever turns the flag on
    should take the bound above instead of inheriting this. What must NOT
    be inherited is the old justification, which claimed the bound was
    impossible; it is not, and saying so kept a refusal standing on a
    reason that no longer exists.

    So today the desk does not guess: a rotation is refused outright on any
    session with a cover pending. That costs a rotation on cover days and
    fails toward not trading, which is the direction every other guard on
    this path fails in.
    """
    by_symbol = {str(getattr(p, "symbol", "") or "").strip().upper(): p for p in (positions or [])}
    symbols = []
    for decision in cover_decisions or []:
        symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
        if not symbol or symbol in symbols:
            continue
        position = by_symbol.get(symbol)
        if position is None:
            continue  # nothing held: the COVER loop skips it
        if _projected_sale_qty(decision, position) <= 0:
            continue  # a no-op cover: it cannot move anything to bound
        symbols.append(symbol)
    return tuple(symbols)


from src.rotation_sell_order import _rotation_sell_last  # noqa: E402,F401


def _rotation_sell_gate(
    pipeline,
    ctx,
    decision,
    buy_decisions,
    positions,
    total_value: float,
    cash: float,
    cover_decisions: list | None = None,
):
    """Board item 39 — may this RANKED-MARGIN rotation close be submitted?

    Returns `None` for any SELL that is not a ranked-margin rotation close;
    every pre-item-39 path lands there and is unaffected. Otherwise returns
    `(cleared, positions, total_value, cash)`, with a REFRESHED book the
    caller must adopt — clearing the gate against a fresh book while the
    loop then sizes and prices off a stale one would reintroduce, one
    statement later, exactly the divergence `_projected_sale_qty` exists to
    prevent.

    **Why it lives inside the SELL loop rather than ahead of it.** The
    rotation's close is ordered last (`_rotation_sell_last`), so by the
    time this runs every other SELL this session has been submitted and
    waited on to a terminal status. Re-reading the account here therefore
    MEASURES what those did instead of assuming it.

    COVERs are the exception: their loop runs AFTER this one, so no cover
    has happened, none can be measured, and — see `_pending_cover_symbols`
    — none can honestly be bounded either. A session with a cover pending
    refuses the rotation outright.

    An earlier version ran ahead of the whole loop and projected the other
    exits too, bounding the uncertainty by evaluating two books ("all exits
    fill" and "only this one fills"). That is not a bound — the worst case
    is per-position and is neither book — and it also put this gate in
    direct conflict with `apply_gross_ceiling`, which nets out EVERY
    planned exit when it sizes the replacement. Measuring removes both
    problems.

    `cleared=False` means: do not submit this close. The replacement BUY
    then dies on the existing `_drop_rotation_buy_if_room_not_freed` path,
    because `rotation["sell_order_id"]` is never set — the room it was
    granted was never freed. Both legs fall together and the desk simply
    keeps the position.

    Scope: the RANKED-MARGIN tier only. The categorical tier
    (`ineligible_hold`, already live) sells a holding that fails the desk's
    own entry rules today — on that ground alone. OWNER RULING 2026-10-01
    removed the broken-protection conjunct: a name may now be cut with its
    structural protection still intact, because a position that would not
    be bought today has to earn its place regardless of where its stop is.
    The protection state is still measured, still carried in the sale's
    reason and still said to the owner; it no longer vetoes. That sale
    stands on its own and nothing here touches it.
    """
    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return None
    held_symbol = str(rotation.get("held_symbol") or "").strip().upper()
    new_symbol = str(rotation.get("new_symbol") or "").strip().upper()
    if str(getattr(decision, "symbol", "") or "").strip().upper() != held_symbol:
        return None

    def _withdraw(reason: str, detail: str):
        logger.warning(
            "Rotation withdrawn BEFORE the sell (%s): %s. %s is NOT sold — "
            "the desk keeps the position rather than going naked.",
            reason,
            detail,
            held_symbol,
        )
        _record_pipeline_event(
            pipeline,
            ctx,
            held_symbol,
            "rotation",
            "withdrawn",
            reason,
            new_symbol=new_symbol,
            detail=str(detail)[:400],
            tier="ranked_margin",
        )
        _record_execution_skip(
            pipeline,
            ctx,
            held_symbol,
            "rotation_withdrawn",
            str(detail)[:400],
        )
        # `ctx.rotation` is MARKED withdrawn, not cleared. Clearing it would
        # make `_drop_rotation_buy_if_room_not_freed` a no-op, and the
        # replacement BUY — which the constructor sized on the premise that
        # this close frees room — would then go out against room that was
        # never freed, putting the book over the risk ceiling. Leaving the
        # dict in place with no `sell_order_id` is exactly the state that
        # function already reads as "not submitted, no room freed".
        rotation["withdrawn"] = reason
        rotation["clearance"] = None

    pending_covers = _pending_cover_symbols(cover_decisions, positions)
    if pending_covers:
        _withdraw(
            "cover_pending",
            f"this session still intends to cover "
            f"{', '.join(pending_covers)}, and the COVER loop runs after "
            f"this one — their effect on the daily-loss limit cannot be "
            f"measured yet and cannot be bounded either (the numerator, the "
            f"volatility-relative threshold and gross exposure each have a "
            f"different worst case). The desk does not guess: "
            f"{held_symbol} is kept.",
        )
        return False, positions, total_value, cash

    buy_leg = next(
        (d for d in (buy_decisions or []) if str(getattr(d, "symbol", "") or "").strip().upper() == new_symbol),
        None,
    )
    if buy_leg is None:
        _withdraw(
            "buy_leg_absent",
            f"the replacement buy of {new_symbol} is not in this session's "
            f"orders, so closing {held_symbol} would free room for nothing",
        )
        return False, positions, total_value, cash

    # Re-read the account. This is a measurement of everything the session
    # has already done, and the book the projection below starts from. A
    # read it cannot make is a refusal to act — the posture every other
    # guard on this path takes.
    try:
        account, positions, price_map = pipeline._refresh_account_state()
        total_value = float(
            account["portfolio_value"]
            if isinstance(account, dict)
            else getattr(account, "portfolio_value", total_value)
        )
        cash = float(account["cash"] if isinstance(account, dict) else getattr(account, "cash", cash))
    except Exception as exc:  # noqa: BLE001
        _withdraw(
            "account_refresh_failed",
            f"close withheld: the post-sale book could not be projected from a current account state ({exc})",
        )
        return False, positions, total_value, cash

    held = next(
        (p for p in (positions or []) if str(getattr(p, "symbol", "") or "").strip().upper() == held_symbol),
        None,
    )
    if held is None or float(getattr(held, "qty", 0.0) or 0.0) <= 0:
        # The refreshed book no longer holds it (a stop filled, a
        # broker-side close). The SELL loop would drop it silently two
        # statements below; a candidate must not leave this pipeline
        # without a durable, per-symbol reason.
        _withdraw(
            "held_position_gone",
            f"{held_symbol} is no longer held on a current broker read, so "
            f"there is nothing to close and no room to free for {new_symbol}",
        )
        return False, positions, total_value, cash

    clearance, reason, detail = _rotation_buy_leg_projected_refusal(
        pipeline,
        ctx,
        rotation=rotation,
        buy_decision=buy_leg,
        positions=positions,
        total_value=total_value,
        cash=cash,
        rotation_sell=decision,
        # The submit loop walks `buy_decisions` in order and subtracts each
        # order's cost from the pool as it goes, so everything ahead of the
        # rotation's own buy has already drawn the budget down by the time
        # it is reached.
        buy_decisions_before=list(
            buy_decisions[: buy_decisions.index(buy_leg)],
        ),
    )
    if clearance is None:
        _record_execution_skip(
            pipeline,
            ctx,
            new_symbol,
            str(reason),
            f"rotation buy leg refused on projected post-sale state: {detail}",
        )
        _withdraw(
            f"buy_leg_would_be_refused:{reason}",
            f"close withheld because the replacement buy of {new_symbol} would be refused ({reason}: {detail})",
        )
        return False, positions, total_value, cash

    rotation["clearance"] = clearance
    _record_pipeline_event(
        pipeline,
        ctx,
        held_symbol,
        "rotation",
        "buy_leg_cleared",
        "projected_post_sale_gates_passed",
        new_symbol=new_symbol,
        gates=list(clearance.gates_checked),
        projected_entry_budget=clearance.projected_entry_budget,
        projected_budget_basis=clearance.projected_budget_basis,
        projected_positions=list(clearance.projected_positions),
        projected_equity=clearance.projected_equity,
        tier="ranked_margin",
    )
    return True, positions, total_value, cash


def _rotation_ranked_margin_sell_reason(pipeline, ctx, decision):
    """The last barrier in front of a RANKED-MARGIN rotation close.

    Returns:
      * `None` — this SELL is not a ranked-margin rotation close. Every
        pre-item-39 path lands here and is unaffected.
      * a `str` — the close is permitted, and this is the reason built from
        the `RotationClearance` that permitted it.
      * `_ROTATION_SELL_REFUSED` — do not submit this SELL.

    Board item 39, and the reason it is written this way. Attempt 1 on this
    item took the structural barrier that made a ranked-margin sale
    impossible to BUILD and replaced it with a config boolean, so one truthy
    value anywhere in the settings chain was sufficient to put a real sale
    on the wire. The barrier here is `src/rotation.py::rotation_sell_reason`
    raising `ValueError` without a `RotationClearance` — an object carrying
    the projected post-sale numbers the replacement buy was cleared on, that
    no configuration can produce. This function reads no flag at all; it
    only asks whether the evidence exists, and refuses the sale when the
    answer raises.
    """
    from src.rotation import RotationOpportunity, rotation_sell_reason

    rotation = getattr(ctx, "rotation", None)
    if not isinstance(rotation, dict) or rotation.get("tier") != "ranked_margin":
        return None
    symbol = str(getattr(decision, "symbol", "") or "").strip().upper()
    if symbol != str(rotation.get("held_symbol") or "").strip().upper():
        return None
    opportunity = rotation.get("opportunity")
    if not isinstance(opportunity, RotationOpportunity):
        logger.error(
            "Rotation SELL of %s REFUSED: the run carries no rotation "
            "opportunity to build a reason from. The position is kept.",
            symbol,
        )
        _record_pipeline_event(
            pipeline,
            ctx,
            symbol,
            "rotation",
            "sell_refused",
            "no_opportunity_on_context",
            new_symbol=rotation.get("new_symbol"),
        )
        return _ROTATION_SELL_REFUSED
    try:
        return rotation_sell_reason(
            opportunity,
            protection_basis=str(rotation.get("protection_basis_text") or ""),
            protection_detail=str(rotation.get("protection_detail_text") or ""),
            headroom_pct=float(rotation.get("headroom_pct") or 0.0),
            ceiling_pct=float(rotation.get("ceiling_pct") or 0.0),
            floor_pct=float(rotation.get("floor_pct") or 0.0),
            clearance=rotation.get("clearance"),
            binding=tuple(rotation.get("binding") or ()),
            entry_budget_usd=rotation.get("entry_budget_usd"),
            min_entry_usd=rotation.get("min_entry_usd"),
        )
    except ValueError as exc:
        logger.error(
            "Rotation SELL of %s REFUSED at the wire: %s. The position is "
            "kept — the desk does not sell into an uncleared replacement.",
            symbol,
            exc,
        )
        _record_pipeline_event(
            pipeline,
            ctx,
            symbol,
            "rotation",
            "sell_refused",
            "no_clearance",
            new_symbol=rotation.get("new_symbol"),
            detail=str(exc)[:400],
        )
        _record_execution_skip(
            pipeline,
            ctx,
            symbol,
            "rotation_sell_refused",
            str(exc)[:400],
        )
        return _ROTATION_SELL_REFUSED
