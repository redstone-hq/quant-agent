"""Self-proof and ratchet for the boundary harness (conversion step 2)."""

import ast, sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from boundary_harness import ROOT, check_boundary, test_files_referencing_pipeline  # noqa: E402
from scripts import struct_allowlist  # noqa: E402

# Fixed lists, no trunk. This file used to carry `TRADING_PIPELINE_TEST_FILE_BASELINE = 79`,
# then a comparison against origin/main re-measured at check time, which reddened waiting
# changes whenever an unrelated merge moved the trunk. Now the coupled test files are pinned
# by PATH in config/check_allowlists/struct_boundary_pipeline_files.txt and the composed
# mixins by class name in struct_boundary_mixins.txt. A path (or mixin) not listed fails, and
# so does a listed one that no longer occurs. Never a count.


def _composed_mixin_modules(source: str | None = None):
    """{mixin class name: module} for every base composed onto `TradingPipeline` in `source`
    (the working tree's `src/pipeline.py` when None, the trunk's when its text is handed in)."""
    tree = ast.parse((ROOT / "src/pipeline.py").read_text() if source is None else source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TradingPipeline")
    bases = {b.id for b in cls.bases if isinstance(b, ast.Name)}
    mods = {}
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("src.pipeline_"):
            for a in n.names:
                if a.name in bases:
                    mods[a.name] = n.module
    assert set(mods) == bases, "could not locate every composed mixin"
    return mods


def composed_mixin_names(source: str | None = None) -> list[str]:
    """Class names composed onto `TradingPipeline` (the working tree's `src/pipeline.py` when None)."""
    return sorted(_composed_mixin_modules(source))


MIXINS = _composed_mixin_modules()


def _shim_only(module):
    """True when every method of every mixin class in the module is a thin shim (docstring + one return): all bodies lifted."""
    tree = ast.parse((ROOT / (module.replace(".", "/") + ".py")).read_text())
    return all(
        len(f.body) <= 2 and isinstance(f.body[-1], ast.Return)
        for c in tree.body
        if isinstance(c, ast.ClassDef) and c.name.endswith("Mixin")
        for f in c.body
        if isinstance(f, ast.FunctionDef)
    )


def test_harness_passes_pipeline_sizing():
    v = check_boundary("src.pipeline_sizing")
    assert v.passed, v.failures


@pytest.mark.parametrize("name,module", sorted(MIXINS.items()))
def test_harness_fails_every_composed_mixin(name, module):
    v = check_boundary(module)
    assert not v.passed, f"{name} must not be a boundary"
    assert 1 in v.failures  # no __init__
    assert 2 in v.failures or _shim_only(module), f"{name} keeps bodies yet reads no foreign self attrs"


MIXIN_FIX = "Hold a part instead of composing another mixin onto TradingPipeline."


def test_composed_mixins_match_the_fixed_list():
    """Replaces `assert len(MIXINS) == 8`: a count let a swap (drop one, compose another) pass.
    Identity by class name against the fixed list; the list only shrinks."""
    bad = struct_allowlist.problems("boundary_mixins", composed_mixin_names(), MIXIN_FIX)
    assert not bad, "\n".join(bad)


def test_swapping_one_mixin_for_another_is_caught(tmp_path):
    """Self-proof: a one-for-one swap keeps the count and is still refused by identity."""
    source = (ROOT / "src/pipeline.py").read_text()
    victim = sorted(MIXINS)[0]
    swapped = source.replace(victim, "FreshlyComposedMixin")
    struct_allowlist.write("boundary_mixins", composed_mixin_names(source), tmp_path)
    assert struct_allowlist.problems("boundary_mixins", composed_mixin_names(source), MIXIN_FIX, tmp_path) == []
    bad = struct_allowlist.problems("boundary_mixins", composed_mixin_names(swapped), MIXIN_FIX, tmp_path)
    assert len(bad) == 2 and any("NEW" in b and "FreshlyComposedMixin" in b for b in bad), bad
    assert any("STALE" in b and victim in b for b in bad), bad


def test_clause_2_catches_foreign_self_reads(tmp_path, monkeypatch):
    import boundary_harness as h

    (tmp_path / "src").mkdir()
    (tmp_path / "src/m.py").write_text(
        "class M:\n    def __init__(self, a):\n        self.a = a\n"
        "    def go(self):\n        return self.a + self.other()\n"
    )
    monkeypatch.setattr(h, "ROOT", tmp_path)
    v = h.check_boundary("src.m", tests_dir=tmp_path)
    assert 2 in v.failures and 1 not in v.failures


PIPELINE_FIX = (
    "Build the pipeline through tests/pipeline_factory.build_pipeline or test the "
    "service in isolation; the set of coupled test files may only shrink."
)


def pipeline_coupling_violations(now=None, directory=None):
    """Coupled test files missing from the fixed list, and listed files that no longer couple."""
    now = test_files_referencing_pipeline() if now is None else now
    return struct_allowlist.problems("boundary_pipeline_files", sorted(now), PIPELINE_FIX, directory)


def test_ratchet_coupled_test_files_match_the_fixed_list():
    bad = pipeline_coupling_violations()
    assert not bad, f"{len(bad)} problem(s) with test files naming TradingPipeline:\n" + "\n".join(bad)


def test_ratchet_new_listed_stale_and_net_zero_swap(tmp_path):
    listed = {"tests/test_a.py", "tests/test_b.py"}
    struct_allowlist.write("boundary_pipeline_files", listed, tmp_path)
    assert pipeline_coupling_violations(listed, tmp_path) == []  # listed passes
    stale = pipeline_coupling_violations({"tests/test_a.py"}, tmp_path)  # a removal leaves a stale entry
    assert len(stale) == 1 and "STALE" in stale[0] and "test_b.py" in stale[0]
    new = pipeline_coupling_violations(listed | {"tests/test_c.py"}, tmp_path)
    assert len(new) == 1 and "NEW" in new[0] and "test_c.py" in new[0]
    # Net-zero swap: a count of 2 vs 2 would pass this; the identity does not.
    swap = pipeline_coupling_violations({"tests/test_a.py", "tests/test_c.py"}, tmp_path)
    assert len(swap) == 2


def test_pipeline_holds_the_review_part_and_inherits_no_review_mixin():
    """The last owed boundary (docs/SPLIT_DEFERRED_FINDINGS.md): `TradingPipeline`'s MRO carries no
    `PromptFactsReviewMixin`; the seat builds one `PromptFactsReview` PER CALL from its own
    collaborators (no host is handed to the part, nothing is cached on the seat), so a collaborator
    swapped on the seat between calls is the one the next part is built from."""
    import src.pipeline_prompt_facts_review as shim_module
    from src.pipeline import TradingPipeline
    from src.pipeline_prompt_facts_review import DELEGATED, HOST_COLLABORATORS, review_of
    from src.prompt_facts.review.held import COLLABORATORS, PromptFactsReview

    assert not hasattr(shim_module, "PromptFactsReviewMixin") and not hasattr(shim_module, "HOLDER_ATTR")
    assert not any("Review" in c.__name__ for c in TradingPipeline.__mro__)
    assert PromptFactsReview not in TradingPipeline.__mro__
    assert tuple(HOST_COLLABORATORS.values()) == COLLABORATORS  # every seat name maps onto a part keyword
    from src.pipeline_prompt_facts import PromptFactsMixin

    pipe = object.__new__(PromptFactsMixin)  # the seat that holds the part; no pipeline is built
    first, second = review_of(pipe), review_of(pipe)
    assert isinstance(first, PromptFactsReview) and first is not second  # rebuilt per call, never cached
    assert not hasattr(first, "_host") and "_prompt_facts_review" not in vars(pipe)
    assert all(getattr(TradingPipeline, name)._held_delegate == name for name in DELEGATED)
    pipe.db = object()
    assert pipe._review_blocked().db is pipe.db  # the next call is built from the seat's current db
