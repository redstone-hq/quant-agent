"""Post-PM decision checkpoint + zero-LLM resume lane for the morning session.

RC2 (2026-07-16 forensics): when LLM latency inflates (relay outage,
provider degradation), the morning session dies at the wrapper's outer
timeout EXACTLY at the PM→RM boundary — research (3 tech chunks + news +
macro) plus PM consume the whole budget, then the kill lands while RM
starts. 61/61 PM BUY-proposal-days between 6/30 and 7/15 were destroyed
this way (zero RM vetoes — 100% mechanical attrition), and every 30-min
retry tick re-burned the full research pipeline just to die identically.

The fix: persist the PM's plan the moment it exists. The next tick's
morning run — after executing its FULL normal preamble (WAL drains, orphan
sweep, coverage audit, stale-order cancel, force_delever, circuit breaker,
fresh account snapshot) — finds the unconsumed checkpoint and re-enters at
the RiskStage with ~2 LLM calls left instead of ~8.

Safety contract (from the adversarial design review):
  - Resume NEVER skips the preamble; divergence starts exactly where
    research would have started.
  - RM ALWAYS re-runs on resume — the two-layer risk philosophy is not
    diluted; there is deliberately no "resume after RM" variant.
  - The checkpoint is marked consumed BEFORE ExecutionStage submits
    (at-most-once: a kill during execution must not re-execute; the BUY
    write-ahead orphan sweep owns partial submits) and also on any
    RiskStage early-exit (an RM-rejected plan must never be retried).
  - Stale protection: same-ET-date only, max age `MAX_AGE_MINUTES`, plus
    the existing ExecutionStage guard (5% entry-price staleness skip) runs
    against FRESH market state.
    MEASURED 2026-10-05, because nobody had checked that the age bound sits
    inside what the entry gates tolerate. The entry-slippage belt is NOT the
    gate it has to sit inside: that belt's reference is captured live at
    submission, so plan age cannot move it (all 6 production slippage_gated
    rows, 2026-09-15 to 2026-09-24, quote a fresh reference). The gate that
    does bound plan age is the 5% entry-price staleness skip. Against that
    5%: ESTIMATE, scaling each day's absolute open-to-close return by the
    square root of 90/390 of a session, over 9,108 symbol-days (33 symbols,
    2025-08-26 to 2026-09-30, from the recorded daily bars under
    ops/rehearsal/recordings) -- median 0.38%, 95th pct 2.09%, 99th 3.81%.
    A 90-minute-old plan exceeds the 5% skip in 0.48% of those symbol-days,
    so the bound sits inside the tolerance for ~99.5% of them and the rest
    is refused, not traded. An estimate, not a measurement: no intraday bars
    are committed, so the within-session path is modelled, not observed.
    The bound is NOT in the number ledger's scope, and the scope guard
    refuses a row for it; it is recorded here instead.
  - Resume RE-SIZES; it does not carry the original size. The checkpoint
    holds the plan, never a share count: share counts are computed at
    execution from a live print against a fresh account snapshot, and the
    risk seat always re-runs. The stale-sizing defect measured elsewhere on
    this desk -- risk sized off the analysis price and filled away from it
    -- does not arise on this path. A pre-BUY daily-loss recheck was named
    here too until 2026-09-20, when the whole account-level loss alarm was
    removed (docs/INCIDENT_HISTORY.md, retired item 32).
  - Every function is best-effort: any error degrades to "no checkpoint"
    (normal full run), never to a crashed session.
"""

import json
import logging
from datetime import datetime, timezone
from src.trading_calendar import utc_now
from pathlib import Path
from src.sentinel.counted import record_swallowed, record_swallowed_here

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 2
CHECKPOINT_DIR = Path("data/checkpoints")
MAX_AGE_MINUTES = 90.0


def checkpoint_path(session: str, et_date: str | None = None) -> Path:
    from src.trading_calendar import session_date_key

    return CHECKPOINT_DIR / f"{et_date or session_date_key()}-{session}.json"


def write(ctx) -> Path | None:
    """Persist the decided-but-not-yet-risk-reviewed plan. Never raises.

    Only writes when there are actual decisions to preserve — an empty
    plan has nothing to resume.
    """
    try:
        pd = ctx.portfolio_decision
        if pd is None or not pd.decisions:
            return None
        payload = {
            "version": CHECKPOINT_VERSION,
            "session": ctx.session,
            "run_id": ctx.run_id,
            "created_at_utc": utc_now().isoformat(),
            "consumed": False,
            "portfolio_decision": pd.model_dump(mode="json"),
            "analyses": [a.model_dump(mode="json") for a in (ctx.analyses or [])],
            "news_intel": (ctx.news_intel.model_dump(mode="json") if ctx.news_intel is not None else None),
            "macro_analysis": (ctx.macro_analysis.model_dump(mode="json") if ctx.macro_analysis is not None else None),
            "macro_summary": ctx.macro_summary or {},
            "earnings_results": ctx.earnings_results or [],
            "data_status": dict(ctx.data_status or {}),
            "admitted_symbols": sorted(
                {str(symbol).strip().upper() for symbol in (ctx.admitted_symbols or set()) if str(symbol).strip()}
            ),
        }
        path = checkpoint_path(ctx.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)  # atomic on POSIX — no torn half-written checkpoint
        logger.info(
            "decision checkpoint written: %s (%d decisions) — a killed run can resume at RiskStage next tick",
            path,
            len(pd.decisions),
        )
        return path
    except Exception as e:  # noqa: BLE001 — checkpointing must never hurt the live run
        record_swallowed("decision_checkpoint.write", e, log=logger)
        return None


