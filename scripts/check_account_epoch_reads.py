#!/usr/bin/env python3
"""Fail when decision code reads `trades` or `daily_pnl` raw instead of the current-account views.

After a broker account reset the old account's rows stay (owner ruling 9 Oct
2026: exclude history, never delete it). Code that decides orders, stops or
gross exposure must read `current_account_trades` / `current_account_daily_pnl`
(src/storage/account_epoch.py). No standard linter can express "this table
name may only appear in these functions", so this is a home-made check with a
FIXED allowlist written in the repo: config/check_allowlists/account_epoch_reads.txt.

Each allowlist line is `path::Qualified.function` (or `path::<module>`),
optionally followed by `#` and the reason that reader is deliberately NOT cut.
It scans every SQL string literal under src/ except src/api/ (the dashboard
shows full history). Docstrings are not SQL and are skipped. A stale entry
(no raw read left there) also fails, so the list cannot silently outgrow the code.

Exit 0 when clean, 1 with one line per offence otherwise.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

RAW_READ = re.compile(r"(?<!DELETE )\b(?:FROM|JOIN)\s+(?:trades|daily_pnl)\b", re.IGNORECASE)
EXCLUDED_DIRS = ("src/api/",)
DEFAULT_ALLOWLIST = "config/check_allowlists/account_epoch_reads.txt"


def _strings_by_scope(tree: ast.AST):
    """Yield (qualname, text) for every string literal that is not a bare expression statement."""

    def walk(node: ast.AST, scope: list[str], parent: ast.AST | None):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = [*scope, node.name]
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and not isinstance(parent, ast.Expr):
            yield (".".join(scope) or "<module>"), node.value
        for child in ast.iter_child_nodes(node):
            yield from walk(child, scope, node)

    yield from walk(tree, [], None)


def raw_readers(root: Path) -> set[str]:
    """Every `path::qualname` under root/src (minus the dashboard) holding a raw trades/daily_pnl read."""
    found: set[str] = set()
    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(EXCLUDED_DIRS):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for qualname, text in _strings_by_scope(tree):
            if RAW_READ.search(text):
                found.add(f"{rel}::{qualname}")
    return found


def load_allowlist(path: Path) -> set[str]:
    entries: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.add(entry)
    return entries


def check(root: Path, allowlist: Path) -> list[str]:
    found = raw_readers(root)
    allowed = load_allowlist(allowlist)
    problems = [
        f"{e}: reads trades/daily_pnl raw; read current_account_trades / current_account_daily_pnl, "
        f"or add it to {DEFAULT_ALLOWLIST} with the reason it must see every account"
        for e in sorted(found - allowed)
    ]
    problems += [f"{e}: allowlisted but holds no raw read any more; remove the entry" for e in sorted(allowed - found)]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--allowlist", default=None)
    args = parser.parse_args(argv)
    root = Path(args.root)
    problems = check(root, Path(args.allowlist) if args.allowlist else root / DEFAULT_ALLOWLIST)
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
