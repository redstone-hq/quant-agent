"""One clock: ``src/`` reads the instant from ``src.trading_calendar`` and nowhere else.

The desk ran on two clocks. SQLite's ``datetime('now')`` / ``CURRENT_TIMESTAMP`` /
``CURRENT_DATE`` reads the OS at the moment each statement runs; a bare
``datetime.now()`` / ``date.today()`` / ``time.time()`` reads it again in
Python. Neither can be frozen by a test, so the SQL half is faked by rewriting
SQL text (``src/cost_circuit/clock.py``), and between UTC midnight and ET
midnight the two halves name different days. Three incidents had this shape.

TWO RULES (tightened 2026-10-13; the earlier guard held only rule B):

A. CLOCK SITES -- outside the clock module, ``src/`` may hold neither
   sql_now  a string (a literal, adjacent literals, a ``+`` chain of literals,
            or the literal parts of an f-string) that reads SQLite's clock:
            ``'now'`` / ``"now"``, ``CURRENT_TIMESTAMP`` / ``CURRENT_DATE`` /
            ``CURRENT_TIME``, or ``datetime()`` / ``date()`` / ``time()`` /
            ``julianday()`` / ``unixepoch()`` with no argument. This covers
            ``datetime('now', '-2 minutes')``, ``date('now')`` and table
            DEFAULTs. Docstrings are excluded.
   py_now   a Python read of the OS clock: ``<x>.now()`` / ``.utcnow()`` /
            ``.today()`` on a module or class (``datetime.now``, ``dt.date.today``),
            the same attribute referenced without a call (``fn = datetime.now``),
            ``time.time()`` / ``time.time_ns()``, ``time.strftime(fmt)`` with
            no explicit instant, ``time.localtime()`` / ``time.gmtime()`` with
            no argument (also when imported bare), ``fromtimestamp(...)`` /
            ``utcfromtimestamp(...)`` whose argument is computed inline rather
            (not a name, field, index or literal, even through ``int()`` / ``float()``),
            and ``getattr(<x>, "now")`` for any
            of those names.
   The limit is FIXED and written in the repo: ``config/check_allowlists/one_clock.txt``
   lists every site identity (path, kind, enclosing scope, the WHOLE
   whitespace-normalised text) that existed on the day the rule was tightened.
   The check fails on any site not in that list and on any listed site that no
   longer exists, so the list only shrinks. Nothing here reads git or the trunk.

B. MIXED MODULES (the original rule, kept absolute -- no allow-list): a module
   may not derive a day in Python (``et_today()``, ``todays_session_*()``,
   ``date.today()`` or ``<call>.date()``) AND also hold a sql_now site. Every
   py_day site in such a module is a violation, except modules named in
   ``EXEMPT`` (identity, with a reason).

NOT CAUGHT STATICALLY, and not claimed: ``'now'`` arriving as a bound
parameter or built at run time (``"%s" % x``, ``.format``, ``"".join``), SQL
held outside Python source, a clock read reached through a call result
(``factory().now()``), and ``time.strftime`` / ``fromtimestamp`` handed a name
that itself came from the OS clock elsewhere. Those are the reviewer's.

Read the instant with ``et_now`` / ``utc_now`` and bind ``sql_utc_stamp(...)``
or ``sql_utc_stamp_ago(...)`` as a parameter where SQL used to read its own
clock. Run: ``python -m scripts.one_clock_guard``.
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

#: Modules allowed to be MIXED (rule B), by path, with the reason. Never a count.
EXEMPT = {
    CLOCK_MODULE: "IS the clock",
    "src/cost_circuit/clock.py": "holds the SQL-'now' rewriting pin that fakes SQLite's clock in "
    "tests and converts a stored UTC stamp to its ET day; deleted once its sites are bound",
}

ALLOWLIST = ALLOWLIST_DIR / "one_clock.txt"

_SQL_NOW = re.compile(
    r"""['"]now['"]|\bCURRENT_(?:TIMESTAMP|DATE|TIME)\b|\b(?:datetime|date|time|julianday|unixepoch)\s*\(\s*\)""",
    re.IGNORECASE,
)
_NOW_ATTRS = {"now", "utcnow", "today"}
_TIME_MODULE_ATTRS = {"time", "time_ns"}
_NO_ARG_ATTRS = {"localtime", "gmtime"}
_FROM_STAMP_ATTRS = {"fromtimestamp", "utcfromtimestamp"}
_ALL_CLOCK_ATTRS = _NOW_ATTRS | _TIME_MODULE_ATTRS | _NO_ARG_ATTRS | _FROM_STAMP_ATTRS | {"strftime"}
_CLOCK_RECEIVERS = {"datetime", "date", "time", "dt", "_dt", "_datetime", "_time"}
_DAY_READERS = {"et_today", "todays_session_stamp", "todays_session_bar_stamp", "todays_session_snapshot_stamps"}
_STORED = (ast.Name, ast.Attribute, ast.Subscript, ast.Constant)
_CONVERSIONS = {"int", "float", "round", "str"}


def _stored(expr: ast.AST) -> bool:
    """A value read from somewhere (a name, field, index or literal), possibly through int()/float()."""
    if isinstance(expr, _STORED):
        return True
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in _CONVERSIONS
        and all(_stored(a) for a in expr.args)
    )


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _terminal_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _string_pieces(node: ast.AST) -> list[str] | None:
    """The literal text of a string expression the AST can join, else None."""
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_pieces(node.left), _string_pieces(node.right)
        return left + right if left is not None and right is not None else None
    return None


