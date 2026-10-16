"""Trade-review prompt facts: the prompt-facts seat HOLDS the standalone part instead of
inheriting a mixin.

The part is `PromptFactsReview` (src/prompt_facts/review/held.py); the fact bodies live on
the five family parts under src/prompt_facts/review/. `hold_prompt_facts_review(host_cls)`
installs one same-named instance-method delegate per review name, each forwarding to the
part built FOR that call (`review_of(host)`: the seat's current collaborators are read off the
instance and handed to `PromptFactsReview(**collaborators)`, so the part never sees the seat and
nothing goes stale between calls). Nothing is cached on the instance. The module-level names below
are re-exported unchanged for importers and patchers of this module (the ONE mirror block
for this module).
"""

import json as _json  # noqa: F401 -- re-exported
import logging
from pathlib import Path  # noqa: F401 -- re-exported

from src.prompt_facts.review.held import PromptFactsReview
from src.risk.rules import peak_to_trough_pct  # noqa: F401 -- re-exported
from src.trading_calendar import et_today  # noqa: F401 -- re-exported

logger = logging.getLogger(__name__)

#: Seat attribute -> part keyword: the ONLY place the seat's names meet the part's.
HOST_COLLABORATORS = {
    "db": "db",
    "broker": "broker",
    "market": "market",
    "_sweeper": "sweeper",
    "_EXIT_AUDIT_ACTIONS": "exit_audit_actions",
    "_log_conviction_outcome_for_operator": "log_conviction_outcome_for_operator",
}

#: Every review name the host exposes, delegated to the held part.
DELEGATED = (
    "_review_grading",
    "_review_exits",
    "_review_calibration",
    "_review_blocked",
    "_review_replay",
    "_build_recent_sells_for_grading",
    "_build_recent_buys_for_grading",
    "_build_trade_grade_summary",
    "_build_recent_missed_lessons",
    "_build_recent_loss_pits",
    "_build_recent_outlook_calibration",
    "_build_calibration_note",
    "_compute_recent_performance",
    "_build_blocked_proposals",
    "_persist_evening_replay_inputs",
)


def review_of(host) -> PromptFactsReview:
    """A fresh part built from the seat's collaborators AS THEY ARE NOW; the part is not handed the seat."""
    return PromptFactsReview(**{keyword: getattr(host, attr, None) for attr, keyword in HOST_COLLABORATORS.items()})


def _delegate(name: str):
    def shim(self, *args, **kwargs):
        return getattr(review_of(self), name)(*args, **kwargs)

    shim.__name__ = name
    shim.__qualname__ = f"hold_prompt_facts_review.<locals>.{name}"
    shim.__doc__ = "Thin delegate: body lives in src/prompt_facts/review/held.py."
    shim._held_delegate = name
    return shim


def hold_prompt_facts_review(host_cls):
    """Delegate the review names on `host_cls` to a `PromptFactsReview` built per call from its collaborators."""
    for name in DELEGATED:
        setattr(host_cls, name, _delegate(name))
    return host_cls
