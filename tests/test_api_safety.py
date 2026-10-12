"""Stage 2 Checkpoint C — structural safety regression tests for the "Thin
Read-Only Mission Control API" under `src/api/`.

These tests are purely static/structural: source-level AST scans of
`src/api/*.py` (and a handful of trading-critical files, checked in the
reverse direction) plus one in-process ASGI smoke test of the app's
GET-only enforcement middleware. None of them seed the DB, hit the network,
or monkeypatch config — that live-behavior coverage lives in a sibling test
file. The goal here is to make it structurally impossible (and loudly
test-visible if it ever becomes possible) for `src/api/` to place, cancel,
or modify a broker order, write to the trading SQLite DB, or import the
trading-execution stack.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
API_DIR = REPO_ROOT / "src" / "api"


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def _api_source_files() -> list[Path]:
    """Every Python module in the API package, however deeply nested.

    `rglob`, not `glob`: a future `src/api/routes/` subpackage must not slip
    out from under the structural guards simply by being one directory down.
    """
    return sorted(API_DIR.rglob("*.py"))


def test_api_source_scan_is_not_silently_empty():
    """A scan that matches almost nothing passes vacuously; fail loudly first.

    Both halves matter: the glob must find files at all, and it must cover
    every module the running app actually loaded, so moving a route module
    somewhere the glob cannot see is a failure here and not a silent hole.
    """
    import sys

    files = _api_source_files()
    assert len(files) >= 5, f"the src/api scan found almost nothing: {files}"
    names = {p.name for p in files}
    assert "server.py" in names, f"src/api scan missed the app module: {sorted(names)}"

    import src.api.server  # noqa: F401  (populates sys.modules)

    scanned = {p.resolve() for p in files}
    loaded = set()
    for mod_name, module in list(sys.modules.items()):
        if mod_name != "src.api" and not mod_name.startswith("src.api."):
            continue
        filename = getattr(module, "__file__", None)
        if filename:
            loaded.add(Path(filename).resolve())
    missing = sorted(str(p) for p in loaded - scanned)
    assert not missing, (
        f"the API package imports modules the structural scan never reads, so they are unguarded: {missing}"
    )


# ---------------------------------------------------------------------------
# 1. Every registered route is GET/HEAD/OPTIONS only.
# ---------------------------------------------------------------------------


def test_api_routes_are_get_only():
    from src.api.server import app

    checked_any = False
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        checked_any = True
        assert methods <= {"GET", "HEAD", "OPTIONS"}, (
            f"route {getattr(route, 'path', route)!r} allows non-safe methods: {methods}"
        )
    assert checked_any, "expected at least one route with a .methods attribute to check"


# ---------------------------------------------------------------------------
# 2. No write-capable broker method is ever referenced as an attribute
#    access anywhere in src/api/ source (AST-level, not substring).
# ---------------------------------------------------------------------------

_FORBIDDEN_BROKER_ATTRS = {
    "submit_order",
    "cancel_order",
    "cancel_order_by_id",
    "cancel_open_orders",
    "cancel_protective_stops",
    "cancel_snapshotted_stops",
    "cancel_open_entry_orders",
    "close_position",
    "place_entry_protection",
    "_restore_stop_orders",
    "_finalize_pending_protections",
    # Independent Stage 2 review (2026-08-09): both write-capable (cancel +
    # resubmit a stop order) and originally missing from this denylist —
    # neither is referenced anywhere in src/api/ today, but a future
    # "current effective stop" read handler could call one of these without
    # this test catching it.
    "shift_stops_down",
    "replace_stop_loss",
}


@pytest.mark.parametrize("path", _api_source_files(), ids=lambda p: p.name)
def test_no_write_capable_broker_calls_in_api_source(path: Path):
    tree = _parse(path)
    found_attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    offenders = found_attrs & _FORBIDDEN_BROKER_ATTRS
    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} references forbidden write-capable "
        f"broker attribute(s) as an ast.Attribute node: {offenders}"
    )


# ---------------------------------------------------------------------------
# 3. AlpacaBroker is constructed in exactly one place: broker_reads.py's
#    _get_broker(). No other file under src/api/ instantiates it.
# ---------------------------------------------------------------------------


def _call_target_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_alpaca_broker_constructed_only_in_broker_reads():
    construction_sites: list[Path] = []
    for path in _api_source_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_target_name(node) == "AlpacaBroker":
                construction_sites.append(path)
                break
    assert construction_sites == [API_DIR / "broker_reads.py"], (
        f"AlpacaBroker(...) constructed outside broker_reads.py: "
        f"{[p.relative_to(REPO_ROOT) for p in construction_sites]}"
    )


# ---------------------------------------------------------------------------
# 4. Trading-critical modules must never import src.api (that direction
#    would be a real coupling hazard: the API process is not supposed to be
#    load-bearing for trading).
# ---------------------------------------------------------------------------

# The set is DERIVED, not retyped. Board item 210 splits `src/pipeline.py`
# into one module per cluster over several steps, and a hand-written list
# silently stopped covering the moved code at every step: the guard kept
# passing while the module it existed to watch was no longer named here.
#
# THE RULE: a fixed core (the entrypoint, the broker, the risk rules, the
# scheduler) UNION every `src/pipeline*.py` module. The split's own naming
# convention is what makes the second half honest -- every step of
# docs/PIPELINE_SPLIT_PLAN.md lands its new module under that name, so a
# module created by a future step is covered the moment it exists, with no
# edit here.
#
# WHAT THE RULE WOULD MISS: a cluster moved OUT of the `src/pipeline*.py`
# namespace (say to `src/protection/stops.py`). That gap is closed by
# `test_every_trading_pipeline_base_module_is_covered` below, which walks the
# live MRO of `TradingPipeline` and fails if any class it is composed from
# lives in a file this list does not contain. The remaining blind spot is a
# moved cluster that is NEITHER named `pipeline*` NOR a base of
# `TradingPipeline` -- plain helper functions in a new namespace. Nothing
# mechanical catches that; the reviewer of that step must add it here.

# The trading-critical entry points, named as IMPORTABLE MODULES rather than
# as file paths. Each is resolved to its file through the import system, so
# moving a module's file on disk cannot turn this guard into a no-op pointing
# at a path that no longer exists.
_ALWAYS_TRADING_CRITICAL_MODULES = (
    "main",
    "src.execution.broker",
    "src.risk.rules",
    "src.scheduler",
)


def _resolve_module_paths(dotted_names) -> list[str]:
    """Repo-relative paths of the given modules, resolved by importing them."""
    import importlib

    root = REPO_ROOT.resolve()
    out: list[str] = []
    for dotted in dotted_names:
        module = importlib.import_module(dotted)
        filename = getattr(module, "__file__", None)
        assert filename, f"{dotted} has no __file__ to guard"
        path = Path(filename).resolve()
        assert root in path.parents, f"{dotted} resolved outside the repo: {path}"
        out.append(str(path.relative_to(root)))
    return out


_ALWAYS_TRADING_CRITICAL = tuple(_resolve_module_paths(_ALWAYS_TRADING_CRITICAL_MODULES))


def test_trading_critical_modules_all_resolve():
    """Canary: every named entry point still imports and still lives in-repo."""
    assert len(_ALWAYS_TRADING_CRITICAL) == len(_ALWAYS_TRADING_CRITICAL_MODULES)
    for rel in _ALWAYS_TRADING_CRITICAL:
        assert (REPO_ROOT / rel).is_file(), f"resolved path does not exist: {rel}"


def _pipeline_split_modules() -> list[str]:
    """Every `src/pipeline*.py` module, as a repo-relative path string."""
    return [f"src/{path.name}" for path in sorted((REPO_ROOT / "src").glob("pipeline*.py"))]


def _trading_critical_files() -> list[str]:
    return sorted(set(_ALWAYS_TRADING_CRITICAL) | set(_pipeline_split_modules()))


_TRADING_CRITICAL_FILES = _trading_critical_files()


def test_trading_critical_set_is_not_silently_empty():
    """The derivation must actually find the split modules, not quietly yield none."""
    split = _pipeline_split_modules()
    assert "src/pipeline.py" in split
    assert len(split) >= 2, f"the pipeline split produced extra modules but the glob found none of them: {split}"
    for rel in _ALWAYS_TRADING_CRITICAL:
        assert rel in _TRADING_CRITICAL_FILES


def test_every_trading_pipeline_base_module_is_covered():
    """Every module `TradingPipeline` is composed from must be on the list.

    This is the half of the rule that does not depend on a filename: if a
    future split step puts a mixin somewhere outside `src/pipeline*.py`, the
    glob misses it and this test says so by name.
    """
    import sys

    from src.pipeline import TradingPipeline

    root = REPO_ROOT.resolve()
    missing: list[str] = []
    for klass in TradingPipeline.__mro__:
        if klass is object:
            continue
        module = sys.modules.get(klass.__module__)
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if root not in path.parents:
            continue
        rel = str(path.relative_to(root))
        if rel not in _TRADING_CRITICAL_FILES:
            missing.append(rel)
    assert not missing, (
        "TradingPipeline is composed from modules that the trading-critical "
        f"import guard does not watch: {sorted(set(missing))}"
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def _api_imports(tree: ast.AST) -> list[str]:
    """Imports of the API layer found in `tree`, in source order."""
    return [name for name in _imported_module_names(tree) if name == "src.api" or name.startswith("src.api.")]


@pytest.mark.parametrize("rel_path", _TRADING_CRITICAL_FILES)
def test_api_package_never_imports_trading_critical_modules(rel_path: str):
    path = REPO_ROOT / rel_path
    assert path.is_file(), f"expected trading-critical file to exist: {path}"
    tree = _parse(path)
    offenders = _api_imports(tree)
    assert not offenders, f"{rel_path} imports src.api, which trading-critical code must never depend on: {offenders}"


def test_trading_critical_import_guard_can_fail():
    """Prove the guard above can FAIL -- a guard that cannot fail is not a guard.

    The check is run against the real source text of each derived split module
    with an `import src.api` appended, so a regression in the detector (not
    just in the list) is caught too.
    """
    assert _api_imports(ast.parse("from src.api.server import app\n")) == ["src.api.server"]
    for rel_path in _pipeline_split_modules():
        source = (REPO_ROOT / rel_path).read_text() + "\nimport src.api  # injected\n"
        assert _api_imports(ast.parse(source)) == ["src.api"], (
            f"the guard would not notice {rel_path} importing src.api"
        )


# ---------------------------------------------------------------------------
# 5. src/api/ never imports the pipeline/risk stack, and never imports the
#    write-capable Database class from src.storage.db.
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_PREFIXES = ("src.pipeline", "src.pipeline_stages", "src.risk")


@pytest.mark.parametrize("path", _api_source_files(), ids=lambda p: p.name)
def test_api_source_files_never_import_pipeline_or_risk(path: Path):
    tree = _parse(path)
    imported = _imported_module_names(tree)
    offenders = [
        m for m in imported if any(m == prefix or m.startswith(prefix + ".") for prefix in _FORBIDDEN_IMPORT_PREFIXES)
    ]
    assert not offenders, f"{path.relative_to(REPO_ROOT)} imports forbidden pipeline/risk module(s): {offenders}"

    # Sibling assertion, same test: no file under src/api/ may import the
    # read/WRITE Database class from src.storage.db (db_reads.py is
    # supposed to open its own independent mode=ro connection instead).
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.storage.db":
            imported_names = {alias.name for alias in node.names}
            assert "Database" not in imported_names, (
                f"{path.relative_to(REPO_ROOT)} imports src.storage.db.Database (the write-capable class) directly"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "src.storage.db", (
                    f"{path.relative_to(REPO_ROOT)} does `import src.storage.db` directly"
                )


# ---------------------------------------------------------------------------
# 6. No SQL write statement is ever passed to conn.execute(...) in
#    db_reads.py — every execute() call's literal SQL argument must be a
#    SELECT (checked via AST on the string/f-string literal, not a raw
#    substring scan of the whole file, so the module's own docstring
#    prose about INSERT/UPDATE/DELETE/commit() can't cause a false
#    positive or a false negative).
# ---------------------------------------------------------------------------


def _literal_sql_text(node: ast.AST) -> str | None:
    """Best-effort reconstruction of the literal text of a string/f-string
    argument, ignoring any interpolated `{where}`-style expressions (those
    are always parameter-free structural fragments built from constants
    in this module, never externally-controlled write SQL)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return "".join(parts)
    return None


