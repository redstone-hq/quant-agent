"""Tests for scripts/desk_reset.py — the daily paper-desk reset.

The headline invariant, and the reason this file exists: **the tool must
refuse to liquidate anything that is not provably a paper account.** A
simulated live-account response has to stop the run before a single broker
write is attempted. Everything else here (dry-run default, backup-before-
delete, table selection) protects a paper account from a fat-fingered
morning; the live-account refusal protects real money.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.trading_calendar import sql_utc_stamp
from src import alert_watchdog, llm_route_journal
from src.storage import risk_budget_record
from src.storage.db import Database

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "desk_reset.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("desk_reset_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dr():
    return _load_module()


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
LIVE_ENDPOINT = "https://api.alpaca.markets"


def _account(number="PA3ABCDEF", equity="9825.11", cash="1200.00"):
    return SimpleNamespace(
        account_number=number,
        id="acct-uuid",
        status="ACTIVE",
        cash=cash,
        equity=equity,
        portfolio_value=equity,
        long_market_value="8625.11",
        short_market_value="0",
        buying_power="19650.22",
        non_marginable_buying_power="1200.00",
        multiplier="2",
        pattern_day_trader=False,
    )


def _position(symbol, qty, avg, mv):
    return SimpleNamespace(
        symbol=symbol,
        qty=str(qty),
        side="long",
        avg_entry_price=str(avg),
        current_price=str(avg),
        market_value=str(mv),
        unrealized_pl="0",
    )


def _order(oid, symbol):
    return SimpleNamespace(
        id=oid,
        symbol=symbol,
        side="sell",
        order_type="stop",
        qty="1",
        limit_price=None,
        stop_price="100.00",
        status="new",
        time_in_force="gtc",
        submitted_at="2026-09-01T20:00:00Z",
    )


class FakeClient:
    """Records every mutating call so a test can assert none happened."""

    def __init__(self, *, account=None, positions=None, orders=None, endpoint=PAPER_ENDPOINT, market_open=False):
        self._account = account if account is not None else _account()
        self._positions = list(
            positions
            if positions is not None
            else [
                _position("CMCSA", 18, 26.78, 474.39),
                _position("DIS", 3, 108.09, 318.63),
                _position("MSFT", 1, 491.63, 501.07),
                _position("RSG", 2, 221.26, 444.70),
                _position("SGOV", 75, 100.65, 7530.00),
                _position("V", 1, 380.33, 373.70),
            ]
        )
        self._orders = list(
            orders
            if orders is not None
            else [_order(f"ord-{i}", s) for i, s in enumerate(["CMCSA", "DIS", "MSFT", "RSG", "V"])]
        )
        self._base_url = endpoint
        self._market_open = market_open
        self.writes: list[str] = []

    # --- reads ---
    def get_account(self):
        return self._account

    def get_all_positions(self):
        return list(self._positions)

    def get_orders(self, filter=None):  # noqa: A002 — matches the SDK signature
        return list(self._orders)

    def get_clock(self):
        return SimpleNamespace(is_open=self._market_open)

    # --- writes (must never fire on a refusal) ---
    def close_all_positions(self, cancel_orders=None):
        self.writes.append(f"close_all_positions(cancel_orders={cancel_orders})")
        closed = [
            SimpleNamespace(
                symbol=p.symbol, status=200, order_id=f"liq-{p.symbol}", body=SimpleNamespace(id=f"liq-{p.symbol}")
            )
            for p in self._positions
        ]
        self._positions = []
        self._orders = []
        return closed

    def cancel_orders(self):
        self.writes.append("cancel_orders()")
        n = len(self._orders)
        self._orders = []
        return [SimpleNamespace(id=f"c{i}") for i in range(n)]

    def close_position(self, symbol):
        self.writes.append(f"close_position({symbol})")


# ---------------------------------------------------------------------------
# database fixture
# ---------------------------------------------------------------------------

_EVIDENCE_ROWS = [
    ("tech_analyst", "analysis", 40),
    ("earnings_analyst", "analysis", 30),
    ("macro_analyst", "analysis", 5),
    ("smart_money_analyst", "finding", 7),
    ("smart_money_analyst", "admission", 3),
    ("smart_money_analyst", "scan_summary", 2),
    ("macro_provider", "coverage", 2),
    ("pipeline", "pipeline_event", 60),
    ("portfolio_manager", "target", 9),
    ("portfolio_manager", "proposed_order", 6),
    ("portfolio_manager", "reasoning", 4),
    ("risk_manager", "verdict", 3),
    ("risk_manager", "modification", 2),
    ("position_reviewer", "review_metrics", 5),
    ("technical", "seat_stance", 4),
    ("execution", "execution_skip", 1),
    ("pipeline", "nomination_summary", 1),
    ("somebody_new", "brand_new_kind", 2),  # unrecognised: must be reported
]

EVIDENCE_KEEP_TOTAL = 40 + 30 + 5 + 7 + 3 + 2 + 2  # 89
EVIDENCE_DROP_TOTAL = 60 + 9 + 6 + 4 + 3 + 2 + 5 + 4 + 1 + 1 + 2  # 97


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "quant_agent.db"
    # The REAL schema, not a hand-built imitation: a table the reset tool has
    # never heard of must show up here, not hide behind a fake.
    database = Database(str(p))
    database.initialize()
    database.conn.close()
    conn = sqlite3.connect(p)
    # Created lazily at runtime, not by initialize(): use each module's own DDL.
    alert_watchdog.ensure_schema(conn)
    risk_budget_record.ensure_table(conn)
    conn.executescript(llm_route_journal._SCHEMA)
    for i in range(45):
        conn.execute("INSERT INTO trades(symbol,action,qty,price) VALUES(?,?,?,?)", (f"S{i}", "BUY", 1.0, 10.0))
    for s in ("CMCSA", "DIS", "MSFT", "RSG", "SGOV", "V"):
        conn.execute(
            "INSERT INTO positions(symbol,qty,avg_entry,current_price,market_value,unrealized_pnl) VALUES(?,?,1,1,1,0)",
            (s, 1.0),
        )
    for d in range(12):
        conn.execute(
            "INSERT INTO daily_pnl(date,total_value,daily_pnl,daily_return_pct) VALUES(?,?,0,0)",
            (f"2026-08-{d + 1:02d}", 9900.0),
        )
    for d in range(11):
        conn.execute("INSERT INTO insights(date,lessons) VALUES(?,?)", (f"2026-08-{d + 1:02d}", "lesson"))
    for i in range(137):
        conn.execute(
            "INSERT INTO intraday_evaluations(symbol,run_id,status) VALUES(?,?,?)", (f"S{i}", "r", "evaluated")
        )
    conn.execute(
        "INSERT INTO pending_protection_restores"
        "(symbol,sell_order_id,position_qty_before_sell,specs_json) VALUES('X','o1',1,'[]')"
    )
    conn.execute("INSERT INTO pending_repegs(symbol,old_order_id,new_order_id) VALUES('Y','o2','o3')")
    for agent, kind, n in _EVIDENCE_ROWS:
        for _ in range(n):
            conn.execute(
                "INSERT INTO specialist_evidence(run_id,agent_name,kind,scope,"
                "symbol,evidence_json) VALUES(?,?,?,?,?,?)",
                ("run-1", agent, kind, "symbol", "AAA", "{}"),
            )
    for i in range(332):
        conn.execute(
            "INSERT INTO agent_logs(agent_name,run_id,cost_usd) VALUES(?,?,?)", ("tech_analyst", "run-1", 0.01)
        )
    for i in range(50):
        conn.execute("INSERT INTO alert_channel_checks(checked_at,ok,stage) VALUES('2026-09-01T00:00:00',1,'send')")
    stamp = sql_utc_stamp()
    conn.execute(
        "INSERT INTO llm_budget_days(day,baseline_cost_usd,seeded_at,updated_at) VALUES('2026-09-01', 1.5, ?, ?)",
        (stamp, stamp),
    )
    conn.commit()
    conn.close()
    return p


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


@pytest.fixture
def wired(dr, db_path, monkeypatch):
    """run() with config + broker + live-probe stubbed. Returns a handle."""
    state = SimpleNamespace(client=FakeClient(), probe={"status": 403, "authenticated": False, "error": None})

    cfg = SimpleNamespace(
        alpaca=SimpleNamespace(paper=True, base_url="https://paper-api.alpaca.markets"),
        storage=SimpleNamespace(db_path=str(db_path)),
    )
    import src.config as src_config

    monkeypatch.setattr(src_config, "load_config", lambda _p: cfg)

    import alpaca.trading.client as ac

    monkeypatch.setattr(
        ac,
        "TradingClient",
        lambda *a, **k: state.client,
    )
    monkeypatch.setattr(dr, "probe_live_endpoint", lambda *a, **k: state.probe)
    monkeypatch.setenv("ALPACA_API_KEY", "PKTESTKEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "sekrit")
    # No .env in the tmp cwd; keep the loader from finding the real one.
    monkeypatch.setattr(dr, "_load_env_file", lambda: None)
    state.cfg = cfg
    state.db_path = db_path
    return state


def _base_argv(db_path, backup_root):
    # --allow-session-window keeps the suite deterministic: the desk's session
    # windows are wall-clock ET, so without it these tests would pass or fail
    # depending on what time of day CI runs. The window guardrail itself is
    # tested directly against a frozen clock further down.
    return [
        "--config",
        "/does/not/matter.yaml",
        "--db",
        str(db_path),
        "--backup-root",
        str(backup_root),
        "--allow-session-window",
        # The tests above the keep-history section exercise the old delete path, now opt-in.
        "--delete-history",
    ]


# ===========================================================================
# 1. THE LIVE-ACCOUNT GUARDRAIL
# ===========================================================================


def test_paper_account_passes_every_check(dr):
    passed = dr.assert_paper_account(
        config_paper=True,
        config_base_url="https://paper-api.alpaca.markets",
        endpoint=PAPER_ENDPOINT,
        account={"account_number": "PA3ABCDEF"},
        live_probe={"status": 403, "authenticated": False, "error": None},
    )
    assert len(passed) == 5


def test_refuses_when_config_paper_flag_is_false(dr):
    with pytest.raises(dr.ResetRefused) as exc:
        dr.assert_paper_account(
            config_paper=False,
            config_base_url="https://paper-api.alpaca.markets",
            endpoint=PAPER_ENDPOINT,
            account={"account_number": "PA3ABCDEF"},
        )
    assert "alpaca.paper is False" in str(exc.value)


def test_refuses_when_config_base_url_is_the_live_host(dr):
    with pytest.raises(dr.ResetRefused) as exc:
        dr.assert_paper_account(
            config_paper=True,
            config_base_url="https://api.alpaca.markets",
            endpoint=PAPER_ENDPOINT,
            account={"account_number": "PA3ABCDEF"},
        )
    assert "LIVE host" in str(exc.value)


def test_refuses_when_the_client_resolves_to_the_live_endpoint(dr):
    """A client built with paper=False, or url_override'd, never gets through."""
    with pytest.raises(dr.ResetRefused) as exc:
        dr.assert_paper_account(
            config_paper=True,
            config_base_url="https://paper-api.alpaca.markets",
            endpoint=LIVE_ENDPOINT,
            account={"account_number": "PA3ABCDEF"},
        )
    assert "api.alpaca.markets" in str(exc.value)
    assert "refusing" in str(exc.value).lower()


