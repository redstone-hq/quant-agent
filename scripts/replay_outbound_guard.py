"""Board item 202: every place ``src/`` can reach a provider is a NAMED replay seam.

A replay of a recorded session must be deterministic and free. The runtime wall
(``ops/rehearsal/network_wall.py``) catches an outbound attempt when it happens;
this guard catches it when it is WRITTEN. Any source file that imports an HTTP,
socket or provider-SDK client is an outbound site, and a NEW site — a new file,
or a new client module inside an existing file — must first be handled in the
rehearsal: served from a recording by the patch-where-the-client-is-built
pattern, or refused loudly naming the missing recording (see
``ops/rehearsal/feed_recording.py``, ``market_recording.py``).

FIXED EXCEPTION LIST, PER IMPORT
--------------------------------
Every existing outbound import is named in
``config/check_allowlists/struct_replay_outbound.txt`` as ``file | client``: one
line per (file, client module) pair, never per module and never a count. The
check scans the working tree and compares nothing with any trunk. It fails on a
(file, client) pair not in the list (a new file, or a new client in an old file)
and on a listed pair that no longer occurs (stale entry).

``CLIENT_MODULES`` below is the one thing that IS policy — the list of module
names that mean "this code can leave the box" — and it stays in code, where it
is reviewed. It is a definition, not a record of the current state.

Run it directly: ``python -m scripts.replay_outbound_guard``.
"""

from __future__ import annotations

import ast
import sys

from scripts import struct_allowlist
from scripts.guard_reference import ROOT, working_paths

SCAN_DIR = "src"

CLIENT_MODULES = {
    "requests",
    "httpx",
    "urllib",
    "urllib3",
    "aiohttp",
    "curl_cffi",
    "yfinance",
    "fredapi",
    "openai",
    "anthropic",
    "websockets",
    "websocket",
    "http",
    "smtplib",
    "ftplib",
    "telegram",
    "alpaca",
    "alpaca_trade_api",
    "feedparser",
    "pandas_datareader",
    "socket",
    "ssl",
}


# Submodules of a client package that open no connection. Each is named, with
# its reason; there is no wildcard and no count. A module not listed here is a
# client, so a NEW outbound import in the same package is still refused.
NOT_CLIENT_SUBMODULES = {
    "urllib.error": "exception classes (HTTPError, URLError); performs no I/O",
    "urllib.parse": "string splitting and quoting of URLs; performs no I/O",
    "http.server": "inbound listener on a local port; opens no outbound connection",
    "alpaca.trading.requests": "pydantic request models (data classes); no client",
    "alpaca.trading.enums": "plain enums; no client",
}

# Attributes of a client module whose use alone opens nothing, by identity.
NOT_CLIENT_ATTRIBUTES = {
    "socket": {
        "setdefaulttimeout": "sets a process default; opens no socket",
        "getdefaulttimeout": "reads the process default; opens no socket",
    },
}


def _excluded_submodule(dotted: str) -> bool:
    return any(dotted == m or dotted.startswith(m + ".") for m in NOT_CLIENT_SUBMODULES)


def _only_harmless_attributes(tree: ast.AST, name: str) -> bool:
    """True when every use of ``name`` is an attribute listed as not a client."""
    allowed = NOT_CLIENT_ATTRIBUTES.get(name, {})
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            up = parents.get(node)
            if not (isinstance(up, ast.Attribute) and up.value is node and up.attr in allowed):
                return False
    return True


def scan_text(text: str) -> set[str]:
    """Every client module one source file imports, at any nesting depth."""
    tree = ast.parse(text)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in CLIENT_MODULES or _excluded_submodule(a.name):
                    continue
                if a.asname is None and root in NOT_CLIENT_ATTRIBUTES and a.name == root:
                    if _only_harmless_attributes(tree, root):
                        continue
                hits.add(root)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            if root not in CLIENT_MODULES or _excluded_submodule(node.module):
                continue
            allowed = NOT_CLIENT_ATTRIBUTES.get(root, {})
            if node.module == root and allowed and all(a.name in allowed for a in node.names):
                continue
            hits.add(root)
    return hits


def scanned_paths() -> list[str]:
    """Tracked ``.py`` files under ``src/`` in the working tree."""
    return working_paths(f"{SCAN_DIR}/*.py")


def working_sites() -> dict[str, set[str]]:
    """Outbound-client imports per file in this working tree."""
    out: dict[str, set[str]] = {}
    for path in scanned_paths():
        try:
            hits = scan_text((ROOT / path).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue  # unparsable source is a different failure, caught elsewhere
        if hits:
            out[path] = hits
    return out


def found() -> list[str]:
    """One ``file | client`` entry per outbound import in the working tree."""
    return [f"{path} | {client}" for path, hits in sorted(working_sites().items()) for client in sorted(hits)]


def violations(directory=None) -> list[str]:
    """Pairs missing from the fixed list, and listed pairs that no longer occur."""
    return struct_allowlist.problems("replay_outbound", found(), FIX, directory)


FIX = (
    "Handle it in ops/rehearsal first (serve from a recording or refuse naming the "
    "missing recording); only then add the `file | client` line."
)


def main(argv: list[str] | None = None) -> int:
    bad = violations()
    if bad:
        print(
            "Outbound-client imports in src/ differ from the fixed list. A replay must be "
            "served from a recording or refuse naming the missing recording, never reach a "
            "live provider (board item 202):\n%s" % "\n".join(bad),
            file=sys.stderr,
        )
        return 1
    print("replay outbound guard: outbound imports match the fixed list.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
