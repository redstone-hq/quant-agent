from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from src.risk.exit_trigger import ExitTrigger, normalize_trigger
from src.models.base import LLMOutputModel, _normalize_enum_case_fields, _normalize_symbol, logger


class Position(BaseModel):
    symbol: str
    qty: float
    avg_entry: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_intraday_pnl: float = 0.0
    sector: str
    # The broker's own free quantity: held minus shares reserved by open
    # orders. None when the broker did not report it.
    qty_available: float | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)


#: The seats that can object (name-level seats) and the directions an
#: objection can point. `src.risk.conviction_bar.NAME_LEVEL_SEATS` is
#: derived from `ObjectionSeat`, so the schema the AI is sent and the
#: matcher read one list.
ObjectionSeat = Literal["earnings", "news", "smart_money"]
ObjectionDirection = Literal["bullish", "bearish"]


class ObjectionAnswer(LLMOutputModel):
    """One written answer to one seat's objection on one symbol.

    Desk archetype (owner, 19 Oct 2026): a seat may object to a name; the
    deciding AI either accepts the objection or overrules it IN WRITING.
    This is the record of that written answer. Since step D it is in the
    schema the planning AI and the position reviewer are handed. Saved per symbol as
    specialist evidence kind `OBJECTION_ANSWER_KIND` (src/stage_decision.py).
    """

    seat: ObjectionSeat
    objection_direction: ObjectionDirection
    answer_text: str = Field(min_length=1)
    decision: Literal["overrule", "accept"]

    @model_validator(mode="before")
    @classmethod
    def _normalize_case(cls, values):
        return _normalize_enum_case_fields(values, lower_fields=("seat", "objection_direction", "decision"))

    @field_validator("answer_text")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class PositionAction(LLMOutputModel):
    # Stage 3 (shorts): COVER is the short-side twin of SELL/REDUCE for the
    # intraday reviewer — closes/trims a held SHORT, never opens one. The
    # reviewer's prompt (config/prompts/position_reviewer.md) asks for it,
    # and `TradingPipeline._midday_execute_llm_actions` executes it: same
    # named-trigger gate, exit-guard veto, noise band, same-day-trim
    # discipline and AI Risk routing a SELL/REDUCE gets, always as a full
    # close (this schema carries no allocation fraction for it).
    # Owner ruling 2026-10-09: no REDUCE. A held position is kept whole or
    # sold whole; the reviewer has no partial-sell action. A legacy REDUCE in
    # a model answer is lifted out by `PositionReview` and refused on record.
    action: Literal["SELL", "TRAIL_STOP", "COVER", "HOLD"]
    symbol: str
    reason: str
    new_stop_price: float | None = None  # required when action == TRAIL_STOP

    # 2026-09-18. `reason` alone was the whole of an exit's justification,
    # and the whole of its CHECK was a substring match over this prose
    # (`pipeline._reason_cites_hard_trigger`). On 2026-09-16 both real
    # exits carried the reason "adverse news" — two words, entire — and
    # passed every gate, because the phrase is on the list and no checker
    # could tell which claim was being made. These two fields move the
    # claim out of the sentence and into the schema so it becomes
    # decidable; see `src/risk/exit_trigger.py` for the full write-up.
    #
    # Both are OPTIONAL and default to "not stated". That is deliberate,
    # not laxity: an action carrying only prose is mechanically healed
    # from the same phrase vocabulary as before, so nothing that used to
    # execute stops executing on paperwork. `ExitTrigger
    # .CANNOT_SUBSTANTIATE` is a first-class value precisely so the seat
    # is never pushed into naming a trigger it cannot support — recording
    # it is a correct answer that triggers a re-ask, not a refusal.
    exit_trigger: ExitTrigger | None = None
    trigger_evidence: str = ""
    # Desk archetype step A (19 Oct 2026): written answers to seat
    # objections on this holding. Optional; in the reviewer's schema since
    # step D (its prompt describes it); an empty list means none answered.
    objection_answers: list[ObjectionAnswer] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # Action is UPPERCASE per Literal — fold LLM drift like "sell".
        values = _normalize_enum_case_fields(values, upper_fields=("action",))
        # `exit_trigger` is lower_snake per ExitTrigger; fold "Adverse News"
        # and "ADVERSE-NEWS". An UNRECOGNISED value becomes None rather
        # than a ValidationError: raising would drop the whole action (see
        # `PositionReviewerAgent._drop_invalid_actions`) and losing an exit
        # over a misspelled enum is the failure direction this path is
        # explicitly not willing to take. None means "no trigger named",
        # which is then healed from the prose.
        if isinstance(values, dict) and "exit_trigger" in values:
            raw = values.get("exit_trigger")
            coerced = normalize_trigger(raw)
            if coerced is None and raw not in (None, ""):
                logger.warning(
                    "PositionAction: unrecognised exit_trigger %r on %s — "
                    "read as 'no trigger named' and healed from the reason",
                    raw,
                    values.get("symbol"),
                )
            values["exit_trigger"] = coerced
        return values

    @model_validator(mode="after")
    def _trail_stop_requires_new_price(self):
        if self.action == "TRAIL_STOP" and (self.new_stop_price is None or self.new_stop_price <= 0):
            raise ValueError("TRAIL_STOP requires new_stop_price > 0")
        return self


