"""The evening thesis-health review reads every held stock from the POSITION'S OPEN.

Measured 12-14 Oct 2026: XOM was bought six times on one position, and the
review dated the holding (and its tech/news window) from the newest top-up, so
a three-day-old trade looked new. The open is the first filled opening row on
the current position_id; tech rows and news items count only from there, and
the tech trajectory is one rating per session, not one per intraday run.
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

SYM = "ZZZT"
PID = "pos-zzzt-open"


def _day(n: int):
    return et_today() - timedelta(days=n)


def _trade(p, ts: str, reasoning: str) -> None:
    p.db.conn.execute(
        "INSERT INTO trades (symbol, action, qty, price, reasoning, run_id, fill_status, "
        "fill_qty, fill_price, timestamp, position_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (SYM, "BUY", 10, 50.0, reasoning, "r", "filled", 10, 50.0, ts, PID),
    )
    p.db.conn.commit()


def _tech(p, ts: str, rating: str) -> None:
    p.db.conn.execute(
        "INSERT INTO agent_logs (agent_name, run_id, timestamp, input_summary, output_summary, "
        "full_response, model, tokens_used) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("tech_analyst", "t", ts, "", "", json.dumps([{"symbol": SYM, "rating": rating}]), "m", 0),
    )
    p.db.conn.commit()


def _news(root, day, event: str) -> None:
    d = root / str(day)
    d.mkdir(parents=True, exist_ok=True)
    (d / "full_report.json").write_text(
        json.dumps({"state_changes": [{"event": event, "conviction": "high", "affected_symbols": [SYM]}]})
    )


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    monkeypatch.setattr("src.sector_reference._get_sector", lambda _sym: "")  # no network
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
    # Opened 10 days ago with the original thesis; topped up yesterday.
    _trade(p, f"{_day(10)} 14:00:00", "original thesis")
    _trade(p, f"{_day(1)} 15:00:00", "top-up note")
    return p


def _ctx(p) -> dict:
    pos = Position(
        symbol=SYM, qty=20, avg_entry=50.0, current_price=52.0, market_value=1040, unrealized_pnl=40, sector=""
    )
    return p._build_thesis_health_context([pos])[SYM]


def test_top_up_does_not_reset_entry_date_or_sessions_held(pipe):
    ctx = _ctx(pipe)
    assert ctx["entry_date"] == str(_day(10))
    assert ctx["sessions_held"] == trading_sessions_held(_day(10), et_today())
    assert ctx["entry_reasoning"] == "original thesis"


def test_tech_rows_before_the_open_are_excluded(pipe):
    _tech(pipe, f"{_day(12)} 15:00:00", "strong_sell")  # before the open
    _tech(pipe, f"{_day(5)} 15:00:00", "buy")
    traj = _ctx(pipe)["tech_trajectory"]
    assert not any("strong_sell" in t for t in traj)
    assert len(traj) == 1 and traj[0].endswith("buy")


def test_one_tech_rating_per_session_the_sessions_last(pipe):
    for ts, rating in [("14:00:00", "hold"), ("16:00:00", "sell"), ("19:00:00", "strong_buy")]:
        _tech(pipe, f"{_day(3)} {ts}", rating)
    _tech(pipe, f"{_day(2)} 15:00:00", "buy")
    assert _ctx(pipe)["tech_trajectory"] == [f"{_day(2)}: buy", f"{_day(3)}: strong_buy"]


def test_news_before_the_open_is_excluded(pipe, tmp_path):
    _news(tmp_path / "news", _day(12), "pre-open headline")
    _news(tmp_path / "news", _day(4), "post-open headline")
    ctx = _ctx(pipe)
    assert ctx["news_count_8w"] == 1
    assert ctx["latest_news_headlines"] == ["post-open headline"]
