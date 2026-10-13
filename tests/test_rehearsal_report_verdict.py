"""Regression: the rehearsal verdict must not call a healthy session a FAIL.

Additional regression: for intra_check sessions, the nested
result["intraday_scan"]["status"] must be extracted and used as the report
status, not the top-level "ok" status. This mirrors production's
src/trader_feed.py behavior and makes the nested statuses intraday_no_trades
and intraday_executed actually reachable in the rig's reporting.

Found running the rig against real production history (2026-08-29): plain,
uneventful `midday`, `close` and `intra_check` rehearsals — no crash, no
missing recording, no blocked agent, nothing stopped — all came back
`VERDICT: FAIL` because `report.py`'s status vocabulary had not kept up with
`src/pipeline.py`'s. `run_position_review` (shared by midday/close) returns
"reviewed" for both "no positions to review" and "reviewed them successfully"
— only "position_review_parse_error" is the real failure. `run_intra_check`
returns "ok" when there is no loss violation or no positions — a normal,
majority-of-the-time outcome for a session that runs every 30 minutes.
`run_evening` returns "analyzed" on success. Production's own
`src/trader_feed.py` (groups "reviewed", "intraday_no_trades", "no_trades",
"ok") and `src/notifier.py` (groups "executed", "analyzed", "reviewed",
"preprocessed", "reflected") already treat these as healthy completions, not
failures — the rig disagreeing with production about what counts as "the
session worked" is exactly the kind of dishonest output this harness exists
to avoid producing itself.
"""

from __future__ import annotations

import ast
from src.storage.db import Database
from pathlib import Path

from ops.rehearsal.report import RehearsalReport, STATUS_PLAIN, _verdict
from tests.intraday_sources import PART_MODULES

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Derive, from source, every terminal status a SESSIONS-mapped session (plus
# run_earnings_preprocess) can hand back as `report.status` -- see
# test_every_known_pipeline_terminal_status_is_classified below for why this
# exists instead of a hand-maintained list.
#
# A plain `ast.walk` over the whole of src/pipeline.py for every dict literal
# keyed "status" was tried first and rejected: it also matches unrelated
# "status" keys that are never a session's terminal status (an embedded
# sub-report like `smart_money_refresh = {"status": "disabled"}`, or an
# `agent_logs`-write kwargs dict) and it does not resolve indirection at all,
# so it misses real statuses that are not literals at their `"status": ...`
# site. Two kinds of indirection are structural, not incidental, so they are
# bridged explicitly below rather than discovered generically:
#
#   1. A handful of statuses are decided by a ternary at the dict-literal
#      site itself, e.g. `"status": "reviewed" if ... else
#      "position_review_parse_error"` (run_position_review) or the doubly
#      nested one in run_evening ("analyzed" / "evening_analysis_error" /
#      "evening_parse_error"). `_status_literals` below walks into both
#      branches of an `ast.IfExp`, recursively.
#
#   2. A few statuses are constructed in a *different* function/method than
#      the one that returns them to the caller -- the value flows back as a
#      bare `return NAME`, where NAME's dict literal lives elsewhere. Found
#      by walking each scanned function for `return NAME` where NAME was
#      last assigned from a *call* rather than a dict literal (see the
#      `_BRIDGES` list below for exactly which four calls these are and
#      where each one's real dict literal lives). This is the case the
#      previous hand-list's docstring cited for `pm_agent_failure` and the
#      four `pm_*` statuses (three call frames into
#      src/agents/portfolio_manager/__init__.py's `_semantic_failure`) -- true, and
#      handled below -- but the *same* shape also applies to
#      `hard_risk_block` / `agent_failure` / `rejected` / `symbol_block`
#      (RiskStage.run, a different file: src/pipeline_stages.py),
#      and `paid_analysis_suspended` (`_paid_suspended_payload`) -- in
#      pipeline.py but in a shared helper, not at the `return` site.
#
# run_intra_check has its own nested-indirection wrinkle: the opportunity
# scan's outcome is assigned to a local (`scan_result`) that is never itself
# `return`-ed -- it is embedded as `result["intraday_scan"] = scan_result`
# and *that* dict is returned. This mirrors exactly how
# ops/rehearsal/report.py's own `collect()` reads it back (`result.get(
# "intraday_scan")`), so `_names_of_interest` below extends "returned names"
# to include the right-hand side of that one specific subscript assignment.
#
# What this does NOT attempt: a fully generic call graph (following an
# arbitrary `self.foo(...)` to `foo`'s body, anywhere, unbounded). The four
# bridges below were found by inspecting every `return NAME` in the scanned
# functions whose NAME traces to a call rather than a literal (this file's
# derivation script prints them; none were left unresolved as of 2026-08-31).
# A fully generic resolver would also have to disambiguate same-named
# methods on unrelated classes (pipeline_stages.py alone defines four
# different `run` methods) -- attempting that generically risked silently
# resolving to the *wrong* one, which would be a worse failure mode than the
# hand list this replaces. If a future refactor adds a fifth bridge, this
# derivation raises AssertionError (a bare `return NAME` this file's helpers
# do not know how to resolve is *not* silently skipped -- see
# `_assert_no_unresolved_bridges` below), rather than silently under-counting.
def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _all_str_consts(node: ast.AST) -> set[str]:
    """Every string literal `node` could evaluate to, including through a
    (possibly nested) ternary -- several real statuses are written as
    `"a" if cond else "b"` at their dict-literal site."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _all_str_consts(node.body) | _all_str_consts(node.orelse)
    return set()


def _dict_status_values(node: ast.Dict) -> set[str]:
    found: set[str] = set()
    for key, value in zip(node.keys, node.values):
        if _str_const(key) == "status":
            found |= _all_str_consts(value)
    return found


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(
        f"{name!r} not found -- it moved or was renamed; update the function name in this test's derivation"
    )


def _class_str_attr(tree: ast.Module, class_name: str, attr: str) -> str:
    """The string a class-level `ATTR = "..."` assignment is bound to."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                    and item.targets[0].id == attr
                ):
                    value = _str_const(item.value)
                    assert value is not None, (
                        f"{class_name}.{attr} is no longer a plain string "
                        "constant -- this derivation can no longer read the "
                        "status it names"
                    )
                    return value
    raise AssertionError(f"{class_name}.{attr} not found -- it moved or was renamed; update this test's derivation")


