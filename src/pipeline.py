import logging
from pathlib import Path


from src.config import AppConfig
from src.cash_park import CashPark
from src.data.market import MarketDataProvider
from src.data.macro import MacroDataProvider
from src.data.event_calendar import FOMCCalendarProvider, MacroEventCalendarProvider
from src.data.news import NewsCoverage, NewsDataProvider
from src.data.news_store import NewsStore
from src.data.macro_store import MacroStore
from src.data.tech_store import TechStore
from src.agents.base import (
    BaseAgent,
)
from src.agents.tech_analyst import TechAnalystAgent

# Re-exported for backward-compat with tests that patch
# `src.pipeline.compute_indicators` (the name historically lived here).
from src.data.technical import compute_indicators  # noqa: F401
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.risk_manager import RiskManagerAgent
from src.agents.position_reviewer import PositionReviewerAgent
from src.agents.evening_analyst import EveningAnalystAgent
from src.agents.news_analyst import NewsAnalystAgent
from src.agents.macro_analyst import MacroAnalystAgent
from src.agents.earnings_analyst import EarningsAnalystAgent
from src.agents.meta_reflector import MetaReflectorAgent
from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from src.data.congressional_trading import CombinedSmartMoneyProvider, CongressionalTradingProvider
from src.data.smart_money import SECForm4Provider
from src.data.earnings import EarningsDataProvider
from src.risk.rules import (
    RiskRuleEngine,
)
from src.execution.broker import (
    AlpacaBroker,
    _split_protective_qty,  # noqa: F401
)
from src.pipeline_context import RunContext

# --- MIRROR: config builders moved to src/pipeline_config_build.py (pure move). ---
# Re-exported so `from src.pipeline import ...` and `patch("src.pipeline.<name>")` still resolve.
from src.pipeline_config_build import (  # noqa: F401
    _smart_money_refresh_sources_word,
    _threaded_risk_settings,
    build_constructor_config,
    build_risk_config,
)

# --- END MIRROR ---
# Step 1 of docs/PIPELINE_SPLIT_PLAN.md: these moved to a mixin module and are
# re-exported here because tests and other modules import them from `src.pipeline`.
from src.pipeline_protection import (  # noqa: F401
    ProtectionMixin,
    _WAL_SELL_SENTINEL,
    _classify_coverage_gap,
    _finite_float_or_none,
    _market_is_open_now,
    _position_notional,
    _price_is_through_stop,
    _reconciled_exit_action,
)

# Step 3 of docs/PIPELINE_SPLIT_PLAN.md: the Spec §11.2 de-levering ladder moved
# to a mixin module. `_optional_risk_number`/`_risk_number` travelled with it
# because the moved bodies read them and that module may not import this one;
# they are re-exported here, still used by `build_risk_config`, and still
# importable as `src.pipeline._risk_number`.
from src.pipeline_exits import (  # noqa: F401
    ExitEngineMixin,
    _CANONICAL_TRIGGER_NAMES,
    _CHART_VERIFIED_TRIGGER_NAMES,
    _HARD_TRIGGER_KEYWORDS,
    _actions_with_scan_fallback,
    _reason_claims_alignment_exit,
    _reason_cites_hard_trigger,
)
from src.admission_build import AdmissionSlot
from src.pipeline_delever import (  # noqa: F401
    DeleverMixin,
    _optional_risk_number,
    _risk_number,
)
from src.pipeline_risk_gate import RiskGate
from src.risk_gate_build import RiskGateSlot

# Step 7 of docs/PIPELINE_SPLIT_PLAN.md: the research-continuity cluster
# (change detectors, carry-forward, Form-4 backlog, seat healing) moved to a
# mixin module. `CarryForward` travelled with it because only those bodies
# construct it and that module may not import this one; it is re-exported here
# so `from src.pipeline import CarryForward` keeps working.
from src.pipeline_research_continuity import (  # noqa: F401
    CarryForward,
    ResearchContinuityMixin,
)
from src.pipeline_intraday import IntradayMixin
from src.pipeline_prompt_facts import (  # noqa: F401
    PromptFactsMixin,
    _PM_PROFILE_SYMBOL_CAP,
    _missed_ops_quality_metrics,
    _valuation_signal_from,
)
from src.pipeline_stages import (
    DecisionStage,
    ExecutionStage,
    MorningResearchStage,
    RiskStage,
)

# RE-EXPORT MIRROR (pipeline split, run gates): the paid-analysis gate and the
# two pre-decision halt gates moved VERBATIM to function-only modules.
# `TradingPipeline` keeps a one-line shim per moved name below, so every
# `self._x(...)` caller and `patch.object(TradingPipeline, "_x")` is untouched.
from src import pipeline_cost_gate as _cost_gate
from src import pipeline_halt_gates as _halt_gates
from src.portfolio_constructor.refusal_recorder import TradeRefusalRecorder
from src.portfolio_constructor import PortfolioConstructor
from src.cash_park_retired import release_retired_cash_park, retired_cash_park_symbol
from src.sessions.live_session_context_session import LiveSessionContextSession
from src.sessions.live_context_resolve_session import LiveContextResolveSession
from src.sessions.quarterly_meta_session import QuarterlyMetaReflectionSession
from src.sessions.morning_session import MorningSession
from src.storage.db import Database
from src.cost_circuit import (
    LLMCostCircuitBreaker,
)
from src.models import (
    NewsIntelligenceReport,
    RiskVerdict,  # noqa: F401
)

logger = logging.getLogger(__name__)

# Lives in a leaf module so a session can catch it; re-exported here.
from src.sessions.termination import SessionTerminated  # noqa: E402,F401
from src.pipeline_parts import (  # noqa: E402
    evening as _evening,
    kill_repair as _kill_repair,
    morning_helpers as _morning_helpers,
    pnl_gaps as _pnl_gaps,
    review as _review,
)


# `HARD_BLOCK_RULES` now lives in `src/risk/rules.py`, beside the engine that
# emits the rule names, so the RISK SEAT'S RENDERER can classify an entry
# without importing the pipeline (which imports the seat — a cycle). Re-
# exported here because this is the name every caller and test already
# imports, and moving the import site would be churn with no benefit.
from src.risk.rules import HARD_BLOCK_RULES  # noqa: E402,F401