def test_refuses_when_the_account_itself_is_live_shaped(dr):
    """A live Alpaca account number has no PA prefix — that is the account's
    own answer to 'am I paper?', and the only one GET /v2/account offers."""
    with pytest.raises(dr.ResetRefused) as exc:
        dr.assert_paper_account(
            config_paper=True,
            config_base_url="https://paper-api.alpaca.markets",
            endpoint=PAPER_ENDPOINT,
            account={"account_number": "927451638"},
        )
    assert "927451638" in str(exc.value)


def test_refuses_when_the_broker_returns_no_account_number(dr):
    with pytest.raises(dr.ResetRefused) as exc:
        dr.assert_paper_account(
            config_paper=True,
            config_base_url="https://paper-api.alpaca.markets",
            endpoint=PAPER_ENDPOINT,
            account={"account_number": ""},
        )
    assert "no account_number" in str(exc.value)


def test_refuses_when_credentials_authenticate_against_the_live_host(dr):
    """Config and endpoint both say paper, but the KEYS are live keys."""
    with pytest.raises(dr.ResetRefused) as exc:
        dr.assert_paper_account(
            config_paper=True,
            config_base_url="https://paper-api.alpaca.markets",
            endpoint=PAPER_ENDPOINT,
            account={"account_number": "PA3ABCDEF"},
            live_probe={"status": 200, "authenticated": True, "error": None},
        )
    assert "LIVE credentials" in str(exc.value)


