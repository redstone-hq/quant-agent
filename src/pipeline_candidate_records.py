"""Candidate-accounting and heal records (split out of src/pipeline_stages.py).

Moved VERBATIM out of `src/pipeline_stages.py`, following the pattern of
`src/pipeline_seat_evidence.py`: the execution-skip row, the
typed pipeline-event row, and the portfolio
manager's candidate accounting (the mechanical heal, its ONE paid re-ask under
the per-seat cap, the per-symbol reason rows and the never-fatal heal record).
Every function takes the pipeline and run context as plain arguments and
reads each collaborator off that handed-in host per call, so each can be
exercised with stand-ins and no `TradingPipeline` is ever built here
(`tests/test_boundary_pipeline_candidate_records.py` is the clause-5
witness). Every moved name is re-exported from `src.pipeline_stages` through
its one lazy re-export table, so each original import path and each test
patch target is unchanged; the write-through mirror on that module keeps a
patched name the same object on both sides.

This module sits BELOW `src.pipeline_stages`: it imports its shared helpers
from their own homes, never from the stage module, and the stage module
imports it, so the graph stays acyclic. Not in `src.number_sources.SCOPED_PATHS`: the
block carries no ledgered number site. This module must not import
`src.pipeline` and never reaches the broker seam. `_record_scale_in_window_closed`
stays behind in `src.pipeline_stages` on purpose: its body imports the broker
seam, which the import-layering guard refuses in a part.
"""

from __future__ import annotations

import logging

from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs
from src.pipeline_stage_helpers import _persist_evidence, record_stage
from src.sentinel.order_attempts import record_order_attempt_from_event
from src.storage.event_journal import pipeline_event_fields

logger = logging.getLogger(__name__)


def _record_execution_skip(pipeline, ctx, symbol: str, reason: str, detail: str) -> None:
    """Durable record of a deterministic BUY skip in the execution phase.

    Every skip path in the BUY loop used to be a log-only `continue`: the
    DB, funnel, Mission Control and the evening reflection all read a
    session whose approved BUYs were dropped here as a deliberate no-trade
    (2026-08-19: three risk-approved BUYs skipped as unfunded; the evening
    analyst concluded the system needed "proactive idea generation").
    Appends to ctx.execution_skips (drives the run's final status) and
    persists an `execution_skip` evidence row (drives the funnel/journal).
    Best-effort by construction — persistence failure never affects the
    skip decision itself (trading-core rule).
    """
    ctx.execution_skips.append(
        {"symbol": symbol, "reason": reason, "detail": detail},
    )
    import json as _json

    _persist_evidence(
        pipeline.db,
        run_id=ctx.run_id,
        agent_name="execution",
        kind="execution_skip",
        scope="symbol",
        symbol=symbol,
        decision_id=ctx.decision_id,
        evidence_json=_json.dumps(
            {"symbol": symbol, "reason": reason, "detail": detail},
        ),
    )


def _record_pipeline_event(
    pipeline, ctx, symbol: str | None, stage: str, outcome: str, reason: str = "", **details
) -> None:
    """Append one typed lifecycle fact to the existing evidence stream.
    Conversion step 6: shim over `EventJournal.record_pipeline_event`. Routes
    through this module's `_persist_evidence` on purpose, so a test that
    patches that name still sees every event, exactly as before.
    """
    if stage == "order":
        # one Sentinel attempt row per order event
        record_order_attempt_from_event(
            db=pipeline.db,
            symbol=symbol,
            outcome=outcome,
            reason=reason,
            run_id=ctx.run_id,
            details=details,
        )
    _persist_evidence(
        pipeline.db,
        **pipeline_event_fields(
            run_id=ctx.run_id,
            decision_id=ctx.decision_id,
            symbol=symbol,
            stage=stage,
            outcome=outcome,
            reason=reason,
            details=details,
        ),
    )


#: The seat this accounting spends its one paid retry under. Distinct from
#: `portfolio_manager` itself so a bookkeeping re-ask can never consume the
#: retry another PM heal may need in the same session, and vice versa.
_PM_ACCOUNTING_SEAT = "portfolio_manager_candidate_accounting"


def _record_accounted_candidate(pipeline, ctx, accounted) -> None:
    """One durable per-symbol row for one accounted candidate.

    `refusal` carries the comparable CODE and `note` the reader-facing
    prose. That split is deliberate: `refusal_signature.signature_key`
    reads `refusal` and does not read `note`, so two sessions are compared
    on the named ground rather than on the seat's choice of words.
    """
    payload = accounted.event_kwargs()
    _record_pipeline_event(
        pipeline,
        ctx,
        accounted.symbol,
        payload["stage"],
        payload["outcome"],
        payload["reason"],
        refusal=payload["refusal"],
        note=payload["note"],
    )


