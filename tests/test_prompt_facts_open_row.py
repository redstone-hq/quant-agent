"""An add-on buy must not make a holding look brand new to the AI seats.

Measured 13 Oct 2026 (AAPL): the position opened on 12 Oct; a 13 Oct 10:26 ET
add wrote a second opening row on the same `position_id`. The 1 pm and 3:31 pm
position-reviewer prompts then read age, target and thesis off that add —
days_held=0, sessions_held=0, the add's 368.74 target — so a day-old trade at
-6% looked like a fresh entry.

Every ENTRY field in the holding facts must come from the row that OPENED the
position (`Database.get_position_open_row`), never the newest add.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.models import Position
from src.storage.db import Database
from src.trading_calendar import trading_sessions_held
from tests.pipeline_factory import build_pipeline

DAY1 = date(2026, 10, 12)
DAY2 = date(2026, 10, 13)
OPEN_THESIS = "day-1 open: base breakout over 250 with earnings tailwind"
ADD_THESIS = "day-2 add: momentum continuation"
OPEN_STOP = 240.0
ADD_STOP = 248.0


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "openrow.db"))
    database.initialize()
    yield database
    database.close()


def _buy(db, ts, *, price, stop, target, reasoning, horizon):
    db.insert_trade(
        symbol="AAPL",
        action="BUY",
        qty=10.0,
        price=price,
        reasoning=reasoning,
        run_id="r",
        stop_loss=stop,
        take_profit=target,
        fill_status="filled",
        expected_horizon_sessions=horizon,
    )
    with db._lock:
        db.conn.execute(
            "UPDATE trades SET timestamp = ? WHERE id = (SELECT MAX(id) FROM trades WHERE symbol = 'AAPL')",
            (ts,),
        )
        db.conn.commit()


@pytest.fixture
def pipeline(db):
    _buy(db, "2026-10-12 14:05:00", price=255.0, stop=OPEN_STOP, target=290.0, reasoning=OPEN_THESIS, horizon=10)
    _buy(db, "2026-10-13 14:26:00", price=262.0, stop=ADD_STOP, target=368.74, reasoning=ADD_THESIS, horizon=12)
    assert db.get_symbol_last_buy("AAPL")["reasoning"] == ADD_THESIS, "fixture sanity: newest row is the add"

    broker = MagicMock()
    broker.get_current_stop_price.return_value = 245.0
    broker.trading_sessions_held.side_effect = trading_sessions_held
    pipe = build_pipeline(db=db, broker=broker, market=MagicMock())
    pipe._atr_for_symbol = lambda s: 6.0
    return pipe


AAPL = Position(
    symbol="AAPL",
    qty=20.0,
    avg_entry=258.5,
    current_price=243.0,
    market_value=4860.0,
    unrealized_pnl=-310.0,
    unrealized_intraday_pnl=0.0,
    sector="Technology",
)


def test_add_does_not_reset_age_or_entry_stop_in_holding_facts(pipeline):
    with patch("src.pipeline_prompt_facts.et_today", return_value=DAY2):
        facts = pipeline._build_position_facts([AAPL], morning_trades=[], total_value=100_000.0)["AAPL"]
    assert facts["sessions_held"] is not None and facts["sessions_held"] >= 1, (
        f"opened {DAY1}, added {DAY2}: the add must not make it a day-0 entry (got {facts['sessions_held']})"
    )
    assert facts["days_held"] >= 1
    assert facts["initial_stop"] == OPEN_STOP, "R's denominator is the day-1 stop, not the add's"


def test_add_does_not_replace_entry_thesis_or_stop_in_position_history(pipeline):
    with patch("src.pipeline_prompt_facts.et_today", return_value=DAY2):
        hist = pipeline._build_position_history([AAPL])["AAPL"]
    assert hist["entry_reasoning"].startswith("day-1 open"), "the thesis is the day-1 thesis"
    assert hist["entry_date"] == str(DAY1)
    assert hist["stop_loss"] == OPEN_STOP


def test_morning_add_row_without_position_id_still_resolves_the_open(pipeline):
    """Today's morning row is the add, possibly an in-memory dict with no id."""
    morning_add = {"symbol": "AAPL", "action": "BUY", "timestamp": "2026-10-13 14:26:00", "stop_loss": ADD_STOP}
    with patch("src.pipeline_prompt_facts.et_today", return_value=DAY2):
        facts = pipeline._build_position_facts([AAPL], morning_trades=[morning_add], total_value=100_000.0)["AAPL"]
    assert facts["sessions_held"] >= 1
    assert facts["initial_stop"] == OPEN_STOP
