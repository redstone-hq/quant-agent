import logging

from src.pipeline_config_build import (  # noqa: F401
    _smart_money_refresh_sources_word,
)
from src.sessions.earnings_preprocess_session import EarningsPreprocessSession
from src.sessions.position_review_session import PositionReviewSession

logger = logging.getLogger(__name__)


def _persist_review_metrics(pipeline, position_facts: dict, *, run_id: str) -> None:
    """Snapshot this review's metrics so the next one can compare.

    Never raises: losing a snapshot degrades the NEXT review to "no prior",
    which the guard handles, and must not take down the current session.
    """
    import json as _json

    for symbol, facts in (position_facts or {}).items():
        payload = {key: facts.get(key) for key in pipeline._REVIEW_METRIC_KEYS if facts.get(key) is not None}
        if not payload:
            continue
        try:
            pipeline.db.save_position_review_metrics(
                run_id=run_id,
                symbol=symbol,
                metrics_json=_json.dumps(payload, sort_keys=True),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "review memory: failed to snapshot %s (%s) — next review will have no prior for it",
                symbol,
                e,
            )


def run_position_review(pipeline, session_type: str = "midday") -> dict:
    """Midday/close, plus the durable record of its own output.

    Same gap as `run_morning` (2026-09-18 sweep): `leverage`,
    `stop_coverage_gaps` and this session's own `daily_pnl`/`total_pnl`
    snapshot are computed from live broker state and handed to the
    notifier with no other durable home. The body is unchanged; this
    wrapper persists every return path, keyed by (date, session_type)
    so midday and close each keep their own row. Fail-soft.
    """
    if session_type not in ("midday", "close"):
        raise ValueError(f"run_position_review: unknown session_type {session_type!r}")
    pipeline._last_account_snapshot = None
    result = pipeline._run_position_review_body(session_type)
    pipeline._attach_pnl(result)
    pipeline._persist_session_report(session_type, result)
    return result