def _find_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return item
    raise AssertionError(
        f"{class_name}.{method_name} not found -- it moved or was renamed; update this test's derivation"
    )


def _returned_names(func: ast.FunctionDef) -> set[str]:
    """Names X such that a bare `return X` appears in this function."""
    return {
        node.value.id for node in ast.walk(func) if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    }


def _names_of_interest(func: ast.FunctionDef) -> set[str]:
    interesting = _returned_names(func)
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id in interesting
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "intraday_scan"
                and isinstance(node.value, ast.Name)
            ):
                interesting.add(node.value.id)
    return interesting


def _unresolved_bridges(func: ast.FunctionDef, known_bridge_calls: set[str]) -> list[str]:
    """`return NAME` where NAME's assigned value is a call this derivation
    does not already have an explicit bridge for."""
    interesting = _names_of_interest(func)
    unresolved = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in interesting:
                    call_fn = node.value.func
                    label = call_fn.attr if isinstance(call_fn, ast.Attribute) else getattr(call_fn, "id", "?")
                    if label not in known_bridge_calls:
                        unresolved.append(f"{target.id} = {label}(...) @ line {node.lineno}")
    return unresolved


def _status_literals(func: ast.FunctionDef) -> set[str]:
    interesting = _names_of_interest(func)
    found: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            found |= _dict_status_values(node.value)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in interesting:
                    found |= _dict_status_values(node.value)
        # The `ctx.analysis_failure_status or "pm_agent_failure"` fallback
        # (src/pipeline.py) -- a literal, but not inside a dict at all: it
        # is the right-hand side of an `or` whose left-hand side reads
        # AgentResult.semantic_status by way of ctx.analysis_failure_status.
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(isinstance(v, ast.Attribute) and v.attr == "analysis_failure_status" for v in node.values):
                for v in node.values:
                    s = _str_const(v)
                    if s is not None:
                        found.add(s)
    return found


