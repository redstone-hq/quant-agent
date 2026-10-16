import os
import pytest
from pathlib import Path


def test_load_config_from_yaml(tmp_path):
    yaml_content = """
api_keys:
  anthropic: "test-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY", "QQQ"]
  lookback_days: 120
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/quant_agent.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from src.config import load_config

    cfg = load_config(config_file)

    assert cfg.api_keys.anthropic == "test-key"
    assert cfg.risk.max_position_pct == 20
    assert cfg.trading.universe == ["SPY", "QQQ"]
    assert cfg.alpaca.paper is True


def test_load_config_env_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-123")
    yaml_content = """
api_keys:
  anthropic: "${ANTHROPIC_API_KEY}"
  fred: "direct-key"
  alpaca_key: "ak"
  alpaca_secret: "as"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from src.config import load_config

    cfg = load_config(config_file)
    assert cfg.api_keys.anthropic == "env-key-123"


def test_load_config_missing_env_var_raises(tmp_path):
    yaml_content = """
api_keys:
  anthropic: "${MISSING_VAR_THAT_DOES_NOT_EXIST}"
  fred: "key"
  alpaca_key: "ak"
  alpaca_secret: "as"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "m"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    import pytest
    from src.config import load_config

    # Missing required API keys now raise ValidationError
    with pytest.raises(Exception, match="API key"):
        load_config(config_file)


def test_load_config_requires_openai_key_for_selected_openai_model(tmp_path):
    yaml_content = """
api_keys:
  anthropic: "anthropic-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "gpt-5.4"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from src.config import load_config

    with pytest.raises(Exception, match="OPENAI_API_KEY"):
        load_config(config_file)


