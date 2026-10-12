"""The session's ladder-resolved gross-exposure ceiling (spec §11.2).

Lifted out of `src/pipeline_stages.py` so that `src/pipeline_sizing.py` (which
`src.pipeline_stages` imports) can read it without importing back up. This
module imports nothing from `src.pipeline_stages` or `src.pipeline_sizing`;
`src.pipeline_stages` re-exports the name so every import path is unchanged.
"""

from __future__ import annotations

from src.pipeline_stage_helpers import record_stage


def _session_gross_ceiling(pipeline, ctx):
    """Spec §11.2 — this session's ladder-resolved gross-exposure ceiling.

    The run preamble already resolved it from account state before any agent
    ran; this re-derives it so the resume lane (where the preamble did not
    run) sizes against a real ceiling too. Returns None when the pipeline
    has no resolver or the resolver raises (the raise is recorded as the
    `gross_ceiling` stage). Each caller owns its fallback; it is NOT
    automatically the standing cap. `_entry_deployment_budget`, for one,
    falls back to the pre-margin raw settled-cash clamp and reports the
    headroom as not ladder-backed. None means "unresolved", never "no
    ceiling".
    """
    resolve = getattr(pipeline, "_resolve_gross_ceiling", None)
    if resolve is None:
        return None
    try:
        from src.risk.rules import GrossCeiling

        ceiling = resolve(ctx)
        return ceiling if isinstance(ceiling, GrossCeiling) else None
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "gross_ceiling", exc)
        return None
