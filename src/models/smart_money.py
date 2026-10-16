from datetime import datetime, date
from typing import Annotated, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from src.models.base import LLMOutputModel, _normalize_symbol
from src.models.analysis import AnalystVerdict, NO_STATED_STRENGTH, VerdictEvidence


class InsiderPurchaseCluster(BaseModel):
    """Two or more distinct insiders buying the same stock on the same day.

    Computed in Python by
    `src.data.smart_money_cluster.insider_purchase_clusters` from SEC Form 4
    rows only; never parsed from a model response. The definition is the one
    Alldredge & Blank measure (J. Financial Research 42(2), 2019; SSRN
    2781761): a purchase "on the same day as another insider purchase at the
    same company". Members are restricted to open-market purchases (Form 4
    code P) that the routine test in `src.data.insider_signal` classified
    OPPORTUNISTIC, because Cohen, Malloy & Pomorski (NBER w16454) find routine
    trades earn "essentially zero" abnormal return.

    `filing_age_days` is today minus the latest member filing's disclosure
    date — how long ago the cluster became fully knowable. It changes daily
    and is therefore excluded from the synthesis evidence hash.
    """

    transaction_date: date
    distinct_insiders: int = Field(ge=2)
    insider_ciks: list[str] = Field(min_length=2)
    combined_value_usd: float = Field(ge=0)
    latest_disclosure_date: date
    filing_age_days: int = Field(ge=0)


