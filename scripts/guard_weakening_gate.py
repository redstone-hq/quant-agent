"""Refuse a change that edits a guard unless a commit justifies it.

Why: a change was nearly merged that loosened the file-size ratchet so two
blocked changes would go green; the loosened rule was gameable (delete a
300-line test file, grow a monolith by 299 lines). Nothing in CI objected.

WHICH FILES ARE GUARDS (derived, never listed -- a list rots)
    scripts/*guard*.py          any script whose name says guard
    tests/test_*guard*.py       any test whose name says guard
    tests/test_*ratchet*.py     any test whose name says ratchet
A new guard that follows the naming rule is covered the day it is added.

WHAT A GUARD IS, BY IDENTITY (measured, 2026-10-05, scripts/*.py on trunk)
    A script that REFUSES (raises SystemExit / calls sys.exit) AND INSPECTS the
    repository or trunk (a git call, origin/main, a tree walk, an AST parse).
    Of 78 scripts, 39 behave that way; the name rule covered only 17 of them.
    That identity set is REPORTED on every run, and with ENFORCE_BEHAVIOURAL
    switched on it is also demanded. It is OFF today on purpose: open changes
    edit 8 of the 22 uncovered scripts, and demanding a trailer of them would
    turn every one red at once. Tests are not widened (see coverage()).

WHAT IS CAUGHT
    Any edit or deletion of an existing guard file whose behaviour could have
    changed. Tightening cannot be told from loosening reliably (a threshold's
    direction depends on the guard), so the rule is deliberately over-asking:
    EVERY behavioural edit needs the justification. The only exemptions are
    provable no-ops: a new guard file (it only adds refusals), and a .py edit
    whose AST is identical once docstrings are stripped (comments, blank lines
    and formatting never reach the AST).

WHAT ELSE IS COVERED (limits and exceptions, by pattern)
    config/check_allowlists/*.txt   any edit that ADDS a line (blank lines and
                                    # comments do not count). Pure deletions
                                    shrink an allow-list and need nothing.
    pyproject.toml                  any added line inside a [tool.ruff...]
                                    table: per-file-ignores, limits, selects.
                                    Removed lines need nothing.
Pattern-based, so a new allow-list file is covered the day it lands.

THE JUSTIFICATION
    A commit message line, ONE PHYSICAL LINE, starting `Guard-rule-change:`
    of at least 25 words that (a) names the mechanism making the existing rule
    incorrect and (b) states why the new rule cannot be gamed. Word count is
    mechanical; (a)/(b) are for the reviewer and the adversary.

STORES NOTHING. Computed against origin/main at check time; when the base
cannot be read the gate FAILS -- inability to compare is never a pass.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

try:
    from scripts import definition_of_done as dod
except ImportError:  # run as a bare script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import definition_of_done as dod

GUARD_PATTERNS = ("scripts/*guard*.py", "tests/test_*guard*.py", "tests/test_*ratchet*.py")
ALLOWLIST_PATTERN = "config/check_allowlists/*.txt"
PYPROJECT = "pyproject.toml"
ENFORCE_BEHAVIOURAL = False  # report-only until the open changes that edit them land
_INSPECTS = {"rglob", "glob", "walk", "iterdir", "parse", "_git", "base_ref", "file_at", "check_output", "run"}
MIN_WORDS = 25
LINE = re.compile(r"^[ \t]*Guard-rule-change[ \t]*:[ \t]*(.+?)[ \t]*$", re.M)

REQUIREMENT = (
    "a change that edits a guard must carry, on ONE PHYSICAL LINE of a commit "
    f"message, `Guard-rule-change:` followed by at least {MIN_WORDS} words that "
    "(a) name the mechanism making the existing rule incorrect and (b) state "
    "why the new rule cannot be gamed"
)


def is_guard(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, p) and path.count("/") == p.count("/") for p in GUARD_PATTERNS)


def is_allowlist(path: str) -> bool:
    return fnmatch.fnmatchcase(path, ALLOWLIST_PATTERN) and path.count("/") == 2


def _allowlist_entries(text: str | None) -> Counter:
    lines = (ln.strip() for ln in (text or "").splitlines())
    return Counter(ln for ln in lines if ln and not ln.startswith("#"))


def _ruff_entries(text: str | None) -> Counter:
    """Meaningful lines inside any [tool.ruff...] table of a pyproject."""
    out: Counter = Counter()
    inside = False
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if ln.startswith("["):
            inside = ln.startswith("[tool.ruff")
            continue
        if inside and ln and not ln.startswith("#"):
            out[ln] += 1
    return out


def adds_exception(path: str, before: str | None, after: str | None) -> bool:
    """True when the edit adds an allow-list line or a ruff exception/limit line."""
    if is_allowlist(path):
        extract = _allowlist_entries
    elif path == PYPROJECT:
        extract = _ruff_entries
    else:
        return False
    return bool(extract(after) - extract(before))


def behaves_as_guard(source: str) -> bool:
    """Identity test: the code can refuse (non-zero exit) and reads the repo."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    refuses = inspects = False
    for n in ast.walk(tree):
        if isinstance(n, ast.Raise) and n.exc is not None:
            refuses = refuses or "SystemExit" in ast.dump(n.exc)
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            refuses = refuses or (name == "exit" and getattr(getattr(f, "value", None), "id", "") == "sys")
            inspects = inspects or name in _INSPECTS
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            inspects = inspects or "origin/main" in n.value
    return refuses and inspects


def _is_script(path: str) -> bool:
    return path.startswith("scripts/") and path.endswith(".py")


def _covered(path: str, source: str | None, enforce_behaviour: bool) -> bool:
    if is_guard(path):
        return True
    return bool(enforce_behaviour and _is_script(path) and source is not None and behaves_as_guard(source))


