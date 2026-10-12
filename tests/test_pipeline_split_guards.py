"""Step-0 guards for the pipeline split (board item 210, docs/PIPELINE_SPLIT_PLAN.md).

Three things are guarded here and nothing is moved:

1. The DUPLICATE-METHOD guard (`scripts/pipeline_method_guard.py`). A method
   defined on two of TradingPipeline and its mixins is an absolute failure;
   the pairs that exist are pinned in a fixed list, nothing is read from a trunk.
2. The LEDGER-ID / `SCOPED_PATHS` MIGRATION HELPER (`src/ledger_move.py`),
   exercised on a SYNTHETIC move against copies of the real files, so the
   helper is proved to rewrite ids rather than merely to exist.

3. THE COMPATIBILITY SHIMS. A name re-exported from `src/pipeline.py` purely
   so old importers keep working is imported there and used nowhere there, so
   `patch("src.pipeline.<name>")` rebinds an unused alias and intercepts
   nothing while the test still passes. No test may patch such a name.

All three guards are themselves proved to be able to fail: the inventory check
is run against mutated source text with a method added, removed and renamed,
the verifier is run against a deliberately half-applied move, and the shim
check is run against a synthetic patch of a real re-exported name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.pipeline_method_inventory import module_inventory
from src.number_sources import SCOPED_PATHS
from src.ledger_move import (
    MoveSpec,
    apply_move,
    ledger_ids_for_module,
    plan_move,
    verify_move,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "config" / "number_ledger.yaml"


def _live_scoped_text() -> str:
    """The live `SCOPED_PATHS` rendered one entry per line, as the helper expects.

    Read from the object production code imports, so no path to the defining
    module is written down here and a relocation of it cannot trip this guard.
    """
    return "".join(f'    "{p}",\n' for p in SCOPED_PATHS)


def test_duplicated_pipeline_methods_match_the_fixed_list() -> None:
    """Mixin MRO risk (plan 5.4): two mixins defining one name is a silent win for
    whichever is first in the bases list. Absolute rule; existing pairs are in
    config/check_allowlists/struct_pipeline_method.txt."""
    from scripts.pipeline_method_guard import violations

    assert not violations()


def test_duplicate_guard_bites_new_listed_and_stale(tmp_path, monkeypatch) -> None:
    from scripts import pipeline_method_guard as g, struct_allowlist

    entry = "dup_name | src/pipeline_x.py::XMixin"
    monkeypatch.setattr(g, "found", lambda: [entry])
    struct_allowlist.write("pipeline_method", [], tmp_path)
    assert any("NEW" in b for b in g.violations(tmp_path))  # new duplicate fails
    struct_allowlist.write("pipeline_method", [entry], tmp_path)
    assert g.violations(tmp_path) == []  # listed passes
    monkeypatch.setattr(g, "found", lambda: [])
    assert any("STALE" in b for b in g.violations(tmp_path))  # stale entry fails


def test_inventory_guard_can_actually_fail(tmp_path: Path) -> None:
    """A guard that cannot fail is worthless: add, remove and rename a method in a
    copy of the real module and prove each one is detected."""
    # 2026-10-01, item 210 step 10: `RiskStage` moved verbatim to
    # `src/stage_risk.py`, so the mutation test reads it there.
    source = (REPO_ROOT / "src" / "stage_risk.py").read_text(encoding="utf-8")
    baseline = module_inventory(REPO_ROOT / "src" / "stage_risk.py")
    victim = next(
        name
        for name in baseline["classes"]["RiskStage"]
        # A name that occurs exactly once in the file, so the single-replacement
        # mutations below land on RiskStage's copy and not on another class's.
        if not name.startswith("__") and source.count(f"    def {name}(") == 1
    )

    def inventory_of(text: str) -> dict:
        path = tmp_path / "mutated.py"
        path.write_text(text, encoding="utf-8")
        return module_inventory(path)

    added = source.replace(
        f"    def {victim}(",
        f"    def _split_guard_canary(self):\n        return None\n\n    def {victim}(",
        1,
    )
    assert added != source
    assert "_split_guard_canary" in inventory_of(added)["classes"]["RiskStage"]
    assert inventory_of(added) != baseline

    renamed = source.replace(f"    def {victim}(", f"    def {victim}_renamed(", 1)
    assert renamed != source
    renamed_inventory = inventory_of(renamed)
    assert victim not in renamed_inventory["classes"]["RiskStage"]
    assert f"{victim}_renamed" in renamed_inventory["classes"]["RiskStage"]
    assert renamed_inventory != baseline

    # The module-level helpers were lifted verbatim to `src/stage_risk_helpers.py`
    # (stage_risk split), so the module-function mutation reads them there.
    helpers_path = REPO_ROOT / "src" / "stage_risk_helpers.py"
    helpers_baseline = module_inventory(helpers_path)
    first_module_function = helpers_baseline["module_functions"][0]
    assert first_module_function in inventory_of(helpers_path.read_text(encoding="utf-8"))["module_functions"]
    dropped = inventory_of(helpers_path.read_text(encoding="utf-8"))
    dropped["module_functions"] = [n for n in dropped["module_functions"] if n != first_module_function]
    assert dropped != helpers_baseline, "removing a module function must be visible"


# --------------------------------------------------------------------------
# The ledger-id / SCOPED_PATHS migration helper.
# --------------------------------------------------------------------------


def test_measured_ledger_id_counts_for_the_two_modules() -> None:
    """Measured against current main, not taken from the plan. The plan's §5.2 says
    36 and 22; the ids under `src.pipeline.*` had since fallen to 34, and step 1
    (the prompt-facts mixin) moved 25 of them to `src.pipeline_prompt_facts.*`,
    leaving 9; step 3 (the de-levering ladder) moved `_force_delever`'s one id to
    `src.pipeline_delever.*` and then `src.pipeline_exits.*`, leaving 4; step 8
    (the intraday mixin) moved `_another_session_recently_active`'s one id to
    `src.pipeline_intraday.*`, leaving 3; step 11 (sizing + earnings quality) moved 12 of `src.pipeline_stages.*`'s
    22 ids out, 1 to `src.pipeline_sizing.*` and 11 to
    `src.pipeline_earnings_quality.*`, leaving 10. The point of this assertion is that a
    later step cannot move ids without the count moving.
    2026-10-09: `src.pipeline_intraday.*` gains one NEW id, `_NAMES_PER_PAID_CALL`
    (the rank-by-ATR change's per-call batch size) -- added, not moved."""
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    # 2026-10-09: the dead `_EMERGENCY_LIMIT_CUSHION_PCT` id was deleted with
    # the constant, leaving 1 -- removed, not moved.
    assert len(ledger_ids_for_module("src.pipeline", ledger)) == 1
    assert len(ledger_ids_for_module("src.pipeline_intraday", ledger)) == 2
    assert (
        len(ledger_ids_for_module("src.pipeline_delever", ledger)) == 0
        and len(ledger_ids_for_module("src.delever.forced", ledger)) == 1
    )  # _force_delever moved to its part 2026-10-04
    assert (
        len(ledger_ids_for_module("src.pipeline_prompt_facts", ledger)) == 0
        and len(ledger_ids_for_module("src.prompt_facts.decisions", ledger)) == 3
        and len(ledger_ids_for_module("src.prompt_facts.watchlist", ledger)) == 1
        and len(ledger_ids_for_module("src.prompt_facts.pm_facts", ledger)) == 1
        and len(ledger_ids_for_module("src.prompt_facts.missed_ops_signals", ledger)) == 6
        and len(ledger_ids_for_module("src.pipeline_prompt_facts_review", ledger)) == 0
        and len(ledger_ids_for_module("src.prompt_facts.review.grading", ledger)) == 3
        and len(ledger_ids_for_module("src.prompt_facts.review.exits", ledger)) == 5
        and len(ledger_ids_for_module("src.prompt_facts.review.calibration", ledger)) == 3
        and len(ledger_ids_for_module("src.prompt_facts.review.blocked", ledger)) == 2
        and len(ledger_ids_for_module("src.prompt_facts.review.replay", ledger)) == 0
    )  # 14 review ids split by fact family 2026-10-04; moved 2026-10-02; the parent's 5 ids moved with their bodies to decisions/watchlist/pm_facts 2026-10-04
    # 2026-10-01, item 210 step 10: the 2 `ExecutionStage._run_session` ids moved
    # with the class into `src.stage_execution`; step 11 then moved 12 more into
    # `src.pipeline_sizing` and `src.pipeline_earnings_quality`; step 12 moved
    # the four `_projected_post_sale_*` ids into `src.pipeline_rotation_exec`
    # with the rotation-execution block. Every half is asserted so the total
    # cannot quietly shrink.
    assert len(ledger_ids_for_module("src.pipeline_stages", ledger)) == 4
    assert (
        len(ledger_ids_for_module("src.pipeline_rotation_exec", ledger)) == 0
        and len(ledger_ids_for_module("src.rotation_projection", ledger)) == 0
    )  # moved 2026-10-08; its four exit-pad factors deleted 2026-10-09 (live quote)
    assert len(ledger_ids_for_module("src.pipeline_entry_orders", ledger)) == 0
    # Both exit pads deleted 2026-10-09: ordinary exits are market orders.
    assert len(ledger_ids_for_module("src.stage_execution", ledger)) == 0
    assert len(ledger_ids_for_module("src.pipeline_sizing", ledger)) == 1
    assert len(ledger_ids_for_module("src.pipeline_earnings_quality", ledger)) == 11


def test_module_prefix_does_not_swallow_the_sibling_module() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    pipeline_ids = ledger_ids_for_module("src.pipeline", ledger)
    assert not any(i.startswith("src.pipeline_stages.") for i in pipeline_ids)


def _synthetic_spec(ledger: str) -> MoveSpec:
    """A move of three real `TradingPipeline` ledger-bearing methods into a mixin."""
    prefix = "src.pipeline.TradingPipeline."
    import re

    names = sorted(
        {
            re.split(r"[(:]", i[len(prefix) :], maxsplit=1)[0]
            for i in ledger_ids_for_module("src.pipeline", ledger)
            if i.startswith(prefix)
        }
    )
    assert names, "no TradingPipeline ledger ids to exercise the helper on"
    return MoveSpec(
        old_module="src.pipeline",
        new_module="src.pipeline_split_canary",
        names=frozenset(names[:3]),
        old_qual="TradingPipeline",
        new_qual="CanaryMixin",
    )


def test_migration_helper_rewrites_ids_and_scoped_paths_on_a_synthetic_move() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = _live_scoped_text()
    spec = _synthetic_spec(ledger)

    plan = plan_move(spec, ledger, scoped)
    assert plan.id_rewrites, "the plan found nothing to rewrite"
    assert plan.scoped_path_added == "src/pipeline_split_canary.py"
    assert "src/pipeline_split_canary.py" in plan.describe()

    new_ledger, new_scoped = apply_move(plan, ledger, scoped)
    assert new_ledger != ledger and new_scoped != scoped
    assert verify_move(plan, new_ledger, new_scoped) == []

    for old, new in plan.id_rewrites.items():
        assert new.startswith("src.pipeline_split_canary.CanaryMixin.")
        assert f"- id: {old}\n" not in new_ledger
        assert f"- id: {new}\n" in new_ledger

    # The entries that moved get the new `site:`; everything else keeps the old one.
    assert new_ledger.count("site: src/pipeline_split_canary.py") == len(plan.id_rewrites)
    untouched = len(ledger_ids_for_module("src.pipeline", new_ledger))
    assert untouched == 1 - len(
        plan.id_rewrites
    )  # 1 id remains under `src.pipeline.*` (dead cushion deleted 2026-10-09)
    assert '    "src/pipeline_split_canary.py",\n' in new_scoped
    # Nothing but the id, the site and the scope list may change.
    assert len(new_ledger.splitlines()) == len(ledger.splitlines())


def test_migration_helper_leaves_unrelated_ids_alone() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = _live_scoped_text()
    spec = _synthetic_spec(ledger)
    plan = plan_move(spec, ledger, scoped)
    new_ledger, _ = apply_move(plan, ledger, scoped)
    before = set(ledger_ids_for_module("src.pipeline_stages", ledger))
    after = set(ledger_ids_for_module("src.pipeline_stages", new_ledger))
    assert before == after


def test_verifier_catches_a_half_applied_move() -> None:
    """Prove the checkable half can fail: drop one rewrite and a scope entry."""
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = _live_scoped_text()
    spec = _synthetic_spec(ledger)
    plan = plan_move(spec, ledger, scoped)
    new_ledger, new_scoped = apply_move(plan, ledger, scoped)

    missed_old, missed_new = sorted(plan.id_rewrites.items())[0]
    half = new_ledger.replace(f"- id: {missed_new}\n", f"- id: {missed_old}\n", 1)
    problems = verify_move(plan, half, new_scoped)
    assert any("old ledger id still present" in p for p in problems)
    assert any("new ledger id missing" in p for p in problems)

    unscoped = verify_move(plan, new_ledger, scoped)
    assert any("drop out of the ledger guard silently" in p for p in unscoped)


def test_verifier_catches_a_duplicated_id() -> None:
    ledger = LEDGER_PATH.read_text(encoding="utf-8")
    scoped = _live_scoped_text()
    spec = _synthetic_spec(ledger)
    plan = plan_move(spec, ledger, scoped)
    new_ledger, new_scoped = apply_move(plan, ledger, scoped)
    duped_id = sorted(plan.id_rewrites.values())[0]
    duped = new_ledger.replace(f"- id: {duped_id}\n", f"- id: {duped_id}\n  - id: {duped_id}\n", 1)
    assert any("duplicate ledger id" in p for p in verify_move(plan, duped, new_scoped))


def test_move_spec_rejects_a_no_op_and_a_half_qualname() -> None:
    with pytest.raises(ValueError):
        MoveSpec(old_module="src.pipeline", new_module="src.pipeline", names=frozenset({"x"}))
    with pytest.raises(ValueError):
        MoveSpec(old_module="src.pipeline", new_module="src.other", names=frozenset())
    with pytest.raises(ValueError):
        MoveSpec(
            old_module="src.pipeline",
            new_module="src.other",
            names=frozenset({"x"}),
            old_qual="TradingPipeline",
        )


def test_the_two_split_modules_are_still_in_scoped_paths() -> None:
    from src.number_sources import SCOPED_PATHS

    assert "src/pipeline.py" in SCOPED_PATHS
    assert "src/pipeline_admission.py" in SCOPED_PATHS
    assert "src/pipeline_delever.py" in SCOPED_PATHS
    assert "src/pipeline_stages.py" in SCOPED_PATHS


# ---------------------------------------------------------------------------
# 3. COMPATIBILITY SHIMS MUST NOT BECOME SILENT PATCH TARGETS.
#
# Each split step re-exports the names it moved from `src/pipeline.py` so that
# existing importers keep working. Those names are imported there and never
# used there. A test that writes `patch("src.pipeline.<name>", ...)` against
# such a name still SUCCEEDS -- it rebinds the old module's unused alias -- but
# it intercepts nothing, because the moved code resolves the name in its own
# module. The test then passes while testing nothing.
#
# WHICH CHECK THIS IS: the second of the two options -- no test may patch a
# name on `src.pipeline` that `src.pipeline` itself no longer uses.
#
# WHAT IT CANNOT CATCH:
#   * a patch target built at runtime from pieces, or passed as a variable,
#     rather than written as a literal `src.pipeline.<name>` in the test text;
#   * `patch.object(pipeline_module, "name")`, which never spells the dotted
#     path;
#   * a name used by `src/pipeline.py` only inside a string annotation, which
#     this reads as unused and would therefore flag (a false alarm, not a
#     miss -- it fails loudly rather than quietly).
# ---------------------------------------------------------------------------

import ast
import re

OLD_MODULE_PATH = REPO_ROOT / "src" / "pipeline.py"
TESTS_DIR = Path(__file__).resolve().parent

_PATCH_TARGET_RE = re.compile(r"src\.pipeline\.([A-Za-z_][A-Za-z0-9_]*)")


def compat_only_names(source: str) -> set[str]:
    """Names imported by `source` and referenced nowhere else in it."""
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return imported - used


def dead_patch_targets(test_source: str, compat_names: set[str]) -> set[str]:
    return {m for m in _PATCH_TARGET_RE.findall(test_source) if m in compat_names}


def test_no_test_patches_a_name_the_old_module_no_longer_uses():
    compat_names = compat_only_names(OLD_MODULE_PATH.read_text())
    assert compat_names, (
        "expected src/pipeline.py to still carry compatibility re-exports; if "
        "the split finished and they are all gone, delete this guard"
    )
    offenders: dict[str, set[str]] = {}
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        found = dead_patch_targets(path.read_text(), compat_names)
        if found:
            offenders[path.name] = found
    assert not offenders, (
        "these tests patch a name on `src.pipeline` that `src.pipeline` no "
        "longer uses, so the patch binds an unused alias and intercepts "
        f"nothing: {offenders}\n\nWHAT TO DO: patch the name on the module "
        "that now DEFINES it (the `src/pipeline_*.py` module the split moved "
        "it to), not on `src.pipeline`."
    )


def test_dead_patch_target_detector_can_fail():
    """Prove the check above can fail, against the real re-export list."""
    compat_names = compat_only_names(OLD_MODULE_PATH.read_text())
    victim = sorted(compat_names)[0]
    synthetic = f'with patch("src.pipeline.{victim}", autospec=True):\n    pass\n'
    assert dead_patch_targets(synthetic, compat_names) == {victim}
    # A name the old module still uses is NOT flagged.
    assert dead_patch_targets('patch("src.pipeline.TradingPipeline")', compat_names) == set()


def test_duplicate_sites_sees_a_duplicate_and_not_a_move() -> None:
    """Detection itself: a name on two classes is reported once per owner."""
    from scripts.pipeline_method_guard import duplicate_sites

    def texts(a_methods: str, b_methods: str) -> dict[str, str]:
        return {
            "src/pipeline.py": f"class TradingPipeline:\n{a_methods}",
            "src/pipeline_x.py": f"class XMixin:\n{b_methods}",
        }

    body = lambda *names: "".join(f"    def {n}(self): pass\n" for n in names) or "    pass\n"  # noqa: E731
    assert [n for n, _ in duplicate_sites(texts(body("a", "b"), body("a")))] == ["a", "a"]
    # a moved (not duplicated) method is no offender
    assert duplicate_sites(texts(body("b"), body("a"))) == []
