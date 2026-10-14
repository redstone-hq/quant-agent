"""One definition of room for a NEW position, and no lost sale by judgement.

Measured 13 Oct: the intraday no-room check skipped only 2 paid PM calls while
7 ran with $209 of room or less. A held mover counted as room on ANY headroom
(18:57: held VLO and META, $186 of room), and a sell-rated mover counted as a
short that always fits when the gross ladder did not back the budget. The
other way round, "no room to buy" also stopped a call that could only sell.

Room is the headroom against each mover's own minimum position: the owner's
minimum-risk floor (read from RiskConfig) at the mover's own stop.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.config import RiskConfig
from src.intraday.buying_room import NO_BUYING_ROOM, BuyingRoom, assess_buying_room
from src.risk.min_risk import min_risk_shortfall

_EQUITY = 10_000.0
_ENTRY, _STOP = 100.0, 95.0
_FLOOR = RiskConfig.model_fields["min_position_risk_pct"].default


def _min_position() -> float:
    return min_risk_shortfall(
        position_notional=None, entry=_ENTRY, stop=_STOP, equity=_EQUITY, floor_pct=_FLOOR
    ).min_notional


def _room(positions, analyses, movers, headroom, ladder_backed=True) -> BuyingRoom:
    pipeline = SimpleNamespace(
        portfolio_constructor=SimpleNamespace(entry_stop_preview=lambda a, d, regime=None: (_ENTRY, _STOP, 110.0))
    )
    ctx = SimpleNamespace(positions=positions, deployable_cash=0.0, total_value=_EQUITY, macro_analysis=None)
    with (
        patch("src.pipeline_sizing._entry_deployment_budget", return_value=(headroom, ladder_backed, "note")),
        patch("src.pipeline_sizing._min_position_risk_pct", return_value=_FLOOR),
        patch("src.pipeline_gross_ceiling._session_gross_ceiling", return_value=None),
        patch("src.pipeline_stage_helpers._macro_regime", return_value="neutral"),
    ):
        return assess_buying_room(pipeline, ctx, analyses, movers)


def test_held_movers_below_the_minimum_position_are_not_room():
    """13 Oct 18:57: held VLO and META, $186 of headroom, below each one's minimum position."""
    assert _min_position() > 186.0
    held = [{"symbol": "VLO", "market_value": 2_000.0}, {"symbol": "META", "market_value": 2_500.0}]
    reads = [SimpleNamespace(symbol="VLO", rating="buy"), SimpleNamespace(symbol="META", rating="strong_buy")]
    room = _room(held, reads, ["VLO", "META"], 186.0)
    assert not room.has_room, room.candidates
    assert all(row["min_notional_usd"] == round(_min_position(), 2) for row in room.candidates)


def test_a_sell_rated_mover_gets_no_short_exemption_when_the_ladder_does_not_back_the_budget():
    """13 Oct 15:17/15:49: budget not ladder-backed, a sell-rated mover counted as a short that always fits."""
    room = _room([], [SimpleNamespace(symbol="DE", rating="sell")], ["DE"], 186.0, ladder_backed=False)
    assert not room.has_room, room.candidates
    assert room.candidates[0]["direction"] == "short"


def _db(*, research_at: str, fill_at: str, seat: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE agent_logs (id INTEGER PRIMARY KEY, agent_name TEXT, run_id TEXT, timestamp TEXT)")
    db.execute(
        "CREATE TABLE current_account_trades (id INTEGER PRIMARY KEY, symbol TEXT, action TEXT, "
        "fill_status TEXT, filled_at TEXT, timestamp TEXT)"
    )
    db.execute("INSERT INTO agent_logs (agent_name, run_id, timestamp) VALUES (?, 'm', ?)", (seat, research_at))
    db.execute(
        "INSERT INTO agent_logs (agent_name, run_id, timestamp) "
        "VALUES ('portfolio_manager', 'intra_check-a', '2026-10-12 14:17:00')"
    )
    db.execute(
        "INSERT INTO current_account_trades (symbol, action, fill_status, filled_at, timestamp) "
        "VALUES ('XOM', 'SELL', 'filled', ?, ?)",
        (fill_at, fill_at),
    )
    return db


def _no_room() -> BuyingRoom:
    row = {"symbol": "PWR", "direction": "long", "min_notional_usd": round(_min_position(), 2), "fits": False}
    return BuyingRoom(34.80, "§11.2 ladder headroom $34.80", True, _FLOOR, 2.0, [row])


def _judge(read: str, *, research_at="2026-10-12 13:00:00", fill_at="2026-10-12 12:00:00", seat="news_analyst_midday"):
    from src.pipeline_intraday import IntradayScanBody

    room = _no_room()
    host = SimpleNamespace(
        _intraday_buying_room=lambda c, a, s, sold: room, db=_db(research_at=research_at, fill_at=fill_at, seat=seat)
    )
    ctx = SimpleNamespace(positions=[{"symbol": "AMD", "market_value": 1_500.0}], run_id="intra_check-b")
    reads = [SimpleNamespace(symbol="AMD", rating=read), SimpleNamespace(symbol="PWR", rating="buy")]
    events = MagicMock()
    with (
        patch("src.pipeline_intraday._record_pipeline_event", events),
        patch("src.pipeline_intraday.record_dropped_name", MagicMock()),
    ):
        result = IntradayScanBody._intraday_no_buying_room(host, ctx, reads, ["PWR"])
    rows = [c for c in events.call_args_list if c.args[3] == "buying_room"]
    return result, rows


def test_no_room_and_a_held_read_that_left_buy_does_not_buy_a_paid_call():
    """A neutral read does not sell (owner Option A, 14 Oct); a read against the holding is sold
    by the free below-entry-bar sale with no PM call. Neither is a reason to pay for the PM."""
    for read in ("neutral", "sell"):
        result, rows = _judge(read)
        assert result is not None and result["pm_call_skipped"] == NO_BUYING_ROOM, read
        assert rows[0].args[4] == "pm_call_skipped"


def test_no_room_and_nothing_changed_skips_with_a_recorded_reason_and_the_room_figures():
    result, rows = _judge("buy")
    assert result is not None and result["pm_call_skipped"] == NO_BUYING_ROOM
    assert len(rows) == 1 and rows[0].args[4] == "pm_call_skipped"
    assert rows[0].args[5] == "no room and no sale by judgement possible"
    assert rows[0].kwargs["headroom_usd"] == 34.8
    assert rows[0].kwargs["candidates_considered"][0]["min_notional_usd"] == round(_min_position(), 2)


def test_a_news_read_or_an_exit_fill_since_the_last_pm_call_runs_the_call_with_buys_blocked():
    """The live news seat logs as news_analyst_<session> (12-14 Oct: morning, midday, intra_check, close)."""
    for seat in ("news_analyst_intra_check", "news_analyst_midday", "news_analyst_close"):
        result, rows = _judge("buy", research_at="2026-10-12 14:30:00", seat=seat)
        assert result is None, seat
        assert rows[0].args[4] == "pm_called_buys_blocked" and seat in rows[0].args[5]
    assert _judge("buy", research_at="2026-10-12 14:00:00", seat="news_analyst_intra_check")[0] is not None
    assert _judge("buy", fill_at="2026-10-12T14:40:00.123456Z")[0] is None
