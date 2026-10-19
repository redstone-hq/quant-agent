from decimal import Decimal
from typing import Annotated, Literal
from pydantic import ConfigDict, Field, PrivateAttr, ValidationInfo, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from src.models.base import (
    LLMOutputModel,
    SOFT_EXIT_UNKNOWN,
    _normalize_enum_case_fields,
    _normalize_symbol,
    open_target_missing_falsifier,
    logger,
)
from src.models.decisions import AnalystProvenance, ReasoningChain, TradeDecision
from src.models.positions import ObjectionAnswer

from src.models.risk_narrative_claims import _explicit_risk_pct_claim_texts, risk_pct_half_ulp


class TargetPosition(LLMOutputModel):
    """PM's per-symbol intent — WHAT the book should look like, not HOW to get there.

    The PortfolioConstructor translates a list of TargetPositions + current
    holdings + market prices + TA ATR into concrete TradeDecision orders. The
    LLM no longer guesses entry prices, stops, or share counts — it only
    expresses intent.

    Semantics:
    - target_weight_pct = 0 and symbol currently held → close the position.
    - target_weight_pct > 0 on a new symbol → open.
    - target_weight_pct > current weight → add (partial BUY for the delta).
    - target_weight_pct < current weight → trim (partial SELL for the delta).
    - Held symbols NOT appearing in the target list → hold at current weight
      (no instruction = no change). PM may include them explicitly with a
      `keep` note for audit clarity, but it's not required.
    """

    model_config = ConfigDict(validate_assignment=True)

    symbol: str
    # Stage 3 (shorts). Default "long" so every stored decision — every
    # historical agent_logs / specialist_evidence row parsed through this
    # model, and every live target a PM prompt that doesn't yet know this
    # field exists ever emits — replays with EXACTLY the behaviour it had
    # before this field existed. The constructor turns this + the
    # (unsigned) size field into a SIGNED target weight:
    #     signed_target = -target_pct if direction == "short" else +target_pct
    # `current_pct` (see `_current_weights`) is already signed — negative
    # means a held short. Delta math then operates on signed weights: a
    # positive delta is buy-side (BUY to open/add a long, or COVER to
    # reduce a short); a negative delta is sell-side (SELL to reduce a
    # long, or SHORT to open/add a short).
    direction: Literal["long", "short"] = "long"
    # --- Sizing (Phase 2b, 2026-08-27) -------------------------------------
    # Conviction is expressed as RISK, not as notional weight.
    #
    # `target_weight_pct` is risk-blind: a 3% position stopped 10% away risks
    # 0.3% of equity; the same 3% stopped 2% away risks 0.06%. The PM was
    # choosing the number that does NOT determine what a losing trade costs,
    # while the number that does — the distance to the stop — was set by
    # somebody else entirely.
    #
    # `risk_allocation_pct` is the share of equity this idea may lose if its
    # stop is hit. The constructor derives share count from it:
    #     shares = (equity x risk_pct / 100) / |entry - stop|
    # A wider stop therefore yields a SMALLER position rather than a rejected
    # trade, which eliminates the entire "stops too tight" failure class:
    # risk is never controlled by squeezing the stop.
    #
    # Envelope is owner-ratified (2026-08-27): 5% ceiling, 0.5% floor, below
    # which the idea is not worth trading. 0.0 is legal and means CLOSE.
    risk_allocation_pct: float | None = Field(default=None, ge=0.0, le=5.0)
    # Legacy notional sizing. Retained ONLY so historical agent_logs and
    # specialist_evidence rows still parse — `src/replay.py` and the Mission
    # Control API both re-validate stored PM output through this model. New
    # live decisions must supply `risk_allocation_pct`; the grounding
    # validator enforces that. When both are present, risk wins.
    target_weight_pct: float | None = Field(default=None, ge=0.0, le=20.0)
    conviction: Literal["high", "medium", "low"] = "medium"
    thesis: str
    # Same null-coercion as TechAnalysisResult above, same measured reason: a
    # model emitting an explicit null here must not invalidate the target.
    # (No production null observed on this field — 272 occurrences, 0 nulls —
    # but the tech-side field is the same prompt instruction to the same
    # models, so the exposure is real even though it has not fired yet.)
    thesis_invalid_if: str = ""

    @field_validator("thesis_invalid_if", "catalyst", mode="before")
    @classmethod
    def _null_soft_exit_is_unknown_not_a_wipe(cls, v, info: ValidationInfo):
        """A target is always an actionable intent, so an explicit null
        records `unknown` rather than wiping a stated soft-exit to silent
        empty. Never invents a falsifier or catalyst string."""
        if isinstance(v, str) and v.strip():
            return v
        if v is None:
            return SOFT_EXIT_UNKNOWN
        return v

    # Optional override hints the constructor MAY use. Non-binding — if
    # absent, the constructor falls back to TA's ATR-based stop (2*ATR) and
    # the broker's live price for entry.
    suggested_stop_price: float | None = None
    catalyst: str = ""  # populated when target violates R/R < 1.5 discipline
    # --- The sub-floor catalyst exception, as a CHECKED fact (2026-09-11) --
    # Set to True by `PortfolioManagerAgent._apply_subfloor_catalyst_rule`,
    # and by nothing else, for a below-floor target whose `catalyst` cited
    # the ISO date of a real `active_state_changes` row that names this
    # symbol in the direction this trade needs — and which that same gate
    # then capped at `STARTER_POSITION_RISK_PCT`.
    #
    # It exists because the exception was previously INERT end to end. The
    # PM gate verified the citation and capped the size, and then
    # `PortfolioConstructor._widen_stop_past_noise` refused the order
    # anyway on `min_reward_risk_after_widening` — the same floor, applied
    # a second time one stage later, with no knowledge that the exception
    # had been granted. Measured 2026-09-11 on a level-backed stop with a
    # real structural target at reward:risk 1.2: the PM kept and capped it,
    # the constructor returned `(None, None)`. So a verified catalyst could
    # never produce a trade, which made parts (b) and (c) of docs/WORK.md
    # item 1 decorative.
    #
    # NOT MODEL-SETTABLE, and that is the whole point of making it a PRIVATE
    # attribute rather than a field: prompt compliance is not a control, so
    # the flag the constructor trusts must be one only Python can write. A
    # private attribute cannot be supplied in input at all — not by raw LLM
    # JSON, and not by a stored historical row re-validated through this
    # model by `src/replay.py` or the Mission Control API, which would
    # otherwise be claiming a check that never ran. Stripping a public field
    # in a validator was tried and is NOT equivalent: pydantic runs
    # model-level before-validators on the `validate_assignment` path too,
    # so the same strip that blocks the model also blocks the gate.
    #
    # Read it via `subfloor_catalyst_verified`; write it only via
    # `mark_subfloor_catalyst_verified()`. The PM decision object is handed
    # to `PortfolioConstructor.construct_orders` directly (see
    # `src/pipeline_stages.py`) with no serialization hop in between, so a
    # private attribute survives the whole journey it needs to.
    _subfloor_catalyst_verified: bool = PrivateAttr(default=False)

    @property
    def subfloor_catalyst_verified(self) -> bool:
        """Was a sub-floor reward:risk catalyst exception GRANTED for this
        target by `PortfolioManagerAgent._apply_subfloor_catalyst_rule`?

        Read-only on purpose. See `_subfloor_catalyst_verified` above.
        """
        return self._subfloor_catalyst_verified

    def mark_subfloor_catalyst_verified(self) -> None:
        """Record that the exception was granted. Called by the PM gate, in
        the same breath as the starter-size cap, and by nothing else."""
        self._subfloor_catalyst_verified = True

    # Default preserves read compatibility for historical agent logs.  New
    # live PM decisions are required to populate this by the deterministic
    # PM grounding validator before they may reach PortfolioConstructor.
    provenance: list[AnalystProvenance] = Field(default_factory=list)

    # Desk archetype step A (19 Oct 2026): written answers to seat
    # objections on this symbol (`ObjectionAnswer`). Optional, so old
    # outputs parse unchanged. In the schema the PM is handed since step D
    # (its prompt describes it); an empty list means no objection answered.
    objection_answers: list[ObjectionAnswer] = Field(default_factory=list)

    # --- Risk-narrative cross-check (item 163) -----------------------------
    # `risk_allocation_pct` is authoritative for anything the desk acts on;
    # this flag NEVER changes it and prose NEVER overrides it. It only
    # records that the PM's own words and its own structured field disagreed
    # about how much this idea risks, so the mismatch is surfaced instead of
    # silently trusted. Set only by `_flag_risk_narrative_mismatch` below.
    risk_narrative_mismatch: bool = False
    risk_narrative_mismatch_detail: str = ""

    @model_validator(mode="after")
    def _flag_risk_narrative_mismatch(self):
        """Flag when `thesis` prose states an explicit risk % that
        materially differs from the authoritative `risk_allocation_pct`.

        Runs only when `risk_allocation_pct` is populated -- a legacy target
        carrying only `target_weight_pct` has no authoritative risk number to
        check prose against. Uses `object.__setattr__` rather than a plain
        assignment because `model_config` sets `validate_assignment=True`:
        a normal `self.x = ...` here re-enters this same validator on every
        assignment and recurses without terminating (measured).
        """
        if self.risk_allocation_pct is None:
            return self
        half_ulp = risk_pct_half_ulp(self.risk_allocation_pct)
        if half_ulp is None:
            return self
        field = Decimal(str(self.risk_allocation_pct))
        mismatched = [
            text for text in _explicit_risk_pct_claim_texts(self.thesis) if abs(Decimal(text) - field) > half_ulp
        ]
        if mismatched:
            detail = (
                f"{self.symbol}: thesis states risk {float(mismatched[0]):g}% but "
                f"risk_allocation_pct={self.risk_allocation_pct:g}%"
            )
            object.__setattr__(self, "risk_narrative_mismatch", True)
            object.__setattr__(self, "risk_narrative_mismatch_detail", detail)
            logger.warning("risk_narrative_mismatch: %s", detail)
        return self

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_enum_case_fields(values, lower_fields=("conviction",))
        # Common plain-English synonym emitted by otherwise valid PM plans.
        # This is a confidence label only; mapping moderate→medium changes no
        # target, holding, risk, or execution authority.
        if isinstance(values, dict) and values.get("conviction") == "moderate":
            values["conviction"] = "medium"
        return values

    @model_validator(mode="after")
    def _requires_one_sizing_field(self):
        """A target must size itself somehow.

        Both fields are Optional so historical rows (which carry only
        `target_weight_pct`) still parse, but a target carrying NEITHER is
        meaningless — it names a symbol and asks for nothing. Rejecting it
        here keeps the "drop malformed entries" path in the PM agent from
        having to special-case a silently zero-sized position.
        """
        if self.risk_allocation_pct is None and self.target_weight_pct is None:
            raise ValueError(
                f"{self.symbol}: target supplies neither risk_allocation_pct "
                f"nor target_weight_pct — it sizes to nothing"
            )
        return self

    @property
    def is_close(self) -> bool:
        """PM asking to exit this name entirely."""
        if self.risk_allocation_pct is not None:
            return self.risk_allocation_pct == 0.0
        return self.target_weight_pct == 0.0

    @property
    def missing_open_falsifier(self) -> bool:
        """Open/increase intent with no real thesis_invalid_if.

        Target-only view: a full close is exempt; a non-zero trim is
        not classified here because that needs the live book. The
        ticket-book gate classifies via `_target_intent` and passes
        `intent` so reductions are admitted. Enforced at the ticket
        book (heal + one paid retry, then refuse), not as a
        ValidationError — stored historical rows with an empty field
        must still parse. Catalyst stays optional here.
        """
        return open_target_missing_falsifier(self)