def load(session: str, max_age_minutes: float = MAX_AGE_MINUTES) -> dict | None:
    """Load today's unconsumed checkpoint with models reconstructed.

    Returns {run_id, age_minutes, portfolio_decision, analyses, news_intel,
    macro_analysis, macro_summary, earnings_results, data_status} or None
    (missing / consumed / stale / wrong version / unparseable). Never raises.
    """
    try:
        path = checkpoint_path(session)
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        if payload.get("version") != CHECKPOINT_VERSION:
            return None
        if payload.get("consumed") is not False:
            return None
        created = datetime.fromisoformat(payload["created_at_utc"])
        age_min = (utc_now() - created).total_seconds() / 60.0
        if not (0 <= age_min <= max_age_minutes):
            logger.info(
                "decision checkpoint %s ignored: age %.0f min exceeds %.0f",
                path,
                age_min,
                max_age_minutes,
            )
            return None
        from src.models import (
            MacroAnalysis,
            NewsIntelligenceReport,
            PortfolioDecision,
            TechAnalysisResult,
        )

        return {
            "run_id": payload.get("run_id"),
            "age_minutes": age_min,
            "portfolio_decision": PortfolioDecision.model_validate(payload["portfolio_decision"]),
            "analyses": [TechAnalysisResult.model_validate(a) for a in payload.get("analyses") or []],
            "news_intel": (
                NewsIntelligenceReport.model_validate(payload["news_intel"]) if payload.get("news_intel") else None
            ),
            "macro_analysis": (
                MacroAnalysis.model_validate(payload["macro_analysis"]) if payload.get("macro_analysis") else None
            ),
            "macro_summary": payload.get("macro_summary") or {},
            "earnings_results": payload.get("earnings_results") or [],
            "data_status": payload.get("data_status") or {},
            "admitted_symbols": {
                str(symbol).strip().upper() for symbol in (payload.get("admitted_symbols") or []) if str(symbol).strip()
            },
        }
    except Exception as e:  # noqa: BLE001
        record_swallowed("decision_checkpoint.load", e, log=logger)
        return None


def mark_consumed(session: str) -> bool:
    """Flip consumed=true in place; returns True when the checkpoint is
    guaranteed dead (consumed or gone). Never raises.

    Called (a) right before ExecutionStage submits (at-most-once for BUYs),
    (b) on any RiskStage early-exit — an RM-rejected or hard-blocked plan
    must never be re-offered. A third call site, (c) on a daily-loss HALT
    exit, was deleted 2026-09-20 with the account-level loss alarm itself
    (docs/INCIDENT_HISTORY.md, retired item 32); the at-most-once contract
    is unaffected, because that exit no longer happens.

    Fail-CLOSED: if rewriting the file fails (disk full, permissions), fall
    back to deleting it — for load() a missing checkpoint equals a consumed
    one, and unlink succeeds under ENOSPC where write_text cannot. A
    swallowed failure here would leave the plan live, which is the unsafe
    direction for the at-most-once contract.
    """
    path = checkpoint_path(session)
    try:
        if not path.exists():
            return True
        payload = json.loads(path.read_text())
        if payload.get("consumed") is True:
            return True
        payload["consumed"] = True
        payload["consumed_at_utc"] = utc_now().isoformat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
        logger.info("decision checkpoint %s marked consumed", path)
        return True
    except Exception as e:  # noqa: BLE001
        record_swallowed("decision_checkpoint.mark_consumed", e, log=logger)
        logger.error("decision checkpoint consume-mark failed (%s) — deleting the checkpoint instead (fail-closed)", e)
        try:
            path.unlink(missing_ok=True)
            return True
        except Exception as e2:  # noqa: BLE001
            record_swallowed("decision_checkpoint.mark_consumed", e2, log=logger)
            logger.error("decision checkpoint delete also failed: %s — resume lane may re-offer this plan!", e2)
            return False


def write_status(session: str, status: str) -> None:
    """Record a legitimate PM-less terminal status for the ET day
    (no_data, say). The evening dead-man probe reads this to
    avoid false 'morning killed mid-run' alarms. Never raises.
    """
    try:
        path = checkpoint_path(session).with_suffix(".status")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": status,
                    "at_utc": utc_now().isoformat(),
                }
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("session status write failed: %s", e)


def read_status(session: str) -> str | None:
    """Today's recorded terminal status, or None. Never raises."""
    try:
        path = checkpoint_path(session).with_suffix(".status")
        if not path.exists():
            return None
        return json.loads(path.read_text()).get("status")
    except Exception:  # noqa: BLE001
        record_swallowed_here("decision_checkpoint.read_status", log=logger)
        return None
