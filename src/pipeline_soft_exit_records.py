"""Soft-exit records (split out of src/pipeline_stages.py).

Moved VERBATIM out of `src/pipeline_stages.py`, following the pattern of
`src/pipeline_seat_evidence.py`: the open/increase missing-falsifier test and
the admitted-to-book filter, the mechanical soft-exit restore record and the
per-name soft-exit heal drain (board item 78), the missing-after-retry row,
the refusal count, and the last-resort isolate of BUY/SHORT names still
missing a falsifier after heal and retry. Every function takes the pipeline
and run context as plain arguments and reads each collaborator off that
handed-in host per call, so each can be exercised with stand-ins and no
`TradingPipeline` is ever built here
(`tests/test_boundary_pipeline_soft_exit_records.py` is the clause-5
witness). Every moved name is re-exported from `src.pipeline_stages` through
its one lazy re-export table, so each original import path and each test
patch target is unchanged; the write-through mirror on that module keeps a
patched name the same object on both sides.

The shared names below are imported FROM `src.pipeline_stages` as the stage
modules do (`_record_pipeline_event` resolves through that same lazy table to
`src.pipeline_candidate_records`); `src.pipeline_stages` imports this module
only lazily, by name, so the graph stays acyclic. Not in
`src.number_sources.SCOPED_PATHS`: the block carries no ledgered number site.
This module must not import `src.pipeline` and never reaches the broker seam.
"""

from __future__ import annotations

from src.recording_accessors import pinned_evidence
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    PortfolioManagerAgent,
    SOFT_EXIT_HEAL_EVENT_REASON,
    SOFT_EXIT_MISSING_AFTER_RETRY,
    _book_risk_inputs,
    _record_pipeline_event,
    _soft_exit_heal_detail,
    logger,
    missing_stated_falsifier,
    open_target_missing_falsifier,
    record_refusal_count,
    record_stage,
)


def _target_increase_missing_falsifier(
    target,
    *,
    positions=None,
    total_value: float = 0.0,
    existing_risk_pct=None,
) -> bool:
    """True iff this target is an open/increase still missing a real falsifier.

    Classification reuses `PortfolioManagerAgent._target_intent` (current
    size/risk vs the proposed target). Reductions and closes are never
    this check — a blank `thesis_invalid_if` on a checkable size-down
    is not a missing open falsifier. The constructor still must name a
    mechanical live-book warrant rather than PM thesis; that is not
    this function. Fail-safe matches `_target_intent`: unknown current
    risk is treated as an increase, the stricter gate. Never invents a
    string.
    """
    held = {str(getattr(p, "symbol", "")).upper(): p for p in list(positions or []) if getattr(p, "symbol", None)}
    intent = PortfolioManagerAgent._target_intent(
        target,
        held,
        total_value,
        existing_risk_pct=existing_risk_pct,
    )
    return open_target_missing_falsifier(target, intent=intent)


def _targets_admitted_to_book(
    targets,
    *,
    positions=None,
    total_value: float = 0.0,
    existing_risk_pct=None,
) -> tuple[list, list[str]]:
    """Open/increase names still missing a real falsifier never reach the constructor.

    Permanent never-blank path: heal + one paid retry already ran on the
    PM seat so it actually produces the field. Remaining blanks on
    opens/increases are refused here so they do not consume risk budget
    or become tickets. Reductions and closes with a blank
    `thesis_invalid_if` are admitted only when `_target_intent`
    classifies a checkable size-down vs the live book (risk/weight).
    Those are not soft-exits: the constructor stamps a mechanical
    size-down warrant as the named trigger. PM thesis free text cannot
    create the sell. Classification reuses `_target_intent`.
    That label can disagree with the constructor's order side when a
    lower risk request meets a tighter stop (more shares). The RiskStage
    isolate still drops any constructed BUY/SHORT whose falsifier is
    blank, so a mis-labelled add cannot be ticketed. That refuse is
    last-resort after the producing step was asked, not skip-and-continue
    as the product (owner 2026-09-17). Targets stay on the proposal so
    Risk is told why the narrative names a symbol that is not in the
    list. Never invents a falsifier or catalyst string.
    """
    admitted: list = []
    refused: list[str] = []
    for target in list(targets or []):
        if _target_increase_missing_falsifier(
            target,
            positions=positions,
            total_value=total_value,
            existing_risk_pct=existing_risk_pct,
        ):
            refused.append(str(target.symbol).upper())
        else:
            admitted.append(target)
    return admitted, refused