class TargetRevisionFlag(LLMOutputModel):
    """A seat's claim that a held position's take-profit was measured against
    structure that no longer exists. EVIDENCE ONLY — there is no price field.

    This schema carries no target price ON PURPOSE. A revision is a
    RE-DERIVATION: `src.risk.target_revision.assess_target_revision` decides
    whether a structural event legitimises re-asking, and
    `src.data.levels.derive_structural_target` supplies the number from
    today's bars. The seat supplies the observation; the code supplies the
    price. Accepting a typed target here would put a model-guessed number on
    the DENOMINATOR of `thesis_progress_pct` and `pace` — i.e. docs/WORK.md
    item 80 (stop provenance) reappearing on the field that feeds the exit
    guard — so the field simply does not exist to be filled in.

    Raising a flag is not a revision. The overwhelmingly common outcome is
    `REFUSAL_NO_STRUCTURAL_EVENT`, recorded per symbol: a view that a name
    has further to run is not a structural event.
    """

    symbol: str
    evidence: str = Field(min_length=1)
    """WHAT was observed about the structure, not what the seat expects.
    "gapped through and closed above the 214 resistance on earnings" is
    evidence; "I think there is more upside here" is not and will be
    refused."""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)


class PositionReasoningChain(LLMOutputModel):
    """Six-step chain the position reviewer must fill before emitting actions.

    Parallel depth to morning PM's 7-step reasoning_chain — prevents
    intraday-price knee-jerk selling and forces memory-aware, thesis-driven
    decisions. Each field is required; empty strings will fail validation
    so the agent can't skip a step by sending "".
    """

    macro_continuity_check: str = Field(min_length=1)
    """Regime + outlook today vs morning vs this week. Stable ⇒ HOLD bias."""

    thesis_progress_check: str = Field(min_length=1)
    """Per-position thesis_progress_pct / pace / distance-to-stop|target.
    Distinguishes 'fast mover' / 'on pace' / 'stalled' / 'broken'."""

    thesis_integrity_check: str = Field(min_length=1)
    """Every SELL/REDUCE must cite a specific named trigger — thesis_invalid_if
    condition, HIGH-conviction state_change reversal, or bearish earnings
    analysis. Intraday price alone is NOT a trigger, and "correlation breach"
    stopped being one 2026-09-13 (nothing can verify it)."""

    winners_discipline_check: str = Field(min_length=1)
    """For positions with profit > 10%: is momentum fading, is it parabolic,
    has target been exceeded? If no, default is HOLD regardless of size —
    good stocks are meant to be held."""

    session_disposition_check: str = Field(min_length=1)
    """Session-aware framing: 'midday' = afternoon patience, TRAIL_STOP over
    SELL; 'close' = act-if-triggered-not-act-because-time, 17.5h no control,
    act only on clear thesis signals never on clock-driven fear."""

    execution_rationale: str = Field(min_length=1)
    """For each SELL/REDUCE action, a 'lock now' vs 'hold outcome' comparison.
    HOLD needs no comparison. TRAIL_STOP names the upside protected vs given up."""


class PositionReview(LLMOutputModel):
    reasoning_chain: PositionReasoningChain
    actions: list[PositionAction] = []
    overall_assessment: str = Field(min_length=1)
    risk_level: Literal["low", "moderate", "elevated", "high"]
    target_revision_flags: list[TargetRevisionFlag] = []
    """Positions whose take-profit was measured against structure the seat
    observes is gone. Evidence only — see `TargetRevisionFlag`. Each flag is
    adjudicated by `src.risk.target_revision` and recorded per symbol
    whichever way it goes; a flag is never an instruction and never an exit."""

    refused_reduce: list[dict] = Field(default_factory=list, exclude=True)
    """Legacy REDUCE actions lifted out of `actions` at parse (owner ruling
    2026-10-09: whole exits only). Never executed, never converted into a
    SELL — the executor records each one as a refusal. Not part of the
    answer the seat is asked for."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        return _normalize_enum_case_fields(values, lower_fields=("risk_level",))

    @model_validator(mode="before")
    @classmethod
    def _lift_out_legacy_reduce(cls, values):
        # One stray REDUCE must not fail the whole review (and with it every
        # SELL / TRAIL_STOP beside it), and must not be silently dropped.
        if not isinstance(values, dict) or not isinstance(values.get("actions"), list):
            return values
        kept, refused = [], []
        for item in values["actions"]:
            act = item.get("action") if isinstance(item, dict) else None
            if isinstance(act, str) and act.strip().upper() == "REDUCE":
                refused.append(dict(item))
            else:
                kept.append(item)
        if refused:
            values = {**values, "actions": kept, "refused_reduce": refused}
        return values
