"""The gate that refuses an unjustified edit to a guard, proved both ways.

Refusal cases run in throwaway repos; origin/main is never touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import guard_weakening_gate as gate
from tests.test_definition_of_done import _commit, _git, _repo, _write

JUSTIFIED = (
    "Guard-rule-change: the old byte-total rule is gameable because deleting "
    "a test file frees budget for monolith growth; the new rule compares "
    "per-file identities so freed budget in one file never offsets another."
)


def _base(tmp_path: Path) -> tuple[Path, str]:
    repo = _repo(tmp_path)
    _write(repo, "scripts/size_guard.py", '"""Doc."""\nLIMIT = 300\n')
    _write(repo, "src/app.py", "x = 1\n")
    _commit(repo, "base", "scripts/size_guard.py", "src/app.py")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def _problems(repo: Path, base: str) -> list[str]:
    return gate.problems(base, repo)


def test_guard_edit_without_justification_fails(tmp_path):
    repo, base = _base(tmp_path)
    _write(repo, "scripts/size_guard.py", '"""Doc."""\nLIMIT = 600\n')
    _commit(repo, "loosen", "scripts/size_guard.py")
    out = _problems(repo, base)
    assert out and "Guard-rule-change:" in out[0] and "25 words" in out[0]


def test_guard_edit_with_justification_passes(tmp_path):
    repo, base = _base(tmp_path)
    _write(repo, "scripts/size_guard.py", '"""Doc."""\nLIMIT = 600\n')
    _commit(repo, "loosen\n\n" + JUSTIFIED, "scripts/size_guard.py")
    assert _problems(repo, base) == []


def test_too_short_or_wrapped_justification_fails(tmp_path):
    repo, base = _base(tmp_path)
    _write(repo, "scripts/size_guard.py", "LIMIT = 600\n")
    wrapped = "\n".join(JUSTIFIED.split(". ")[0].split(";"))
    _commit(
        repo,
        "x\n\nGuard-rule-change: because I said so\n" + wrapped.replace("Guard-rule-change:", "Guard-rule-change:\n"),
        "scripts/size_guard.py",
    )
    assert _problems(repo, base)


def test_deleting_a_guard_needs_the_line(tmp_path):
    repo, base = _base(tmp_path)
    _git(repo, "rm", "-q", "scripts/size_guard.py")
    _git(repo, "commit", "-q", "-m", "drop")
    out = _problems(repo, base)
    assert out and "deleted" in out[0]


def test_comment_and_docstring_only_edit_is_not_caught(tmp_path):
    repo, base = _base(tmp_path)
    _write(repo, "scripts/size_guard.py", '"""A rewritten docstring."""\n# new comment\nLIMIT   =   300\n')
    _commit(repo, "docs", "scripts/size_guard.py")
    assert _problems(repo, base) == []


def test_a_new_guard_file_is_not_caught_and_a_non_guard_edit_is_not(tmp_path):
    repo, base = _base(tmp_path)
    _write(repo, "scripts/new_guard.py", "LIMIT = 1\n")
    _write(repo, "src/app.py", "x = 2\n")
    _commit(repo, "add", "scripts/new_guard.py", "src/app.py")
    assert _problems(repo, base) == []


def test_unreadable_base_is_a_refusal_not_a_pass(tmp_path):
    repo, _ = _base(tmp_path)
    assert gate.problems(None, repo)
    assert gate.problems("0" * 40, repo)


def test_the_guard_set_is_derived_from_the_naming_rule():
    assert gate.is_guard("scripts/file_size_guard.py")
    assert gate.is_guard("tests/test_file_size_ratchet.py")
    assert gate.is_guard("tests/test_disk_guard.py")
    assert not gate.is_guard("scripts/definition_of_done.py")
    assert not gate.is_guard("src/deep/x_guard.py")


def test_this_change_justifies_every_guard_it_edits():
    out = gate.problems(gate.dod.base_ref())
    assert not out, "\n".join(f"- {p}" for p in out)


UNNAMED = (
    'import subprocess\nimport sys\n\n\ndef main():\n    out = subprocess.run(["git", "diff"])\n'
    "    raise SystemExit(1 if out.stdout else 0)\n"
)


def test_identity_signal_sees_a_guard_whatever_it_is_called():
    assert gate.behaves_as_guard(UNNAMED)
    assert not gate.is_guard("scripts/check_thing.py")
    assert not gate.behaves_as_guard("def f():\n    return 1\n")
    assert not gate.behaves_as_guard("raise SystemExit(1)\n")  # refuses, reads nothing


def test_identity_gate_catches_an_unnamed_guard_the_name_rule_missed(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "scripts/check_thing.py", UNNAMED)
    _commit(repo, "base", "scripts/check_thing.py")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    _write(repo, "scripts/check_thing.py", UNNAMED.replace("1 if out.stdout else 0", "0"))
    _commit(repo, "weaken", "scripts/check_thing.py")
    assert gate.problems(base, repo, enforce_behaviour=False) == []  # old name rule: silent
    out = gate.problems(base, repo, enforce_behaviour=True)
    assert out and "scripts/check_thing.py" in out[0]
    assert gate.unenforced_touched(base, repo) == ["scripts/check_thing.py"]


def test_identity_is_judged_on_the_base_so_a_rewrite_cannot_escape(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "scripts/check_thing.py", UNNAMED)
    _commit(repo, "base", "scripts/check_thing.py")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    _write(repo, "scripts/check_thing.py", "def main():\n    return 0\n")
    _commit(repo, "gut it", "scripts/check_thing.py")
    assert gate.problems(base, repo, enforce_behaviour=True)


def test_coverage_is_reported_and_enforcement_state_is_explicit():
    scripts, real, named = gate.coverage()
    assert scripts >= real >= named > 0
    assert gate.ENFORCE_BEHAVIOURAL is False


# --- allow-lists and ruff exceptions: additions need the line, shrinking does not ---

_ALLOW = "config/check_allowlists/example.txt"
_PYPROJECT = (
    '[project]\nname = "x"\n\n[tool.ruff.lint.per-file-ignores]\n'
    '"src/a.py" = ["E501"]\n\n[tool.ruff.lint.mccabe]\nmax-complexity = 10\n'
)


def _limits_base(tmp_path: Path) -> tuple[Path, str]:
    repo = _repo(tmp_path)
    _write(repo, _ALLOW, "# comment\nsrc/a.py\nsrc/b.py\n")
    _write(repo, "pyproject.toml", _PYPROJECT)
    _commit(repo, "base", _ALLOW, "pyproject.toml")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def test_an_added_allowlist_line_is_refused_without_the_line(tmp_path):
    repo, base = _limits_base(tmp_path)
    _write(repo, _ALLOW, "# comment\nsrc/a.py\nsrc/b.py\nsrc/c.py\n")
    _commit(repo, "grow", _ALLOW)
    out = _problems(repo, base)
    assert out and _ALLOW in out[0]


def test_an_added_allowlist_line_passes_with_the_line(tmp_path):
    repo, base = _limits_base(tmp_path)
    _write(repo, _ALLOW, "# comment\nsrc/a.py\nsrc/b.py\nsrc/c.py\n")
    _commit(repo, "grow\n\n" + JUSTIFIED, _ALLOW)
    assert _problems(repo, base) == []


def test_removing_an_allowlist_line_or_editing_a_comment_passes(tmp_path):
    repo, base = _limits_base(tmp_path)
    _write(repo, _ALLOW, "# a new comment\nsrc/a.py\n")
    _commit(repo, "shrink", _ALLOW)
    assert _problems(repo, base) == []


def test_a_new_allowlist_file_with_entries_is_refused(tmp_path):
    repo, base = _limits_base(tmp_path)
    _write(repo, "config/check_allowlists/new.txt", "src/z.py\n")
    _commit(repo, "new list", "config/check_allowlists/new.txt")
    assert _problems(repo, base)


def test_a_pyproject_per_file_ignore_addition_is_refused(tmp_path):
    repo, base = _limits_base(tmp_path)
    _write(
        repo,
        "pyproject.toml",
        _PYPROJECT.replace('"src/a.py" = ["E501"]\n', '"src/a.py" = ["E501"]\n"src/b.py" = ["C901"]\n'),
    )
    _commit(repo, "ignore", "pyproject.toml")
    out = _problems(repo, base)
    assert out and "pyproject.toml" in out[0]


def test_a_raised_ruff_limit_is_refused_and_a_removed_ignore_passes(tmp_path):
    repo, base = _limits_base(tmp_path)
    _write(repo, "pyproject.toml", _PYPROJECT.replace("max-complexity = 10", "max-complexity = 20"))
    _commit(repo, "raise", "pyproject.toml")
    assert _problems(repo, base)
    (tmp_path / "two").mkdir()
    repo2, base2 = _limits_base(tmp_path / "two")
    _write(repo2, "pyproject.toml", _PYPROJECT.replace('"src/a.py" = ["E501"]\n', ""))
    _commit(repo2, "drop ignore", "pyproject.toml")
    assert _problems(repo2, base2) == []


def test_pyproject_edits_outside_ruff_tables_are_not_caught(tmp_path):
    repo, base = _limits_base(tmp_path)
    _write(repo, "pyproject.toml", _PYPROJECT.replace('name = "x"', 'name = "y"'))
    _commit(repo, "rename", "pyproject.toml")
    assert _problems(repo, base) == []


FONT_BYTES = b"wOF2\x00\x01\xa8\xff\x00binary font body \xa8\xfe"


def test_binary_deletion_does_not_crash_and_guard_edits_still_fail(tmp_path):
    # A deleted binary (here a font, as when the dashboard bundle was
    # untracked) once crashed the gate with UnicodeDecodeError on its
    # base contents. Binaries cannot be guard files: they are skipped.
    repo, base = _base(tmp_path)
    (repo / "src/font.woff2").write_bytes(FONT_BYTES)
    _commit(repo, "add font", "src/font.woff2")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "src/font.woff2").unlink()
    _commit(repo, "drop font", "src/font.woff2")
    assert _problems(repo, base) == []
    # The same binary deletion alongside an unjustified guard edit still fails.
    _write(repo, "scripts/size_guard.py", '"""Doc."""\nLIMIT = 600\n')
    _commit(repo, "loosen", "scripts/size_guard.py")
    out = _problems(repo, base)
    assert out and "size_guard.py" in out[0] and "Guard-rule-change:" in out[0]
