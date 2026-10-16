"""Fixed benchmark scenarios for QAMC model selection.

Each scenario drives a REAL agent class (`src/agents/*`) with the REAL
system prompt (`config/prompts/*.md`) over a frozen, synthetic input, and
grades the result with deterministic Python assertions.

Why real agents rather than a standalone prompt harness: the thing we are
choosing a model for is not "can it write JSON" — it is "does it survive
`analyze_batch` / `decide` / `review`, including `parse_json()`'s candidate
scan, the per-entry isolation dropping malformed rows, and every Pydantic
validator in `src/models.py`". A model that scores well on a paraphrase of
the prompt but trips `_validate_rating_price_consistency` is not usable, and
only the real call path can tell us that.

Why synthetic (not recorded market) inputs: the grading has to be
deterministic and re-runnable by a reviewer months later, so every scenario
is constructed so that the *correct* answer is forced by arithmetic, not by
market opinion. `tech_uptrend` has an unambiguous uptrend; `risk_rr_breach`
contains a RANGE buy whose payoff is 0.42R and which is at the same time the
largest line in the plan at 18% of the book — two facts, and it is the pair
that the risk-manager prompt requires action on. **There is no universal
reward:risk floor to cite anywhere in this file any more** — the owner
retired it on 2026-09-11 (docs/WORK.md item 1(d)): a breakout carries no
reward:risk judgement at all, and a range trade's real ratio is a ranking
input and a starter-size cap, never a pass mark. Grading never asks "did the
model agree with me about the market" — only "did it apply the rule the
prompt states", which means the rule the prompt states TODAY.

ONE scenario is an exception to "synthetic", deliberately: `pm_selection`
replays a frozen, verbatim pull of a real production session
(`fixtures/run_64290730_pm_input.json`). Selection quality is the one thing
a constructed candidate set cannot measure — hand-built tiers with planted
traps test whether a model finds the author's pattern, not whether it reads
evidence. It stays deterministic and re-runnable for the same reason the
others do: the fixture is frozen on disk and the correct answer is forced by
the run's own recorded arithmetic, not by anyone's market opinion.

**2026-09-14, owner rule — what an exam may be built from.** Everything
above describes how these scenarios were first built. The owner has since
ruled that an exam may contain RAW FACTS ONLY, fetched fresh from the original
public source, with every derived value recomputed by today's code and no
agent output filled in from a recording or from invented text
(`ops/model_policy/fixture_policy.py`, docs/INCIDENT_HISTORY.md). Under that
rule most scenarios in this file cannot run: each now carries either a
`fixture` the policy checks or a `blocked_reason` naming exactly what is
missing, and `benchmark_models.py` refuses both before any paid call. The
runnable, rule-compliant exams are in the "External-source seat exams"
section near the end.

No secrets live here: scenarios are prices, tickers, public filings, and two
QUARANTINED paper-account recordings.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from src.models import (
    OHLCV,
    NewsIntelligenceReport,
    Position,
    PortfolioDecision,
    ReasoningChain,
    TechAnalysisResult,
    TechnicalIndicators,
    TradeDecision,
)
from src.portfolio_constructor import PortfolioConstructor
from src.risk.constants import reward_risk_floor_applies
from src.risk.rules import RiskViolation

# The desk's own admission rules, replayed in plain Python with no LLM call.
# Imported at module level and safe from circularity: that module imports
# THIS one only inside its operator entry point.
from ops.model_policy import deterministic_selection as _deterministic_selection
from ops.model_policy import fixture_policy as _fixture_policy


# --------------------------------------------------------------------------
# Grading primitives
# --------------------------------------------------------------------------


@dataclass
class Check:
    """One graded assertion. `weight` is the share of the scenario's score."""

    name: str
    weight: float
    passed: bool
    detail: str = ""


def production_max_tokens(agent: str) -> int:
    """The `max_tokens` this agent actually runs with, read from settings.yaml.

    Hardcoding a benchmark-local ceiling here was a real bug, not a
    simplification. The first sweep capped the risk-manager scenario at
    16,000 while production allows 128,000, and two candidates were scored
    0.00 for "unparseable RiskVerdict" when their reasoning was in fact
    correct — they had simply spent the smaller budget on reasoning tokens
    and been cut off mid-JSON. `deepseek-v4-flash-0731` reported output
    tokens of exactly 16,000, which is the cap, not a coincidence.

    Reading the real value keeps the harness honest and self-maintaining: a
    model is now judged under the conditions it would actually run in, and
    retuning an agent's budget in settings.yaml retunes the benchmark too.
    """
    import yaml

    settings = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    llm = (yaml.safe_load(settings.read_text()) or {}).get("llm") or {}
    value = llm.get(f"{agent}_max_tokens")
    if not isinstance(value, int) or value <= 0:
        value = llm.get("max_tokens")
    if not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"no usable max_tokens for {agent} in {settings}")
    return value


@dataclass
class Scenario:
    """A benchmark case: build an agent, run it, grade the output.

    `agent_cls` is instantiated as `agent_cls(api_key, model, max_tokens,
    provider="openrouter")` by the runner — the same constructor the pipeline
    uses — and `invoke` calls the agent's real public entry point.
    """

    key: str
    role: str                      # which config/settings.yaml agent this informs
    agent_path: str                # "module:ClassName"
    invoke: Callable[[Any], Any]   # (agent) -> parsed output (or None)
    grade: Callable[[Any], list[Check]]
    description: str
    # Excluded from the default sweep. Used for the production-scale tech
    # batch, which is far too expensive to run against every candidate but
    # is the decisive latency measurement for the finalists.
    default: bool = True
    # Owner rule 2026-09-14. `fixture` names the manifest under fixtures/ this
    # exam is built on; `fixture_policy.check_fixture` must admit it.
    # `blocked_reason` says, with citations, why the exam cannot be a valid
    # test of the live seat today. Either one makes the benchmark refuse the
    # scenario before any paid call — see `refusal_reason`.
    fixture: str | None = None
    blocked_reason: str | None = None

    @property
    def max_tokens(self) -> int:
        return production_max_tokens(self.role)


def refusal_reason(scenario: "Scenario") -> str | None:
    """Why `scenario` must not run, or None when it may.

    The single gate `benchmark_models.py` consults. A blocked exam and an exam
    on a quarantined fixture are refused the same way: loudly, by name, before
    anything is spent.
    """
    if scenario.blocked_reason:
        return f"{scenario.key}: BLOCKED — {scenario.blocked_reason}"
    if scenario.fixture:
        verdict = _fixture_policy.check_fixture(_fixture_policy.FIXTURES_DIR / scenario.fixture)
        if not verdict.admissible:
            return f"{scenario.key}: {verdict.reason()}"
    return None


def _bars(
    symbol: str,
    start: float,
    step: float,
    n: int = 60,
    noise: tuple[float, ...] = (0.0, 0.4, -0.3, 0.2, -0.1),
) -> list[OHLCV]:
    """Deterministic bar series: linear drift + a fixed repeating wiggle.

    Fixed (not random) so two runs of the benchmark see byte-identical input
    and any score difference is attributable to the model alone.
    """
    out: list[OHLCV] = []
    day = date(2026, 5, 4)
    for i in range(n):
        close = start + step * i + noise[i % len(noise)]
        out.append(
            OHLCV(
                date=day + timedelta(days=i),
                open=round(close - 0.3, 2),
                high=round(close + 0.8, 2),
                low=round(close - 0.9, 2),
                close=round(close, 2),
                volume=40_000_000 + (i % 7) * 1_500_000,
            )
        )
    return out


# --------------------------------------------------------------------------
# 1. tech_analyst — see "External-source seat exams" below. The synthetic
#    3-symbol batch that lived here (invented bars and indicators) was
#    removed 2026-09-14: invented raw facts break the owner's exam rule.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 2. macro_analyst — light input, structured regime output
# --------------------------------------------------------------------------

_MACRO_SUMMARY = {
    "vix": {"current": 27.4, "change_pct": 18.2, "percentile_1y": 88},
    "treasury": {"ten_year": 4.62, "two_year": 4.41, "spread": 0.21, "inverted": False},
    "fed_funds_rate": {"current": 4.25, "trend": "holding"},
    "inflation": {"core_cpi_yoy": 3.4, "headline_cpi_yoy": 3.1, "trend": "sticky"},
    "unemployment": {"current": 4.4, "trend": "rising"},
    "credit_spread": {"current_bps": 412, "change_bps": 63, "trend": "widening"},
    "consumer_sentiment": {"current": 58.2, "trend": "falling"},
}

_MACRO_UNIVERSE = ["SPY", "QQQ", "XLE", "XLU", "XLP", "XLF", "SMH", "AAPL", "NVDA"]


def _macro_invoke(agent):
    analysis, _ = agent.analyze(
        macro_summary=_MACRO_SUMMARY, universe=_MACRO_UNIVERSE,
    )
    return analysis


def _macro_grade(analysis) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.35, analysis is not None, "MacroAnalysis validated"))
    if analysis is None:
        return checks

    # VIX at the 88th percentile, credit spreads widening 63bps, sentiment
    # falling, unemployment rising. "risk_on with 95% invested" is not a
    # defensible reading of that input — grade the direction, not a number.
    # (Macro no longer emits an invested %: owner fully-invested mandate,
    # 2026-09-17. Defensive now means the outlook, not a cash share.)
    outlook = getattr(analysis, "equity_outlook", None)
    checks.append(Check(
        "defensive_positioning", 0.35,
        outlook in ("bearish", "neutral"),
        f"equity_outlook={outlook} (stress input; expected not bullish)",
    ))

    guidance = getattr(analysis, "sector_guidance", None) or []
    checks.append(Check(
        "sector_guidance_present", 0.15,
        len(guidance) >= 2,
        f"{len(guidance)} sector calls",
    ))

    chain = getattr(analysis, "reasoning_chain", None)
    steps = [] if chain is None else [
        v for v in chain.model_dump().values() if isinstance(v, str)
    ]
    checks.append(Check(
        "cot_complete", 0.15,
        bool(steps) and all(s.strip() for s in steps),
        f"{sum(1 for s in steps if s.strip())}/{len(steps)} steps",
    ))
    return checks


# --------------------------------------------------------------------------
# 3. portfolio_manager — the synthesis step
# --------------------------------------------------------------------------

_PM_ANALYSES = [
    TechAnalysisResult(
        symbol="AAPL", rating="buy", conviction="high",
        entry_price=198.5, reference_target=214.0, stop_loss=191.5,
        # No overhead resistance — price is riding the upper band above a
        # rising MA stack after the March 50/200 cross, i.e. no one is
        # defending a level above; 191.5 is the old breakout shelf now
        # acting as support underneath. That's the "breakout" case per
        # TechAnalysisResult.setup_type: target is a measured-move
        # reference, managed by trailing rather than a defended level.
        setup_type="breakout",
        support_levels=[191.5],
        expected_horizon_sessions=15,
        reasoning="Uptrend intact above rising 20/50/200 stack.",
        reasoning_chain={
            "trend": "Above all MAs, 50 crossed 200 in March.",
            "momentum": "RSI 64, MACD histogram expanding.",
            "volatility": "ATR 3.45, price riding the upper band without piercing it.",
            "volume": "Volume +12% on advance days.",
            "support_resistance": "Support 191.5 at prior breakout shelf.",
        },
        thesis_invalid_if="Daily close below 191.5.",
        atr_14=3.45,
    ),
    TechAnalysisResult(
        symbol="XLE", rating="sell", conviction="medium",
        entry_price=81.2, reference_target=76.0, stop_loss=84.6,
        # Both sides of this short are named, defended levels — 84.6
        # resistance above (the stop) and 76.0 the next support (the
        # target) — so this is "range": a fixed target is meaningful and
        # thesis_progress/pace can be measured against it.
        setup_type="range",
        resistance_levels=[84.6],
        support_levels=[76.0],
        expected_horizon_sessions=10,
        reasoning="Downtrend below falling MA stack.",
        reasoning_chain={
            "trend": "Below 20/50/200, all declining.",
            "momentum": "RSI 32, MACD below signal.",
            "volatility": "ATR 1.62, closes pinned to the lower band.",
            "volume": "Distribution volume rising.",
            "support_resistance": "Next support 76.0.",
        },
        thesis_invalid_if="Reclaims 84.6 on a daily close.",
        atr_14=1.62,
    ),
]

_PM_POSITIONS = [
    Position(symbol="NVDA", qty=120, avg_entry=142.0, current_price=151.3,
             market_value=18_156.0, unrealized_pnl=1_116.0, sector="Technology"),
    Position(symbol="XLU", qty=90, avg_entry=73.9, current_price=74.8,
             market_value=6_732.0, unrealized_pnl=81.0, sector="Utilities"),
]