def test_no_sql_write_statements_in_db_reads():
    path = API_DIR / "db_reads.py"
    tree = _parse(path)

    execute_calls = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            sql_text = _literal_sql_text(node.args[0])
            if sql_text is None:
                continue
            execute_calls += 1
            stripped = sql_text.strip()
            assert stripped.upper().startswith("SELECT") or stripped.upper().startswith("PRAGMA"), (
                f"conn.execute(...) call in db_reads.py does not start with SELECT/PRAGMA: {sql_text!r}"
            )
    assert execute_calls > 0, "expected to find at least one conn.execute(...) call to check"

    # Belt-and-suspenders: also confirm no .commit() call site exists at all.
    commit_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "commit"]
    assert not commit_calls, "db_reads.py should never call .commit()"


# ---------------------------------------------------------------------------
# 7. db_reads.py never imports the shared write-capable Database class.
#    (Explicit, narrowly-named duplicate of part of test 5, kept because
#    it documents the specific invariant db_reads.py's own docstring calls
#    out by name.)
# ---------------------------------------------------------------------------


def test_no_db_write_calls_via_shared_database_class():
    path = API_DIR / "db_reads.py"
    tree = _parse(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.storage.db":
            imported_names = {alias.name for alias in node.names}
            assert "Database" not in imported_names
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "src.storage.db"


# ---------------------------------------------------------------------------
# 8. db_reads.py's connection helper actually uses the read-only SQLite URI
#    mode, not just a plan mentioned in a docstring.
# ---------------------------------------------------------------------------


def test_read_only_sqlite_connection_actually_refuses_writes(tmp_path, monkeypatch):
    """Execute the helper instead of reading it: a write must RAISE.

    Previously this grepped `db_reads.py` for the string `mode=ro`, which both
    hard-coded the filename and proved nothing about the connection. Now the
    real helper opens a real database and SQLite itself is asked to reject the
    write, which is the property the API layer depends on.
    """
    import sqlite3

    from src.api import db_reads

    db_path = tmp_path / "ro_probe.sqlite3"
    seed = sqlite3.connect(db_path)
    seed.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, v TEXT)")
    seed.execute("INSERT INTO probe (v) VALUES ('seeded')")
    seed.commit()
    seed.close()

    monkeypatch.setattr(db_reads, "get_db_path", lambda: db_path)
    conn = db_reads._connect()
    try:
        assert conn.execute("SELECT v FROM probe").fetchone()[0] == "seeded", (
            "the read-only connection could not even read the seeded row"
        )
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            conn.execute("INSERT INTO probe (v) VALUES ('written')")
            conn.commit()
        assert "readonly" in str(excinfo.value).lower(), f"write failed for the wrong reason: {excinfo.value}"
    finally:
        conn.close()

    check = sqlite3.connect(db_path)
    rows = check.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    check.close()
    assert rows == 1, "the API's read-only connection managed to write a row"


