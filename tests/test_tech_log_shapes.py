"""Logged tech_analyst answers are read in every shape the seat or older code wrote.

Measured 17 Oct 2026: 39 of 41 production rows were `{"results": [...]}` and
the readers (written for a bare list / "analyses") saw nothing, so the evening
thesis-health block said "no tech history" and had_ta_signal was always false.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from src.models import Position
from src.storage.db import Database
from src.trading_calendar import et_today, trading_sessions_held
from tests.pipeline_factory import build_pipeline

A = {"symbol": "ZZZA", "rating": "buy"}
B = {"symbol": "ZZZB", "rating": "sell"}
RESULTS = json.dumps({"results": [A, B]})
CHUNKED = f"--- chunk 1/2 ---\n{json.dumps({'results': [A]})}\n--- chunk 2/2 ---\n{json.dumps({'results': [B]})}"
BARE = json.dumps([A, B])
ANALYSES = json.dumps({"analyses": [A, B]})


def _read(raw):
    from src.evolution.quarterly_digest import _tech_analyses_from_row

    return _tech_analyses_from_row(raw)


@pytest.mark.parametrize("raw", [RESULTS, CHUNKED, BARE, ANALYSES], ids=["results", "chunked", "bare", "analyses"])
def test_every_logged_shape_yields_symbols_and_ratings(raw):
    got = {a["symbol"]: a["rating"] for a in _read(raw)}
    assert got == {"ZZZA": "buy", "ZZZB": "sell"}


@pytest.mark.parametrize("raw", [None, "", "not json"])
def test_unreadable_rows_yield_nothing(raw):
    assert _read(raw) == []


def test_thesis_trajectory_reads_results_shaped_row(tmp_path, monkeypatch):
    monkeypatch.setattr("src.sector_reference._get_sector", lambda _sym: "")
    p = build_pipeline(db=Database(str(tmp_path / "t.db")))
    p.db.initialize()
    p.market = MagicMock()
    p.market.get_valuation_metrics.return_value = {}
    p.news_store = MagicMock()
    p.news_store.data_dir = tmp_path / "news"
    p.earnings_provider = MagicMock()
    p.earnings_provider.manifest = {}
    p.macro_store = MagicMock()
    p.macro_store.load_last_state.return_value = None
    p.broker = MagicMock()
    p.broker.trading_sessions_held.side_effect = trading_sessions_held

    def day(n):
        return et_today() - timedelta(days=n)

    p.db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, fill_status, "
        "fill_qty, fill_price, timestamp, position_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ZZZR", "BUY", 10, 50.0, "thesis", "r", "filled", 10, 50.0, f"{day(10)} 14:00:00", "pos-zzzr"),
    )
    p.db.conn.execute(
        "INSERT INTO agent_logs (agent_name, run_id, timestamp, input_summary, output_summary, "
        "full_response, model, tokens_used) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "tech_analyst",
            "t",
            f"{day(3)} 15:00:00",
            "",
            "",
            json.dumps({"results": [{"symbol": "ZZZR", "rating": "buy"}]}),
            "m",
            0,
        ),
    )
    p.db.conn.commit()
    pos = Position(
        symbol="ZZZR", qty=10, avg_entry=50.0, current_price=52.0, market_value=520, unrealized_pnl=20, sector=""
    )
    traj = p._build_thesis_health_context([pos])["ZZZR"]["tech_trajectory"]
    assert traj == [f"{day(3)}: buy"]