def _run_position_review_body(pipeline, session_type: str) -> dict:
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/position_review_session.py)."""
    return PositionReviewSession(
        activate_cost_session=pipeline._collab("_activate_cost_session"),
        adjudicate_target_revision_flags=pipeline._collab("_adjudicate_target_revision_flags"),
        apply_deterministic_trails=pipeline._collab("_apply_deterministic_trails"),
        build_active_state_changes=pipeline._collab("_build_active_state_changes"),
        build_calibration_note=pipeline._collab("_build_calibration_note"),
        build_macro_trajectory=pipeline._collab("_build_macro_trajectory"),
        build_own_recent_decisions=pipeline._collab("_build_own_recent_decisions"),
        build_position_facts=pipeline._collab("_build_position_facts"),
        build_review_metric_deltas=pipeline._collab("_build_review_metric_deltas"),
        build_trade_grade_summary=pipeline._collab("_build_trade_grade_summary"),
        build_weekly_narrative=pipeline._collab("_build_weekly_narrative"),
        compute_deployable_cash=pipeline._collab("_compute_deployable_cash"),
        compute_recent_performance=pipeline._collab("_compute_recent_performance"),
        cost_circuit_status=pipeline._collab("_cost_circuit_status"),
        drain_pending_protection_restores=pipeline._collab("_drain_pending_protection_restores"),
        drain_pending_repegs=pipeline._collab("_drain_pending_repegs"),
        enforce_gross_ceiling=pipeline._collab("_enforce_gross_ceiling"),
        force_delever=pipeline._collab("_force_delever"),
        handle_ex_dividends=pipeline._collab("_handle_ex_dividends"),
        is_trading_day=pipeline._collab("_is_trading_day"),
        kill_switch_halt_result=pipeline._collab("_kill_switch_halt_result"),
        load_earnings_analyses=pipeline._collab("_load_earnings_analyses"),
        midday_execute_llm_actions=pipeline._collab("_midday_execute_llm_actions"),
        news_held_symbols=pipeline._collab("_news_held_symbols"),
        paid_suspension_after_late_safety=pipeline._collab("_paid_suspension_after_late_safety"),
        persist_review_metrics=pipeline._collab("_persist_review_metrics"),
        reconcile_fills=pipeline._collab("_reconcile_fills"),
        reconcile_orphan_pending_submits=pipeline._collab("_reconcile_orphan_pending_submits"),
        reconcile_stop_coverage=pipeline._collab("_reconcile_stop_coverage"),
        reconcile_stop_out_fills=pipeline._collab("_reconcile_stop_out_fills"),
        record_account_snapshot=pipeline._collab("_record_account_snapshot"),
        require_paid_analysis=pipeline._collab("_require_paid_analysis"),
        risk_review_exits=pipeline._collab("_risk_review_exits"),
        structural_protection_for_holding=pipeline._collab("_structural_protection_for_holding"),
        build_position_history=pipeline._collab("_build_position_history"),
        run_news_update=pipeline._collab("_run_news_update"),
        substantiate_exit_triggers=pipeline._collab("_substantiate_exit_triggers"),
        surface_reconcile_outcomes=pipeline._collab("_surface_reconcile_outcomes"),
        sweeper=pipeline._collab("_sweeper"),
        symbols_already_trimmed_today=pipeline._collab("_symbols_already_trimmed_today"),
        sync_positions_from_broker=pipeline._collab("_sync_positions_from_broker"),
        total_pnl_since_reset=pipeline._collab("_total_pnl_since_reset"),
        trade_executed_or_pending=pipeline._collab("_trade_executed_or_pending"),
        broker=pipeline._collab("broker"),
        config=pipeline._collab("config"),
        db=pipeline._collab("db"),
        macro=pipeline._collab("macro"),
        macro_store=pipeline._collab("macro_store"),
        position_reviewer=pipeline._collab("position_reviewer"),
        pipeline=pipeline,
    ).run(session_type)


def run_earnings_preprocess(pipeline) -> dict:
    """Pre-market earnings analysis, plus the one true sentence its
    P&L block can say.

    This mode runs before the open and makes no broker account read at
    all, so its message genuinely has no figure. It says so explicitly
    rather than letting the renderer guess from absent keys — the guess
    is what produced the false "built without an account read" line on
    trading sessions that HAD read the account (2026-09-23).
    """
    result = pipeline._run_earnings_preprocess_body()
    if isinstance(result, dict):
        result.setdefault("pnl_unavailable_reason", "no_account_read")
    return result


def _run_earnings_preprocess_body(pipeline) -> dict:
    """Thin shim: builds the standalone session and runs it (body moved to src/sessions/earnings_preprocess_session.py)."""
    return EarningsPreprocessSession(
        activate_cost_session=pipeline._collab("_activate_cost_session"),
        alert_form4_backlog_before_open=pipeline._collab("_alert_form4_backlog_before_open"),
        drain_pending_protection_restores=pipeline._collab("_drain_pending_protection_restores"),
        drain_pending_repegs=pipeline._collab("_drain_pending_repegs"),
        earnings_preprocess_symbols=pipeline._collab("_earnings_preprocess_symbols"),
        is_trading_day=pipeline._collab("_is_trading_day"),
        paid_suspended_payload=pipeline._collab("_paid_suspended_payload"),
        reconcile_orphan_pending_submits=pipeline._collab("_reconcile_orphan_pending_submits"),
        record_congressional_refresh=pipeline._collab("_record_congressional_refresh"),
        record_form4_backlog=pipeline._collab("_record_form4_backlog"),
        require_paid_analysis=pipeline._collab("_require_paid_analysis"),
        surface_reconcile_outcomes=pipeline._collab("_surface_reconcile_outcomes"),
        watched_research_symbols=pipeline._collab("_watched_research_symbols"),
        smart_money_refresh_sources_word=_smart_money_refresh_sources_word,
        config=pipeline._collab("config"),
        db=pipeline._collab("db"),
        earnings_analyst=pipeline._collab("earnings_analyst"),
        earnings_provider=pipeline._collab("earnings_provider"),
        smart_money_provider=pipeline._collab("smart_money_provider"),
    ).run()