# The functions/methods this derivation scans directly -- every SESSIONS-
# mapped session entry point, plus run_earnings_preprocess (a real session
# not yet wired into runner.SESSIONS, kept in scope for the same reason the
# old hand list kept it: STATUS_PLAIN is already correct for it and should
# stay that way).
_PIPELINE_SESSION_FUNCTIONS = (
    "run_morning",
    "run_position_review",
    "run_intra_check",
    "_run_intraday_opportunity_scan",
    "_intraday_opportunity_scan_body",
    # `run_evening` is a thin wrapper (2026-09-18) that runs
    # `_run_evening_body` and then persists the result to `evening_reports`
    # so the report can be re-rendered later; every evening status literal
    # lives in the body, so the body is scanned directly — the same shape as
    # run_intra_check / _run_intraday_opportunity_scan above.
    "run_evening",
    "_run_evening_body",
    "run_earnings_preprocess",
    # `run_morning` / `run_position_review` / `run_intra_check` themselves
    # became thin persistence wrappers the same day, for the same reason
    # (2026-09-18 gap sweep — see `Database.save_session_report` /
    # `save_intra_check_report`): every status literal for these three now
    # lives in `_run_morning_body` / `_run_position_review_body` /
    # `_run_intra_check_body`, so the bodies are scanned directly too.
    "_run_morning_body",
    "_run_position_review_body",
    "_run_intra_check_body",
    # `run_earnings_preprocess` became the same shape on 2026-09-23: the
    # wrapper stamps the P&L block's `pnl_unavailable_reason` (this mode
    # genuinely reads no account) and every status literal lives in the
    # body, so the body is scanned directly too.
    "_run_earnings_preprocess_body",
)

# Explicit indirection bridges: (source file, lookup, resolved status set).
# See the derivation's module comment above for why each of these needed a
# named bridge rather than falling out of a generic walk.
_BRIDGE_FUNCTION_NAMES = {
    # The `_halt_on_daily_loss_breach` / `_check_late_breach_and_halt`
    # bridges that used to be here are gone: the account-level loss breaker
    # was removed entirely on 2026-09-20 (retired item 32), so there is no
    # `daily_loss_halted` status left to derive.
    "_risk_stage",
    "run",
    "_paid_suspended_payload",
    # 2026-09-02: the kill switch halts all three sessions from a shared
    # helper rather than at each return site, the same shape as the entries
    # above. Its status dict lives in `_kill_switch_halt_result`.
    "_kill_switch_halt_result",
    # 2026-09-14 (docs/WORK.md item 20): run_morning refuses to DECIDE when a
    # seat's answer never arrived, from a shared helper whose status dict
    # lives in `_evidence_gate_skip` — same shape as the entries above, and
    # bridged below so its status is derived rather than hand-listed.
    "_evidence_gate_skip",
    "failed_scan_result",
}


def _derive_known_pipeline_statuses() -> set[str]:
    pipeline_tree = ast.Module(
        body=[stmt for mod in PART_MODULES for stmt in ast.parse((REPO_ROOT / "src" / mod).read_text()).body],
        type_ignores=[],
    )
    # item 210 step 10 moved RiskStage into src/stage_risk.py.
    stages_tree = ast.parse((REPO_ROOT / "src" / "stage_risk.py").read_text())
    pm_tree = ast.parse((REPO_ROOT / "src" / "agents" / "portfolio_manager" / "__init__.py").read_text())

    # A call to another _PIPELINE_SESSION_FUNCTIONS entry (e.g.
    # run_intra_check calling _run_intraday_opportunity_scan) is not an
    # unresolved bridge — its statuses are already picked up because that
    # function is *also* scanned directly, below.
    known_calls = _BRIDGE_FUNCTION_NAMES | set(_PIPELINE_SESSION_FUNCTIONS)

    statuses: set[str] = set()
    unresolved: list[str] = []
    for fn_name in _PIPELINE_SESSION_FUNCTIONS:
        func = _find_function(pipeline_tree, fn_name)
        statuses |= _status_literals(func)
        unresolved += [f"{fn_name}: {u}" for u in _unresolved_bridges(func, known_calls)]
    assert not unresolved, (
        "this derivation found a return of a name sourced from a call it "
        f"has no bridge for: {unresolved} -- either it is a new terminal "
        "status hiding behind an unresolved indirection (add a bridge "
        "below) or it is unrelated to session status (narrow "
        "_unresolved_bridges' caller instead)"
    )

    # Bridge 1: the PM's semantic failure statuses -- set on
    # AgentResult.semantic_status three call frames from run_morning, by
    # src/agents/portfolio_manager/__init__.py's `_semantic_failure(result, status,
    # error)`. All 11 call sites pass a literal as the status argument.
    for node in ast.walk(pm_tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_semantic_failure"
            and len(node.args) >= 2
        ):
            s = _str_const(node.args[1])
            if s is not None:
                statuses.add(s)

    # Bridge 2: RiskStage.run (src/pipeline_stages.py) -- run_morning's and
    # the intraday scan's own risk stage both `return early_exit` straight
    # from this method's result.
    statuses |= _status_literals(_find_method(stages_tree, "RiskStage", "run"))

    # Bridge 3: a shared helper inside pipeline.py itself, called (and
    # bare-returned) from several session functions.
    statuses |= _status_literals(_find_function(pipeline_tree, "_paid_suspended_payload"))

    # Bridge 5: the item-20 evidence gate — run_morning bare-returns this
    # helper's result when a seat's answer never arrived.
    statuses |= _status_literals(_find_function(pipeline_tree, "_evidence_gate_skip"))

    return statuses