def test_load_config_allows_openai_only_when_all_models_are_openai(tmp_path):
    yaml_content = """
api_keys:
  anthropic: ""
  openai: "openai-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "gpt-5.4"
  news_analyst_model: "gpt-5.4"
  macro_analyst_model: "gpt-5.4"
  earnings_analyst_model: "gpt-5.4"
  portfolio_manager_model: "gpt-5.4"
  risk_manager_model: "gpt-5.4"
  position_reviewer_model: "gpt-5.4"
  evening_analyst_model: "gpt-5.4"
  meta_reflector_model: "gpt-5.4"
  fallback_provider: "openai"
  fallback_model: "gpt-5.4"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from src.config import load_config

    cfg = load_config(config_file)
    assert cfg.api_keys.openai == "openai-key"
    assert cfg.api_keys.anthropic == ""


def test_llm_config_rejects_tiny_max_tokens():
    """A garbage max_tokens (0 / negative / too-small) must fail at parse time,
    not silently reach the LLM provider and error opaquely."""
    from pydantic import ValidationError

    from src.config import LLMConfig

    for bad in (0, -1, 100):
        with pytest.raises(ValidationError):
            LLMConfig(max_tokens=bad)

    # A sane value loads fine
    cfg = LLMConfig(max_tokens=4096)
    assert cfg.max_tokens == 4096


def test_llm_config_get_max_tokens_falls_back_to_global():
    """No per-agent override set → every agent uses the global max_tokens."""
    from src.config import LLMConfig

    cfg = LLMConfig(max_tokens=8192)
    for agent in (
        "tech_analyst",
        "news_analyst",
        "macro_analyst",
        "earnings_analyst",
        "smart_money_analyst",
        "portfolio_manager",
        "risk_manager",
        "position_reviewer",
        "evening_analyst",
    ):
        assert cfg.get_max_tokens(agent) == 8192


def test_smart_money_provider_defaults_disabled_until_configured():
    from src.config import SmartMoneyConfig

    assert SmartMoneyConfig().enabled is False


def test_smart_money_sec_limits_fail_closed():
    from pydantic import ValidationError
    from src.config import SmartMoneyConfig

    with pytest.raises(ValidationError):
        SmartMoneyConfig(requests_per_second=10.1)
    with pytest.raises(ValidationError):
        SmartMoneyConfig(max_external_candidates=0)


def test_smart_money_insider_thresholds_default_to_pre_config_values():
    """These fields replaced module-level constants in
    `src/data/insider_signal.py` on 2026-08-28 (the owner rejects hardcoded
    classification thresholds on sight — every one must be an operator
    setting with a default here). The defaults must equal the values that
    module hardcoded before the change, or unconfigured behavior changes
    silently.

    One field is deliberately gone rather than changed:
    `insider_min_material_sell_fraction` was deleted on 2026-09-13. It was a
    cutoff no cited source measures for one Form 4 row. Scott & Xu (FAJ 2004)
    measure an aggregate stock-wide six-month unit, so their bands cannot
    justify a per-filing cutoff. The point of this test is that a default never
    drifts unnoticed, not that an unsourced knob has to be kept alive — see the
    2026-09-13 `docs/INCIDENT_HISTORY.md` entry for WORK.md items 52 and 63."""
    from src.config import SmartMoneyConfig

    config = SmartMoneyConfig()
    assert config.insider_calendar_routine_years == 3
    assert config.insider_history_retention_days == 5 * 366


def test_smart_money_insider_thresholds_are_operator_overridable():
    """A settings.yaml edit must reach the classifier without a code
    change — the whole point of moving these out of insider_signal.py."""
    from src.config import SmartMoneyConfig

    config = SmartMoneyConfig(
        insider_calendar_routine_years=2,
        insider_history_retention_days=1000,
    )
    assert config.insider_calendar_routine_years == 2
    assert config.insider_history_retention_days == 1000


def test_smart_money_insider_history_retention_must_cover_the_calendar_years():
    """A retention window shorter than the calendar-routine lookback would
    silently prune the very history the calendar test depends on before it
    could ever match — fail at config load, not with a quietly-worse
    classifier months later."""
    from pydantic import ValidationError
    from src.config import SmartMoneyConfig

    with pytest.raises(ValidationError):
        SmartMoneyConfig(
            insider_calendar_routine_years=5,
            insider_history_retention_days=400,
        )


def test_reconciliation_config_defaults_to_seven_day_lookback():
    """2026-08-28 ONDS/CCJ fix: a settings.yaml without a `reconciliation`
    section must still get a working stop-out reconciler, not a disabled
    one — unlike cash_sweep/intraday_scan, there is no safe 'off' state for
    accounting correctness, so the default is a real, working lookback
    rather than a feature flag defaulting False."""
    from src.config import ReconciliationConfig

    assert ReconciliationConfig().stop_out_lookback_days == 7


def test_reconciliation_config_lookback_bounds_are_enforced():
    from pydantic import ValidationError
    from src.config import ReconciliationConfig

    with pytest.raises(ValidationError):
        ReconciliationConfig(stop_out_lookback_days=0)
    with pytest.raises(ValidationError):
        ReconciliationConfig(stop_out_lookback_days=61)
    # In-bounds values are accepted at both ends.
    assert ReconciliationConfig(stop_out_lookback_days=1).stop_out_lookback_days == 1
    assert ReconciliationConfig(stop_out_lookback_days=60).stop_out_lookback_days == 60


def test_load_config_reconciliation_section_overrides_default(tmp_path):
    """A settings.yaml that DOES set `reconciliation` must actually take."""
    yaml_content = """
api_keys:
  anthropic: "test-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY", "QQQ"]
  lookback_days: 120
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/quant_agent.db"
reconciliation:
  stop_out_lookback_days: 3
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from src.config import load_config

    cfg = load_config(config_file)
    assert cfg.reconciliation.stop_out_lookback_days == 3


def test_llm_config_get_max_tokens_respects_per_agent_override():
    """When a per-agent override is set, it takes precedence over the global."""
    from src.config import LLMConfig

    cfg = LLMConfig(
        max_tokens=4096,
        portfolio_manager_max_tokens=16384,
        evening_analyst_max_tokens=32000,
    )
    assert cfg.get_max_tokens("portfolio_manager") == 16384
    assert cfg.get_max_tokens("evening_analyst") == 32000
    # Unspecified agents still fall back.
    assert cfg.get_max_tokens("tech_analyst") == 4096
    assert cfg.get_max_tokens("risk_manager") == 4096


