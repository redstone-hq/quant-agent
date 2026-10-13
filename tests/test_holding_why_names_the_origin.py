"""End to end: a decision reaches the dashboard able to say who raised it.

This is deliberately NOT a unit test of the assembler. It seeds a
database the way the desk writes one — a position OPENED by an intraday
mover scan in one run, ADDED to in a later run — and then asks the HTTP
endpoint the dashboard actually fetches, `GET /holdings/{symbol}/why`,
the question the owner asks: why do we hold this.

Before the fix that accompanies this file, both halves failed:

* the view loaded only the add's run, so the opening run's discovery
  event was never read (`src/api/holding_entry_evidence.py`), and
* nothing recognised an intraday discovery as an origin even when it WAS
  loaded (`src/api/holding_origin.py`).

Measured against the production database on 2026-10-04, those two gaps
between them left eight of the eleven open positions rendering "which
seat raised it: Not recorded".
"""

from __future__ import annotations

import json
import sqlite3
from src.storage.db import Database

import pytest
from fastapi.testclient import TestClient

from src.api import db_reads
from src.api.server import create_app

OPENING_RUN = "intra_check-opening"
ADD_RUN = "intra_check-add"


def _insert_trade(conn, **row) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})", tuple(row.values()))


def _insert_evidence(conn, run_id, agent, kind, payload, when) -> None:
    conn.execute(
        "INSERT INTO specialist_evidence (run_id, agent_name, kind, scope, "
        "symbol, evidence_json, timestamp) VALUES (?, ?, ?, 'symbol', 'ZZT', ?, ?)",
        (run_id, agent, kind, json.dumps(payload), when),
    )


def _seed(path) -> None:
    """One position: opened by the intraday scan, added to a week later."""
    _d = Database(str(path))
    _d.initialize()  # the real schema, not a hand-built copy
    _d.close()
    conn = sqlite3.connect(path)
    _insert_trade(
        conn,
        symbol="ZZT",
        action="BUY",
        qty=10.0,
        price=50.0,
        reasoning="Open ZZT on a confirmed range breakout with volume behind it.",
        run_id=OPENING_RUN,
        timestamp="2026-09-24 15:19:17",
        position_id="pos-zzt",
        stop_loss=45.0,
        take_profit=62.0,
        initial_take_profit=62.0,
        expected_horizon_sessions=12,
        setup_type="range",
        conviction="high",
        fill_price=50.0,
        thesis_invalid_if="Price closes below 45.00 on heavy volume.",
    )
    _insert_trade(
        conn,
        symbol="ZZT",
        action="BUY",
        qty=4.0,
        price=55.0,
        reasoning="Adding to ZZT; the breakout held and the thesis is intact.",
        run_id=ADD_RUN,
        timestamp="2026-10-01 13:49:54",
        position_id="pos-zzt",
        stop_loss=49.0,
        take_profit=62.0,
        initial_take_profit=62.0,
        expected_horizon_sessions=12,
        setup_type="range",
        conviction="high",
        fill_price=55.0,
        thesis_invalid_if="Price closes below 49.00 on heavy volume.",
    )
    # The opening run is the ONLY place the discovery is written.
    _insert_evidence(
        conn,
        OPENING_RUN,
        "pipeline",
        "pipeline_event",
        {
            "stage": "opportunity",
            "outcome": "discovered",
            "reason": "intraday_move_threshold",
            "move_pct": 3.5393168759310507,
        },
        "2026-09-24 15:10:00",
    )
    _insert_evidence(
        conn,
        OPENING_RUN,
        "portfolio_manager",
        "target",
        {
            "symbol": "ZZT",
            "thesis": "Range breakout with volume confirmation.",
            "provenance": [
                {
                    "source": "technical",
                    "observed_stance": "buy",
                    "relationship": "supports",
                    "evidence": "Breakout above the range on accumulation volume.",
                }
            ],
        },
        "2026-09-24 15:12:00",
    )
    # The add's run carries a current thesis and nothing about origin.
    _insert_evidence(
        conn,
        ADD_RUN,
        "portfolio_manager",
        "target",
        {
            "symbol": "ZZT",
            "thesis": "Breakout held; adding into continuation.",
            "provenance": [
                {
                    "source": "technical",
                    "observed_stance": "buy",
                    "relationship": "supports",
                    "evidence": "Trend continuity intact above the breakout.",
                }
            ],
        },
        "2026-10-01 13:45:00",
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "q.db"
    _seed(db)
    monkeypatch.setattr(db_reads, "get_db_path", lambda: db)
    return TestClient(create_app())


def test_the_page_names_who_raised_the_name(client):
    body = client.get("/holdings/ZZT/why").json()
    assert body["readable"]["primary_driver"] == "Intraday move"
    assert "which seat raised it" not in body["not_recorded"]


def test_the_sentence_carries_the_move_that_caused_the_look(client):
    raised = client.get("/holdings/ZZT/why").json()["readable"]["raised_by"]
    assert raised and "3.5%" in raised
    assert "intraday scan" in raised


def test_the_live_thesis_still_comes_from_the_most_recent_entry(client):
    """The opening run must fill gaps, never overwrite the current state."""
    body = client.get("/holdings/ZZT/why").json()
    assert body["readable"]["stop_price"] == 49.0
    assert "held" in (body["readable"]["why"] or "")


def test_a_position_with_no_add_is_unchanged(client, tmp_path, monkeypatch):
    """The opening run IS the entry run; nothing is loaded twice."""
    db = tmp_path / "single.db"
    _d = Database(str(db))
    _d.initialize()  # the real schema, not a hand-built copy
    _d.close()
    conn = sqlite3.connect(db)
    _insert_trade(
        conn,
        symbol="ZZT",
        action="BUY",
        qty=10.0,
        price=50.0,
        reasoning="Open ZZT on a confirmed range breakout.",
        run_id=OPENING_RUN,
        timestamp="2026-09-24 15:19:17",
        position_id="pos-zzt",
        stop_loss=45.0,
    )
    _insert_evidence(
        conn,
        OPENING_RUN,
        "pipeline",
        "pipeline_event",
        {
            "stage": "opportunity",
            "outcome": "discovered",
            "reason": "intraday_move_threshold",
            "move_pct": 7.951993805652329,
        },
        "2026-09-24 15:10:00",
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: db)
    body = TestClient(create_app()).get("/holdings/ZZT/why").json()
    assert body["readable"]["primary_driver"] == "Intraday move"
    assert "8.0%" in body["readable"]["raised_by"]


def test_an_unrecorded_origin_still_says_so(client, tmp_path, monkeypatch):
    """The fix must not invent a driver where the desk wrote none down."""
    db = tmp_path / "silent.db"
    _d = Database(str(db))
    _d.initialize()  # the real schema, not a hand-built copy
    _d.close()
    conn = sqlite3.connect(db)
    _insert_trade(
        conn,
        symbol="ZZT",
        action="BUY",
        qty=1.0,
        price=10.0,
        reasoning="Bought.",
        run_id="run-quiet",
        timestamp="2026-09-24 15:19:17",
        position_id="pos-quiet",
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: db)
    body = TestClient(create_app()).get("/holdings/ZZT/why").json()
    assert body["readable"]["primary_driver"] is None
    assert "which seat raised it" in body["not_recorded"]
