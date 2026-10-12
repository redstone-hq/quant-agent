"""Board item 219 - why each below-bar holding was kept, recorded at the source.

The pruning pass's `precheck` row stores a reason only for the one name it
cut. The owner's rule is "sell anything below the bar, always", so a name
that was below the bar and SURVIVED is the line that looks like the rule did
not fire, and it must say why. Two causes are decided here, not inferred
later: the pass never reached it, or it was reached and refused (that refusal
is already a durable `rotation`/`skipped` row under the name's own reason,
which the dashboard joins). This module adds one run-scoped
`rotation`/`dispositions` row carrying, per below-bar name, the conviction
reasons it fails the bar on and, where the pass never reached it, the exact
"not reached: <why>". No threshold, limit or lookback is introduced.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _walk_order(precheck) -> list[tuple[str, tuple[str, ...]]]:
    opp = getattr(precheck, "opportunity", None)
    cands = tuple(getattr(opp, "ineligible_candidates", ()) or ())
    if cands:
        return [(str(s).upper(), tuple(r)) for s, r in cands]
    below = tuple(getattr(precheck, "held_below_entry_bar", ()) or ())
    return [(str(s).upper(), ()) for s in below]


def disposition_payload(
    precheck,
    closed: set[str],
    execution_enabled: bool,
) -> dict[str, str]:
    """Pure: the per-name reasons for one pass. Empty when nothing is below the bar."""
    order = _walk_order(precheck)
    if not order:
        return {}
    first_cut = next((s for s, _ in order if s in closed), "")
    cut_seen = False
    fail_on, not_reached = [], []
    for sym, reasons in order:
        fail_on.append(f"{sym}={'; '.join(str(r) for r in reasons)}")
        if sym in closed:
            cut_seen = True
        elif not execution_enabled:
            not_reached.append(
                f"{sym}=not reached: pruning execution is switched off, so "
                "no below-bar name was put up to be cut this session"
            )
        elif cut_seen:
            not_reached.append(
                f"{sym}=not reached: the pass closes one below-bar name per run and {first_cut} was closed first"
            )
    return {
        "below_bar_reasons": "|".join(fail_on),
        "not_reached": "|".join(not_reached),
    }


def holdings_outcome_payload(precheck, closed: set[str], ctx) -> dict[str, str]:
    """Pure: one outcome per holding the entry-bar pass examined this run.

    `sale_proposed` (closed this run), `below_bar_kept` (below the bar and
    not closed; the `dispositions` row and the name's own `skipped` row say
    why), `not_judged_seat_failed:<seats>` (below the bar only on the
    evidence sum while a seat feeding it failed), `meets_bar` otherwise.
    Run-scoped (symbols in the payload) for the same reason as the
    `precheck` row: a symbol-scoped event reads as "considered as a new idea".
    """
    from src.pipeline_rotation_exec import (
        below_bar_only_on_incomplete_evidence,
        failed_evidence_seats,
    )

    examined = tuple(getattr(precheck, "held_examined", ()) or ())
    if not examined:
        return {}
    reasons_by = dict(_walk_order(precheck))
    below = {str(s).upper() for s in (getattr(precheck, "held_below_entry_bar", ()) or ())}
    parts = []
    for sym in examined:
        sym = str(sym).upper()
        failed = failed_evidence_seats(ctx, sym)
        if sym in closed:
            outcome = "sale_proposed"
        elif sym in below and below_bar_only_on_incomplete_evidence(reasons_by.get(sym, ()), failed):
            outcome = "not_judged_seat_failed:" + "+".join(failed)
        elif sym in below:
            outcome = "below_bar_kept"
        else:
            outcome = "meets_bar"
        parts.append(f"{sym}={outcome}")
    return {"held_outcomes": "|".join(parts)}


def apply_rotation_recording_dispositions(
    pipeline,
    ctx,
    portfolio_decision,
    positions,
    position_history,
    *,
    pm_status: str = "answered",
) -> None:
    """Run the rotation acting path, then record why each below-bar name stayed.

    Also records, every run, one outcome per examined holding
    (`holdings_outcome_payload`), with `pm_status` saying whether the PM
    answered, was deliberately skipped, or failed.

    Never raises on the bookkeeping side: a failed record must not take a
    live session with it.
    """
    from src.pipeline_rotation_exec import (
        _apply_rotation_execution,
        _rotation_execution_enabled,
    )
    from src.pipeline_stages import _record_pipeline_event

    _apply_rotation_execution(
        pipeline,
        ctx,
        portfolio_decision,
        positions,
        position_history,
    )
    try:
        precheck = getattr(
            getattr(pipeline, "portfolio_manager", None),
            "last_rotation_precheck",
            None,
        )
        closed = {
            str(getattr(t, "symbol", "")).upper()
            for t in (getattr(portfolio_decision, "targets", None) or [])
            if getattr(t, "is_close", False)
        }
        payload = disposition_payload(
            precheck,
            closed,
            _rotation_execution_enabled(pipeline),
        )
        if payload:
            _record_pipeline_event(
                pipeline,
                ctx,
                None,
                "rotation",
                "dispositions",
                "below_bar_names",
                **payload,
            )
        outcomes = holdings_outcome_payload(precheck, closed, ctx)
        if outcomes:
            _record_pipeline_event(
                pipeline,
                ctx,
                None,
                "rotation",
                "holdings_checked",
                pm_status,
                **outcomes,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rotation dispositions record failed: %s", exc)


#: The keyword arguments `PortfolioManagerAgent.decide` turns from None into
#: an empty container before it assembles the prompt. The no-PM step below
#: hands the SAME kwargs to the same prompt assembly, so it applies the same
#: normalisation rather than letting a None reach a renderer `decide` never
#: lets one reach.
_NONE_AS_EMPTY = {
    "earnings_analyses": list,
    "smart_money_findings": list,
    "recent_performance": dict,
    "position_history": dict,
    "symbol_sectors": dict,
    "allowed_buy_symbols": set,
    "transient_admitted_symbols": set,
}

#: Every mandatory reasoning field of the empty decision the no-PM step
#: carries. Says what it is; never reads as a model's reasoning.
_NO_PM_REASONING = "not decided: no portfolio-manager answer this run; below-entry-bar sale only"


def below_bar_sale_without_pm(pipeline, ctx, pm_decide_kwargs: dict, *, pm_status: str = "failed"):
    """The below-entry-bar sale as its own step, with no paid PM call.

    Owner rulings 2026-10-01 (a holding that fails the desk's own fresh-entry
    bar is SOLD, unconditionally, every run) and 2026-10-12 (that sale must
    not depend on the portfolio manager answering). Before this step the
    sale was only ever proposed inside a run whose PM call returned a
    decision, so a run with no PM answer could not sell.

    Computes the below-bar set itself through the PM agent's own
    deterministic prompt assembly (`build_user_message`: candidate ranking,
    the conviction bar and `rotation_precheck`) -- nothing is sent to a model
    -- and returns an EMPTY, PM-free decision for the existing sell path
    (`apply_rotation_recording_dispositions` -> `_apply_rotation_execution`)
    to append the close to, so every per-holding guard, the constructor, the
    hard risk rules and the AI risk-manager review apply exactly as today.

    Returns None -- today's no-decision behaviour -- when rotation execution
    is switched off, when nothing categorical is below the bar, or when the
    below-bar set cannot be computed (fail closed, recorded). The
    ranked-margin tier is never acted on here: it sells an eligible name to
    fund a PM buy, so with no PM answer it has no reason to sell.
    """
    from src.models import PortfolioDecision, ReasoningChain
    from src.pipeline_rotation_exec import _rotation_execution_enabled
    from src.pipeline_stages import _record_pipeline_event
    from src.rotation import RotationPrecheck

    if not _rotation_execution_enabled(pipeline):
        return None
    pm = getattr(pipeline, "portfolio_manager", None)
    kwargs = dict(pm_decide_kwargs or {})
    for key, empty in _NONE_AS_EMPTY.items():
        if key in kwargs and not kwargs[key]:
            kwargs[key] = empty()
    try:
        # Reset first: a value left by an earlier run, or by a PM call that
        # failed before its pre-check, must never be acted on in this one.
        pm.last_rotation_precheck = None
        pm.build_user_message(**kwargs)
        precheck = pm.last_rotation_precheck
    except Exception as exc:  # noqa: BLE001
        logger.warning("Below-entry-bar pre-check without the PM failed: %s", exc)
        try:
            _record_pipeline_event(
                pipeline,
                ctx,
                None,
                "rotation",
                "skipped",
                "below_bar_precheck_failed_without_pm",
                detail=str(exc)[:400],
            )
        except Exception as rec_exc:  # noqa: BLE001
            logger.warning("Rotation skip record failed: %s", rec_exc)
        return None
    if not isinstance(precheck, RotationPrecheck):
        return None
    opportunity = precheck.opportunity
    if opportunity is None or opportunity.tier != "ineligible_hold":
        # Nothing to sell: still one outcome per holding, every check.
        try:
            outcomes = holdings_outcome_payload(precheck, set(), ctx)
            if outcomes:
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    None,
                    "rotation",
                    "holdings_checked",
                    pm_status,
                    **outcomes,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Holdings outcome record failed: %s", exc)
        return None
    return PortfolioDecision(
        reasoning_chain=ReasoningChain(
            macro_filter=_NO_PM_REASONING,
            news_check=_NO_PM_REASONING,
            earnings_check=_NO_PM_REASONING,
            signal_conflicts=_NO_PM_REASONING,
            sizing_logic=_NO_PM_REASONING,
            portfolio_balance=_NO_PM_REASONING,
            cash_target=_NO_PM_REASONING,
        ),
        portfolio_view=_NO_PM_REASONING,
        targets=[],
    )