def test_llm_config_get_max_tokens_unknown_agent_falls_back():
    """Accidental typo in agent name should not crash — fall back, not throw."""
    from src.config import LLMConfig

    cfg = LLMConfig(max_tokens=4096, portfolio_manager_max_tokens=16384)
    # Typo: "pm" instead of "portfolio_manager" — no field by that name → fallback.
    assert cfg.get_max_tokens("pm") == 4096
    assert cfg.get_max_tokens("nonexistent_agent") == 4096


def test_llm_config_rejects_tiny_per_agent_max_tokens():
    """Per-agent overrides get the same >=512 floor as the global."""
    from pydantic import ValidationError

    from src.config import LLMConfig

    with pytest.raises(ValidationError):
        LLMConfig(max_tokens=4096, portfolio_manager_max_tokens=100)
    # None (unset) is fine — means inherit.
    cfg = LLMConfig(max_tokens=4096, portfolio_manager_max_tokens=None)
    assert cfg.get_max_tokens("portfolio_manager") == 4096


def test_trading_config_rejects_empty_universe():
    """Empty universe → no data, no analyses, no trades all session.
    Pre-fix this loaded silently; catch at config load so a typo in
    settings.yaml doesn't silently degrade the day to no-op."""
    import pytest
    from src.config import TradingConfig, ScheduleConfig

    schedule = ScheduleConfig(morning="06:00", midday="12:00", evening="16:30")
    with pytest.raises(ValueError, match="at least 1 item"):
        TradingConfig(universe=[], lookback_days=60, schedule=schedule)


def test_trading_config_rejects_non_positive_lookback():
    """lookback_days <= 0 fails opaquely in pandas slicing downstream;
    floor at 1 (one day of bars is the absolute minimum for any indicator).
    """
    import pytest
    from src.config import TradingConfig, ScheduleConfig

    schedule = ScheduleConfig(morning="06:00", midday="12:00", evening="16:30")
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        TradingConfig(universe=["SPY"], lookback_days=0, schedule=schedule)
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        TradingConfig(universe=["SPY"], lookback_days=-5, schedule=schedule)


def test_risk_config_rejects_position_and_sector_bound_violations():
    """max_position_pct and max_sector_pct also bounded to (0, 100]
    for the same reasons (0 blocks any BUY; >100 is nonsense single-name).
    max_total_position_pct only floored at >0 — leverage can push it
    above 100 when allow_margin=true."""
    import pytest
    from src.config import RiskConfig

    def kw(**overrides):
        base = dict(
            max_position_pct=20,
            max_total_position_pct=90,
            require_stop_loss=True,
        )
        base.update(overrides)
        return base

    with pytest.raises(ValueError):
        RiskConfig(**kw(max_position_pct=0))
    with pytest.raises(ValueError):
        RiskConfig(**kw(max_position_pct=150))
    with pytest.raises(ValueError):
        RiskConfig(**kw(max_total_position_pct=0))
    # 150% total exposure is legal when allow_margin=true (just the
    # config check; runtime risk engine still has its own caps).
    cfg = RiskConfig(**kw(max_total_position_pct=150))
    assert cfg.max_total_position_pct == 150


def test_load_config_preserves_allow_margin_from_yaml(tmp_path):
    """Regression: the allow_margin flag must survive the settings.yaml → RiskConfig
    round-trip. Class-default False can mask a loader bug where the key is
    silently dropped. Lock both values explicitly."""
    from src.config import load_config

    base_yaml = """
api_keys:
  anthropic: "k"
  fred: "k"
  alpaca_key: "k"
  alpaca_secret: "k"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
  allow_margin: {margin}
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/t.db"
"""
    for yaml_bool, expected in (("false", False), ("true", True)):
        f = tmp_path / f"settings_{yaml_bool}.yaml"
        f.write_text(base_yaml.format(margin=yaml_bool))
        cfg = load_config(f)
        assert cfg.risk.allow_margin is expected, f"settings.yaml allow_margin={yaml_bool} should load as {expected}"

    # Omitting the key falls back to the class default (False).
    no_key_yaml = base_yaml.format(margin="false").replace("  allow_margin: false\n", "")
    f = tmp_path / "settings_default.yaml"
    f.write_text(no_key_yaml)
    cfg = load_config(f)
    assert cfg.risk.allow_margin is False