#: The named grounds on which the portfolio manager may drop a candidate it
#: was shown. This is a VOCABULARY, not a gate: nothing here is consulted
#: before a trade, no threshold reads it, and adding or removing a code
#: cannot change what the desk is allowed to buy. What it changes is that
#: "why was this name dropped" has an answer that two different sessions can
#: be COMPARED on.
#:
#: Why an enum and not free text (2026-09-18, board item 133). The jam
#: detector (`src/refusal_signature.py`) separates a jammed gate from a quiet
#: market by asking whether every candidate died for the SAME reason while
#: the candidates changed. Free prose defeats that from both ends: it varies
#: with wording where the cause is identical (a missed alarm), and it cannot
#: be counted. A code beside the prose is the shape the constructor's own
#: refusals already use (`refusal=` / `fault=` on the deterministic-gate
#: events), so this is the established pattern rather than a new one.
#:
#: The codes name causes the seat can actually have, and deliberately do NOT
#: mirror any Python gate — a PM rejection is the seat's own judgement, and
#: the deterministic gates record their own refusals separately.
CANDIDATE_REJECTION_CODES: tuple[str, ...] = (
    # the evidence itself
    "evidence_insufficient",  # too few current sources to justify risk
    "evidence_conflicts",  # sources disagree materially, unresolved
    "evidence_stale",  # what exists is too old to act on
    # the idea
    "thesis_not_compelling",  # evidence present; the setup does not earn a slot
    "no_readable_structure",  # no level to enter or invalidate against
    "reward_not_worth_risk",  # the seat's own read of the payoff
    "event_risk",  # earnings/known event too close
    # the book
    "risk_budget_full",  # no portfolio risk budget left for a new name
    "no_deployment_headroom",  # no cash/buying power to deploy
    "sector_or_cluster_crowded",  # concentration against something already held
    "better_use_of_the_slot",  # ranked below a name that was taken instead
    "already_sized_correctly",  # held, and the current size is the right one
    # the escape hatch — detail is what carries it
    "other",
)


