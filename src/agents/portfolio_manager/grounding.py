"""Decision grounding: the agent HOLDS the standalone part instead of inheriting a mixin.

Bodies live in src/agents/portfolio_manager/decision_grounding.py (`DecisionGrounding`).
`hold_decision_grounding(agent_cls)` builds one part and installs same-named classmethod
delegates on the agent class, so every existing call site (`self.validate_grounding(...)`,
`PortfolioManagerAgent._canonical_targets(...)`) keeps resolving. Nothing is snapshotted:
`build_evidence_registry` is called through the agent class, `_CONFLICT_SOURCE_ALIASES`
(now a class attribute of the agent) is read at every lookup, and the three bodies the
part reads through `self.` are handed in live (see held_part.live_body). The
module-level names are re-exported unchanged for the package's patch mirror.
"""

from src.agents.portfolio_manager.decision_grounding import (  # noqa: F401 — re-exported
    CONFLICT_UNADJUDICATED_STATUS,
    SUBFLOOR_CATALYST_UNVERIFIED_STATUS,
    DecisionGrounding,
    _ISO_DATE_RE,
    _STATE_CHANGE_ROW_RE,
    _SYMBOL_DIRECTION_RE,
    logger,
)
from src.agents.portfolio_manager.held_part import LiveMapping, hold, live_body

_HOLDER = "_decision_grounding"
_BODIES = "src/agents/portfolio_manager/decision_grounding.py"

#: Every name the agent exposes for decision grounding, delegated to the held part.
DELEGATED = (
    "_target_intent",
    "validate_grounding",
    "_conflict_is_named",
    "_drop_unadjudicated_conflicts",
    "_drop_unanswered_objections",
    "_state_change_symbols_by_date",
    "_catalyst_cites_state_change",
    "_apply_subfloor_catalyst_rule",
    "_drop_invalid_rejections",
    "_drop_invalid_targets",
    "_canonical_targets",
    "_decision_fields_unchanged",
)

#: Bodies the part owns that it also reads through `self.` — handed in live.
LIVE_BODIES = ("_target_intent", "_canonical_targets", "_conflict_is_named")

# §9.3 "disagreement must be adjudicated" ------------------------------
#
# `source` values that need a plainer English alias to be recognised in
# free-form prose. The four other sources (technical/news/earnings/
# macro) are themselves ordinary words; `smart_money` is normally
# written "smart money" by a model composing a sentence, so it is
# aliased explicitly rather than guessed at by a second rule.
_CONFLICT_SOURCE_ALIASES = {
    "smart_money": ("smart_money", "smart money", "smart-money"),
}

_DECISION_FIELDS = ("targets",)

#: Class attributes the mixin used to carry onto the host; installed on the agent
#: class by `hold_decision_grounding` (the aliases are then read live off the class).
HOST_ATTRIBUTES = ("_CONFLICT_SOURCE_ALIASES", "_DECISION_FIELDS")


def build_decision_grounding(agent_cls) -> DecisionGrounding:
    """The part, wired to `agent_cls` live; no agent object is constructed."""
    return DecisionGrounding(
        build_evidence_registry=lambda *args, **kwargs: agent_cls.build_evidence_registry(*args, **kwargs),
        conflict_source_aliases=LiveMapping(agent_cls, "_CONFLICT_SOURCE_ALIASES"),
        **{attr.lstrip("_"): live_body(agent_cls, _HOLDER, attr) for attr in LIVE_BODIES},
    )


def hold_decision_grounding(agent_cls, part: DecisionGrounding | None = None):
    """Make `agent_cls` hold one `DecisionGrounding` and delegate the grounding names to it."""
    for attr in HOST_ATTRIBUTES:
        if attr not in vars(agent_cls):
            setattr(agent_cls, attr, globals()[attr])
    part = part if part is not None else build_decision_grounding(agent_cls)
    return hold(agent_cls, holder_attr=_HOLDER, part=part, delegated=DELEGATED, bodies_module=_BODIES)