def test_llm_config_defaults_are_current_claude_model():
    """Stale claude-*-4-6 defaults are gone — if settings.yaml omits a model,
    agents fall back to a current, priced Claude model, not a 4-6."""
    from src.config import AGENT_NAMES, LLMConfig
    from src.cost_table import estimate_cost

    # Per-AGENT model fields only — excludes `fallback_model` (2026-08-31),
    # a single process-wide field that also happens to end in "_model" but
    # is not one of the per-agent defaults this test is about.
    defaults = {
        f: LLMConfig.model_fields[f].default
        for f in LLMConfig.model_fields
        if f.endswith("_model") and f[: -len("_model")] in AGENT_NAMES
    }
    assert set(defaults.values()) == {"claude-opus-4-7"}, defaults
    # and the default is actually priced (cost won't show $?.??)
    assert estimate_cost("claude-opus-4-7", 1000, 1000) is not None


def test_load_config_requires_deepseek_key_for_selected_deepseek_model(tmp_path):
    """A deepseek-* model with no DEEPSEEK_API_KEY must fail naming that key —
    NOT silently fall into the Anthropic bucket (the pre-fix regression)."""
    yaml_content = """
api_keys:
  anthropic: "anthropic-key"
  deepseek: ""
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "deepseek-v4-flash"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)
    from src.config import load_config

    with pytest.raises(Exception, match="DEEPSEEK_API_KEY"):
        load_config(config_file)


def test_load_config_deepseek_only_does_not_require_anthropic(tmp_path):
    """All-DeepSeek config with the DeepSeek key set, and the fallback ALSO
    pinned to deepseek (no cross-provider failover configured — a legitimate
    operator choice), loads clean and does NOT demand ANTHROPIC_API_KEY. A
    fallback credential is only mandatory when failover is actually reachable
    (2026-08-31) — here it deliberately is not, which is the modern shape of
    "failover is best-effort, not mandatory"."""
    yaml_content = """
api_keys:
  anthropic: ""
  deepseek: "deepseek-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "deepseek-v4-flash"
  news_analyst_model: "deepseek-v4-flash"
  macro_analyst_model: "deepseek-v4-flash"
  earnings_analyst_model: "deepseek-v4-flash"
  portfolio_manager_model: "deepseek-v4-flash"
  risk_manager_model: "deepseek-v4-flash"
  position_reviewer_model: "deepseek-v4-flash"
  evening_analyst_model: "deepseek-v4-flash"
  meta_reflector_model: "deepseek-v4-flash"
  fallback_provider: "deepseek"
  fallback_model: "deepseek-v4-flash"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)
    from src.config import load_config

    cfg = load_config(config_file)
    assert cfg.api_keys.deepseek == "deepseek-key"
    assert cfg.llm.tech_analyst_model == "deepseek-v4-flash"


# === Stage 1 (QAMC provider/model/correlation plumbing) ===

_BASE_YAML = """
api_keys:
  anthropic: "anthropic-key"
  {extra_keys}
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "{model}"
  {extra_llm}
  # anthropic-key is always present in this template above, so pinning the
  # cross-provider failover target here too means every _BASE_YAML-based
  # fixture satisfies AppConfig._check_llm_provider_keys's fallback-key
  # requirement (2026-08-31) without each call site having to reason about
  # it individually.
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""


def test_load_config_requires_openrouter_key_for_explicit_provider(tmp_path):
    """An agent with provider: openrouter and no OPENROUTER_API_KEY must fail
    naming that key — even though the model string alone (an Anthropic-shaped
    id) would otherwise bucket as Anthropic, which already has a key set."""
    yaml_content = _BASE_YAML.format(
        extra_keys="",
        model="anthropic/claude-3.5-sonnet",
        extra_llm='tech_analyst_provider: "openrouter"',
    )
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)
    from src.config import load_config

    with pytest.raises(Exception, match="OPENROUTER_API_KEY"):
        load_config(config_file)


def test_load_config_openrouter_only_does_not_require_anthropic(tmp_path):
    """An OpenRouter-only config (no anthropic/openai/deepseek key), with
    EVERY agent explicitly routed to openrouter (mirrors
    test_load_config_allows_openai_only_when_all_models_are_openai above),
    passes the 'at least one provider key' check and loads clean."""
    yaml_content = """