def test_an_unreachable_live_probe_is_inconclusive_not_a_refusal(dr):
    passed = dr.assert_paper_account(
        config_paper=True,
        config_base_url="https://paper-api.alpaca.markets",
        endpoint=PAPER_ENDPOINT,
        account={"account_number": "PA3ABCDEF"},
        live_probe={"status": None, "authenticated": False, "error": "timeout"},
    )
    assert any("inconclusive" in p for p in passed)


def test_refusal_lists_every_failing_signal_not_just_the_first(dr):
    with pytest.raises(dr.ResetRefused) as exc:
        dr.assert_paper_account(
            config_paper=False,
            config_base_url="https://api.alpaca.markets",
            endpoint=LIVE_ENDPOINT,
            account={"account_number": "927451638"},
            live_probe={"status": 200, "authenticated": True, "error": None},
        )
    msg = str(exc.value)
    assert msg.count("  - ") == 5


def test_unreadable_endpoint_refuses_rather_than_guessing(dr):
    with pytest.raises(dr.ResetRefused):
        dr.assert_paper_account(
            config_paper=True,
            config_base_url="https://paper-api.alpaca.markets",
            endpoint="",
            account={"account_number": "PA3ABCDEF"},
        )


def test_probe_live_endpoint_reports_authentication(dr, monkeypatch):
    import requests

    calls = {}

    def fake_get(url, headers=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests, "get", fake_get)
    out = dr.probe_live_endpoint("k", "s")
    assert out == {"status": 200, "authenticated": True, "error": None}
    assert calls["url"] == "https://api.alpaca.markets/v2/account"
    assert calls["headers"]["APCA-API-KEY-ID"] == "k"


def test_probe_live_endpoint_swallows_network_errors(dr, monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "get", boom)
    out = dr.probe_live_endpoint("k", "s")
    assert out["authenticated"] is False
    assert out["status"] is None
    assert "no route" in out["error"]


def test_resolve_endpoint_reads_the_real_sdk_client(dr):
    """The guardrail's endpoint check must reflect what alpaca-py will hit."""
    from alpaca.trading.client import TradingClient

    assert dr.resolve_endpoint(TradingClient("k", "s", paper=True)) == PAPER_ENDPOINT
    assert dr.resolve_endpoint(TradingClient("k", "s", paper=False)) == LIVE_ENDPOINT


