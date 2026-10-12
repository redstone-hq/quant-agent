"""The Risk Manager is SHOWN its limits, never TOLD them.

Two jobs in this file.

1. THE BUILD CHECK (`test_no_hand_typed_limit_beside_a_risk_setting`,
   `test_every_wired_setting_still_has_a_placeholder`,
   `test_every_placeholder_in_the_sheet_resolves`). These fail CI when a
   numeric limit is hand-typed into `config/prompts/risk_manager.md` instead
   of being rendered from `config/settings.yaml`.

2. THE BEHAVIOUR (the rest). Each wired limit renders its live value; moving
   the setting moves what the reviewer reads; a placeholder naming no setting
   fails loudly rather than rendering blank or a stale default.

WHY THIS EXISTS — AND WHAT ACTUALLY HAPPENED, WHICH IS NOT DRIFT. Commit
e1c639a2 (2026-09-11, PR #297, "single-name cap 100 -> 33") set
`risk.max_position_pct: 65` and, in the SAME diff about a hundred lines away,
wrote "tighter than the 33% long single-name ceiling" into this sheet. The
prompt was wrong at birth, not stale over time — the commit's title says 33,
it landed at 65, and only one of the two hunks got the correction. The same
commit ALSO wrote `max_position_pct=65` correctly into the sheet's hard-rule
inventory, so the sheet contradicted itself from the first minute.

Worse, and the reason this file's check has the shape it does: the line that
commit REPLACED read "`max_single_short_pct` (10%, half the long single-name
ceiling". That is a RELATION. It carries no second copy of any number and
could not drift. The commit swapped a drift-immune phrasing for a hand-typed
literal.

TWO CHECKS, BECAUSE ONE OF THEM WOULD HAVE MISSED THE ORIGINATING BUG.

  (a) `_hand_typed_limits` — adjacency. A digit stated as the value of a
      `RiskConfig` field name (within ADJACENCY_CHARS of it) must be a
      rendered placeholder. Catches `max_position_pct=65` and
      "`max_single_short_pct` (10%".

  (b) `_unrendered_limit_phrases` — the limit-noun pattern. A numeral read as
      the value of a "ceiling / cap / budget / limit / floor / maximum"
      phrase must be a rendered placeholder. This is the one that catches the
      2026-09-11 shape, where the literal ("33% long single-name ceiling")
      sits well past the adjacency window of any setting name — and check (a)
      demonstrably does NOT catch it. See
      `test_adjacency_alone_would_have_missed_the_originating_bug`, which
      pins that gap so nobody re-describes (a) as sufficient.

WHY BOTH ARE DELIBERATELY NARROW. A general "no numbers in the prompt" rule
would cry wolf constantly — the sheet is full of legitimate figures: worked
arithmetic ("a 15% position stopped 3% below entry risks 0.45% of equity"),
break-even hit rates, prices in a `reason` example, dates, spec section
numbers. Both checks therefore key on a limit being STATED AS SUCH, which an
illustration does not do.

WHAT NEITHER CHECK CATCHES, stated plainly rather than papered over:

  * a limit restated with no setting name nearby AND no limit noun — "no
    single holding may exceed 33% of the book" passes both. Only the
    placeholder-coverage check (c) constrains this, and only by ensuring the
    correct rendered statement still exists somewhere.
  * a number that is a limit elsewhere in the codebase but is not a
    `RiskConfig` field. The 50% correlation-cluster advisory is a function
    default in `src/risk/rules.py`, not a setting, and is invisible here.
  * a placeholder rendering the WRONG setting for the sentence it sits in.
    Nothing mechanical can know that a sentence about shorts should cite the
    short cap.

So: these checks make the 2026-09-11 shape fail the build. They do not make
this class of defect impossible, and this file must not be cited as if they
did.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.agents.prompt_limits import (
    PLACEHOLDER_RE,
    PromptPlaceholderError,
    load_risk_config_from_settings,
    placeholders_in,
    render_prompt_limits,
    resolve_placeholder,
)
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.agents.portfolio_manager import PROMPT_PATH as PM_PROMPT_PATH
from src.agents.risk_manager import PROMPT_PATH, SETTINGS_PATH, RiskManagerAgent
from src.config import RiskConfig
from src.pipeline import build_constructor_config, build_risk_config

#: Settings whose value the sheet states and must therefore render. This IS a
#: hand-maintained list — the module docstring's "no hand-maintained table"
#: claim applies to `src/agents/prompt_limits.py`, which holds no limit VALUES
#: and resolves fields reflectively; it does not apply here. What this list
#: holds is an intent ("the sheet is supposed to state these"), which cannot
#: be derived from either the config or the sheet, because deriving it from
#: the sheet is exactly what a regression would defeat.
WIRED_SETTINGS = (
    "max_position_pct",
    "max_total_position_pct",
    # `effective_max_daily_loss_pct` was here until 2026-09-20. The sheet no
    # longer states that limit because the limit no longer exists: the
    # account-level daily-loss halt was removed entirely on the owner's
    # instruction (retired item 32, docs/INCIDENT_HISTORY.md).
    "max_position_risk_pct",
    "min_position_risk_pct",
)

#: Settings the Portfolio Manager's sheet states. PM SIZES under these where
#: the reviewer AUDITS against them, so the two lists differ: PM needs the
#: cluster share and the gross-exposure multiple it sizes into, RM does not.
PM_WIRED_SETTINGS = (
    "max_position_pct",
    "max_position_risk_pct",
    "min_position_risk_pct",
    "max_portfolio_risk_pct",
    "max_cluster_risk_share_pct",
    "max_gross_exposure_x",
)


#: Nouns that mark a numeral as being stated AS a limit rather than used in an
#: illustration. This is the pattern that catches the 2026-09-11 shape.
_LIMIT_NOUN = r"(?:ceiling|caps?|budget|limit|maximum|floor|allowance)\b"

#: A numeral read as the value of a limit noun close after it.
_LIMIT_PHRASE = re.compile(
    r"(?<![\w.$])(\d+(?:\.\d+)?)\s*%?[^.\n]{0,32}?" + _LIMIT_NOUN,
    re.I,
)

#: STRUCTURAL HISTORY. A value quoted as history (a removed gate, a past
#: setting value in a provenance note) sits inside a `<history>...</history>`
#: block in the sheet. The checks scan only the text OUTSIDE such blocks, so
#: the distinction is where a figure sits, never how it is phrased, and there
#: is no stored list of excused phrases to go stale. The tags stay in the
#: rendered prompt: the reviewer reads them as "this is background".
_HISTORY_BLOCK = re.compile(r"<history>.*?</history>", re.S)


def _live_region(text: str) -> str:
    """The sheet with every history block removed; what the checks scan."""
    assert text.count("<history>") == text.count("</history>"), "unbalanced <history> block"
    return _HISTORY_BLOCK.sub("", text)


#: How far past a setting's name a digit still counts as "restating its
#: value". Short on purpose: `name=65` and `` `name` (10%, `` are the two
#: shapes this defect has actually taken, and both put the digit within a few
#: characters. A longer window starts swallowing the sentence's ordinary
#: prose and the check turns noisy, which is worse than a narrower one.
ADJACENCY_CHARS = 24

#: Stripped before the digit scan: a spec section reference (§12.3), a dollar
#: figure from a worked example ($6K), and an issue/item pointer (item 32).
#: None of these is a limit, and all three legitimately sit near a setting
#: name in this sheet's provenance notes.
_NOT_A_LIMIT = re.compile(r"§[\d.]+|\$[\d.,]+[KkMmBb]?|item\s+\d+|#\d+|\d{4}-\d{2}-\d{2}")


def _sheet() -> str:
    return PROMPT_PATH.read_text()


def _live_risk_config() -> RiskConfig:
    return load_risk_config_from_settings(SETTINGS_PATH)


def _risk_setting_names() -> list[str]:
    """Every declared `RiskConfig` field, longest first so that scanning for
    `max_position_pct` does not also match inside `max_position_risk_pct`."""
    return sorted(RiskConfig.model_fields, key=len, reverse=True)


# --------------------------------------------------------------------------
# 1. The build check
# --------------------------------------------------------------------------

#: Stands in for a placeholder while scanning. It occupies the position the
#: rendered value will take, which is what makes the check precise: the
#: question is not "is there a digit near this setting's name" but "does the
#: FIRST thing stated after this setting's name come from the config".
_RENDERED = "\x00"


def _hand_typed_limits(text: str, sheet: str = "risk_manager.md") -> list[str]:
    """Every place a setting's name is followed by a hand-typed value.

    Placeholders collapse to a marker rather than to nothing, so
    `max_position_pct={{risk.max_position_pct}}` scans as
    `max_position_pct=<rendered>` and passes, while `max_position_pct=65`
    does not. A digit inside the window is a finding only when it comes
    BEFORE any marker — otherwise the value IS rendered and the digits that
    follow are ordinary prose ("...=65% — a 15% position stopped 3% below
    entry...").
    """
    marked = PLACEHOLDER_RE.sub(_RENDERED, _live_region(text))
    findings: list[str] = []
    claimed: list[tuple[int, int]] = []
    for name in _risk_setting_names():
        for match in re.finditer(re.escape(name), marked):
            # A longer setting name already covering this span wins, so
            # `max_position_risk_pct` is not also reported as
            # `max_position_pct`.
            if any(s <= match.start() < e for s, e in claimed):
                continue
            claimed.append((match.start(), match.end()))
            window = _NOT_A_LIMIT.sub("", marked[match.end() : match.end() + ADJACENCY_CHARS])
            digit = re.search(r"\d", window)
            if digit is None:
                continue
            marker = window.find(_RENDERED)
            if marker == -1 or marker > digit.start():
                findings.append(f"{name} -> {window.replace(_RENDERED, '<rendered>')!r}")
    return findings


def _unrendered_limit_phrases(text: str, sheet: str = "risk_manager.md") -> list[str]:
    """Every numeral stated as the value of a limit noun without being
    rendered.

    Check (b). This is the one that catches the shape the 2026-09-11 commit
    actually introduced — "tighter than the 33% long single-name ceiling" —
    which sits outside the adjacency window of any setting name and which
    check (a) misses entirely.
    """
    # Spec section references (§9.4, §12.3) are numbered pointers, not
    # values; blanked so a §9.4 mention does not read as a limit
    # stated at 9.4.
    marked = re.sub(r"§\s*[\d.]+", "", PLACEHOLDER_RE.sub(_RENDERED, _live_region(text)))
    findings = []
    for match in _LIMIT_PHRASE.finditer(marked):
        phrase = match.group(0).replace(_RENDERED, "<rendered>")
        findings.append(phrase)
    return findings


def test_no_unrendered_limit_phrase():
    findings = _unrendered_limit_phrases(_sheet())
    assert not findings, (
        "A numeral is stated as the value of a limit in "
        "config/prompts/risk_manager.md without being rendered from the "
        "config. Either render it from its setting, or — better where the "
        "sentence is about how two limits RELATE — state the relation and "
        "name the settings, which carries no copy of any number at all:\n  " + "\n  ".join(findings)
    )


def test_the_check_catches_the_shape_that_actually_happened():
    """The 2026-09-11 regression, reproduced exactly: the placeholder is left
    in place and a SECOND, hand-typed ceiling is added beside it. This is what
    commit e1c639a2 did, and it is not the same as deleting a placeholder."""
    regressed = _sheet().replace(
        "single-name cap and gross/net exposure ceilings as a BUY",
        "single-name cap and gross/net exposure ceilings as a BUY, tighter than the 33% long single-name ceiling",
    )
    assert regressed != _sheet(), "fixture text no longer present in the sheet"
    assert any("33" in f for f in _unrendered_limit_phrases(regressed))


def test_adjacency_alone_would_have_missed_the_originating_bug():
    """Pins the known gap in check (a) so it is never described as
    sufficient. If this test starts failing because adjacency got stronger,
    that is good news — delete the test and say so in the docstring."""
    regressed = _sheet().replace(
        "single-name cap and gross/net exposure ceilings as a BUY",
        "single-name cap and gross/net exposure ceilings as a BUY, tighter than the 33% long single-name ceiling",
    )
    assert _hand_typed_limits(regressed) == [], (
        "check (a) now catches this shape — update the module docstring, which currently states that it does not"
    )


def test_no_hand_typed_limit_beside_a_risk_setting():
    findings = _hand_typed_limits(_sheet())
    assert not findings, (
        "A numeric limit is hand-typed into config/prompts/risk_manager.md "
        "next to the setting it restates. Replace it with a "
        "{{risk.<setting>}} placeholder so the reviewer reads the value the "
        "engine enforces:\n  " + "\n  ".join(findings)
    )


def test_build_check_catches_a_hand_typed_limit():
    """The regression the check exists for: a placeholder swapped back to a
    literal. Uses the exact shape of the 2026-09-13 drift."""
    regressed = _sheet().replace(
        "`max_position_pct` ({{risk.max_position_pct}}%",
        "`max_position_pct` (10%",
    )
    assert regressed != _sheet(), "fixture text no longer present in the sheet"
    findings = _hand_typed_limits(regressed)
    assert any("max_position_pct" in f for f in findings)


def test_build_check_does_not_fire_on_example_arithmetic():
    """Worked arithmetic in an illustration is not a limit and must pass.

    This is the exact sentence from the sheet's `Sizing Sanity` step, plus
    the inverse-ETF leverage illustration and a hit-rate table — the three
    kinds of legitimate number that a blunter check would flag.
    """
    illustrations = (
        "A 15% position stopped 3% below entry risks 0.45% of equity; a 5% "
        "position stopped 20% below entry risks 1.0%.\n"
        "Worked illustration: 3x `SQQQ` at $6K notional is $18K of gross "
        "bearish exposure.\n"
        "R/R X breaks even at a hit rate of `1/(1+X)` (1.5 -> 40%, 2.0 -> "
        "33%, 3.0 -> 25%).\n"
        "stop $61.54 sits under no level the chart defends.\n"
    )
    assert _hand_typed_limits(illustrations) == []
    # And the same text still passes when a setting name is present but its
    # value is properly rendered.
    assert (
        _hand_typed_limits(
            "`max_position_pct={{risk.max_position_pct}}` — " + illustrations,
        )
        == []
    )


def test_every_wired_setting_still_has_a_placeholder():
    """Deleting a placeholder to type the number back in fails here even if
    the literal lands too far from the setting's name for the adjacency
    check to see it."""
    keys = placeholders_in(_sheet())
    missing = [s for s in WIRED_SETTINGS if f"risk.{s}" not in keys]
    assert not missing, (
        "config/prompts/risk_manager.md no longer renders these settings: "
        + ", ".join(missing)
        + ". Restore the {{risk.<setting>}} placeholder. Do NOT resolve this "
        "by typing the number into the sheet, and do NOT resolve it by "
        "deleting the entry here — that is defeating the check, not passing "
        "it. Removing an entry from WIRED_SETTINGS is correct ONLY when the "
        "sheet genuinely no longer needs to state that limit at all (for "
        "instance because the sentence was rewritten as a relation between "
        "two named settings), and the removal should say which."
    )


def test_every_placeholder_in_the_sheet_resolves():
    """No placeholder may name a setting that does not exist — the sheet must
    render cleanly against the live settings file."""
    rendered = render_prompt_limits(_sheet(), _live_risk_config())
    assert "{{" not in rendered and "}}" not in rendered


# --------------------------------------------------------------------------
# 2. Behaviour — the reviewer reads the live value
# --------------------------------------------------------------------------


def test_each_wired_limit_renders_its_live_value():
    cfg = _live_risk_config()
    rendered = render_prompt_limits(_sheet(), cfg)
    raw = yaml.safe_load(SETTINGS_PATH.read_text())["risk"]
    for setting in WIRED_SETTINGS:
        value = getattr(cfg, setting)
        assert f"{value:g}" in rendered, f"{setting} (live value {value}) does not appear in the rendered sheet"
    # The specific defect this PR closes. The stale "33% long single-name
    # ceiling" is gone; the hard-rule inventory renders the live ceiling, and
    # the SHORT-discipline sentence that carried the bad literal is now a
    # RELATION naming both settings and quoting neither long number.
    # NB: "33%" still legitimately appears in the break-even hit-rate table
    # (R/R 2.0 -> 33%), which is arithmetic, not a limit. Assert on the
    # PHRASE that carried the defect, not on the digits.
    assert "33% long single-name ceiling" not in rendered
    assert f"`max_position_pct={raw['max_position_pct']:g}`" in rendered
    # Owner decision 2026-09-17: the short cap IS the long cap.
    assert (f"same `max_position_pct` ({raw['max_position_pct']:g}%) single-name cap") in rendered


def test_changing_the_setting_changes_what_the_reviewer_is_shown():
    cfg = _live_risk_config()
    before = render_prompt_limits(_sheet(), cfg)
    moved = cfg.model_copy(update={"max_position_pct": 41.0})
    after = render_prompt_limits(_sheet(), moved)
    assert "`max_position_pct=41`" in after
    assert "`max_position_pct=41`" not in before
    assert f"`max_position_pct={cfg.max_position_pct:g}`" not in after


def test_short_cap_tracks_the_long_single_name_setting():
    """Owner decision 2026-09-17: a short's single-name cap IS
    `max_position_pct` — the sheet must render that one setting for it."""
    cfg = _live_risk_config().model_copy(update={"max_position_pct": 41.0})
    rendered = render_prompt_limits(_sheet(), cfg)
    assert "same `max_position_pct` (41%) single-name cap" in rendered
    assert "max_single_short_pct" not in rendered
    assert "max_gross_bearish_pct" not in rendered


def test_per_trade_risk_budget_is_the_ratified_unit_not_the_old_half_percent():
    """The sheet used to tell the reviewer the constructor capped a stop-out
    at 0.5% of equity. The ratified per-trade unit is
    `risk.max_position_risk_pct`; 0.5 is `min_position_risk_pct`, a different
    setting. Both now render from their own key."""
    cfg = _live_risk_config().model_copy(
        update={
            "max_position_risk_pct": 4.0,
            "min_position_risk_pct": 0.25,
        }
    )
    rendered = render_prompt_limits(_sheet(), cfg)
    assert "`max_position_risk_pct`,\n   4% of equity" in rendered
    assert "`min_position_risk_pct`, 0.25% risk" in rendered


def test_placeholder_with_no_matching_setting_fails_loudly():
    cfg = _live_risk_config()
    with pytest.raises(PromptPlaceholderError) as exc:
        render_prompt_limits("ceiling is {{risk.max_positon_pct}}%", cfg)
    assert "max_positon_pct" in str(exc.value)


def test_unknown_namespace_fails_loudly():
    with pytest.raises(PromptPlaceholderError):
        render_prompt_limits("{{execution.slippage_pct}}", _live_risk_config())


def test_placeholder_never_renders_blank_or_a_default():
    """The failure mode this must not have: substituting nothing, or a number
    typed into the renderer."""
    cfg = _live_risk_config()
    for bad in ("{{risk.nonexistent}}", "{{risk.model_dump}}", "{{risk.require_stop_loss}}"):
        with pytest.raises(PromptPlaceholderError):
            render_prompt_limits(bad, cfg)


def test_a_null_optional_setting_is_refused_rather_than_blanked():
    """No live setting is null today (the last one, `max_daily_loss_pct`,
    went with the retired account-level breaker on 2026-09-20), so this
    builds the unset case rather than reading it off settings.yaml. The
    refusal is the behaviour under test, not which field happens to be
    optional."""
    cfg = _live_risk_config().model_copy(update={"max_position_pct": None})
    assert cfg.max_position_pct is None
    with pytest.raises(PromptPlaceholderError) as exc:
        resolve_placeholder("risk.max_position_pct", cfg)
    assert "None" in str(exc.value)


def test_integer_limits_render_without_a_trailing_point_zero():
    cfg = _live_risk_config()
    assert resolve_placeholder("risk.max_position_pct", cfg) == "65"


# --------------------------------------------------------------------------
# 4. The agent wiring
# --------------------------------------------------------------------------


def _agent(**kwargs) -> RiskManagerAgent:
    return RiskManagerAgent(api_key="test-key", model="test-model", **kwargs)


def test_agent_system_prompt_renders_the_live_settings_file():
    prompt = _agent().system_prompt
    assert "{{" not in prompt
    assert f"`max_position_pct={_live_risk_config().max_position_pct:g}`" in prompt


def test_agent_uses_the_injected_config_over_the_settings_file():
    injected = _live_risk_config().model_copy(update={"max_position_pct": 12.0})
    assert "`max_position_pct=12`" in _agent(risk_config=injected).system_prompt


def test_risk_ceiling_defaults_to_the_live_portfolio_risk_setting():
    """The rendered Portfolio Risk block's ceiling used to fall back to a
    hand-typed 25.0 in this module. It now reads the setting."""
    injected = _live_risk_config().model_copy(update={"max_portfolio_risk_pct": 17.0})
    agent = _agent(risk_config=injected)
    assert agent.risk_config.max_portfolio_risk_pct == 17.0
    # build_user_message reads it through the same property.
    assert float(agent.risk_config.max_portfolio_risk_pct) == 17.0


def test_settings_file_without_a_risk_block_fails_loudly(tmp_path: Path):
    bad = tmp_path / "settings.yaml"
    bad.write_text("llm:\n  fallback_model: x\n")
    with pytest.raises(PromptPlaceholderError):
        load_risk_config_from_settings(bad)


# --------------------------------------------------------------------------
# 5. The rendered value and the ENFORCED value must be the same object
# --------------------------------------------------------------------------
#
# Rendering a limit into the reviewer's briefing from settings.yaml while the
# deterministic engine enforced a different object's DEFAULT would be the same
# two-homes defect this change removes, pointed the other way. `Pipeline`
# builds the engine's `RiskConfig` from a hand-enumerated argument list, so a
# setting left out of it silently falls back to the pydantic class default and
# settings.yaml is ignored for that field — the exact bug already found once
# for `allow_margin` (Codex r11 P2, see the comment at that argument).

# WHY THIS IS A BEHAVIOURAL CHECK AND NOT A SOURCE SCAN. The first version of
# these tests regex-scanned `src/pipeline.py` for `^\s*([a-z_]+)=` inside the
# `RiskConfig(` call, which proved only that a KEYWORD NAME was typed there.
# a limit hard-coded at its current value would have satisfied it — the very
# defect the test claims to exclude. So instead: move the setting, rebuild the
# enforcing object through the pipeline's OWN builder, and see whether the
# object moved with it.


def _config_for(risk_config) -> SimpleNamespace:
    """The minimum `config` shape the two builders read. Only `risk` and
    `cash_sweep` are touched (grep either builder for `config.`)."""
    return SimpleNamespace(
        risk=risk_config,
        cash_sweep=SimpleNamespace(symbol=None, enabled=False),
    )


def _perturb(live: RiskConfig, field: str):
    """A legal, DIFFERENT value for `field`, or None if nothing legal exists.

    Derived from the live value and the field's own validators rather than
    typed, so this file keeps no copy of any limit. Candidates are tried in
    order and the first that survives `RiskConfig`'s validation wins; a field
    hemmed in by a cross-field validator can legitimately yield None.
    """
    current = getattr(live, field, None)
    if isinstance(current, bool):
        candidates = [not current]
    elif isinstance(current, (int, float)):
        candidates = [current * 0.9, current * 1.1, current + 1, current - 1, current / 2, current * 2]
    elif current is None:
        # An UNSET optional numeric setting (`max_sector_hard_pct` is null in
        # settings.yaml today — RiskConfig derives it).
        # Probe with numbers ALREADY PRESENT elsewhere in the live config
        # rather than inventing one, so this file still holds no number of
        # its own. A field that is optional but not numeric simply finds no
        # candidate that validates and yields None.
        candidates = sorted(
            {
                float(value)
                for value in live.model_dump().values()
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            }
        )
    else:
        return None
    base = live.model_dump()
    for candidate in candidates:
        if candidate == current:
            continue
        try:
            RiskConfig(**{**base, field: candidate})
        except Exception:  # noqa: BLE001 — an illegal probe is simply skipped
            continue
        return candidate
    return None


def _built_pair(risk_config):
    """`(engine RiskConfig, sizer ConstructorConfig)` as the pipeline builds
    them. These are the two objects that ENFORCE the desk's numeric limits."""
    config = _config_for(risk_config)
    engine_config = build_risk_config(config)
    return engine_config, build_constructor_config(config, engine_config)


def _numbers_carried(obj) -> set[float]:
    """Every numeric value an enforcing config object carries.

    Compared by VALUE, not by field name, because the sizer renames as it
    threads — `max_position_risk_pct` arrives as `risk_budget_pct` and
    `min_position_risk_pct` as `min_risk_pct`.
    """
    fields = getattr(type(obj), "model_fields", None) or getattr(obj, "__dataclass_fields__", {})
    carried = set()
    for name in fields:
        value = getattr(obj, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        carried.add(float(value))
    return carried


def _engine_threaded_fields() -> set[str]:
    """Every `RiskConfig` field that DEMONSTRABLY reaches the risk engine:
    move the setting, and `build_risk_config` returns the moved value.

    A hard-coded literal in `src/pipeline.py` does not appear here, which is
    the whole difference from scanning the keyword names.
    """
    live = _live_risk_config()
    base = live.model_dump()
    threaded = set()
    for field in RiskConfig.model_fields:
        probe = _perturb(live, field)
        if probe is None:
            continue
        moved = RiskConfig(**{**base, field: probe})
        built = build_risk_config(_config_for(moved))
        if getattr(built, field, None) == probe:
            threaded.add(field)
    return threaded


def test_every_rendered_setting_is_also_threaded_into_the_engine():
    """A setting the sheet SHOWS must be one the engine READS from the same
    file. Without this, the seat could be briefed with settings.yaml's value
    while the engine hard-blocked against a class default."""
    threaded = _engine_threaded_fields()
    # `effective_max_daily_loss_pct` is derived, not a field; it is covered by
    # its three inputs, all of which are threaded.
    rendered_fields = [s for s in WIRED_SETTINGS if s in RiskConfig.model_fields]
    missing = [s for s in rendered_fields if s not in threaded]
    assert not missing, (
        "config/prompts/risk_manager.md renders these settings from "
        "settings.yaml, but moving them does not move what the risk engine's "
        "RiskConfig carries — so the engine enforces something else, and the "
        "reviewer is shown a number the engine is not using: " + ", ".join(missing)
    )


def test_the_parity_check_would_actually_catch_a_hard_coded_limit():
    """Teeth. An UNTHREADED setting must fail the same check the threaded ones
    pass — otherwise `test_every_rendered_setting_is_also_threaded_into_the_
    engine` is asserting nothing. The witness is drawn from the omitted list
    below rather than named here, so it cannot go stale."""
    threaded = _engine_threaded_fields()
    live = _live_risk_config()
    unthreaded = sorted(f for f in RiskConfig.model_fields if f not in threaded and _perturb(live, f) is not None)
    assert unthreaded, (
        "every declared risk setting now reaches the engine — good, but this "
        "test can no longer prove the check has teeth. Delete it and say so."
    )
    witness = unthreaded[0]
    probe = _perturb(live, witness)
    moved = RiskConfig(**{**live.model_dump(), witness: probe})
    built = build_risk_config(_config_for(moved))
    assert getattr(built, witness, None) != probe, witness


def test_the_rest_of_the_omission_is_recorded_not_silently_swept():
    """This PR threaded only the settings it renders. The rest of the
    hand-enumerated list is still incomplete, is pre-existing, and is
    currently latent (every omitted field's class default equals its
    settings.yaml value). Pinned here so the count cannot grow unnoticed and
    so nobody reads this file as a claim that the whole list is wired."""
    threaded = _engine_threaded_fields()
    raw = yaml.safe_load(SETTINGS_PATH.read_text())["risk"]
    omitted = sorted(f for f in RiskConfig.model_fields if f not in threaded and f in raw)
    live_divergence = [
        f
        for f in omitted
        # `get_default(call_default_factory=True)` — a field declared with a
        # default_factory reports `.default` as PydanticUndefined, which
        # would read as a false divergence.
        if raw[f]
        != RiskConfig.model_fields[f].get_default(
            call_default_factory=True,
        )
    ]
    assert not live_divergence, (
        "A setting is present in settings.yaml with a NON-DEFAULT value and "
        "is NOT passed to the engine's RiskConfig — the engine is enforcing "
        "something settings.yaml does not say. This is live-wrong, not "
        "latent: " + ", ".join(live_divergence)
    )
    # 16 since 2026-09-13 (docs/WORK.md item 56): `max_stop_width_reach_atr_
    # multiple` joined the list when the stop-refusal threshold was split off
    # from `max_target_reach_atr_multiple`. Both are CONSTRUCTOR settings,
    # threaded through `ConstructorConfig` in `pipeline.py`, not engine
    # `RiskConfig` fields — the new one is omitted here for exactly the same
    # reason its sibling directly above it in this list always was.
    # 16 -> 15 on 2026-09-14: `agreement_ceiling_pct` was deleted outright
    # with the graduated agreement sizing ladder.
    # 15 -> 13 on 2026-09-20: `drawdown_5d_risk_multiple` and
    # `drawdown_20d_risk_multiple` were deleted from settings.yaml with the
    # rest of the account-level loss alarms (retired item 32).
    # 13 -> 12 on 2026-09-24, board item 81: `min_reward_risk_after_widening`
    # was deleted from settings.yaml AND from `RiskConfig` (zero-reader dead
    # code — it refused nothing and capped nothing), so it can no longer
    # appear in this "present in settings.yaml but not threaded" list at all.
    # 12 -> 11 on 2026-09-26, board item 56 (route (c)): the same thing
    # happened to `max_stop_width_reach_atr_multiple`. The stop-WIDTH refusal
    # it thresholded was deleted after refusing zero of 648 recorded sized
    # stops, and no published work fixes the touch probability below which a
    # stop stops being a stop, so the key is gone from settings.yaml and from
    # `RiskConfig` and a stale settings file carrying it now raises. Its
    # sibling `max_target_reach_atr_multiple` is a different number, is still
    # omitted here for the original reason, and is unchanged.
    assert len(omitted) == 11, (
        f"the engine's hand-enumerated RiskConfig now omits {len(omitted)} "
        f"settings present in settings.yaml, not 11 — if that grew, thread "
        f"the new one; if it shrank, lower this number. Omitted: {omitted}"
    )


# --------------------------------------------------------------------------
# 6. The render happens at construction, not mid-session
# --------------------------------------------------------------------------


def test_a_bad_placeholder_fails_at_construction_not_at_first_llm_call(tmp_path, monkeypatch):
    """`system_prompt` is a lazy property read inside `BaseAgent.run`, i.e. at
    the risk stage, after the whole day's analysis has been paid for. The
    commissioning render in `__init__` moves that failure to startup."""
    broken = tmp_path / "risk_manager.md"
    broken.write_text("ceiling is {{risk.no_such_setting}}%\n")
    # `_prompt_path` is the class attribute the LiveLimitPrompt mixin reads.
    monkeypatch.setattr(RiskManagerAgent, "_prompt_path", broken)
    with pytest.raises(PromptPlaceholderError):
        RiskManagerAgent(api_key="k", model="m")


def test_a_good_sheet_constructs_cleanly():
    agent = RiskManagerAgent(api_key="k", model="m")
    agent.assert_prompt_renders()  # idempotent, callable by a commissioning script


# --------------------------------------------------------------------------
# 7. The Portfolio Manager's sheet — same treatment, same checks
# --------------------------------------------------------------------------
#
# PM's sheet was CORRECT when the reviewer's went wrong, and that is precisely
# why it gets the same mechanism rather than a note saying it is fine: commit
# e1c639a2 edited both sheets and got one right and one wrong, so the human
# process protecting this copy is exactly as reliable as the one that failed.
# PM is also the seat that picks the sizes, so a wrong ceiling here shapes the
# order rather than only the review of it.


def _pm_sheet() -> str:
    return PM_PROMPT_PATH.read_text()


def test_pm_sheet_has_no_hand_typed_limit_beside_a_risk_setting():
    findings = _hand_typed_limits(_pm_sheet(), "portfolio_manager.md")
    assert not findings, (
        "A numeric limit is hand-typed next to the setting it restates in "
        "config/prompts/portfolio_manager.md:\n  " + "\n  ".join(findings)
    )


def test_pm_sheet_has_no_unrendered_limit_phrase():
    findings = _unrendered_limit_phrases(_pm_sheet(), "portfolio_manager.md")
    assert not findings, (
        "A numeral is stated as the value of a limit in "
        "config/prompts/portfolio_manager.md without being rendered:\n  " + "\n  ".join(findings)
    )


def test_pm_sheet_renders_every_setting_it_states():
    keys = placeholders_in(_pm_sheet())
    missing = [s for s in PM_WIRED_SETTINGS if f"risk.{s}" not in keys]
    assert not missing, (
        "config/prompts/portfolio_manager.md no longer renders: "
        + ", ".join(missing)
        + ". Restore the placeholder; do not type the number back in, and do "
        "not delete the entry here to make this pass."
    )


def test_pm_sheet_resolves_and_tracks_the_live_config():
    cfg = _live_risk_config()
    rendered = render_prompt_limits(_pm_sheet(), cfg)
    assert "{{" not in rendered
    for setting in PM_WIRED_SETTINGS:
        assert f"{getattr(cfg, setting):g}" in rendered, setting
    moved = cfg.model_copy(update={"max_position_pct": 44.0})
    after = render_prompt_limits(_pm_sheet(), moved)
    assert "44% single-name" in after
    assert "44% single-name" not in rendered


def test_pm_agent_renders_at_construction():
    agent = PortfolioManagerAgent(api_key="k", model="m")
    assert "{{" not in agent.system_prompt
    assert f"{_live_risk_config().max_position_pct:g}% single-name" in agent.system_prompt


def test_pm_bad_placeholder_fails_at_construction(tmp_path, monkeypatch):
    broken = tmp_path / "portfolio_manager.md"
    broken.write_text("cap is {{risk.no_such_setting}}%\n")
    monkeypatch.setattr(PortfolioManagerAgent, "_prompt_path", broken)
    with pytest.raises(PromptPlaceholderError):
        PortfolioManagerAgent(api_key="k", model="m")


def test_every_setting_pm_renders_reaches_the_object_that_enforces_it():
    """Same parity requirement as the reviewer's sheet, against the RIGHT
    object.

    The sizing seat's limits are NOT all enforced by the risk engine. Grep
    `src/risk/rules.py`: it contains no reference at all to
    `max_cluster_risk_share_pct`,
    `min_position_risk_pct` or `max_portfolio_risk_pct`. Those four are
    enforced by `PortfolioConstructor`, from a separately built
    `ConstructorConfig` with its own fallbacks — so checking PM's sheet only
    against `RiskConfig` would declare parity for four settings whose
    enforcement home was never looked at.

    Compared by value across BOTH objects, because the sizer renames as it
    threads.
    """
    live = _live_risk_config()
    for setting in PM_WIRED_SETTINGS:
        probe = _perturb(live, setting)
        assert probe is not None, (
            f"{setting} admits no legal alternative value, so this test "
            f"cannot tell whether it is threaded. Fix the probe, do not "
            f"delete the entry."
        )
        moved = RiskConfig(**{**live.model_dump(), setting: probe})
        engine_config, constructor_config = _built_pair(moved)
        carried = _numbers_carried(engine_config) | _numbers_carried(constructor_config)
        assert float(probe) in carried, (
            f"config/prompts/portfolio_manager.md renders `{setting}` from "
            f"settings.yaml, but moving it to {probe} moved nothing in either "
            f"object that enforces the desk's limits (the risk engine's "
            f"RiskConfig or the constructor's ConstructorConfig). The sizing "
            f"seat would be shown a number nothing enforces."
        )


def test_the_value_pm_is_shown_is_the_value_the_two_engines_carry():
    """The live case of the test above: with settings.yaml as it stands, every
    number PM's sheet renders is a number one of the enforcing objects holds."""
    live = _live_risk_config()
    engine_config, constructor_config = _built_pair(live)
    carried = _numbers_carried(engine_config) | _numbers_carried(constructor_config)
    missing = [s for s in PM_WIRED_SETTINGS if float(getattr(live, s)) not in carried]
    assert not missing, (
        "PM's sheet renders these from settings.yaml but neither the risk "
        "engine nor the constructor carries the value: " + ", ".join(missing)
    )


def test_a_legal_zero_risk_floor_reaches_both_engines_unchanged():
    """`min_position_risk_pct` is declared `ge=0` — zero is a legal "no
    floor". A `> 0` read would swallow it into a hand-typed default, briefing
    the sizing seat on a floor nothing enforces. That is the same two-homes
    defect pointed inward, so it is pinned."""
    live = _live_risk_config()
    zeroed = RiskConfig(**{**live.model_dump(), "min_position_risk_pct": 0.0})
    engine_config, constructor_config = _built_pair(zeroed)
    assert engine_config.min_position_risk_pct == 0.0
    assert constructor_config.min_risk_pct == 0.0
    assert "0" in render_prompt_limits(
        "{{risk.min_position_risk_pct}}",
        zeroed,
    )


# --------------------------------------------------------------------------
# Item 62 (settled 2026-09-14) — the three prompt-only order-size ceilings
# --------------------------------------------------------------------------

#: Each entry is (label, regex over the PM sheet, why it must not come back).
#: These three were ceilings that shaped order size, lived only as text in
#: `config/prompts/portfolio_manager.md`, had no settings key and no recorded
#: derivation, and were exempted from the checks above so those checks could
#: run at all. All three are gone. This test is the mechanical replacement for
#: that exemption: the exemption recorded a question, this records the answer.
_RETIRED_PROMPT_ONLY_CEILINGS = (
    (
        "earnings-queued risk cap",
        re.compile(r"queued_cap|JUST FILED[^.\n]{0,60}risk cap", re.I),
        "A `JUST FILED` name carries no earnings stance, so it reaches the "
        "sizing formula with one fewer agreeing seat and the DERIVED "
        "agreement schedule prices it. A separate risk number double-counts "
        "the same missing evidence. The only enforcement that exists clamps "
        "position WEIGHT, not risk.",
    ),
    (
        "starter-sleeve ceiling",
        re.compile(r"sleeve ceiling|starter position \(\s*≤", re.I),
        "Tech-alone is one seat of evidence and the derived agreement "
        "schedule already prices one seat; a sleeve figure is a second, "
        "un-derived home for the same idea.",
    ),
    (
        "cash floor",
        re.compile(r"\d+(?:\.\d+)?\s*%\s*(?:cash\s+)?floor", re.I),
        "The regime cash-floor rule was deleted from this sheet on "
        "2026-09-01; the number that survived it lived only inside a worked "
        "example and never matched any rung of the rule it referred to.",
    ),
)


def test_no_prompt_only_order_size_ceilings():
    """None of item 62's three ceilings has come back to the PM sheet.

    A ceiling that shapes order size must render from `config/settings.yaml`
    or not exist. Re-adding one as prompt text — with or without a fresh
    exemption above — puts back exactly the defect item 62 recorded.
    """
    sheet = _sheet()
    found = [
        f"{label}: {pattern.search(sheet).group(0)!r} — {why}"
        for label, pattern, why in _RETIRED_PROMPT_ONLY_CEILINGS
        if pattern.search(sheet)
    ]
    assert not found, (
        "A prompt-only order-size ceiling is back in config/prompts/portfolio_manager.md:\n  " + "\n  ".join(found)
    )


def test_history_block_is_structural_not_phrased():
    """Bite tests for the history region. A live limit in the live region
    FAILS; the same sentence inside <history> is ignored; and there is no
    stored exemption list."""
    assert "_EXEMPTIONS" not in globals()
    base = _sheet()
    live = base + "\nNever hold more than a 40% ceiling in one name.\n"
    assert _unrendered_limit_phrases(live), "live limit must be caught"
    hist = base + "\n<history>It once had a 40% ceiling in one name.</history>\n"
    assert not _unrendered_limit_phrases(hist), "history region must be ignored"
    assert not _hand_typed_limits(base + "\n<history>max_position_pct=65 was once typed here.</history>\n")
    assert _hand_typed_limits(base + "\nmax_position_pct=65\n")
    with pytest.raises(AssertionError):
        _live_region("<history>never closed")


def test_a_just_filed_name_loses_its_earnings_seat():
    """The mechanism the sheet now relies on instead of a hand-typed number.

    A queued placeholder carries no `analysis`, so it produces no registry
    stance — the seat is absent and the signed source score is one lower.

    **Read this with the test below.** Item 62 justified deleting the
    prompt's earnings-queued risk figure partly on the ground that the §9.4
    ceiling already priced that missing seat at a lower rung. The graduated
    ceiling was retired on 2026-09-14 (owner decision — the sqrt law prices
    INDEPENDENT estimates and these seats are not independent), so a lower
    net no longer costs size; it only refuses at or below zero. Since board
    item 186 (2026-10-01) the deterministic Python belt,
    `TradingPipeline._refuse_queued_earnings_buys`, no longer caps such a
    name at 5% of the book either — it REFUSES the BUY, because an unread
    filing is an unconvicted seat. The absent seat this test pins is the
    same fact read upstream.
    """
    from src.agents.portfolio_manager import PortfolioManagerAgent

    analysed = {
        "symbol": "AAA",
        "is_new": False,
        "filing_date": "2026-09-14",
        "analysis": {"investment_implications": {"sentiment": "bullish"}},
    }
    queued = {
        "symbol": "AAA",
        "analysis": None,
        "is_new": True,
        "queued": True,
        "form_type": "10-Q",
        "filing_date": "2026-09-14",
    }
    assert PortfolioManagerAgent._earnings_stance_rows([analysed]), "an analysed filing must produce an earnings stance"
    assert PortfolioManagerAgent._earnings_stance_rows([queued]) == [], (
        "a JUST FILED placeholder must produce NO earnings stance — that "
        "absence IS the size reduction the sheet now relies on"
    )


def test_losing_a_seat_no_longer_costs_size_only_the_refusal_remains():
    """The other half of item 62's justification, corrected 2026-09-14.

    Losing a seat used to drop the name a rung on the graduated agreement
    ceiling. That ladder is retired: the net is now a go/no-go, so every
    positive net sizes identically and only a net at or below zero stops the
    trade. Asserted here, in the file that carries item 62's reasoning, so
    the reasoning cannot keep resting on a mechanism that no longer exists.
    """
    from src.risk.rules import agreement_refuses_trade

    assert not hasattr(_live_risk_config(), "agreement_ceiling_pct")
    assert not agreement_refuses_trade(1)
    assert not agreement_refuses_trade(2)
    assert agreement_refuses_trade(0)