def _sql_strings(tree: ast.AST, skip: set[int]) -> list[tuple[ast.AST, str]]:
    """Every joined string expression, outermost first; a joined expression is one site."""
    out: list[tuple[ast.AST, str]] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        pieces = _string_pieces(node) if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)) else None
        if pieces is not None and id(node) not in skip:
            out.append((node, " ".join("".join(pieces).split())))
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _py_now_call(call: ast.Call) -> bool:
    fn = call.func
    nargs = len(call.args) + len(call.keywords)
    if isinstance(fn, ast.Name):
        if fn.id == "getattr" and len(call.args) >= 2:
            name = call.args[1]
            return isinstance(name, ast.Constant) and name.value in _ALL_CLOCK_ATTRS
        return fn.id in _TIME_MODULE_ATTRS | _NO_ARG_ATTRS and nargs == 0
    if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, (ast.Name, ast.Attribute)):
        return False
    receiver = _terminal_name(fn.value)
    if fn.attr in _NOW_ATTRS:
        return True
    if fn.attr in _TIME_MODULE_ATTRS:
        return receiver in _CLOCK_RECEIVERS
    if fn.attr in _NO_ARG_ATTRS:
        return nargs == 0
    if fn.attr == "strftime":
        return receiver in _CLOCK_RECEIVERS and nargs == 1
    if fn.attr in _FROM_STAMP_ATTRS:
        return bool(call.args) and not _stored(call.args[0])
    return False


def _py_now_reference(attr: ast.Attribute) -> bool:
    """``datetime.now`` named without being called (bound to a variable, passed along)."""
    return attr.attr in _ALL_CLOCK_ATTRS and _terminal_name(attr.value) in _CLOCK_RECEIVERS


def _py_day(call: ast.Call) -> bool:
    fn = call.func
    if _terminal_name(fn) in _DAY_READERS:
        return True
    if isinstance(fn, ast.Attribute) and fn.attr == "date" and isinstance(fn.value, ast.Call):
        return True
    return isinstance(fn, ast.Attribute) and fn.attr == "today" and isinstance(fn.value, (ast.Name, ast.Attribute))


def _classify(path: str, text: str) -> tuple[list[tuple[str, int, str, str]], list[tuple[str, int, str, str]]]:
    """``(clock_sites, py_day_sites)`` for one module."""
    tree = ast.parse(text)
    scopes = enclosing_scopes(tree)
    skip = _docstrings(tree)
    clock: list[tuple[str, int, str, str]] = []
    days: list[tuple[str, int, str, str]] = []
    called: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(id(node.func))
            if _py_now_call(node):
                clock.append(("py_now", node.lineno, *site_identity(node, scopes)))
            if _py_day(node):
                days.append(("py_day", node.lineno, *site_identity(node, scopes)))
        elif isinstance(node, ast.Attribute) and id(node) not in called and _py_now_reference(node):
            clock.append(("py_now", node.lineno, *site_identity(node, scopes)))
    for node, text_ in _sql_strings(tree, skip):
        if _SQL_NOW.search(text_):
            scope, _ = site_identity(node, scopes)
            clock.append(("sql_now", node.lineno, scope, text_))
    return clock, days


def scan_sites(path: str, text: str) -> list[tuple[str, int, str, str]]:
    """Rule A: every (kind, line, scope, text) clock site in one module; the clock module has none."""
    if path == CLOCK_MODULE:
        return []
    return _classify(path, text)[0]


def scan_mixed(path: str, text: str) -> list[tuple[str, int, str, str]]:
    """Rule B: every py_day site in a module that also holds a sql_now site; else none."""
    if path in EXEMPT:
        return []
    clock, days = _classify(path, text)
    return days if days and any(kind == "sql_now" for kind, *_ in clock) else []


def scanned_paths() -> list[str]:
    return sorted(working_paths("src/*.py"))


def _collect(scan, paths: list[str] | None) -> dict[Site, list[int]]:
    out: dict[Site, list[int]] = {}
    for path in scanned_paths() if paths is None else paths:
        try:
            text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
            found = scan(path, text)
        except SyntaxError:
            continue
        for kind, line, scope, src in found:
            out.setdefault((path, kind, scope, src), []).append(line)
    return out


def working_sites(paths: list[str] | None = None) -> dict[Site, list[int]]:
    return _collect(scan_sites, paths)


def mixed_violations(paths: list[str] | None = None) -> list[str]:
    return [
        f"{path} [{kind}] in {scope}: `{src}`; lines {sorted(lines)}"
        for (path, kind, scope, src), lines in sorted(_collect(scan_mixed, paths).items())
    ]


def check(allowlist: Path = ALLOWLIST, sites: dict[Site, list[int]] | None = None) -> tuple[list[str], list[str]]:
    """Rule A ``(unlisted, stale)``: sites missing from the fixed list, and listed sites now gone."""
    found = working_sites() if sites is None else sites
    return compare([site for site, lines in found.items() for _ in lines], allowlist)


def main(argv: list[str] | None = None) -> int:
    unlisted, stale = check()
    mixed = mixed_violations()
    if unlisted or stale:
        print(
            "src/ reads a second clock (SQL 'now'/CURRENT_TIMESTAMP/CURRENT_DATE or a bare "
            "datetime.now/date.today/time.time); read the instant from src.trading_calendar "
            "(et_now / utc_now) and bind sql_utc_stamp() as a parameter instead of adding these:\n"
            + report(unlisted, stale, ALLOWLIST),
            file=sys.stderr,
        )
    if mixed:
        print(
            "a module mixes a Python exchange day with SQL 'now' (UTC); use one clock "
            "(bind the day from et_today and compare timestamps against its UTC bounds):\n" + "\n".join(mixed),
            file=sys.stderr,
        )
    if unlisted or stale or mixed:
        return 1
    print("one-clock guard: clock sites match the fixed allow-list exactly; no mixed module.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