# Macro says be defensive. Cash is thin. Margin is OFF. Together these force
# a checkable answer: the book cannot grow gross exposure here.
_PM_MACRO = {
    "regime": "risk_off",
    "equity_outlook": "bearish",
    "position_guidance": {
        "reasoning": "VIX 88th percentile, credit spreads widening.",
    },
    "sector_guidance": [
        {"sector": "Energy", "stance": "underweight", "reason": "Crude breaking down."},
        {"sector": "Utilities", "stance": "overweight", "reason": "Defensive bid."},
    ],
    "reasoning_chain": {
        "rates_path": "Fed on hold, cuts priced out.",
        "growth_signal": "Unemployment rising, sentiment falling.",
        "inflation_read": "Core CPI sticky at 3.4%.",
        "risk_appetite": "Credit spreads +63bps.",
        "cross_asset": "Bid for duration and defensives.",
        "positioning": "Reduce gross, favour defensives.",
    },
}

_PM_TOTAL_VALUE = 42_000.0
_PM_CASH = 2_400.0  # ~5.7% cash, margin disabled


def _pm_invoke(agent):
    decision, _ = agent.decide(
        analyses=_PM_ANALYSES,
        positions=_PM_POSITIONS,
        macro_analysis=_PM_MACRO,
        cash_balance=_PM_CASH,
        total_value=_PM_TOTAL_VALUE,
        allow_margin=False,
    )
    return decision


def _pm_size_repr(target) -> str:
    """How a target stated its size, for a check's evidence string."""
    if target is None:
        return "None"
    if target.risk_allocation_pct is not None:
        return f"risk {target.risk_allocation_pct}%"
    return f"weight {target.target_weight_pct}%"


def _effective_weight_pct(target, analyses) -> float | None:
    """A target's notional weight, whichever field the model sized it with.

    Phase 2b (spec §2.1) made conviction a RISK allocation and left
    `target_weight_pct` optional so stored decisions still replay. These
    scenarios kept doing arithmetic on the notional field, which after that
    change is `None` for every risk-sized target — the grading path is
    outside the pytest suite (`ops/` is not collected), so nothing caught it.

    Risk converts to weight the same way `PortfolioConstructor` does:
    `risk_pct x entry / (entry - stop)`. Returns None when a risk-sized
    target has no usable stop in the fixture, so callers can exclude it
    rather than silently score it as zero.
    """
    if target.risk_allocation_pct is not None:
        if target.risk_allocation_pct == 0.0:
            return 0.0
        analysis = analyses.get(target.symbol)
        entry = getattr(analysis, "entry_price", None)
        stop = target.suggested_stop_price or getattr(analysis, "stop_loss", None)
        if not entry or not stop or entry <= stop:
            return None
        return target.risk_allocation_pct * entry / (entry - stop)
    return target.target_weight_pct or 0.0


def _pm_grade(decision: PortfolioDecision | None) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.30, decision is not None, "PortfolioDecision validated"))
    if decision is None:
        return checks

    chain = decision.reasoning_chain.model_dump()
    required = [k for k in chain if k not in ("continuity_check", "premortem_check")]
    checks.append(Check(
        "cot_complete", 0.15,
        all(str(chain[k]).strip() for k in required),
        f"{sum(1 for k in required if str(chain[k]).strip())}/{len(required)} steps",
    ))

    targets = decision.targets
    # Hard constraint from config/settings.yaml: allow_margin=false and only
    # 5.7% cash. Every new/added weight has to be funded by trimming, so the
    # total ADDED weight cannot exceed available cash. This is arithmetic the
    # prompt states, not a market view.
    held = {"NVDA": 18_156.0 / _PM_TOTAL_VALUE * 100, "XLU": 6_732.0 / _PM_TOTAL_VALUE * 100}
    by_symbol = {a.symbol: a for a in _PM_ANALYSES}
    weights = [(t, _effective_weight_pct(t, by_symbol)) for t in targets]
    # A risk-sized target with no stop in the fixture cannot be converted to a
    # notional weight. Excluding it is right: scoring it as 0% would read as
    # "freed cash" and could turn an over-committed book into a pass.
    sized = [(t, w) for t, w in weights if w is not None]
    added = sum(max(0.0, w - held.get(t.symbol, 0.0)) for t, w in sized)
    freed = sum(max(0.0, held.get(t.symbol, 0.0) - w) for t, w in sized)
    cash_pct = _PM_CASH / _PM_TOTAL_VALUE * 100
    checks.append(Check(
        "respects_cash_no_margin", 0.30,
        added <= freed + cash_pct + 1.0,  # 1pt tolerance for rounding
        f"added={added:.1f}% vs funded={freed + cash_pct:.1f}% "
        f"(cash {cash_pct:.1f}% + trims {freed:.1f}%)",
    ))

    # Single-name cap is a schema bound (TargetPosition le=20), so a breach
    # is dropped by _drop_invalid_targets rather than raised. Check it here
    # so "silently lost a target" cannot read as a clean pass.
    checks.append(Check(
        "targets_emitted", 0.05, bool(targets), f"{len(targets)} targets",
    ))

    # Macro says underweight Energy; TA says short XLE. A PM that opens a
    # long XLE target has contradicted both inputs without a stated catalyst.
    xle = next((t for t in targets if t.symbol == "XLE"), None)
    checks.append(Check(
        "no_signal_contradiction", 0.10,
        xle is None or xle.is_close or bool(xle.catalyst.strip()),
        f"XLE target={_pm_size_repr(xle)}",
    ))

    # The book is ~59% invested against a macro target of 55%, in a
    # documented risk-off regime, with the only actionable long being a
    # high-conviction AAPL buy. A PM that emits a single token target and
    # nothing else has not actually managed the book: it must either act on
    # the AAPL signal or say in portfolio_view why it is standing down.
    named = {t.symbol for t in targets}
    view = (decision.portfolio_view or "").upper()
    checks.append(Check(
        "acts_on_the_actionable_signal", 0.10,
        "AAPL" in named or "AAPL" in view,
        f"targets={sorted(named)} portfolio_view mentions AAPL={'AAPL' in view}",
    ))

    # Every target must carry a thesis — PortfolioConstructor and the
    # journal both render it, and an empty one is an unauditable order.
    checks.append(Check(
        "targets_have_thesis", 0.10,
        all((t.thesis or "").strip() for t in targets) and bool(targets),
        f"{sum(1 for t in targets if (t.thesis or '').strip())}/{len(targets)} with thesis",
    ))
    return checks


# Production-sized PM regression derived from the observed 11/17-target
# failures: enough candidates, holdings and memory text to exercise the real
# prompt shape rather than a toy two-symbol schema check.
_PM_PRODUCTION_SYMBOLS = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "AVGO", "AMD", "ORCL", "MU", "JPM", "GS", "V", "MA",
    "UNH", "LLY", "XOM", "CVX", "COST", "WMT", "CAT", "GE", "BA",
    "NEE", "VST", "CEG", "BRK-B",
]
_PM_PRODUCTION_ANALYSES = [
    TechAnalysisResult(
        symbol=symbol, rating="buy", conviction="medium",
        entry_price=100.0, stop_loss=94.0, reference_target=112.0,
        # Named support (94, the stop) and a named target level (112) on
        # both sides — this is the "range" case, not an unstructured
        # breakout.
        setup_type="range",
        support_levels=[94.0],
        resistance_levels=[112.0],
        expected_horizon_sessions=12,
        reasoning="Validated uptrend with positive momentum and volume.",
        reasoning_chain={
            "trend": "Above rising 20/50-day averages.",
            "momentum": "RSI and MACD positive.",
            "volatility": "ATR supports a bounded stop.",
            "volume": "Accumulation on advance days.",
            "support_resistance": "Support at 94, target at 112.",
        },
        thesis_invalid_if="Daily close below 94.",
    )
    for symbol in _PM_PRODUCTION_SYMBOLS
]
_PM_PRODUCTION_POSITIONS = [
    Position(
        symbol=symbol, qty=10, avg_entry=90.0, current_price=100.0,
        market_value=1_000.0, unrealized_pnl=100.0, sector="Diversified",
    )
    for symbol in _PM_PRODUCTION_SYMBOLS[:15]
]


# A macro read that VALIDATES as `src.models.MacroAnalysis`. Until
# 2026-09-14 this was `{"regime": "risk_on", "equity_outlook": "bullish"}`:
# wrong regime literal (`risk_on` vs `risk-on`) and missing every required
# field. `PortfolioManagerAgent` catches the parse failure and continues
# with no macro seat verdict, so the scenario silently ran without the macro
# evidence production always supplies. Guarded by
# `tests/test_model_policy_harness_imports.py`.
_PM_PRODUCTION_MACRO = {
    "reasoning_chain": {
        "volatility_analysis": "VIX in the mid-teens and drifting lower.",
        "yield_curve_analysis": "2s10s modestly positive and steepening.",
        "monetary_policy_analysis": "Fed funds unchanged; no near-term move priced.",
        "inflation_labor_credit": "CPI easing, unemployment stable, HY spreads tight.",
        "cross_signal_synthesis": "Volatility, curve and credit agree on risk appetite.",
        "sector_implications": "Cyclicals and technology favoured over defensives.",
    },
    "regime": "risk-on",
    "confidence": "medium",
    "equity_outlook": "bullish",
    "position_guidance": {
        "reasoning": "Constructive regime; lean net long.",
    },
    "summary": "Risk-on regime with supportive credit and easing volatility.",
}


def _pm_production_invoke(agent):
    decision, _ = agent.decide(
        analyses=_PM_PRODUCTION_ANALYSES,
        positions=_PM_PRODUCTION_POSITIONS,
        macro_analysis=_PM_PRODUCTION_MACRO,
        cash_balance=45_000.0, total_value=100_000.0, allow_margin=False,
        weekly_narrative="Seven-day portfolio narrative. " * 80,
        macro_trajectory="Regime trajectory evidence. " * 80,
        active_state_changes="Current state change. " * 80,
        pm_recent_decisions="Prior grounded target. " * 80,
        rm_recent_verdicts="Prior risk verdict. " * 80,
    )
    return decision


def _pm_production_grade(decision: PortfolioDecision | None) -> list[Check]:
    checks = [Check("parsed_and_grounded", 0.55, decision is not None,
                    "checked PortfolioDecision parses and is grounded "
                    "against live data")]
    if decision is None:
        return checks
    held = {p.symbol for p in _PM_PRODUCTION_POSITIONS}
    phantom_exits = [
        target.symbol for target in decision.targets
        if target.is_close and target.symbol not in held
    ]
    checks.append(Check(
        "no_phantom_exits", 0.20, not phantom_exits,
        f"phantom exits={phantom_exits}",
    ))
    checks.append(Check(
        "actionable_book", 0.15, len(decision.targets) >= 3,
        f"{len(decision.targets)} grounded targets",
    ))
    checks.append(Check(
        "provenance_present", 0.10,
        bool(decision.targets) and all(target.provenance for target in decision.targets),
        f"{sum(bool(t.provenance) for t in decision.targets)}/{len(decision.targets)} targets",
    ))
    return checks