# ---------------------------------------------------------------------------
# 9. Behavioral smoke test: the app-level GET-only middleware actually
#    rejects non-safe methods with 405, before any route handler (and thus
#    before any config/DB access) runs.
# ---------------------------------------------------------------------------


def test_server_has_get_only_enforcement_middleware():
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)

    assert client.post("/health").status_code == 405
    assert client.put("/account").status_code == 405
    assert client.delete("/trades").status_code == 405
    assert client.patch("/positions").status_code == 405


# ---------------------------------------------------------------------------
# 10. Stage 3 cockpit: the `/ui` static mount is covered by the SAME
#     app-level GET-only middleware as the JSON routes above (it wraps the
#     whole ASGI app regardless of mount registration order) and cannot be
#     used to escape `src/api/static/` via path traversal. Pins the
#     behavior verified by hand during the Stage 3 review so a future
#     middleware/mount-ordering change or Starlette upgrade can't silently
#     regress it.
# ---------------------------------------------------------------------------


def test_ui_static_mount_is_get_only_and_has_no_path_traversal():
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)

    assert client.post("/ui/app.js").status_code == 405
    assert client.put("/ui/index.html").status_code == 405
    assert client.delete("/ui/styles.css").status_code == 405

    get_resp = client.get("/ui/index.html")
    assert get_resp.status_code == 200

    traversal_resp = client.get("/ui/../server.py")
    assert traversal_resp.status_code in (403, 404)


