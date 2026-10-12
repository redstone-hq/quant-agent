"""Board item 202: no new outbound-client site in ``src/`` without a replay seam.

A replay of a recorded session must be deterministic and free. The runtime wall
catches an outbound attempt when it happens; this guard catches it when it is
written. The scanner and the policy list of client modules live in
``scripts/replay_outbound_guard.py``.

FIXED EXCEPTION LIST
--------------------
Every existing outbound import is pinned as ``file | client`` in
config/check_allowlists/struct_replay_outbound.txt. Nothing is compared with any
trunk. A pair not in the list fails, and so does a listed pair that no longer
occurs.
"""

from __future__ import annotations

import pytest

from scripts import replay_outbound_guard, struct_allowlist


def test_no_new_outbound_client_site_without_a_replay_seam():
    bad = replay_outbound_guard.violations()
    assert not bad, (
        "NEW outbound-client import(s) in src/ that a replay has no named seam for. "
        "A replay must be served from a recording or refuse naming the missing "
        "recording, never reach a live provider (board item 202). Handle it in "
        "ops/rehearsal first:\n  " + "\n  ".join(bad)
    )


def test_the_scan_is_actually_measuring():
    """A guard that measured nothing would pass vacuously."""
    assert len(replay_outbound_guard.scanned_paths()) > 50
    assert replay_outbound_guard.found(), "no outbound site seen in src/ -- the scan is dead"


def _pretend(monkeypatch, entries):
    monkeypatch.setattr(replay_outbound_guard, "found", lambda: list(entries))


def _list(tmp_path, entries):
    struct_allowlist.write("replay_outbound", entries, tmp_path)
    return tmp_path


def test_a_new_pair_fails(tmp_path, monkeypatch):
    _pretend(monkeypatch, ["src/a.py | requests"])
    bad = replay_outbound_guard.violations(_list(tmp_path, []))
    assert len(bad) == 1 and "NEW" in bad[0] and "src/a.py | requests" in bad[0], bad


def test_a_listed_pair_passes(tmp_path, monkeypatch):
    _pretend(monkeypatch, ["src/a.py | requests"])
    assert replay_outbound_guard.violations(_list(tmp_path, ["src/a.py | requests"])) == []


def test_a_stale_entry_fails(tmp_path, monkeypatch):
    _pretend(monkeypatch, [])
    bad = replay_outbound_guard.violations(_list(tmp_path, ["src/a.py | requests"]))
    assert len(bad) == 1 and "STALE" in bad[0], bad


def test_a_second_client_in_a_listed_file_is_a_new_pair(tmp_path, monkeypatch):
    """Listing is per import: clearing a file for one client clears it for no other."""
    _pretend(monkeypatch, ["src/a.py | requests", "src/a.py | smtplib"])
    bad = replay_outbound_guard.violations(_list(tmp_path, ["src/a.py | requests"]))
    assert len(bad) == 1 and "smtplib" in bad[0], bad


def test_the_scanner_sees_each_import_shape():
    assert replay_outbound_guard.scan_text("import requests\n") == {"requests"}
    assert replay_outbound_guard.scan_text("import urllib.request\n") == {"urllib"}
    assert replay_outbound_guard.scan_text("from httpx import Client\n") == {"httpx"}
    assert replay_outbound_guard.scan_text("def f():\n    import yfinance\n") == {"yfinance"}
    assert replay_outbound_guard.scan_text("from . import socket\n") == set()
    assert replay_outbound_guard.scan_text("import json\nimport pandas\n") == set()


# --- the guard's SUBJECT is named by identity: what is a client, what is not ---

EXCLUDED_IMPORTS = {
    "from urllib.error import HTTPError\n": "urllib.error",
    "import urllib.error\n": "urllib.error",
    "from urllib.parse import urlsplit\n": "urllib.parse",
    "import urllib.parse\n": "urllib.parse",
    "from http.server import ThreadingHTTPServer\n": "http.server",
    "from alpaca.trading.requests import GetOrdersRequest\n": "alpaca.trading.requests",
    "from alpaca.trading.enums import OrderSide\n": "alpaca.trading.enums",
}


@pytest.mark.parametrize("source,submodule", sorted(EXCLUDED_IMPORTS.items()))
def test_each_excluded_submodule_is_excluded_for_its_stated_reason(source, submodule):
    reason = replay_outbound_guard.NOT_CLIENT_SUBMODULES[submodule]
    assert reason.strip(), f"{submodule} is excluded with no reason"
    assert replay_outbound_guard.scan_text(source) == set()


def test_the_exclusion_list_is_exactly_these_names():
    """Identity, not a count: a new entry must be named here with its reason."""
    assert set(replay_outbound_guard.NOT_CLIENT_SUBMODULES) == {
        "urllib.error",
        "urllib.parse",
        "http.server",
        "alpaca.trading.requests",
        "alpaca.trading.enums",
    }
    assert set(replay_outbound_guard.NOT_CLIENT_ATTRIBUTES["socket"]) == {"setdefaulttimeout", "getdefaulttimeout"}


@pytest.mark.parametrize(
    "source,client",
    [
        ("import urllib.request\n", "urllib"),
        ("from urllib.request import urlopen\n", "urllib"),
        ("from urllib.parse import urlsplit\nimport urllib.request\n", "urllib"),
        ("from urllib.error import HTTPError\nfrom urllib.request import urlopen\n", "urllib"),
        ("from alpaca.trading.client import TradingClient\n", "alpaca"),
        ("from alpaca.trading.stream import TradingStream\n", "alpaca"),
        ("from alpaca.data.historical.stock import StockHistoricalDataClient\n", "alpaca"),
        (
            "from alpaca.trading.requests import GetOrdersRequest\nfrom alpaca.trading.client import TradingClient\n",
            "alpaca",
        ),
        ("import socket\nsocket.create_connection(('h', 1))\n", "socket"),
        ("import socket\nsocket.setdefaulttimeout(3)\nsocket.socket()\n", "socket"),
        ("from socket import create_connection\n", "socket"),
    ],
)
def test_a_genuinely_outbound_import_is_still_flagged(source, client):
    assert replay_outbound_guard.scan_text(source) == {client}


def test_socket_timeout_helpers_alone_are_not_a_client():
    src = "import socket\nold = socket.getdefaulttimeout()\nsocket.setdefaulttimeout(old)\n"
    assert replay_outbound_guard.scan_text(src) == set()


def test_a_new_outbound_import_in_a_cleared_file_is_refused(monkeypatch):
    """The failing case: a file with only a request model gains a real client."""
    path = "src/execution/stop_read.py"
    assert path not in replay_outbound_guard.working_sites()
    grown = dict(replay_outbound_guard.working_sites())
    grown[path] = replay_outbound_guard.scan_text(
        "from alpaca.trading.requests import GetOrdersRequest\nfrom alpaca.trading.client import TradingClient\n"
    )
    monkeypatch.setattr(replay_outbound_guard, "working_sites", lambda: grown)
    bad = replay_outbound_guard.violations()
    assert len(bad) == 1 and path in bad[0] and "alpaca" in bad[0], bad


def test_http_client_is_still_a_client_after_the_http_server_exclusion():
    """The listener exclusion is by submodule: an outbound http.client import still bites."""
    assert replay_outbound_guard.scan_text("import http.client\n") == {"http"}
    assert replay_outbound_guard.scan_text("from http.client import HTTPSConnection\n") == {"http"}
