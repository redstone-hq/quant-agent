"""Render a standing instruction sheet's risk limits from the live config.

THE DEFECT THIS CLOSES. The AI Risk Manager's standing sheet
(`config/prompts/risk_manager.md`) stated the desk's limits as hand-typed
prose. Nothing tied that prose to `config/settings.yaml`, which is what the
deterministic engine actually enforces, so the two drifted — provably, twice:

  * the sheet told the reviewer the long single-name ceiling was 33% while
    `risk.max_position_pct` had been 65 since 2026-09-11 (owner override).
    Every plan for two days was audited against a ceiling less than half the
    real one.
  * the same sheet told the seat a 1.5 reward:risk floor was "the number to
    enforce" months after the floor stopped being a gate — the named cause of
    three of the four whole-plan vetoes in the archived database (fixed
    separately, PR #341).

The pattern is not "someone forgot to update the prompt". It is that a limit
had TWO homes and only one of them was mechanically enforced. This module
gives it one: the sheet carries `{{risk.<field>}}` placeholders, and the
value is read at run time off the same `RiskConfig` the engine is built from.

FAIL LOUD, NEVER BLANK. A placeholder naming a setting that does not exist
raises `PromptPlaceholderError` rather than rendering an empty string or a
stale default. A risk reviewer briefed with "the single-name ceiling is %"
is worse than one briefed with a wrong number, because nothing downstream
can see that it happened. The pipeline builds the agent at startup, so the
failure surfaces before any capital is at risk.

Scope is deliberately narrow: numeric fields on `src.config.RiskConfig` (and
a short allowlist of its COMPUTED properties, which are derivations of those
fields rather than independent numbers). No other namespace resolves. This
module holds no copy of any limit's value — only the rules for reading one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: `{{risk.max_position_pct}}` — one namespace, one dotted field, no
#: expressions. Whitespace inside the braces is tolerated so a wrapped line
#: in the sheet does not silently stop substituting.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s*\}\}")

#: The only namespace that resolves today. A second seat's sheet would add
#: its own here rather than reaching into arbitrary attributes.
#:
#: WARNING before you route another sheet through `LiveLimitPrompt`.
#: `config/prompts/tech_analyst.md` already carries `{{tech.bars_per_symbol}}`,
#: which `PLACEHOLDER_RE` matches but this module cannot resolve. That sheet is
#: rendered by `src/agents/tech_analyst.py` instead (a code constant, not a
#: settings key, and that seat is the only one allowed to halt the desk — the
#: raise below would be a halt). Sending it through here without first adding a
#: `tech` namespace raises `PromptPlaceholderError` at agent construction. Item
#: 107(c) widened the mechanism's COVERAGE (`audit_prompt_coverage` now checks
#: all ten sheets) without widening what it substitutes: `tech` is declared in
#: `PROMPT_NAMESPACES` as another renderer's business, and this is still the
#: trap if anyone routes that sheet through `LiveLimitPrompt`.
RISK_NAMESPACE = "risk"

#: A second namespace, added for board item 107. It resolves NAMED CONSTANTS
#: in `src.risk.metrics` rather than settings fields, because the numbers it
#: carries have no settings key — the drift flag's weight and P&L
#: thresholds. Naming them here is what lets the prose that describes the
#: flag render from the same pair the flag is computed from, closing the
#: "three homes" half of item 107.
#:
#: This namespace is NOT a place to park a number so that it looks
#: governed. Every entry must be a module-level constant that real code
#: reads at run time; a value that only a prompt ever uses belongs in
#: settings or nowhere (item 107(b)).
FLAGS_NAMESPACE = "flags"


def _flags_values() -> dict[str, float]:
    """Imported lazily: `src.risk.metrics` must not import this module back."""
    from src.risk.trailing import NOISE_BAND_ATR_MULTIPLE

    # The drift flag's two thresholds were deleted 2026-10-09 with the rule
    # they served. Register a constant here only if real code reads it at
    # run time. The noise band is read by the midday TRAIL_STOP clamp and the
    # deterministic trail; the position reviewer's sheet renders it so the
    # model is told the band the pipeline actually enforces.
    return {"noise_band_atr_multiple": NOISE_BAND_ATR_MULTIPLE}


#: Every prompt sheet on disk, and what is responsible for its placeholders.
#: Item 107(c): the rendering mechanism reached 2 of 10 sheets and nothing
#: said what covered the other eight, so a sheet could be added carrying a
#: placeholder nobody substitutes and the desk would ship `{{...}}` to a
#: paid model. `audit_prompt_coverage` refuses an unlisted sheet, so the ten
#: are covered by name and an eleventh cannot appear unregistered.
#:
#: The value is the set of namespaces that sheet is allowed to use. An
#: EMPTY set means "this sheet carries no placeholders at all" and is
#: enforced as such — it is coverage, not an exemption.
#:
#: `tech` is declared for the tech sheet alone and is deliberately NOT
#: resolvable here: `src/agents/tech_analyst.py` renders those three from a
#: code constant and from the run's own lookback, and routing that sheet
#: through `render_prompt_limits` would raise at construction on the one
#: seat allowed to halt the desk. The audit knows the difference between
#: "resolved here" and "resolved by a named other renderer".
PROMPT_NAMESPACES: dict[str, frozenset[str]] = {
    "earnings_analyst.md": frozenset(),
    "evening_analyst.md": frozenset(),
    "macro_analyst.md": frozenset(),
    "meta_reflector.md": frozenset(),
    "news_analyst.md": frozenset(),
    "portfolio_manager.md": frozenset({"risk", "flags"}),
    "position_reviewer.md": frozenset({"flags"}),
    "risk_manager.md": frozenset({"risk"}),
    "smart_money_analyst.md": frozenset(),
    "tech_analyst.md": frozenset({"tech"}),
}

#: Namespaces this module can substitute itself. `tech` is absent on purpose
#: — see `PROMPT_NAMESPACES`.
RESOLVED_HERE = frozenset({RISK_NAMESPACE, FLAGS_NAMESPACE})

#: Computed properties of `RiskConfig` that are legitimate placeholder
#: targets. Each is a DERIVATION of the model's own fields (see
#: `src/config.py`), not an independently chosen number, so rendering one is
#: still rendering the single source of truth. Anything not a declared field
#: and not on this list is refused, so a typo cannot silently reach a method.
#: Empty since the sector ceiling property was deleted 2026-10-09.
RISK_COMPUTED_PROPERTIES: frozenset[str] = frozenset()


class PromptPlaceholderError(RuntimeError):
    """A prompt placeholder names something the live config does not have.

    Deliberately a hard error. The alternative — rendering blank, or a
    default typed into this file — reinstates exactly the second home for a
    number that this module exists to remove.
    """


def _format_number(value: float) -> str:
    """Render a limit the way an operator writes it: `65`, not `65.0`.

    `float` is what pydantic gives back for every one of these fields even
    when settings.yaml wrote an integer, and `max_position_pct=65.0` in a
    prompt reads as spurious precision next to `max_gross_exposure_x=2.0`
    where the decimal is real.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a limit
        raise PromptPlaceholderError(
            "placeholder resolved to a boolean, which is not a numeric limit",
        )
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    # Trim float noise (1.3416407865 -> 1.34 is already done in config; this
    # only guards a future derived property that isn't pre-rounded).
    return f"{as_float:g}"


