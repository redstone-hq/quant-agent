import os
import re
from pathlib import Path
from typing import ClassVar

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from src.agents.base import (
    VALID_PROVIDERS,
    provider_attempt_budget,
    resolve_provider,
)
from src.config.notifications import NotificationsConfig  # noqa: F401  (re-export)
from src.config.macro import MacroConfig  # noqa: F401  (re-export; lives in its own module)
from src.live_capital_preflight import LiveCapitalBlocked, assert_live_capital_authorized
from src.trading_calendar import SESSION_WINDOWS
from src.risk.constants import (
    STARTER_POSITION_RISK_PCT,
)


class ApiKeysConfig(BaseModel):
    anthropic: str
    openai: str = ""
    deepseek: str = ""
    # OpenRouter (Stage 1 QAMC provider/model plumbing) — optional, only
    # required when an agent's explicit `provider: openrouter` is selected
    # (enforced in AppConfig._check_llm_provider_keys, not here, since that's
    # the layer that already knows which agents are configured for it).
    openrouter: str = ""
    # Google AI Studio direct (2026-08-31 owner decision: gemini-3.5-flash-lite
    # direct becomes the PRIMARY route for the eight specialist/review seats) —
    # optional, only required when an agent's explicit `provider: google` is
    # selected, or google is reachable as the configured cross-provider
    # failover target (both enforced in AppConfig._check_llm_provider_keys).
    google: str = ""
    fred: str
    alpaca_key: str
    alpaca_secret: str

    @model_validator(mode="after")
    def _check_required_keys(self):
        for field_name in ("alpaca_key", "alpaca_secret", "fred"):
            if not getattr(self, field_name):
                raise ValueError(f"Required API key '{field_name}' is empty — check your .env file")
        if not (self.anthropic or self.openai or self.deepseek or self.openrouter or self.google):
            raise ValueError(
                "At least one of 'anthropic', 'openai', 'deepseek', 'openrouter', or 'google' API key must be set"
            )
        return self


# Alpaca's paper-trading host. `base_url` is declarative today — no code path
# reads it (the real switch is the `paper` flag below, which alpaca-py turns
# into an endpoint choice) — so the validator's job is to stop the two from
# disagreeing and giving a reader a false impression of which venue is in use.
_ALPACA_PAPER_HOST = "paper-api.alpaca.markets"

# The deliberate, reviewed code-level authorization for live capital. Flipping
# this is one of the TWO things that must happen for the desk to leave paper;
# the other is the live-capital pre-flight gate (board item 150,
# `src/live_capital_preflight.py`) passing every condition in its ACTIVATION
# scope. Neither alone is enough, on purpose:
#
#   * a settings.yaml edit alone still fails — this constant is False;
#   * flipping this constant alone still fails — the gate blocks and the error
#     names every unmet condition;
#   * a signed attestation file alone still fails — this constant is False.
#
# Before this existed the guard simply raised, which was safe but silent about
# WHY; the checklist lived in prose and nothing checked it. Do not flip this
# without the gate reporting PASS, and never as a drive-by edit.
LIVE_TRADING_AUTHORIZED = False


class AlpacaConfig(BaseModel):
    base_url: str
    paper: bool

    @model_validator(mode="after")
    def _enforce_paper_only(self):
        """Fail closed unless this is a paper account.

        "Alpaca **Paper only**; live trading is not authorized" is a hard
        boundary in CLAUDE.md, docs/STATE.md and AGENTS.md, but until now
        it lived entirely in prose: flipping `paper: false` in settings.yaml
        would have silently pointed the whole decision chain at a live
        brokerage account with no test, guard, or log to notice. A one-token
        config edit should not be able to do that.

        This is deliberately a hard failure with no env-var escape hatch. If
        live trading is ever authorized, it takes BOTH a reviewed code change
        flipping `LIVE_TRADING_AUTHORIZED` above AND the live-capital pre-flight
        gate (board item 150) reporting every activation-scope condition
        satisfied. This is the point where the paper lock would be lifted, so
        this is where the gate sits: there is no code path to a live account
        that does not run it, and a refusal names the conditions that failed.
        """
        if self.paper is not True:
            if not LIVE_TRADING_AUTHORIZED:
                raise ValueError(
                    "alpaca.paper must be true — live trading is not authorized "
                    "(see the hard boundaries in CLAUDE.md / docs/STATE.md). "
                    "Enabling live trading requires BOTH a reviewed change to "
                    "config.LIVE_TRADING_AUTHORIZED and a passing live-capital "
                    "pre-flight gate (src/live_capital_preflight.py), not a "
                    "settings.yaml edit."
                )
            # Authorized in code — the gate still has the last word, and names
            # which condition failed rather than refusing anonymously.
            try:
                assert_live_capital_authorized()
            except LiveCapitalBlocked as exc:
                raise ValueError(
                    f"alpaca.paper is false and live trading is code-authorized, "
                    f"but the live-capital pre-flight gate refuses: {exc}"
                ) from exc
            return self
        host = self.base_url.strip().lower()
        if host and _ALPACA_PAPER_HOST not in host:
            raise ValueError(
                f"alpaca.base_url must point at {_ALPACA_PAPER_HOST} while "
                f"paper-only is in force; got {self.base_url!r}"
            )
        return self


