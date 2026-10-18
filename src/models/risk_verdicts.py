from pydantic import Field, model_validator
from src.models.base import LLMOutputModel, _normalize_enum_case_fields
from src.models.portfolio import (  # noqa: F401  RiskReasonCategory re-exported
    EntrySymbolRejection,
    RiskModification,
    RiskReasonCategory,
    SymbolRejection,
    _normalize_rejected_symbols_field,
)


class _PerSymbolRejections:
    """`rejections_by_symbol()` for both risk verdict shapes.

    A plain mixin, deliberately carrying NO annotations: pydantic collects
    field annotations from every base, so declaring `rejected_symbols` here
    would reorder the fields of `RiskVerdict`, which is serialised into
    archived agent logs.
    """

    def rejections_by_symbol(self) -> dict[str, str]:
        """`{SYMBOL: reason}` for every per-symbol refusal in this verdict.

        First entry wins on a duplicated symbol — two reasons for refusing
        the same name still refuse it once, and the first is the one the
        audit trail carries.
        """
        out: dict[str, str] = {}
        for rejection in self.rejected_symbols:
            out.setdefault(rejection.symbol, rejection.reason)
        return out


class RiskReasoningChain(LLMOutputModel):
    """6-step CoT for the risk manager — forces audit trail on the last gate.
    Every field has `min_length=1` so the LLM can't skip a step by sending
    `""`. Matches the discipline on the other CoT chains.

    MORNING PLAN path only. The exit review uses `ExitRiskReasoningChain`,
    which is this chain minus the three steps the exit prompt itself stands
    down or inverts — see that class.
    """

    rr_audit: str = Field(
        min_length=1
    )  # setup-aware since 2026-09-11: breakouts carry no R/R judgement; a range trade's real ratio is an input, not a floor
    signal_fidelity: str = Field(min_length=1)  # does PM's action align with Tech/Macro/News? silent contradictions?
    correlation_check: str = Field(min_length=1)  # any hidden cluster / factor concentration across decisions?
    event_risk: str = Field(min_length=1)  # fetched earnings dates for these names (information only; scheduled economic releases are not an input, owner ruling 2026-10-18)
    sizing_sanity: str = Field(min_length=1)  # is size proportional to conviction and R/R? any outsized bet?
    overall: str = Field(min_length=1)  # final synthesis and why approved/rejected/modified


class ExitRiskReasoningChain(LLMOutputModel):
    """The risk seat's chain on the EXIT-REVIEW path.

    Same six steps as `RiskReasoningChain`, but three of them are no longer
    MANDATORY, because the exit prompt itself already tells the seat they do
    not apply here (`src/agents/risk_review_mode._EXIT_REVIEW_HEADER`), and a
    `min_length=1` field is a demand for an answer:

    - `rr_audit` — checklist 2 is stood down. An exit has no entry geometry:
      `_risk_review_exits` sends entry, stop and target as `0.0` because there
      is no such thing to send. There is no ratio to audit.
    - `sizing_sanity` — checklist 5 is stood down. No BUY or SHORT reaches
      this path, and the % on an exit is the position reviewer's call on its
      own position.
    - `event_risk` — checklist 4 INVERTS here. "A binary event inside the
      window, so downsize or reject" was written for an ENTRY, where refusing
      carries LESS risk through the event. On an exit, refusing carries the
      position THROUGH it. Compelling an answer to a question whose standing
      instruction points the wrong way is not a safeguard; it produced the one
      measured harm on this path. Archived row 330 (2026-09-01, `close-`
      `0e9129f1`) answered "next earnings dates were NOT FETCHED this run" and
      then set `reason_category: "data_degraded"` on that basis — a label
      `portfolio_manager` reads back to self-calibrate. Optional here means
      the seat may still report the dates it was given; it is no longer forced
      to manufacture a paragraph about them.

    `signal_fidelity`, `correlation_check` and `overall` stay mandatory: all
    three are live questions on an exit, and `overall` is the audit trail.

    Nothing here touches the morning BUY path — `RiskReasoningChain` is
    unchanged and all six of its fields remain mandatory.
    """

    rr_audit: str = ""  # stood down on this path (no entry geometry)
    signal_fidelity: str = Field(min_length=1)  # does the reviewer's exit align with News/Macro? silent contradictions?
    correlation_check: str = Field(min_length=1)  # what closing these leaves the book concentrated in
    event_risk: str = ""  # instruction inverts on this path; report, never compelled
    sizing_sanity: str = ""  # stood down on this path (nothing to size)
    overall: str = Field(min_length=1)  # final synthesis and why approved/refused