class CandidateRejection(LLMOutputModel):
    """One candidate the portfolio manager was shown and chose NOT to target.

    Why this exists (board item 133, 2026-09-18). The PM returned a list of
    TARGETS and nothing else, so `DecisionStage` recorded every analysed
    candidate missing from that list as
    `portfolio_manager|omitted|candidate_not_selected_for_target`. The seat
    was never ASKED why it dropped a name, so there was no per-candidate
    reason and there could not be one — the absence of a reason WAS the
    reason, identically, for every name and every session. On 2026-09-18 the
    jam detector fired on exactly that: two sessions, three different
    candidates, one unvarying key. It could not have reported anything else,
    which is what made it useless as a diagnosis.

    `code` is the comparable identity; `detail` is what a person reads.
    Both are kept — the code alone does not tell the owner what happened to
    HIS candidate, and the prose alone cannot be compared across sessions.

    Fail-OPEN on shape, exactly as `SymbolRejection` is and for the same
    reason: a rejection lost to a formatting slip turns back into the silent
    omission this model exists to end. An unrecognised code becomes `other`
    with the original spelling preserved in `detail`; a missing detail
    becomes a stated absence. Only an entry naming no recoverable symbol is
    dropped, because there is nothing to file it against.
    """

    symbol: str
    code: str = Field(default="other")
    detail: str = Field(default="", max_length=600)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, values):
        _absent = "the portfolio manager named no reason beyond dropping the name"
        if isinstance(values, str):
            return {"symbol": values, "code": "other", "detail": _absent}
        if not isinstance(values, dict):
            return values
        values = dict(values)
        # Tolerate the two spellings a model actually reaches for.
        for alias in ("reason_code", "rejection_code"):
            if not values.get("code") and values.get(alias):
                values["code"] = values[alias]
        for alias in ("reason", "explanation", "why"):
            if not values.get("detail") and values.get(alias):
                values["detail"] = values[alias]
        raw_code = str(values.get("code") or "").strip().lower().replace(" ", "_")
        detail = values.get("detail")
        detail = detail.strip() if isinstance(detail, str) else ""
        if raw_code not in CANDIDATE_REJECTION_CODES:
            if raw_code:
                # Never paraphrase an unknown code into a known one — that
                # would invent a cause. Keep the spelling where a person can
                # read it and let it count as `other` when compared.
                detail = f"[unrecognised reason code '{raw_code}'] {detail}".strip()
            raw_code = "other"
        values["code"] = raw_code
        values["detail"] = detail or _absent
        return values

    @field_validator("symbol")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return _normalize_symbol(v)


