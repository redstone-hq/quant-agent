from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from src.quantities import collapse_stances
from src.models.base import (
    LLMOutputModel,
    _ALLOWED_SECTORS,
    _SECTOR_ALIASES,
    _normalize_enum_case_fields,
    normalize_sector_stance,
)
from src.models.analysis import (
    AnalystVerdict,
    NO_STATED_STRENGTH,
    Nomination,
    VerdictEvidence,
    _sanitize_nominations_field,
)


class MacroObservation(LLMOutputModel):
    indicator: str
    reading: str
    interpretation: str


class MacroSectorGuidance(LLMOutputModel):
    sector: Literal[
        "Technology",
        "Financial Services",
        "Healthcare",
        "Consumer Cyclical",
        "Consumer Defensive",
        "Energy",
        "Industrials",
        "Communication Services",
        "Utilities",
        "Basic Materials",
        "Real Estate",
        "Broad",
    ]
    stance: Literal["overweight", "neutral", "underweight"]
    reason: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # `sector` is canonicalized via _SECTOR_ALIASES in MacroAnalysis's
        # _sanitize_sector_guidance; only `stance` needs case-folding.
        return _normalize_enum_case_fields(values, lower_fields=("stance",))


class MacroPositionGuidance(LLMOutputModel):
    """Macro's read on which way the book should LEAN (net long vs net
    short, and why) — never how much of it sits idle.

    Owner mandate 2026-09-17: the desk is fully invested, always. The
    invested target is `src.risk.rules.DESK_INVESTED_TARGET_PCT`, a fixed
    mandate, not a macro output. `target_invested_pct` and
    `cash_recommendation_pct` were deleted from this model that day so no
    seat can be handed a macro reason to hold cash. Snapshots persisted
    before then still carry both keys; unknown keys are ignored, so they
    still parse and the numbers simply stop reaching anyone.
    """

    reasoning: str


class MacroReasoningChain(LLMOutputModel):
    """Six-step CoT, one field per step — forces the LLM to walk each stage.
    Every field has `min_length=1` so the LLM can't skip a step by sending
    `""`. Matches the discipline on the other CoT chains.
    """

    volatility_analysis: str = Field(min_length=1)  # VIX regime, trend, term structure if inferable
    yield_curve_analysis: str = Field(min_length=1)  # 2Y/10Y level, spread, inversion trajectory
    monetary_policy_analysis: str = Field(min_length=1)  # Fed funds (DFF) level + direction
    inflation_labor_credit: str = Field(min_length=1)  # CPI + UNRATE + HY OAS combined read
    cross_signal_synthesis: str = Field(min_length=1)  # How the above reinforce or contradict each other
    sector_implications: str = Field(min_length=1)  # What this means for sector tilts


