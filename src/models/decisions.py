from typing import Literal
from pydantic import ConfigDict, Field, computed_field, field_validator, model_validator
from src.models.base import LLMOutputModel, _normalize_enum_case_fields, _normalize_symbol, reward_to_risk


class TradeDecision(LLMOutputModel):
    model_config = ConfigDict(validate_assignment=True)

    # Stage 3 (shorts): SHORT opens/adds a short (mirror of BUY); COVER
    # reduces/closes one (mirror of SELL). SELL never means "open a short"
    # — every existing consumer already reads SELL as "reduce or close a
    # long", and overloading it would silently reinterpret them.
    action: Literal["BUY", "SELL", "SHORT", "COVER", "HOLD"]
    symbol: str
    allocation_pct: float = Field(ge=0, le=100)
    entry_price: float
    stop_loss: float
    # None = NO take-profit (owner rule 2026-10-09): the chart was measured
    # and holds no structural level, so the trade runs on its ATR stop as a
    # trend trade. A missing number is never invented; when one IS present
    # the side and sign checks below still apply to it.
    take_profit: float | None
    reasoning: str
    # --- Conviction ledger (QAMC remediation spec §7.2) --------------------
    # Pinned at ENTRY (BUY/SHORT) only, mirroring how `expected_horizon_
    # sessions`/`setup_type` are pinned at entry rather than recomputed
    # later. All three default to None so every pre-existing construction
    # site (HOLD/_build_sell/_build_cover, tests, replay.py, ops/model_
    # policy/scenarios.py) is unaffected.
    #
    # `conviction` mirrors TargetPosition.conviction — the PM's own label
    # for the idea, carried through the constructor unchanged.
    conviction: Literal["high", "medium", "low"] | None = None
    # `requested_risk_pct` is TargetPosition.risk_allocation_pct AS THE PM
    # ASKED FOR IT — before the constructor's single-name clamp or the
    # portfolio/cluster budget rationing touch it. None for a legacy
    # notional (target_weight_pct-only) target, which asked for no risk
    # figure at all.
    requested_risk_pct: float | None = None
    # `allocated_risk_pct` is `RiskPlan.risk_pct` — "what the budget
    # actually granted" per that dataclass's own docstring, i.e. the
    # PRE-clamp request rationed through `allocate_risk_budget` and the
    # single-name ceiling in `_plan_risk_targets`. This is the closest
    # cheaply-available approximation of "what was really used to size the
    # order" — a further downstream notional clamp in `_build_buy`/
    # `_build_short` (the single-name weight ceiling) can
    # still shrink the FINAL position below what this figure implies; that
    # last clamp is not re-derived into a third number here. None when no
    # risk-based plan exists for this symbol (legacy notional target).
    allocated_risk_pct: float | None = None
    # --- Which rule placed the shipping stop (2026-09-02) ----------------
    # One of the `STOP_RULE_*` codes in `src/portfolio_constructor.py`, or
    # None when nothing resolved a stop (SELL/COVER/HOLD, legacy callers,
    # tests). Set by the constructor at the moment the order is built.
    #
    # It exists so the EXECUTION stage can tell a stop that sits at a
    # computed structural level from one the constructor merely accepted.
    # `src/pipeline_stages.py` carries a second, execution-time 1x ATR stop
    # floor, and without this field that floor re-widened level-backed
    # stops the constructor had deliberately honoured under §12.1 — undoing
    # the fix one stage later, against an ATR recomputed from different
    # bars. Recomputing level-backing there instead would have created
    # exactly the second data path §12.1 was careful not to build.
    stop_rule: str | None = None
    # --- Item 55 RECORDING: what the stop was BASED on (2026-10-01) ------
    # A JSON record, written at entry and read by nothing in the decision
    # path, describing the structural level standing behind `stop_loss`:
    # its price, how many times price turned there, how many bars confirm a
    # swing point, how wide its zone was and how far the stop sat from it —
    # or `level_backed: false` when no computed level stood behind it, which
    # is the control the question needs. Produced by
    # `PortfolioConstructor.shipped_stop_level_basis`, stored on the
    # `trades` row, and governed by the FALSIFICATION-ONLY limit in
    # `src.data.levels.describe_stop_level_basis`: it may show the current
    # definition of a level is wrong and may NEVER be swept for a better bar
    # count or zone width. Changes no behaviour whatsoever.
    stop_level_basis: str | None = None
    # --- The sub-floor catalyst exception, carried to execution (2026-09-11)
    # True when this order was permitted BELOW `min_reward_risk_after_
    # widening` because the PM's sub-floor catalyst gate verified its
    # citation against a real dated state-change row and capped it at
    # starter size. Mirrors `stop_rule` directly above in both purpose and
    # mechanism: a fact the constructor knows and the execution stage cannot
    # re-derive without building a second copy of the gate.
    #
    # `src/pipeline_stages.py` carries a SECOND, execution-time reward:risk
    # belt (a flat 1.2) that only fires when execution changed the geometry
    # the Risk Manager audited. Without this field that belt killed every
    # such order the moment anything drifted — not because execution had
    # degraded the trade, but because the trade was deliberately below 1.2
    # to begin with, which is the condition the exception exists to permit.
    # docs/WORK.md funnel item 4 ("a SECOND reward:risk floor at execution
    # time") says to fold that belt into item 1(b); this is that fold. With
    # the flag set, the belt checks for DEGRADATION instead: execution may
    # not make the ratio worse than the geometry the RM approved.
    subfloor_catalyst_exception: bool = False
    # --- How this position is MANAGED (2026-09-11, WORK.md item 1(d)) -----
    # `TechAnalysisResult.setup_type` for the analysis this order was built
    # from — "range" (Type A) or "breakout" (Type B) — or None for
    # SELL/COVER/HOLD, a legacy caller, or a target with no analysis.
    #
    # Carried for exactly the same reason as `stop_rule` and
    # `subfloor_catalyst_exception` above: a fact the constructor knows and
    # the execution stage cannot re-derive without building a second copy of
    # it. Two consumers:
    #   - `src/pipeline_stages.py`'s execution-time reward:risk belt, which
    #     must not run at all for a breakout (no overhead level exists to
    #     measure a reward against — see
    #     `src.risk.constants.reward_risk_floor_applies`);
    #   - `src/agents/risk_manager.py`'s rendering of the order, which must
    #     not show a Risk Manager an "R/R x:1" figure for a trade whose
    #     approval never depended on one.
    setup_type: str | None = None
    # --- The MEASURED half of the same verdict (item 82, 2026-09-25) ------
    # `structural_ceiling=(derivation.level_used is not None)` — the SAME
    # value the constructor itself fed into `is_trend_trade`/
    # `reward_risk_floor_applies` when it decided whether this trade's stop
    # got a reward:risk check at all. `setup_type` above is only the
    # analyst's raw label; construction's actual verdict is
    # `reward_risk_floor_applies(setup_type, structural_ceiling=...)`, which
    # is True (breakout, no ratio) whenever EITHER the label says
    # "breakout" OR this field is False.
    #
    # Without this, a downstream consumer that re-derives the verdict from
    # `setup_type` alone (no structural_ceiling) sees only the label half —
    # so a measured breakout the analyst still labelled "range" is shown a
    # real R/R ratio and can be refused/resized, which construction's own
    # exemption forbids. `src/agents/risk_manager.py`'s rendering of the
    # order must reach the SAME verdict construction reached, not a
    # label-only approximation of it.
    structural_ceiling: bool | None = None
    # --- Thesis invalidation, as a real field (2026-09-03) ----------------
    # Mirrors the conviction-ledger fields above: pinned at ENTRY (BUY/
    # SHORT) only, default None so every pre-existing construction site
    # (HOLD/_build_sell/_build_cover, tests, replay.py, ops/model_policy/
    # scenarios.py) is unaffected.
    #
    # `TargetPosition.thesis_invalid_if` (the analyst's own falsifier
    # condition, see that field's docstring) was previously carried
    # downstream ONLY as text appended to `reasoning` — e.g. "(invalid if:
    # ...)" — and `TradingPipeline._build_position_history` shortens
    # `reasoning` to 280 chars for the prompts (the constructor's own
    # 500-char cut was removed in #1766). A long condition could silently
    # lose the one piece of information a later holding-discipline check
    # needs to verify an exit. This field carries the same string untruncated
    # (aside from the 2000-char cap below, which exists only to bound a
    # misbehaving LLM's output, not to truncate a normal thesis) so it
    # survives regardless of what happens to `reasoning`.
    #
    # The embedded-in-reasoning text is NOT removed — some existing code
    # may still parse it out of `reasoning` — this field is additive.
    thesis_invalid_if: str | None = Field(default=None, max_length=2000)

    @computed_field
    @property
    def reward_risk(self) -> float | None:
        """Reward/risk ratio of the CONSTRUCTED order, computed in Python.

        Deliberately mirrors `TechAnalysisResult.risk_reward` — including its
        "not trusted to the LLM" rule — because the object that ACTUALLY
        REACHES EXECUTION never got that treatment, and the omission cost a
        live trading session on 2026-08-31.

        What happened: the Risk Manager is handed entry/stop/target as bare
        text with no ratio, so it does the division inside the model. For a
        BUY on RSG (entry $221.14, stop $207.90, target $242.96) it computed
        the ratio TWICE IN ONE RESPONSE — `rr_audit` said "R/R = 1.65 ...
        above 1.5, so compliant", while `reasoning` said "R/R = 1.31, which
        is below the 1.5 floor" and rejected the trade. The pipeline acts on
        the second field. 1.65 is correct; 1.31 matches no combination of the
        inputs and was simply wrong.

        An LLM must not own the arithmetic of a gate. It judges; the
        deterministic side computes. Geometry rules match the tech-analyst
        field: prices must be present and the inequalities must hold for the
        side, else None, so nobody renders a fake ratio.

        Since 2026-09-02 the division itself lives in `reward_to_risk`, so
        this field, `TechAnalysisResult.risk_reward`, the constructor's
        entry gate and the execution-time re-check are all the SAME
        arithmetic on whatever geometry each is handed. See that function
        for the XLE 1.67-vs-1.18 rejection that forced it.
        """
        if self.action == "BUY":
            is_short = False
        elif self.action == "SHORT":
            is_short = True
        else:
            # SELL / COVER reduce an existing position; HOLD opens nothing.
            # No entry geometry to measure, so no ratio exists.
            return None
        ratio = reward_to_risk(
            self.entry_price,
            self.stop_loss,
            self.take_profit,
            is_short=is_short,
        )
        return None if ratio is None else round(ratio, 2)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return _normalize_symbol(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_enum_case(cls, values):
        # action is UPPERCASE per Literal — fold LLM drift like "buy".
        return _normalize_enum_case_fields(values, upper_fields=("action",))

    @model_validator(mode="after")
    def validate_buy_prices(self):
        if self.action == "BUY":
            if self.entry_price <= 0:
                raise ValueError("BUY decisions require entry_price > 0")
            if self.stop_loss < 0:
                raise ValueError("BUY decisions require stop_loss >= 0")
            if self.take_profit is not None and self.take_profit <= 0:
                raise ValueError("BUY decisions require take_profit > 0 when one is given")
            if self.stop_loss > 0 and self.stop_loss >= self.entry_price:
                raise ValueError("BUY decisions require stop_loss to stay below entry_price")
            if self.take_profit is not None and self.take_profit <= self.entry_price:
                raise ValueError("BUY decisions require take_profit to stay above entry_price")
        elif self.action == "SHORT":
            # Mirror of the BUY geometry: a short's stop protects ABOVE
            # entry and its take-profit sits BELOW entry (price must fall
            # for a short to profit).
            if self.entry_price <= 0:
                raise ValueError("SHORT decisions require entry_price > 0")
            if self.stop_loss < 0:
                raise ValueError("SHORT decisions require stop_loss >= 0")
            if self.take_profit is not None and self.take_profit <= 0:
                raise ValueError("SHORT decisions require take_profit > 0 when one is given")
            if self.stop_loss > 0 and self.stop_loss <= self.entry_price:
                raise ValueError("SHORT decisions require stop_loss to stay above entry_price")
            if self.take_profit is not None and self.take_profit >= self.entry_price:
                raise ValueError("SHORT decisions require take_profit to stay below entry_price")
        # SELL and COVER don't need live entry/stop/target — execution uses
        # market price, exactly as SELL always has.
        return self


class ReasoningChain(LLMOutputModel):
    """7-step CoT for the portfolio manager — forces the audit trail on the
    central decision. Every required field has `min_length=1` so the LLM
    can't dodge a step with `""`. continuity_check AND premortem_check are
    intentionally optional (default `""`) for backward-compat with older logs
    (pre-memory-layer / pre-2026-06 respectively) but are mandatory per the
    prompt; everything else is mandatory at the schema layer too.
    """

    macro_filter: str = Field(min_length=1)
    news_check: str = Field(min_length=1)
    earnings_check: str = Field(min_length=1)
    signal_conflicts: str = Field(min_length=1)
    sizing_logic: str = Field(min_length=1)
    portfolio_balance: str = Field(min_length=1)
    cash_target: str = Field(min_length=1)
    # Continuity check — narrates how today's decisions fit the 7-day arc.
    # Optional (old logs don't carry it) but required when memory layers are provided.
    continuity_check: str = ""
    # Pre-mortem — the disconfirming/red-team step. The strongest case AGAINST
    # today's biggest position(s) + the single observable that would prove the
    # thesis wrong. Optional-default for backward-compat with pre-2026-06 logs
    # (same pattern as continuity_check) but MANDATORY per the prompt — its job
    # is to catch the systematic directional bias a forward-only CoT misses.
    premortem_check: str = ""
    # Macro logic audit — the answer to a question the prompt had been asking
    # with nowhere to put the reply. PM's rendered briefing ships the macro
    # seat's full six-paragraph `reasoning_chain` verbatim under the heading
    # "audit these for logic errors", and until 2026-09-14 this schema had no
    # field of any kind that such a finding could land in. Across the archived
    # PM calls that carried those paragraphs, no response ever named a macro
    # logic error — which is what an instruction with no output channel looks
    # like from the outside. That is a structural argument, not a measured
    # improvement: this desk has no rig that can validate a prompt rewrite
    # (the rehearsal rig replays recorded answers into a changed prompt and
    # passes regardless), so the claim rests on "no field existed", which is
    # checkable by reading this class, and not on a benchmark.
    #
    # Optional-default at the schema layer for the same reason as the two
    # fields above — every archived log predates it — but MANDATORY per the
    # prompt. Deliberately NOT `min_length=1`: forcing a non-empty string
    # when the chain is sound is how the fabricated `or "n/a"` placeholders
    # started (see `ExitReviewChain`). "No logic error found" is a real
    # verdict and the prompt asks for it; an invented one is not.
    macro_audit: str = ""


class ExitReviewChain(ReasoningChain):
    """The POSITION REVIEWER's chain, in the container the risk seat reads.

    `_risk_review_exits` carries the reviewer's own six checks in a
    `ReasoningChain` because that is the container `RiskManagerAgent` renders;
    `risk_review_mode._CHAIN_ROWS[EXIT_REVIEW]` relabels each slot with the
    reviewer's real field name. One PM slot has no counterpart at all —
    `news_check` — and is not rendered to the seat on this path. It was
    nonetheless being filled with a placeholder string ("[unused on the
    exit-review path...]") for one reason only: `ReasoningChain` makes it
    `min_length=1`. Inventing content to satisfy a constraint that does not
    apply is how the fabricated `or "n/a"` placeholders started.

    So this subclass relaxes exactly that one field, and only for the exit
    path. It is a deliberate narrowing of a parent constraint, kept to a
    single field so the relaxation is legible: the MORNING path never
    constructs this class, and `ReasoningChain.news_check` stays mandatory
    there. `continuity_check` and `premortem_check` are already optional on
    the parent and are never rendered on this path.
    """

    news_check: str = ""


class AnalystProvenance(LLMOutputModel):
    """Machine-checkable specialist claim supporting a PM target.

    ``relationship=conflicts`` is an explicit, legitimate PM disagreement;
    it is not a veto.  The PM boundary verifies that the named specialist
    actually covered the symbol and that ``observed_stance`` matches its
    validated output.
    """

    source: Literal["technical", "news", "earnings", "macro", "smart_money"]
    observed_stance: str = Field(min_length=1)
    relationship: Literal["supports", "conflicts", "context"]
    evidence: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _normalize_case(cls, values):
        return _normalize_enum_case_fields(
            values,
            lower_fields=("source", "observed_stance", "relationship"),
        )