api_keys:
  anthropic: ""
  openrouter: "or-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "anthropic/claude-3.5-sonnet"
  news_analyst_model: "anthropic/claude-3.5-sonnet"
  macro_analyst_model: "anthropic/claude-3.5-sonnet"
  earnings_analyst_model: "anthropic/claude-3.5-sonnet"
  portfolio_manager_model: "anthropic/claude-3.5-sonnet"
  risk_manager_model: "anthropic/claude-3.5-sonnet"
  position_reviewer_model: "anthropic/claude-3.5-sonnet"
  evening_analyst_model: "anthropic/claude-3.5-sonnet"
  meta_reflector_model: "anthropic/claude-3.5-sonnet"
  tech_analyst_provider: "openrouter"
  news_analyst_provider: "openrouter"
  macro_analyst_provider: "openrouter"
  earnings_analyst_provider: "openrouter"
  portfolio_manager_provider: "openrouter"
  risk_manager_provider: "openrouter"
  position_reviewer_provider: "openrouter"
  evening_analyst_provider: "openrouter"
  meta_reflector_provider: "openrouter"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY"]
  lookback_days: 60
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/test.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)
    from src.config import load_config

    cfg = load_config(config_file)
    assert cfg.api_keys.openrouter == "or-key"
    assert cfg.llm.tech_analyst_provider == "openrouter"


def test_provider_field_omitted_config_loads_identically_to_pre_stage1(tmp_path):
    """A settings.yaml that predates the `provider` field entirely (today's
    shape) must still load, with every provider field defaulting to None —
    the backward-compatible case. Uses the default claude-opus-4-7 model
    (only anthropic key needed) so no *_provider field is exercised at all."""
    yaml_content = _BASE_YAML.format(extra_keys="", model="claude-opus-4-7", extra_llm="")
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)
    from src.config import load_config

    cfg = load_config(config_file)
    assert cfg.llm.tech_analyst_provider is None
    assert cfg.llm.get_provider("tech_analyst") is None


def test_invalid_provider_string_rejected_at_config_load(tmp_path):
    """A typo'd provider must fail loudly at config load, not silently fall
    through to prefix inference and pick an unintended provider."""
    yaml_content = _BASE_YAML.format(
        extra_keys="",
        model="gpt-5.5",
        extra_llm='tech_analyst_provider: "openrooter"',  # typo
    )
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)
    from src.config import load_config

    with pytest.raises(Exception):
        load_config(config_file)


def test_llm_config_get_provider_unknown_agent_returns_none():
    from src.config import LLMConfig

    cfg = LLMConfig(max_tokens=4096, tech_analyst_provider="openrouter")
    assert cfg.get_provider("tech_analyst") == "openrouter"
    assert cfg.get_provider("nonexistent_agent") is None


