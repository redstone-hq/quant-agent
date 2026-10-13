import os
import sys
from pathlib import Path

import pytest

from tests.network_guard import _no_sockets_leave_the_box, offline_calendars  # noqa: F401,E501

# Add src to path so tests can import from src.*
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def freeze_clock():
    """Freeze the process clock (time-machine, tick=False). Call ``freeze_clock(when)``
    (aware datetime; naive is read as UTC) to start; the returned traveller has
    ``.move_to(when)`` / ``.shift(delta)``. Unlike patching ``trading_calendar.utc_now``,
    this also reaches modules that did ``from ... import utc_now``. Undone at teardown."""
    import datetime as _dt

    import time_machine

    travellers = []

    def _freeze(when):
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        traveller = time_machine.travel(when, tick=False)
        traveller.start()
        travellers.append(traveller)
        return traveller

    yield _freeze
    for t in reversed(travellers):
        t.stop()


def _register_merge_drivers() -> None:
    """Self-heal: register the merge drivers .gitattributes names (idempotent,
    local to this clone and its worktrees). Without it the docs/WORK.md driver
    is silently inert; scripts/check_merge_drivers.py is the loud check."""
    try:
        from scripts import check_merge_drivers as _c

        _c.main(["--install"])
    except Exception:
        pass


_register_merge_drivers()


def _check_interpreter_matches_ci() -> None:
    """Fail loudly, at test-collection time, if the interpreter running this
    session isn't the one CI runs.

    Item 192: CI pins Python via `.python-version` (both `.github/workflows/
    test.yml` jobs read it with `python-version-file`, rather than each
    carrying its own literal). Nothing previously checked that a developer's
    or agent's local interpreter matched it, and the drift was invisible: a
    prompt-drift check once hashed `ast.dump()` of a parsed function, and
    Python 3.12 added a `type_params` field to `FunctionDef`/`AsyncFunctionDef`/
    `ClassDef` that 3.11 doesn't have, so the same unchanged source hashed
    differently under the two interpreters. CI went red, local ran green, and
    two agents produced confident but wrong diagnoses before the version skew
    itself was found.

    This runs unconditionally at collection (module scope, not a fixture) so
    it fires on every local pytest invocation, including a single-file run —
    that is how agents on this repo actually invoke tests, and a fixture only
    a full-suite run would exercise would miss exactly that case. A floor
    check via `pyproject.toml`'s `requires-python` was considered instead:
    it's declarative and nothing evaluates it against the running
    interpreter, so it can't fire and was rejected for that reason.
    """
    pin_file = Path(__file__).resolve().parent.parent / ".python-version"
    try:
        pinned = pin_file.read_text().strip()
    except OSError:
        return  # nothing to check against; don't block tests on this file's absence
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if running != pinned:
        pytest.exit(
            "Local Python is "
            f"{running} (full version {sys.version.split()[0]}) but CI runs "
            f"{pinned}, pinned in .python-version and read by both jobs in "
            ".github/workflows/test.yml. Run tests under Python "
            f"{pinned} instead — a version mismatch here can silently change "
            "behaviour (e.g. Python 3.12 added an AST field 3.11 doesn't "
            "have, which once made an unchanged file hash differently in CI "
            "vs. local and produced two false diagnoses before anyone found "
            "the real cause). See docs/WORK.md item 192.",
            returncode=1,
        )