def test_cockpit_static_mount_is_get_only_and_has_no_path_traversal():
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)

    assert client.post("/cockpit/app.js").status_code == 405
    assert client.put("/cockpit/index.html").status_code == 405
    assert client.delete("/cockpit/styles.css").status_code == 405

    traversal_resp = client.get("/cockpit/../server.py")
    assert traversal_resp.status_code in (403, 404)


def test_cockpit_static_mount_serves_the_built_index():
    """Needs the built bundle, so it runs in the CI `frontend` job (see _built_cockpit)."""
    _built_cockpit()
    from fastapi.testclient import TestClient

    from src.api.server import app

    assert TestClient(app).get("/cockpit/index.html").status_code == 200


def test_diary_static_mount_is_get_only_and_serves_without_entries():
    """``/diary`` is a read-only StaticFiles mount over gitignored
    ``data/diary/``. An empty or missing folder must still 200 (placeholder
    index), and GET-only middleware must cover it the same way as /ui.
    CI must not require a real diary page to exist.
    """
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)

    assert client.post("/diary/").status_code == 405
    assert client.put("/diary/index.html").status_code == 405
    assert client.delete("/diary/index.html").status_code == 405

    get_slash = client.get("/diary/")
    assert get_slash.status_code == 200
    assert "text/html" in get_slash.headers.get("content-type", "")

    get_bare = client.get("/diary")
    assert get_bare.status_code == 200

    traversal_resp = client.get("/diary/../server.py")
    assert traversal_resp.status_code in (403, 404)