class RiskConfig(BaseModel):
    max_position_pct: float = Field(gt=0, le=100)
    max_total_position_pct: float = Field(gt=0)
    require_stop_loss: bool
    # Owner-ratified total at-risk ceiling (2026-08-27): the sum of every
    # position's loss-if-stopped, measured against cost basis, may not exceed
    # this share of equity. Distinct from `max_total_position_pct`, which caps
    # NOTIONAL: a $50k book with 10% stops is 50% invested and 5% at risk.
    # Capital is meant to be fully deployed; it is RISK that is rationed.
    #
    # Reporting-only today — `PMFacts` renders the figure and its headroom so
    # the Portfolio Manager sizes against a real number instead of a rule it
    # was told about but never shown. Phase 2b makes it a hard gate.
    max_portfolio_risk_pct: float = Field(default=25.0, gt=0, le=100)
    # Spec §2.1. The owner-ratified per-trade envelope (2026-08-27). Conviction
    # is expressed as the share of equity an idea may lose if its stop is hit,
    # and the constructor derives share count from it:
    #     shares = (equity x risk_pct / 100) / |entry - stop|
    # A wider stop therefore yields a SMALLER position rather than a rejected
    # trade, which is what removes the incentive to squeeze stops. The prior
    # 0.5% ceiling lived in a constructor dataclass default nobody chose.
    max_position_risk_pct: float = Field(default=5.0, gt=0, le=100)
    # Owner rule 2026-08-27: below this risk per position the desk does not
    # trade. Not a commission floor -- Alpaca charges none. A request
    # rationed under the floor is denied outright rather than shrunk, and the
    # constructor and execution re-check it after every later size cut.
    min_position_risk_pct: float = Field(
        default=STARTER_POSITION_RISK_PCT,
        ge=0,
        le=100,
    )
    # Spec §2.2. The most of the total at-risk ceiling any ONE correlated
    # cluster may take. Without it "total risk is under 25%" says nothing
    # about diversification — a book holding one theme four times over
    # satisfies it while carrying exactly the concentration the ceiling
    # exists to prevent. Correlated names consume one bet's budget.
    max_cluster_risk_share_pct: float = Field(default=40.0, gt=0, le=100)
    # Minimum stop distance in ATRs. Structure places the stop; this only
    # pushes it out when structure put it inside ordinary volatility. Measured
    # 2026-08-27: stops sat a median 4.3% below entry against a median ATR of
    # 2.56% of price — about 1.7 ATRs, barely more than one ordinary day's
    # range, which is what was firing exits inside noise AND forcing enormous
    # positions to reach any meaningful risk.
    #
    # 3.0 -> 1.5 -> 2.5 (2026-09-10). The 1.5 came from this desk's own
    # ~2-week MAE sample — later found to overlap the window whose seat
    # outputs were misreporting confidence/data quality, so no longer trusted
    # as the sole basis. 2.5 comes from published swing-trading doctrine
    # instead (2.5-3.0x ATR for a fixed entry stop on a multi-day hold),
    # independent of this desk's own data. Only applies with no real level
    # backing the stop — a level-backed stop is judged on its own honest
    # distance regardless of this number. Full derivation and caveats:
    # `config/settings.yaml` (this key) and docs/INCIDENT_HISTORY.md
    # 2026-09-10. Keep the three in sync.
    #
    # 2026-09-30, board item 90 — value UNCHANGED. Reasons live in
    # `config/number_ledger.yaml` under this id rather than copied here;
    # two corrections belong at the definition site itself.
    #
    # FIRST, the "3.0 -> 1.5 -> 2.5 (2026-09-10)" line above is CORRECT and
    # is confirmed here because its counterpart in config/settings.yaml is
    # not: that file says the first leg happened on 2026-09-04, and no such
    # move exists. Read from git, the DEPLOYED value went 3.0 (3dff9408,
    # 2026-08-27) straight to 2.5 (0088328c, 2026-09-10) and was never 1.5
    # — `git log --all -S'min_stop_atr_multiple: 1.5' -- config/settings.yaml`
    # returns nothing. 0088328c squashes PR #269, whose sub-commit one moved
    # 3.0 -> 1.5 and whose sub-commit two moved 1.5 -> 2.5 on the same day;
    # the 1.5 never left that branch. Both legs, one merge, one date.
    #
    # SECOND, "2.5 comes from published swing-trading doctrine" is NOT a
    # citation and this number is not sourced. docs/OUTCOME.md requires a
    # URL a later reader can open. Searched 2026-09-30: no such source
    # exists for a FIXED entry stop, the corroborating 2-3x figures are
    # trailing-stop mechanisms (which the comment below already concedes),
    # and one secondary claim points the other way entirely. Both ends of
    # the quoted band are unsupported, not merely the point inside it.
    # 2.5 is therefore the INTERIM value and is deliberately not re-picked:
    # with no cited band, moving it would be one more unsourced choice.
    # The route that removes this constant for most names it governs is
    # board item 199 (read the floor off the nearest computed level rather
    # than off an ATR multiple).
    min_stop_atr_multiple: float = Field(default=2.5, gt=0, le=10)
    # NO `min_reward_risk_after_widening` HERE ANY MORE — removed 2026-09-24
    # (board item 81). It refused nothing and capped nothing: no code in
    # `PortfolioConstructor` ever read `self.min_reward_risk_after_widening`,
    # and the one place the value was threaded to
    # (`PortfolioManagerAgent._apply_subfloor_catalyst_rule`) explicitly
    # discards it. Removed keys are rejected loudly by
    # `_reject_deleted_reward_risk_floor_key` below.
    # --- Level-backed stops (spec §12.1, 2026-09-01) ---------------------
    # `min_stop_atr_multiple` above used to OVERWRITE the structural stop
    # whenever the level sat closer than the band, after which the stop was
    # at nothing real. On 2026-09-01 the desk reviewed 38 qualified
    # signals and placed zero trades. A stop that sits at a level
    # `src/data/levels.py::find_structural_levels` actually computed is now
    # honoured whatever its ATR distance; the band only applies when nothing
    # computed backs it.
    #
    # NO `level_match_atr_tolerance` HERE ANY MORE — removed 2026-09-13,
    # docs/WORK.md item 46. It was 0.25 ATR and justified itself as being "at
    # least as wide" as the 1% zone `find_structural_levels` clusters pivots
    # into. Those are different units, so the claim was only ever true above
    # a particular volatility: 0.25 x ATR >= 0.01 x price needs ATR >= 4% of
    # price. At the 2.56%-of-price median ATR this repo's own comments quote,
    # 0.25 ATR is 0.64% — 1.56x NARROWER than the zone it claimed to cover,
    # so a stop sitting inside a level's real zone was not counted as sitting
    # at that level. There is no honest ATR multiple to replace it with: the
    # zone is defined as a percentage of price, the tolerance was a multiple
    # of ATR, and the ratio between them changes with every name on every
    # day. It is not replaced by a different constant — it is deleted, and
    # "is this stop AT this level" now reads the zone's own bound from
    # `src.data.levels.level_zone_halfwidth`, which derives it from the same
    # `CLUSTER_TOLERANCE_PCT` that built the zone. The two can no longer
    # disagree because there is only one of them.
    #
    # The ATR argument the old comment made is not lost, it was misplaced:
    # "is this stop far enough out to survive the name's noise" IS an ATR
    # question, and it is already asked, deterministically, by
    # `min_stop_atr_multiple` and `absolute_min_stop_atr_multiple` below.
    # "Which level is this stop sitting on" is an identity question about a
    # zone, and is answered in the zone's own unit.
    # The deterministic backstop under the exemption above. §12.1's safety
    # argument rests on the 1*ATR hard floor in
    # `config/prompts/tech_analyst.md` — but that is a PROMPT, and Invariant
    # 2 requires deterministic Python protections to be the final authority
    # and to fail closed. A real support level 0.2 ATR under entry is genuine
    # structure AND a guaranteed whipsaw. So a level-backed stop is honoured
    # however tight down to this many ATRs; inside it the stop is pushed out
    # to exactly this floor — never to the full `min_stop_atr_multiple` band.
    absolute_min_stop_atr_multiple: float = Field(default=1.0, ge=0, le=10)
    # How many prior touches a computed level needs before a stop sitting on
    # it is trusted enough to be honoured however tight (Phase 12.1,
    # 2026-09-03 — docs/RESEARCH_FINDINGS.md §7). `find_structural_levels`
    # already requires 2 touches to register a level at all (`MIN_TOUCHES`),
    # but §12.1's own text names that as a SEPARATE, undecided question: "a
    # level currently qualifies on two touches ever ... so two old swing
    # points can justify a very tight stop." The measured table (101
    # symbols, 5 years, daily bars, real vs shuffled arithmetic control)
    # only clears real-vs-shuffled separation cleanly at 5+ touches: real
    # 0.644 [0.590, 0.696] against shuffled 0.505 [0.470, 0.539] — the
    # confidence intervals do not overlap at all. Every lower bucket's
    # intervals overlap or nearly touch (2 touches: real floor 0.510 versus
    # shuffled ceiling 0.510; 3 and 4 touches overlap outright), so a bar
    # below 5 would be honouring a tight stop on a separation that could be
    # noise. Below this bar the stop is NOT treated as level-backed and
    # falls back to `min_stop_atr_multiple` / `absolute_min_stop_atr_multiple`
    # exactly as an unbacked stop does — it does not become untradeable, it
    # loses only the tight-stop exemption.
    min_level_touches_for_stop_honor: int = Field(default=5, ge=1, le=20)
    # --- Target derivation (2026-09-01) ---------------------------------
    # The floor above was dividing a stop computed from measured volatility
    # by a target a language model guessed. On 2026-09-01's morning run that
    # rejected 30 of 38 actionable signals (79%) before any judgement was
    # applied, the two highest-conviction calls among them. The floor is not
    # the defect; its numerator was. These tune the deterministic target
    # derivation that replaced it — see the target-derivation section of
    # src/data/levels.py for the rule and the arithmetic.
    #
    # Flags a target inside this many ATRs of entry as thin reward — the
    # whole payoff sits inside one ordinary session's range. It LABELS the
    # derived target and never selects it (changed 2026-09-30; it used to
    # drop such a level and take the next one out, past real structure).
    min_target_atr_multiple: float = Field(default=1.0, gt=0, le=5)
    # Measured move claimed when no structural level stands in the way, in
    # sqrt(session)-scaled ATRs. 1.0 = the typical excursion over the stated
    # horizon. NOTE the interaction with `min_stop_atr_multiple`: a stop at
    # k ATRs and a target at p*ATR*sqrt(H) clear a floor f only when
    # sqrt(H) >= f*k/p — at k=3.0, p=1.0, f=1.5 that is H >= ~21 sessions.
    breakout_projection_atr_multiple: float = Field(default=1.0, gt=0, le=5)
    # How far price can plausibly travel within the horizon, same units.
    # Looser than the projection on purpose: this asks "could it get there",
    # the projection asks "how far do I claim it goes".
    max_target_reach_atr_multiple: float = Field(default=1.5, gt=0, le=5)
    # NO `max_stop_width_reach_atr_multiple` HERE ANY MORE -- the stop-width
    # REFUSAL it threshold-ed was deleted 2026-09-26 (board item 56, route
    # (c)). It was split off from `max_target_reach_atr_multiple` on
    # 2026-09-13 so that estimating a target and refusing a trade stopped
    # sharing one number; the split made them independent without making
    # either derived, and no published work fixes the touch probability
    # below which a stop stops being a stop. Measured before deletion: 648
    # sized stops recorded a touch-probability reading in production
    # (quant_agent.log, 2026-09-13..2026-09-26) and the refusal fired zero
    # times; the widest stop ever seen was 1.29 x ATR x sqrt(H) against a
    # 1.5 cap. A wide stop is answered by a smaller position
    # (`_plan_risk_targets`, the ratified spec 2.1 invariant) and, at the
    # extreme, by `position_sized_to_zero`. Removed keys are rejected loudly
    # by `_reject_deleted_stop_width_gate_key` below. The target-side
    # `max_target_reach_atr_multiple` is UNAFFECTED and still in force.
    # Ceiling on `expected_horizon_sessions` before it enters the sqrt()
    # travel estimate, so an implausible horizon cannot licence a target far
    # outside anything the symbol does.
    max_target_horizon_sessions: int = Field(default=60, ge=1, le=500)
    # Absolute gap between the computed target and the analyst's guess above
    # which the disagreement is logged at WARNING. The guess is kept as
    # evidence, never as arithmetic.
    target_divergence_warn_pct: float = Field(default=25.0, gt=0, le=200)
    # Cash-only default. When False: no BUY may drive `cash` below zero, and
    # any session that starts with `cash < 0` must de-lever (SELL) before any
    # new BUY. When True: normal margin account behavior, risk engine only
    # enforces the exposure / sector / loss caps. Default False is the
    # conservative choice — margin leverage amplifies drawdowns and is not
    # the bot's intended mode unless explicitly opted in.
    allow_margin: bool = False
    # --- Margin interest tracker (spec §11.2, 2026-09-01) ----------------
    # MEASURES, does not gate — this field feeds an estimate/alert only,
    # never a risk check. Alpaca's live non-elite margin rate (elite is
    # 4.75%); a config value rather than a code constant so the desk can
    # correct it without a deploy if Alpaca's rate moves. Interest accrues
    # ONLY on the END-OF-DAY (overnight) debit balance — intraday leverage
    # is free — per `(overnight debit balance x rate) / 360`. See
    # src/margin_interest.py: whether PAPER trading actually charges
    # this is UNCONFIRMED (Alpaca's own comparison lists short-borrow fees
    # as "Coming Soon" and is silent on margin interest either way), so
    # every figure this produces is a labelled ESTIMATE until the broker's
    # own `INT` account activity settles it empirically.
    margin_interest_rate_pct: float = Field(default=6.25, ge=0, le=100)
    # --- Spec §11.2: the gross-exposure ceiling (owner-ratified 2026-09-01)
    #
    # Gross exposure = long market value + ABSOLUTE short market value,
    # measured against equity. Before this setting existed the codebase had
    # NO gross-exposure ceiling of any kind: `max_portfolio_risk_pct` bounds
    # AT-RISK capital (the sum of stop distances), not exposure. Nothing stopped the book reaching the
    # broker's full 4x. Adding this is a TIGHTENING, not a loosening.
    #
    # 2.0x is the owner's deliberate paper-account learning setting, taken
    # against the recommendation to defer — see the §11.2 spec entry and
    # [[qamc-live-capital-checklist]]. Re-derive it before real money.
    #
    # This is the STANDING cap, day AND night. There is deliberately no
    # separate, lower overnight ceiling: an intraday-only allowance would
    # force a trim into every close, selling on a clock rather than on merit,
    # and this desk holds for days so it would almost never use one. The
    # overnight cushion comes from the de-levering ladder
    # (`src/risk/rules.py::resolve_gross_ceiling`) instead.
    #
    # The ladder can only ever tighten this number, never raise it — so
    # lowering this setting lowers every rung with it.
    max_gross_exposure_x: float = Field(default=2.0, gt=0, le=4.0)
    # Broker maintenance-margin requirement, as a percent of gross exposure,
    # used ONLY to report distance-to-forced-liquidation
    # (`src/risk/rules.py::distance_to_forced_liquidation_pct`). It computes
    # nothing the engine enforces; it answers "how far could the book fall
    # before the broker sells without asking", which nothing watched before
    # §11.2. 25% is Alpaca's standard equity maintenance requirement and
    # reproduces the spec's two published figures exactly: ~33% at 2.0x,
    # ~55% at 1.5x.
    maintenance_margin_pct: float = Field(default=25.0, gt=0, lt=100)
    # --- Stage 3 (shorts) -----------------------------------------------
    # Shorts carry the SAME limits as longs (owner decision 2026-09-17).
    # There is deliberately no short-specific concentration or gross-bearish
    # cap: the former `max_single_short_pct` (10) and `max_gross_bearish_pct`
    # (20) were unsourced numbers. One short is capped by `max_position_pct`
    # exactly as one long is (src/risk/rules.py), and the book either way is
    # bounded by `max_gross_exposure_x` and `max_total_position_pct`. Both
    # removed keys are rejected loudly by `_reject_removed_short_cap_keys`.
    # Sizing-only haircut (never applied to stop placement) on a short's
    # risk-per-share. A short gaps through its stop upward with no bound —
    # equal nominal risk is not equal real risk — so the same risk
    # allocation opens a SMALLER short than an equivalent long.
    #
    # --- Kill switch (2026-09-02 operational safety guard) ---------------
    # A file whose mere EXISTENCE halts every order this desk would place —
    # entries, exits, covers, and protective-stop placement/replacement
    # alike. Read with `Path(...).exists()` and nothing else: no parsing, no
    # schema, so a malformed or empty file still halts — it cannot fail open
    # on bad content because it never reads any content. Ops stops the desk
    # with `touch <path>` and resumes it by deleting the file: no code
    # change, no deploy, and it takes effect on the NEXT order attempt even
    # if the process was already mid-session when the file appeared.
    #
    # Checked in `src/execution/broker.py` (the deterministic execution
    # layer), never by an agent or a prompt — a language model has no path
    # to talk the desk out of a halt it cannot see or reason about.
    #
    # UNLIKE every other guard in this file, this ONE also blocks
    # risk-REDUCING orders. Every other hard block and circuit breaker here
    # deliberately lets a SELL/COVER through even while it blocks new risk
    # (`RiskRuleEngine.check`'s `action in ("SELL", "COVER")` exemption
    # below), precisely so a bad account state can never trap a position.
    # The kill switch is the one lever that overrides that, for the case
    # where ops needs EVERYTHING stopped — including an exit that might
    # otherwise go out into a broken/stale market. It only blocks NEW
    # broker-bound order flow; a protective stop already resting at the
    # broker from before the halt is untouched and keeps protecting the
    # position.
    kill_switch_path: str = Field(default="data/KILL_SWITCH")

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_loss_alarm_keys(cls, data):
        # Owner instruction 2026-09-20 (docs/INCIDENT_HISTORY.md, retired
        # board item 32): the entire account-level loss-alarm mechanism — the
        # daily halt and the 5-day / 20-day BUY-halving brakes — was removed.
        # Same pattern and reason as the validators below: `extra="ignore"`
        # would let a stale deployment's settings.yaml keep these keys and
        # load silently, and an operator would believe a daily halt was
        # armed when nothing reads it. That is the single most dangerous
        # form this particular removal could rot into, because the belief
        # it creates is a belief about loss protection.
        if isinstance(data, dict):
            stale = [
                k
                for k in (
                    "max_daily_loss_pct",  # retired-ok
                    "daily_loss_risk_multiple",  # retired-ok
                    "drawdown_vol_sensitivity",  # retired-ok
                    "drawdown_5d_risk_multiple",  # retired-ok
                    "drawdown_20d_risk_multiple",  # retired-ok
                )
                if k in data
            ]
            if stale:
                raise ValueError(
                    f"risk.{', risk.'.join(stale)} was removed 2026-09-20 on "
                    "the owner's instruction: the account-level daily-loss "
                    "halt and the 5-day/20-day rolling-return BUY brakes are "
                    "retired in full (docs/INCIDENT_HISTORY.md, board item "
                    "32). There is NO replacement key and no account-level "
                    "loss limit — per-position stops are the desk's loss "
                    "protection, and the §11.2 gross-exposure de-levering "
                    "ladder (risk.max_gross_exposure_x) is the only "
                    "account-level drawdown response left. Delete the key "
                    "from the settings file."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_agreement_ceiling_key(cls, data):
        # Same pattern as `_reject_removed_short_cap_keys` below and
        # `ExecutionConfig._reject_deleted_repeg_keys`: BaseModel's default
        # `extra="ignore"` would let a stale deployment's settings.yaml keep
        # the key and load silently, and an operator would believe a sizing
        # ladder they set was in force when nothing reads it.
        if isinstance(data, dict) and "agreement_ceiling_pct" in data:
            raise ValueError(
                "risk.agreement_ceiling_pct was removed 2026-09-14: the "
                "graduated agreement sizing ladder is retired (the sqrt(n/5) "
                "law prices INDEPENDENT estimates and this desk's seats are "
                "not independent). Agreement is now a refusal gate only — "
                "see src/risk/rules.py::agreement_refuses_trade. Delete the "
                "key from the settings file."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_short_cap_keys(cls, data):
        # Owner decision 2026-09-17: shorts carry the same limits as longs.
        # `max_single_short_pct` and `max_gross_bearish_pct` (and the latter's
        # pre-2026-08-30 name `max_short_gross_pct`) no longer exist. Same
        # pattern and reason as the validators around it: `extra="ignore"`
        # would let a settings.yaml still carrying one load silently, and an
        # operator would believe a short cap was in force when nothing reads
        # it. There is no replacement key — `max_position_pct` now governs a
        # short exactly as it governs a long.
        if isinstance(data, dict):
            stale = [
                k
                for k in (
                    "max_single_short_pct",
                    "max_gross_bearish_pct",
                    "max_short_gross_pct",
                )
                if k in data
            ]
            if stale:
                raise ValueError(
                    f"risk.{', risk.'.join(stale)} removed 2026-09-17: shorts "
                    "carry the same limits as longs (risk.max_position_pct, "
                    "risk.max_gross_exposure_x, risk.max_total_position_pct). "
                    "Delete the key from the settings file; there is no "
                    "replacement key."
                )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_level_match_key(cls, data):
        # docs/WORK.md item 46 (2026-09-13). Same pattern and same reason as
        # the validators above: `extra="ignore"` would let a settings.yaml
        # still carrying this key load silently, and an operator would
        # believe a match tolerance they set was in force when nothing reads
        # it any more. There is deliberately NO replacement key to point at
        # — the tolerance is no longer configurable, because it is derived
        # from the level zone's own definition. See the block where this
        # field used to be declared, above.
        if isinstance(data, dict) and "level_match_atr_tolerance" in data:
            raise ValueError(
                "risk.level_match_atr_tolerance was removed 2026-09-13 "
                "(docs/WORK.md item 46): an ATR multiple can never stay "
                "consistent with the percentage-of-price zone it claimed to "
                "cover. The tolerance is now derived from "
                "src.data.levels.CLUSTER_TOLERANCE_PCT and is not "
                "configurable. Delete the key from the settings file; there "
                "is no replacement key."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_reward_risk_floor_key(cls, data):
        # Board item 81 (2026-09-24). Same pattern and same reason as the
        # validators above: `extra="ignore"` would let a settings.yaml still
        # carrying this key load silently, and an operator would believe a
        # reward:risk floor was in force when nothing read it. It refused
        # nothing and capped nothing since 2026-09-17 (owner: residual
        # invented R/R is a defect) — see `src.risk.constants.REWARD_RISK_FLOOR`
        # for the full history. There is no replacement key.
        if isinstance(data, dict) and "min_reward_risk_after_widening" in data:
            raise ValueError(
                "risk.min_reward_risk_after_widening was removed 2026-09-24 "
                "(board item 81): it refused nothing and capped nothing. "
                "Delete the key from the settings file; there is no "
                "replacement key."
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def _reject_deleted_stop_width_gate_key(cls, data):
        # Board item 56 (2026-09-26), route (c). Same pattern and same
        # reason as the validator above: with `extra="ignore"` a
        # settings.yaml still carrying this key would load silently and an
        # operator would believe a stop-width refusal was in force when
        # nothing reads it. The gate is deleted, not retuned -- see
        # docs/INCIDENT_HISTORY.md 2026-09-26. There is no replacement key:
        # width is answered by position size, and the touch-probability
        # READING is still recorded on every sized stop
        # (`src.data.levels.touch_probability`).
        if isinstance(data, dict) and "max_stop_width_reach_atr_multiple" in data:
            raise ValueError(
                "risk.max_stop_width_reach_atr_multiple was removed "
                "2026-09-26 (board item 56): the stop-width refusal it "
                "thresholded is deleted, never having refused a single "
                "trade. Delete the key from the settings file; there is no "
                "replacement key. `max_target_reach_atr_multiple` is a "
                "different number and is unchanged."
            )
        return data


# ---- RE-EXPORT MIRROR (the one and only block; keep every moved name here) ----
# Sits before AppConfig because AppConfig composes these sections.
# Each section lives in its own module; `src.config.X` keeps resolving for every
# import site and every test patch target.
from src.config.llm import (  # noqa: E402,F401
    AGENT_NAMES,
    LLMConfig,
    _VALID_REASONING_EFFORTS,
)
from src.config.execution import (  # noqa: E402,F401
    ExecutionConfig,
)
from src.config.risk_adjuncts import (  # noqa: E402,F401
    CashReserveConfig,
    CashSweepConfig,
    EventRiskConfig,
)
from src.config.research import (  # noqa: E402,F401
    IntradayScanConfig,
    NewsConfig,
    NominationConfig,
    UniverseScreenConfig,
)
from src.config.smart_money import (  # noqa: E402,F401
    SmartMoneyConfig,
)
from src.config.operations import (  # noqa: E402,F401
    DeploymentGapConfig,
    EvolutionConfig,
    ReconciliationConfig,
    ScheduleConfig,
    StorageConfig,
    TradingConfig,
)
from src.config.llm_cost import (  # noqa: E402,F401
    INTRA_CHECK_TICK_MINUTES,
    LLMCostCircuitConfig,
    _paid_run_count,
)
# ---- END RE-EXPORT MIRROR ----


class AppConfig(BaseModel):
    api_keys: ApiKeysConfig
    alpaca: AlpacaConfig
    llm: LLMConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig
    trading: TradingConfig
    storage: StorageConfig
    llm_cost_circuit: LLMCostCircuitConfig = Field(default_factory=LLMCostCircuitConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    # Optional section — a settings.yaml without it gets a disabled sweeper
    # (enabled=False default), so older configs keep working unchanged.
    cash_sweep: CashSweepConfig = Field(default_factory=CashSweepConfig)
    deployment_gap: DeploymentGapConfig = Field(default_factory=DeploymentGapConfig)
    cash_reserve: CashReserveConfig = Field(default_factory=CashReserveConfig)
    # Optional section — a settings.yaml without it gets the scan disabled
    # (enabled=False default), so intra_check's existing behavior is
    # unchanged unless explicitly opted in.
    intraday_scan: IntradayScanConfig = Field(default_factory=IntradayScanConfig)
    smart_money: SmartMoneyConfig = Field(default_factory=SmartMoneyConfig)
    # Optional section — a settings.yaml without it gets the documented
    # defaults (3 per seat / 6 total), so older configs keep working
    # unchanged and Phase 9 stays off-by-default-bound rather than
    # unbounded.
    nominations: NominationConfig = Field(default_factory=NominationConfig)
    # Optional section — absent means the screen is off (enabled=False).
    universe_screen: UniverseScreenConfig = Field(default_factory=UniverseScreenConfig)
    # Optional section — a settings.yaml without it gets the documented
    # default lookback (7 days), so older configs keep working unchanged.
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    # Optional section — a settings.yaml without it gets the tailnet cockpit
    # default (see NotificationsConfig docstring), so older configs keep
    # alerting exactly as before, just with a link added.
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    # Optional section — a settings.yaml without it gets the pre-existing
    # 50-item prompt cap (see NewsConfig docstring), so older configs keep
    # working unchanged.
    news: NewsConfig = Field(default_factory=NewsConfig)
    # Optional section — a settings.yaml without it gets the documented FRED
    # resilience defaults (see MacroConfig docstring), so older configs keep
    # working unchanged.
    macro: MacroConfig = Field(default_factory=MacroConfig)
    # Optional section — a settings.yaml without it gets the documented
    # event-lookup ceilings (see EventRiskConfig docstring), so older configs
    # keep working unchanged.
    event_risk: EventRiskConfig = Field(default_factory=EventRiskConfig)

    def _fallback_key_for_provider(self) -> str:
        """The API key credential that must be present for `llm.fallback_provider`
        to actually be reachable as a cross-provider failover target. Reuses
        the same provider-name -> api_keys.* mapping pipeline.py's own
        `_key_for` closure uses, so config validation and client construction
        can never disagree about which credential a given fallback provider
        needs."""
        return {
            "openai": self.api_keys.openai,
            "deepseek": self.api_keys.deepseek,
            "openrouter": self.api_keys.openrouter,
            "google": self.api_keys.google,
        }.get(self.llm.fallback_provider, self.api_keys.anthropic)

    def _tertiary_key_for_provider(self) -> str:
        """The credential route 3 needs, by the same mapping as the fallback's.

        Deliberately a separate method rather than a parameterised one: the
        two are read in different places and a shared helper with a provider
        argument invites a call site passing the wrong one silently.
        """
        return {
            "openai": self.api_keys.openai,
            "deepseek": self.api_keys.deepseek,
            "openrouter": self.api_keys.openrouter,
            "google": self.api_keys.google,
        }.get(self.llm.tertiary_provider, self.api_keys.anthropic)

    def tertiary_available(self) -> bool:
        """True when route 3 is both configured and credentialed.

        Mirrors `BaseAgent._tertiary_reachable` minus the per-agent
        distinct-pair test, for the same reason `_fallback_reachable_for_any_
        agent` exists: the load-time attempt-budget check and the runtime gate
        drifting apart is the 2026-08-31 outage.
        """
        if not (self.llm.tertiary_model or "").strip():
            return False
        return bool((self._tertiary_key_for_provider() or "").strip())

    def _fallback_reachable_for_any_agent(self) -> bool:
        """True when at least one agent's (provider, model) pair differs from
        the configured fallback pair — i.e. failover could ever actually fire
        for that agent (mirrors `BaseAgent._failover_reachable`'s own
        not-identical-pair rule in src/agents/base.py, minus the key check,
        which the two call sites below apply separately).

        Shared by `_check_llm_provider_keys` and `_check_provider_attempt_
        budget` so they can never independently compute this and drift apart
        — which is exactly what caused the 2026-08-31 outage (see
        `provider_attempt_budget`'s docstring): the config check keyed off
        `api_keys.anthropic` while the runtime gate keyed off the primary
        provider, and the two were never proven to agree.
        """
        fallback_pair = (self.llm.fallback_provider, self.llm.fallback_model)
        return any(
            (
                resolve_provider(
                    getattr(self.llm, f"{agent_name}_model"),
                    self.llm.get_provider(agent_name),
                ),
                getattr(self.llm, f"{agent_name}_model"),
            )
            != fallback_pair
            for agent_name in AGENT_NAMES
        )

    @model_validator(mode="after")
    def _check_llm_provider_keys(self):
        openai_models = []
        anthropic_models = []
        deepseek_models = []
        openrouter_models = []
        google_models = []

        # Bucket by resolve_provider(model, explicit_provider) — the SAME
        # helper BaseAgent.__init__ uses to pick a client — rather than
        # re-deriving prefix logic here. An agent with an explicit
        # `*_provider` override is bucketed by that override, not by
        # whatever its model string's prefix would otherwise imply; this is
        # what makes an OpenRouter "vendor/model" id (which would otherwise
        # mis-bucket as Anthropic) require OPENROUTER_API_KEY instead.
        for agent_name in AGENT_NAMES:
            model_name = getattr(self.llm, f"{agent_name}_model")
            explicit_provider = self.llm.get_provider(agent_name)
            provider = resolve_provider(model_name, explicit_provider)
            label = f"{agent_name}_model={model_name}" + (
                f" (provider={explicit_provider})" if explicit_provider else ""
            )
            if provider == "deepseek":
                deepseek_models.append(label)
            elif provider == "openrouter":
                openrouter_models.append(label)
            elif provider == "google":
                google_models.append(label)
            elif provider == "openai":
                openai_models.append(label)
            else:
                anthropic_models.append(label)

        if openai_models and not self.api_keys.openai:
            selected = ", ".join(openai_models)
            raise ValueError(f"OPENAI_API_KEY is required for selected OpenAI models: {selected}")

        if deepseek_models and not self.api_keys.deepseek:
            selected = ", ".join(deepseek_models)
            raise ValueError(f"DEEPSEEK_API_KEY is required for selected DeepSeek models: {selected}")

        if openrouter_models and not self.api_keys.openrouter:
            selected = ", ".join(openrouter_models)
            raise ValueError(f"OPENROUTER_API_KEY is required for selected OpenRouter models: {selected}")

        if google_models and not self.api_keys.google:
            selected = ", ".join(google_models)
            raise ValueError(f"GOOGLE_API_KEY is required for selected Google models: {selected}")

        if anthropic_models and not self.api_keys.anthropic:
            selected = ", ".join(anthropic_models)
            raise ValueError(f"ANTHROPIC_API_KEY is required for selected Anthropic models: {selected}")

        # The failover credential cannot be silently missing when failover is
        # actually reachable — otherwise it is discovered only when the
        # primary fails and the failover attempt itself gets a 401. That is
        # the second half of the 2026-08-31 incident: no agent used Anthropic
        # as a primary, so the missing ANTHROPIC_API_KEY sat unnoticed until
        # a retry-exhausted call fell through to failover and hit
        # `401 credential_not_found` — after the attempt-budget arithmetic
        # above had ALREADY been fixed, so the failover fired for the first
        # time and immediately hit the second, independent gap.
        if self._fallback_reachable_for_any_agent() and not self._fallback_key_for_provider():
            raise ValueError(
                f"An API key for llm.fallback_provider={self.llm.fallback_provider!r} "
                f"is required: llm.fallback_model={self.llm.fallback_model!r} is "
                "reachable as the cross-provider failover target for at least one "
                "agent, but its credential is not configured. A silently-missing "
                "fallback key is precisely how the 2026-08-31 outage's second half "
                "happened — do not let this ship unnoticed again."
            )

        # DELIBERATELY NOT CHECKED HERE: a missing credential for
        # `llm.tertiary_alt_provider`. The fallback check above is a hard
        # error because failover is a route the operator configured and is
        # relying on; the route-3 second-road substitute is a default-on
        # improvement nobody asked for, and refusing to BOOT over a key it
        # needs would turn a resilience feature into an outage of its own —
        # a single-provider deployment (every seat and the fallback on one
        # road) is a legal configuration and must still start. Without the
        # key the substitution simply does not happen and route 3 stays
        # where it was configured. The guard that matters for THIS
        # deployment is mechanical and lives in CI instead:
        # tests/test_route_failover_ladder.py::
        # test_the_shipped_config_leaves_no_seat_on_a_single_road reads
        # config/settings.yaml and fails if any seat's routes collapse onto
        # one provider — which is the 2026-09-29 defect stated as a test
        # rather than as a runtime hope.

        return self

    @model_validator(mode="after")
    def _check_provider_attempt_budget(self):
        """Refuse to start if the circuit would trip on the retry loop itself.

        The cost circuit stops a logical call once it exceeds
        `llm_cost_circuit.max_provider_attempts_per_call` provider attempts.
        `BaseAgent.run()` decides how many attempts actually happen. When the
        ceiling is below what the loop can spend, the circuit fires on the
        loop's normal, designed behaviour rather than on anything unsafe — and
        because that stop is scoped to the session, a routine upstream
        rate-limit costs the desk a trading session for pennies of spend.

        That is not hypothetical: it is the 2026-08-31 09:32 ET incident
        recorded on `provider_attempt_budget`, where a hand-pinned 2 sat
        against a worst case of 3 and made cross-provider failover impossible
        to ever complete.

        The two numbers live in different worlds — one an env-overridable
        module constant, the other a YAML setting — which is exactly how they
        drifted apart unnoticed for six days across five separate trips. So
        the agreement is enforced here, at load, rather than trusted to
        whoever edits either one next. Failing to boot is the loud failure;
        going dark two minutes after the opening bell is the quiet one.

        `failover_available` is derived from the SAME not-identical-pair rule
        `BaseAgent._failover_reachable` uses at runtime (via
        `_fallback_reachable_for_any_agent`/`_fallback_key_for_provider`
        above) rather than independently keying off `api_keys.anthropic` —
        that independent keying is exactly what let this check and the
        runtime gate disagree in the first place.
        """
        failover_available = bool(self._fallback_key_for_provider()) and self._fallback_reachable_for_any_agent()
        required = provider_attempt_budget(
            failover_available=failover_available,
            tertiary_available=self.tertiary_available(),
        )
        configured = int(self.llm_cost_circuit.max_provider_attempts_per_call)
        if configured < required:
            raise ValueError(
                "llm_cost_circuit.max_provider_attempts_per_call is "
                f"{configured}, below the {required} provider attempts one "
                "agent call can make ("
                f"{required - (1 if failover_available else 0)} primary "
                + (
                    "attempts plus one cross-provider failover"
                    if failover_available
                    else "attempts, no failover configured"
                )
                + "). The circuit would stop the session on the retry loop's "
                "own designed behaviour — the failure this check exists to "
                "prevent. Raise it to at least "
                f"{required}, or remove it from settings.yaml to let it derive."
            )
        return self


def _substitute_env_vars(value: str, overrides: dict[str, str] | None = None) -> str:
    """Replace ${VAR_NAME} with environment variable values.

    `overrides` takes precedence over `os.environ` for the names it carries. It
    exists for credentials systemd delivered as files rather than environment
    variables (see `src/credentials.py`): on the live box `.env` still holds a
    placeholder for those names, and the placeholder must not win.

    With `overrides` omitted or empty this behaves exactly as it always has —
    every other interpolation in `settings.yaml` is untouched.
    """

    def replacer(match):
        var_name = match.group(1)
        if overrides:
            override_value = overrides.get(var_name)
            if override_value is not None:
                return override_value
        env_value = os.environ.get(var_name)
        if env_value is None:
            return ""  # Optional env vars resolve to empty string
        return env_value

    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _walk_and_substitute(obj, overrides: dict[str, str] | None = None):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj, overrides)
    if isinstance(obj, dict):
        return {k: _walk_and_substitute(v, overrides) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_substitute(item, overrides) for item in obj]
    return obj


def load_config(path: Path) -> AppConfig:
    """Build the application config.

    Credentials systemd delivered as files are preferred over the environment;
    everything else resolves from the environment as before. A visibly broken
    systemd hand-off raises `CredentialDeliveryError` here rather than letting
    the desk start on a placeholder and fail later at the broker.
    """
    from src.credentials import load_systemd_credentials

    with open(path) as f:
        raw = yaml.safe_load(f)
    credential_overrides = load_systemd_credentials()
    substituted = _walk_and_substitute(raw, credential_overrides)
    return AppConfig(**substituted)