class MacroAnalysis(LLMOutputModel):
    reasoning_chain: MacroReasoningChain
    regime: Literal["risk-on", "risk-off", "neutral", "transitional"]
    confidence: Literal["high", "medium", "low"]
    equity_outlook: Literal["bullish", "bearish", "neutral"]
    regime_shift: bool = False
    shift_reason: str = ""
    key_observations: list[MacroObservation] = []
    sector_guidance: list[MacroSectorGuidance] = []
    risk_factors: list[str] = []
    position_guidance: MacroPositionGuidance
    bull_triggers: list[str] = []
    bear_triggers: list[str] = []
    alignment_with_news: str = ""
    summary: str
    # Phase 9 (§9.1): sector leaders Macro wants Technical to look at when
    # a regime turns. Default [] so a MacroAnalysis persisted before this
    # field existed (macro_store snapshots, replayed decisions) still
    # parses unchanged. Bounded and deduped by the pipeline, not here —
    # see src/nominations.py.
    nominations: list[Nomination] = []

    # --- board item 119: how much of the set this verdict was formed on ----
    #
    # NOT model output. The economist is never asked to self-report its own
    # coverage — it would be grading its own inputs. These two fields are
    # stamped by the pipeline immediately after validation, from
    # `src.data.macro.MacroCoverage.verdict_stamp()`, which is the
    # deterministic fetch record.
    #
    # They default to "unknown"/"" so every MacroAnalysis written before this
    # existed (macro_store snapshots, replayed decisions, the offline
    # rehearsal fixtures) still parses. "unknown" means nobody stamped it,
    # which is deliberately NOT the same claim as "complete".
    #
    # Measured reason this is on the verdict rather than only on the run:
    # production paid the economist on a partial set twice (2026-09-17 8/15,
    # 2026-09-22 7/15 — `agent_logs`), and on the first of those the regime
    # came back `transitional` where the sessions either side of it said
    # `risk-on`. That verdict was then persisted, carried into the later
    # sessions and shown to the owner with nothing marking it as a read with
    # eight holes in it.
    coverage_state: Literal["complete", "partial", "failed", "unknown"] = "unknown"
    coverage_note: str = ""

    @property
    def partial_read(self) -> bool:
        """True when this verdict is known to have been formed on an
        incomplete series set. "unknown" is not partial and is not complete —
        an unstamped verdict makes no claim either way, so it must not be
        rendered as a caveat the fetch record does not support."""
        return self.coverage_state in ("partial", "failed")

    # DELETED 2026-09-13 (retired item 31): `_MAGNITUDE_BY_CONFIDENCE`
    # ({high 0.75, medium 0.5, low 0.25}) and `_REGIME_SHIFT_BONUS` (0.25).
    # The first was a table on `confidence`, which is the very field this
    # verdict hands to `conviction` — so `score_verdict`'s two-signal
    # composite was counting macro's confidence twice, at an unsourced
    # spacing. The second was an unsourced constant on top of it. Magnitude
    # is now `NO_STATED_STRENGTH` (None); `regime_shift`/`shift_reason`
    # still reach the reader through `invalidation` below, where they are the
    # analyst's own words rather than a number nobody derived.

    def to_verdict(self, symbol: str, *, sector: str | None = None) -> "AnalystVerdict":
        """This read, restated in the shared Phase 13 verdict shape.

        A RESTATEMENT, not a second opinion, mirroring
        `TechAnalysisResult.to_verdict` — see that method for the pattern
        this follows.

        `MacroAnalysis` is market-wide, not per-symbol (no `symbol` field
        on this model — see the evidence-registry seat-name comment
        threaded through `PortfolioManagerAgent.build_evidence_registry`,
        which already applies one macro read to many symbols). The caller
        supplies which symbol this verdict is being cast for, and — as of
        2026-09-13 — which SECTOR that symbol belongs to.

        direction    — `sector`'s own stance from `sector_guidance` when this
                       read stated one for it; otherwise `equity_outlook`.

                       **2026-09-13, retired item 31.** This used to be
                       `equity_outlook` for every symbol, while
                       `build_evidence_registry` — the SAME macro read, in
                       the same prompt — already resolved a per-symbol stance
                       from `sector_guidance` and only fell back to the broad
                       outlook when the sector had no row. So the desk could
                       tell the PM "macro is bearish energy" in the evidence
                       registry and simultaneously rank an energy name on a
                       bullish broad read. One belief, two answers.

                       The resolution here is deliberately the registry's
                       own, not a second rule: the matching rows' stances go
                       through the identical `collapse_stances` reduction
                       (`src/quantities.py`, the one definition both call),
                       and the result is translated tilt -> direction by
                       `normalize_sector_stance` — the module-level map that
                       exists precisely so overweight/underweight is not
                       respelled per consumer. An unresolved split across
                       rows ("mixed") normalizes to None and is treated as
                       NEUTRAL, matching the registry, where "mixed" fails
                       `stance_is_aligned` and supports nothing. Nothing is
                       invented and no number is introduced.

                       The objection this method used to record — that
                       `MacroAnalysis` "carries one conviction/evidence/
                       invalidation set for its whole read, so there is
                       nothing sector-specific to attach" — is only half
                       true, and the true half does not block this. Evidence
                       IS sector-specific: the matching row's own `reason`,
                       already on the model, is surfaced first and labelled
                       as the stance that decided the direction. Conviction
                       is not, so `confidence` is still used unchanged — it
                       is the only confidence this seat states, it is what
                       the broad read used too, and substituting a
                       sector-specific one would mean inventing it.
        conviction   — `confidence` verbatim; already high/medium/low.
        magnitude    — `NO_STATED_STRENGTH` (None), directional or not. See
                       that constant for why the previous confidence-keyed
                       table and regime-shift bonus were deleted rather than
                       re-derived, and why nothing was borrowed in their
                       place. `regime_shift` in particular changes nothing
                       here — `AnalystVerdict` refuses a neutral verdict with
                       nonzero magnitude anyway, and a "neutral, but
                       shifting" read is a contradiction in terms this
                       method does not try to resolve silently. This seat
                       reaches the ranking through its weighted conviction.
        evidence     — the deciding `sector_guidance` row first when a sector
                       stance was applied (labelled `sector_stance:<sector>`,
                       so a reader can see WHY this symbol's direction
                       differs from the broad one), then `key_observations`
                       (indicator/reading/interpretation, one VerdictEvidence
                       each), the full `sector_guidance` list (sector +
                       stance + reason), and `risk_factors` (one per item).
                       All qualitative (`text=`); MacroAnalysis carries no
                       evidence-shaped numbers to attach as `value=`.
        invalidation — `shift_reason` when `regime_shift` is True and the
                       reason is non-empty: the stated condition already IS
                       the falsifier for a regime call. Otherwise, mirroring
                       Technical's "fall back to the analyst's own stated
                       falsifier" pattern: for a bullish call, the first
                       `bear_triggers` entry (what would prove it wrong); for
                       a bearish call, the first `bull_triggers` entry.
                       UNLIKE Technical, when direction is not neutral and
                       none of the above is available (no shift_reason, no
                       trigger on the falsifying side), a generic fallback
                       string is used rather than leaving this blank —
                       `AnalystVerdict` REQUIRES a non-empty invalidation for
                       any non-neutral call (see its
                       `_a_call_must_be_falsifiable_and_backed` validator) and
                       raises `ValidationError` otherwise, so an empty string
                       is only ever valid here for a neutral outlook.
        """
        # The sector rows this read stated for the symbol's own sector, if
        # the caller supplied one. Matched on the canonical `sector` Literal
        # (`_sanitize_sector_guidance` has already aliased it), case- and
        # whitespace-insensitively, the same way `build_evidence_registry`
        # keys its own lookup.
        wanted = (sector or "").strip().lower()
        sector_rows = [row for row in self.sector_guidance if wanted and row.sector.strip().lower() == wanted]
        sector_direction = (
            normalize_sector_stance(collapse_stances(row.stance for row in sector_rows)) if sector_rows else None
        )
        if sector_rows and sector_direction is None:
            # `collapse_stances` returned "mixed" — the sector's own rows
            # disagree. The registry treats that as supporting nothing, so
            # this does too, rather than quietly falling back to the broad
            # read the sector rows were specifically contradicting.
            sector_direction = "neutral"

        direction = sector_direction or self.equity_outlook
        # Absent either way — None, not 0.0. See `NO_STATED_STRENGTH`.
        magnitude = NO_STATED_STRENGTH

        evidence: list[VerdictEvidence] = []
        for row in sector_rows:
            evidence.append(
                VerdictEvidence(
                    label=f"sector_stance:{row.sector}",
                    text=(
                        f"{row.stance} — {row.reason} (this sector stance sets "
                        f"{symbol}'s macro direction; broad equity_outlook is "
                        f"{self.equity_outlook})"
                    ),
                )
            )
        for obs in self.key_observations:
            evidence.append(
                VerdictEvidence(
                    label=obs.indicator,
                    text=f"{obs.reading} — {obs.interpretation}",
                )
            )
        for row in self.sector_guidance:
            evidence.append(
                VerdictEvidence(
                    label=f"sector:{row.sector}",
                    text=f"{row.stance} — {row.reason}",
                )
            )
        for i, factor in enumerate(self.risk_factors):
            evidence.append(VerdictEvidence(label=f"risk_factor_{i}", text=factor))
        if not evidence and direction != "neutral":
            # `key_observations`/`sector_guidance`/`risk_factors` are all
            # `= []` defaults — an actionable read that populated none of
            # them still has to satisfy `AnalystVerdict`'s "a directional
            # call must cite at least one piece of evidence" rule. The
            # reasoning chain is mandatory (`min_length=1` on every field),
            # so it is always available as a last-resort citation — nothing
            # is invented, this is the analyst's own synthesis restated.
            evidence.append(
                VerdictEvidence(
                    label="cross_signal_synthesis",
                    text=self.reasoning_chain.cross_signal_synthesis,
                )
            )

        invalidation = ""
        if direction != "neutral":
            shift_reason = (self.shift_reason or "").strip()
            if self.regime_shift and shift_reason:
                invalidation = shift_reason
            elif direction == "bullish" and self.bear_triggers:
                invalidation = self.bear_triggers[0]
            elif direction == "bearish" and self.bull_triggers:
                invalidation = self.bull_triggers[0]
            else:
                # No stated falsifier anywhere on the read. Technical can
                # fall back to its own hard stop; macro has no analogous
                # always-present number, so the fallback is a generic but
                # honest statement rather than a blank field that would
                # fail `AnalystVerdict`'s non-neutral-invalidation rule.
                invalidation = f"equity_outlook reverses from {direction} (macro analyst stated no explicit trigger)"

        return AnalystVerdict(
            seat="macro",
            symbol=symbol,
            direction=direction,
            magnitude=magnitude,
            conviction=self.confidence,
            evidence=evidence,
            invalidation=invalidation,
        )

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # Three top-level enums on MacroAnalysis are LLM-emitted lowercase.
        # Runs before _sanitize_sector_guidance and the Literal check.
        return _normalize_enum_case_fields(
            values,
            lower_fields=("regime", "confidence", "equity_outlook"),
        )

    @model_validator(mode="before")
    @classmethod
    def _sanitize_nominations(cls, values):
        return _sanitize_nominations_field(values)

    @model_validator(mode="before")
    @classmethod
    def _sanitize_sector_guidance(cls, values):
        """Map aliases, drop unknown sectors — preserves the rest of the analysis.

        Previously a single bad sector name (e.g. "Financials" instead of
        "Financial Services") rejected the whole MacroAnalysis and left PM blind.
        """
        if not isinstance(values, dict):
            return values
        sg = values.get("sector_guidance")
        # MacroStore persists {sector: bullish|neutral|bearish}. Live LLM
        # output is a list of {sector, stance, reason}. A dict used to be
        # left untouched, then MacroAnalysis.model_validate raised
        # (measured 2026-09-16, two intra ticks). Coerce the dict to the
        # list shape here — mechanical, no invented reasons — so a stored
        # snapshot and a live dict-shaped answer both parse. Missing
        # reasoning_chain is still a ValidationError: we do not invent it.
        if isinstance(sg, dict):
            converted: list[dict] = []
            reverse = {
                "bullish": "overweight",
                "bearish": "underweight",
                "neutral": "neutral",
            }
            for sector, direction in sg.items():
                stance = reverse.get(str(direction or "").strip().lower())
                if stance is None:
                    stance = str(direction or "").strip().lower()
                converted.append(
                    {
                        "sector": sector,
                        "stance": stance,
                        "reason": "",
                    }
                )
            values = dict(values)
            values["sector_guidance"] = converted
            sg = converted
        if not isinstance(sg, list):
            return values
        cleaned: list[dict] = []
        for item in sg:
            if not isinstance(item, dict):
                continue
            sec = item.get("sector")
            if not isinstance(sec, str):
                continue
            canon = _SECTOR_ALIASES.get(sec.strip().lower(), sec.strip())
            if canon in _ALLOWED_SECTORS:
                new_item = dict(item)
                new_item["sector"] = canon
                cleaned.append(new_item)
            # else: silently drop — we'd rather lose one guidance row than the whole analysis
        values["sector_guidance"] = cleaned
        return values