def coverage(repo: Path | None = None) -> tuple[int, int, int]:
    """(scripts searched, behave as guards, of those caught by name)."""
    repo = repo or dod.REPO_ROOT
    found = [p for p in sorted((repo / "scripts").glob("*.py")) if behaves_as_guard(p.read_text())]
    named = sum(is_guard(f"scripts/{p.name}") for p in found)
    return len(list((repo / "scripts").glob("*.py"))), len(found), named


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def behaviour_unchanged(before: str, after: str) -> bool:
    """True only when provably the same code: identical AST sans docstrings."""
    try:
        a = ast.dump(_strip_docstrings(ast.parse(before)))
        b = ast.dump(_strip_docstrings(ast.parse(after)))
    except SyntaxError:
        return False
    return a == b


def _is_candidate(path: str) -> bool:
    """A path the gate may have to judge: never skipped, whatever git says about it."""
    return is_guard(path) or is_allowlist(path) or path == PYPROJECT or _is_script(path)


def _changed_paths(base: str, repo: Path) -> subprocess.CompletedProcess:
    return dod._git("diff", "--name-only", "--no-renames", f"{base}...HEAD", repo=repo)


def _text_at(base: str, path: str, repo: Path) -> tuple[str | None, str | None]:
    """(base text, working text); None where the file is absent.

    Raises UnicodeDecodeError when either side is not UTF-8 text. "Binary"
    is decided by decoding the bytes, never by git's attributes: a
    .gitattributes `binary`/`-diff` line must not be able to hide a file.
    """
    r = subprocess.run(["git", "-C", str(repo), "show", f"{base}:{path}"], capture_output=True, check=False)
    before = r.stdout.decode("utf-8") if r.returncode == 0 else None
    f = repo / path
    after = f.read_bytes().decode("utf-8") if f.exists() else None
    return before, after


def _texts_or_flag(base: str, path: str, repo: Path, unreadable: list[str]) -> tuple[str | None, str | None] | None:
    """Both texts of `path`, or None when it is not text.

    Only a non-candidate (a font, an image) is skipped silently; a candidate
    that cannot be decoded is added to `unreadable`, which fails the gate.
    """
    try:
        return _text_at(base, path, repo)
    except UnicodeDecodeError:
        if _is_candidate(path):
            unreadable.append(_undecodable(path))
        return None


def _undecodable(path: str) -> str:
    return (
        f"{path}: a file this gate must judge is not readable as UTF-8 text; "
        "refusing rather than skipping it (an unreadable guard is never a pass)"
    )


def unenforced_touched(base: str, repo: Path) -> list[str]:
    """Identity-guards this change edits that the name rule does not cover."""
    r = _changed_paths(base, repo)
    out = []
    for path in sorted(p for p in r.stdout.split("\n") if _is_script(p)):
        f = repo / path
        if is_guard(path) or not f.exists():
            continue
        try:
            text = f.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            out.append(path)  # unreadable: reported, never assumed harmless
            continue
        if behaves_as_guard(text):
            out.append(path)
    return out


def _touched_entry(path: str, before: str | None, after: str | None, enforce_behaviour: bool) -> str | None:
    """How this edit is listed as needing justification, or None when it needs none."""
    if adds_exception(path, before, after):
        return path + " (adds an allow-list line or ruff exception)"
    # judged on the BASE text: rewriting a guard so it stops looking like
    # one cannot take it out of scope
    source = before if before is not None else after
    if not _covered(path, source, enforce_behaviour):
        return None
    if before is None:
        return None  # brand-new guard: only adds refusals
    if after is not None and behaviour_unchanged(before, after):
        return None
    return path + (" (deleted)" if after is None else "")


def problems(
    base: str | None, repo: Path | None = None, messages: str | None = None, enforce_behaviour: bool | None = None
) -> list[str]:
    if enforce_behaviour is None:
        enforce_behaviour = ENFORCE_BEHAVIOURAL
    if not base:
        return [
            "cannot read origin/main to compare against; refusing rather "
            "than passing (inability to compare is never a pass)"
        ]
    repo = repo or dod.REPO_ROOT
    r = _changed_paths(base, repo)
    if r.returncode != 0:
        return ["git diff against the base failed; refusing: " + r.stderr.strip()]
    touched = []
    unreadable = []
    for path in sorted(p for p in r.stdout.split("\n") if p):
        texts = _texts_or_flag(base, path, repo, unreadable)
        if texts is None:
            continue
        entry = _touched_entry(path, *texts, enforce_behaviour)
        if entry is not None:
            touched.append(entry)
    if unreadable:
        return unreadable
    if not touched:
        return []
    if messages is None:
        log = dod._git("log", "--format=%B%n", f"{base}..HEAD", repo=repo)
        if log.returncode != 0:
            return ["git log failed; refusing: " + log.stderr.strip()]
        messages = log.stdout
    if any(len(m.split()) >= MIN_WORDS for m in LINE.findall(messages)):
        return []
    return [f"guard file(s) changed without justification: {', '.join(touched)}. Requirement: {REQUIREMENT}."]


def main() -> int:
    base = dod.base_ref()
    scripts, real, named = coverage()
    print(
        f"guard coverage: {real} of {scripts} scripts behave as guards; "
        f"the name rule covers {named}; enforcing identity: {ENFORCE_BEHAVIOURAL}"
    )
    if base and not ENFORCE_BEHAVIOURAL:
        for path in unenforced_touched(base, dod.REPO_ROOT):
            print(f"NOTE (not enforced yet): {path} behaves as a guard")
    out = problems(base)
    for p in out:
        print(f"- {p}")
    return 1 if out else 0


if __name__ == "__main__":
    raise SystemExit(main())