class PortfolioDecision(LLMOutputModel):
    reasoning_chain: ReasoningChain
    # Phase 2 output: PM emits intent (target weights), not orders.
    targets: list[TargetPosition] = Field(default_factory=list)
    #: Every candidate the seat was shown and is NOT targeting, with the
    #: named ground it dropped it on (board item 133, 2026-09-18). A target
    #: or a rejection — the seat must account for each name one way or the
    #: other. Empty here is not an error at PARSE time (an old stored row
    #: must still load, and a validation failure would fail the whole
    #: decision closed over a bookkeeping field). The accounting is enforced
    #: downstream in `DecisionStage`, in the desk's standing heal order:
    #: mechanical heal, one bounded re-ask, then a durable per-symbol
    #: refusal — the same posture `missing_open_falsifier` documents.
    rejections: list[CandidateRejection] = Field(default_factory=list)
    # Phase 2 derived: populated by PortfolioConstructor AFTER the LLM returns.
    # Downstream stages (hard risk filter, RM review, execution) read this.
    # PM must never fill it directly — the LLM output is validated with
    # `decisions` empty; the pipeline injects constructor output before
    # handing the object off to downstream stages.
    # `SkipJsonSchema`: kept off the rendered response_format entirely. Strict
    # structured output made this field REQUIRED, so the seat was ordered to
    # emit a full order book (entry_price / stop_loss / take_profit per name)
    # that `PortfolioManagerAgent.validate_grounding` then refuses outright —
    # "portfolio manager supplied concrete decisions; only grounded targets
    # may cross the PM boundary" discards every target and the whole book.
    # The prompt never mentions the field, so there was nothing telling the
    # seat to leave it empty.
    decisions: Annotated[list[TradeDecision], SkipJsonSchema()] = Field(default_factory=list)
    #: Symbols the PM proposed that the deterministic constructor DROPPED
    #: (unmeasurable range reward:risk, no readable structure, no valid
    #: stop, ... — no reward:risk floor since 2026-09-11). Set by
    #: the pipeline after `construct_orders`, never by the LLM.
    #:
    #: Exists because PM writes its `reasoning_chain` BEFORE the constructor
    #: runs, and that narrative is rendered to the Risk Manager verbatim. When
    #: the constructor silently removed a trade, the RM saw a story arguing for
    #: symbols absent from the order list and — correctly, given what it was
    #: shown — vetoed the WHOLE plan as incoherent. On 2026-08-31 that killed
    #: two trades the RM had just called valid ("While COP and V are valid, the
    #: plan as presented is not internally consistent"). Telling the RM what
    #: was removed, and that removal was deterministic, is what makes the
    #: remaining plan legible. Same reasoning as the constructor's existing
    #: `cap_note` provenance, which solved this once already for allocation
    #: caps (portfolio_constructor.py ~947).
    # `SkipJsonSchema` for the same reason as `decisions` above: set by the
    # pipeline after `construct_orders`, never by the LLM, so it has no place
    # in the schema the LLM is handed.
    constructor_dropped: Annotated[list[str], SkipJsonSchema()] = Field(default_factory=list)
    portfolio_view: str