# --------------------------------------------------------------------------
# 3c. portfolio_manager — SELECTION quality against a REAL opportunity set
# --------------------------------------------------------------------------
#
# WHAT THIS MEASURES: given the evidence a real morning actually produced,
# does the model choose the candidates the evidence supports?
#
# WHAT IT DOES NOT MEASURE: profitability. Nobody knows which of these picks
# would have made money, and this scenario does not pretend to. Every check
# below is answerable from the run's own recorded numbers — the ratings,
# convictions, setup types and evidence the seats emitted — put through the
# desk's OWN CURRENT admission rules (`deterministic_selection.evaluate`,
# the shadow of production `candidate_eligibility`). A model that scores 1.00
# here has selected in line with the evidence it was shown; whether that
# evidence was RIGHT about the market is a separate, unmeasured question.
#
# **REBUILT 2026-09-14 (docs/WORK.md PM-gate item 8).** Every check here used
# to key off `analyst reward/risk >= 1.5`. That floor was retired on
# 2026-09-11 (item 1(d)) and this scenario went on grading against it, so it
# was scoring obedience to a deleted rule. The admitted set now comes from
# the desk's own current admission rules instead.
#
# **RE-POINTED 2026-09-14 (docs/WORK.md item 72, CLOSED).** The day this
# scenario first replayed, `run-64290730` (2026-09-01), carried no
# `computed_levels` on any of its 59 rows, so the structural reward:risk the
# live gate reads was None for every name. Measured on that file: the live
# `candidate_eligibility`, handed those real (all-None) ratios, admits 12
# names — this grader admitted 25, so 13 of its "admitted" names were ones
# production would refuse as unmeasurable. The earlier note here that "the
# admitted set does not depend on it" held only for the shadow in
# `deterministic_selection`, which reads the analyst's ratio. That file is
# kept only for three older audits (`LEVEL_LESS_SELECTION_FIXTURE`).
#
# WHY IT IS NOT SYNTHETIC. The first draft of this scenario hand-built ~30
# candidates in tiers with planted "traps". That measures whether a model can
# find a pattern the author planted, which is not the same thing as reading
# evidence — real signals carry real ambiguity, invented ones carry the
# author's assumptions. The fixture is therefore a verbatim pull of
# `run-bba4d4f3`, the 2026-09-02 13:31 UTC session, from the read-only
# Mission Control API: every analysis row is the API's `tech` payload for
# that symbol, in the order the run's own recorded prompt presented them;
# earnings are the API's per-symbol payloads; macro and news are those seats'
# recorded responses; account, positions, memory layers, evening insights and
# the BUY-eligibility universe are read out of the run's recorded
# portfolio_manager prompt. See the fixture's `_provenance` block, whose
# `fidelity` figures are measured, not asserted: rendered through today's
# `build_user_message`, 12 of 22 shared sections are byte-identical and all 64
# technical rows match the recorded prompt's rating, conviction, entry, stop
# and target. The rest differ because the renderer changed after the run, or
# because the section is computed live (PMFacts, portfolio heat, company
# profiles, proposal conversion) or would need SEC source URLs the payloads do
# not carry (smart-money findings — so AUGO, MAIR and RSG lose that one
# source in the registry here).
#
# THE DAY, measured. 91 candidates considered, 64 technical reads, 34
# actionable (14 breakout / 20 range), 10 of them bearish. **63 of the 64
# rows carry `computed_levels`** (the one without is MRVL, rated neutral), and
# `PortfolioConstructor.real_reward_risk_preview` returns a ratio for every
# one of the 34 actionable names. The live PM proposed nine targets — eight
# longs and an UNH short — and the risk seat approved them (category
# `rr_fail`, one allocation halved), yet the funnel recorded zero proposed
# orders, zero fills and `decision_state=no_proposal`. Why an approved plan
# produced no order is NOT established by this pull. Nothing below grades
# against the live output.
#
# WHAT "QUALIFIED" MEANS NOW, AND WHY IT IS NOT A NUMBER THIS FILE CHOOSES.
# The admitted set is whatever `deterministic_selection.evaluate` admits: the
# desk's own stated rules — current technical coverage, an actionable rating,
# BUY-eligibility for a long, a MEASURABLE payoff (its size no longer gates)
# or a dated catalyst row, and a net independent source score of at least 1
# so the §9.4 agreement ceiling leaves a rung to stand on. On this fixture it
# admits 25 of the 64 read names, exactly one of them a short (FLNC,
# sell/medium, net +1), and production `candidate_eligibility` handed the
# real structural ratios admits the IDENTICAL 25
# (tests/test_pm_selection_scenario.py pins that). The other nine bearish
# names, the live PM's UNH short among them, are refused by the net-evidence
# rule. Nothing here is tuned, and this file introduces no threshold of its
# own.
#
# WHY IT STILL SEPARATES EVIDENCE FROM FAMILIARITY. Nine bearish names the
# desk refuses, one it admits, and a live plan that shorted a refused one. What
# is NOT gradeable is famousness: of the five famous names with a read (AAPL,
# GOOGL, MSFT, NVDA, SPY), AAPL, MSFT and NVDA are ADMITTED by the desk's own
# rules, AAPL and NVDA on breakouts the prompt forbids judging on reward:risk
# at all. So `familiarity_bias` survives as a weight-0 DIAGNOSTIC — the share
# is reported on every run, and it scores nothing, because the desk has no
# rule against picking a well-evidenced mega-cap and the score must not invent
# one.
#
# THE CHECKS, and why they weigh what they weigh:
#   parsed_and_grounded          0.10  survived the grounding validator
#   opens_a_position             0.10  did anything at all
#   selection_from_eligible_set  0.25  every pick is one the desk admits
#   takes_an_eligible_short      0.25  took one of the two admitted shorts
#   familiarity_bias             0.00  DIAGNOSTIC ONLY, reported not scored
# The two 0.25s are the measurement; the two 0.10s are a guard around it.
# **The surviving shares are deliberately NOT rescaled to sum to 1.0.**
# `benchmark_models._run_scenario` divides by the actual total weight, so the
# four scoring checks keep exactly the proportions they had; rescaling them
# would mean typing four new weights, which is precisely the invented number
# this rewrite exists to remove. A model that opens nothing therefore scores
# 0.10/0.70 — below any book of real picks — because the two selection checks
# are gated on having made a pick. Inaction is the failure being studied, so
# it must not collect credit for rules it never had the chance to break.
#
# TWO CHECKS WERE DELETED RATHER THAN RESTATED, because neither could be
# stated honestly against the desk that exists:
#   * `rr_floor_discipline` (was 0.15) graded "every sub-floor pick names a
#     catalyst". The live rule (`PortfolioManagerAgent._apply_subfloor_
#     catalyst_rule`) requires a catalyst ONLY when a range trade's real
#     payoff is UNMEASURABLE; a thin-but-measurable one is kept, uncited, and
#     capped at starter size. There is no longer any rule that a sub-floor
#     pick must cite anything, so there is nothing left to grade.
#   * `selection_from_qualified_set`'s old majority-of-picks-clear-1.5 bar is
#     gone with the floor. Its replacement above is a different check with
#     the same weight, not a renamed one: it asks whether the desk would have
#     ADMITTED each pick, which is a rule, not a ratio.

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_SELECTION_FIXTURE = _FIXTURES_DIR / "run_bba4d4f3_pm_input.json"
# The 2026-09-01 pull this scenario ran on until 2026-09-14. NOT graded any
# more: none of its 59 rows carries `computed_levels`, so the structural
# reward:risk the live admission gate reads is None for every name, and the
# live gate given those ratios admits 12 names where this grader admitted 25
# (measured 2026-09-14, docs/INCIDENT_HISTORY.md). Kept ONLY because three
# older audits pin their write-ups to that exact day
# (tests/test_deterministic_selection.py, tests/test_analyst_verdict.py,
# tests/test_pm_input_shape.py); they load it through this constant.
LEVEL_LESS_SELECTION_FIXTURE = _FIXTURES_DIR / "run_64290730_pm_input.json"

# The famous names the familiarity diagnostic is about. Fixed list, not derived:
# deriving "famous" from the data would let the fixture redefine the very
# thing being measured. Only those with a technical read this session can be
# targeted at all; the rest are listed so a future fixture that DOES cover
# them is scored the same way.
_SELECTION_FAMOUS = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "QQQ", "SPY")


def load_frozen_selection(path: Path) -> SimpleNamespace:
    """A frozen production pull, parsed. Raises rather than degrading quietly.

    Loaded from disk instead of inlined as Python literals for one reason:
    every value has to be provably the API's, not a transcription of it. The
    file is the record; the module only reads it. Returns `raw` (the dict),
    `analyses`, `positions` and `news` as the live models.
    """
    if not path.exists():
        raise RuntimeError(f"selection fixture missing: {path}")
    raw = json.loads(path.read_text())
    # The recording predates `current_until` (2026-10-16), which the earnings
    # provider now stamps from the SEC submissions list at decision time. The
    # recorded session served each of these as the company's latest periodic
    # report, so each was current ON the recorded day — and a replay judges
    # currency as of that day (`asof=session_day`), never the day it runs.
    recorded_day = _deterministic_selection.session_day(raw).isoformat()
    for entry in raw.get("earnings_analyses") or []:
        entry.setdefault("current_until", recorded_day)
    return SimpleNamespace(
        raw=raw,
        analyses=[TechAnalysisResult.model_validate(r) for r in raw["analyses"]],
        positions=[Position.model_validate(r) for r in raw["positions"]],
        news=NewsIntelligenceReport.model_validate(raw["news_intel"]),
    )


_FROZEN = load_frozen_selection(_SELECTION_FIXTURE)
_SELECTION = _FROZEN.raw
_SELECTION_ANALYSES = _FROZEN.analyses
_SELECTION_POSITIONS = _FROZEN.positions
_SELECTION_NEWS = _FROZEN.news
_SELECTION_BY_SYMBOL = {a.symbol: a for a in _SELECTION_ANALYSES}
_SELECTION_TOTAL_VALUE = _SELECTION["account"]["total_value"]
_SELECTION_HELD_WEIGHT_PCT = {
    p.symbol: p.market_value / _SELECTION_TOTAL_VALUE * 100
    for p in _SELECTION_POSITIONS
}

# `risk_reward` is a computed property on TechAnalysisResult — Python's
# arithmetic over the analyst's own entry/stop/target, never the analyst's
# claim about its own ratio. None for a neutral rating or malformed geometry.
_SELECTION_RR = {a.symbol: a.risk_reward for a in _SELECTION_ANALYSES}
_SELECTION_ACTIONABLE = {a.symbol for a in _SELECTION_ANALYSES if a.rating != "neutral"}
_SELECTION_BEARISH = {
    a.symbol for a in _SELECTION_ANALYSES if a.rating in ("sell", "strong_sell")
}
_SELECTION_BREAKOUT = {
    a.symbol for a in _SELECTION_ANALYSES
    if not reward_risk_floor_applies(a.setup_type)
}

#: symbol -> the STRUCTURAL reward:risk the live admission gate reads — the
#: constructor's derived-target, noise-widened ratio, never the analyst's.
#: Exactly what `pipeline_stages` hands `candidate_eligibility` in production.
_SELECTION_STRUCTURAL_RR = {
    a.symbol: PortfolioConstructor().real_reward_risk_preview(
        a, "short" if a.rating in ("sell", "strong_sell") else "long",
    )
    for a in _SELECTION_ANALYSES if a.rating != "neutral"
}

# THE ADMITTED SET, computed by the desk's own rules rather than restated
# here. `deterministic_selection.evaluate` is the maintained shadow of
# production `PortfolioManagerAgent.candidate_eligibility` and is itself
# pinned against `config/settings.yaml` by `tests/test_deterministic_
# selection.py`, so this scenario cannot drift from the desk without that
# test failing first. It runs no LLM call and touches no network.
_SELECTION_ELIGIBILITY = {
    row["symbol"]: row for row in _deterministic_selection.evaluate(
        _SELECTION, _SELECTION_ANALYSES, _SELECTION_POSITIONS, _SELECTION_NEWS,
    )
}
#: symbol -> the ONE direction the desk admits it in ("long" / "short"),
#: derived from the analyst's own rating exactly as production derives it.
_SELECTION_ELIGIBLE_DIRECTION = {
    symbol: row["direction"]
    for symbol, row in _SELECTION_ELIGIBILITY.items() if row["eligible"]
}
_SELECTION_ELIGIBLE = set(_SELECTION_ELIGIBLE_DIRECTION)
_SELECTION_ELIGIBLE_SHORTS = {
    symbol for symbol, direction in _SELECTION_ELIGIBLE_DIRECTION.items()
    if direction == "short"
}
# Famous AND admitted. Not "famous and weak" — that phrase needed the retired
# floor to define "weak". Reported, never scored.
_SELECTION_FAMOUS_ELIGIBLE = {
    symbol for symbol in _SELECTION_FAMOUS if symbol in _SELECTION_ELIGIBLE
}


def _selection_blocked_by(symbol: str, direction: str) -> str:
    """Why the desk would refuse this pick, in its own words."""
    row = _SELECTION_ELIGIBILITY.get(symbol)
    if row is None:
        return "R1 no current technical coverage"
    if not row["eligible"]:
        return "; ".join(row["blocked_by"])
    if row["direction"] != direction:
        return (
            f"admitted {row['direction']}, not {direction} "
            f"(rating {row['rating']})"
        )
    return ""

# The shape of the day, asserted at import. These numbers are what the
# checks below mean; if a fixture edit moves any of them, the benchmark has
# silently become a different test and must fail loudly instead. The import
# is covered by tests/test_model_policy_harness_imports.py, so this runs in
# CI without a single LLM call.
_SELECTION_SHAPE = {
    "analysed": len(_SELECTION_ANALYSES),
    "actionable": len(_SELECTION_ACTIONABLE),
    "breakout_actionable": len(_SELECTION_BREAKOUT & _SELECTION_ACTIONABLE),
    "eligible": len(_SELECTION_ELIGIBLE),
    "eligible_shorts": len(_SELECTION_ELIGIBLE_SHORTS),
    "bearish_actionable": len(_SELECTION_BEARISH),
    "famous_eligible": len(_SELECTION_FAMOUS_ELIGIBLE),
    "positions_held": len(_SELECTION_POSITIONS),
    "with_computed_levels": sum(1 for a in _SELECTION_ANALYSES if a.computed_levels),
    "actionable_with_structural_rr": sum(
        1 for v in _SELECTION_STRUCTURAL_RR.values() if v is not None
    ),
}
_SELECTION_SHAPE_EXPECTED = {
    "analysed": 64,
    "actionable": 34,
    "breakout_actionable": 14,
    "eligible": 25,
    "eligible_shorts": 1,
    "bearish_actionable": 10,
    "famous_eligible": 3,
    "positions_held": 5,
    # The reason this fixture replaced run-64290730: the quantity the live
    # admission gate reads exists here. If either falls, the scenario has
    # silently gone back to a level-less day.
    "with_computed_levels": 63,
    "actionable_with_structural_rr": 34,
}
if _SELECTION_SHAPE != _SELECTION_SHAPE_EXPECTED:
    raise RuntimeError(
        "run-bba4d4f3 selection fixture no longer has the shape this scenario "
        f"grades: got {_SELECTION_SHAPE}, expected {_SELECTION_SHAPE_EXPECTED}"
    )