# STATUS_PLAIN entries this derivation has confirmed are genuinely missing,
# tracked here rather than silently causing this test to fail on a defect
# outside the scope of the change that found it.
#
# EMPTY, and that is the point. The one entry it ever held --
# `intraday_analysis_error`, the portfolio manager failing inside the
# intraday opportunity scan, reachable since 2026-08-25 and printing the
# generic "ended with status " fallback to the account owner ever since --
# was found by this derivation rather than by the hand list it replaced, and
# has now been given a real STATUS_PLAIN entry. Nothing is deferred here any
# more.
#
# Keep it empty. `test_every_known_pipeline_terminal_status_is_classified`
# below fails loudly in both directions: a new, untracked gap fails, and an
# entry parked here that has since been fixed also fails as stale. Adding to
# this set is deferring a defect in writing, not fixing one.
_KNOWN_STATUS_PLAIN_GAPS: set[str] = set()


def _report(status: str) -> RehearsalReport:
    return RehearsalReport(
        session="midday",
        rehearsed_date="2026-08-31",
        run_id="r",
        source_run_id=None,
        status=status,
        completed=True,
    )


def test_reviewed_with_no_positions_is_not_a_failure():
    """run_midday / run_close's normal outcome when there is nothing to
    review — not a crash, not a degraded result."""
    assert _verdict(_report("reviewed")) == "PASS"


def test_intra_check_ok_is_not_a_failure():
    """run_intra_check's normal outcome absent a loss-limit breach — the
    common case on a 30-minute cadence, not an error."""
    assert _verdict(_report("ok")) == "PASS"


def test_evening_analyzed_is_not_a_failure():
    assert _verdict(_report("analyzed")) == "PASS"


def test_intraday_no_trades_and_intraday_executed_are_not_failures():
    assert _verdict(_report("intraday_no_trades")) == "PASS"
    assert _verdict(_report("intraday_executed")) == "PASS"


def test_nested_intraday_no_trades_status_is_treated_as_healthy():
    """An intra_check report with the nested intraday_no_trades status
    should be treated as a healthy completion, matching production behavior.
    """
    # The status would be extracted by collect() from the nested result
    report = RehearsalReport(
        session="intra_check",
        rehearsed_date="2026-08-31",
        run_id="r",
        source_run_id=None,
        status="intraday_no_trades",
        completed=True,
    )
    assert _verdict(report) == "PASS"


def test_nested_intraday_executed_status_is_treated_as_healthy():
    """An intra_check report with the nested intraday_executed status
    should be treated as a healthy completion, matching production behavior.
    """
    # The status would be extracted by collect() from the nested result
    report = RehearsalReport(
        session="intra_check",
        rehearsed_date="2026-08-31",
        run_id="r",
        source_run_id=None,
        status="intraday_executed",
        completed=True,
    )
    assert _verdict(report) == "PASS"


def test_genuine_failure_statuses_are_still_fail():
    """The fix must not swallow real failures alongside the healthy ones."""
    for status in (
        "position_review_parse_error",
        "evening_analysis_error",
        "evening_parse_error",
        "broker_error",
        "pm_agent_failure",
        # Giving this one a plain-English entry must not smuggle it into the
        # healthy set: the intraday scan paid for an analysis and got no
        # usable decision back.
        "intraday_analysis_error",
    ):
        assert _verdict(_report(status)) == "FAIL", status


def test_every_newly_recognized_status_has_a_plain_english_entry():
    for status in ("reviewed", "ok", "analyzed", "intraday_no_trades", "intraday_executed"):
        assert status in STATUS_PLAIN
        assert STATUS_PLAIN[status]  # non-empty