class RiskModification(LLMOutputModel):
    symbol: str
    field: str
    original_value: float
    new_value: float
    reason: str

    @field_validator("symbol")
    @classmethod
    def _normalize(cls, v: str) -> str:
        # Same normalization as `SymbolRejection._normalize` — 2026-09-03,
        # item 26. Without it, `_apply_risk_modifications` (src/pipeline.py)
        # matched `mod.symbol` against `decision.symbol` case-sensitively,
        # silently dropping a modification whose case differed (fixed at
        # that one comparison site the same day) — but a second, independent
        # comparison (`decision.symbol in modified_symbols`,
        # src/pipeline_stages.py, deciding whether a symbol's funnel outcome
        # reads "modified" or "approved") read the SAME unnormalized field
        # and would have kept silently mismatching. Normalizing at the model
        # boundary closes both call sites (and any future one) at once,
        # rather than patching each comparison site individually.
        return _normalize_symbol(v)


def _normalize_rejected_symbols_field(values):
    """Coerce the container shapes an LLM emits for `rejected_symbols` into
    the list of objects the schema declares.

    Same fail-open logic as `SymbolRejection._coerce_shorthand`: losing a
    refusal to a container-shape slip means a name the risk manager refused
    goes on to trade, so the shapes that unambiguously carry the same
    information are accepted —

        "XLE"                        -> [{"symbol": "XLE"}]
        "XLE, CHPX"                  -> [{"symbol": "XLE"}, {"symbol": "CHPX"}]
        {"symbol": "XLE", ...}       -> [ {"symbol": "XLE", ...} ]
        {"XLE": "R/R 1.18 < 1.5"}    -> [{"symbol": "XLE", "reason": "..."}]

    Any other shape (a number, a bool) is left exactly as-is so Pydantic
    raises on it — `rejected_symbols` is decision-bearing, so that failure
    correctly fails the whole verdict closed rather than silently trading a
    refused name.
    """
    if not isinstance(values, dict):
        return values
    raw = values.get("rejected_symbols")
    if raw is None or isinstance(raw, list):
        return values
    values = dict(values)
    if isinstance(raw, str):
        values["rejected_symbols"] = [{"symbol": part} for part in raw.split(",") if part.strip()]
    elif isinstance(raw, dict):
        if "symbol" in raw:
            values["rejected_symbols"] = [raw]
        else:
            values["rejected_symbols"] = [
                {"symbol": sym, "reason": reason if isinstance(reason, str) else None} for sym, reason in raw.items()
            ]
    return values


