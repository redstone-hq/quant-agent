"""Read-only daily Research Intelligence projection over canonical records."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, date as Date, datetime

from fastapi import APIRouter, HTTPException

from src.api import db_reads
from src.api.schemas import (
    DailyPnlPoint,
    ReflectionItem,
    ResearchAgentCall,
    ResearchDailyResponse,
    ResearchDecisionDelta,
    ResearchEvidenceItem,
    ResearchFreshness,
    ResearchRun,
    RunSummary,
    TradeItem,
)
from src.trading_calendar import et_today, utc_now

router = APIRouter()


def _evidence_item(row: dict) -> ResearchEvidenceItem:
    payload = None
    state = "invalid"
    try:
        parsed = json.loads(row.get("evidence_json") or "")
        if isinstance(parsed, (dict, list)):
            payload, state = parsed, "valid"
    except (TypeError, ValueError):
        pass
    return ResearchEvidenceItem(
        id=row["id"],
        run_id=row["run_id"],
        decision_id=row.get("decision_id"),
        agent_name=row["agent_name"],
        kind=row["kind"],
        scope=row["scope"],
        symbol=row.get("symbol"),
        timestamp=row.get("timestamp"),
        state=state,
        payload=payload,
    )


def _freshness(date_str: str, timestamps: list[str]) -> ResearchFreshness:
    if not timestamps:
        return ResearchFreshness()
    latest = max(timestamps)
    if Date.fromisoformat(date_str) < et_today():
        return ResearchFreshness(latest_recorded_at=latest, label="historical")
    try:
        recorded = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        age = max(0.0, (utc_now() - recorded).total_seconds() / 60)
    except (TypeError, ValueError):
        return ResearchFreshness(latest_recorded_at=latest)
    label = "current" if age <= 90 else "aging" if age <= 360 else "stale"
    return ResearchFreshness(latest_recorded_at=latest, age_minutes=round(age, 1), label=label)


@router.get("/research/daily/{date}", response_model=ResearchDailyResponse)
def get_research_daily(date: str) -> ResearchDailyResponse:
    raw = db_reads.get_research_day(date)
    if raw.get("invalid_date"):
        raise HTTPException(422, "date must be ISO YYYY-MM-DD")
    as_of = utc_now().isoformat()
    if raw.get("read_error"):
        return ResearchDailyResponse(
            date=date,
            as_of=as_of,
            state="error",
            read_error=raw["read_error"],
            missing_sources=["canonical_database"],
        )

    logs = raw["agent_logs"]
    evidence_items = [_evidence_item(r) for r in raw["specialist_evidence"]]
    trades = [TradeItem(**r) for r in raw["trades"]]
    evidence_counts = Counter((e.run_id, e.agent_name) for e in evidence_items)
    by_run_logs: dict[str, list[dict]] = defaultdict(list)
    by_run_evidence: dict[str, list[ResearchEvidenceItem]] = defaultdict(list)
    by_run_trades: dict[str, list[TradeItem]] = defaultdict(list)
    for row in logs:
        by_run_logs[row["run_id"]].append(row)
    for item in evidence_items:
        by_run_evidence[item.run_id].append(item)
    for item in trades:
        if item.run_id:
            by_run_trades[item.run_id].append(item)

    run_ids = set(by_run_logs) | set(by_run_evidence) | set(by_run_trades)
    runs: list[ResearchRun] = []
    error_evidence = [e for e in evidence_items if e.kind in {"provider_error", "analysis_error"}]
    missing_sources = sorted(
        {f"{e.agent_name}/{'provider' if e.kind == 'provider_error' else 'analysis'}" for e in error_evidence}
    )
    partial = bool(error_evidence) or any(e.state == "invalid" for e in evidence_items)
    for run_id in run_ids:
        run_logs = by_run_logs[run_id]
        run_evidence = by_run_evidence[run_id]
        run_trades = by_run_trades[run_id]
        calls = [
            ResearchAgentCall(
                id=r["id"],
                agent_name=r["agent_name"],
                run_id=run_id,
                decision_id=r.get("decision_id"),
                timestamp=r.get("timestamp"),
                status=r.get("status"),
                output_summary=r.get("output_summary"),
                requested_provider=r.get("requested_provider"),
                requested_model=r.get("requested_model"),
                actual_provider=r.get("actual_provider"),
                model=r.get("model"),
                prompt_version=r.get("prompt_version"),
                latency_s=r.get("latency_s"),
                cost_usd=r.get("cost_usd"),
                structured_evidence_count=evidence_counts[(run_id, r["agent_name"])],
            )
            for r in run_logs
        ]
        partial = partial or any(c.status not in (None, "success", "fallback", "hard_risk_block") for c in calls)
        timestamps = [r.get("timestamp") for r in run_logs if r.get("timestamp")]
        timestamps += [e.timestamp for e in run_evidence if e.timestamp]
        timestamps += [t.timestamp for t in run_trades if t.timestamp]
        decision_id = next((r.get("decision_id") for r in run_logs if r.get("decision_id")), None)
        if decision_id is None:
            decision_id = next((e.decision_id for e in run_evidence if e.decision_id), None)
        proposed = [
            e for e in run_evidence if e.agent_name == "portfolio_manager" and e.kind in {"target", "proposed_order"}
        ]
        risk_changes = [
            e for e in run_evidence if e.agent_name == "risk_manager" and e.kind in {"verdict", "modification"}
        ]
        events = [e for e in run_evidence if e.kind == "pipeline_event"]
        hard_block = any(r["agent_name"] == "risk_gate" for r in run_logs)
        executed = any(db_reads.is_executed_trade(t.model_dump()) for t in run_trades)
        state = (
            "executed"
            if executed
            else "hard_risk_block"
            if hard_block
            else "proposed_not_executed"
            if proposed
            else "no_proposal"
        )
        costs = [r.get("cost_usd") for r in run_logs]
        total_cost = None if not costs or any(c is None for c in costs) else sum(costs)
        summary = RunSummary(
            run_id=run_id,
            session_prefix=run_id.rsplit("-", 1)[0] if "-" in run_id else run_id,
            first_timestamp=min(timestamps) if timestamps else None,
            last_timestamp=max(timestamps) if timestamps else None,
            agent_count=len(run_logs),
            decision_id=decision_id,
            total_cost_usd=total_cost,
        )
        runs.append(
            ResearchRun(
                summary=summary,
                agent_calls=calls,
                evidence=run_evidence,
                decision_delta=ResearchDecisionDelta(
                    run_id=run_id,
                    decision_id=decision_id,
                    state=state,
                    proposed=proposed,
                    risk_changes=risk_changes,
                    deterministic_events=events,
                    trades=run_trades,
                ),
            )
        )
    runs.sort(key=lambda r: r.summary.first_timestamp or "")

    has_data = bool(runs or raw["daily_pnl"] or raw["insights"])
    all_timestamps = [r.summary.last_timestamp for r in runs if r.summary.last_timestamp]
    return ResearchDailyResponse(
        date=date,
        as_of=as_of,
        state="partial" if has_data and partial else "complete" if has_data else "empty",
        freshness=_freshness(date, all_timestamps),
        missing_sources=missing_sources,
        daily_pnl=DailyPnlPoint(**raw["daily_pnl"]) if raw["daily_pnl"] else None,
        reflection=ReflectionItem(**raw["insights"]) if raw["insights"] else None,
        runs=runs,
    )