def _pm_selection_invoke(agent):
    account = _SELECTION["account"]
    memory = _SELECTION["memory"]
    decision, _ = agent.decide(
        analyses=_SELECTION_ANALYSES,
        positions=_SELECTION_POSITIONS,
        macro_analysis=_SELECTION["macro_analysis"],
        cash_balance=account["cash_balance"],
        reserve_balance=account["reserve_balance"],
        total_value=account["total_value"],
        news_intel=_SELECTION_NEWS,
        earnings_analyses=_SELECTION["earnings_analyses"],
        asof=_deterministic_selection.session_day(_SELECTION),
        recent_performance=_SELECTION["recent_performance"],
        position_history=_SELECTION["position_history"],
        yesterday_insights=_SELECTION["yesterday_insights"],
        weekly_narrative=memory["weekly_narrative"],
        macro_trajectory=memory["macro_trajectory"],
        active_state_changes=memory["active_state_changes"],
        rm_recent_verdicts=memory["rm_recent_verdicts"],
        pm_recent_decisions=memory["pm_recent_decisions"],
        projected_portfolio=memory["projected_portfolio"],
        calibration_note=memory["calibration_note"],
        recent_missed_lessons=memory["recent_missed_lessons"],
        recent_loss_pits=memory["recent_loss_pits"],
        allow_margin=account["allow_margin"],
        session_type=account["session_type"],
        allowed_buy_symbols=set(_SELECTION["allowed_buy_symbols"]),
        transient_admitted_symbols=set(_SELECTION["transient_admitted_symbols"]),
    )
    return decision


def _selection_opens_or_adds(decision: PortfolioDecision) -> list:
    """The targets that are a SELECTION, not book management.

    A close, a trim, or a restatement of a position the model inherited is
    not a choice about which candidate to back, so scoring it would credit
    or blame a model for what the fixture handed it. Held names count only
    when the target raises their weight, using the same risk-to-notional
    conversion `PortfolioConstructor` does (`_effective_weight_pct`). The
    0.5pp band absorbs the rounding between a risk allocation and the weight
    it implies — the same tolerance `_pm_grade` applies to its funding sum.
    """
    picks = []
    for target in decision.targets:
        if target.is_close:
            continue
        held_pct = _SELECTION_HELD_WEIGHT_PCT.get(target.symbol)
        if held_pct is None:
            picks.append(target)
            continue
        weight = _effective_weight_pct(target, _SELECTION_BY_SYMBOL)
        if weight is not None and weight > held_pct + 0.5:
            picks.append(target)
    return picks


def _selection_evidence_repr(target) -> str:
    """`SYM long rr=1.67 buy/high` — the real evidence behind one pick.

    A breakout reports `rr=n/a[breakout]` rather than its arithmetic ratio,
    for the same reason the PM prompt renders it that way: there is no
    overhead level to measure a reward against, so the number would be an
    invented one and printing it invites a reader to judge on it.
    """
    analysis = _SELECTION_BY_SYMBOL.get(target.symbol)
    if analysis is None:
        return f"{target.symbol} {target.direction} NO-COVERAGE"
    rr = (
        "n/a[breakout]" if target.symbol in _SELECTION_BREAKOUT
        else _SELECTION_RR.get(target.symbol)
    )
    return (
        f"{target.symbol} {target.direction} rr={rr} "
        f"{analysis.rating}/{analysis.conviction}"
    )


def _pm_selection_grade(decision: PortfolioDecision | None) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check(
        "parsed_and_grounded", 0.10, decision is not None,
        "checked PortfolioDecision parses and is grounded against live data",
    ))
    if decision is None:
        return checks

    picks = _selection_opens_or_adds(decision)
    pick_symbols = [t.symbol for t in picks]
    evidence = "; ".join(_selection_evidence_repr(t) for t in picks) or "none"

    # On this day 34 actionable signals and a nine-target plan the risk seat
    # approved ended with zero proposed orders and zero fills. A model that
    # opens nothing has reproduced that outcome, whatever its reasoning says.
    checks.append(Check(
        "opens_a_position", 0.10, bool(picks),
        f"{len(picks)} opening/adding target(s): {evidence}",
    ))

    # THE selection signal. Did every pick come out of the set the desk's own
    # rules admit? Purity, not a majority: each of these is a HARD refusal in
    # production (no coverage, a neutral rating, not BUY-eligible, no
    # measurable payoff and no dated catalyst row, or a net evidence score
    # with no agreement rung), so a pick outside the set is a proposal that
    # could not have become a trade. The old majority bar existed because a
    # sub-floor-with-catalyst pick was legal-but-lesser under a floor that no
    # longer exists; with a set built out of refusals, "mostly admitted" is
    # not a meaningful standard.
    ineligible = [
        f"{t.symbol}({_selection_blocked_by(t.symbol, t.direction)})"
        for t in picks
        if _selection_blocked_by(t.symbol, t.direction)
    ]
    checks.append(Check(
        "selection_from_eligible_set", 0.25,
        bool(picks) and not ineligible,
        f"{len(picks) - len(ineligible)}/{len(picks)} picks are admitted by "
        f"the desk's own rules; refused: {ineligible}; admitted set was "
        f"{sorted(_SELECTION_ELIGIBLE)}"
        if picks else
        "no picks to judge — an empty book selects from nothing",
    ))

    # The specific failure this scenario exists to detect. Ten validated
    # bearish candidates were on offer; the desk's own rules admit exactly one
    # — FLNC (sell/medium, net evidence +1). The other nine are refused
    # deterministically by the §9.4 net-evidence rule, including UNH, the one
    # short the live PM did propose, so this check credits only a short
    # production would actually have placed. Shorts are a first-class, prompt-
    # documented instrument here (`direction: "short"`, its own caps and
    # borrow gate) and are not blocked by the cash-only account, so declining
    # the admitted one is a choice, not a constraint.
    shorts = [t for t in picks if t.direction == "short"]
    eligible_shorts = [
        t.symbol for t in shorts if t.symbol in _SELECTION_ELIGIBLE_SHORTS
    ]
    other_shorts = [
        t.symbol for t in shorts if t.symbol not in _SELECTION_ELIGIBLE_SHORTS
    ]
    checks.append(Check(
        "takes_an_eligible_short", 0.25, bool(eligible_shorts),
        f"admitted shorts taken={eligible_shorts} other shorts={other_shorts} "
        f"(available: {sorted(_SELECTION_ELIGIBLE_SHORTS)})",
    ))

    # DIAGNOSTIC, WEIGHT 0 — reported on every run, scores nothing. The owner
    # asked for the familiarity rate as a number, and it is still worth
    # watching across models and repeats. It is no longer a pass/fail because
    # it cannot be one honestly: three of the famous names with a read this
    # session (AAPL, MSFT, NVDA) are ADMITTED by the desk's own rules, two of
    # them breakouts the PM prompt explicitly forbids judging on reward:risk. Failing a model for
    # taking a well-evidenced name the desk permits would be scoring a rule
    # this desk does not have. What the number still shows is the shape of a
    # book: reaching for the names everyone knows while admitted candidates
    # sit untaken is what selecting on familiarity looks like from outside.
    famous_picks = [s for s in pick_symbols if s in _SELECTION_FAMOUS_ELIGIBLE]
    passed_over = sorted(_SELECTION_ELIGIBLE - set(pick_symbols))
    famous_share = len(famous_picks) / len(picks) if picks else 0.0
    checks.append(Check(
        "familiarity_bias", 0.0,
        not famous_picks,
        f"DIAGNOSTIC (not scored): famous picks {len(famous_picks)}/{len(picks)} "
        f"({famous_share * 100:.0f}%)={famous_picks}; "
        f"admitted candidates passed over={passed_over}",
    ))
    return checks


# --------------------------------------------------------------------------
# 4. risk_manager — the last LLM gate before deterministic Python
# --------------------------------------------------------------------------


def _pm_reasoning_chain() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="Risk-off but selective longs still warranted.",
        news_check="No single-name headline risk found.",
        earnings_check="No earnings inside 3 sessions.",
        signal_conflicts="TA and macro broadly aligned.",
        sizing_logic="Sized to conviction.",
        portfolio_balance="Tech-heavy but within cap.",
        cash_target="Holding 12% cash.",
    )


# The BUY below is deliberately indefensible on the arithmetic the RM prompt
# tells it to audit:
#   MU:  entry 118.0, stop 112.0 (risk 6.0), target 120.5 (reward 2.5) => 0.42R
#        on a RANGE setup, AND sized at 18% of the book — the largest line in
#        the plan.
# **The thin ratio alone is NOT the finding, and has not been since 2026-09-11
# (docs/WORK.md item 1(d)).** The prompt now says in terms that "below 1.5" is
# not by itself grounds to refuse, and that Python has already capped a
# sub-floor range target at starter size before the risk manager ever sees it.
# What makes this plan indefensible is the PAIR: a payoff that thin arriving
# as the biggest line in the book is a sizing fact the prompt still requires
# to be audited, and a state the live cap should have made impossible.
# A competent risk manager must not approve this untouched. Everything else
# in the plan is clean, so a model that rejects the whole plan for the wrong
# reason still has to name MU in its chain to score the rr_audit check.
_RISK_DECISION = PortfolioDecision(
    reasoning_chain=_pm_reasoning_chain(),
    decisions=[
        TradeDecision(
            action="BUY", symbol="MU", allocation_pct=18.0,
            entry_price=118.0, stop_loss=112.0, take_profit=120.5,
            reasoning="Memory cycle turning; adding aggressively.",
        ),
        TradeDecision(
            action="BUY", symbol="XLU", allocation_pct=5.0,
            entry_price=74.8, stop_loss=72.4, take_profit=80.2,
            reasoning="Defensive ballast, 2.25R.",
        ),
        TradeDecision(
            action="SELL", symbol="XLE", allocation_pct=100.0,
            entry_price=81.2, stop_loss=84.6, take_profit=76.0,
            reasoning="Close the energy position entirely.",
        ),
    ],
    portfolio_view="Rotating from energy into memory and defensives.",
)

_RISK_POSITIONS = [
    Position(symbol="NVDA", qty=120, avg_entry=142.0, current_price=151.3,
             market_value=18_156.0, unrealized_pnl=1_116.0, sector="Technology"),
    Position(symbol="XLE", qty=95, avg_entry=88.4, current_price=81.2,
             market_value=7_714.0, unrealized_pnl=-684.0, sector="Energy"),
]


# Holding ages and system-performance state. RiskStage always passes both
# (rebuilding them on the resume lane), so a scenario that omitted them would
# be measuring the seat in a configuration production never runs. Neither
# position is inside the <5d protection period and `in_drawdown` is false, so
# the arithmetic this scenario grades is unchanged — the extra evidence is
# present and simply gives nothing away.
_RISK_POSITION_HISTORY = {
    "NVDA": {"days_held": 30, "entry_date": "2026-07-13"},
    "XLE": {"days_held": 46, "entry_date": "2026-06-27"},
}
_RISK_RECENT_PERFORMANCE = {
    "rolling_5d_pct": -0.8,
    "rolling_20d_pct": 1.4,
    "in_drawdown": False,
    "trailing_days": 24,
}


def _risk_invoke(agent):
    verdict, _ = agent.review(
        portfolio_decision=_RISK_DECISION,
        positions=_RISK_POSITIONS,
        macro_summary=_MACRO_SUMMARY,
        rule_violations=[],
        total_value=42_000.0,
        cash=5_040.0,
        position_history=_RISK_POSITION_HISTORY,
        recent_performance=_RISK_RECENT_PERFORMANCE,
    )
    return verdict


