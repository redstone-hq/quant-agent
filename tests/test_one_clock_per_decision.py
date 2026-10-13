"""src/ reads one clock: no SQL 'now' and no bare datetime.now outside the clock module.

Rule and rationale: scripts/one_clock_guard.py. The limit is the fixed
allow-list config/check_allowlists/one_clock.txt: no baseline, no trunk delta.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scripts import one_clock_guard as g
from scripts.check_allowlist import load
from src.cost_circuit import clock as cc_clock
from src.trading_calendar import sql_utc_stamp, utc_now

PLANTED = (
    "from datetime import datetime\n"
    "def f(c):\n"
    "    stamp = datetime.now()\n"
    "    return c.execute(\"SELECT 1 FROM t WHERE created_at < datetime('now', '-1 day')\")\n"
)


def test_tree_matches_the_fixed_allowlist_exactly():
    assert g.check() == ([], [])


def test_a_new_offending_file_is_refused():
    sites = g.working_sites()
    for kind, line, scope, src in g.scan_sites("src/planted.py", PLANTED):
        sites[("src/planted.py", kind, scope, src)] = [line]
    unlisted, stale = g.check(sites=sites)
    assert stale == []
    assert any("src/planted.py" in u and "py_now" in u for u in unlisted), unlisted
    assert any("src/planted.py" in u and "sql_now" in u for u in unlisted), unlisted


def test_an_allowlisted_file_passes():
    listed_paths = sorted({ln.split('"')[1] for ln in load(g.ALLOWLIST)})
    assert listed_paths, "the allow-list is empty; this test needs one listed file"
    path = listed_paths[0]
    unlisted, _stale = g.check(sites=g.working_sites([path]))
    assert unlisted == []


def test_a_converted_file_holds_no_clock_site():
    for path in ("src/cost_circuit/parts/owner_notify.py", "src/cost_circuit/parts/settlement.py"):
        assert g.working_sites([path]) == {}, path
        assert not any(path in ln for ln in load(g.ALLOWLIST)), path


def test_clock_module_is_the_only_exemption():
    assert g.scan_sites(g.CLOCK_MODULE, PLANTED) == []
    assert g.scan_sites("src/other/trading_calendar.py", PLANTED) != []


def test_what_is_not_a_clock_read():
    assert g.scan_sites("src/a.py", "x = et_now().date()\ny = f().today()\n") == []  # a call result, not the OS
    assert g.scan_sites("src/a.py", '"""docs say datetime(\'now\')"""\n') == []  # docstring
    assert g.scan_sites("src/a.py", "import datetime as dt\nx = dt.datetime.utcnow()\n") != []
    assert g.scan_sites("src/a.py", 'q = "created_at TEXT DEFAULT CURRENT_TIMESTAMP"\n') != []


# --- the SQL-ready stamp helper -------------------------------------------------------------


def test_sql_utc_stamp_is_sqlites_own_format():
    stamp = sql_utc_stamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp), stamp
    # SQLite parses it as one of its own and hands it back byte for byte.
    assert sqlite3.connect(":memory:").execute("SELECT datetime(?)", (stamp,)).fetchone()[0] == stamp


def test_sql_utc_stamp_converts_to_utc_and_reads_naive_as_utc():
    et = datetime(2026, 10, 13, 10, 5, 9, tzinfo=ZoneInfo("America/New_York"))
    assert sql_utc_stamp(et) == "2026-10-13 14:05:09"
    assert sql_utc_stamp(datetime(2026, 10, 13, 14, 5, 9)) == "2026-10-13 14:05:09"
    assert sql_utc_stamp(datetime(2026, 10, 13, 14, 5, 9, 999_000, tzinfo=timezone.utc)) == "2026-10-13 14:05:09"


def test_utc_now_is_aware_utc():
    now = utc_now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_cost_circuit_stamp_follows_its_frozen_clock(monkeypatch):
    pinned = datetime(2026, 10, 13, 16, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(cc_clock, "_now_utc", lambda: pinned)
    assert cc_clock._sql_stamp() == "2026-10-13 16:00:00"
    assert cc_clock._sql_stamp(minutes_ago=2) == "2026-10-13 15:58:00"
