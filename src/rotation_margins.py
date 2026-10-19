"""Board item 228 - how far each holding sits from failing each entry rule.

OWNER RULING 2026-10-02: a visibility-only heads-up of which holdings are
drifting toward the chopping block. Clears-or-fails is the sale itself; the
heads-up is the margin. This module records, once per session and per held
name, the distance to failing the rules the entry bar is made of that HAVE a
distance, in the unit each is really measured in:

  R2 rating actionable   - `r2_steps_from_neutral`: steps on the five-step
                           rating scale (strong_buy/buy/neutral/sell/
                           strong_sell). 2 = strong, 1 = plain, 0 = neutral,
                           which is the failure.
  R5 net evidence        - `r5_net_evidence`: the desk's own net independent
                           source score for the name's direction (agreeing
                           minus opposing independent sources, stale and
                           broadcast-macro stances removed exactly as
                           `candidate_eligibility` removes them). The rule
                           fails at 0 or below, so the value IS the number of
                           independent-source points above failing.
                           R5 is retired as a GATE (desk archetype, owner
                           19 Oct 2026): nothing refuses or sells on it;
                           the margin is kept only as a ranking measure.
  R3 BUY-eligible, R6 constructor refusal and R7 conviction bar are
  membership tests with no distance; their failures stay in the existing
  `precheck` reasons and are not restated here.

No threshold, danger band or day count is chosen anywhere: the value is
recorded and the dashboard shows its direction of travel. NOTHING in the
trading path reads this row. It never raises, never gates and never delays: a
failed record must not take a live session with it. It is run-scoped (symbol
None) for the reason given in `_record_rotation_precheck`.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Steps from neutral on the rating ladder, read off the ladder's own order
# (distance from the "neutral" rung), not stored as separate numbers.
_LADDER = ("strong_sell", "sell", "neutral", "buy", "strong_buy")
_STEPS = {r: abs(i - _LADDER.index("neutral")) for i, r in enumerate(_LADDER)}


def margins_for(held, analyses, registry, stale, non_corroborating) -> dict:
    """Pure: {SYMBOL: {rating, r2_steps_from_neutral, r5_net_evidence}}.

    A held name with no Technical read this session is simply absent: no
    margin is invented for a rule that was not evaluated.
    """
    from src.risk.rules import signed_source_score

    by_symbol = {str(a.symbol).upper(): a for a in analyses or []}
    out: dict = {}
    for sym in held:
        a = by_symbol.get(str(sym).upper())
        if a is None:
            continue
        rating = str(getattr(a, "rating", "") or "")
        if rating not in _STEPS:
            continue
        entry = {"rating": rating, "r2_steps_from_neutral": _STEPS[rating]}
        if rating != "neutral":
            direction = "short" if rating in ("sell", "strong_sell") else "long"
            sources = (registry or {}).get(sym, {})
            entry["r5_net_evidence"] = (
                int(
                    signed_source_score(
                        sym,
                        sources,
                        direction,
                        ignored_sources=(stale or {}).get(sym),
                        non_corroborating_sources=(non_corroborating or {}).get(sym),
                    )
                )
                if sources
                else 0
            )
        out[sym] = entry
    return out


def record_rotation_margins(pipeline, ctx) -> None:
    from src.pipeline_stages import _record_pipeline_event

    try:
        precheck = getattr(
            getattr(pipeline, "portfolio_manager", None),
            "last_rotation_precheck",
            None,
        )
        held = tuple(getattr(precheck, "held_examined", ()) or ())
        if not held:
            return
        margins = margins_for(
            held,
            getattr(ctx, "analyses", None),
            getattr(ctx, "evidence_registry", None),
            getattr(ctx, "evidence_stale_sources", None),
            getattr(ctx, "evidence_non_corroborating_sources", None),
        )
        _record_pipeline_event(
            pipeline,
            ctx,
            None,
            "rotation",
            "margins",
            "bar_margins",
            held_margins=json.dumps(margins, sort_keys=True),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rotation margins record failed: %s", exc)