def test_intraday_analysis_error_reads_as_english_not_as_a_raw_status():
    """Regression pin for the last tracked STATUS_PLAIN gap.

    `intraday_analysis_error` has been reachable since 2026-08-25 --
    `_intraday_opportunity_scan_body`'s `if not ctx.portfolio_decision`
    branch, the intraday twin of `pm_agent_failure`. It had no STATUS_PLAIN
    entry, so `RehearsalReport.render()` fell through to the generic "The
    session ended with status 'X'." line and the account owner was handed the
    identifier instead of an explanation.

    Asserted through `render()` rather than against the dict, because the
    fallback lives in `render()` and that is where the defect was visible.
    """
    report = _report("intraday_analysis_error")
    report.verdict = _verdict(report)
    rendered = report.render()
    # `render()` hard-wraps the explanation, so compare on collapsed
    # whitespace rather than asserting a sentence survives the line breaks.
    flat = " ".join(rendered.split())

    assert "ended with status" not in flat
    assert " ".join(STATUS_PLAIN["intraday_analysis_error"].split()) in flat
    # It is a failure, and the rendered verdict has to say so.
    assert "VERDICT: FAIL" in rendered


def test_every_known_pipeline_terminal_status_is_classified():
    """Guard against vocabulary drift: every terminal status a session entry
    point in src/pipeline.py can return must be classified by report.py — in
    STATUS_PLAIN, whether as a healthy completion or a named failure — so a
    pipeline status this rig has never seen prints a real explanation
    instead of the generic "The session ended with status 'X'." fallback in
    RehearsalReport.render().

    `known_statuses` used to be a hardcoded set built from a one-time manual
    read of pipeline.py — which meant this test only ever checked that the
    hand list agreed with itself, never that the hand list was actually
    complete. Proof it wasn't: this file's own companion tests
    (`test_intraday_no_trades_and_intraday_executed_are_not_failures` and the
    two `test_nested_intraday_*_is_treated_as_healthy` tests above) already
    exercised `intraday_no_trades` and `intraday_executed` as real,
    already-classified run_intra_check outcomes, while the old
    `known_statuses` below `# run_intra_check` listed only `"ok"`. The
    assertion still passed, because nothing ever checked the list against
    pipeline.py itself.

    `known_statuses` is now derived from src/pipeline.py (plus the two other
    files real statuses hide behind — see the module-level derivation
    helpers and their comments above, `_derive_known_pipeline_statuses` in
    particular) instead of hand-maintained, so a newly added pipeline status
    cannot silently escape this guard the way `intraday_no_trades` /
    `intraday_executed` did.
    """
    from ops.rehearsal import runner

    # If runner.SESSIONS ever grows beyond these five, _PIPELINE_SESSION_
    # FUNCTIONS above needs a matching audit before this test can be trusted
    # again.
    unexpected_sessions = set(runner.SESSIONS) - {
        "morning",
        "midday",
        "close",
        "evening",
        "intra_check",
    }
    assert not unexpected_sessions, (
        f"runner.SESSIONS grew {sorted(unexpected_sessions)} — audit "
        "_PIPELINE_SESSION_FUNCTIONS above against src/pipeline.py before "
        "trusting this test again"
    )

    known_statuses = _derive_known_pipeline_statuses()
    missing = {s for s in known_statuses if s not in STATUS_PLAIN}

    # Pre-existing STATUS_PLAIN gaps this derivation confirmed are real but
    # out of scope for this change (see _KNOWN_STATUS_PLAIN_GAPS above).
    untracked_gaps = sorted(missing - _KNOWN_STATUS_PLAIN_GAPS)
    assert not untracked_gaps, (
        f"{untracked_gaps} would print as a raw, unexplained status to the "
        "account owner — add a STATUS_PLAIN entry (and, if it is a "
        "healthy completion, add it to _verdict's `healthy` set)"
    )
    stale_tracked_gaps = sorted(_KNOWN_STATUS_PLAIN_GAPS - missing)
    assert not stale_tracked_gaps, (
        f"{stale_tracked_gaps} now have STATUS_PLAIN entries — remove them "
        "from _KNOWN_STATUS_PLAIN_GAPS above, they are no longer a gap"
    )


# Integration tests for nested intraday_scan status extraction
# ================================================================


