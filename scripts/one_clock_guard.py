"""One clock: ``src/`` reads the instant from ``src.trading_calendar`` and nowhere else.

The desk ran on two clocks. SQLite's ``datetime('now')`` / ``CURRENT_TIMESTAMP`` /
``CURRENT_DATE`` reads the OS at the moment each statement runs; a bare
``datetime.now()`` / ``date.today()`` / ``datetime.utcnow()`` reads it again
in Python. Neither can be frozen by a test, so the SQL half is faked by
rewriting SQL text (``src/cost_circuit/clock.py``), and between UTC midnight and
ET midnight the two halves name different days. Three incidents had this shape.

THE RULE (tightened 2026-10-13; the earlier guard only refused a module that
MIXED a Python day with SQL ``'now'``): outside the clock module, ``src/`` may
hold NEITHER
  sql_now  a string constant containing ``'now'``, ``CURRENT_TIMESTAMP`` or
           ``CURRENT_DATE`` (this covers ``datetime('now')``, ``date('now')``
           and table DEFAULTs); docstrings excluded
  py_now   a call ``<name>.now(...)``, ``<name>.utcnow(...)`` or
           ``<name>.today(...)`` on a module or class (``datetime.now``,
           ``dt.date.today``, ...) -- never on a call result

Read the instant with ``et_now`` / ``utc_now`` and bind ``sql_utc_stamp(...)``
as a parameter where SQL used to read its own clock.

THE LIMIT IS FIXED and written in the repo: ``config/check_allowlists/one_clock.txt``
lists every site identity that existed on the day the rule was tightened. The
check fails on any site not in that list and on any listed site that no longer
exists, so the list only shrinks. Nothing here reads git or the trunk.
Run: ``python -m scripts.one_clock_guard``.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from scripts.check_allowlist import ALLOWLIST_DIR, compare, report
from scripts.guard_reference import ROOT, enclosing_scopes, site_identity, working_paths

Site = tuple[str, str, str, str]

#: The one module allowed to read the OS clock, by path. Not a count: a new module is never exempt.
CLOCK_MODULE = "src/trading_calendar.py"

ALLOWLIST = ALLOWLIST_DIR / "one_clock.txt"

_SQL_NOW = re.compile(r"'now'|CURRENT_TIMESTAMP|CURRENT_DATE", re.IGNORECASE)
_PY_NOW = {"now", "utcnow", "today"}


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _py_now(call: ast.Call) -> bool:
    fn = call.func
    return isinstance(fn, ast.Attribute) and fn.attr in _PY_NOW and isinstance(fn.value, (ast.Name, ast.Attribute))


def scan_sites(path: str, text: str) -> list[tuple[str, int, str, str]]:
    """Every (kind, line, scope, source) clock site in one module; the clock module has none."""
    if path == CLOCK_MODULE:
        return []
    tree = ast.parse(text)
    scopes = enclosing_scopes(tree)
    skip = _docstrings(tree)
    found: list[tuple[str, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _py_now(node):
            found.append(("py_now", node.lineno, *site_identity(node, scopes)))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and _SQL_NOW.search(node.value)
        ):
            scope, _ = site_identity(node, scopes)
            found.append(("sql_now", node.lineno, scope, " ".join(node.value.split())[:120]))
    return found


def scanned_paths() -> list[str]:
    return sorted(working_paths("src/*.py"))


def working_sites(paths: list[str] | None = None) -> dict[Site, list[int]]:
    out: dict[Site, list[int]] = {}
    for path in scanned_paths() if paths is None else paths:
        try:
            text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            found = scan_sites(path, text)
        except SyntaxError:
            continue
        for kind, line, scope, src in found:
            out.setdefault((path, kind, scope, src), []).append(line)
    return out


def check(allowlist: Path = ALLOWLIST, sites: dict[Site, list[int]] | None = None) -> tuple[list[str], list[str]]:
    """``(unlisted, stale)``: sites missing from the fixed list, and listed sites now gone."""
    found = working_sites() if sites is None else sites
    return compare([site for site, lines in found.items() for _ in lines], allowlist)


def main(argv: list[str] | None = None) -> int:
    unlisted, stale = check()
    if unlisted or stale:
        print(
            "src/ reads a second clock (SQL 'now'/CURRENT_TIMESTAMP/CURRENT_DATE or a bare "
            "datetime.now/date.today/utcnow); read the instant from src.trading_calendar "
            "(et_now / utc_now) and bind sql_utc_stamp() as a parameter instead of adding these:\n"
            + report(unlisted, stale, ALLOWLIST),
            file=sys.stderr,
        )
        return 1
    print("one-clock guard: clock sites match the fixed allow-list exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