def _risk_grade(verdict) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.25, verdict is not None, "RiskVerdict validated"))
    if verdict is None:
        return checks

    chain = verdict.reasoning_chain.model_dump()
    checks.append(Check(
        "cot_complete", 0.10,
        all(str(v).strip() for v in chain.values()),
        f"{sum(1 for v in chain.values() if str(v).strip())}/{len(chain)} steps",
    ))

    # THE discriminating check: did it act on the 0.42R, 18%-of-book BUY?
    # Acting means any of: not approving, modifying MU, or scaling buys down.
    mu_mods = [m for m in verdict.modifications if m.symbol.upper() == "MU"]
    acted = (
        (not verdict.approved)
        or bool(mu_mods)
        or verdict.scale_all_buys < 1.0
    )
    checks.append(Check(
        "catches_rr_breach", 0.40, acted,
        f"approved={verdict.approved} mu_mods={len(mu_mods)} "
        f"scale_all_buys={verdict.scale_all_buys}",
    ))

    # And did it say WHY in the audit trail — the rr_audit step must actually
    # mention the offending symbol, not just assert "all fine". Word-boundary
    # match: a substring test would score "must"/"multiple"/"cumulative" as a
    # hit and hand every model a free point.
    rr_text = str(chain.get("rr_audit", ""))
    checks.append(Check(
        "names_offender_in_rr_audit", 0.15,
        re.search(r"\bMU\b", rr_text) is not None,
        f"rr_audit={rr_text[:110]!r}",
    ))

    checks.append(Check(
        "reason_category_not_clean", 0.10,
        verdict.reason_category != "clean" or verdict.approved is False,
        f"reason_category={verdict.reason_category}",
    ))
    return checks


# --------------------------------------------------------------------------
# 4b. risk_manager — the two rules the 2026-08-13 audit gave it the evidence
#     for. Separate scenario, not extra checks on `risk_rr_breach`: that one
#     grades the R/R gate and mixing a second rule into it would make a
#     failure unattributable.
#
#     Both breaches below are forced by arithmetic the prompts already state,
#     the same discipline as every other scenario here:
#
#       - `in_drawdown=true` (5d -4.6% is past the -3% trigger) requires PM
#         to halve every new BUY. PM sized MSFT at the full 12% high-
#         conviction base and SAID SO in `sizing_logic`. Nothing
#         deterministic enforces this rule, so RM is the only check on it.
#       - AMD is 2 days held, inside the <5d protection period, and the SELL
#         cites a Tech downgrade — which portfolio_manager.md names
#         explicitly as NOT sufficient.
#
#     MSFT's R/R is 2.5 (entry 100 / stop 94 / target 115) — neither thin nor
#     inside the 3.0 "don't nick it" band the prompt still names, so a model
#     cannot score here by re-running the payoff audit from `risk_rr_breach`.
# --------------------------------------------------------------------------

_DRAWDOWN_PM_CHAIN = ReasoningChain(
    macro_filter="Regime risk-on, target invested 75%.",
    news_check="No HIGH state changes on held or proposed names.",
    earnings_check="Nothing queued inside the window.",
    signal_conflicts="Tech and macro aligned on MSFT.",
    sizing_logic=(
        "MSFT at the full 12% high-conviction base — 4/4 alignment and "
        "R/R 2.5, so no reduction applied."
    ),
    portfolio_balance="Technology 34%, inside the 40% cap.",
    cash_target="Cash 14% after the adds, inside the risk-on band.",
    continuity_check="Consistent with the week's constructive stance.",
    premortem_check=(
        "Bear case on MSFT: the AI capex trade is crowded and a soft "
        "Azure print would take the multiple down. Falsifier: a close "
        "below the 20-day on rising volume."
    ),
)

_DRAWDOWN_DECISION = PortfolioDecision(
    reasoning_chain=_DRAWDOWN_PM_CHAIN,
    decisions=[
        TradeDecision(
            action="BUY", symbol="MSFT", allocation_pct=12.0,
            entry_price=100.0, stop_loss=94.0, take_profit=115.0,
            reasoning="High conviction, 4/4 aligned, R/R 2.5.",
        ),
        TradeDecision(
            action="SELL", symbol="AMD", allocation_pct=100.0,
            entry_price=162.0, stop_loss=155.0, take_profit=180.0,
            reasoning="Tech rating downgraded to neutral today.",
        ),
    ],
    portfolio_view="Adding quality tech, cutting the weak AMD entry.",
)

_DRAWDOWN_POSITIONS = [
    Position(symbol="NVDA", qty=120, avg_entry=142.0, current_price=151.3,
             market_value=18_156.0, unrealized_pnl=1_116.0, sector="Technology"),
    Position(symbol="AMD", qty=40, avg_entry=162.0, current_price=159.4,
             market_value=6_376.0, unrealized_pnl=-104.0, sector="Technology"),
]

_DRAWDOWN_POSITION_HISTORY = {
    "NVDA": {"days_held": 30, "entry_date": "2026-07-13"},
    "AMD": {"days_held": 2, "entry_date": "2026-08-10"},
}

_DRAWDOWN_RECENT_PERFORMANCE = {
    "rolling_5d_pct": -4.6,     # past the -3% trigger
    "rolling_20d_pct": -2.1,
    "in_drawdown": True,
    "trailing_days": 24,
}


def _risk_drawdown_invoke(agent):
    verdict, _ = agent.review(
        portfolio_decision=_DRAWDOWN_DECISION,
        positions=_DRAWDOWN_POSITIONS,
        macro_summary=_MACRO_SUMMARY,
        rule_violations=[],
        total_value=42_000.0,
        cash=8_400.0,
        position_history=_DRAWDOWN_POSITION_HISTORY,
        recent_performance=_DRAWDOWN_RECENT_PERFORMANCE,
    )
    return verdict


def _risk_drawdown_grade(verdict) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.20, verdict is not None, "RiskVerdict validated"))
    if verdict is None:
        return checks

    chain = verdict.reasoning_chain.model_dump()
    checks.append(Check(
        "cot_complete", 0.10,
        all(str(v).strip() for v in chain.values()),
        f"{sum(1 for v in chain.values() if str(v).strip())}/{len(chain)} steps",
    ))

    # Did it ACT on a BUY that ignored the halving requirement? Same shape as
    # the rr_breach check: a mod on the name, a portfolio-wide scale-down, or
    # a rejection all count.
    msft_mods = [m for m in verdict.modifications if m.symbol.upper() == "MSFT"]
    acted = (
        (not verdict.approved)
        or bool(msft_mods)
        or verdict.scale_all_buys < 1.0
    )
    checks.append(Check(
        "acts_on_unhalved_drawdown_buy", 0.30, acted,
        f"approved={verdict.approved} msft_mods={len(msft_mods)} "
        f"scale_all_buys={verdict.scale_all_buys}",
    ))

    # And did it say why. "Acted for some other reason" is not the same as
    # applying the rule, so the audit trail has to name the drawdown.
    chain_text = " ".join(str(v) for v in chain.values())
    checks.append(Check(
        "names_drawdown_in_chain", 0.20,
        re.search(r"drawdown|halv", chain_text, re.IGNORECASE) is not None,
        f"sizing_sanity={str(chain.get('sizing_sanity', ''))[:110]!r}",
    ))

    # The <5d SELL. RM cannot cleanly cancel a SELL (setting allocation_pct
    # to 0 is forbidden — it silently skips the exit), so the graded response
    # is that it flags the name, not that it acts on it.
    # Bound outside the f-string: a backslash inside an f-string EXPRESSION is
    # a SyntaxError before Python 3.12 (PEP 701 relaxed it), and this project
    # declares requires-python >=3.11 and runs CI on 3.11. Developing on 3.12
    # hides that entirely — this module never imported on 3.11.
    mentions_amd = re.search(r"\bAMD\b", chain_text) is not None
    checks.append(Check(
        "flags_protected_period_sell", 0.20,
        mentions_amd
        or any(m.symbol.upper() == "AMD" for m in verdict.modifications),
        f"chain mentions AMD={mentions_amd}",
    ))
    return checks


# --------------------------------------------------------------------------
# 5. news_analyst — noisy free text in, structured intelligence out
# --------------------------------------------------------------------------

_NEWS_TEXT = """
[2026-08-11 07:12] Reuters — Micron guides Q4 revenue above consensus on
HBM4 demand; says memory supply remains tight into 2027.
[2026-08-11 07:40] Bloomberg — Fed's Barkin says policy must stay
restrictive "for some time"; futures trim September cut odds to 22%.
[2026-08-11 08:03] WSJ — Crude falls a fourth session as OPEC+ signals
higher quotas; energy majors slide premarket.
[2026-08-11 08:15] CNBC — Apple supplier checks point to a softer iPhone
build plan for the December quarter, per Morgan Stanley note.
[2026-08-11 08:31] AP — Weekly jobless claims rise to 254k, highest since
February; continuing claims also up.
[2026-08-11 08:44] Reuters — Nvidia and Broadcom named in a new export
licence review covering advanced accelerators to two Gulf states.
[2026-08-11 09:02] Barron's — Utilities ETF sees largest weekly inflow in
14 months as investors rotate defensive.
"""


def _news_invoke(agent):
    report, _ = agent.analyze(
        news_text=_NEWS_TEXT,
        universe=["SPY", "QQQ", "XLE", "XLU", "SMH", "AAPL", "NVDA", "MU", "AVGO"],
    )
    return report


def _news_grade(report) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("parsed", 0.30, report is not None, "NewsIntelligenceReport validated"))
    if report is None:
        return checks

    # macro_narrative is a MacroNarrative object, not a string — the regime
    # line is the part PM actually reads.
    regime = (getattr(report.macro_narrative, "current_regime", "") or "")
    checks.append(Check(
        "macro_narrative_present", 0.15,
        len(regime.strip()) >= 40,
        f"current_regime {len(regime.strip())} chars",
    ))

    # The tape above contains two genuine state changes (Fed cut odds cut to
    # 22%; an export-licence review naming NVDA/AVGO). A report that emits
    # none has flattened the news into sentiment and dropped the signal PM
    # is meant to act on.
    checks.append(Check(
        "state_changes_extracted", 0.20,
        len(report.state_changes) >= 1,
        f"{len(report.state_changes)} state changes",
    ))

    # Every headline above names a symbol in the universe. A report that
    # surfaces none of them has summarised without attributing. Word-boundary
    # match so "MU" doesn't score a hit off "VOLUME"/"MUST".
    blob = str(report.model_dump()).upper()
    hits = [
        s for s in ("MU", "NVDA", "AAPL", "XLE", "XLU", "AVGO")
        if re.search(rf"\b{s}\b", blob)
    ]
    checks.append(Check(
        "symbols_attributed", 0.20,
        len(hits) >= 3,
        f"named {hits}",
    ))

    # Per-symbol news is the structure PM consumes; a report that fills only
    # the prose fields is not usable by the downstream template.
    checks.append(Check(
        "stock_news_structured", 0.15,
        len(report.stock_news) >= 2,
        f"{len(report.stock_news)} symbols in stock_news",
    ))
    return checks


# --------------------------------------------------------------------------
# 5b. tech_analyst at PRODUCTION scale — the session's time budget
# --------------------------------------------------------------------------
#
# `tech_batch` above uses 3 symbols, which is right for grading judgement
# but wrong for predicting wall-clock. Production is a 101-symbol universe
# at `_CHUNK_SIZE = 25`, so `analyze_batch` issues FIVE sequential calls of
# 25 symbols each, and that chain is the longest pole in the morning:
# `pipeline_stages.py` fans macro/news/tech/earnings out across four
# threads, so the parallel stage finishes when tech does, and PM then RM
# run after it.
#
# The ceiling is hard and external: `scripts/run_if_et_window.sh:225` wraps
# each session in `timeout --kill-after=30 1200`. A model that needs 200s
# for one chunk needs ~1000s for the stage and leaves nothing for the
# decision seats — it is unusable here no matter how well it scores or how
# little it costs. This scenario measures ONE real chunk so that per-chunk
# latency can be multiplied out honestly.

_FULL_BATCH_SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY",
    "XLU", "XLRE", "XLB", "SMH", "SOXX", "AAPL", "MSFT", "GOOGL", "AMZN",
    "NVDA", "META", "AVGO", "CRM", "AMD", "ORCL",
]


def _full_batch_data() -> list[dict]:
    """25 symbols with deterministic, per-symbol-varied series.

    Varied so the model cannot answer once and copy: each symbol gets its
    own price level and drift sign, cycling through up / down / flat.
    """
    out = []
    for i, symbol in enumerate(_FULL_BATCH_SYMBOLS):
        base = 40.0 + i * 7.5
        drift = (0.45, -0.22, 0.02)[i % 3]
        atr = round(base * 0.018, 2)
        ma20 = round(base + drift * 50, 2)
        out.append({
            "symbol": symbol,
            "bars": _bars(symbol, base, drift),
            "indicators": TechnicalIndicators(
                symbol=symbol,
                ma_20=ma20,
                ma_50=round(base + drift * 30, 2),
                ma_200=round(base + drift * 10, 2),
                rsi_14=round(50 + drift * 30, 1),
                macd=round(drift * 4, 2),
                macd_signal=round(drift * 3, 2),
                macd_hist=round(drift, 2),
                bb_upper=round(ma20 + atr * 2, 2),
                bb_middle=ma20,
                bb_lower=round(ma20 - atr * 2, 2),
                atr_14=atr,
                volume_change_pct=round(5.0 + i, 1),
            ),
        })
    return out


