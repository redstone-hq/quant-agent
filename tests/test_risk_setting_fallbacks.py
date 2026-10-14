"""Every `_risk_setting` fallback must equal the value the desk trades on.

WHY THIS EXISTS
---------------
`TradingPipeline.__init__` wires the constructor's risk inputs with
`_risk_setting("<field>", <literal>)`. The literal is used when the
configured value is absent or is not a real number, and `_risk_setting`'s
own docstring calls it "the ratified default". Nothing enforced that.

On 2026-09-30 (board item 90) exactly one of the fifteen disagreed:
`min_stop_atr_multiple` fell back to 1.5 while the desk trades 2.5.

HOW THE 1.5 GOT THERE — read from git, not inferred
---------------------------------------------------
Commit `0088328c` (PR #269, 2026-09-10) is a SQUASH of three sub-commits.
The first re-derived the stop floor 3.0 -> 1.5 and correctly moved this
fallback 3.0 -> 1.5 with it. The second moved the base 1.5 -> 2.5 in
`config/settings.yaml` and `src/config.py` and did not move the fallback.
So this is a half-landed second sub-commit — precisely the failure
`scripts/definition_of_done.py` was built for ("the half in front of the
author got fixed") — and NOT, as was briefly supposed, a transcription
slip from the neighbouring `min_reward_risk_after_widening", 1.5` line.
The diff shows the deliberate matched 3.0 -> 1.5 edit in sub-commit one.

Note also what `config/settings.yaml` never held: the deployed value went
3.0 -> 2.5 and was never 1.5. `git log --all -S'min_stop_atr_multiple: 1.5'
-- config/settings.yaml` returns nothing. The 1.5 era existed only inside
PR #269's branch.

WHAT THIS CLAIMS, AND WHAT IT DOES NOT
--------------------------------------
The 1.5 was NOT reachable on the production path: a real `RiskConfig`
always carries the attribute and pydantic coerces the deployed YAML.
Measured 2026-09-30: the three test modules that construct a
`TradingPipeline` (`test_pipeline`, `test_agent_log_attribution`,
`test_credential_outage_fail_closed`) give 108 passed with the fallback at
either 1.5 or 2.5, so no test outcome depended on it either. It is a stale
constant, not a live defect, and it is written down that way rather than
oversold.

WHY NOT THE LEDGER GATE
-----------------------
`src/number_sources.py` names "fallback arguments (`_risk_number(x, 25.0)`,
`kwargs.get(k, 25.0)`)" among the shapes it structurally cannot scan, and
suggests hoisting each literal to a named constant — which would work but
would add a ledger row, and therefore a new number to account for, per
fallback. Pinning each fallback to the deployed value closes the same hole
and introduces no number: every assertion here is an equality between two
things already in the tree.

THREE THINGS THIS CHECKS THAT AN EARLIER DRAFT DID NOT
------------------------------------------------------
1. It compares against `config/settings.yaml`, the file the desk actually
   trades, falling back to the `RiskConfig` field default only where the
   YAML does not carry the key. Comparing two code defaults to each other
   would have passed while the deployed value diverged from both.
2. The expected field list is an EQUALITY, not `len(...) >= 10`. A wiring
   line that disappears fails here instead of silently shrinking coverage.
3. The scan resolves a fallback written as a NAME as well as one written
   as a bare numeric literal, so hoisting a literal to a constant cannot
   make it invisible to this test.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import yaml
from pydantic_core import PydanticUndefined

from src.config import RiskConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = REPO_ROOT / "src" / "pipeline_config_build.py"
SETTINGS = REPO_ROOT / "config" / "settings.yaml"

#: Every `risk.*` field wired through `_risk_setting` in
#: `TradingPipeline.__init__`. An EQUALITY: adding a wiring line without
#: adding it here fails, and so does removing one.
EXPECTED_FIELDS = {
    "max_position_risk_pct",
    "min_position_risk_pct",
    "max_portfolio_risk_pct",
    "max_cluster_risk_share_pct",
    "max_position_pct",
    "max_gross_exposure_x",
    "min_stop_atr_multiple",
    "absolute_min_stop_atr_multiple",
    "min_target_atr_multiple",
    "breakout_projection_atr_multiple",
    "max_target_reach_atr_multiple",
    "max_target_horizon_sessions",
}


def _scan_fallbacks() -> dict[str, float]:
    """`{field: fallback}` for every `_risk_setting(...)` call in the file.

    An AST walk rather than a regex, so a fallback hoisted to a named
    module constant resolves to its value instead of vanishing. The constant
    may live in src/pipeline.py or be IMPORTED from another module: board
    item 216 (2026-10-01) collapsed the short-side gap haircut to a single
    definition in `src.risk.constants`, so the wiring line now reads
    `_risk_setting("max_gross_exposure_x", MAX_GROSS_EXPOSURE_X_DEFAULT)`
    with the number defined one import away. Following the import is the
    right answer: restoring a literal here to keep the scanner happy would
    recreate exactly the duplicate definition that item was closed to remove,
    and the fallback stays genuinely checked against the deployed value.
    """
    tree = ast.parse(PIPELINE.read_text())
    module_consts: dict[str, float] = {}
    imported_from: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                imported_from[alias.asname or alias.name] = node.module
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, (int, float)) and not isinstance(
                node.value.value,
                bool,
            ):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        module_consts[tgt.id] = float(node.value.value)

    found: dict[str, float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_risk_setting"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
            found[name] = float(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in module_consts:
            found[name] = module_consts[arg.id]
        elif isinstance(arg, ast.Name) and arg.id in imported_from:
            # A constant defined in another module and imported here.
            module = importlib.import_module(imported_from[arg.id])
            value = getattr(module, arg.id, None)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AssertionError(
                    f'`_risk_setting("{name}", {arg.id})` falls back to '
                    f"`{imported_from[arg.id]}.{arg.id}`, which is not a "
                    "number. The fallback must resolve to the value the desk "
                    "trades, not to an object this check cannot compare.",
                )
            found[name] = float(value)
        else:  # pragma: no cover - a shape this test cannot resolve
            raise AssertionError(
                f'`_risk_setting("{name}", ...)` has a fallback this test '
                "cannot resolve to a number. Teach `_scan_fallbacks` the new "
                "shape rather than leaving the fallback unchecked.",
            )
    return found


def _deployed_risk() -> dict[str, float]:
    data = yaml.safe_load(SETTINGS.read_text()) or {}
    risk = data.get("risk") or {}
    return {k: float(v) for k, v in risk.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def test_the_expected_field_set_is_exactly_what_is_wired() -> None:
    found = set(_scan_fallbacks())
    assert found == EXPECTED_FIELDS, (
        "The set of risk fields wired through `_risk_setting` in "
        "src/pipeline.py changed.\n"
        f"  wired but not expected: {sorted(found - EXPECTED_FIELDS)}\n"
        f"  expected but not wired: {sorted(EXPECTED_FIELDS - found)}\n"
        "Update EXPECTED_FIELDS deliberately. This is an equality precisely "
        "so a wiring line cannot disappear and quietly shrink the check."
    )


def test_every_fallback_equals_the_value_the_desk_trades() -> None:
    """The fallback must equal the DEPLOYED value, not merely a code default.

    `config/settings.yaml` is what the desk runs on. Where it carries the
    key, that is the comparison. Where it does not, the ratified
    `RiskConfig` field default is, and a field with neither is reported
    rather than skipped.
    """
    deployed = _deployed_risk()
    problems: list[str] = []

    for name, fallback in sorted(_scan_fallbacks().items()):
        if name in deployed:
            if deployed[name] != fallback:
                problems.append(
                    f"{name}: src/pipeline.py falls back to {fallback} but "
                    f"config/settings.yaml deploys {deployed[name]}",
                )
            continue

        field = RiskConfig.model_fields.get(name)
        if field is None:
            problems.append(
                f"{name}: wired through `_risk_setting` but is neither a "
                "`RiskConfig` field nor a key in config/settings.yaml",
            )
            continue
        default = field.default
        if default is PydanticUndefined or default is None:
            problems.append(
                f"{name}: absent from config/settings.yaml AND has no "
                "RiskConfig default, so the pipeline fallback is the only "
                f"thing deciding it ({fallback}). Deploy it or give it a "
                "default; do not leave it resolved by a fallback alone",
            )
            continue
        if float(default) != fallback:
            problems.append(
                f"{name}: src/pipeline.py falls back to {fallback} but the "
                f"ratified RiskConfig default is {default} (key absent from "
                "config/settings.yaml)",
            )

    assert not problems, (
        "A `_risk_setting` fallback disagrees with the value the desk "
        "trades on. The fallback is what sizing uses whenever the "
        "configured value is absent or is not a real number, so the two "
        "must not drift:\n  " + "\n  ".join(problems)
    )