def test_check_llm_provider_keys_uses_resolve_provider_not_prefix_alone(tmp_path):
    """An explicit provider override must be able to DISAGREE with what the
    model string's prefix would imply, and the key requirement follows the
    override — proving _check_llm_provider_keys doesn't re-derive its own
    independent prefix logic (the triplication risk Stage 1 closes)."""
    from src.config import (
        AppConfig,
        ApiKeysConfig,
        AlpacaConfig,
        LLMConfig,
        RiskConfig,
        TradingConfig,
        ScheduleConfig,
        StorageConfig,
    )

    # A "gpt-"-prefixed model explicitly routed to openrouter must require
    # OPENROUTER_API_KEY, not OPENAI_API_KEY.
    with pytest.raises(Exception, match="OPENROUTER_API_KEY"):
        AppConfig(
            api_keys=ApiKeysConfig(anthropic="a", openai="o", fred="f", alpaca_key="ak", alpaca_secret="as"),
            alpaca=AlpacaConfig(base_url="https://paper-api.alpaca.markets", paper=True),
            llm=LLMConfig(max_tokens=4096, tech_analyst_model="gpt-5.5", tech_analyst_provider="openrouter"),
            risk=RiskConfig(max_position_pct=20, max_total_position_pct=90, max_sector_pct=40, require_stop_loss=True),
            trading=TradingConfig(
                universe=["SPY"],
                lookback_days=60,
                schedule=ScheduleConfig(morning="06:00", midday="12:00", evening="16:30"),
            ),
            storage=StorageConfig(db_path="data/test.db"),
        )


# --- Alpaca paper-only guard ---------------------------------------------
#
# "Alpaca Paper only; live trading is not authorized" is a hard boundary in
# CLAUDE.md / docs/STATE.md / AGENTS.md. Before this guard existed it was
# prose only: `paper: false` in settings.yaml would have pointed the whole
# decision chain at a live brokerage account with nothing to notice. These
# tests are the enforcement's regression net — if one of them ever has to be
# deleted, that deletion is the authorization decision, in the open.


def test_paper_false_is_rejected_at_config_load():
    from src.config import AlpacaConfig

    with pytest.raises(Exception, match="live trading is not authorized"):
        AlpacaConfig(base_url="https://paper-api.alpaca.markets", paper=False)


def test_live_base_url_is_rejected_even_when_paper_is_true():
    """A live `base_url` alongside `paper: true` is a contradiction.

    Nothing reads `base_url` today, so this combination would not actually
    trade live — but it would tell every future reader (and the next person
    wiring a client from config) that live is the configured venue. Reject
    the disagreement rather than leaving a misleading field in place.
    """
    from src.config import AlpacaConfig

    with pytest.raises(Exception, match="paper-api.alpaca.markets"):
        AlpacaConfig(base_url="https://api.alpaca.markets", paper=True)


def test_paper_config_still_loads_unchanged():
    from src.config import AlpacaConfig

    cfg = AlpacaConfig(base_url="https://paper-api.alpaca.markets", paper=True)
    assert cfg.paper is True