def test_collect_extracts_nested_intraday_no_trades_status():
    """Test that collect() properly extracts the nested intraday_no_trades
    status from result["intraday_scan"]["status"] and uses it as the report
    status, making it reachable in the verdict logic.
    """
    import sqlite3
    import tempfile

    # Create a minimal test database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        _real = Database(db_path)
        _real.initialize()  # the real schema, not a hand-built copy
        _real.close()

        # Now test collect() with nested status
        from ops.rehearsal.report import collect

        result = {
            "status": "ok",  # top-level status
            "intraday_scan": {
                "status": "intraday_no_trades",
                "candidates": ["AAPL"],
                "run_id": "test-run",
            },
        }
        report = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result,
            db_path=db_path,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        assert report.status == "intraday_no_trades"
        assert _verdict(report) == "PASS"
    finally:
        import os

        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_collect_extracts_nested_intraday_executed_status():
    """Test that collect() extracts nested intraday_executed status and
    makes it reachable in the verdict logic.
    """
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        _real = Database(db_path)
        _real.initialize()  # the real schema, not a hand-built copy
        _real.close()

        from ops.rehearsal.report import collect

        result = {
            "status": "ok",  # top-level status
            "intraday_scan": {
                "status": "intraday_executed",
                "candidates": ["AAPL"],
                "orders": [{"symbol": "AAPL", "qty": 10}],
                "run_id": "test-run",
            },
        }
        report = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result,
            db_path=db_path,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        assert report.status == "intraday_executed"
        assert _verdict(report) == "PASS"
    finally:
        import os

        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_collect_ignores_nested_scan_for_non_intra_check_sessions():
    """Non-intra_check sessions should use the top-level status, even if
    an intraday_scan key exists (which would be unusual but possible).
    This ensures backwards compatibility.
    """
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        _real = Database(db_path)
        _real.initialize()  # the real schema, not a hand-built copy
        _real.close()

        from ops.rehearsal.report import collect

        # A morning session with an intraday_scan key (should be ignored)
        result = {
            "status": "no_trades",
            "intraday_scan": {
                "status": "intraday_executed",  # This should be ignored
                "run_id": "test-run",
            },
        }
        report = collect(
            session="morning",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result,
            db_path=db_path,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should use top-level status, not nested
        assert report.status == "no_trades"
        assert _verdict(report) == "PASS"
    finally:
        import os

        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_collect_handles_malformed_nested_intraday_scan():
    """collect() should degrade gracefully when intraday_scan is malformed:
    None, not a dict, or missing the status key.
    """
    import sqlite3
    import tempfile

    def make_db():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name
        _real = Database(db_path)
        _real.initialize()  # the real schema, not a hand-built copy
        _real.close()
        return db_path

    from ops.rehearsal.report import collect
    import os

    # Case 1: intraday_scan is None
    db_path1 = make_db()
    try:
        result1 = {
            "status": "ok",
            "intraday_scan": None,
        }
        report1 = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result1,
            db_path=db_path1,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should fall back to top-level status
        assert report1.status == "ok"
        assert _verdict(report1) == "PASS"
    finally:
        try:
            os.unlink(db_path1)
        except Exception:
            pass

    # Case 2: intraday_scan is a dict but has no status key
    db_path2 = make_db()
    try:
        result2 = {
            "status": "ok",
            "intraday_scan": {
                "candidates": ["AAPL"],
                "run_id": "test-run",
            },
        }
        report2 = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result2,
            db_path=db_path2,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should fall back to top-level status
        assert report2.status == "ok"
        assert _verdict(report2) == "PASS"
    finally:
        try:
            os.unlink(db_path2)
        except Exception:
            pass

    # Case 3: intraday_scan is not a dict
    db_path3 = make_db()
    try:
        result3 = {
            "status": "ok",
            "intraday_scan": "not a dict",
        }
        report3 = collect(
            session="intra_check",
            rehearsed_date="2026-08-31",
            run_id="test-run",
            source_run_id=None,
            result=result3,
            db_path=db_path3,
            library=None,
            trading_stub=None,
            isolation_checks=[],
            unavailable=[],
            network_attempts=[],
            notes=[],
            fill_model="immediate",
            duration_s=0.1,
        )
        # Should fall back to top-level status
        assert report3.status == "ok"
        assert _verdict(report3) == "PASS"
    finally:
        try:
            os.unlink(db_path3)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# INCONCLUSIVE — "the rig could not judge" is not the same claim as "the code
# is broken", and until 2026-09-02 the report made them look identical.
#
# An unpinned rehearsal replayed the morning's decision stage using a
# portfolio-manager answer recorded in a DIFFERENT session (`intra_check-
# d0909ddc`, agent_logs row 312, a real decision about ZS) instead of the
# morning's own row 309. The grounding check compared that decision against a
# morning that never analysed ZS and reported `pm_grounding_error` — printed
# exactly like a genuine defect, and argued about for an hour as if it were
# one. See docs/INCIDENT_HISTORY.md, "the rehearsal rig's verdict was a coin
# flip", and `_replay_fidelity` in ops/rehearsal/report.py.
#
# These tests pin BOTH directions. The downgrade must fire on the transplant,
# and must NOT fire on the same symptom when the replay was faithful — a
# genuine defect being quietly relabelled "inconclusive" would be a strictly
# worse gate than the coin flip it replaced.
# ---------------------------------------------------------------------------


class _FakeLibrary:
    """Just the two attributes `collect()` reads off a ResponseLibrary."""

    def __init__(self, matches):
        self.matches = matches
        self.findings: list[dict] = []


def _grounding_db(tmp_path, *, run_id, analysed=("AAPL", "MSFT")):
    import sqlite3

    path = tmp_path / "sandbox.db"
    _real = Database(str(path))
    _real.initialize()  # the real schema, not a hand-built copy
    _real.close()
    conn = sqlite3.connect(str(path))
    conn.executemany(
        "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope, symbol, "
        "evidence_json) VALUES (?, 'tech_analyst', 'analysis', 'symbol', ?, '{}')",
        [(run_id, symbol) for symbol in analysed],
    )
    conn.commit()
    conn.close()
    return str(path)


def _grounding_report(
    tmp_path,
    *,
    pm_run,
    analyst_run,
    error,
    analysed=("AAPL", "MSFT"),
    source_run_symbols=("ZS",),
):
    import sqlite3

    from ops.rehearsal.report import collect

    run_id = "rehearsal-morning-20260901"
    db_path = _grounding_db(tmp_path, run_id=run_id, analysed=analysed)
    # The recorded run the PM answer came from owns the symbol it decided
    # about — the third leg of the test, and what makes the downgrade an
    # attribution rather than a guess at a capitalised word.
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope, symbol, "
        "evidence_json) VALUES (?, 'tech_analyst', 'analysis', 'symbol', ?, '{}')",
        [(pm_run, symbol) for symbol in source_run_symbols],
    )
    conn.commit()
    conn.close()
    library = _FakeLibrary(
        [
            {
                "agent": "tech_analyst",
                "run_id": analyst_run,
                "row_id": 307,
                "similarity": 0.91,
                "recorded_at": "2026-09-01 13:33:27",
            },
            {
                "agent": "portfolio_manager",
                "run_id": pm_run,
                "row_id": 309,
                "similarity": 0.88,
                "recorded_at": "2026-09-01 13:34:33",
            },
        ]
    )
    return collect(
        session="morning",
        rehearsed_date="2026-09-01",
        run_id=run_id,
        source_run_id=None,
        result={"status": "pm_grounding_error", "error": error},
        db_path=db_path,
        library=library,
        trading_stub=None,
        isolation_checks=[],
        unavailable=[],
        network_attempts=[],
        notes=[],
        fill_model="immediate",
        duration_s=1.0,
    )