_check_interpreter_matches_ci()


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch, request):
    """Every test runs in its own tmp cwd so stores with relative data_dir defaults
    (NewsStore/MacroStore → 'data/news', 'data/macro') don't write to the real repo
    during the test suite.

    Agent PROMPT_PATH values are computed from __file__ (absolute), so they're
    unaffected. Tests that need real data/config paths should use explicit absolute
    paths or dedicated fixtures.

    Also disables outbound HTTP by default: `requests.get` raises unless a test
    explicitly re-patches it. This keeps the suite hermetic now that
    `cost_table.estimate_cost` does an on-demand LiteLLM lookup for unfamiliar
    models — without this, a test exercising a not-yet-priced model would make a
    real network call (slow + flaky in CI). Tests that want specific fetch
    behaviour (the refresh_pricing tests) override `requests.get` themselves, and
    that override wins within the test body.
    """
    monkeypatch.chdir(tmp_path)

    import requests

    def _no_network(*args, **kwargs):
        # Journal the attempt BEFORE raising. Raising alone is invisible to a
        # run: a test that CATCHES this error looks exactly like a test that
        # never called out, so "no test reached the network" could never be
        # measured, only assumed (board item 202, criterion one). Set
        # QAMC_NETWORK_JOURNAL to a file path and every blocked attempt lands
        # there with the test that made it, whether or not the test swallows
        # the error. Unset (the default, and CI) this costs one env lookup.
        _journal = os.environ.get("QAMC_NETWORK_JOURNAL")
        if _journal:
            _target = ""
            for _arg in args:
                if isinstance(_arg, str) and "//" in _arg:
                    _target = _arg
                    break
            else:
                _target = str(kwargs.get("url", ""))
            try:
                with open(_journal, "a", encoding="utf-8") as _fh:
                    _fh.write(f"{request.node.nodeid}\t{_target}\n")
            except OSError:
                pass  # journalling must never change what the suite does
        raise requests.ConnectionError(
            "outbound HTTP disabled in tests (conftest autouse); patch requests.get in the test if you need it"
        )

    monkeypatch.setattr(requests, "get", _no_network)

    # `requests.get` is NOT the only way out. Two holes let live traffic
    # escape this guard, and one of them reached Yahoo Finance from CI and
    # failed two tests in tests/test_shorts_stage3.py with HTTP 401 "Invalid
    # Crumb" -- a failure that had nothing to do with the change under test
    # and blocked every open PR (2026-09-30).
    #
    #   * a Session bypasses the module-level function entirely;
    #   * yfinance does not use `requests` at all -- it ships its own
    #     transport on curl_cffi (verified: `yfinance.data` references
    #     curl_cffi and `session.get`, and `requests.Session` zero times).
    #
    # Close both. A test that genuinely needs a response still overrides
    # these itself, exactly as the refresh_pricing tests already override
    # `requests.get`.
    monkeypatch.setattr(requests.Session, "request", _no_network)

    try:
        import curl_cffi.requests as _curl_requests
    except Exception:  # pragma: no cover - absent in a minimal env
        _curl_requests = None
    if _curl_requests is not None:
        for _attr in ("get", "post", "request"):
            if hasattr(_curl_requests, _attr):
                monkeypatch.setattr(_curl_requests, _attr, _no_network)
        _curl_session = getattr(_curl_requests, "Session", None)
        if _curl_session is not None:
            monkeypatch.setattr(_curl_session, "request", _no_network)

    # With the network genuinely closed, `_get_sector` can no longer reach
    # Yahoo, so EVERY symbol resolves to the same unknown sector and the
    # sector/cluster rules fire in tests that are not about sectors at all.
    # Five tests across four files depended on that live fetch without ever
    # saying so (2026-09-30).
    #
    # Default every symbol to its OWN sector, which is what those tests
    # always assumed. A test that is genuinely about sector crowding, or
    # about the lookup FAILING, overrides this with its own monkeypatch --
    # and now has to say so rather than inherit it from whether Yahoo
    # happened to answer.
    # WRAP the real lookup, do not replace it: the ETF map and every other
    # offline branch must keep working, and a test that is ABOUT sector
    # resolution must still see the real answer. Only substitute where the
    # real function would have gone to the network and come back "Unknown".
    try:
        from src.execution import broker as _broker

        _real_get_sector = _broker._get_sector

        def _sector_offline(symbol: str) -> str:
            try:
                resolved = _real_get_sector(symbol)
            except Exception:
                resolved = "Unknown"
            if resolved and resolved != "Unknown":
                return resolved
            return f"sector-{symbol}"

        # A test that is ABOUT the lookup failing needs the real function
        # back. Publish it under a name such a test can restore, so opting
        # out is explicit and greppable rather than a hidden ordering trick.
        _sector_offline.real_get_sector = _real_get_sector
        monkeypatch.setattr(_broker, "_get_sector", _sector_offline)
    except Exception:  # pragma: no cover - module not importable in a stub env
        pass

    # Clear OPENAI_BASE_URL / OPENAI_CA_BUNDLE so a developer who `source .env`'d
    # a relay endpoint into their shell doesn't change client-construction
    # assertions. Tests that exercise relay routing set them via monkeypatch.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_CA_BUNDLE", raising=False)
    # Same reason, one line down the stack: with a key present the morning
    # message goes to openrouter.ai for the balance line (measured 2026-10-01,
    # two tests in test_universe_screen.py). Tests about that line set it.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Most unit tests exercise an agent in isolation with a fully mocked SDK
    # and no real provider request. Production defaults fail closed when an
    # agent lacks the persistent cost circuit; focused breaker tests override
    # this flag to verify that boundary explicitly.
    from src.agents.base import BaseAgent

    monkeypatch.setattr(BaseAgent, "_allow_unmetered_for_tests", True)

    # The alert-channel watchdog writes its durable check history to an
    # ABSOLUTE path (project-root/data/quant_agent.db) — sessions are
    # started by systemd from any working directory, so it cannot be
    # relative and the chdir above does not contain it. Every session now
    # calls it, so without this redirect an ordinary `main.main()` test
    # would create and append to the developer's real database. Tests that
    # care about the contents point it at their own file.
    from src import alert_watchdog

    monkeypatch.setattr(
        alert_watchdog,
        "DB_PATH",
        tmp_path / "watchdog" / "quant_agent.db",
    )

    # `src.agents.base._TOKEN_GOVERNORS` is a module-level singleton per
    # provider domain, shared by every test in the process. `charge()` and
    # `reconcile()` record REAL token counts into its sliding 60s window, so
    # a test that sends a large mocked usage figure (e.g. a chunked batch at
    # 80k+12k tokens) leaves that charge sitting in the window for whatever
    # test runs next. That next test's own `charge()` call then sees the
    # ceiling already breached by a PRIOR test's traffic and genuinely calls
    # `time.sleep` for however long it takes the window to drain — up to the
    # full 60s window. Measured: test_short_response_recovers_the_missing_
    # symbols in test_tech_analyst.py took 1.3s alone and 60.00s (a real
    # sleep, not noise) immediately after
    # test_tech_analyst_chunked_merged_cost_sums_when_model_priced, which
    # charges ~184k tokens against the 150k/min Anthropic ceiling.
    #
    # Clearing every governor's window before each test makes tests
    # independent of run order without changing what the governor is or how
    # it behaves — production still shares one real singleton per process,
    # and tests that assert on it (test_provider_attempt_budget.py,
    # test_token_rate_governor.py) already compare before/after snapshots
    # within a single test, so starting from an empty window changes nothing
    # they check.
    from src.agents.base import _TOKEN_GOVERNORS

    for _governor in _TOKEN_GOVERNORS.values():
        with _governor._lock:
            _governor._events.clear()

    # `src.agents.base._ROUTE_BREAKERS` is a process-wide dict of half-open
    # provider breakers (see `RouteBreaker`). Production wants exactly that:
    # every seat sharing one Google key must share one view of whether that
    # key is in a cooldown. Tests do not — a case that demotes a provider
    # would otherwise make the NEXT test's first call skip its primary and
    # fail for a reason that has nothing to do with what it is testing.
    # Clearing the registry before each test restores per-test independence
    # without changing the breaker's own behaviour.
    from src.agents.base import _reset_route_breakers_for_tests

    _reset_route_breakers_for_tests()

    # The route journal memoises which DB path it created its schema on, and
    # counts write failures per process. Both are process-global; reset them
    # so a test that repoints QUANT_AGENT_DB_PATH gets a real schema and a
    # clean failure count.
    from src import llm_route_journal

    llm_route_journal._reset_schema_cache_for_tests()
    monkeypatch.setenv("QUANT_AGENT_DB_PATH", str(tmp_path / "route" / "quant_agent.db"))
    (tmp_path / "route").mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _isolate_alerting_state(tmp_path, monkeypatch):
    """Point the alerting state files at this test's tmp dir.

    `src/coverage_watchdog.py` keeps per-symbol, per-trading-day alert claims
    in `data/alerting/coverage_heartbeat.json` under ABSOLUTE paths computed
    from `__file__`, so `_isolate_cwd` above does not cover them. Once
    `TradingPipeline._alert_owner_no_stop` started claiming on that file,
    the first test to escalate a naked position burned the claim for the
    symbol for the whole trading day and every later test -- and every later
    RUN that same day -- got silence instead of the alert it asserted. Four
    tests in tests/test_fractional_sizing.py failed that way, with the
    CRITICAL "NO STOP AT ALL" log present and `send_owner_alert` never
    called: real suppression logic, fed by state the suite never meant to
    share.

    This is a hermeticity fix, not a relaxation: the production dedup is
    unchanged and still suppresses a same-day repeat for the same symbol.
    An autouse fixture rather than another per-test monkeypatch because the
    per-test pattern is exactly what was forgotten here; tests that patch
    these paths themselves still win inside their own body.

    It also stops the suite writing into THIS checkout's `data/alerting/`.
    That is a development-checkout concern only: `STATE_PATH` is built from
    `Path(__file__).resolve().parent.parent`, so it is relative to whichever
    checkout the module is imported from. Production runs from its own
    checkout with its own `data/alerting/`, and no live desk alert claim was
    ever reachable from running this suite.
    """
    alerting = tmp_path / "alerting"
    alerting.mkdir(exist_ok=True)
    import src.coverage_watchdog as _cw
    import src.drift_state as _ds

    heartbeat = alerting / "coverage_heartbeat.json"
    drift = alerting / "deploy_drift.json"
    import src.alert_claims as _claims

    # `load_state`/`save_state` live in `src.alert_claims` and resolve the
    # default path from THAT module's globals; the watchdog only re-exports.
    monkeypatch.setattr(_claims, "STATE_PATH", heartbeat)
    monkeypatch.setattr(_cw, "STATE_PATH", heartbeat)
    monkeypatch.setattr(_ds, "DEPLOY_DRIFT_STATE_PATH", drift)

    # The reading side moves with the writing side, or the isolation itself
    # would break the invariant that pins them together
    # (tests/test_alert_suppression_api.py). `src.api.db_reads` spells the
    # paths as literals because it may not import trading modules, so
    # redirecting only the watchdog would leave the endpoint reading this
    # checkout's real files while the writer wrote to tmp — the exact drift
    # that pin exists to catch, introduced by the fixture meant to prevent
    # pollution.
    import src.api.db_reads as _db_reads

    monkeypatch.setattr(_db_reads, "SUPPRESSION_STATE_PATHS", (heartbeat, drift))