def _tech_full_invoke(agent):
    analyses, _ = agent.analyze_batch(symbols_data=_full_batch_data())
    return analyses


def _tech_full_grade(analyses: dict | None) -> list[Check]:
    checks: list[Check] = []
    analyses = analyses or {}
    expected = set(_FULL_BATCH_SYMBOLS)

    # Partial coverage is the specific failure this scale surfaces: a model
    # that handles 3 symbols cleanly can silently drop half of a 25-symbol
    # batch, and PM then reasons over a book it thinks it has seen.
    checks.append(Check(
        "all_25_returned", 0.55,
        set(analyses) == expected,
        f"{len(analyses)}/25 returned, missing={sorted(expected - set(analyses))[:6]}",
    ))

    indicators = {d["symbol"]: d["indicators"] for d in _full_batch_data()}
    bad: list[str] = []
    for sym, res in analyses.items():
        if res.rating == "neutral" or res.entry_price is None or res.stop_loss is None:
            continue
        atr = indicators[sym].atr_14 or 0
        if atr > 0 and not (0.8 <= abs(res.entry_price - res.stop_loss) / atr <= 8.0):
            bad.append(sym)
    checks.append(Check(
        "atr_stop_discipline_at_scale", 0.25, not bad,
        f"{len(bad)} symbol(s) with an out-of-band stop: {bad[:6]}",
    ))

    wrong = [
        sym for sym, res in analyses.items()
        if (res.rating == "neutral") == bool((res.thesis_invalid_if or "").strip())
    ]
    checks.append(Check(
        "thesis_invalid_if_discipline_at_scale", 0.20, not wrong,
        f"{len(wrong)} symbol(s) wrong: {wrong[:6]}",
    ))
    return checks




# External-source seat exams (owner rule 2026-09-14)
# --------------------------------------------------------------------------
#
# Built ONLY from raw public facts fetched fresh from the original source
# (SEC EDGAR, yfinance) and pinned under fixtures/ with per-section
# provenance. Every value the seat is shown is recomputed at exam time by
# TODAY's live provider and builder code — nothing derived is stored, and no
# agent output is filled in. `fixture_policy.check_fixture` must admit the
# manifest or the scenario is refused.


@contextmanager
def _frozen_today(day: date, *modules):
    """Pin `et_today` in the given modules to the exam's session date.

    The providers age every fact against the clock (freshness, disclosure
    age, the discovery window). An exam replayed next month must see the
    same ages it was built on, so the clock is the one input frozen here.
    """
    saved = [(module, module.et_today) for module in modules]
    for module in modules:
        module.et_today = (lambda d=day: d)
    try:
        yield
    finally:
        for module, original in saved:
            module.et_today = original


def _manifest(name: str) -> dict:
    return json.loads((_fixture_policy.FIXTURES_DIR / name).read_text())


# ---- earnings_analyst: one real 10-Q from EDGAR ---------------------------

_EARNINGS_FIXTURE = "sec_mrvl_10q_2026-08-28.json"

#: The only `[UNSOURCED:<reason>]` tokens the prompt defines
#: (config/prompts/earnings_analyst.md:37 and :83).
_EARNINGS_UNSOURCED_REASONS = ("not_in_filing", "truncated", "ambiguous", "no_market_data")


def earnings_exam_report(scratch: Path | None = None):
    """The live `EarningsReport`, rebuilt by today's provider from raw EDGAR bytes.

    Mirrors `EarningsDataProvider._check_symbol` (src/data/earnings.py:945-961)
    step for step — extract text from the filing HTML, fetch XBRL facts,
    prepend the STRUCTURED FINANCIAL FACTS block — with the one network call
    replaced by the pinned companyfacts bytes.
    """
    import tempfile

    from src.data.earnings import EarningsDataProvider, EarningsReport, extract_text

    manifest = _manifest(_EARNINGS_FIXTURE)
    filing = manifest["filing"]
    html = _fixture_policy.load_blob(_EARNINGS_FIXTURE, "sec_mrvl_10q_2026-08-28.htm.gz")
    facts = _fixture_policy.load_blob(_EARNINGS_FIXTURE, "sec_mrvl_companyfacts.json.gz")
    root = Path(scratch or tempfile.mkdtemp(prefix="seat-exam-earnings-"))
    provider = EarningsDataProvider(data_dir=str(root / "provider"))
    from src.data.xbrl_facts import fetch_xbrl_raw, format_xbrl_text, xbrl_comparable_values
    html_path = root / f"{filing['form_type']}_{filing['filing_date']}.html"
    html_path.write_bytes(html)
    text = extract_text(str(html_path))
    xbrl_raw = fetch_xbrl_raw(lambda _url: facts, filing["cik"], filing["symbol"], filing["filing_date"])
    block = format_xbrl_text(xbrl_raw)
    if block:
        text = block + "\n" + text
    analysis_dir = root / "analyses" / filing["symbol"]
    analysis_dir.mkdir(parents=True, exist_ok=True)
    return EarningsReport(
        symbol=filing["symbol"], form_type=filing["form_type"],
        filing_date=filing["filing_date"], filing_path=str(html_path),
        analysis_path=str(analysis_dir / f"analysis_{filing['form_type']}_{filing['filing_date']}.md"),
        text_excerpt=text, is_new=True,
        xbrl_facts=xbrl_comparable_values(xbrl_raw),
    )


def _earnings_invoke(agent):
    report = earnings_exam_report()
    results = agent.analyze_reports([report])
    analysis = results[0]["analysis"] if results else None
    return SimpleNamespace(analysis=analysis, report=report)


def _earnings_grade(output) -> list[Check]:
    from src.agents.earnings_analyst import _UNSOURCED_VALUATION_DISCLOSURE
    from src.pipeline_stages import (
        _earnings_analysis_has_real_figures, _earnings_xbrl_mismatch_fields,
    )

    analysis = getattr(output, "analysis", None)
    report = getattr(output, "report", None)
    checks = [Check(
        "parsed_and_identifiers_echoed", 0.25, analysis is not None,
        "survived EarningsAnalystAgent._validate_analysis "
        "(src/agents/earnings_analyst.py:285): schema, and symbol / form / "
        "filing date echoed verbatim (config/prompts/earnings_analyst.md:39)",
    )]
    if analysis is None:
        return checks

    checks.append(Check(
        "has_real_figures", 0.15, _earnings_analysis_has_real_figures(analysis),
        "src/pipeline_stages.py:1590 — the live content check behind "
        "data_status['earnings']",
    ))
    mismatches = _earnings_xbrl_mismatch_fields(analysis, report.xbrl_facts)
    checks.append(Check(
        "figures_match_sec_xbrl", 0.20, not mismatches,
        f"contradicted={mismatches} (src/pipeline_stages.py:1752; the live desk "
        f"reports such a filing as figures_contradicted, :1850)",
    ))
    bad_tokens = sorted({
        reason for reason in re.findall(r"\[UNSOURCED:([^\]]*)\]", json.dumps(analysis))
        if reason not in _EARNINGS_UNSOURCED_REASONS
    })
    checks.append(Check(
        "unsourced_tokens_valid", 0.15, not bad_tokens,
        f"undefined reasons {bad_tokens} (config/prompts/earnings_analyst.md:37, :83)",
    ))
    truncated = "truncated ...]" in report.text_excerpt
    quality = str(analysis.get("data_quality") or "").lower()
    checks.append(Check(
        "truncation_flagged", 0.15, (not truncated) or ("truncat" in quality),
        f"input truncated={truncated}; data_quality={quality[:80]!r} "
        f"(config/prompts/earnings_analyst.md:14 — 'must flag truncation')",
    ))
    valuation = (
        ((analysis.get("investment_implications") or {}).get("reasoning_chain") or {})
        .get("valuation_context") or ""
    )
    checks.append(Check(
        "no_price_derived_valuation_claim", 0.10,
        valuation != _UNSOURCED_VALUATION_DISCLOSURE,
        "src/agents/earnings_analyst.py:358 redacted a valuation claim the "
        "filing cannot ground" if valuation == _UNSOURCED_VALUATION_DISCLOSURE else "ok",
    ))
    return checks


# ---- smart_money_analyst: real SEC Form 4 submissions ---------------------

_SMART_MONEY_FIXTURE = "sec_form4_2026-08-28_to_31.json"
_SMART_MONEY_BLOB = "sec_form4_submissions_2026-08-28_to_31.json.gz"


def smart_money_exam_observations(scratch: Path | None = None) -> list:
    """The observations today's `SECForm4Provider` selects from the pinned
    raw submissions: parse (`_parse_submission`), then classify, age, admit,
    cluster and cap (`fetch`) — with the session date frozen."""
    import tempfile

    import yaml

    import src.data.smart_money as smart_money

    manifest = _manifest(_SMART_MONEY_FIXTURE)
    payload = json.loads(_fixture_policy.load_blob(_SMART_MONEY_FIXTURE, _SMART_MONEY_BLOB))
    exam = manifest["_exam"]
    table = manifest["sec_company_tickers_exchange"]
    fields = table["fields"]
    listed: dict[str, dict[str, str]] = {}
    for row in table["data"]:
        listed.setdefault(str(int(row[fields.index("cik")])), {})[
            smart_money._symbol(row[fields.index("ticker")])
        ] = str(row[fields.index("exchange")])
    settings = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    universe = (yaml.safe_load(settings.read_text()) or {})["trading"]["universe"]
    root = Path(scratch or tempfile.mkdtemp(prefix="seat-exam-form4-"))
    with _frozen_today(date.fromisoformat(exam["session_date"]), smart_money):
        provider = smart_money.SECForm4Provider(data_dir=str(root), **exam["provider_settings"])
        rows = []
        for accession in manifest["discovery"]["accessions"]:
            entry = payload[accession]
            rows.extend(
                row.model_dump(mode="json")
                for row in provider._parse_submission(
                    entry["submission"], source_url=entry["source_url"], listed=listed,
                )
            )
        (root / "observations.json").write_text(json.dumps(rows))
        observations, error = provider.fetch(universe)
    if error:
        raise RuntimeError(f"smart-money exam fixture did not re-derive cleanly: {error}")
    return observations


def _smart_money_invoke(agent):
    import tempfile

    observations = smart_money_exam_observations()
    # Never read or write the desk's synthesis cache: a hit would skip the
    # model entirely and score a stored answer.
    agent.synthesis_cache_path = (
        Path(tempfile.mkdtemp(prefix="seat-exam-synthesis-")) / "synthesis_cache.json"
    )
    # Capture every raw reply so coverage is graded from what the model
    # actually returned, not from the agent's own gap bookkeeping.
    replies: list = []
    real_run = agent.run

    def run(**kwargs):
        result = real_run(**kwargs)
        replies.append(result.parse_json())
        return result

    agent.run = run
    findings, _result, error, _gaps = agent.analyze_with_gaps(observations)
    no_read_symbols = {
        str(row.get("symbol", "")).upper()
        for reply in replies if isinstance(reply, dict)
        for row in (reply.get("no_reads") or []) if isinstance(row, dict)
    }
    return SimpleNamespace(
        findings=findings, error=error, no_read_symbols=no_read_symbols,
        presented=tuple(agent._presented_symbols(observations)),
    )


def _smart_money_grade(output) -> list[Check]:
    if output is None:
        return [Check("parsed", 0.30, False, "no output")]
    error = output.error
    findings = list(output.findings or [])
    checks = [Check(
        "parsed", 0.30, error not in ("analysis_parse_error", "analysis_schema_error"),
        f"error={error} (src/agents/smart_money_analyst.py:514, :533)",
    )]
    checks.append(Check(
        "no_finding_dropped", 0.40, error is None,
        f"error={error} — a finding is dropped when its stance contradicts its "
        f"P/S source rows (config/prompts/smart_money_analyst.md:16-20, enforced "
        f"at src/agents/smart_money_analyst.py:434) or names a symbol that was "
        f"not presented or already answered (:449)",
    ))
    # Symbols SENT vs symbols ANSWERED (a validated finding or a no-read in
    # a raw reply), computed here -- never from the agent's own gap list.
    answered = {f.symbol.upper() for f in findings} | set(getattr(output, "no_read_symbols", ()) or ())
    unanswered = sorted(set(output.presented) - answered)
    checks.append(Check(
        "covers_every_presented_symbol", 0.30, not unanswered,
        f"{len(findings)} finding(s) of {len(output.presented)} presented; "
        f"sent but unanswered: {unanswered}",
    ))
    return checks