def resolve_placeholder(key: str, risk_config: Any) -> str:
    """Resolve one dotted placeholder key against the live config.

    Raises `PromptPlaceholderError` for an unknown namespace, an unknown
    field, or a value that is not a number — never returns a fallback.
    """
    namespace, _, field = key.partition(".")
    if namespace == FLAGS_NAMESPACE:
        values = _flags_values()
        if field not in values:
            raise PromptPlaceholderError(
                f"placeholder {{{{{key}}}}} names no constant: {field!r} is "
                f"not one of the named flag thresholds "
                f"({', '.join(sorted(values))}). Add the constant to "
                f"src/risk/metrics.py and register it — do not type the "
                f"number into the prompt.",
            )
        return _format_number(values[field])
    if namespace != RISK_NAMESPACE:
        raise PromptPlaceholderError(
            f"unknown placeholder namespace {namespace!r} in {{{{{key}}}}} — only {sorted(RESOLVED_HERE)} resolve here",
        )
    declared = set(getattr(type(risk_config), "model_fields", {}) or {})
    if field not in declared and field not in RISK_COMPUTED_PROPERTIES:
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} names no setting: {field!r} is not a "
            f"field of {type(risk_config).__name__} and is not one of the "
            f"allowed computed properties "
            f"({', '.join(sorted(RISK_COMPUTED_PROPERTIES))}). "
            f"Add the setting to config/settings.yaml — do not type the "
            f"number into the prompt.",
        )
    try:
        value = getattr(risk_config, field)
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} could not be read from the live config: {exc}",
        ) from exc
    if value is None:
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} resolved to None. An unset optional "
            f"setting must be rendered through a derived property, not left "
            f"blank in the prompt.",
        )
    if not isinstance(value, (int, float)):
        raise PromptPlaceholderError(
            f"placeholder {{{{{key}}}}} resolved to {type(value).__name__}, "
            f"not a number — only numeric limits may be rendered",
        )
    return _format_number(value)