def _record_mechanical_soft_exit_restores(pipeline, ctx) -> None:
    """Write down what the MECHANICAL soft-exit heal did this run.

    Board item 78. `src.seat_heal.restore_stated_soft_exits` puts back a
    `thesis_invalid_if` that a later null-wipe blanked, using the sentence
    the model itself already wrote, and it never invents one. It runs
    inside a Pydantic validator, so it has no run id and no database
    handle and has never recorded a single thing. Two of item 78's three
    removal criteria are claims about this heal, so they could not be
    judged at all.

    RECORDING ONLY. Nothing reads these rows back into a trading
    decision and they may never be swept for a threshold. Never raises.
    """
    try:
        from src.seat_heal import drain_restore_observations

        observations, dropped = drain_restore_observations(getattr(ctx, "run_id", None))
        if not observations:
            return
        db = getattr(pipeline, "db", None)
        writer = getattr(db, "record_soft_exit_heal_restores", None)
        if not callable(writer):
            return
        writer(
            observations=observations,
            run_id=pinned_evidence(ctx, "run_id"),
            dropped=dropped,
        )
    except Exception as exc:  # noqa: BLE001 — a recording never blocks a trade
        record_stage(pipeline, "soft_exit_restore", exc)
    else:
        record_stage(pipeline, "soft_exit_restore")


def _record_soft_exit_heals(pipeline, ctx) -> None:
    """Drain the PM's per-name soft-exit heal outcomes onto ctx and to disk.

    Board item 78. The heal itself (`PortfolioManagerAgent.
    _fill_missing_open_falsifiers`, the desk's standing heal order:
    mechanical restore, then ONE paid seat retry under the existing
    per-seat cap) has four exits that used to leave nothing but a log
    line — never attempted for want of a replayable message, blocked by
    the spend cap, errored, or answered without a falsifier. A name can
    therefore be refused for a blank falsifier without any durable record
    of whether the seat was ever actually re-asked.

    One `pipeline_event` row per name fixes that, and `ctx.soft_exit_heals`
    carries the same fact forward so the refusal can quote what really
    happened instead of asserting a retry. Recording only; never raises.
    """
    _record_mechanical_soft_exit_restores(pipeline, ctx)
    try:
        agent = getattr(pipeline, "portfolio_manager", None)
        drain = getattr(agent, "drain_soft_exit_heals", None)
        heals = dict(drain() if callable(drain) else {})
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "soft_exit_drain", exc)
        return
    else:
        record_stage(pipeline, "soft_exit_drain")
    if not heals:
        return
    try:
        ctx.soft_exit_heals = {**(getattr(ctx, "soft_exit_heals", None) or {}), **heals}
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "soft_exit_attach", exc)
    else:
        record_stage(pipeline, "soft_exit_attach")
    for symbol, heal in heals.items():
        _record_pipeline_event(
            pipeline,
            ctx,
            symbol,
            "soft_exit_heal",
            str(heal.get("outcome") or "unknown"),
            SOFT_EXIT_HEAL_EVENT_REASON,
            detail=str(heal.get("detail") or ""),
        )


def _record_soft_exit_missing_after_retry(
    pipeline,
    ctx,
    symbol: str,
    *,
    action: str | None = None,
) -> None:
    _record_pipeline_event(
        pipeline,
        ctx,
        symbol,
        "deterministic_gate",
        "blocked",
        SOFT_EXIT_MISSING_AFTER_RETRY,
        detail=(
            "thesis_invalid_if still empty or unknown; refusing this "
            "name before the book. No falsifier was invented. " + _soft_exit_heal_detail(ctx, symbol)
        ),
        heal_outcome=str(
            ((getattr(ctx, "soft_exit_heals", None) or {}).get(str(symbol).strip().upper()) or {}).get("outcome")
            or "none_recorded"
        ),
        **({"action": action} if action else {}),
    )