#: The one label a risk verdict carries, shared by both verdict shapes AND by
#: each entry-risk refusal (`EntrySymbolRejection.category`). Defined here,
#: not in `risk_verdicts`, because that module imports this one; it is
#: re-exported from there unchanged so every existing import keeps working.
#: `portfolio_manager` reads this field's recent history to self-calibrate,
#: and a category it does not know is a silently dropped signal.
RiskReasonCategory = Literal[
    "clean",  # approved untouched, no mods
    "oversized",  # sizing too aggressive vs conviction
    "rr_fail",  # legacy label: a range BUY cut/refused with reward:risk as a named factor (no universal floor since 2026-09-11; never applies to a breakout)
    "concentration",  # sector / single-name too heavy
    "correlation_risk",  # theme/factor clustering flagged
    "event_risk",  # FOMC / macro release volatility (never earnings: owner ruling, earnings never resize or block)
    "macro_misalign",  # plan direction contradicts the macro read (legacy: exposure vs a macro target, removed 2026-09-17)
    "data_degraded",  # multiple upstream sources failed
    "signal_fidelity",  # PM contradicts TechAnalyst without explanation
    "other",  # doesn't fit the above
]


class SymbolRejection(LLMOutputModel):
    """One symbol refused on its own merits, without touching the rest of
    the plan. The per-TRADE lane of the risk verdict.

    Why this exists (spec Phase 10.1). `RiskVerdict.approved` is one bool
    for the whole plan, and `RiskModification` can retune a symbol's fields
    but cannot refuse one. So a single failing leg killed every other leg:
    on run `run-64290730` (2026-09-01 morning) the risk manager rejected the
    whole plan citing XLE alone — constructed R/R 1.18, under the 1.5 floor —
    and CHPX died with it at R/R 3.03, different sector, unrelated thesis.
    Zero trades.

    The governing principle, in the owner's words: *"The batch is arbitrary —
    it is whatever happened to be proposed in one run. Judging a trade against
    its accidental co-passengers makes no sense. Judge it against what the
    account actually holds."* A per-SYMBOL failure (an R/R breach on one name,
    an event-risk flag on one name) refuses that name and nothing else. A
    BOOK-level failure (a correlation cluster, total exposure, drawdown state)
    is a property of the whole account and still refuses everything, via
    `approved=false` — see the field comment on `RiskVerdict.rejected_symbols`.

    `reason` is not optional prose: it is the per-symbol audit trail, written
    to `specialist_evidence` (kind=`rejection`, scope=`symbol`) and to the
    symbol's `pipeline_event`, so "why was this name refused" is answerable
    per name rather than only per run.
    """

    symbol: str
    reason: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _coerce_shorthand(cls, values):
        """Accept the two shorthands an LLM actually emits, and NEVER lose a
        refusal to a formatting slip.

        A dropped rejection is fail-OPEN — a name the risk manager refused
        would trade — so this normalizes rather than discards: a bare
        `"XLE"` string becomes a rejection with a stated absent reason, and
        a missing/blank `reason` on an otherwise well-formed entry becomes
        the same. An entry naming NO recoverable symbol is deliberately left
        to fail validation: the verdict then fails closed as a whole (see
        `RiskManagerAgent._DECISION_FIELDS`), because we know a refusal was
        intended and cannot tell which name it was for.
        """
        _absent = "risk manager refused this symbol without stating a reason"
        if isinstance(values, str):
            return {"symbol": values, "reason": _absent}
        if isinstance(values, dict):
            values = dict(values)
            reason = values.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                values["reason"] = _absent
        return values

    @field_validator("symbol")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return _normalize_symbol(v)