def render_prompt_limits(text: str, risk_config: Any) -> str:
    """Substitute every `{{risk.*}}` placeholder in `text` with its live value.

    Every placeholder must resolve; the first that does not raises. There is
    no partial render, because a sheet that is half live and half stale is
    the condition this replaces.
    """

    def _sub(match: re.Match[str]) -> str:
        return resolve_placeholder(match.group(1), risk_config)

    return PLACEHOLDER_RE.sub(_sub, text)


def placeholders_in(text: str) -> set[str]:
    """Every dotted key the text asks to have substituted."""
    return {m.group(1) for m in PLACEHOLDER_RE.finditer(text)}


def load_risk_config_from_settings(path: str | Path):
    """Parse the `risk:` block of a settings file into `src.config.RiskConfig`.

    Used only as the fallback for a `RiskManagerAgent` constructed without an
    explicit config (every test fixture and script that predates this change).
    It reads the SAME file the pipeline reads, so it is not a second copy of
    the settings — it is a second read of the one copy.
    """
    import yaml

    from src.config import RiskConfig, _walk_and_substitute

    with open(path) as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict) or "risk" not in raw:
        raise PromptPlaceholderError(
            f"{path} has no `risk:` block — the risk manager's standing sheet "
            f"cannot be rendered without the settings the engine enforces",
        )
    return RiskConfig(**_walk_and_substitute(raw["risk"]))