@pytest.fixture(autouse=True)
def _the_suite_is_not_the_production_desk(request, monkeypatch):
    """Name the test process honestly for `src.session_identity`.

    Tests routinely chdir into a temporary tree and then open the fill
    socket, which is precisely the shape the identity check refuses for the
    desk: a session writing its data outside its own checkout. Declaring the
    suite's own identity states the truth rather than weakening the check —
    the desk-identity refusal stays fully armed for anything that is the desk.
    """
    module = getattr(request.module, "__name__", "")
    desk = module.endswith("test_credentials") or module.endswith("test_session_identity")
    monkeypatch.setenv("QAMC_SESSION_IDENTITY", "desk" if desk else "pytest")
    # CI exports dummy broker keys for the whole job. Test brokers carry their
    # own fake keys, so the real REST-versus-socket comparison would read that
    # job-level pair as "REST" and refuse every test socket. The suite is not
    # a session with delivered credentials; tests that exercise the comparison
    # set both sides themselves.
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_credit_line_on_alerts(monkeypatch):
    """Tests that are not about credit see the historical payload.

    The public send appends an "AI credit" line (owner ruling 2026-10-12). With
    no network in tests that line would read "could not read" and break every
    exact-payload assert. tests/test_credit_on_every_alert.py sets the switch
    back to True explicitly.
    """
    monkeypatch.setattr("src.notifier.credit_line.APPEND_CREDIT_LINE", False)
