"""The deploy-drift snapshot read behind /health (moved out of routes_live)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.api.loud_reads import record_dashboard_fault
from src.trading_calendar import utc_now

#: A drift snapshot older than this is no longer evidence of anything: the
#: timer runs far more often, so silence means the checker itself stopped.
#: Reported as `stale`, which is degraded — the same treatment the alert
#: channel gets, and for the same reason.
_DRIFT_SNAPSHOT_MAX_AGE_H = 26


def deploy_drift_state() -> dict:
    """Read the deploy-drift snapshot written by scripts/check_deploy_drift.py.

    Never raises and never runs git. A missing file means the check has not
    run on this box yet, which is `unknown`, not healthy.
    """
    try:
        from src.drift_state import load_drift_state

        record = load_drift_state().get("deploy_drift")
        if not isinstance(record, dict) or not record.get("status"):
            return {"status": "unknown", "reason": "no drift check recorded"}
        record = dict(record)
        checked_at = record.get("checked_at")
        if checked_at:
            try:
                seen = datetime.fromisoformat(str(checked_at))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                age_h = (utc_now() - seen).total_seconds() / 3600.0
                record["age_hours"] = round(age_h, 2)
                if age_h > _DRIFT_SNAPSHOT_MAX_AGE_H and record["status"] != "behind":
                    record["status"] = "stale"
                    record["reason"] = "drift check has not run recently"
            except (TypeError, ValueError):
                record["age_hours"] = None
        else:
            record["status"] = "stale"
            record["reason"] = "snapshot carries no timestamp"
        return record
    except Exception:
        record_dashboard_fault("drift_state.read")
        return {"status": "unknown", "reason": "drift state read failed"}