def test_diary_mount_survives_missing_directory(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api import server as api_server

    missing = tmp_path / "no-such-diary"
    assert not missing.exists()
    monkeypatch.setattr("src.api.diary_pages.DIARY_DIR", missing)

    app = api_server.create_app()
    client = TestClient(app)
    resp = client.get("/diary/")
    assert resp.status_code == 200
    assert "No diary entries yet" in resp.text
    assert client.get("/board").status_code == 200
    # /cockpit/ serves build output that exists only after `npm run build`;
    # its 200 is asserted by test_cockpit_static_mount_serves_the_built_index,
    # which the CI `frontend` job runs against a fresh build.
    assert client.get("/ui/index.html").status_code == 200
    assert client.get("/").json()["service"] == "qamc-mission-control-api"


def test_rebuild_desk_diary_index_lists_newest_first(tmp_path):
    from src.api.diary_pages import rebuild_diary_index

    diary = tmp_path / "diary"
    diary.mkdir()
    (diary / "2026-09-01.html").write_text("<h1>older</h1>", encoding="utf-8")
    (diary / "2026-09-15.html").write_text("<h1>newer</h1>", encoding="utf-8")
    (diary / "notes.html").write_text("ignore me", encoding="utf-8")
    (diary / "not-a-date.html").write_text("ignore me too", encoding="utf-8")

    index = rebuild_diary_index(diary)
    text = index.read_text(encoding="utf-8")
    assert "2026-09-15.html" in text
    assert "2026-09-01.html" in text
    assert text.index("2026-09-15") < text.index("2026-09-01")
    assert "notes.html" not in text
    assert "not-a-date.html" not in text


def test_rebuild_desk_diary_index_empty_folder_is_placeholder(tmp_path):
    from src.api.diary_pages import rebuild_diary_index

    diary = tmp_path / "diary"
    index = rebuild_diary_index(diary)
    assert "No diary entries yet" in index.read_text(encoding="utf-8")


def _built_cockpit() -> Path:
    """The dashboard bundle built from frontend/ (build output, not committed).

    Skipped when absent so a plain local run needs no node -- EXCEPT when
    QAMC_REQUIRE_COCKPIT_BUILD=1, which the CI `frontend` job sets after
    building: there an absent bundle FAILS. That job is where these run in CI.
    """
    import os

    cockpit = REPO_ROOT / "src" / "api" / "static_cockpit"
    if not (cockpit / "index.html").is_file():
        if os.environ.get("QAMC_REQUIRE_COCKPIT_BUILD") == "1":
            pytest.fail(f"QAMC_REQUIRE_COCKPIT_BUILD=1 but {cockpit} holds no built index.html")
        pytest.skip("dashboard not built (cd frontend && npm ci && npm run build)")
    return cockpit


def test_cockpit_homepage_links_to_desk_diary():
    """The built /cockpit bundle must expose Desk diary -> /diary/
    so the owner does not have to type the URL.
    """
    cockpit = _built_cockpit()
    blob = []
    for path in cockpit.rglob("*"):
        if path.suffix in {".js", ".html", ".css"}:
            blob.append(path.read_text(encoding="utf-8", errors="ignore"))
    text = "\n".join(blob)
    assert "Desk diary" in text
    assert "/diary/" in text


def test_cockpit_index_html_only_references_built_assets():
    """index.html's /cockpit/assets/ references and the built assets/ folder
    must agree exactly: every reference exists, and no index-*.{js,css}
    bundle sits there unreferenced (a leftover from an older build, which the
    temp-folder-then-swap build in scripts/merge_and_deploy.sh rules out).
    """
    import re

    cockpit = _built_cockpit()
    index_html = (cockpit / "index.html").read_text(encoding="utf-8")

    referenced = set(re.findall(r'(?:src|href)="/cockpit/(assets/[^"]+)"', index_html))
    assert referenced, "expected index.html to reference at least one /cockpit/assets/ file"

    for rel in referenced:
        assert (cockpit / rel).is_file(), f"index.html references {rel!r} but the build did not emit it"

    built_bundle_files = {
        f"assets/{p.name}" for p in (cockpit / "assets").glob("index-*.*") if p.suffix in {".js", ".css"}
    }
    orphaned = built_bundle_files - referenced
    assert not orphaned, f"built but unreferenced cockpit bundle file(s): {sorted(orphaned)}"