class SmartMoneyObservation(LLMOutputModel):
    """Source-backed smart-money fact; timestamps and amounts are source facts.

    Congressional fields remain optional-compatible with records already stored
    by the first provider.  SEC Form 4 observations add accession-level
    provenance and never rely on the LLM to classify a transaction code.
    """

    symbol: str
    stream: Literal["congressional", "insider"] = "congressional"
    actor: str = Field(min_length=1)
    actor_cik: str = ""
    actor_roles: list[str] = []
    joint_owner_ciks: list[str] = []
    direction: Literal["buy", "sell", "exchange", "unknown"]
    amount_range: str = ""
    transaction_date: date
    disclosure_date: date
    accepted_at: datetime | None = None
    known_at: datetime | None = None
    source_url: str = Field(min_length=1)
    accession_number: str = ""
    filing_form: Literal["", "4", "4/A"] = ""
    transaction_code: Literal["", "P", "S"] = ""
    transaction_row: int | None = Field(default=None, ge=0)
    security_title: str = ""
    shares: float | None = Field(default=None, ge=0)
    price_per_share: float | None = Field(default=None, ge=0)
    transaction_value_usd: float | None = Field(default=None, ge=0)
    post_transaction_shares: float | None = Field(default=None, ge=0)
    ownership_nature: Literal["", "direct", "indirect", "unknown"] = ""
    amendment: bool = False
    late_filing: bool = False
    is_10b5_1: bool | None = None
    listed_exchange: str = ""
    in_core_universe: bool = False
    in_trading_universe: bool = False
    admission_eligible: bool = False
    transient_admission_eligible: bool = False
    transient_admitted: bool = False
    lag_days: int = Field(ge=0)
    disclosure_age_days: int = Field(ge=0)
    freshness: Literal["fresh", "delayed", "stale"]
    # Routine/opportunistic verdict from src.data.insider_signal. Defaults are
    # empty so rows cached before the classifier existed still validate; they
    # are populated deterministically on every ``fetch``.
    signal_class: Literal["", "opportunistic", "routine", "indeterminate"] = ""
    signal_class_reason: str = ""
    signal_class_detail: str = ""
    # Trade size relative to what the insider already held, reconstructed from
    # the Form 4 fields the desk already parses (`shares` and
    # `post_transaction_shares`) — see
    # `src/data/insider_signal.py::holdings_fraction`. Reported for buys and
    # sells alike and never used as an admission cutoff. Only the exact ratio
    # is shown; `None` when the filing does not carry enough to compute one,
    # and `no_prior_holding` True for a purchase by an insider who held
    # nothing beforehand (no ratio exists — a distinct fact, not a missing one).
    holdings_fraction: float | None = Field(default=None, ge=0.0)
    no_prior_holding: bool = False
    economic_role: Literal["actionable", "confirmatory", "contradictory", "historical"]
    # Populated only for stream="congressional" (src/data/congressional_trading.py).
    # Two independent free sources (kadoa-org/congress-trading-monitor,
    # congresswatch.us) are merged there; when both carry a record for what
    # looks like the same real trade, "agreement" means they matched on
    # direction and overlapping amount bracket, "discrepancy" means they did
    # not (the disagreement is never silently resolved — see
    # `cross_source_note`), and "single_source" means only one source had
    # the trade. Defaults to "" for every SEC Form 4 row and for any
    # congressional row cached before this field existed.
    cross_source_agreement: Literal["", "single_source", "agreement", "discrepancy"] = ""
    cross_source_note: str = ""
    # True only for a congresswatch.us row: that feed carries no filing-date
    # field at all, so `CongressionalTradingProvider._normalize_congresswatch`
    # estimates one at the STOCK Act's 45-day ceiling. That estimate is a
    # guess about WHEN we could have learned of the trade, not a measurement,
    # so it must never be read as evidence the disclosure was timely — see
    # `SmartMoneyFinding.deterministic_eligibility`'s lag_days check, which
    # treats an estimated date as failing the freshness gate outright rather
    # than as satisfying it by construction. Always False for kadoa and for
    # every SEC Form 4 (stream="insider") row.
    disclosure_date_estimated: bool = False
    # The most recent same-day opportunistic insider purchase cluster in this
    # row's symbol, stamped by `SECForm4Provider.fetch` on every insider row
    # of a CONFIGURED-UNIVERSE symbol (never on a non-universe or
    # congressional row). A per-symbol fact carried on each row so it
    # survives whichever rows the materiality filter and the observation cap
    # keep. Its only effect is `SmartMoneyFinding.to_verdict`'s
    # medium -> high conviction lift; it never changes sorting or admission.
    purchase_cluster: InsiderPurchaseCluster | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="after")
    def normalize_form4_aliases(self):
        if self.stream == "insider":
            if self.known_at is None:
                self.known_at = self.accepted_at
            if self.accepted_at is None:
                self.accepted_at = self.known_at
            if self.admission_eligible or self.transient_admission_eligible:
                eligible = (
                    self.direction == "buy"
                    and self.transaction_code == "P"
                    # A routine purchase carries no predictive power, so it can
                    # never be the reason a symbol is admitted to the trading
                    # surface. This only ever narrows admission.
                    and self.signal_class != "routine"
                )
                self.admission_eligible = eligible
                self.transient_admission_eligible = eligible
        return self

    @property
    def signal_direction(self) -> int:
        """Sign of this row's directional view: +1 bullish, -1 bearish, 0 none.

        The class rank CANNOT carry a sign, so on its own a large insider SALE and a large
        insider BUY of the same dollar value rank and size identically (board
        item 63). This derived channel supplies the missing sign so the
        deterministic ranking in ``src/agents/smart_money_analyst.py`` can
        multiply ``value`` by it and never let a contra signal
        rank or size as if it were bullish. It is computed from ``direction``,
        never stored -- nothing to keep that code cannot recompute.

        A BUY (open-market purchase) is unambiguously bullish -> +1; its
        effective sign is unchanged from before this channel existed, so every
        currently-admitted row keeps the exact ranking contribution it had.

        A SALE is NOT counted as bullish (that identity WAS the bug) but is
        also NOT signed bearish here: one Form 4 row does not establish a
        directional view, and no published signed scoring scheme defines a
        -1-vs-0 boundary for that per-filing quantity (board item 63). The
        exact holdings ratio is descriptive only and may not be fitted to
        desk outcomes. The
        desk is long-only on smart-money admission (a row is admission-eligible
        only when ``direction == "buy"``), so neutralising a sale to 0 -- rather
        than guessing a bearish magnitude -- is the safe minimal structure fix:
        a sale never inflates a bullish ranking. ``exchange``/``unknown`` state
        no directional view -> 0.
        """
        return 1 if self.direction == "buy" else 0


