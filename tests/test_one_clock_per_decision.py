"""src/ reads one clock: no SQL 'now' and no bare datetime.now outside the clock module,
and (the original rule) no module that mixes a Python exchange day with SQL 'now'.

Rule and rationale: scripts/one_clock_guard.py. Rule A's limit is the fixed
allow-list config/check_allowlists/one_clock.txt; rule B has none.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scripts import one_clock_guard as g
from scripts.check_allowlist import load
from src import trading_calendar
from src.cost_circuit import clock as cc_clock
from src.trading_calendar import sql_utc_stamp, sql_utc_stamp_ago, utc_now

PLANTED = (
    "from datetime import datetime\n"
    "def f(c):\n"
    "    stamp = datetime.now()\n"
    "    return c.execute(\"SELECT 1 FROM t WHERE created_at < datetime('now', '-1 day')\")\n"
)

MIXED = (
    "from src.trading_calendar import et_today\n"
    "def f(c):\n"
    "    d = et_today()\n"
    "    return c.execute(\"SELECT 1 FROM t WHERE date(timestamp) = date('now')\")\n"
)


def _kinds(path: str, text: str) -> list[str]:
    return [kind for kind, *_ in g.scan_sites(path, text)]


# --- rule A: the fixed allow-list ------------------------------------------------------------


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
    unlisted, _stale = g.check(sites=g.working_sites([listed_paths[0]]))
    assert unlisted == []


def test_converted_and_deleted_files_hold_no_clock_site():
    listed = load(g.ALLOWLIST)
    for path in (
        "src/cost_circuit/parts/owner_notify.py",
        "src/cost_circuit/parts/settlement.py",
        "src/storage/clock_stamp.py",
    ):
        assert not any(f'"{path}"' in ln for ln in listed), path
        if (g.ROOT / path).is_file():
            assert g.working_sites([path]) == {}, path
    # The cost circuit no longer reads the OS clock itself; only its SQL-rewriting pin remains.
    assert not any('"src/cost_circuit/clock.py","py_now"' in ln for ln in listed)


def test_identity_is_the_whole_normalised_sql_text():
    columns = "    x TEXT,\n" * 30
    long_sql = f"CREATE TABLE t (\n    id INTEGER,\n{columns}    created_at TEXT DEFAULT CURRENT_TIMESTAMP\n)"
    ((kind, _line, _scope, text),) = g.scan_sites("src/a.py", f"SQL = '''{long_sql}'''\n")
    assert kind == "sql_now"
    assert text == " ".join(long_sql.split())
    assert len(text) > 120 and text.endswith("CURRENT_TIMESTAMP )")


def test_clock_module_is_the_only_exemption():
    assert g.scan_sites(g.CLOCK_MODULE, PLANTED) == []
    assert g.scan_sites("src/other/trading_calendar.py", PLANTED) != []


# --- rule B: the original mixed-module refusal, still absolute --------------------------------


def test_mixed_module_is_still_refused():
    assert [kind for kind, *_ in g.scan_mixed("src/planted.py", MIXED)] == ["py_day"]
    assert g.scan_mixed("src/a.py", MIXED.replace("et_today()", "1")) == []
    assert g.scan_mixed("src/a.py", MIXED.replace("date('now')", "?")) == []
    assert g.mixed_violations() == []


def test_mixed_module_is_refused_even_when_allowlisted(tmp_path):
    allow = tmp_path / "one_clock.txt"
    allow.write_text("".join(f"{s}\n" for s in sorted(load(g.ALLOWLIST).elements())))
    sites = g.working_sites()
    for kind, line, scope, src in g.scan_sites("src/planted.py", MIXED):
        sites[("src/planted.py", kind, scope, src)] = [line]
    with allow.open("a") as fh:
        for (path, kind, scope, src), _ in sites.items():
            if path == "src/planted.py":
                fh.write(f'["{path}","{kind}","{scope}","{src}"]\n')
    assert g.check(allow, sites=sites) == ([], [])  # rule A is satisfied by the listing...
    assert g.scan_mixed("src/planted.py", MIXED)  # ...and rule B refuses it anyway


# --- the probes: one per dodge ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'q = "SELECT datetime()"\n',
        'q = "SELECT date()"\n',
        'q = "SELECT time()"\n',
        'q = "SELECT julianday( )"\n',
        "q = 'SELECT unixepoch()'\n",
        "q = 'UPDATE t SET at = datetime(\"now\")'\n",
        "q = 'x INTEGER DEFAULT CURRENT_TIME'\n",
        'q = "SELECT datetime(\'no" "w\')"\n',
        'q = "SELECT datetime(" + "\'now\'" + ")"\n',
        "q = f\"SELECT datetime('now') FROM {table}\"\n",
        "q = \"WHERE a < datetime(\\n  'now', '-2 minutes')\"\n",
    ],
    ids=[
        "datetime()",
        "date()",
        "time()",
        "julianday()",
        "unixepoch()",
        "double-quoted-now",
        "CURRENT_TIME",
        "adjacent-literals",
        "plus-chain",
        "f-string",
        "multiline",
    ],
)
def test_sql_probe_is_caught(text):
    assert _kinds("src/a.py", text) == ["sql_now"], text


@pytest.mark.parametrize(
    "text",
    [
        "import time\nx = time.time()\n",
        "import time\nx = time.time_ns()\n",
        "from time import time\nx = time()\n",
        "import time\nx = time.strftime('%Y')\n",
        "import time\nx = time.localtime()\n",
        "import time\nx = time.gmtime()\n",
        "from time import gmtime\nx = gmtime()\n",
        "from datetime import datetime\nx = datetime.fromtimestamp(compute())\n",
        "from datetime import datetime\nx = datetime.utcfromtimestamp(a + b)\n",
        "from datetime import datetime\nx = getattr(datetime, 'now')()\n",
        "from datetime import datetime\nfn = datetime.now\n",
        "from datetime import datetime\nx = datetime.utcnow()\n",
        "import datetime as dt\nx = dt.date.today()\n",
    ],
    ids=[
        "time.time",
        "time.time_ns",
        "bare-time",
        "time.strftime",
        "time.localtime",
        "time.gmtime",
        "bare-gmtime",
        "fromtimestamp-computed",
        "utcfromtimestamp-computed",
        "getattr-now",
        "uncalled-reference",
        "utcnow",
        "date.today",
    ],
)
def test_py_probe_is_caught(text):
    assert _kinds("src/a.py", text) == ["py_now"], text


@pytest.mark.parametrize(
    "text",
    [
        "x = et_now().date()\n",
        "y = f().today()\n",
        "when.strftime('%Y')\n",
        "when.time()\n",
        "time.localtime(ts)\n",
        "datetime.fromtimestamp(row['ts'])\n",
        "datetime.fromtimestamp(int(ts), tz=UTC)\n",
        '"""docs say datetime(\'now\')"""\n',
        "q = 'SELECT datetime(ts)'\n",
    ],
    ids=[
        "call-result-date",
        "call-result-today",
        "instance-strftime",
        "instance-time",
        "localtime-of-value",
        "fromtimestamp-stored",
        "fromtimestamp-converted",
        "docstring",
        "datetime-of-column",
    ],
)
def test_what_is_not_a_clock_read(text):
    assert g.scan_sites("src/a.py", text) == [], text


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


def test_freezing_the_desk_clock_freezes_every_stamp(monkeypatch):
    pinned = datetime(2026, 10, 13, 16, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(trading_calendar, "utc_now", lambda: pinned)
    assert sql_utc_stamp() == "2026-10-13 16:00:00"
    assert sql_utc_stamp_ago(days=1, minutes=30) == "2026-10-12 15:30:00"
    assert cc_clock._now_utc() == pinned  # the cost circuit reads the same clock
    assert cc_clock._sql_stamp() == "2026-10-13 16:00:00"
    assert cc_clock._sql_stamp(minutes_ago=2) == "2026-10-13 15:58:00"