class LiveLimitPrompt:
    """Mixin: this seat's standing sheet renders its limits from live config.

    Two agents now brief their model with `{{risk.*}}` placeholders (the Risk
    Manager, which AUDITS against the limits, and the Portfolio Manager, which
    SIZES under them). The wiring is identical, so it lives here rather than
    being copied — a copy of the loading rule is the same species of defect as
    a copy of a limit's value.

    A subclass supplies `_prompt_path` and `_fallback_prompt`; everything else
    is provided.
    """

    #: Overridden by the subclass.
    _prompt_path: Path
    _fallback_prompt: str = "Respond with JSON."
    #: Settings file used when the caller injects no config. The SAME file the
    #: pipeline reads — a second read of one copy, not a second copy.
    _settings_path: Path

    def __init__(self, *args, risk_config=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._risk_config = risk_config
        # COMMISSIONING RENDER. `system_prompt` is a lazy property read inside
        # `BaseAgent.run`, i.e. mid-session, after the day's analysis budget
        # has already been spent. Rendering once here moves a bad-placeholder
        # failure to construction time, which is what lets this be described
        # as a startup check. Cost is one file read.
        self.assert_prompt_renders()

    @property
    def risk_config(self):
        """The live risk settings this seat's briefing renders from.

        `getattr`, not attribute access: several tests build an agent with
        `__new__` to exercise message building without an LLM client, so
        `__init__` never runs. Absent the attribute is the same case as
        absent the config — load it.
        """
        if getattr(self, "_risk_config", None) is None:
            self._risk_config = load_risk_config_from_settings(self._settings_path)
        return self._risk_config

    @property
    def system_prompt(self) -> str:
        if self._prompt_path.exists():
            return render_prompt_limits(
                self._prompt_path.read_text(),
                self.risk_config,
            )
        return self._fallback_prompt

    def assert_prompt_renders(self) -> None:
        """Render the standing sheet once and discard it, to surface a bad
        placeholder now rather than in the middle of a trading session.

        Separate from `__init__` so a commissioning script or a test can call
        it against a candidate config without building an agent.
        """
        _ = self.system_prompt


# ---------------------------------------------------------------------------
# Coverage: all ten sheets, not the two that happened to be wired up.
# ---------------------------------------------------------------------------

PROMPT_DIR = Path(__file__).resolve().parents[2] / "config" / "prompts"


def audit_prompt_coverage(
    prompt_dir: Path | str | None = None,
    risk_config: Any = None,
) -> list[str]:
    """Every way the ten standing sheets can carry an unrendered number.

    Empty list is a pass. Returns strings rather than raising because this
    is a BUILD-TIME check run by a test over the whole directory, and a
    caller wants all the problems at once, not the first.

    Four failures are reported, and each one has happened or nearly has:

    1. A sheet on disk that is not in `PROMPT_NAMESPACES`. Nothing else in
       the repo enumerates the sheets, so an eleventh could be added and
       inherit no coverage at all. This is the check that makes "all ten"
       true tomorrow as well as today.
    2. A registered sheet that no longer exists — a rename that left the
       registry pointing at nothing, which would otherwise read as a pass.
    3. A placeholder whose namespace that sheet is not registered for. This
       is the `{{tech.*}}` trap: `PLACEHOLDER_RE` matches it, the risk
       renderer cannot resolve it, and the failure lands at agent
       construction on a live morning instead of in CI.
    4. A placeholder in a namespace THIS module resolves that does not
       actually resolve — a typo, a renamed setting, a deleted constant.
       Checked against the live config when one is supplied, which is how a
       settings rename gets caught before it halts the desk.
    """
    directory = Path(prompt_dir) if prompt_dir is not None else PROMPT_DIR
    problems: list[str] = []
    on_disk = {p.name for p in sorted(directory.glob("*.md"))}

    for name in sorted(on_disk - set(PROMPT_NAMESPACES)):
        problems.append(
            f"{name}: prompt sheet is not registered in PROMPT_NAMESPACES, so "
            f"nothing states which renderer is responsible for its numbers. "
            f"Register it (an empty set means 'carries no placeholders' and "
            f"is enforced).",
        )
    for name in sorted(set(PROMPT_NAMESPACES) - on_disk):
        problems.append(
            f"{name}: registered in PROMPT_NAMESPACES but no such file in "
            f"{directory} — a rename left the coverage registry pointing at "
            f"nothing, which would otherwise read as a pass.",
        )

    for name in sorted(on_disk & set(PROMPT_NAMESPACES)):
        allowed = PROMPT_NAMESPACES[name]
        text = (directory / name).read_text()
        for key in sorted(placeholders_in(text)):
            namespace = key.partition(".")[0]
            if namespace not in allowed:
                problems.append(
                    f"{name}: placeholder {{{{{key}}}}} uses namespace "
                    f"{namespace!r}, which this sheet is not registered for "
                    f"(registered: {sorted(allowed) or 'none'}). Either the "
                    f"sheet's renderer must be taught it, or the number does "
                    f"not belong in the sheet.",
                )
                continue
            if namespace not in RESOLVED_HERE:
                continue  # another named renderer's business
            if namespace == RISK_NAMESPACE and risk_config is None:
                continue  # nothing to resolve against; (3) still checked
            try:
                resolve_placeholder(key, risk_config)
            except PromptPlaceholderError as exc:
                problems.append(f"{name}: {exc}")
    return problems
