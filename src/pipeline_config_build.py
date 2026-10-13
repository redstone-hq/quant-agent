# Config-building functions lifted verbatim out of src/pipeline.py (pure move).
import math  # noqa: F401

from src.config import RiskConfig
from src.pipeline_delever import _optional_risk_number, _risk_number


def _threaded_risk_settings(risk_config, *names: str) -> dict[str, float]:
    """Real numeric risk settings, keyed by field name, ready to splat into
    `RiskConfig(...)`.

    A name whose value is NOT a real number is OMITTED from the dict rather
    than replaced with a literal, so pydantic applies the field's own
    declared default and the number keeps exactly ONE home in this file's
    source. The omission case is the MagicMock config many pipeline tests
    build, where attribute access auto-creates a child mock pydantic refuses.

    ZERO PASSES THROUGH, unlike `_risk_number`. `min_position_risk_pct` is
    declared `ge=0` — zero is a legal "no floor" — so a `> 0` read would hand
    the engine a floor nobody configured while the seat's standing sheet
    rendered the configured 0. A seat briefed on a number nothing enforces is
    the defect this whole change removes.
    """
    threaded: dict[str, float] = {}
    for name in names:
        value = getattr(risk_config, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        threaded[name] = float(value)
    return threaded


# ---------------------------------------------------------------------------
# The two objects that ENFORCE the desk's numeric limits.
#
# Lifted out of `TradingPipeline.__init__` so a test can build them from a
# candidate settings object and read back what the engine and the sizer would
# actually enforce. That matters because the parity these limits need is
# behavioural: `tests/test_risk_prompt_limits_live.py` asserts that a value a
# seat's standing sheet SHOWS is the value these objects CARRY. Parsing the
# source text of a keyword list could only ever prove a kwarg name was typed,
# not that the setting reached the object — a limit hard-coded at its current
# value would have satisfied it.
#
# NOTHING ELSE CHANGED IN THE MOVE. Both bodies are the code that ran inline.
# ---------------------------------------------------------------------------


def build_risk_config(config) -> RiskConfig:
    """The `RiskConfig` the deterministic risk engine is built from.

    Hand-enumerated: a declared setting left out falls back to the pydantic
    CLASS DEFAULT and settings.yaml is ignored for that field. See the
    comments inline for which are threaded and why the rest are not.
    """
    return RiskConfig(
        max_position_pct=config.risk.max_position_pct,
        max_total_position_pct=config.risk.max_total_position_pct,
        max_position_risk_pct=_risk_number(
            getattr(config.risk, "max_position_risk_pct", None),
            5.0,
        ),
        require_stop_loss=config.risk.require_stop_loss,
        # Codex r11 P2: previously omitted, defaulting to False even
        # when settings.yaml said True. Prompts + force_delever read
        # config.risk.allow_margin directly, so the agent saw "margin
        # OK" while the deterministic engine still applied cash_only.
        # Result: a user opting in to margin had their BUYs blocked
        # by a hard rule the agent didn't know was active.
        allow_margin=config.risk.allow_margin,
        # SAME OMISSION CLASS AS `allow_margin` DIRECTLY ABOVE. This
        # `RiskConfig(...)` is hand-enumerated, so any declared setting
        # left out of it silently falls back to the pydantic CLASS
        # DEFAULT and settings.yaml is ignored for that field. 22 of the
        # declared risk settings were in that state before this change;
        # today every one of those defaults happens to equal the settings
        # value, so nothing is live-wrong — it is latent, and
        # `allow_margin` directly above is the proof that it does not
        # stay latent forever.
        #
        # The seven threaded here are the ones the Risk Manager's and
        # Portfolio Manager's standing sheets now RENDER from settings.yaml (see
        # src/agents/prompt_limits.py). Rendering a value into the
        # reviewer's briefing while the engine enforced a different
        # object's default would be the same two-homes defect this
        # change removes, pointed the other way. Threading them makes
        # "the seat is briefed against what the engine enforces" true
        # rather than merely intended, and `tests/
        # test_risk_prompt_limits_live.py` now pins it.
        #
        # The other 15 are NOT touched here: they predate this work, they
        # are not live-wrong, and sweeping them would change enforcement
        # nobody has reviewed. Recorded in docs/WORK.md instead.
        # Splatted through `_threaded_risk_settings`, NOT read through
        # `_risk_number`: a `_risk_number(x, <literal>)` per field would
        # type seven more copies of seven limits into this file, which is
        # the two-homes defect this change removes, pointed inward. The
        # helper omits a non-numeric (MagicMock) read instead, leaving
        # pydantic's own field default as the single fallback home — and
        # it lets a legal 0 through, which `_risk_number` does not.
        **_threaded_risk_settings(
            getattr(config, "risk", None),
            "min_position_risk_pct",
            "max_portfolio_risk_pct",
            # Rendered into the Portfolio Manager's sheet by the same
            # mechanism, so they carry the same parity requirement.
            "max_cluster_risk_share_pct",
            "max_gross_exposure_x",
        ),
    )


def build_constructor_config(config, risk_engine_config):
    """The `ConstructorConfig` the deterministic sizer is built from.

    Takes the risk engine's ALREADY-RESOLVED config rather than re-deriving
    from settings, so the ceilings the sizer shrinks against are provably the
    identical objects the engine enforces.

    This is the enforcement home for four settings the Portfolio Manager's
    standing sheet renders — `min_position_risk_pct`, `max_portfolio_risk_pct`,
    `max_cluster_risk_share_pct` and `max_gross_exposure_x` — none of which
    `src/risk/rules.py` reads at all. The sizing seat's parity is against THIS
    object, not only against `RiskConfig`.
    """
    from src.portfolio_constructor import ConstructorConfig
    from src.config import RiskConfig

    _risk_cfg = getattr(config, "risk", None)

    def _declared_default(name: str, literal: float) -> float:
        """The default `RiskConfig` itself declares for `name`.

        The fallback literals below used to be hand-copied from
        `src/config.py`, and one of them silently rotted: this function
        passed 1.5 for `min_stop_atr_multiple` long after the declared
        default became 2.5 (2026-09-10), so any path reaching here with the
        setting ABSENT sized live stops against a floor nobody ratified. A
        literal repeated in two files is drift waiting to happen, so the
        declared default now WINS; the literal survives only as the last
        resort for a field `RiskConfig` declares with no default of its own
        (`max_position_pct` is required, so it has none).
        """
        field = RiskConfig.model_fields.get(name)
        if field is not None:
            declared = getattr(field, "default", None)
            if not isinstance(declared, bool) and isinstance(declared, (int, float)):
                return float(declared)
        return float(literal)

    def _risk_setting(name: str, default: float, allow_zero: bool = False) -> float:
        """Read a risk ceiling, or the ratified default.

        Coerced through a real float check rather than trusted from
        `getattr`: many tests construct the pipeline against a MagicMock
        config, where attribute access auto-creates a child mock that is
        neither the default nor a number — and a MagicMock reaching the
        sizing arithmetic fails with an opaque TypeError deep inside the
        constructor. Same defensive posture as `_coerce_token_count`.

        `allow_zero` for the one setting where 0 is a CONFIGURED value rather
        than an absent one: `min_position_risk_pct` is declared `ge=0`, so 0
        means "no floor". Swallowing it into the default would size under a
        floor nobody configured while the sizing seat's sheet rendered the 0.
        """
        default = _declared_default(name, default)
        value = getattr(_risk_cfg, name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        if allow_zero and value >= 0:
            return float(value)
        return float(value) if value > 0 else default

    return ConstructorConfig(
        risk_budget_pct=_risk_setting("max_position_risk_pct", 5.0),
        min_risk_pct=_risk_setting("min_position_risk_pct", 0.5, allow_zero=True),
        max_portfolio_risk_pct=_risk_setting("max_portfolio_risk_pct", 25.0),
        max_cluster_risk_share_pct=_risk_setting("max_cluster_risk_share_pct", 40.0),
        # Same setting the risk engine enforces (line ~326), so the
        # constructor sizes under the ceiling rather than proposing orders
        # `max_position_pct` — a HARD_BLOCK rule — will drop outright.
        max_position_pct=_risk_setting("max_position_pct", 65.0),
        # No `min_order_usd`: board item 183 deleted
        # `ConstructorConfig.min_order_usd` on 2026-09-26. Nothing in the
        # constructor read it — the one call that forwarded it reached an
        # argument `apply_gross_ceiling` has ignored since 2026-09-24.
        # Stage 3 (shorts): nothing direction-specific is passed. A
        # short's single-name ceiling is `max_position_pct` above, the
        # same as a long's, and owner ruling 2026-10-04 deleted the
        # short-side gap haircut outright — same math, same behaviour.
        # Spec §11.2 — same "size under the hard block" pattern again.
        # `max_gross_exposure` is in HARD_BLOCK_RULES, so an entry that
        # breaches the ceiling would be DROPPED rather than taken
        # smaller without this. The per-session ladder step is passed to
        # `construct_orders`; this is the standing cap it starts from.
        max_gross_exposure_x=_risk_setting("max_gross_exposure_x", 2.0),
        # The cash park is not exposure. Read from the SAME config gate
        # `_sweeper()` uses (enabled + symbol) so the sizing gate and the
        # execution gate can never disagree about what counts.
        cash_park_symbol=(
            getattr(getattr(config, "cash_sweep", None), "symbol", None)
            if bool(getattr(getattr(config, "cash_sweep", None), "enabled", False))
            else None
        ),
        # 1.5 -> 2.5 on 2026-09-30 (board item 90). This fallback was
        # left behind by the 2026-09-10 base move and still named the
        # value the desk EXPLICITLY ABANDONED: 1.5 was the Sweeney MAE
        # fit to this desk's own ~2-week history, dropped both because
        # that window's seat outputs were later found to misreport
        # confidence/data quality AND because fitting a threshold to
        # past outcomes is barred outright (docs/OUTCOME.md, "No
        # arbitrary numbers, ever", the 2026-09-12 correction). Not
        # reachable on the production path today — a real `RiskConfig`
        # always carries the attribute and pydantic coerces the YAML —
        # so this is a stale constant, not a live defect, and it is
        # corrected rather than reported as one. `_risk_setting`'s own
        # docstring says it returns "the ratified default", and 2.5 is
        # the ratified default. The ledger gate cannot see this line:
        # `src/number_sources.py` names "fallback arguments" among the
        # shapes it structurally cannot scan, which is why every
        # fallback in this block is now pinned to its `RiskConfig`
        # field default by `tests/test_risk_setting_fallbacks.py`.
        min_stop_atr_multiple=_risk_setting("min_stop_atr_multiple", 2.5),
        # Spec §12.1 — a stop sitting at a level the system COMPUTED is
        # honoured whatever the band says, down to a deterministic 1x ATR
        # floor. Same "wire from the ratified setting, not the
        # constructor's own default" pattern as every ceiling above.
        # There is no `level_match_atr_tolerance` to wire any more: item
        # 46 (2026-09-13) deleted it, and the constructor reads the
        # match tolerance off the level zone's own definition.
        absolute_min_stop_atr_multiple=_risk_setting(
            "absolute_min_stop_atr_multiple",
            1.0,
        ),
        # Phase 12.1, 2026-09-03 — how many prior touches a computed
        # level needs before the tight-stop exemption above trusts it.
        # docs/RESEARCH_FINDINGS.md §7.
        min_level_touches_for_stop_honor=int(
            _risk_setting("min_level_touches_for_stop_honor", 5),
        ),
        # Target derivation (2026-09-01) — the numerator of the ratio
        # above, computed from bars instead of guessed by the analyst.
        # Wired from the ratified settings, same pattern as every
        # ceiling above.
        min_target_atr_multiple=_risk_setting("min_target_atr_multiple", 1.0),
        breakout_projection_atr_multiple=_risk_setting(
            "breakout_projection_atr_multiple",
            1.0,
        ),
        max_target_reach_atr_multiple=_risk_setting(
            "max_target_reach_atr_multiple",
            1.5,
        ),
        max_target_horizon_sessions=int(
            _risk_setting("max_target_horizon_sessions", 60),
        ),
    )


def _smart_money_refresh_sources_word(congress_enabled: bool) -> str:
    """What the pre-market smart-money refresh log line should say it read.

    Congressional trading disclosures (`src/data/congressional_trading.py`)
    are only ever fetched when `config.smart_money.congress_enabled` is
    True — switched on 2026-09-20 per owner ruling (see that date's entry
    in `docs/INCIDENT_HISTORY.md`). The log line must say so honestly rather
    than always naming both sources.
    """
    if congress_enabled:
        return "SEC Form 4 + congressional"
    return "SEC Form 4 only (congressional cross-check switched off)"
