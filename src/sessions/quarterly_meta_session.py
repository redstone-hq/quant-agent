"""Quarterly meta-reflection session (moved verbatim from TradingPipeline)."""

from __future__ import annotations

import logging
from pathlib import Path
from src.agents.base import agent_log_kwargs
from src.cost_circuit import PaidAnalysisSuspended
from src.trading_calendar import et_today
import uuid

logger = logging.getLogger(__name__)


class QuarterlyMetaReflectionSession:
    """Quarterly meta-reflection session (moved verbatim from TradingPipeline)."""

    # No broker stop audit in this session; see src/sessions/stop_audit.py.
    AUDITS_BROKER_STOPS = False

    def __init__(
        self,
        *,
        activate_cost_session,
        paid_suspended_payload,
        require_paid_analysis,
        pipeline_file,
        broker,
        config,
        db,
        market,
        meta_reflector,
    ) -> None:
        self._activate_cost_session = activate_cost_session
        self._paid_suspended_payload = paid_suspended_payload
        self._require_paid_analysis = require_paid_analysis
        self._pipeline_file = pipeline_file
        self._broker = broker
        self._config = config
        self._db = db
        self._market = market
        self._meta_reflector = meta_reflector

    def run(
        self,
        *,
        force: bool = False,
        period_end=None,
        lookback_days: int = 90,
        evolution_root: str = "data/evolution",
        prompts_dir: str | Path | None = None,
    ) -> dict:
        """Build the quarterly digest, run the meta-reflector, persist both.

        Cadence: normally this is a NOP unless today is the last trading day
        of the current quarter (`broker.is_last_trading_day_of_quarter`).
        Pass `force=True` to override — used by CLI `--mode meta --force`
        for ad-hoc runs and by tests.

        Output always includes `digest_path` (persisted) and, when the LLM
        succeeded, `reflection_path`. PR3 intentionally stops here — it
        does NOT edit any prompt files. PR4 will pick up reflection.json
        from disk and apply proposed_learnings through prompt_editor.
        """
        from src.evolution.quarterly_digest import (
            build_quarterly_digest,
            load_previous_digest,
            persist_digest,
        )
        from src.agents.meta_reflector import (
            load_previous_reflection,
            persist_reflection,
        )

        today = period_end or et_today()
        if not force:
            try:
                is_last = self._broker.is_last_trading_day_of_quarter(on_date=today)
            except Exception as exc:
                logger.warning(
                    "meta reflection skipped: quarter-end check failed (%s); pass --force to override",
                    exc,
                )
                return {"status": "skipped", "reason": "quarter_end_check_failed"}
            if not is_last:
                logger.info(
                    "meta reflection skipped: %s is not the last trading "
                    "day of the quarter. Pass --force to run anyway.",
                    today,
                )
                return {"status": "skipped", "reason": "not_quarter_end"}

        logger.info("=== Quarterly meta-reflection: %s ===", today)

        # 1. Build digest — deterministic facts layer.
        prev_digest = load_previous_digest(today, root_dir=evolution_root)
        digest = build_quarterly_digest(
            self._db,
            self._market,
            period_end=today,
            lookback_days=lookback_days,
            prev_digest=prev_digest,
            prompts_dir=prompts_dir,
        )
        digest_path = persist_digest(digest, root_dir=evolution_root)
        logger.info(
            "Quarterly digest built for %s: alpha=%s, total_real_misses=%s, total_wrong_buys=%s",
            digest["period"],
            (digest.get("period_performance") or {}).get("alpha_vs_spy_pct"),
            (digest.get("missed_themes") or {}).get("total_real_misses"),
            (digest.get("loss_patterns") or {}).get("total_wrong_buys"),
        )

        # Every invocation is a distinct paid session.  The period remains in
        # the artifacts/result, while a UUID suffix prevents forced reruns of
        # the same quarter from reusing SQLite counters under the run_id PK.
        meta_run_id = f"meta-{digest['period']}-{uuid.uuid4().hex[:8]}"
        self._activate_cost_session(meta_run_id, "meta")
        try:
            self._require_paid_analysis("meta_reflector")
        except PaidAnalysisSuspended as exc:
            payload = self._paid_suspended_payload(meta_run_id, error=exc)
            payload.update(
                period=digest["period"],
                digest_path=str(digest_path),
                reflection_path=None,
                reflection=None,
            )
            return payload

        # 2. Meta-reflector LLM — observe-only in PR3 (no prompt edits).
        # analyze() can raise on provider/network failures after retries. The
        # digest has already been persisted so we must degrade to the
        # digest_only path rather than let the exception abort the run
        # (operators lose the audit / status payload otherwise).
        prev_reflection = load_previous_reflection(today, root_dir=evolution_root)
        reflection = None
        ev_result = None
        try:
            reflection, ev_result = self._meta_reflector.analyze(
                digest=digest,
                prev_reflection=prev_reflection,
            )
        except PaidAnalysisSuspended as exc:
            payload = self._paid_suspended_payload(meta_run_id, error=exc)
            payload.update(
                period=digest["period"],
                digest_path=str(digest_path),
                reflection_path=None,
                reflection=None,
            )
            return payload
        except Exception as exc:
            logger.error(
                "meta_reflector.analyze raised; falling back to digest_only: %s",
                exc,
                exc_info=True,
            )

        # Always log the agent's raw output for audit, even on failure.
        if ev_result is not None:
            try:
                self._db.insert_agent_log(
                    agent_name="meta_reflector",
                    run_id=meta_run_id,
                    input_summary=(
                        f"{digest['period']} · alpha={(digest.get('period_performance') or {}).get('alpha_vs_spy_pct')}"
                    ),
                    input_message=ev_result.user_message,
                    output_summary=(reflection.style_self_portrait[:200] if reflection else "parse_error"),
                    full_response=ev_result.raw_text,
                    model=ev_result.model,
                    tokens_used=ev_result.tokens_used,
                    input_tokens=ev_result.input_tokens,
                    output_tokens=ev_result.output_tokens,
                    cost_usd=ev_result.cost_usd,
                    **agent_log_kwargs(ev_result),
                )
            except Exception as exc:
                logger.warning("meta_reflector agent_log insert failed: %s", exc)

        if reflection is None:
            logger.error("Meta-reflector returned no valid reflection; digest persisted, reflection missing.")
            return {
                "status": "digest_only",
                "run_id": meta_run_id,
                "period": digest["period"],
                "digest_path": str(digest_path),
                "reflection_path": None,
                "reflection": None,
            }

        reflection_path = persist_reflection(reflection, root_dir=evolution_root)
        logger.info(
            "Quarterly meta-reflection complete: %s · %d proposed learnings",
            digest["period"],
            len(reflection.proposed_learnings),
        )

        # 3. Prompt editor — only runs when evolution.enabled. When off
        # (default until a deployment has reviewed a quarter or two of
        # reflection.json contents by hand), we return without touching any
        # prompt file. The editor itself short-circuits to a full-rejection
        # report; we still persist the attempt log for audit continuity.
        editor_report: dict | None = None
        try:
            from src.config import EvolutionConfig

            evolution_cfg = getattr(self._config, "evolution", None)
            if evolution_cfg is None:
                evolution_cfg = EvolutionConfig()
        except Exception:
            from src.config import EvolutionConfig

            evolution_cfg = EvolutionConfig()

        try:
            from src.evolution.prompt_editor import PromptEditor

            resolved_prompts_dir = (
                Path(prompts_dir)
                if prompts_dir is not None
                else Path(self._pipeline_file).resolve().parent.parent / "config" / "prompts"
            )
            editor = PromptEditor(
                config=evolution_cfg,
                prompts_dir=resolved_prompts_dir,
                evolution_dir=evolution_root,
            )
            result_obj = editor.apply_reflection(reflection)
            editor_report = result_obj.to_dict()
            if result_obj.applied:
                logger.info(
                    "Prompt editor applied %d learning(s) across %d agent(s); git_commit=%s",
                    len(result_obj.applied),
                    result_obj.agents_edited,
                    result_obj.git_commit,
                )
            elif result_obj.rejected:
                # Most common: evolution.enabled=false (observe-only). Log
                # at INFO so operators see why nothing was applied.
                logger.info(
                    "Prompt editor did not apply any learnings (%d rejected). First reason: %s",
                    len(result_obj.rejected),
                    result_obj.rejected[0].reason,
                )
        except Exception as exc:
            logger.error("Prompt editor invocation failed: %s", exc, exc_info=True)

        return {
            "status": "reflected",
            "run_id": meta_run_id,
            "period": digest["period"],
            "digest_path": str(digest_path),
            "reflection_path": str(reflection_path),
            "reflection": reflection.model_dump(),
            "proposed_learnings_count": len(reflection.proposed_learnings),
            "editor_report": editor_report,
        }