def test_a_pm_answer_replayed_from_another_session_is_inconclusive(tmp_path):
    """The 2026-09-02 ZS case. Two recorded sessions stitched into one, and a
    failure naming a symbol this morning never touched: the rig did not
    reproduce the session, so it has not judged the code."""
    report = _grounding_report(
        tmp_path,
        pm_run="intra_check-d0909ddc",
        analyst_run="run-64290730",
        error=(
            "ZS: increase lacks a current-run Technical analysis; ZS: claims technical coverage that does not exist"
        ),
    )
    assert report.verdict == "INCONCLUSIVE"
    rendered = report.render()
    assert "VERDICT: INCONCLUSIVE" in rendered
    # And it must say plainly that this is not a judgement on the code.
    assert "NOT a judgement on the code" in rendered
    assert "ZS" in rendered


def test_the_same_failure_from_one_recorded_run_is_still_a_failure(tmp_path):
    """THE GUARD THAT MATTERS. Identical symptom — a grounding error naming a
    symbol with no coverage — but every seat replayed from the SAME recorded
    morning. Nothing was stitched, so nothing is excused: a portfolio manager
    that invents a ticker is the defect this rig exists to catch."""
    report = _grounding_report(
        tmp_path,
        pm_run="run-64290730",
        analyst_run="run-64290730",
        error=(
            "ZS: increase lacks a current-run Technical analysis; ZS: claims technical coverage that does not exist"
        ),
    )
    assert report.verdict == "FAIL"


