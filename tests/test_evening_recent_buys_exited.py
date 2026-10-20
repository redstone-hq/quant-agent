"""Recent BUYs whose position has since closed must say so (PWR, 20 Oct)."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from src.agents.evening_analyst import EveningAnalystAgent
from src.models.positions import Position
from tests.pipeline_factory import build_pipeline


def _buys_with_exit():
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.broker.get_latest_price.return_value = 90.0
    d3 = (date.today() - timedelta(days=3)).isoformat()
    d1 = (date.today() - timedelta(days=1)).isoformat()
    pipeline.db.get_trades.return_value = [
        {"action": "STOP_OUT", "symbol": "ZZZ", "fill_price": 92.0, "timestamp": f"{d1} 15:00:00"},
        {"action": "BUY", "symbol": "ZZZ", "fill_price": 100.0, "timestamp": f"{d3} 14:00:00"},
        {"action": "BUY", "symbol": "ZZZB", "fill_price": 50.0, "timestamp": f"{d3} 14:00:00"},
    ]
    return pipeline._build_recent_buys_for_grading(lookback_days=5), d1


def _msg(buys, positions):
    with patch("anthropic.Anthropic"):
        agent = EveningAnalystAgent(api_key="test", model="claude-sonnet-4-6")
        return agent.build_user_message(
            positions=positions,
            macro_summary={"vix": {"current": 18}},
            total_value=100_000.0,
            daily_pnl=0.0,
            daily_return_pct=0.0,
            recent_buys=buys,
        )


def _line(msg, sym):
    return next(ln for ln in msg.splitlines() if f" {sym}: bought" in ln)


def test_sold_buy_shows_since_closed():
    buys, d1 = _buys_with_exit()
    line = _line(_msg(buys, []), "ZZZ")
    assert f"since closed {d1} at $92.00" in line


def test_held_buy_has_no_since_closed():
    buys, _ = _buys_with_exit()
    held = Position(
        symbol="ZZZB", qty=1, avg_entry=50, current_price=90, market_value=90, unrealized_pnl=0, sector="Tech"
    )
    msg = _msg(buys, [held])
    assert "since closed" not in _line(msg, "ZZZB")
    assert "since closed" in _line(msg, "ZZZ")