def test_live_account_refusal_places_no_broker_calls_and_leaves_db_intact(
    dr,
    wired,
    tmp_path,
):
    """END TO END: the broker reports a LIVE-shaped account.

    The run must abort with a non-zero code, must not have issued a single
    liquidation or cancellation, and must not have deleted one database row
    or created a backup directory.
    """
    wired.client = FakeClient(account=_account(number="927451638"))
    import alpaca.trading.client as ac

    ac.TradingClient = lambda *a, **k: wired.client  # re-point after mutation

    before = _counts(wired.db_path)
    backup_root = tmp_path / "resets"
    rc = dr.run(_base_argv(wired.db_path, backup_root) + ["--execute", "--owner-approved", "--yes"])

    assert rc == 3
    assert wired.client.writes == []
    assert _counts(wired.db_path) == before
    assert not backup_root.exists()


def test_live_endpoint_refusal_places_no_broker_calls(dr, wired, tmp_path):
    """Same, but the account looks fine and the ENDPOINT is live."""
    wired.client = FakeClient(endpoint=LIVE_ENDPOINT)
    import alpaca.trading.client as ac

    ac.TradingClient = lambda *a, **k: wired.client

    before = _counts(wired.db_path)
    rc = dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes"])
    assert rc == 3
    assert wired.client.writes == []
    assert _counts(wired.db_path) == before


# ===========================================================================
# 2. DRY RUN IS THE DEFAULT
# ===========================================================================


def test_dry_run_is_the_default_and_changes_nothing(dr, wired, tmp_path, capsys):
    before = _counts(wired.db_path)
    backup_root = tmp_path / "resets"

    rc = dr.run(_base_argv(wired.db_path, backup_root))

    assert rc == 0
    assert wired.client.writes == []  # no broker writes
    assert _counts(wired.db_path) == before  # no rows deleted
    assert not backup_root.exists()  # no backup written either

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Re-run with --execute" in out


def test_dry_run_prints_every_position_order_and_table(dr, wired, tmp_path, capsys):
    dr.run(_base_argv(wired.db_path, tmp_path / "resets"))
    out = capsys.readouterr().out

    for symbol in ("CMCSA", "DIS", "MSFT", "RSG", "SGOV", "V"):
        assert symbol in out
    assert "OPEN ORDERS TO CANCEL (5)" in out
    assert "POSITIONS TO CLOSE (6)" in out
    for table in dr.CLEAR_TABLES:
        assert f"CLEAR  {table}" in out
    assert "45 rows" in out  # trades row count