#: `SmartMoneyFinding.economic_role` -> `AnalystVerdict.conviction`. NEW
#: JUDGMENT, not a restatement (the finding carries no confidence field to
#: read off). The ordering is not invented here: `_ROLE_TIERS` in
#: `src/agents/smart_money_analyst.py` already ranks these four labels
#: actionable(3) > confirmatory(2) > contradictory(1) > historical(0) to
#: decide which fact the seat surfaces first, and the seat's own prompt
#: explains WHY — "actionable" is present-tense, source-backed trading
#: evidence; "confirmatory" is thematic context (congressional/13F, filed
#: up to 45 days late); "historical" is stale and cannot support a target
#: (`build_evidence_registry`/`stance_is_aligned` refuse it); "contradictory"
#: describes a fact's RELATIONSHIP to the current thesis, not its own
#: strength, so folding it in at the bottom alongside "historical" is the
#: more conservative of two readings, not the only defensible one. Squeezed
#: onto the desk's 3-rung high/medium/low scale, contradictory and
#: historical collapse to the same "low" rung. Flag for review.
_SMART_MONEY_ROLE_CONVICTION: dict[str, str] = {
    "actionable": "high",
    "confirmatory": "medium",
    "contradictory": "low",
    "historical": "low",
}

#: DELETED 2026-09-13 (retired item 31): `_SMART_MONEY_ROLE_MAGNITUDE`,
#: an unsourced {actionable 1.0, confirmatory 0.6, contradictory 0.3,
#: historical 0.3} table keyed on the SAME `economic_role` that
#: `_SMART_MONEY_ROLE_CONVICTION` above is keyed on. Both halves of
#: `score_verdict` therefore read one categorical label, so the composite
#: counted it twice and an "actionable" finding alone scored the maximum.
#: Magnitude is now `NO_STATED_STRENGTH` (None — this seat has no strength
#: scale of its own, and does not borrow one); the role still sets
#: conviction, which is the one place it has a derivation behind it.


def _purchase_cluster_lift(
    conviction: str,
    direction: str,
    cluster: "InsiderPurchaseCluster | None",
) -> str:
    """Owner ask 2026-09-19 (board item 124): a confirmed same-day
    opportunistic insider purchase cluster lifts this seat's conviction from
    "medium" to "high" on a bullish read, and does nothing else.

    Why only buying and only one rung: Alldredge & Blank (2019) report
    abnormal returns after CLUSTERED PURCHASES; nothing in that source is
    about sales, and a purchase cluster says nothing in favour of a bearish
    call. The lift is capped at the existing high rung — this seat's
    conviction is a three-rung label, and a cluster cannot create a rung the
    table does not have, nor rescue a "low" (contradictory/historical) read,
    which the table puts there for reasons the cluster does not address.
    """
    if cluster is not None and direction == "bullish" and conviction == "medium":
        return "high"
    return conviction