def _account_for_pm_candidates(
    pipeline,
    ctx,
    *,
    run_id,
    analyses,
    positions,
    decision,
    pm_decide_kwargs,
) -> None:
    """Make the portfolio manager account for every candidate it was shown.

    Board item 133 (2026-09-18). Replaces the loop that recorded every
    analysed candidate missing from `targets` as
    `omitted / candidate_not_selected_for_target` — one unvarying string
    that was not a reason, because the seat was never asked for one. See
    `src/pm_accounting.py` for why that defeated the jam detector by
    construction.

    The desk's standing heal order, no step skipped and no new retry
    invented: mechanical heal from what the seat did say, ONE re-ask under
    the existing per-seat paid-retry cap, then a durable per-symbol reason.

    Decides nothing. Every candidate's trading fate was already settled by
    `decide()` before this function runs; all that changes here is whether
    the desk can say WHY. It never raises — a bookkeeping failure must not
    take a live session with it.
    """
    from src.pm_accounting import (
        REASK_DIRECTIVE,
        account_for_candidates,
        unaccounted_row,
    )
    from src.seat_heal import (
        HealResult,
        HEAL_CAP_BLOCKED,
        HEAL_FAILED,
        HEAL_MECHANICAL,
        HEAL_PAID_RETRY,
        can_paid_retry,
        record_paid_retry,
    )

    # The ranking's own refusals (R2-R7) for this session, read BEFORE any
    # re-ask (a second decide() resets them).
    blocked = dict(getattr(pipeline.portfolio_manager, "last_blocked", None) or {})
    result = account_for_candidates(
        analyses=analyses,
        decision=decision,
        positions=positions,
        blocked=blocked,
    )
    for accounted in result.accounted:
        _record_accounted_candidate(pipeline, ctx, accounted)
    if not result.unaccounted:
        if result.accounted:
            logger.info(
                "PM candidate accounting: every one of the %d non-targeted candidate(s) carries a named ground",
                len(result.accounted),
            )
        return

    pending = sorted(result.unaccounted)
    logger.warning(
        "PM candidate accounting: the seat dropped %s without naming a ground. Re-asking once (bookkeeping only).",
        ", ".join(pending),
    )

    def _finish(symbols, *, asked: bool) -> None:
        for symbol in sorted(symbols):
            _record_accounted_candidate(
                pipeline,
                ctx,
                unaccounted_row(symbol, asked=asked),
            )

    retries = dict(getattr(ctx, "heal_paid_retries", None) or {})
    if not can_paid_retry(retries, _PM_ACCOUNTING_SEAT):
        logger.warning(
            "PM candidate accounting: the one re-ask for this seat is "
            "already spent this session — %s stay(s) unaccounted and "
            "recorded",
            ", ".join(pending),
        )
        _finish(pending, asked=False)
        return

    from src.cost_circuit import PaidAnalysisSuspended

    try:
        pipeline._require_paid_analysis("portfolio_manager")
    except PaidAnalysisSuspended as exc:
        _record_heal_safely(
            pipeline,
            ctx,
            HealResult(
                seat=_PM_ACCOUNTING_SEAT,
                outcome=HEAL_CAP_BLOCKED,
                reason=(f"spend cap blocked the candidate-accounting re-ask: {exc}"),
                details={"symbols": pending},
            ),
        )
        _finish(pending, asked=False)
        return

    ctx.heal_paid_retries = record_paid_retry(retries, _PM_ACCOUNTING_SEAT)
    challenge = REASK_DIRECTIVE + ", ".join(pending)
    try:
        reasked, reask_result = pipeline.portfolio_manager.decide(
            **{**pm_decide_kwargs, "accounting_challenge": challenge},
        )
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "accounting_reask", exc)
        _record_heal_safely(
            pipeline,
            ctx,
            HealResult(
                seat=_PM_ACCOUNTING_SEAT,
                outcome=HEAL_FAILED,
                reason=f"candidate-accounting re-ask raised: {exc}",
                paid_retry=True,
                details={"symbols": pending},
            ),
        )
        _finish(pending, asked=True)
        return
    else:
        record_stage(pipeline, "accounting_reask")

    try:
        pipeline.db.insert_agent_log(
            **seat_acceptance_kwargs(
                "no_valid_grounded_decision" if not reasked else None,
                result=reask_result,
            ),
            agent_name="portfolio_manager",
            run_id=run_id,
            input_summary=(f"candidate-accounting re-ask | {', '.join(pending)}"),
            input_message=reask_result.user_message,
            output_summary=(reasked.portfolio_view if reasked else "parse_error"),
            full_response=reask_result.raw_text,
            model=reask_result.model,
            tokens_used=reask_result.tokens_used,
            input_tokens=reask_result.input_tokens,
            output_tokens=reask_result.output_tokens,
            cost_usd=reask_result.cost_usd,
            **agent_log_kwargs(reask_result),
        )
    except Exception as e:  # noqa: BLE001
        record_stage(pipeline, "accounting_reask_log", e)
    else:
        record_stage(pipeline, "accounting_reask_log")

    if reasked is None:
        _record_heal_safely(
            pipeline,
            ctx,
            HealResult(
                seat=_PM_ACCOUNTING_SEAT,
                outcome=HEAL_FAILED,
                reason="candidate-accounting re-ask returned no parseable decision",
                paid_retry=True,
                details={"symbols": pending},
            ),
        )
        _finish(pending, asked=True)
        return

    # BOOKKEEPING ONLY. The re-ask's targets, sizes and reasoning_chain are
    # DISCARDED: the trade decision was made on the first call and a paid
    # retry must never become a way to re-decide the book. Only rejections
    # naming a CHALLENGED symbol are taken, and only where the first answer
    # had none — so the re-ask cannot overwrite a ground the seat already
    # stated, nor invent an accounting for a name it was not asked about.
    challenged = set(pending)
    already = {
        str(getattr(r, "symbol", "") or "").strip().upper() for r in (getattr(decision, "rejections", None) or [])
    }
    gained = [
        r
        for r in (getattr(reasked, "rejections", None) or [])
        if str(getattr(r, "symbol", "") or "").strip().upper() in challenged
        and str(getattr(r, "symbol", "") or "").strip().upper() not in already
    ]
    if gained:
        try:
            decision.rejections = (
                list(
                    getattr(decision, "rejections", None) or [],
                )
                + gained
            )
        except Exception as e:  # noqa: BLE001
            record_stage(pipeline, "accounting_rejections", e)
        else:
            record_stage(pipeline, "accounting_rejections")

    second = account_for_candidates(
        analyses=[a for a in analyses if str(getattr(a, "symbol", "") or "").strip().upper() in challenged],
        decision=decision,
        positions=positions,
        blocked=blocked,
    )
    for accounted in second.accounted:
        _record_accounted_candidate(pipeline, ctx, accounted)
    still = sorted(second.unaccounted)
    logger.info(
        "PM candidate accounting: the re-ask accounted for %d of %d challenged candidate(s); %d still unaccounted",
        len(second.accounted),
        len(pending),
        len(still),
    )

    if not still:
        _record_heal_safely(
            pipeline,
            ctx,
            HealResult(
                seat=_PM_ACCOUNTING_SEAT,
                outcome=HEAL_PAID_RETRY,
                reason=("the candidate-accounting re-ask named a ground for every candidate it was asked about"),
                paid_retry=True,
                usable=True,
                details={"symbols": pending},
            ),
            alert=False,
        )
        return

    _finish(still, asked=True)
    _record_heal_safely(
        pipeline,
        ctx,
        HealResult(
            seat=_PM_ACCOUNTING_SEAT,
            outcome=HEAL_FAILED,
            reason=(
                "the portfolio manager would not name a ground for: "
                + ", ".join(still)
                + ". Nothing was bought or sold differently "
                "because of this — what is lost is the desk's ability to say "
                "why these candidates were dropped. Recorded per symbol."
            ),
            paid_retry=True,
            details={"symbols": still},
            owner_consequence=(
                "NOTHING was bought, sold or held differently because of this — "
                "the decision itself was already made and stands. What is "
                "missing is the desk's account of why it passed on these names."
            ),
        ),
    )
    # Mechanical-heal bookkeeping, so a reader can tell a session where the
    # seat answered from one where the code recovered the answer.
    if second.accounted:
        _record_heal_safely(
            pipeline,
            ctx,
            HealResult(
                seat=_PM_ACCOUNTING_SEAT,
                outcome=HEAL_MECHANICAL,
                reason=(f"{len(second.accounted)} candidate(s) accounted for after the re-ask"),
                mechanical=True,
                paid_retry=True,
                usable=True,
            ),
            alert=False,
        )


def _record_heal_safely(pipeline, ctx, result, alert: bool = True) -> None:
    """`TradingPipeline._record_heal`, but never fatal to the session.

    The accounting path is bookkeeping; a failure to WRITE the bookkeeping
    must not be able to end a live trading session.
    """
    try:
        pipeline._record_heal(ctx, result, alert=alert)
    except Exception as e:  # noqa: BLE001
        record_stage(pipeline, "heal_record", e)
    else:
        record_stage(pipeline, "heal_record")
