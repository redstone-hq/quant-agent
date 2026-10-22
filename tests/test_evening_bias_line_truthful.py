"""The evening message's bias line must name who really reads the bias."""

from tests.test_trader_feed import _evening_result, _insert_position, _make_db

from src import trader_feed


def test_bias_line_names_true_consumer_not_morning_decisions(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    _insert_position(db, "NVDA")
    msg = trader_feed.format_session_result("evening", _evening_result(), 41.6)

    assert "Leaning bullish" in msg
    assert "decisions start from this" not in msg
    assert "AI that reviews held stocks" in msg
    assert "the buying AI does not see it" in msg
