"""The evening AI's trade list must say whether a BUY opened or added, a SELL trimmed or exited
(21 Oct: an add to a held RSG was reported as "opening RSG")."""

from types import SimpleNamespace
from unittest.mock import patch

from src.agents.evening_analyst import EveningAnalystAgent
from src.sessions.evening_session import _label_held_before


def _msg(trades, positions):
    _label_held_before(trades, positions)
    with patch("anthropic.Anthropic"):
        agent = EveningAnalystAgent(api_key="test", model="claude-sonnet-4-6")
        return agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 18}},
            total_value=100_000.0,
            daily_pnl=0.0,
            daily_return_pct=0.0,
            today_trades=trades,
        )


def _trade(action, symbol, qty):
    return {"action": action, "symbol": symbol, "qty": qty, "price": 10.0, "reasoning": "r", "fill_qty": qty}


def _line(msg, symbol):
    return next(ln for ln in msg.splitlines() if f" {symbol}:" in ln and "shares @" in ln)


def test_buy_of_held_symbol_is_labelled_add():
    msg = _msg([_trade("BUY", "RSG", 5)], [SimpleNamespace(symbol="RSG", qty=15)])
    assert "add to a holding already held" in _line(msg, "RSG")


def test_buy_of_new_symbol_is_labelled_new_position():
    msg = _msg([_trade("BUY", "XYZ", 5)], [SimpleNamespace(symbol="XYZ", qty=5)])
    assert "new position" in _line(msg, "XYZ")


def test_sells_are_partial_or_full():
    msg = _msg(
        [_trade("SELL", "AAA", 4), _trade("SELL", "BBB", 6)],
        [SimpleNamespace(symbol="AAA", qty=6)],
    )
    assert "partial sale, still held" in _line(msg, "AAA")
    assert "full exit" in _line(msg, "BBB")


def test_unreconstructable_history_prints_no_label():
    msg = _msg([_trade("BUY", "RSG", 50)], [SimpleNamespace(symbol="RSG", qty=15)])
    assert "(" not in _line(msg, "RSG").split("—")[0]


def test_second_same_day_buy_of_a_new_name_is_an_add():
    # Newest first, as get_trades returns them: the 2nd buy (top) followed the 1st.
    trades = [_trade("BUY", "XYZ", 3), _trade("BUY", "XYZ", 5)]
    _label_held_before(trades, [SimpleNamespace(symbol="XYZ", qty=8)])
    assert trades[1]["held_before_qty"] == 0.0
    assert trades[0]["held_before_qty"] == 5.0


def test_no_labels_when_the_day_was_cut_short():
    trades = [_trade("BUY", "XYZ", 5)]
    _label_held_before(trades, [SimpleNamespace(symbol="XYZ", qty=5)], complete=False)
    assert "held_before_qty" not in trades[0]