# ---- tech_analyst: real daily bars ------------------------------------

_TECH_FIXTURE = "yf_daily_bars_2026-08-28.json"


def tech_exam_symbols_data() -> list[dict]:
    """`analyze_batch` input rebuilt from raw yfinance bars: indicators by
    today's `compute_indicators`; levels and market context are computed by
    today's builder when it renders."""
    from src.data.technical import compute_indicators

    manifest = _manifest(_TECH_FIXTURE)
    raw = json.loads(_fixture_policy.load_blob(_TECH_FIXTURE, "yf_daily_bars_2026-08-28.json.gz"))
    out = []
    for symbol in manifest["symbols"]:
        bars = [
            OHLCV(date=date.fromisoformat(b["date"]), open=b["open"], high=b["high"],
                  low=b["low"], close=b["close"], volume=int(b["volume"]))
            for b in raw[symbol]
        ]
        out.append({"symbol": symbol, "bars": bars, "indicators": compute_indicators(symbol, bars)})
    return out


def _tech_invoke(agent):
    analyses, _ = agent.analyze_batch(symbols_data=tech_exam_symbols_data())
    return analyses


def _tech_grade(analyses: dict | None) -> list[Check]:
    import yaml

    analyses = analyses or {}
    expected = set(_manifest(_TECH_FIXTURE)["symbols"])
    settings = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
    floor = float((yaml.safe_load(settings.read_text()) or {})["risk"]["absolute_min_stop_atr_multiple"])
    resolved = {s for s, a in analyses.items() if a is not None}
    checks = [Check(
        "all_symbols_resolved", 0.30, resolved == expected,
        f"unresolved={sorted(expected - resolved)} (a row failing "
        f"TechAnalysisResult validation resolves to None, "
        f"src/agents/tech_analyst.py analyze_batch)",
    )]
    real = {s: a for s, a in analyses.items() if a is not None}
    wrong = [
        s for s, a in real.items()
        if (a.rating == "neutral") == bool((a.thesis_invalid_if or "").strip())
    ]
    checks.append(Check(
        "thesis_invalid_if_discipline", 0.25, bool(real) and not wrong,
        f"wrong={wrong} (config/prompts/tech_analyst.md:11 and :133 — filled on "
        f"actionable, empty on neutral)",
    ))
    inside = [
        s for s, a in real.items()
        if a.rating != "neutral" and a.entry_price and a.stop_loss and a.atr_14
        and abs(a.entry_price - a.stop_loss) < floor * a.atr_14
    ]
    checks.append(Check(
        "stop_outside_absolute_atr_floor", 0.20, bool(real) and not inside,
        f"inside {floor}xATR: {inside} (config/prompts/tech_analyst.md:65, :71; "
        f"config/settings.yaml:843 risk.absolute_min_stop_atr_multiple)",
    ))
    thin = [
        s for s, a in real.items()
        if a.rating != "neutral" and a.setup_type == "range"
        and a.risk_reward is not None and a.risk_reward < 2.0
    ]
    checks.append(Check(
        "range_setups_designed_to_2r", 0.25, bool(real) and not thin,
        f"range R/R < 2.0: {thin} (config/prompts/tech_analyst.md:83 — 'design "
        f"the trade so R/R is >= 2.0'; a breakout carries no such rule)",
    ))
    return checks


# ---- macro_analyst: real FRED series, fetched via the OneCLI gateway ------

_MACRO_FIXTURE = "fred_macro_2026-09-14.json"
_MACRO_BLOB = "fred_series_2026-09-14.json.gz"

# Same universe shape as the retired synthetic scenario — plain tickers,
# not fixture-governed data.
_PUBLIC_MACRO_UNIVERSE = ["SPY", "QQQ", "XLE", "XLU", "XLP", "XLF", "SMH", "AAPL", "NVDA"]


def _public_macro_summary():
    """Today's `MacroDataProvider.get_macro_summary()` (src/data/macro.py:1125),
    replayed against the pinned raw FRED observations — the fetch (`fred.
    get_series` / `get_series_info`) is stubbed with the pinned bytes; every
    computed field (change_pct, trend, percentile, freshness, ...) is
    produced by today's live provider code, not stored in the fixture.
    """
    import pandas as pd

    from src.data.macro import MacroDataProvider

    payload = json.loads(_fixture_policy.load_blob(_MACRO_FIXTURE, _MACRO_BLOB))
    series_data, series_info = payload["series"], payload["series_info"]
    provider = MacroDataProvider(api_key="unused-fixture-replay-no-network")

    def _get_series(series_id, **_kw):
        obs = series_data.get(series_id) or {}
        return pd.Series(
            {pd.Timestamp(d): v for d, v in obs.items()}
        ).sort_index()

    def _get_series_info(series_id):
        return series_info.get(series_id) or {}

    provider.fred.get_series = _get_series
    provider.fred.get_series_info = _get_series_info
    return provider.get_macro_summary()


def _public_macro_invoke(agent):
    analysis, _ = agent.analyze(
        macro_summary=_public_macro_summary(), universe=_PUBLIC_MACRO_UNIVERSE,
        last_state=None, news_narrative=None,
    )
    return analysis


def _public_macro_grade(analysis) -> list[Check]:
    """Schema/rule compliance only — real market conditions on the fetch
    date are whatever they are, so there is no forced-arithmetic correct
    direction to grade against (unlike the retired synthetic `macro_stress`).
    """
    checks: list[Check] = [Check(
        "parsed", 0.40, analysis is not None,
        "MacroAnalysis validated (src/models.py:2258 — regime/confidence/"
        "equity_outlook enums enforced by pydantic on parse)",
    )]
    if analysis is None:
        return checks

    # Was `target_invested_pct_in_range`; macro no longer emits an invested
    # % (owner fully-invested mandate, 2026-09-17). The directional lean is
    # what position_guidance carries now.
    pos = getattr(analysis, "position_guidance", None)
    lean = str(getattr(pos, "reasoning", "") or "").strip()
    checks.append(Check(
        "position_guidance_lean_present", 0.20,
        bool(lean),
        "position_guidance.reasoning populated" if lean else "missing",
    ))

    guidance = getattr(analysis, "sector_guidance", None) or []
    checks.append(Check(
        "sector_guidance_present", 0.20,
        len(guidance) >= 2,
        f"{len(guidance)} sector calls (config/prompts/macro_analyst.md)",
    ))

    chain = getattr(analysis, "reasoning_chain", None)
    checks.append(Check(
        "reasoning_chain_present", 0.20,
        chain is not None,
        "ReasoningChain populated" if chain is not None else "missing",
    ))
    return checks


# ---- news_analyst: real RSS wires, fetched live at fixture-build time -----

_NEWS_FIXTURE = "rss_feeds_2026-09-14.json"
_NEWS_BLOB = "rss_feeds_2026-09-14.json.gz"

_PUBLIC_NEWS_UNIVERSE = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "TSLA", "META",
    "AMD", "MU", "AVGO", "XLE", "XLF", "XLU", "XLK",
]


def _public_news_report(scratch: Path | None = None):
    """Today's `NewsDataProvider.fetch_news` / `format_for_prompt` /
    `tag_symbol_mentions` (src/data/news.py), replayed against the pinned
    raw RSS bytes — `urlopen` is stubbed with the pinned per-feed bytes
    fetched live at fixture-build time; parsing, dedup, formatting and
    symbol tagging all run as today's code.
    """
    import src.data.news as news_mod

    payload = json.loads(_fixture_policy.load_blob(_NEWS_FIXTURE, _NEWS_BLOB))
    manifest = _manifest(_NEWS_FIXTURE)
    feed_urls = {name: v["url"] for name, v in manifest["rss_feeds"].items()}

    def _replay_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        name = next((n for n, u in feed_urls.items() if u == url), None)
        raw = (payload.get(name) or "").encode("utf-8")

        class _Replay:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return raw

        return _Replay()

    real_urlopen = news_mod.urlopen
    news_mod.urlopen = _replay_urlopen
    try:
        provider = news_mod.NewsDataProvider(feeds=feed_urls, per_symbol_enabled=False)
        items, coverage = provider.fetch_news(symbols=None)
    finally:
        news_mod.urlopen = real_urlopen
    news_text = provider.format_for_prompt(items, max_items=60)
    stock_mentions = provider.tag_symbol_mentions(items, _PUBLIC_NEWS_UNIVERSE)
    return news_text, stock_mentions, coverage


def _public_news_invoke(agent):
    news_text, stock_mentions, coverage = _public_news_report()
    report, _ = agent.analyze(
        news_text=news_text, universe=_PUBLIC_NEWS_UNIVERSE,
        stock_mentions=stock_mentions, previous_narrative=None,
        session="morning", prior_session_report=None, news_coverage=coverage,
    )
    return report


def _public_news_grade(report) -> list[Check]:
    """Rule/schema compliance only, per the owner's 2026-09-14 direction:
    real fetched news has no engineered correct answer to grade judgement
    against (unlike the retired synthetic `news_intel`)."""
    checks: list[Check] = [Check(
        "parsed", 0.35, report is not None,
        "NewsIntelligenceReport validated (src/models.py:2758 — enums on "
        "market_sentiment/confidence/StateChange.conviction/StockNewsItem."
        "sentiment+conviction all pydantic-enforced on parse)",
    )]
    if report is None:
        return checks

    regime = (getattr(report.macro_narrative, "current_regime", "") or "")
    checks.append(Check(
        "macro_narrative_present", 0.20,
        len(regime.strip()) >= 5,
        f"current_regime {len(regime.strip())} chars "
        "(src/models.py:2515 MacroNarrative.current_regime min_length=5)",
    ))

    universe = set(_PUBLIC_NEWS_UNIVERSE)
    stock_news_symbols = set(report.stock_news or {})
    invented_stock_news = sorted(stock_news_symbols - universe)
    checks.append(Check(
        "no_invented_stock_news_symbols", 0.25, not invented_stock_news,
        f"stock_news symbols outside the given universe: {invented_stock_news}",
    ))

    affected = {
        s for sc in (report.state_changes or []) for s in (sc.affected_symbols or [])
    }
    invented_affected = sorted(affected - universe)
    checks.append(Check(
        "no_invented_state_change_symbols", 0.20, not invented_affected,
        f"state_change affected_symbols outside the given universe: "
        f"{invented_affected}",
    ))
    return checks


# ---- portfolio_manager: fresh analyst output, public raw facts only ------
#
# Built by ops/model_policy/build_pm_public_day_fixture.py: the tech/macro/
# news/earnings/smart-money agent classes, run FRESH (google-direct,
# gemini-3.5-flash-lite, free tier — see that script) over the same raw
# public-fact fixtures the exams above use. Account/positions are a labelled
# SYNTHETIC ACCOUNT STATE (flat cash, no positions) — a real account is desk
# state, which the policy refuses. No desk recording, no invented price or
# news item anywhere in this fixture.

_PM_PUBLIC_DAY_FIXTURE = "pm_public_day_pm_input.json"


def _pm_public_day_manifest() -> dict:
    return _manifest(_PM_PUBLIC_DAY_FIXTURE)


def _pm_public_day_inputs():
    """Typed objects from the fixture manifest, for `agent.decide()`."""
    manifest = _pm_public_day_manifest()
    analyses = [TechAnalysisResult.model_validate(a) for a in manifest["analyses"]]
    news_intel = NewsIntelligenceReport.model_validate(manifest["news_intel"])
    from src.models import SmartMoneyFinding

    smart_money_findings = [
        SmartMoneyFinding.model_validate(f) for f in manifest["smart_money_findings"]
    ]
    account = manifest["account_state"]
    return manifest, analyses, news_intel, smart_money_findings, account


def _pm_public_day_eligible_set(analyses: list[TechAnalysisResult]) -> dict[str, list[str]]:
    """The desk's own pre-decision admission gate, replayed with no LLM call.

    `PortfolioManagerAgent.candidate_eligibility` (src/agents/
    portfolio_manager.py:1376) is the exact function `decide()`'s prompt
    builder calls to order/label candidates before the model ever sees them
    — reusing it here (rather than a second copy of the rule) means this
    scenario's "eligible set" is definitionally the live gate's, not a
    grader's opinion of it.
    """
    from src.agents.portfolio_manager import PortfolioManagerAgent

    evidence_registry = PortfolioManagerAgent.build_evidence_registry(
        analyses=analyses, positions=[], news_intel=None,
        earnings_analyses=[], macro_analysis=None, smart_money_findings=[],
    )
    # allowed_buy_symbols: every analysed symbol is treated as within the
    # session's research universe (this fixture has no separate "configured
    # universe minus admitted" distinction to replay — see the practice-day
    # input inventory in the PR).
    allowed = {a.symbol.upper() for a in analyses}
    return PortfolioManagerAgent.candidate_eligibility(
        analyses=analyses, evidence_registry=evidence_registry,
        allowed_buy_symbols=allowed, active_state_changes="",
    )