class EntrySymbolRejection(SymbolRejection):
    """One entry-risk refusal carrying its OWN category (owner ruling
    2026-10-16: the risk seat keeps its power to block a buy it judges risky;
    earnings never resize or block a trade).

    Why per entry. `RiskVerdict.reason_category` is ONE label for the whole
    verdict, and a bare `{symbol, reason}` entry is free text — so on
    2026-10-15 17:17 UTC a buy blocked on an earnings date ~3 sessions away,
    tagged `event_risk` at verdict level, was indistinguishable in code from a
    legitimate block. Each entry now names its category from the same closed
    set, and an `event_risk` entry must name the fetched macro release or FOMC
    meeting it relies on in `event`; `RiskStage` applies such an entry only
    when that event matches one fetched onto the run context inside the
    horizon.

    A missing `category` does NOT fail the verdict: one forgotten label must
    not stop every trade on the run (a whole-verdict failure stopped the desk
    trading on 2026-10-15). `RiskStage` applies an uncategorised block — the
    seat keeps its veto — and records it, so the gap is visible. An
    `event_risk` entry with no `event` names nothing fetched, so `RiskStage`
    refuses it like any other unverified event block.
    """

    category: RiskReasonCategory | None = None
    event: str | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _lower_category(cls, v):
        return v.strip().lower() if isinstance(v, str) else v