def test_a_stitched_replay_is_still_a_failure_when_the_symbol_was_covered(tmp_path):
    """The other half of the conjunction. The replay was imperfect, but the
    failure is about a symbol this session really did analyse, so the code
    genuinely produced an ungrounded plan about something in front of it."""
    report = _grounding_report(
        tmp_path,
        pm_run="intra_check-d0909ddc",
        analyst_run="run-64290730",
        error="AAPL: claims technical coverage that does not exist",
        analysed=("AAPL", "MSFT"),
    )
    assert report.verdict == "FAIL"


def test_a_pinned_rehearsal_can_never_be_downgraded(tmp_path):
    """A pinned replay draws every seat from one run, so the mechanical half
    of the test can never hold. Pinning is the default; the downgrade is
    therefore unreachable on a default invocation, by construction."""
    report = _grounding_report(
        tmp_path,
        pm_run="run-64290730",
        analyst_run="run-64290730",
        error="NVDA: claims macro coverage that does not exist",
    )
    assert report.verdict == "FAIL"


def test_the_report_states_what_it_replayed_and_how_much_it_covered(tmp_path):
    """A reader must not have to take PASS as 'the session was fully
    exercised'. Offline the rig routinely resolves only part of the batch —
    18 to 22 symbols went unresolved on every 2026-09-01 morning run — so the
    pin and the coverage shortfall are printed next to the verdict, not
    buried in the log."""
    from ops.rehearsal.replay import ReplayRunChoice
    from ops.rehearsal.report import collect

    run_id = "rehearsal-morning-20260901"
    db_path = _grounding_db(tmp_path, run_id=run_id, analysed=("AAPL", "MSFT"))
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope, symbol, "
        "evidence_json) VALUES (?, 'pipeline', 'pipeline_event', 'symbol', ?, ?)",
        [
            (
                run_id,
                sym,
                '{"outcome": "blocked", "stage": "deterministic_gate", '
                '"reason": "technical_analysis_unresolved_after_retry"}',
            )
            for sym in ("KO", "HD", "NKE")
        ],
    )
    conn.commit()
    conn.close()

    report = collect(
        session="morning",
        rehearsed_date="2026-09-01",
        run_id=run_id,
        source_run_id="run-64290730",
        result={"status": "no_orders"},
        db_path=db_path,
        library=_FakeLibrary([]),
        trading_stub=None,
        isolation_checks=[],
        unavailable=[],
        network_attempts=[],
        notes=[],
        fill_model="immediate",
        duration_s=1.0,
        replay_choice=ReplayRunChoice(
            run_id="run-64290730",
            mode="auto",
            reason="replay pinned automatically to run-64290730, the most recent complete morning run",
        ),
    )
    assert report.verdict == "PASS"
    assert report.coverage["unresolved"] == 3
    assert report.coverage["analysed"] == 2

    rendered = report.render()
    verdict_at = rendered.index("VERDICT:")
    numbers_at = rendered.index("BY THE NUMBERS")
    pin_at = rendered.index("run-64290730")
    coverage_at = rendered.index("INCOMPLETE ANALYST COVERAGE")
    # Both sit between the verdict and the first body section, i.e. a reader
    # cannot reach the numbers without having passed them.
    assert verdict_at < pin_at < numbers_at
    assert verdict_at < coverage_at < numbers_at
    assert "3 of 5 symbol(s)" in rendered


def test_a_symbol_the_source_run_does_not_own_is_not_a_transplant(tmp_path):
    """The third leg. Two sessions were stitched and the failure names
    something this morning never touched — but the recorded run the decision
    came from does not own it either, so nothing has been shown to have been
    transplanted. Report the failure; do not explain it away."""
    report = _grounding_report(
        tmp_path,
        pm_run="intra_check-d0909ddc",
        analyst_run="run-64290730",
        error="ZS: claims technical coverage that does not exist",
        source_run_symbols=(),
    )
    assert report.verdict == "FAIL"