class _MissingCollaborator:
    """Stand-in for a TradingPipeline attribute that a `__new__`-built test double
    never set. The session shims below read every collaborator up front; this
    defers the AttributeError to first USE, exactly where the inline body raised it."""

    def __init__(self, name: str) -> None:
        self._name = name

    def _raise(self, *_a, **_k):
        raise AttributeError(f"'TradingPipeline' object has no attribute '{self._name}'")

    __call__ = __iter__ = __len__ = __bool__ = _raise

    def __getattr__(self, attr):
        self._raise()


class TradingPipeline(
    ProtectionMixin,
    PromptFactsMixin,
    DeleverMixin,
    ExitEngineMixin,
    ResearchContinuityMixin,
    IntradayMixin,
):
    admission = AdmissionSlot()
    risk_gate = RiskGateSlot()
    #: Set in __init__ from `risk.kill_switch_path`. Declared here so an
    #: instance built without __init__ (tests do this) reads None rather than
    #: raising: an unconfigured switch is INERT, never armed. Real enforcement
    #: is at the broker seam, where __init__ always runs in production.
    _kill_switch_path: "Path | None" = None

    def __init__(self, config: AppConfig):
        self.config = config
        self.market = MarketDataProvider()
        self.macro = MacroDataProvider(
            api_key=config.api_keys.fred,
            request_timeout_s=config.macro.request_timeout_s,
            max_retries=config.macro.max_retries,
            retry_backoff_base_s=config.macro.retry_backoff_base_s,
            retry_backoff_max_s=config.macro.retry_backoff_max_s,
            retry_backoff_jitter_s=config.macro.retry_backoff_jitter_s,
            breaker_after_failed_series=config.macro.breaker_after_failed_series,
            total_fetch_deadline_s=config.macro.total_fetch_deadline_s,
        )
        # Forward calendar of scheduled macro releases (FRED's free
        # release-dates API). Same host and same failure mode as `self.macro`,
        # so it reuses that feed's operator-set retry/backoff policy verbatim
        # and carries only its own, much tighter, wall-clock ceiling — see
        # src/config.py::EventRiskConfig.
        self.event_calendar = MacroEventCalendarProvider(
            api_key=config.api_keys.fred,
            request_timeout_s=config.macro.request_timeout_s,
            max_retries=config.macro.max_retries,
            retry_backoff_base_s=config.macro.retry_backoff_base_s,
            retry_backoff_max_s=config.macro.retry_backoff_max_s,
            retry_backoff_jitter_s=config.macro.retry_backoff_jitter_s,
            breaker_after_failed_releases=config.macro.breaker_after_failed_series,
            total_fetch_deadline_s=config.event_risk.calendar_deadline_s,
        )
        # FOMC meeting dates, from the Federal Reserve's own free calendar.
        # A separate provider from the one above because it is a different host
        # (federalreserve.gov, not FRED) with its own timeout, its own deadline
        # and a disk cache — see src/data/event_calendar.py's docstring for the
        # live evidence behind the source choice. The backoff CURVE is still
        # the macro feed's: that is a generic retry policy, not a host fact.
        self.fomc_calendar = FOMCCalendarProvider(
            request_timeout_s=config.event_risk.fomc_request_timeout_s,
            max_retries=config.event_risk.fomc_max_retries,
            retry_backoff_base_s=config.macro.retry_backoff_base_s,
            retry_backoff_max_s=config.macro.retry_backoff_max_s,
            retry_backoff_jitter_s=config.macro.retry_backoff_jitter_s,
            total_fetch_deadline_s=config.event_risk.fomc_deadline_s,
            cache_path=config.event_risk.fomc_cache_path,
            cache_ttl_days=config.event_risk.fomc_cache_ttl_days,
        )

        def _key_for(model: str, explicit_provider: str | None = None) -> str:
            """Return the right API key based on (explicit provider, else
            model-name prefix) — the SAME resolve_provider() BaseAgent.__init__
            uses, so this can never pick a different provider than the client
            construction it's keying for."""
            from src.agents.base import resolve_provider

            provider = resolve_provider(model, explicit_provider)
            return {
                "deepseek": config.api_keys.deepseek,
                "openai": config.api_keys.openai,
                "openrouter": config.api_keys.openrouter,
                "google": config.api_keys.google,
            }.get(provider, config.api_keys.anthropic)

        # Cross-provider failover credential — resolved ONCE from the
        # process-wide `config.llm.fallback_provider`/`fallback_model`
        # (2026-08-31 owner decision) via the SAME `_key_for` closure every
        # agent's primary key uses, so config.py's AppConfig._check_llm_
        # provider_keys and this construction site can never pick different
        # credentials for the same configured fallback.
        _fallback_api_key = _key_for(config.llm.fallback_model, config.llm.fallback_provider)
        # Route 3 credential — a genuinely DIFFERENT model (see
        # config.llm.tertiary_model). Resolved through the same closure for
        # the same reason. Empty when route 3 is switched off, which
        # BaseAgent._tertiary_reachable reads as "no third rung".
        _tertiary_api_key = (
            _key_for(config.llm.tertiary_model, config.llm.tertiary_provider)
            if (config.llm.tertiary_model or "").strip()
            else ""
        )
        # Route 3's second-road substitute (2026-09-30). Same closure again,
        # so the credential can never disagree with the provider
        # `select_tertiary_route` picks. Empty switches the substitution off,
        # which BaseAgent reads as "keep the configured tertiary".
        _tertiary_alt_api_key = (
            _key_for(config.llm.tertiary_alt_model, config.llm.tertiary_alt_provider)
            if (config.llm.tertiary_alt_model or "").strip()
            else ""
        )
        self.tech_analyst = TechAnalystAgent(
            api_key=_key_for(config.llm.tech_analyst_model, config.llm.tech_analyst_provider),
            model=config.llm.tech_analyst_model,
            max_tokens=config.llm.get_max_tokens("tech_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.tech_analyst_provider,
            provider_order=config.llm.get_provider_order("tech_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
            # The standing sheet states this seat's history depth back to it.
            # Passed from the SAME config object `market.get_ohlcv` is called
            # with, so the brief and the fetch cannot disagree (board item 168).
            lookback_days=config.trading.lookback_days,
        )
        self.portfolio_manager = PortfolioManagerAgent(
            api_key=_key_for(config.llm.portfolio_manager_model, config.llm.portfolio_manager_provider),
            model=config.llm.portfolio_manager_model,
            max_tokens=config.llm.get_max_tokens("portfolio_manager"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.portfolio_manager_provider,
            provider_order=config.llm.get_provider_order("portfolio_manager"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
            # Same reason as the Risk Manager below, and it bites harder here:
            # without this the sizing seat's sheet falls back to
            # `load_risk_config_from_settings(config/settings.yaml)` — a
            # HARD-CODED path — while `main.py` builds this pipeline from
            # whatever `--config` names. Run the desk against any other
            # settings file and the seat that picks the sizes would be shown
            # the default file's limits while the engine enforced the chosen
            # one. That is the two-homes defect this change exists to remove,
            # on the very seat it is about.
            risk_config=config.risk,
        )
        self.risk_manager = RiskManagerAgent(
            api_key=_key_for(config.llm.risk_manager_model, config.llm.risk_manager_provider),
            model=config.llm.risk_manager_model,
            max_tokens=config.llm.get_max_tokens("risk_manager"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.risk_manager_provider,
            provider_order=config.llm.get_provider_order("risk_manager"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
            # The reviewer's standing sheet renders its limits from THIS
            # object (`{{risk.*}}` placeholders, src/agents/prompt_limits.py),
            # which is the same `config.risk` the engine below is built from.
            # Passing it explicitly means the seat cannot be briefed against a
            # settings file other than the one this process is running on.
            risk_config=config.risk,
        )
        self.risk_engine = RiskRuleEngine(build_risk_config(config))
        self.position_reviewer = PositionReviewerAgent(
            api_key=_key_for(config.llm.position_reviewer_model, config.llm.position_reviewer_provider),
            model=config.llm.position_reviewer_model,
            max_tokens=config.llm.get_max_tokens("position_reviewer"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.position_reviewer_provider,
            provider_order=config.llm.get_provider_order("position_reviewer"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.evening_analyst = EveningAnalystAgent(
            api_key=_key_for(config.llm.evening_analyst_model, config.llm.evening_analyst_provider),
            model=config.llm.evening_analyst_model,
            max_tokens=config.llm.get_max_tokens("evening_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.evening_analyst_provider,
            provider_order=config.llm.get_provider_order("evening_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.news_analyst = NewsAnalystAgent(
            api_key=_key_for(config.llm.news_analyst_model, config.llm.news_analyst_provider),
            model=config.llm.news_analyst_model,
            max_tokens=config.llm.get_max_tokens("news_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.news_analyst_provider,
            provider_order=config.llm.get_provider_order("news_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.macro_analyst = MacroAnalystAgent(
            api_key=_key_for(config.llm.macro_analyst_model, config.llm.macro_analyst_provider),
            model=config.llm.macro_analyst_model,
            max_tokens=config.llm.get_max_tokens("macro_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.macro_analyst_provider,
            provider_order=config.llm.get_provider_order("macro_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        # sec_user_agent reuses config.smart_money.user_agent — the same
        # contact-bearing UA this repo already sends to SEC EDGAR for Form 4
        # — rather than inventing a second politeness convention for the
        # "SEC Press Releases" feed added 2026-08-29 (src/data/news.py).
        # per_symbol_* (2026-08-30 owner decision — src/data/news.py audit
        # block): every cap an operator can tune lives in config.news, never
        # a module constant, same rule already applied to max_prompt_items.
        self.news_provider = NewsDataProvider(
            sec_user_agent=config.smart_money.user_agent,
            per_symbol_enabled=config.news.per_symbol_enabled,
            per_symbol_max_symbols=config.news.per_symbol_max_symbols,
            per_symbol_max_prompt_items=config.news.per_symbol_max_prompt_items,
            per_symbol_requests_per_second=config.news.per_symbol_requests_per_second,
        )
        self.news_store = NewsStore()
        self.macro_store = MacroStore()
        self.tech_store = TechStore()
        self.earnings_analyst = EarningsAnalystAgent(
            api_key=_key_for(config.llm.earnings_analyst_model, config.llm.earnings_analyst_provider),
            model=config.llm.earnings_analyst_model,
            max_tokens=config.llm.get_max_tokens("earnings_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.earnings_analyst_provider,
            provider_order=config.llm.get_provider_order("earnings_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.smart_money_analyst = SmartMoneyAnalystAgent(
            api_key=_key_for(config.llm.smart_money_analyst_model, config.llm.smart_money_analyst_provider),
            model=config.llm.smart_money_analyst_model,
            max_tokens=config.llm.get_max_tokens("smart_money_analyst"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.smart_money_analyst_provider,
            provider_order=config.llm.get_provider_order("smart_money_analyst"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        sec_form4_provider = SECForm4Provider(
            search_url=config.smart_money.search_url,
            archives_url=config.smart_money.archives_url,
            data_dir=config.smart_money.data_dir,
            user_agent=config.smart_money.user_agent,
            request_timeout_s=config.smart_money.request_timeout_s,
            refresh_deadline_s=config.smart_money.refresh_deadline_s,
            watched_drain_deadline_s=config.smart_money.watched_drain_deadline_s,
            requests_per_second=config.smart_money.requests_per_second,
            lookback_days=config.smart_money.lookback_days,
            max_filings_per_refresh=config.smart_money.max_filings_per_refresh,
            max_observations=config.smart_money.max_observations,
            cluster_window_days=config.smart_money.cluster_window_days,
            min_cluster_owners=config.smart_money.min_cluster_owners,
            insider_calendar_routine_years=config.smart_money.insider_calendar_routine_years,
            insider_min_cadence_trades=config.smart_money.insider_min_cadence_trades,
            insider_cadence_min_mean_gap_days=config.smart_money.insider_cadence_min_mean_gap_days,
            insider_cadence_max_mean_gap_days=config.smart_money.insider_cadence_max_mean_gap_days,
            insider_cadence_max_gap_dispersion=config.smart_money.insider_cadence_max_gap_dispersion,
            insider_history_retention_days=config.smart_money.insider_history_retention_days,
        )
        # Congress (House + Senate) trading-disclosure cross-check, switched
        # on 2026-09-20 (config.smart_money.congress_enabled). Two independent
        # free sources fanned into the same SmartMoneySource protocol as SEC
        # Form 4 via CombinedSmartMoneyProvider — one source (or this whole
        # sub-provider) failing never blocks the other's evidence or the
        # rest of the run. See src/data/congressional_trading.py.
        congress_provider = None
        if config.smart_money.congress_enabled:
            congress_provider = CongressionalTradingProvider(
                kadoa_url=config.smart_money.congress_kadoa_url,
                congresswatch_url=config.smart_money.congress_congresswatch_url,
                data_dir=config.smart_money.congress_data_dir,
                user_agent=config.smart_money.user_agent,
                request_timeout_s=config.smart_money.congress_request_timeout_s,
                # Declared in config since 2026-09-04 and never passed until
                # 2026-09-19: the feed's own time budget, separate from Form 4's.
                refresh_deadline_s=config.smart_money.congress_refresh_deadline_s,
                max_trades_per_source=config.smart_money.congress_max_trades_per_source,
                assumed_max_disclosure_lag_days=(config.smart_money.congress_assumed_max_disclosure_lag_days),
                lookback_days=config.smart_money.congress_lookback_days,
                # Board item 52 deleted the shared insider-Form4 dollar
                # floors from `SmartMoneyConfig`; that item is scoped to
                # Form 4 only, so this congressional-trading gate is left
                # unchanged by taking `CongressionalTradingProvider`'s own
                # default floors (same $100k/$250k values) instead of the
                # now-deleted config fields.
                cluster_window_days=config.smart_money.cluster_window_days,
                min_cluster_owners=config.smart_money.min_cluster_owners,
                max_observations=config.smart_money.max_observations,
            )
        self.smart_money_provider = CombinedSmartMoneyProvider([sec_form4_provider, congress_provider])
        # The universe screen's pending-takeover check reads the same SEC
        # client (same rate limiter, same User-Agent, same CIK cache).
        self.sec_form4_provider = sec_form4_provider
        self.meta_reflector = MetaReflectorAgent(
            api_key=_key_for(config.llm.meta_reflector_model, config.llm.meta_reflector_provider),
            model=config.llm.meta_reflector_model,
            max_tokens=config.llm.get_max_tokens("meta_reflector"),
            fallback_api_key=_fallback_api_key,
            fallback_provider=config.llm.fallback_provider,
            fallback_model=config.llm.fallback_model,
            tertiary_api_key=_tertiary_api_key,
            tertiary_provider=config.llm.tertiary_provider,
            tertiary_model=config.llm.tertiary_model,
            tertiary_alt_api_key=_tertiary_alt_api_key,
            tertiary_alt_provider=config.llm.tertiary_alt_provider,
            tertiary_alt_model=config.llm.tertiary_alt_model,
            provider=config.llm.meta_reflector_provider,
            provider_order=config.llm.get_provider_order("meta_reflector"),
            reasoning_effort=config.llm.reasoning_effort,
            structured_output=config.llm.structured_output,
        )
        self.earnings_provider = EarningsDataProvider()
        # Guard 1 (2026-09-02): resolve the kill-switch flag path the same
        # way storage.db_path resolves a few lines down — relative to the
        # repo root, absolute paths passed through unchanged — so
        # `touch data/KILL_SWITCH` from the repo root (run.sh's own cwd) is
        # the file ops actually touches. Resolved ONCE here so the
        # broker-level enforcement (AlpacaBroker._kill_switch_active) and
        # this class's own early, alerting check (_kill_switch_halt_result)
        # can never disagree about which file they are each looking at.
        raw_kill_switch_path = config.risk.kill_switch_path
        kill_switch_path = Path(raw_kill_switch_path)
        if not kill_switch_path.is_absolute():
            kill_switch_path = Path(__file__).resolve().parent.parent / kill_switch_path
        self._kill_switch_path = kill_switch_path
        raw_storage_db_path = config.storage.db_path
        if raw_storage_db_path == ":memory:":
            # ``:memory:`` is a SQLite sentinel, not a relative filename.
            # Rewriting it under the repository creates a persistent DB and
            # lets otherwise-isolated tests/processes contaminate one another.
            self._storage_db_path = raw_storage_db_path
            # No on-disk DB to sit beside. Cwd-relative keeps hermetic
            # tests from flocking the production checkout.
            trade_updates_lease_path = Path("data") / ".trade_updates.lock"
        else:
            storage_db_path = Path(raw_storage_db_path)
            if not storage_db_path.is_absolute():
                storage_db_path = Path(__file__).resolve().parent.parent / storage_db_path
            self._storage_db_path = str(storage_db_path)
            trade_updates_lease_path = Path(self._storage_db_path).parent / ".trade_updates.lock"
        self.broker = AlpacaBroker(
            api_key=config.api_keys.alpaca_key,
            secret_key=config.api_keys.alpaca_secret,
            paper=config.alpaca.paper,
            max_position_pct=config.risk.max_position_pct,
            kill_switch_path=str(self._kill_switch_path),
            trade_updates_lease_path=str(trade_updates_lease_path),
            # ON since 2026-09-18. The only site that threads this
            # through — see `ExecutionConfig.fill_stream_enabled` for why it
            # spent a day off, what actually blocked it (a placeholder
            # credential, not the connection's timing), and how a refusal is
            # logged now.
            fill_stream_enabled=config.execution.fill_stream_enabled,
        )
        # Wire the broker as yfinance's fallback so a yfinance outage doesn't
        # blackout the technical analyst. Alpaca's daily bars cover the same
        # universe we trade on, so fallback coverage is effectively 100%.
        self.market.set_fallback_bars(self.broker.get_bars)
        self.db = Database(self._storage_db_path)
        self.db.initialize()
        from src.sentinel.cancel_attempts import install_cancel_recording as _count_cancels

        _count_cancels(
            broker=self.broker, conn_getter=lambda: getattr(getattr(self, "db", None), "conn", None)
        )  # every broker cancel becomes one order_attempts row
        self._wire_protective_stop_block_recorder()
        if BaseAgent._allow_unmetered_for_tests:
            # Hermetic unit tests use mocked SDKs and explicitly opt out in
            # tests/conftest.py. This flag is false in every application run.
            self.cost_circuit = None
        else:
            try:
                from src.cost_table import refresh_openrouter_pricing

                self.cost_circuit = LLMCostCircuitBreaker(
                    self._storage_db_path,
                    config.llm_cost_circuit,
                )
                # Pricing-staleness SPOF fix (2026-08-28): pass the
                # configured grace window/multiplier through so a stale-
                # but-recent cache is used (widened, logged loudly) instead
                # of latching the whole desk the moment openrouter.ai is
                # briefly unreachable past the cache's 24h freshness mark --
                # see the long note above refresh_openrouter_pricing in
                # src/cost_table.py.
                openrouter_pricing_ok = refresh_openrouter_pricing(
                    grace_period_hours=(config.llm_cost_circuit.openrouter_pricing_grace_period_hours),
                    max_stale_multiplier=(config.llm_cost_circuit.openrouter_pricing_stale_multiplier_max),
                )
                if not openrouter_pricing_ok:
                    self.cost_circuit.mark_unavailable(
                        RuntimeError(
                            "current official OpenRouter pricing is unavailable; paid calls cannot be bounded safely"
                        ),
                        agent_name="pricing_preflight",
                        attempts=0,
                    )
            except Exception as exc:  # safety work must still initialize
                logger.critical(
                    "Mandatory paid-analysis cost circuit failed to initialize; "
                    "all paid calls are suspended while broker safety remains live: %s",
                    exc,
                    exc_info=True,
                )
                existing = getattr(self, "cost_circuit", None)
                marker = getattr(existing, "mark_unavailable", None)
                if callable(marker):
                    marker(exc, agent_name="pricing_preflight", attempts=0)
                    self.cost_circuit = existing
                else:
                    self.cost_circuit = LLMCostCircuitBreaker.fail_closed(
                        self._storage_db_path,
                        config.llm_cost_circuit,
                        exc,
                        agent_name="circuit_startup",
                    )
        self._attach_cost_circuit_to_agents()
        # Deterministic Target → Orders translator. Phase 2 of the architecture:
        # the LLM (PM) emits TargetPositions (intent); the constructor does the
        # math that turns intent into concrete TradeDecision orders.
        # Spec §2.1/§2.2. The risk envelope lives in `risk:` config, not in the
        # constructor's dataclass defaults — the 0.5% per-trade figure the
        # constructor shipped with was a default nobody chose, and the owner
        # ratified 5% / 25% on 2026-08-27. Reading it here means the deployed
        # ceiling is the one `verify_commissioning.py` can see.
        self.portfolio_constructor = PortfolioConstructor(
            build_constructor_config(config, self.risk_engine.config),
            recorder=TradeRefusalRecorder(self._collab("db")),
        )
        # Phase 4 #1: morning research stage — parallel macro/news/tech/earnings
        # fan-out extracted from the inline nested-function block.
        self.morning_research_stage = MorningResearchStage(
            config=config,
            db=self.db,
            market=self.market,
            macro=self.macro,
            news_provider=self.news_provider,
            news_store=self.news_store,
            macro_store=self.macro_store,
            tech_store=self.tech_store,
            earnings_provider=self._collab("earnings_provider"),
            macro_analyst=self._collab("macro_analyst"),
            news_analyst=self._collab("news_analyst"),
            tech_analyst=self._collab("tech_analyst"),
            earnings_analyst=self._collab("earnings_analyst"),
            smart_money_provider=self._collab("smart_money_provider"),
            smart_money_analyst=self._collab("smart_money_analyst"),
            admit_smart_money_candidates_fn=self.admission._admit_transient_smart_money_symbols,
            admit_nominated_candidates_fn=self.admission._admit_nominated_external_symbols,
            admit_screened_universe_fn=self.admission._admit_screened_universe_symbols,
            event_calendar=self._collab("event_calendar"),
            fomc_calendar=self._collab("fomc_calendar"),
            has_actionable_signal_fn=RiskGate._has_actionable_signal_fn,
            live_session_context_fn=self._collab("_live_session_context"),
            run_news_update_fn=self._collab("_run_news_update"),
            load_earnings_analyses_fn=self._collab("_load_earnings_analyses"),
        )
        # Downstream stages for run_morning: decision → risk → execution.
        # They take a `pipeline` reference so they can reuse the 15+ memory /
        # filter / sizing helpers that still live on TradingPipeline. Those
        # helpers are the next extraction boundary — see pipeline_stages.py
        # header for the rationale.
        self.decision_stage = DecisionStage(pipeline=self)
        self.risk_stage = RiskStage(pipeline=self)
        self.execution_stage = ExecutionStage(pipeline=self)
        # Idle-cash sweeper (SGOV parking). Set unconditionally here; every
        # consumer reads it through self._sweeper().
        from src.execution.cash_sweep import CashSweeper

        self.cash_sweeper = CashSweeper(pipeline=self)
        # Exit orders still working at the broker — see the attribute's own
        # comment above `_register_exit_settlement`.
        self._unsettled_exit_orders = {}

    def _sweeper(self):
        """The cash sweeper, or None when disabled (or replaced by a non-sweeper).

        A direct read: `cash_sweeper` is set unconditionally in __init__, so
        a pipeline without it is a wiring fault and raises AttributeError here.
        """
        from src.execution.cash_sweep import sweeper_or_none

        return sweeper_or_none(self.cash_sweeper)

    def _typed_cash_sweeper(self):
        """The owned CashSweeper, or None when not a real one.

        The type gate for the retired-cash-park code in
        `src.cash_park_retired`, which may not import the broker seam
        itself. Read on every call, never captured: tests assign
        `cash_sweeper` after `__new__`, and a snapshot would freeze it.
        """
        from src.execution.cash_sweep import CashSweeper

        sweeper = self.cash_sweeper  # set unconditionally in __init__
        return sweeper if isinstance(sweeper, CashSweeper) else None

    def _retired_cash_park_symbol(self) -> str | None:
        """Thin shim: body moved to src/cash_park_retired.py."""
        return retired_cash_park_symbol(self._typed_cash_sweeper)

    def _release_retired_cash_park(self, run_id: str | None) -> None:
        """Thin shim: body moved to src/cash_park_retired.py."""
        release_retired_cash_park(self._typed_cash_sweeper, run_id)

    def _news_held_symbols(self, positions) -> list[str]:
        """Thin shim: body moved to src/cash_park.py."""
        return self._cash_park()._news_held_symbols(positions)

    def _cash_park(self) -> CashPark:
        """Standalone deployable-cash / held-set helpers (bodies moved to
        src/cash_park.py). Built per call so a collaborator swapped after construction
        is what the body sees. `_sweeper` stays on the host and is handed in as the
        callable the bodies read, so a test's `_sweeper` override is honoured; no lifted
        body is passed back in (see src/cost_circuit/parts/shim_guard.py)."""
        return CashPark(sweeper=self._sweeper)

    def _compute_deployable_cash(self, cash: float, positions) -> float:
        """Thin shim: body moved to src/cash_park.py."""
        return self._cash_park()._compute_deployable_cash(cash, positions)

    @staticmethod
    def _format_qty(qty: float) -> str:
        if float(qty).is_integer():
            return str(int(qty))
        return f"{qty:.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def _full_sell_qty(position_qty: float) -> float | None:
        if position_qty <= 0:
            return None
        return float(position_qty)

    def _total_pnl_since_reset(self, total_value: float) -> tuple[float | None, float | None, str | None]:
        return _pnl_gaps._total_pnl_since_reset(self, total_value)

    @staticmethod
    def _forced_close_side_and_qty(position_qty: float) -> tuple[str, float] | None:
        return _pnl_gaps._forced_close_side_and_qty(position_qty)

    @staticmethod
    def _trade_executed_or_pending(trade: dict) -> bool:
        return _pnl_gaps._trade_executed_or_pending(trade)

    @staticmethod
    def _resolve_live_context(snapshots: dict, symbols: list) -> tuple:
        """Thin shim: builds the standalone session and runs it (body moved to src/sessions/live_context_resolve_session.py)."""
        return LiveContextResolveSession().run(snapshots, symbols)

    def _live_session_context(self, symbols) -> dict[str, dict]:
        """Thin shim: builds the standalone session and runs it (body moved to src/sessions/live_session_context_session.py)."""
        return LiveSessionContextSession(
            resolve_live_context=self._collab("_resolve_live_context"),
            broker=self._collab("broker"),
        ).run(symbols)

    def _is_trading_day(self) -> bool:
        # False is reserved for a successful exchange-calendar answer saying
        # there is no session. Broker/provider faults propagate so the session
        # fails visibly and can be retried; they must not masquerade as a
        # terminal market_holiday completion.
        return self.broker.is_trading_day()

    # Realized-exit actions whose post-exit trajectory is worth auditing.
    # SWEEP_SELL is deliberately absent — parking churn is not a decision.
    # STOP_OUT (2026-08-28 ONDS/CCJ) belongs here even though it is not a
    # reviewer decision — precisely BECAUSE it isn't one: "did the market
    # force us out right before a bounce" is exactly the question this
    # audit exists to answer, and a forced exit is where the answer is
    # most likely to be uncomfortable.
    # TAKE_PROFIT stays for HISTORICAL rows: the auto trim that wrote it was
    # deleted 2026-09-12 and nothing writes the label any more.
    # RECONCILED_EXIT (item 173(a)): a recovered broker exit whose order_type
    # could not be proven a protective stop. Before 173(a) every recovered
    # exit was labelled STOP_OUT and thus audited here; keeping it out would
    # drop real closed exits from decision-quality auditing — exactly the
    # "did the market force us out before a bounce" question this audit
    # exists to answer.
    _EXIT_AUDIT_ACTIONS = (
        "SELL",
        "REDUCE",
        "EMERGENCY_SELL",
        "FORCE_DELEVER",
        "TAKE_PROFIT",
        "STOP_OUT",
        "RECONCILED_EXIT",
    )

    def _refresh_account_state(self):
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        price_map = {p.symbol: p.current_price for p in positions}
        return account, positions, price_map

    def _sync_positions_from_broker(self, positions=None) -> None:
        """Refresh the local SQLite `positions` table from broker truth.

        Broker is book of record. The local table is a derived snapshot for
        Mission Control journal / evening notifier / rehearsal consumers —
        it must not lag the broker after a session snapshot or after fills.

        `positions` is the already-fetched broker list when the caller has
        one (avoids a duplicate round-trip). Omit it to re-read the broker
        after execution. Fail-soft: a snapshot write must never abort a
        trading session.
        """
        try:
            snapshot = list(positions) if positions is not None else self.broker.get_positions()
            self.db.sync_positions(snapshot)
            self._record_short_overnight_gaps(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.error("local positions table refresh failed: %s", exc)

    def _record_short_overnight_gaps(self, positions) -> None:
        return _pnl_gaps._record_short_overnight_gaps(self, positions)

    def _run_news_update(
        self,
        run_id: str,
        session: str = "morning",
        universe: list[str] | None = None,
        held_symbols: list[str] | None = None,
        candidate_symbols: list[str] | None = None,
    ) -> "tuple[NewsIntelligenceReport | None, NewsCoverage | None]":
        return _pnl_gaps._run_news_update(self, run_id, session, universe, held_symbols, candidate_symbols)

    def _load_earnings_analyses(
        self, run_id: str, session: str = "morning", ctx: RunContext | None = None, universe: list[str] | None = None
    ) -> tuple[list, list]:
        return _pnl_gaps._load_earnings_analyses(self, run_id, session, ctx, universe)

    def _earnings_preprocess_symbols(self) -> list[str]:
        return _pnl_gaps._earnings_preprocess_symbols(self)

    # ---------------------------------------------------------------
    # Morning stages (extracted from the legacy monolithic run_morning).
    # Phase 4 #1 final wire-up: each stage is a method taking ctx; the
    # orchestrating run_morning just composes them. Stages can be tested
    # individually by constructing a ctx, populating the needed fields,
    # and calling the method directly.
    # ---------------------------------------------------------------

    def _atr_for_symbol(self, symbol: str) -> float | None:
        """ATR(14) from ~30 days of daily bars; None when unknowable.

        Used by the TRAIL_STOP noise-band clamp and the position-facts
        vol-unit metrics. Failure is always None (callers degrade to the
        pre-clamp behavior) — never raises. The body lives in
        `src.data.technical.atr_for_symbol`; this is the delegation.
        """
        from src.data.technical import atr_for_symbol

        return atr_for_symbol(getattr(self, "market", None), symbol)

    def _sweep_symbol(self) -> str | None:
        """The configured cash-park vehicle, or None when sweeping is off.

        Taken from `cash_sweep.symbol` rather than hardcoded to "SGOV" —
        the setting already exists and an operator who changes the vehicle
        must not have to change the risk engine too.
        """
        sweeper = self._sweeper()
        # An empty park symbol cannot reach here: CashSweepConfig rejects it at load.
        return getattr(sweeper, "symbol", None) if sweeper is not None else None

    def _install_sigterm_unwind(self, context: str):
        return _kill_repair._install_sigterm_unwind(self, context)

    def _repair_stops_on_kill(self, context: str) -> None:
        return _kill_repair._repair_stops_on_kill(self, context)

    @staticmethod
    def _report_kill_repair(context: str, outcomes: list) -> None:
        return _kill_repair._report_kill_repair(context, outcomes)

    def _add_missing_stops(self) -> list:
        return _kill_repair._add_missing_stops(self)

    def _restore_sigterm(self, previous) -> None:
        return _kill_repair._restore_sigterm(self, previous)

    def _execution_stage(self, ctx: RunContext) -> list[dict]:
        """Delegates to ExecutionStage (class lives in pipeline_stages.py)."""
        return self.execution_stage.run(ctx)

    def _risk_stage(self, ctx: RunContext) -> dict | None:
        """Delegates to RiskStage (class lives in pipeline_stages.py)."""
        return self.risk_stage.run(ctx)

    def _decision_stage(self, ctx: RunContext):
        """Delegates to DecisionStage (class lives in pipeline_stages.py)."""
        self.decision_stage.run(ctx)

    def _activate_cost_session(self, run_id: str, mode: str) -> None:
        """Body lives in `src.pipeline_cost_gate`; this shim keeps callers and patch targets."""
        return _cost_gate._activate_cost_session(self, run_id, mode)

    def _require_paid_analysis(self, agent_name: str) -> None:
        """Body lives in `src.pipeline_cost_gate`; this shim keeps callers and patch targets."""
        return _cost_gate._require_paid_analysis(self, agent_name)

    def _attach_cost_circuit_to_agents(self) -> None:
        """Body lives in `src.pipeline_cost_gate`; this shim keeps callers and patch targets."""
        return _cost_gate._attach_cost_circuit_to_agents(self)

    def _cost_circuit_status(self) -> dict:
        """Body lives in `src.pipeline_cost_gate`; this shim keeps callers and patch targets."""
        return _cost_gate._cost_circuit_status(self)

    @staticmethod
    def _parse_logged_agent_response(row: dict):
        """Parse stored fenced/prose-wrapped JSON exactly as live agents do.

        The body lives in `src.agents.logged_response`; this delegation
        keeps every `self._parse_logged_agent_response(row)` caller working.
        """
        from src.agents.logged_response import parse_logged_agent_response

        return parse_logged_agent_response(row)

    @staticmethod
    def _paid_suspended_payload(
        run_id: str,
        *,
        orders: list[dict] | None = None,
        error: BaseException | None = None,
        filings_waiting: list[dict] | None = None,
    ) -> dict:
        """Body lives in `src.pipeline_cost_gate`; this shim keeps callers and patch targets."""
        return _cost_gate._paid_suspended_payload(run_id, orders=orders, error=error, filings_waiting=filings_waiting)

    def _paid_suspension_after_late_safety(
        self,
        run_id: str,
        *,
        session: str,
        error: BaseException,
        where: str,
        orders: list[dict] | None = None,
        extra: dict | None = None,
    ) -> dict:
        """Body lives in `src.pipeline_cost_gate`; this shim keeps callers and patch targets."""
        return _cost_gate._paid_suspension_after_late_safety(
            self, run_id, session=session, error=error, where=where, orders=orders, extra=extra
        )

    def _kill_switch_halt_result(self, run_id: str, **extra) -> dict | None:
        """Body lives in `src.pipeline_halt_gates`; this shim keeps callers and patch targets."""
        return _halt_gates._kill_switch_halt_result(self, run_id, **extra)

    def _evidence_gate_skip(self, ctx, run_id: str, *, session: str = "morning") -> dict | None:
        """Body lives in `src.pipeline_halt_gates`; this shim keeps callers and patch targets."""
        return _halt_gates._evidence_gate_skip(self, ctx, run_id, session=session)

    def pickup_owner_intents(self) -> None:
        """Pick up owner intents, then (FREEZE step 2) settle resting exposure-adding orders.

        The ONE pickup point. Called by main.py once at startup (after the
        pipeline is built, before any session or the scheduler runs) and by
        scheduler._run_safe before EVERY scheduled job, the 30-minute checks
        included. The sweep runs at every pickup while Freeze is in force
        (`sweep_if_frozen` reads the flag), not only when the flag changes, so
        a sweep that failed is retried at the next pickup. Neither step ever
        stops a job; both failures are recorded.
        """
        try:
            from src.owner_intents import intake

            intake(getattr(getattr(self.config, "storage", None), "db_path", None))
        except Exception as exc:  # noqa: BLE001 - recorded below; a failed pickup never stops a job
            from src.sentinel.guarded import record_guarded_pass

            record_guarded_pass(self.broker, "pipeline.owner_intent_pickup", exc, log=logger)
        self._sweep_frozen_resting_orders()

    def _sweep_frozen_resting_orders(self) -> None:
        """FREEZE step 2: settle resting exposure-adding orders (only via `pickup_owner_intents`).

        A failed sweep is recorded and never stops the job (src/execution/freeze_cancel.py).
        """
        try:
            self.broker.sweep_frozen_resting_orders(getattr(getattr(self.config, "storage", None), "db_path", None))
        except Exception as exc:  # noqa: BLE001 - recorded below; a failed sweep never stops a job
            from src.sentinel.guarded import record_guarded_pass

            record_guarded_pass(self.broker, "pipeline.freeze_sweep", exc, log=logger)

    def run_morning(self) -> dict:
        return _morning_helpers.run_morning(self)

    def _record_name_coverage(self, ctx, _record) -> None:
        return _morning_helpers._record_name_coverage(self, ctx, _record)

    def _attach_evidence_freshness(self, result) -> None:
        return _morning_helpers._attach_evidence_freshness(self, result)

    def _record_account_snapshot(self, total_value, last_equity) -> None:
        return _morning_helpers._record_account_snapshot(self, total_value, last_equity)

    def _attach_pnl(self, result) -> None:
        return _morning_helpers._attach_pnl(self, result)

    def _persist_session_report(self, mode: str, result: dict) -> None:
        return _morning_helpers._persist_session_report(self, mode, result)

    def _run_morning_body(self) -> dict:
        """Thin shim: builds the standalone session and runs it (body moved to src/sessions/morning_session.py)."""
        return MorningSession(
            activate_cost_session=self._collab("_activate_cost_session"),
            compute_deployable_cash=self._collab("_compute_deployable_cash"),
            cost_circuit_status=self._collab("_cost_circuit_status"),
            decision_stage=self._collab("_decision_stage"),
            discharge_deferred_gross_ceiling=self._collab("_discharge_deferred_gross_ceiling"),
            drain_pending_protection_restores=self._collab("_drain_pending_protection_restores"),
            drain_pending_repegs=self._collab("_drain_pending_repegs"),
            enforce_gross_ceiling=self._collab("_enforce_gross_ceiling"),
            enforce_gross_ceiling_by_conviction=self._collab("_enforce_gross_ceiling_by_conviction"),
            evidence_gate_skip=self._collab("_evidence_gate_skip"),
            execution_stage=self._collab("_execution_stage"),
            force_delever=self._collab("_force_delever"),
            heal_lost_research_seats=self._collab("_heal_lost_research_seats"),
            install_sigterm_unwind=self._collab("_install_sigterm_unwind"),
            is_trading_day=self._collab("_is_trading_day"),
            kill_switch_halt_result=self._collab("_kill_switch_halt_result"),
            paid_suspension_after_late_safety=self._collab("_paid_suspension_after_late_safety"),
            reconcile_fills=self._collab("_reconcile_fills"),
            reconcile_orphan_pending_submits=self._collab("_reconcile_orphan_pending_submits"),
            reconcile_stop_coverage=self._collab("_reconcile_stop_coverage"),
            reconcile_stop_out_fills=self._collab("_reconcile_stop_out_fills"),
            repair_stops_on_kill=self._collab("_repair_stops_on_kill"),
            record_account_snapshot=self._collab("_record_account_snapshot"),
            release_retired_cash_park=self._collab("_release_retired_cash_park"),
            require_paid_analysis=self._collab("_require_paid_analysis"),
            restore_sigterm=self._collab("_restore_sigterm"),
            risk_stage=self._collab("_risk_stage"),
            surface_reconcile_outcomes=self._collab("_surface_reconcile_outcomes"),
            sync_positions_from_broker=self._collab("_sync_positions_from_broker"),
            broker=self._collab("broker"),
            config=self._collab("config"),
            market=self._collab("market"),
            morning_research_stage=self._collab("morning_research_stage"),
        ).run()

    def run_midday(self) -> dict:
        """13:00 ET — position reviewer, patient disposition."""
        return self.run_position_review(session_type="midday")

    def run_close(self) -> dict:
        """15:30 ET — position reviewer, act-on-trigger disposition.
        17.5 hours until next intraday control; genuine thesis triggers
        fire now rather than waiting for tomorrow morning."""
        return self.run_position_review(session_type="close")

    #: Metric keys snapshotted after every review and compared on the next one.
    #: Kept deliberately small — these are the numbers a "stalling" claim is
    #: actually about, and every one of them has a defined direction.
    #: `stop_loss` / `current_price` / `qty` are NOT metrics and are never
    #: scored. `stop_loss` and `current_price` are the two terms of
    #: `distance_to_stop_pct`, snapshotted so the next review can attribute
    #: a move in it to the market or to the desk's own stop (2026-09-18).
    #: `qty` supplies the SIDE that same recomputation needs to mirror the
    #: numerator correctly for a short (2026-09-18 follow-up, alongside the
    #: sign fix to `distance_to_stop_pct` itself). Snapshots written before
    #: 2026-09-18 lack all three; `compute_deltas` handles that explicitly.
    _REVIEW_METRIC_KEYS = (
        "thesis_progress_pct",
        "distance_to_stop_pct",
        "r_multiple",
        "pace",
        "days_held",
        "expected_horizon_sessions",
        "setup_type",
        "pace_status",
        "stop_loss",
        "current_price",
        "qty",
    )

    def _persist_review_metrics(self, position_facts: dict, *, run_id: str) -> None:
        return _review._persist_review_metrics(self, position_facts, run_id=run_id)

    def run_position_review(self, session_type: str = "midday") -> dict:
        return _review.run_position_review(self, session_type)

    def _collab(self, name: str):
        """A collaborator for a session object: the attribute, or a deferred-error stand-in."""
        try:
            return getattr(self, name)
        except AttributeError:
            return _MissingCollaborator(name)

    def _run_position_review_body(self, session_type: str) -> dict:
        return _review._run_position_review_body(self, session_type)

    def run_earnings_preprocess(self) -> dict:
        return _review.run_earnings_preprocess(self)

    def _run_earnings_preprocess_body(self) -> dict:
        return _review._run_earnings_preprocess_body(self)

    def run_evening(self) -> dict:
        return _evening.run_evening(self)

    def _persist_evening_report(self, result: dict) -> None:
        return _evening._persist_evening_report(self, result)

    def _run_evening_body(self) -> dict:
        return _evening._run_evening_body(self)

    def _evening_stop_proximity(self, positions) -> list[dict]:
        return _evening._evening_stop_proximity(self, positions)

    def _evening_earnings_proximity(self, positions) -> list[dict]:
        return _evening._evening_earnings_proximity(self, positions)

    def _expected_sessions_missing_today(self) -> list[str]:
        return _evening._expected_sessions_missing_today(self)

    def _maybe_run_quarterly_meta(self) -> dict | None:
        return _evening._maybe_run_quarterly_meta(self)

    def run_quarterly_meta_reflection(
        self,
        *,
        force: bool = False,
        period_end=None,
        lookback_days: int = 90,
        evolution_root: str = "data/evolution",
        prompts_dir: str | Path | None = None,
    ) -> dict:
        """Thin shim: builds the standalone session and runs it (body moved to src/sessions/quarterly_meta_session.py)."""
        return QuarterlyMetaReflectionSession(
            activate_cost_session=self._collab("_activate_cost_session"),
            paid_suspended_payload=self._collab("_paid_suspended_payload"),
            require_paid_analysis=self._collab("_require_paid_analysis"),
            pipeline_file=__file__,
            broker=self._collab("broker"),
            config=self._collab("config"),
            db=self._collab("db"),
            market=self._collab("market"),
            meta_reflector=self._collab("meta_reflector"),
        ).run(
            force=force,
            period_end=period_end,
            lookback_days=lookback_days,
            evolution_root=evolution_root,
            prompts_dir=prompts_dir,
        )

    def run_daily(self) -> dict:
        """The daily P&L CSV export; the body lives in src/pipeline_daily_export.py."""
        from src.pipeline_daily_export import run_daily_export

        return run_daily_export(self)