def test_shipped_settings_yaml_is_paper_only():
    """The config the deployment actually runs must satisfy the guard."""
    from pathlib import Path as _Path
    import yaml as _yaml

    raw = _yaml.safe_load((_Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text())
    assert raw["alpaca"]["paper"] is True
    assert "paper-api.alpaca.markets" in raw["alpaca"]["base_url"]


def test_full_config_load_rejects_live_trading(tmp_path):
    """End-to-end through load_config(), not just the sub-model."""
    from src.config import load_config

    config_file = tmp_path / "settings.yaml"
    config_file.write_text(
        _BASE_YAML.format(extra_keys="", model="claude-opus-4-7", extra_llm="").replace("paper: true", "paper: false")
    )
    with pytest.raises(Exception, match="live trading is not authorized"):
        load_config(config_file)


def test_provider_order_returns_the_seat_preference_or_none():
    from src.config import LLMConfig

    cfg = LLMConfig(
        max_tokens=4096,
        portfolio_manager_provider="openrouter",
        portfolio_manager_model="openai/gpt-5.5",
        portfolio_manager_provider_order=["openai/flex"],
    )
    assert cfg.get_provider_order("portfolio_manager") == ["openai/flex"]
    assert cfg.get_provider_order("risk_manager") is None
    assert cfg.get_provider_order("nonexistent_agent") is None


def test_provider_order_on_a_non_openrouter_seat_is_rejected():
    """An endpoint preference is an OpenRouter concept. Set on an Anthropic
    seat it would be silently ignored — which is how an operator ends up
    believing a seat runs on a cheaper tier that it never reached."""
    import pytest
    from src.config import LLMConfig

    with pytest.raises(ValueError, match="not 'openrouter'"):
        LLMConfig(
            max_tokens=4096,
            portfolio_manager_model="claude-opus-4-7",
            portfolio_manager_provider_order=["openai/flex"],
        )


def test_empty_provider_order_is_rejected_rather_than_read_as_no_preference():
    """`[]` is a preference that nothing may serve the seat, which reads as a
    typo far more often than as intent. Make the operator write null."""
    import pytest
    from src.config import LLMConfig

    with pytest.raises(ValueError, match="non-empty list"):
        LLMConfig(
            max_tokens=4096,
            portfolio_manager_provider="openrouter",
            portfolio_manager_model="openai/gpt-5.5",
            portfolio_manager_provider_order=[],
        )
    with pytest.raises(ValueError, match="non-empty strings"):
        LLMConfig(
            max_tokens=4096,
            portfolio_manager_provider="openrouter",
            portfolio_manager_model="openai/gpt-5.5",
            portfolio_manager_provider_order=["  "],
        )


def test_provider_order_absent_loads_exactly_as_before():
    """Additive-only: a settings.yaml that never mentions the field must
    produce None on every seat."""
    from src.config import AGENT_NAMES, LLMConfig

    cfg = LLMConfig(max_tokens=4096)
    assert all(cfg.get_provider_order(name) is None for name in AGENT_NAMES)


# === NotificationsConfig — Telegram alert tap-through link ===


def test_notifications_config_defaults_to_tailnet_cockpit_url():
    """Default target matches the tailnet address Tailscale Serve exposes
    for the qamc API (proxying tailnet-only port 443 to 127.0.0.1:8800),
    which mounts /cockpit (src/api/server.py app.mount("/cockpit", ...))."""
    from src.config import NotificationsConfig

    cfg = NotificationsConfig()
    assert cfg.mission_control_url == "https://ovh-vps.wallaby-bowfin.ts.net/cockpit/"


def test_notifications_config_allows_empty_url_to_disable_the_link():
    from src.config import NotificationsConfig

    cfg = NotificationsConfig(mission_control_url="")
    assert cfg.mission_control_url == ""


def test_notifications_config_rejects_non_http_scheme():
    """The value lands inside an href="..." attribute — reject anything
    that isn't http(s) (or empty) rather than trust it blindly."""
    import pytest
    from src.config import NotificationsConfig

    with pytest.raises(ValueError, match="http"):
        NotificationsConfig(mission_control_url="javascript:alert(1)")


def test_app_config_notifications_section_is_optional(tmp_path):
    """A settings.yaml without a `notifications:` section must still load,
    with the documented default URL — additive-only, like reconciliation
    and cash_sweep above."""
    yaml_content = """
api_keys:
  anthropic: "test-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  fallback_provider: "anthropic"
  fallback_model: "claude-opus-4-7"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY", "QQQ"]
  lookback_days: 120
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/quant_agent.db"
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from src.config import load_config

    cfg = load_config(config_file)
    assert cfg.notifications.mission_control_url == "https://ovh-vps.wallaby-bowfin.ts.net/cockpit/"


def test_shipped_settings_yaml_notifications_url_matches_default():
    from pathlib import Path as _Path
    import yaml as _yaml

    raw = _yaml.safe_load((_Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text())
    assert raw["notifications"]["mission_control_url"] == "https://ovh-vps.wallaby-bowfin.ts.net/cockpit/"


def test_load_config_rejects_renamed_free_failure_key(tmp_path):
    # `llm_cost_circuit.max_paid_sessions_per_mode_per_day` was renamed to
    # `max_free_failure_sessions_per_mode` (2026-08-29), which item 14
    # (OWNER-APPROVED 2026-09-02, docs/WORK.md) then deleted outright along
    # with the whole reservation layer it gated -- replaced by the plain
    # call-count backstop `max_calls_per_session`. There is no backwards-
    # compat alias for either old name: a settings file that still carries
    # one must fail loudly at load time rather than having pydantic's
    # default `extra="ignore"` silently drop it and fall back to a default.
    yaml_content = """
api_keys:
  anthropic: "test-key"
  fred: "fred-key"
  alpaca_key: "alpaca-key"
  alpaca_secret: "alpaca-secret"
alpaca:
  base_url: "https://paper-api.alpaca.markets"
  paper: true
llm:
  tech_analyst_model: "claude-sonnet-4-6"
  max_tokens: 4096
risk:
  max_position_pct: 20
  max_total_position_pct: 90
  max_sector_pct: 40
  require_stop_loss: true
trading:
  universe: ["SPY", "QQQ"]
  lookback_days: 120
  schedule:
    morning: "06:00"
    midday: "12:00"
    evening: "16:30"
storage:
  db_path: "data/quant_agent.db"
llm_cost_circuit:
  max_paid_sessions_per_mode_per_day: 8
"""
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml_content)

    from pydantic import ValidationError
    from src.config import load_config

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_file)

    assert "max_paid_sessions_per_mode_per_day" in str(exc_info.value)
    assert "max_calls_per_session" in str(exc_info.value)


# ---------------------------------------------------------------------------
# The account-level loss alarm — retired 2026-09-20, board item 32.
# ---------------------------------------------------------------------------
#
# `RiskConfig` inherits pydantic's default `extra="ignore"`. Without the
# validator these tests pin, a deployment whose settings.yaml still carried
# `max_daily_loss_pct` would load silently and an operator would believe a
# daily halt was armed when nothing in the desk reads it. That belief is a
# belief about loss protection, which is why this removal is guarded by
# code rather than only by the retired-mechanism prose scanner — and why
# these five keys are deliberately absent from `config/retired_mechanisms
# .yaml`'s symbol list (listing them there would flag the validator itself).
# The registry says so; until 2026-09-20 nothing tested it.

_DELETED_LOSS_ALARM_KEYS = (
    "max_daily_loss_pct",
    "daily_loss_risk_multiple",
    "drawdown_vol_sensitivity",
    "drawdown_5d_risk_multiple",
    "drawdown_20d_risk_multiple",
)


def _live_risk_kwargs(**overrides):
    base = dict(
        max_position_pct=20,
        max_total_position_pct=90,
        require_stop_loss=True,
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("key", _DELETED_LOSS_ALARM_KEYS)
def test_a_stale_settings_file_cannot_resurrect_the_loss_alarm(key):
    """Each deleted key is REFUSED on load, not ignored."""
    from pydantic import ValidationError
    from src.config import RiskConfig

    with pytest.raises(ValidationError) as excinfo:
        RiskConfig(**_live_risk_kwargs(**{key: 3.0}))
    message = str(excinfo.value)
    assert key in message
    # The refusal must say there is no replacement, or the next operator
    # goes looking for the new spelling of an account-level loss limit.
    assert "NO replacement" in message


def test_the_refusal_does_not_fire_on_an_ordinary_unknown_key():
    """`extra="ignore"` still applies to everything else, so this validator
    is a targeted refusal and not a new strictness regime that would fail
    every older settings file."""
    from src.config import RiskConfig

    cfg = RiskConfig(**_live_risk_kwargs(some_unrelated_future_key=1))
    assert cfg.max_position_pct == 20


@pytest.mark.parametrize("key", _DELETED_LOSS_ALARM_KEYS)
def test_the_live_settings_file_carries_none_of_them(key):
    """The repo's own settings.yaml must stay loadable."""
    import yaml

    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "settings.yaml").read_text())
    assert key not in (raw.get("risk") or {}), (
        f"config/settings.yaml carries risk.{key} — the account-level loss alarm is retired and this file will not load"
    )


def test_riskconfig_has_no_loss_alarm_field_left():
    from src.config import RiskConfig

    cfg = RiskConfig(**_live_risk_kwargs())
    for key in _DELETED_LOSS_ALARM_KEYS:
        assert not hasattr(cfg, key)
    assert not hasattr(cfg, "effective_max_daily_loss_pct")