def _pm_public_day_invoke(agent):
    manifest, analyses, news_intel, smart_money_findings, account = _pm_public_day_inputs()
    decision, _ = agent.decide(
        analyses=analyses,
        positions=[],
        macro_analysis=manifest["macro_analysis"],
        cash_balance=account["cash_balance"],
        reserve_balance=account["reserve_balance"],
        total_value=account["total_value"],
        news_intel=news_intel,
        earnings_analyses=manifest["earnings_analyses"],
        smart_money_findings=smart_money_findings,
        allow_margin=account["allow_margin"],
        session_type=account["session_type"],
        allowed_buy_symbols={a.symbol.upper() for a in analyses},
        transient_admitted_symbols=set(),
    )
    return decision


def _pm_public_day_grade(decision: PortfolioDecision | None) -> list[Check]:
    checks: list[Check] = [Check(
        "parsed_and_grounded", 0.35, decision is not None,
        "checked PortfolioDecision parses and is grounded against live data "
        "(PortfolioManagerAgent.validate_grounding, "
        "src/agents/portfolio_manager.py:2328) — every cited symbol, source "
        "and claim must exist in the evidence this session actually built.",
    )]
    if decision is None:
        return checks

    _manifest_, analyses, *_rest = _pm_public_day_inputs()
    eligible = _pm_public_day_eligible_set(analyses)
    picks = [t for t in decision.targets if not t.is_close]
    ineligible = [
        f"{t.symbol}({','.join(eligible.get(t.symbol.upper(), ['no coverage']))})"
        for t in picks if eligible.get(t.symbol.upper())
    ]
    checks.append(Check(
        "opens_only_from_eligible_set", 0.40,
        not ineligible,
        f"{len(picks) - len(ineligible)}/{len(picks)} target(s) admitted by "
        "PortfolioManagerAgent.candidate_eligibility "
        f"(src/agents/portfolio_manager.py:1376); refused: {ineligible or 'none'}; "
        f"eligible set today: {sorted(s for s, why in eligible.items() if not why)}",
    ))

    rc = decision.reasoning_chain
    hard_required = [
        "macro_filter", "news_check", "earnings_check", "signal_conflicts",
        "sizing_logic", "portfolio_balance", "cash_target",
    ]
    soft_required = ["continuity_check", "premortem_check", "macro_audit"]
    missing = [f for f in hard_required + soft_required if not str(getattr(rc, f, "") or "").strip()]
    checks.append(Check(
        "reasoning_chain_all_ten_fields", 0.25,
        rc is not None and not missing,
        "config/prompts/portfolio_manager.md:679 — the 10-field reasoning_chain "
        f"is MANDATORY; missing/empty: {missing or 'none'}",
    ))
    return checks


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def _review_invoke(agent):
    from ops.model_policy.scenarios_midday_exit import _review_invoke as run  # lazy: that module reads this one
    return run(agent)


def _review_grade(review) -> list[Check]:
    from ops.model_policy.scenarios_midday_exit import _review_grade as grade
    return grade(review)


SCENARIOS: list[Scenario] = [
    Scenario(
        key="earnings_filing",
        role="earnings_analyst",
        agent_path="src.agents.earnings_analyst:EarningsAnalystAgent",
        invoke=_earnings_invoke,
        grade=_earnings_grade,
        fixture=_EARNINGS_FIXTURE,
        description="A real MRVL 10-Q (filed 2026-08-28) fetched from SEC EDGAR; "
                    "filing text and XBRL facts recomputed by today's provider. "
                    "Grades identifier echo, real figures, agreement with SEC "
                    "XBRL, UNSOURCED-token and truncation discipline.",
    ),
    Scenario(
        key="smart_money_form4",
        role="smart_money_analyst",
        agent_path="src.agents.smart_money_analyst:SmartMoneyAnalystAgent",
        invoke=_smart_money_invoke,
        grade=_smart_money_grade,
        fixture=_SMART_MONEY_FIXTURE,
        description="Real SEC Form 4 submissions (2026-08-28..31) run through "
                    "today's SECForm4Provider. Grades parse survival, stance vs "
                    "P/S source direction, and the word limit. Narrower "
                    "discovery window and empty insider history than "
                    "production — see the fixture's deviations list.",
    ),
    Scenario(
        key="tech_batch",
        role="tech_analyst",
        agent_path="src.agents.tech_analyst:TechAnalystAgent",
        invoke=_tech_invoke,
        grade=_tech_grade,
        fixture=_TECH_FIXTURE,
        description="Real yfinance bars for 7 symbols (2026-08-28) run through "
                    "today's compute_indicators and analyze_batch "
                    "(src/agents/tech_analyst.py:336). Unblocked 2026-09-14: "
                    "analyze_batch's own signature defaults prior_ratings to "
                    "{} and prior_macro_regime/prior_macro_outlook to None "
                    "(src/agents/tech_analyst.py:339-343) — the tech seat's "
                    "prompt rates chart-driven, not prior-ratings-driven "
                    "(config/prompts/tech_analyst.md). Valuations and the "
                    "intraday snapshot are omitted the same way (both "
                    "optional, default None) rather than invented. Grades "
                    "symbol resolution, thesis_invalid_if discipline, the "
                    "absolute ATR stop floor, and range R:R >= 2.0.",
    ),
    Scenario(
        key="macro_stress",
        role="macro_analyst",
        agent_path="src.agents.macro_analyst:MacroAnalystAgent",
        invoke=_public_macro_invoke,
        grade=_public_macro_grade,
        fixture=_MACRO_FIXTURE,
        description="15 real FRED series (2026-09-14), fetched live through the "
                    "OneCLI credential gateway with a placeholder key — the real "
                    "FRED_API_KEY never entered this process — then recomputed "
                    "by today's MacroDataProvider.get_macro_summary "
                    "(src/data/macro.py:1125). last_state/news_narrative=None "
                    "(both optional, src/agents/macro_analyst.py:203-204). "
                    "Unblocked 2026-09-14 — see the retired synthetic-tape note "
                    "above for why the old scenario was blocked. Grades schema "
                    "and rule compliance only; real conditions have no forced "
                    "correct direction to grade against.",
    ),
    Scenario(
        key="news_intel",
        role="news_analyst",
        agent_path="src.agents.news_analyst:NewsAnalystAgent",
        invoke=_public_news_invoke,
        grade=_public_news_grade,
        fixture=_NEWS_FIXTURE,
        description="Real RSS wires (11 feeds, fetched live 2026-09-14) replayed "
                    "through today's NewsDataProvider.fetch_news / "
                    "format_for_prompt / tag_symbol_mentions "
                    "(src/data/news.py). previous_narrative/prior_session_report="
                    "None (both optional, src/agents/news_analyst.py:376-380). "
                    "Unblocked 2026-09-14. Grades schema/enum compliance and "
                    "'no invented tickers not in input' only — no judgement-"
                    "based answer key for real news.",
    ),
    Scenario(
        key="pm_constrained",
        role="portfolio_manager",
        agent_path="src.agents.portfolio_manager:PortfolioManagerAgent",
        invoke=_pm_invoke,
        grade=_pm_grade,
        description="Risk-off macro, 5.7% cash, margin OFF. Grades funding "
                    "arithmetic and signal consistency.",
    ),
    Scenario(
        key="pm_production_scale",
        role="portfolio_manager",
        agent_path="src.agents.portfolio_manager:PortfolioManagerAgent",
        invoke=_pm_production_invoke,
        grade=_pm_production_grade,
        default=False,
        description="30 candidates, 15 holdings and production-sized memory. "
                    "Grades grounded parse, provenance and phantom exits.",
    ),
    Scenario(
        key="pm_selection",
        role="portfolio_manager",
        agent_path="src.agents.portfolio_manager:PortfolioManagerAgent",
        invoke=_pm_selection_invoke,
        grade=_pm_selection_grade,
        default=False,
        fixture="run_bba4d4f3_pm_input.json",
        description="The REAL 2026-09-02 opportunity set (run-bba4d4f3). "
                    "QUARANTINED 2026-09-14: the fixture is a desk recording "
                    "(analyst outputs and old-code derived values), refused by "
                    "ops/model_policy/fixture_policy.py.",
    ),
    Scenario(
        key="pm_public_day",
        role="portfolio_manager",
        agent_path="src.agents.portfolio_manager:PortfolioManagerAgent",
        invoke=_pm_public_day_invoke,
        grade=_pm_public_day_grade,
        default=False,
        fixture=_PM_PUBLIC_DAY_FIXTURE,
        description="A valid PM practice day under the owner's raw-facts-only "
                    "rule: tech/macro/news/earnings/smart-money analyses run "
                    "FRESH (google-direct gemini-3.5-flash-lite) over the "
                    "existing public-fact exam fixtures, a flat labelled "
                    "SYNTHETIC ACCOUNT STATE (no positions, no desk memory), "
                    "so every memory/history field is left at decide()'s own "
                    "documented default. Grades live grounding, whether every "
                    "opened target is in the desk's own pre-decision eligible "
                    "set (candidate_eligibility), and reasoning_chain schema "
                    "completeness — no judgement answer key.",
    ),
    Scenario(
        key="risk_rr_breach",
        role="risk_manager",
        agent_path="src.agents.risk_manager:RiskManagerAgent",
        invoke=_risk_invoke,
        grade=_risk_grade,
        blocked_reason=(
            "the plan under review is an invented PM decision and the book is "
            "invented positions; the live call (src/pipeline_stages.py:4867) "
            "passes today's PM plan, the tech/news/earnings seats' outputs and "
            "the broker's positions — agent outputs and desk data. The grader's "
            "core check rewards acting on a thin range ratio, which "
            "config/prompts/risk_manager.md:251 says is not by itself grounds; "
            "its macro dict uses keys the renderer does not read "
            "(ten_year vs us10y, src/agents/risk_manager.py:503)"
        ),
        description="Synthetic 0.42R BUY at 18% of book. BLOCKED.",
    ),
    Scenario(
        key="risk_drawdown_discipline",
        role="risk_manager",
        agent_path="src.agents.risk_manager:RiskManagerAgent",
        invoke=_risk_drawdown_invoke,
        grade=_risk_drawdown_grade,
        default=False,
        blocked_reason=(
            "same invented PM plan and book as risk_rr_breach; and the whole "
            "rule it grades against is GONE — the account-level loss alarms, "
            "including the in_drawdown BUY-halving, were retired 2026-09-20 "
            "on the owner's instruction (docs/INCIDENT_HISTORY.md, board "
            "item 32). Rewrite or delete this scenario before ever enabling "
            "it; there is nothing left for the seat to be right or wrong "
            "about here"
        ),
        description="Synthetic unhalved drawdown BUY + young-position SELL. BLOCKED (the rule it grades no longer exists).",
    ),
    Scenario(
        key="tech_batch_full",
        role="tech_analyst",
        agent_path="src.agents.tech_analyst:TechAnalystAgent",
        invoke=_tech_full_invoke,
        grade=_tech_full_grade,
        default=False,
        blocked_reason=(
            "25 symbols of invented bars and indicators; raw facts must come from "
            "the original source. Rebuild on real yfinance bars once tech_batch's "
            "upstream inputs exist"
        ),
        description="Synthetic 25-symbol latency chunk. BLOCKED.",
    ),
    Scenario(
        key="midday_exit",
        role="position_reviewer",
        agent_path="src.agents.position_reviewer:PositionReviewerAgent",
        invoke=_review_invoke,
        grade=_review_grade,
        fixture="run_bba4d4f3_pm_input.json",
        description=(
            "Recorded 2026-09-02 book (run-bba4d4f3). Grades the exit path "
            "against the desk's own rule: distance to stop is advisory "
            "context, never by itself a reason to sell."
        ),
    ),
]

#: Seats with no exam at all, and exactly why (owner rule 2026-09-14).
NO_EXAM_SEATS: dict[str, str] = {
    "evening_analyst": (
        "its inputs are the day's positions and trades (desk data) plus the news "
        "and earnings seats' outputs, the rolling narrative and the evening's own "
        "prior grades (src/pipeline.py:13089)"
    ),
    "meta_reflector": (
        "its only input is a quarterly digest built from the desk's own trades, "
        "grades and outcomes (src/pipeline.py:13595); it has never run in "
        "production, so no recorded digest exists either"
    ),
}

SCENARIOS_BY_KEY = {s.key: s for s in SCENARIOS}
DEFAULT_SCENARIOS = [s for s in SCENARIOS if s.default]