class ExitRiskVerdict(_PerSymbolRejections, LLMOutputModel):
    """The risk seat's verdict on the EXIT-REVIEW path.

    `RiskVerdict` minus the two levers that do nothing here. `modifications`
    and `scale_all_buys` are applied ONLY by `_apply_risk_modifications`,
    which is called ONLY from the morning `RiskStage`
    (`src/pipeline_stages.py`); `_risk_review_exits` returns a veto SET and
    reads neither. They were emitted, parsed, stored and discarded. A field
    the seat is asked to fill and no code consumes is not a harmless extra —
    it is an instruction to spend judgement on a lever that is not connected.

    Refusal is the whole lever here: `approved=False` refuses every exit in
    the batch (the book is what failed), `rejected_symbols` refuses one name
    and lets the rest through. Per doctrine a refusal DROPS the exit from the
    batch with a durable per-symbol reason; nothing on this path zeroes a
    target.
    """

    approved: bool
    reasoning_chain: ExitRiskReasoningChain
    # Same semantics as on `RiskVerdict`: each entry kills exactly one exit
    # and leaves the others standing. The `reason` is the per-symbol audit
    # trail written to `specialist_evidence`.
    rejected_symbols: list[SymbolRejection] = []
    reason_category: RiskReasonCategory = "clean"
    reasoning: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_rejected_symbols_field(values)
        return _normalize_enum_case_fields(values, lower_fields=("reason_category",))


class RiskVerdict(_PerSymbolRejections, LLMOutputModel):
    # The morning-plan risk verdict. Owner ruling 2026-09-24 (final): the seat
    # may NEVER cancel or reject the whole batch of new trades. `approved` is
    # kept in the schema for the audit trail and for backward-compatible
    # parsing, but `approved=False` is a NO-OP for batch rejection: `RiskStage`
    # records it in the durable trail and then proceeds. Every bit of real risk
    # reduction comes from the three levers below.
    approved: bool
    reasoning_chain: RiskReasoningChain
    modifications: list[RiskModification] = []
    # PER-SYMBOL refusal. Each entry drops exactly one NEW entry (BUY/SHORT)
    # and leaves every other decision standing; the survivors then go through
    # `modifications`, `scale_all_buys` and the deterministic hard-risk gate
    # unchanged. A SELL/COVER/HOLD named here is a protective exit / existing
    # holding and is NEVER dropped — `RiskStage` records the attempt and keeps
    # it (owner ruling 2026-09-24).
    #
    # The seat's THREE levers, narrowest first (there is no fourth — the
    # whole-batch veto was removed 2026-09-24):
    #   modifications    — retune one symbol's fields (size, stop, target)
    #   rejected_symbols — refuse one NEW entry outright, book unaffected
    #   scale_all_buys   — size the whole entry side down (to 0.0), refuse nothing
    #
    # An entry naming a symbol not in the plan is a no-op, logged.
    #
    # Default-empty by design — every historical verdict, and every verdict
    # from a model that never emits the field, replays with byte-identical
    # behaviour. Each entry carries its own `category` (and `event` when
    # event_risk) — see `EntrySymbolRejection`.
    rejected_symbols: list[EntrySymbolRejection] = []
    # Portfolio-level size control. Multiplies every BUY/SHORT decision's
    # allocation_pct after per-symbol modifications are applied. 1.0 = no change;
    # 0.5 = half all buys; 0.0 shrinks the entire new-entry side to zero (no new
    # buying) while leaving SELL/COVER/HOLD — every exit and existing holding —
    # untouched. This, not a veto, is how the seat stops new buying.
    scale_all_buys: float = Field(default=1.0, ge=0.0, le=1.0)
    # Categorized reason for any modification / scaling. The recent history of
    # this field is RENDERED INTO PM's sheet, which then asks PM to adjust its
    # own sizing when a tag repeats (`oversized` → smaller bases, `rr_fail` →
    # read TA's R/R more literally, etc.). Nothing here resizes anything: no
    # code reads this field and changes an allocation. One label per verdict.
    reason_category: RiskReasonCategory = "clean"
    reasoning: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        values = _normalize_rejected_symbols_field(values)
        return _normalize_enum_case_fields(values, lower_fields=("reason_category",))