def _record_soft_exit_refusal_count(pipeline, ctx, symbols) -> None:
    record_refusal_count(_record_pipeline_event, logger, pipeline, ctx, symbols)


def _isolate_empty_soft_exit_entries(pipeline, ctx, portfolio_decision) -> list[str]:
    """Refuse BUY/SHORT names still missing a real falsifier after heal+retry.

    TEMPORARY last-resort (#432 isolate-name). Owner 2026-09-17: skip/drop
    is not the product — the producing step must fill. The permanent path
    is schema + prompt + mechanical heal + one paid seat retry, then
    refuse before construct_orders. This filter does not invent a
    thesis_invalid_if or catalyst string. It does not delete the target
    — Risk must still be told the name was proposed and refused.

    EXACTLY WHAT WOULD JUSTIFY DELETING IT (board item 78, 2026-09-26).
    It stays until a LIVE session record shows all three. None of the
    three can be shown offline: each is a claim about what the seats
    really emit when real money is at stake.
      1. `specialist_evidence` holds ZERO `pipeline_event` rows with
         reason `soft-exit missing after retry`, over a window of live
         sessions that actually produced BUY/SHORT targets — not a window
         in which the desk simply proposed nothing. Zero refusals across
         zero opens proves nothing. [measured 2026-09-26: 0 such rows in
         4,709 pipeline_event rows spanning 2026-09-02 to 2026-09-26, so
         criterion 1 alone is already met and is NOT sufficient.]
      2. Over that same window no `soft_exit_heal` row carries outcome
         `paid_retry`: the heal being needed and working is not the same
         as the producing step producing. This is the criterion item 78
         actually names — Tech and the PM emitting a real falsifier
         unaided.
      3. No `soft_exit_heal` row in that window carries `not_attempted`,
         `cap_blocked` or `failed`. Each of those says the desk does not
         yet know whether the seat can fill the field, so a zero refusal
         count in their presence is silence, not evidence.
    Until all three hold, this stays. The heal and the durable heal record
    above are what keep it unreachable in normal operation.

    Catches empty AND `unknown` on BUY/SHORT — omitted empty is missing,
    not "the analyst had nothing to say". Neutrals, reductions and closes
    are not this filter. A constructed BUY/SHORT with a blank falsifier
    is still dropped even when `_target_intent` labelled the target a
    reduction (risk-down / weight-up from a tighter stop).
    """
    if portfolio_decision is None:
        return []
    existing_risk_pct = _book_risk_inputs(
        ctx,
        getattr(ctx, "total_value", 0.0) or 0.0,
    )
    missing_symbols = {
        str(target.symbol).upper()
        for target in list(getattr(portfolio_decision, "targets", None) or [])
        if _target_increase_missing_falsifier(
            target,
            positions=getattr(ctx, "positions", None),
            total_value=getattr(ctx, "total_value", 0.0) or 0.0,
            existing_risk_pct=existing_risk_pct,
        )
    }
    isolated: list[str] = []
    kept = []
    for decision in list(getattr(portfolio_decision, "decisions", None) or []):
        symbol = str(decision.symbol).upper()
        missing_here = symbol in missing_symbols or (
            decision.action in ("BUY", "SHORT")
            and missing_stated_falsifier(getattr(decision, "thesis_invalid_if", None))
        )
        if decision.action in ("BUY", "SHORT") and missing_here:
            isolated.append(symbol)
            _record_soft_exit_missing_after_retry(
                pipeline,
                ctx,
                decision.symbol,
                action=decision.action,
            )
            continue
        kept.append(decision)
    if not isolated:
        return []
    unique = list(dict.fromkeys(isolated))
    logger.warning(
        "Refusing %d BUY/SHORT name(s) %s: %s",
        len(unique),
        SOFT_EXIT_MISSING_AFTER_RETRY,
        unique,
    )
    portfolio_decision.decisions = kept
    existing = list(getattr(portfolio_decision, "constructor_dropped", None) or [])
    for symbol in unique:
        if symbol not in existing:
            existing.append(symbol)
    portfolio_decision.constructor_dropped = existing
    _record_soft_exit_refusal_count(pipeline, ctx, unique)
    return unique