class _AsWireRows:
    """Annotated marker: validate the stored map, but publish `list[row_model]`
    as the JSON schema (generated by pydantic through the shared handler, so
    `$defs` stay consistent). A strict schema cannot express a keyed map."""

    def __init__(self, row_model: type):
        self.row_model = row_model

    def __get_pydantic_json_schema__(self, core_schema, handler):
        return handler(TypeAdapter(list[self.row_model]).core_schema)


def _rows_to_map(raw, key_name: str, value_key: str):
    """Rows `[{<key_name>, <value_key>}]` -> the `{key: value}` map every
    downstream reader and stored report uses. A map passes through unchanged;
    anything else is left for pydantic to reject with its own error. Rows
    sharing a key merge (lists extend)."""
    if not isinstance(raw, list):
        return raw
    out: dict = {}
    for row in raw:
        if isinstance(row, BaseModel):
            row = row.model_dump()
        if not isinstance(row, dict) or key_name not in row:
            return raw
        value = row.get(value_key)
        if isinstance(value, list):
            out.setdefault(row[key_name], []).extend(value)
        else:
            out[row[key_name]] = value
    return out


class TrackerRow(LLMOutputModel):
    """Wire shape of one `key_state_tracker` entry."""

    topic: str
    state: str


class MacroNarrative(LLMOutputModel):
    last_updated: str
    era_themes: list[str] = Field(min_length=1)
    current_regime: str = Field(min_length=5)
    key_state_tracker: Annotated[dict[str, str], _AsWireRows(TrackerRow)] = {}

    @model_validator(mode="before")
    @classmethod
    def _tracker_rows_to_map(cls, values):
        if isinstance(values, dict) and "key_state_tracker" in values:
            values = {**values, "key_state_tracker": _rows_to_map(values["key_state_tracker"], "topic", "state")}
        return values

    @field_validator("last_updated")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        date.fromisoformat(v)
        return v
