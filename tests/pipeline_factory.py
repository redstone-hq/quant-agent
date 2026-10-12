"""The ONE way a test builds a TradingPipeline: through the real `__init__`.

Why this module exists
----------------------
81 test files (measured 2026-10-01) built the pipeline with
``TradingPipeline.__new__(TradingPipeline)`` and then bolted collaborators on
afterwards. A pipeline built that way never ran its constructor, so every
service the constructor wires (risk gate, stages, sweeper, ...) was missing or
wired to nothing — which is exactly why each service pulled out of
src/pipeline.py had to leave a delegating mixin behind that rebuilds the
service from live attributes on every call. The half-built object was the
cause; the delegators were the patch. This factory removes the cause.

How it works
------------
``build_pipeline(**stand_ins)`` loads the production ``config/settings.yaml``
through the production loader (sentinel credentials, repointed at the test's
isolated cwd, in-memory DB, retries off) and calls the REAL
``TradingPipeline.__init__``. Each keyword names a pipeline attribute and
supplies the object the test wants there:

* a name in ``CONSTRUCTOR_SITES`` is injected AT THE CONSTRUCTOR SITE — the
  class the constructor would have called is patched to hand back the
  stand-in — so every service the constructor wires sees it (the risk gate
  holds the test's ``db``, the market feed falls back to the test's
  ``broker``, ...). Overriding ``pipeline.db`` after construction would NOT do
  that, which is the whole point.
* any other name (a private method stub such as ``_atr_for_symbol``, a plain
  state attribute such as ``_last_symbol_sectors``) is set on the built
  instance; a method looked up at call time sees it.

Defaults: ``broker`` is a ``MagicMock`` (no test wants a real Alpaca client);
``db`` is a real in-memory ``Database``; everything else is the genuine
collaborator, built exactly as production builds it. Nothing here opens a
network connection: the data providers and model seats defer all I/O to their
methods, and tests/conftest.py refuses outbound HTTP anyway.

    pipeline = build_pipeline(broker=my_broker)
    pipeline = build_pipeline(db=MagicMock(), _atr_for_symbol=lambda s: 1.0)

tests/test_pipeline_new_ratchet.py counts the ``__new__`` sites here and on
``origin/main``; that number may only go down.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"

#: pipeline attribute -> the name in src.pipeline whose call builds it.
CONSTRUCTOR_SITES = {
    "broker": "AlpacaBroker",
    "db": "Database",
    "market": "MarketDataProvider",
    "macro": "MacroDataProvider",
    "event_calendar": "MacroEventCalendarProvider",
    "fomc_calendar": "FOMCCalendarProvider",
    "news_provider": "NewsDataProvider",
    "earnings_provider": "EarningsDataProvider",
    "news_store": "NewsStore",
    "macro_store": "MacroStore",
    "tech_store": "TechStore",
    "risk_engine": "RiskRuleEngine",
    "portfolio_constructor": "PortfolioConstructor",
    "tech_analyst": "TechAnalystAgent",
    "portfolio_manager": "PortfolioManagerAgent",
    "risk_manager": "RiskManagerAgent",
    "position_reviewer": "PositionReviewerAgent",
    "evening_analyst": "EveningAnalystAgent",
    "news_analyst": "NewsAnalystAgent",
    "macro_analyst": "MacroAnalystAgent",
    "earnings_analyst": "EarningsAnalystAgent",
    "smart_money_analyst": "SmartMoneyAnalystAgent",
    "meta_reflector": "MetaReflectorAgent",
    "sec_form4_provider": "SECForm4Provider",
    "morning_research_stage": "MorningResearchStage",
    "decision_stage": "DecisionStage",
    "risk_stage": "RiskStage",
    "execution_stage": "ExecutionStage",
}

_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "FRED_API_KEY",
)
SENTINEL_KEY = "test-sentinel-not-a-credential"

_RAW_SETTINGS: dict | None = None


def test_config(**overrides):
    """Production settings through the production loader, repointed at the
    test's isolated cwd (tests/conftest.py chdirs every test into a tmp dir).

    ``overrides`` are top-level sections merged over the file, e.g.
    ``test_config(trading={"universe": ["SPY"]})``.
    """
    global _RAW_SETTINGS
    from src.config import AppConfig, _walk_and_substitute

    if _RAW_SETTINGS is None:
        _RAW_SETTINGS = yaml.safe_load(SETTINGS.read_text())
    raw = yaml.safe_load(yaml.safe_dump(_RAW_SETTINGS))  # deep copy
    here = Path.cwd()
    raw.setdefault("storage", {})["db_path"] = ":memory:"
    raw.setdefault("risk", {})["kill_switch_path"] = str(here / "data" / "KILL_SWITCH")
    raw.setdefault("event_risk", {})["fomc_cache_path"] = str(here / "data" / "fomc_calendar.json")
    for section in ("smart_money", "universe_screen"):
        if section in raw and "data_dir" in raw[section]:
            raw[section]["data_dir"] = str(here / section)
    raw.setdefault("execution", {})["fill_stream_enabled"] = False
    raw.setdefault("macro", {}).update(
        {
            "max_retries": 0,
            "retry_backoff_base_s": 0.001,
            "retry_backoff_max_s": 0.001,
            "retry_backoff_jitter_s": 0.0,
        }
    )
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    env = {name: SENTINEL_KEY for name in _CREDENTIAL_ENV}
    return AppConfig(**_walk_and_substitute(raw, env))


ADMISSION_STANDINS = frozenset(
    {
        "_filter_supported_symbols",
        "_evaluate_external_admission_gates",
        "_universe_screen_enabled",
        "_universe_screen_sources",
        "_evaluate_screened_admission",
        "_form4_admission_is_current",
        "_admit_screened_universe_symbols",
        "_run_universe_screen",
        "_attach_universe_changes",
        "_admit_nominated_external_symbols",
        "_admit_transient_smart_money_symbols",
    }
)


def _give_mock_broker_an_empty_order_book(broker) -> None:
    """Explicit "open-order listing read, nothing open" on a mock broker.

    The entry-room budget subtracts open broker orders and FAILS CLOSED (zero
    room) on a listing it cannot read; a bare MagicMock's answer is
    unreadable. A test that configured the listing itself is left alone.
    """
    if not isinstance(broker, Mock):
        return
    listing = broker.list_open_orders_checked
    if listing.side_effect is None and isinstance(listing.return_value, Mock):
        listing.return_value = (True, [])


def build_pipeline(config=None, **stand_ins):
    """Construct a GENUINE TradingPipeline through the real ``__init__``.

    See the module docstring for what each keyword does. Returns the pipeline
    with every constructor-wired service holding the stand-ins.
    """
    import src.pipeline as pipeline_module
    from src.pipeline import TradingPipeline

    stand_ins.setdefault("broker", MagicMock(name="broker"))
    _give_mock_broker_an_empty_order_book(stand_ins["broker"])
    cfg = config if config is not None else test_config()

    with ExitStack() as stack:
        for attr, value in list(stand_ins.items()):
            site = CONSTRUCTOR_SITES.get(attr)
            if site is None or value is None:
                continue  # None means 'absent': set after, never built
            stack.enter_context(
                patch.object(
                    pipeline_module,
                    site,
                    _returning(value),
                )
            )
        pipeline = TradingPipeline(cfg)

    admission_standins = {
        attr: value for attr, value in stand_ins.items() if attr in ADMISSION_STANDINS and value is not None
    }
    if admission_standins:
        # The eleven admission names live on `AdmissionService` since the
        # delegating `AdmissionMixin` was deleted (2026-10-05). A test still
        # names them as pipeline stand-ins; they are wired to the service the
        # pipeline now reads, which is where the production call sites look.
        service = pipeline.admission
        for attr, value in admission_standins.items():
            setattr(service, attr, value)
        pipeline.admission = service

    for attr, value in stand_ins.items():
        if attr in admission_standins:
            continue
        if attr in CONSTRUCTOR_SITES and value is not None:
            built = getattr(pipeline, attr, None)
            assert built is value, (
                f"{attr!r} was not wired through the constructor site "
                f"{CONSTRUCTOR_SITES[attr]!r}; fix CONSTRUCTOR_SITES"
            )
        else:
            setattr(pipeline, attr, value)
    return pipeline


def _returning(value):
    """A constructor-site stand-in: whatever the constructor passes, the
    pipeline gets ``value`` (and ``.fail_closed``-style classmethods, if any
    test ever needs them, are not required by __init__)."""

    def _factory(*args, **kwargs):
        return value

    return _factory