class SmartMoneyFinding(LLMOutputModel):
    symbol: str
    stance: Literal["bullish", "bearish", "neutral", "mixed"]
    economic_role: Literal["actionable", "confirmatory", "contradictory", "historical"]
    summary: str = Field(min_length=1)
    why_now: str = Field(min_length=1)
    # Item 99: the smart-money seat's own falsifier, in its own words,
    # written at call time. Same field name and same plain-string shape
    # the technical seat uses, so `exit_guard.check_thesis_invalid_if`
    # reads it unchanged. Not required: a seat that gives none leaves it
    # empty and the gap is recorded as a gap. A `neutral` stance is the
    # absence of a call, so it carries nothing to disprove — see
    # `clear_falsifier_on_neutral` below.
    thesis_invalid_if: str = ""
    # All four are `SkipJsonSchema`: the desk fills every one of them itself,
    # and before this they were REQUIRED output under strict structured
    # output. `SmartMoneyAnalystAgent._parse_findings` overwrites
    # `observations` with `[o.model_dump() for o in source_rows]` and
    # `evidence_hash` with the desk's own digest on every single finding,
    # cached or live, and `deterministic_eligibility` below recomputes both
    # eligibility booleans from the source rows. So the seat was spending its
    # output budget re-typing a 45-field internal row (accession_number,
    # transaction_row, freshness, admission_eligible,
    # transient_admitted, in_core_universe, lag_days, ...) at least once per
    # finding, plus a sha256 it cannot know, and 100% of it was discarded
    # before the object was constructed. Nothing validates the echo against
    # the source rows, so it was never a grounding device either.
    observations: Annotated[list[SmartMoneyObservation], SkipJsonSchema()] = Field(min_length=1)
    support_eligible: Annotated[bool, SkipJsonSchema()] = False
    transient_admission_eligible: Annotated[bool, SkipJsonSchema()] = False
    evidence_hash: Annotated[str, SkipJsonSchema()] = ""

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("thesis_invalid_if")
    @classmethod
    def strip_falsifier(cls, value: str) -> str:
        return (value or "").strip()

    @model_validator(mode="after")
    def clear_falsifier_on_neutral(self):
        # Same rule the technical sheet and `TechAnalysisResult` already
        # hold: a neutral is the absence of a call, so the falsifier slot
        # must be empty. This only REMOVES text the seat should not have
        # written; it never writes any. Findings parse per-entry in
        # isolation, so raising here would discard an otherwise good
        # neutral finding for a cosmetic breach.
        if self.stance == "neutral" and self.thesis_invalid_if:
            object.__setattr__(self, "thesis_invalid_if", "")
        return self

    @model_validator(mode="after")
    def deterministic_eligibility(self):
        streams = {o.stream for o in self.observations}
        directional = {o.direction for o in self.observations if o.direction in {"buy", "sell"}}
        # 2026-09-11, owner redesign: a calendar-age cutoff on WHETHER this
        # evidence can support a thesis is gone. Owner's framing, direct:
        # "this is one piece of information — if it doesn't correlate with
        # anything else, that's fine, it just changes the decision matrix;
        # if it does correlate, stronger weights." An insider trade is a
        # fact that happened; it does not expire on a clock. Real research
        # backs treating it this way rather than a short fixed window:
        # Seyhun (1986) found only ~1/4 of the eventual abnormal return from
        # an insider purchase realizes in the first 5 days and ~1/2 is still
        # unrealized after a full month; pre-announcement run-ups in real
        # M&A data are documented starting MONTHS before the news breaks.
        # A 7-day cutoff was never grounded in either fact.
        #
        # What decides whether this can actually support a target now is
        # CORRELATION with other CURRENT evidence, checked where that
        # evidence is actually visible together —
        # `PortfolioManagerAgent`'s grounding validator, which requires at
        # least one other live source (technical/news/earnings/macro) to
        # currently agree with this finding's direction before smart_money
        # may be marked `supports` rather than `context`. This model only
        # still enforces STRUCTURAL validity — is this real, single-
        # direction, legally-disclosed evidence at all — never how old it
        # is.
        if streams == {"congressional"}:
            # Preserve the original conservative congressional contract.
            # lag_days cap raised 30 -> 45: the STOCK Act's own legal filing
            # deadline is 45 days after the transaction, so a lag of up to
            # 45 days is a legally on-time disclosure, not a stale one. This
            # is about LEGAL disclosure timing (when we were allowed to
            # learn about the trade), not the trade's own informational
            # age — a different question, kept.
            actors = {o.actor.strip().casefold() for o in self.observations}
            # `lag_days <= 45` on its own cannot fail for a congresswatch.us
            # row: that source has no real filing date, so the provider
            # estimates one at exactly the 45-day ceiling this check applies
            # (see `disclosure_date_estimated`'s docstring). An estimate is
            # not a measurement of timeliness, so it must not be allowed to
            # satisfy this gate — an estimated-date observation always fails
            # it here, regardless of its lag_days value, the same as any
            # other observation whose disclosure timing is unverified.
            self.support_eligible = (
                len(self.observations) >= 2
                and len(actors) >= 2
                and len(directional) == 1
                and all(o.lag_days <= 45 and not o.disclosure_date_estimated for o in self.observations)
            )
            # Owner ruling 2026-09-19 (docs/INCIDENT_HISTORY.md, 2026-09-20
            # entry): congressional disclosures are evidence and must never
            # be zeroed out, but their ceiling is "confirmatory" — they may
            # raise a thesis's conviction, never alone reach "actionable"
            # present-tense trading evidence. A same-day cluster of several
            # members (the >=2-actor gate above) therefore lifts conviction
            # by AT MOST ONE step, historical/low -> confirmatory/medium; it
            # is capped here, not scaled by how many members clustered, so a
            # 2-member and a 10-member cluster land on the same rung.
            if not self.support_eligible:
                self.economic_role = "historical"
            elif self.economic_role == "actionable":
                self.economic_role = "confirmatory"
            self.transient_admission_eligible = False
            return self

        # SEC observations have already passed the provider's deterministic
        # materiality/cluster filter. Real, one-direction evidence may
        # support PM provenance (subject to the correlation check above).
        # Only an explicit open-market purchase can enter the separately
        # governed transient-candidate lane, which keeps its own,
        # deliberately stricter freshness bar (`admission_eligible` in
        # `src/data/smart_money.py`) — a single stale signal must never
        # alone justify pulling a brand-new symbol into the universe, which
        # is a different risk than confirming a thesis on a symbol already
        # in play.
        self.support_eligible = bool(directional) and len(directional) == 1
        self.transient_admission_eligible = any(
            o.transient_admission_eligible and o.transaction_code == "P" and o.direction == "buy"
            for o in self.observations
        )
        # Same honesty rule as the congressional branch above: the model's
        # own economic_role self-report must not survive a deterministic
        # "too thin" verdict. Without this, an SEC/insider finding whose
        # only observation is stale (or whose directions don't agree) could
        # still claim "actionable" and reach the PM at full magnitude/high
        # conviction via to_verdict() while support_eligible is False.
        if not self.support_eligible:
            self.economic_role = "historical"
        return self

    def purchase_cluster(self) -> "InsiderPurchaseCluster | None":
        """The deterministic purchase cluster stamped on this finding's
        insider rows, or None. Congressional rows never carry one."""
        stamped = [
            o.purchase_cluster for o in self.observations if o.stream == "insider" and o.purchase_cluster is not None
        ]
        if not stamped:
            return None
        return max(stamped, key=lambda c: c.transaction_date)

    def to_verdict(self) -> "AnalystVerdict":
        """This finding, restated in the shared Phase 13 verdict shape.

        UNLIKE `TechAnalysisResult.to_verdict`, this is only a PARTIAL
        restatement — see `_SMART_MONEY_ROLE_CONVICTION` above for the one
        field that is new judgment rather than a value already sitting on
        this model. (There used to be a second, `_SMART_MONEY_ROLE_MAGNITUDE`;
        it was deleted on review — see the note where it stood.)

        direction    — `stance`, with "mixed" folded into "neutral". This
                       is a restatement of existing desk convention, not a
                       new call: `PortfolioManagerAgent._collapse_stances`
                       and `_stance_matches_source`
                       (src/agents/portfolio_manager.py,
                       src/agents/smart_money_analyst.py) already treat
                       "mixed" and "neutral" as the same non-directional
                       bucket — conflicting buy/sell activity supports
                       neither a bullish nor a bearish call.
        magnitude    — `NO_STATED_STRENGTH` (None), directional or not. This
                       seat states no strength independent of
                       `economic_role`, and `economic_role` already drives
                       conviction, so a magnitude derived from it would be
                       the same signal counted twice — and a magnitude
                       borrowed off Technical's rungs would be a number this
                       seat has no scale for. The seat still reaches the
                       ranking through its weighted conviction; see
                       `NO_STATED_STRENGTH` and `src/verdicts.py`.
        conviction   — `_SMART_MONEY_ROLE_CONVICTION[economic_role]`. New
                       judgment. Then ONE deterministic lift, owner ask
                       2026-09-19 (board item 124): a bullish finding whose
                       insider rows carry a confirmed same-day opportunistic
                       purchase cluster (`InsiderPurchaseCluster`) is lifted
                       from "medium" to "high". Nothing else moves: "low"
                       stays low, "high" cannot go higher, a bearish or
                       neutral read is untouched, and the model has no say —
                       it may mention the cluster, the code sets the rung.
                       See `_purchase_cluster_lift`.
        evidence     — `summary` and `why_now`, each as one labelled item
                       when present, plus up to 5 observations (most recent
                       transaction_date first) summarized as text.
        invalidation — SmartMoneyFinding has no invalidation-style field to
                       restate. Left "" for a neutral verdict (allowed).
                       For a directional verdict the base model REQUIRES a
                       non-empty invalidation, so one is constructed from
                       `why_now`, framed as a condition: the call stands
                       only while that stated reasoning holds. This is
                       genuinely invented, not read off the model — flagged
                       for review, not presented as a restatement.
        """
        stance = "neutral" if self.stance in ("neutral", "mixed") else self.stance
        # 0.0 either way — a neutral read has no lean, and a directional
        # read from this seat states no distance, and an absent distance is
        # None, never 0.0. See `NO_STATED_STRENGTH`.
        magnitude = NO_STATED_STRENGTH
        conviction = _SMART_MONEY_ROLE_CONVICTION[self.economic_role]
        cluster = self.purchase_cluster()
        conviction = _purchase_cluster_lift(conviction, stance, cluster)

        evidence: list[VerdictEvidence] = []
        if self.summary.strip():
            evidence.append(VerdictEvidence(label="summary", text=self.summary.strip()))
        if self.why_now.strip():
            evidence.append(VerdictEvidence(label="why_now", text=self.why_now.strip()))
        if cluster is not None:
            evidence.append(
                VerdictEvidence(
                    label="insider_purchase_cluster",
                    value=cluster.combined_value_usd,
                    as_of=cluster.transaction_date,
                    text=(
                        f"{cluster.distinct_insiders} distinct insiders made "
                        "opportunistic open-market purchases on "
                        f"{cluster.transaction_date.isoformat()}, combined "
                        f"${cluster.combined_value_usd:,.0f}; latest filing "
                        f"{cluster.filing_age_days} days old"
                    ),
                )
            )
        most_recent = sorted(
            self.observations,
            key=lambda o: o.transaction_date,
            reverse=True,
        )[:5]
        for obs in most_recent:
            detail = f"{obs.actor}: {obs.direction}"
            if obs.amount_range:
                detail += f" {obs.amount_range}"
            detail += f" on {obs.transaction_date.isoformat()}"
            evidence.append(VerdictEvidence(label="observation", text=detail))

        invalidation = ""
        if stance != "neutral":
            invalidation = f"the why-now premise no longer holds: {self.why_now.strip()}"

        return AnalystVerdict(
            seat="smart_money",
            symbol=self.symbol,
            direction=stance,
            magnitude=magnitude,
            conviction=conviction,
            evidence=evidence,
            invalidation=invalidation,
        )


class SmartMoneyNoRead(LLMOutputModel):
    """The seat's explicit "no read" on one presented symbol.

    Every presented symbol must be answered exactly once: a finding or this.
    It is deliberately NOT a SmartMoneyFinding and carries no stance, so it
    can never become a verdict, a ranking input or an evidence-registry row;
    it only proves the symbol was read and left without a call.
    """

    symbol: str
    reason: str = ""


class SmartMoneySynthesis(LLMOutputModel):
    """Top-level shape of the smart money analyst's response: a `findings`
    list plus a `no_reads` list, together covering every presented symbol
    exactly once. Exists ONLY to declare the OpenRouter response_format
    schema (src/agents/base.py) — the agent still reads `parsed["findings"]`
    and validates each entry as SmartMoneyFinding directly, per-entry
    isolated (src/agents/smart_money_analyst.py._parse_findings); this model
    is never itself constructed from a response."""

    findings: list[SmartMoneyFinding]
    no_reads: list[SmartMoneyNoRead] = Field(default_factory=list)