def test_execute_without_a_tty_requires_yes(dr, wired, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    before = _counts(wired.db_path)
    rc = dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved"])
    assert rc == 2
    assert wired.client.writes == []
    assert _counts(wired.db_path) == before


# ===========================================================================
# 3. BACKUP
# ===========================================================================


def test_backup_database_produces_a_readable_identical_copy(dr, db_path, tmp_path):
    dest = tmp_path / "bk" / "copy.db"
    meta = dr.backup_database(db_path, dest)
    assert dest.exists() and meta["bytes"] > 0
    assert _counts(dest) == _counts(db_path)


def test_execute_backs_up_before_deleting(dr, wired, tmp_path):
    """The backup must hold the PRE-reset rows, not the post-reset ones."""
    backup_root = tmp_path / "resets"
    rc = dr.run(_base_argv(wired.db_path, backup_root) + ["--execute", "--owner-approved", "--yes"])
    assert rc == 0

    stamps = list(backup_root.iterdir())
    assert len(stamps) == 1
    backup_db = stamps[0] / "quant_agent.db"
    assert backup_db.exists()

    backed_up = _counts(backup_db)
    assert backed_up["trades"] == 45
    assert backed_up["daily_pnl"] == 12
    assert backed_up["specialist_evidence"] == EVIDENCE_KEEP_TOTAL + EVIDENCE_DROP_TOTAL

    after = _counts(wired.db_path)
    assert after["trades"] == 0


def test_execute_writes_book_snapshots_and_a_manifest(dr, wired, tmp_path):
    backup_root = tmp_path / "resets"
    dr.run(_base_argv(wired.db_path, backup_root) + ["--execute", "--owner-approved", "--yes"])
    stamp_dir = next(iter(backup_root.iterdir()))

    before = json.loads((stamp_dir / "book_before.json").read_text())
    after = json.loads((stamp_dir / "book_after.json").read_text())
    manifest = json.loads((stamp_dir / "reset_manifest.json").read_text())

    assert len(before["positions"]) == 6
    assert len(before["orders"]) == 5
    assert after["positions"] == []
    assert manifest["deleted"]["trades"] == 45
    assert manifest["evidence_mode"] == "analysis-only"
    assert manifest["db_backup"]["bytes"] > 0


def test_backup_directory_is_timestamped(dr):
    stamp = dr._utc_stamp()
    assert len(stamp) == 16 and stamp.endswith("Z") and "T" in stamp


# ===========================================================================
# 4. TABLE SELECTION
# ===========================================================================


def test_policy_covers_clear_and_keep_without_overlap(dr):
    assert not set(dr.CLEAR_TABLES) & set(dr.KEEP_TABLES)
    assert dr.TABLE_POLICY[dr.EVIDENCE_TABLE] == "partial"
    # The tables that must never be cleared, spelled out so a future edit
    # that moves one of them trips this test.
    for kept in ("agent_logs", "llm_budget_days", "llm_circuit_state", "llm_quota_holds", "alert_channel_checks"):
        assert dr.TABLE_POLICY[kept] == "keep"
    for cleared in ("trades", "positions", "daily_pnl", "insights"):
        assert dr.TABLE_POLICY[cleared] == "clear"


def test_plan_classifies_every_table(dr, db_path):
    conn = dr._connect(db_path, read_only=True)
    try:
        plan = dr.plan_database(conn, evidence_mode="analysis-only")
    finally:
        conn.close()

    cleared = {e["table"] for e in plan["clear"]}
    kept = {e["table"] for e in plan["keep"]}
    unknown = {e["table"] for e in plan["unknown"]}

    assert cleared == set(dr.CLEAR_TABLES)
    assert "agent_logs" in kept and "llm_budget_days" in kept
    assert unknown == set(), f"real tables the reset tool does not classify: {sorted(unknown)}"
    assert dr.EVIDENCE_TABLE not in cleared and dr.EVIDENCE_TABLE not in kept


def _seed_every_table(path, tables):
    """Put 2 rows in each table, filling NOT NULL columns by declared type."""
    conn = sqlite3.connect(path)
    for table in tables:
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info or conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
            continue
        cols = [r for r in info if r[3] or r[5]]  # NOT NULL or part of the primary key
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        for i in range(2):
            vals = []
            for _, cname, ctype, _, _, _ in cols:
                t = (ctype or "").upper()
                allowed = re.search(rf"\b{re.escape(cname)}\s+IN\s*\(([^)]*)\)", ddl)  # honour CHECK ... IN (...)
                if allowed:
                    vals.append(re.findall(r"'([^']*)'", allowed.group(1))[i % 2])
                else:
                    vals.append(i + 1 if "INT" in t else float(i + 1) if t in ("REAL", "NUMERIC") else f"v{i}")
            names = ",".join(f'"{r[1]}"' for r in cols)
            conn.execute(f'INSERT INTO "{table}"({names}) VALUES({",".join("?" * len(cols))})', vals)
    conn.commit()
    conn.close()


def test_seeded_rows_cleared_tables_empty_and_kept_tables_intact(dr, wired, tmp_path):
    present = set(_counts(wired.db_path))
    classified = [t for t in dr.TABLE_POLICY if t in present and dr.TABLE_POLICY[t] in ("clear", "keep")]
    _seed_every_table(wired.db_path, classified)
    before = _counts(wired.db_path)
    rc = dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes"])
    assert rc == 0
    after = _counts(wired.db_path)
    for table in dr.CLEAR_TABLES:
        if table in present:
            assert before[table] > 0, f"{table} was not seeded"
            assert after[table] == 0, f"{table} should be empty"
    for table in dr.KEEP_TABLES:
        if table in present and table != "sqlite_sequence":
            assert before[table] > 0, f"{table} was not seeded"
            assert after[table] == before[table], f"{table} must be intact"


def test_execute_without_owner_approval_is_refused(dr, wired, tmp_path, capsys):
    rc = dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--yes"])
    assert rc == 6
    assert "--owner-approved" in capsys.readouterr().err
    assert _counts(wired.db_path)["trades"] == 45
    assert wired.client.writes == []


@pytest.fixture
def db_with_unknown_table(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE future_table (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO future_table(note) VALUES('do not eat me')")
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.parametrize("execute", [False, True])
def test_unknown_table_refuses_the_run_and_names_it(dr, wired, db_with_unknown_table, tmp_path, capsys, execute):
    argv = _base_argv(wired.db_path, tmp_path / "resets") + (
        ["--execute", "--owner-approved", "--yes"] if execute else []
    )
    rc = dr.run(argv)
    err = capsys.readouterr().err
    assert rc == 5
    assert "future_table" in err
    assert "TABLE_POLICY" in err
    assert _counts(wired.db_path)["trades"] == 45  # nothing touched
    assert _counts(wired.db_path)["future_table"] == 1


def test_clear_database_refuses_an_unclassified_plan(dr, db_with_unknown_table):
    conn = dr._connect(db_with_unknown_table, read_only=False)
    try:
        plan = dr.plan_database(conn, evidence_mode="analysis-only")
        with pytest.raises(dr.ResetRefused, match="future_table"):
            dr.clear_database(conn, plan, evidence_mode="analysis-only")
    finally:
        conn.close()


def test_execute_clears_only_the_allowlist(dr, wired, tmp_path):
    rc = dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes"])
    assert rc == 0
    after = _counts(wired.db_path)

    for table in dr.CLEAR_TABLES:
        assert after[table] == 0, f"{table} should have been cleared"

    # Everything expensive or non-decisional survives, byte for byte.
    assert after["agent_logs"] == 332
    assert after["alert_channel_checks"] == 50
    assert after["llm_budget_days"] == 1
    assert after["llm_circuit_state"] == 1


def test_evidence_analysis_only_keeps_paid_specialist_observation(
    dr,
    wired,
    tmp_path,
):
    dr.run(
        _base_argv(wired.db_path, tmp_path / "resets")
        + ["--execute", "--owner-approved", "--yes", "--evidence", "analysis-only"]
    )

    conn = sqlite3.connect(wired.db_path)
    try:
        kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM specialist_evidence GROUP BY kind"))
    finally:
        conn.close()

    assert kinds == {"analysis": 75, "finding": 7, "admission": 3, "scan_summary": 2, "coverage": 2}
    # every decision-shaped row is gone
    for gone in (
        "target",
        "proposed_order",
        "reasoning",
        "verdict",
        "modification",
        "review_metrics",
        "seat_stance",
        "execution_skip",
        "pipeline_event",
        "nomination_summary",
        "brand_new_kind",
    ):
        assert gone not in kinds


def test_evidence_none_empties_the_table(dr, wired, tmp_path):
    dr.run(
        _base_argv(wired.db_path, tmp_path / "resets")
        + ["--execute", "--owner-approved", "--yes", "--evidence", "none"]
    )
    assert _counts(wired.db_path)["specialist_evidence"] == 0


def test_evidence_all_keeps_the_table_intact(dr, wired, tmp_path):
    dr.run(
        _base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes", "--evidence", "all"]
    )
    assert _counts(wired.db_path)["specialist_evidence"] == EVIDENCE_KEEP_TOTAL + EVIDENCE_DROP_TOTAL


def test_unrecognised_evidence_kind_is_dropped_and_flagged(dr, db_path):
    conn = dr._connect(db_path, read_only=True)
    try:
        ev = dr.plan_evidence(conn, evidence_mode="analysis-only")
    finally:
        conn.close()
    dropped = {d["kind"]: d["reason"] for d in ev["drop"]}
    assert "UNRECOGNISED" in dropped["brand_new_kind"]
    assert ev["keep_rows"] == EVIDENCE_KEEP_TOTAL
    assert ev["drop_rows"] == EVIDENCE_DROP_TOTAL


def test_autoincrement_ids_restart_only_for_fully_emptied_tables(
    dr,
    wired,
    tmp_path,
):
    dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes"])
    conn = sqlite3.connect(wired.db_path)
    try:
        seqs = dict(conn.execute("SELECT name, seq FROM sqlite_sequence"))
    finally:
        conn.close()
    assert "trades" not in seqs  # emptied -> counter reset
    assert seqs.get("agent_logs") == 332  # untouched
    assert "specialist_evidence" in seqs  # only partially cleared


def test_clear_is_atomic(dr, db_path, monkeypatch):
    """A failure mid-clear must leave the database exactly as it was."""
    conn = dr._connect(db_path, read_only=True)
    try:
        plan = dr.plan_database(conn, evidence_mode="analysis-only")
    finally:
        conn.close()
    before = _counts(db_path)

    plan["clear"].append({"table": "no_such_table", "rows": 1, "deleting": 1})
    conn = dr._connect(db_path, read_only=False)
    try:
        with pytest.raises(sqlite3.OperationalError):
            dr.clear_database(conn, plan, evidence_mode="analysis-only")
    finally:
        conn.close()
    assert _counts(db_path) == before


def test_the_tool_never_deletes_files(dr):
    """Structural: no filesystem removal API appears in the script at all."""
    source = SCRIPT.read_text()
    for forbidden in ("shutil.rmtree", "os.remove", "os.unlink", "Path.unlink", ".rmdir(", "DROP TABLE"):
        assert forbidden not in source, f"{forbidden} must not appear"


# ===========================================================================
# 5. MARKET / SESSION-WINDOW GUARDRAIL
# ===========================================================================


def _et(hh, mm, *, weekday=True):
    from datetime import datetime
    from src.trading_calendar import ET

    day = 1 if weekday else 6  # 2026-09-01 Tue, 2026-09-06 Sun
    return datetime(2026, 9, day, hh, mm, tzinfo=ET)


def test_refuses_while_the_market_is_open(dr):
    w = dr.check_trading_window(
        market_open=True,
        allow_market_open=False,
        allow_session_window=True,
        now=_et(18, 30, weekday=False),
    )
    assert w["refusals"] and "market is OPEN" in w["refusals"][0]


def test_allow_market_open_overrides_it(dr):
    w = dr.check_trading_window(
        market_open=True,
        allow_market_open=True,
        allow_session_window=True,
        now=_et(18, 30, weekday=False),
    )
    assert w["refusals"] == []


def test_refuses_inside_a_desk_session_window(dr):
    w = dr.check_trading_window(
        market_open=False,
        allow_market_open=False,
        allow_session_window=False,
        now=_et(20, 30),  # evening window
    )
    assert any("session window" in r for r in w["refusals"])
    assert "evening" in w["active_session_windows"]


def test_allow_session_window_overrides_it(dr):
    w = dr.check_trading_window(
        market_open=False,
        allow_market_open=False,
        allow_session_window=True,
        now=_et(20, 30),
    )
    assert w["refusals"] == []


def test_closed_market_warns_that_liquidations_queue(dr):
    w = dr.check_trading_window(
        market_open=False,
        allow_market_open=False,
        allow_session_window=False,
        now=_et(17, 30),
    )
    assert w["refusals"] == []
    assert any("QUEUED" in x for x in w["warnings"])


def test_fresh_decision_checkpoint_is_warned_about(dr, tmp_path):
    """A checkpoint the resume lane can still act on describes the OLD book."""
    ckpt = tmp_path / "checkpoints"
    ckpt.mkdir()
    (ckpt / "2026-09-02-morning.json").write_text("{}")
    warnings = dr.check_live_checkpoints(tmp_path)
    assert len(warnings) == 1
    assert "2026-09-02-morning.json" in warnings[0]
    assert "does not delete files" in warnings[0]


def test_expired_decision_checkpoint_is_not_warned_about(dr, tmp_path):
    import os
    from src.decision_checkpoint import MAX_AGE_MINUTES

    ckpt = tmp_path / "checkpoints"
    ckpt.mkdir()
    old = ckpt / "2026-08-31-morning.json"
    old.write_text("{}")
    stale = time.time() - (MAX_AGE_MINUTES * 60) - 600
    os.utime(old, (stale, stale))
    assert dr.check_live_checkpoints(tmp_path) == []


def test_no_checkpoint_directory_is_not_an_error(dr, tmp_path):
    assert dr.check_live_checkpoints(tmp_path / "nothing-here") == []


def test_unknown_clock_is_warned_not_refused(dr):
    w = dr.check_trading_window(
        market_open=None,
        allow_market_open=False,
        allow_session_window=False,
        now=_et(17, 30),
    )
    assert w["refusals"] == []
    assert any("clock" in x for x in w["warnings"])


def test_run_refuses_when_the_broker_clock_says_open(dr, wired, tmp_path):
    wired.client = FakeClient(market_open=True)
    import alpaca.trading.client as ac

    ac.TradingClient = lambda *a, **k: wired.client

    before = _counts(wired.db_path)
    rc = dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes"])
    assert rc == 4
    assert wired.client.writes == []
    assert _counts(wired.db_path) == before


# ===========================================================================
# 6. THE FLATTEN ITSELF
# ===========================================================================


def test_flatten_cancels_orders_before_liquidating(dr):
    client = FakeClient()
    out = dr.flatten_book(client, settle_seconds=0.0)
    assert client.writes[0] == "close_all_positions(cancel_orders=True)"
    assert "cancel_orders()" in client.writes
    assert len(out["close_all"]) == 6
    assert out["close_all"][0]["order_id"].startswith("liq-")
    assert out["errors"] == []


def test_flatten_reports_broker_errors_instead_of_raising(dr):
    class Broken(FakeClient):
        def close_all_positions(self, cancel_orders=None):
            raise RuntimeError("422 insufficient qty")

    out = dr.flatten_book(Broken(), settle_seconds=0.0)
    assert any("422" in e for e in out["errors"])


def test_execute_flattens_and_summarises(dr, wired, tmp_path, capsys):
    rc = dr.run(_base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes"])
    assert rc == 0
    assert "close_all_positions(cancel_orders=True)" in wired.client.writes
    out = capsys.readouterr().out
    assert "AFTER" in out
    assert "Positions     : 6  ->  0" in out
    assert "Open orders   : 5  ->  0" in out


def test_a_failed_flatten_does_not_wipe_the_ledger(dr, wired, tmp_path):
    """If the liquidation errors, the trade history that explains the
    still-open positions must survive."""

    class Broken(FakeClient):
        def close_all_positions(self, cancel_orders=None):
            self.writes.append("close_all_positions(attempted)")
            raise RuntimeError("503 from broker")

    wired.client = Broken()
    import alpaca.trading.client as ac

    ac.TradingClient = lambda *a, **k: wired.client

    before = _counts(wired.db_path)
    backup_root = tmp_path / "resets"
    rc = dr.run(
        _base_argv(wired.db_path, backup_root) + ["--execute", "--owner-approved", "--yes", "--settle-seconds", "0"]
    )

    assert rc == 5
    assert _counts(wired.db_path) == before  # ledger intact
    stamp_dir = next(iter(backup_root.iterdir()))
    manifest = json.loads((stamp_dir / "reset_manifest.json").read_text())
    assert "503 from broker" in manifest["aborted"]
    assert (stamp_dir / "quant_agent.db").exists()  # backup still taken


def test_positions_left_open_with_the_market_open_aborts_the_clear(dr, wired, tmp_path):
    class NoOp(FakeClient):
        def close_all_positions(self, cancel_orders=None):
            self.writes.append("close_all_positions(no-op)")
            return []  # broker accepted nothing; positions remain

    wired.client = NoOp(market_open=True)
    import alpaca.trading.client as ac

    ac.TradingClient = lambda *a, **k: wired.client

    before = _counts(wired.db_path)
    rc = dr.run(
        _base_argv(wired.db_path, tmp_path / "resets")
        + ["--execute", "--owner-approved", "--yes", "--settle-seconds", "0", "--allow-market-open"]
    )
    assert rc == 5
    assert _counts(wired.db_path) == before


def test_positions_queued_with_the_market_closed_still_clears(dr, wired, tmp_path):
    """The normal daily path: the market is shut, the sells are queued to the
    next open, and the desk's own history is cleared anyway."""

    class Queued(FakeClient):
        def close_all_positions(self, cancel_orders=None):
            self.writes.append("close_all_positions(queued)")
            self._orders = []
            return [
                SimpleNamespace(symbol=p.symbol, status=200, order_id=f"q-{p.symbol}", body=None)
                for p in self._positions
            ]  # positions NOT gone yet

    wired.client = Queued(market_open=False)
    import alpaca.trading.client as ac

    ac.TradingClient = lambda *a, **k: wired.client

    rc = dr.run(
        _base_argv(wired.db_path, tmp_path / "resets")
        + ["--execute", "--owner-approved", "--yes", "--settle-seconds", "0"]
    )
    assert rc == 0
    assert _counts(wired.db_path)["trades"] == 0


def test_skip_broker_leaves_the_book_alone(dr, wired, tmp_path):
    rc = dr.run(
        _base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes", "--skip-broker"]
    )
    assert rc == 0
    assert wired.client.writes == []
    assert _counts(wired.db_path)["trades"] == 0


def test_skip_db_leaves_the_database_alone(dr, wired, tmp_path):
    before = _counts(wired.db_path)
    rc = dr.run(
        _base_argv(wired.db_path, tmp_path / "resets") + ["--execute", "--owner-approved", "--yes", "--skip-db"]
    )
    assert rc == 0
    assert _counts(wired.db_path) == before
    assert "close_all_positions(cancel_orders=True)" in wired.client.writes


# ===========================================================================
# KEEP-HISTORY RESET (the default): record the reset time, clear the old book; delete no history
# ===========================================================================

_KEEP_QUEUES = ("pending_protection_restores", "pending_repegs", "pending_stop_amends", "positions")
_EPOCH_BOOKKEEPING = ("account_epoch", "sqlite_sequence")


def _keep_argv(db_path, backup_root):
    return ["--config", "/does/not/matter.yaml", "--db", str(db_path), "--backup-root", str(backup_root)]


@pytest.fixture
def keep(dr, wired, monkeypatch):
    """`wired`, with an empty paper book at the broker and an idle clock."""
    wired.client = FakeClient(
        account=_account(number="PA00000000", equity="10000", cash="10000"), positions=[], orders=[]
    )
    monkeypatch.setattr(dr, "check_trading_window", lambda **k: {"refusals": [], "warnings": []})
    monkeypatch.setattr(dr, "check_live_checkpoints", lambda *_a, **_k: [])
    return wired


def _epoch_rows(path):
    """One bool per epoch row: does it carry a reset time? (The first, no-cut row does not.)"""
    conn = sqlite3.connect(path)
    try:
        return [bool(r[0]) for r in conn.execute("SELECT epoch_utc FROM account_epoch ORDER BY id")]
    finally:
        conn.close()


def _history(counts):
    return {t: n for t, n in counts.items() if t not in _KEEP_QUEUES and t not in _EPOCH_BOOKKEEPING}


def test_keep_history_is_the_default_and_records_the_reset_without_deleting_history(dr, keep, tmp_path):
    before = _counts(keep.db_path)
    assert before["pending_repegs"] and before["positions"] and before["trades"] and before["daily_pnl"]
    rc = dr.run(_keep_argv(keep.db_path, tmp_path / "resets") + ["--execute", "--yes"])
    assert rc == 0
    after = _counts(keep.db_path)
    assert all(after[t] == 0 for t in _KEEP_QUEUES if t in after)
    assert _history(after) == _history(before)
    assert _epoch_rows(keep.db_path) == [False, True]
    assert keep.client.writes == []  # never flattens, never cancels
    assert list((tmp_path / "resets").glob("*/quant_agent.db"))  # backed up first


def test_keep_history_dry_run_changes_nothing(dr, keep, tmp_path):
    before = _counts(keep.db_path)
    assert dr.run(_keep_argv(keep.db_path, tmp_path / "resets")) == 0
    assert _counts(keep.db_path) == before
    assert _epoch_rows(keep.db_path) == [False]


@pytest.mark.parametrize(
    "setup, why",
    [
        (lambda c: setattr(c, "_positions", [_position("MSFT", 1, 1, 1)]), "book is not empty"),
        (lambda c: setattr(c, "_orders", [_order("o-1", "MSFT")]), "open orders"),
    ],
)
def test_keep_history_refuses_an_unsafe_reset_and_touches_nothing(dr, keep, tmp_path, capsys, setup, why):
    setup(keep.client)
    before = _counts(keep.db_path)
    rc = dr.run(_keep_argv(keep.db_path, tmp_path / "resets") + ["--execute", "--yes"])
    assert rc == 4
    assert why in capsys.readouterr().err
    assert _counts(keep.db_path) == before
    assert _epoch_rows(keep.db_path) == [False]


@pytest.mark.parametrize("flag", ["--skip-db", "--skip-broker", "--allow-market-open", "--allow-session-window"])
def test_keep_history_refuses_every_override(dr, keep, tmp_path, flag):
    before = _counts(keep.db_path)
    assert dr.run(_keep_argv(keep.db_path, tmp_path / "resets") + ["--execute", "--yes", flag]) == 7
    assert _counts(keep.db_path) == before
