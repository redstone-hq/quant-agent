"""Owner ruling 2026-10-08: the 30-minute intra check trails stops too.

The tick reuses the one existing deterministic trail; these drive the REAL
trail (`_apply_deterministic_trails`) through the tick wrapper against a mocked
broker, so tightening, mirroring and never-loosening are the trail's own
behaviour, not a re-implementation.
"""

from __future__ import annotations

import inspect
import logging
import math
from contextlib import contextmanager
from unittest.mock import MagicMock

from src.intraday import tick_trail
from src.intraday.session import IntradaySession
from src.intraday.tick_trail import trail_on_tick
from tests.test_phase3_exit_rework import _pipeline, _position


class _Bar:
    def __init__(self, hi, lo, d="2026-08-10"):
        self.high, self.low, self.date = hi, lo, d


#: The fixture `test_deterministic_trail_places_a_broker_order` already uses:
#: entry 100, price 125, stop 95 -> the trail proposes 110.
_LOWS = [110, 108, 106, 100, 106, 108, 110, 118, 116, 114, 110, 114, 116, 118, 125]


def _desk(*, short=False, stop=None):
    """A pipeline whose broker remembers the stop it was last moved to."""
    p = _pipeline()
    p.db.get_symbol_last_buy.return_value = {
        "setup_type": "breakout",
        "take_profit": None,
        "timestamp": "2026-08-01 14:00:00",
    }
    # The live holding's opening broker fill, 7 Aug: every 10 Aug bar is after it.
    p.db.get_position_chain_rows.return_value = [
        {
            "id": 1,
            "symbol": "BBB" if short else "AAA",
            "action": "SHORT" if short else "BUY",
            "qty": 10,
            "filled_at": "2026-08-07 14:00:00.000000",
        }
    ]
    live = {"stop": stop if stop is not None else (105.0 if short else 95.0)}
    p.broker.get_current_stop_price.side_effect = lambda *a, **k: live["stop"]

    def _replace(symbol, new_stop, **kwargs):
        live["stop"] = new_stop
        return {"id": "o1"}

    p.broker.replace_stop_loss.side_effect = _replace
    p.market = MagicMock()
    if short:  # the long fixture mirrored about the entry (100)
        p.market.get_ohlcv.return_value = [_Bar(200 - lo, 200 - lo - 2) for lo in _LOWS]
    else:
        p.market.get_ohlcv.return_value = [_Bar(lo + 2, lo) for lo in _LOWS]
    return p, live


@contextmanager
def _lock(held=True):
    yield held


def _tick(p, positions, **overrides):
    kwargs = dict(
        positions=positions,
        run_id="intra_check_t",
        preamble_deferred="",
        apply_deterministic_trails=p._apply_deterministic_trails,
        atr_for_symbol=p._atr_for_symbol,
        process_lock=_lock,
        blocking_owner_session=lambda: None,
        split_positions=None,
    )
    kwargs.update(overrides)
    return trail_on_tick(**kwargs)


def _long(price=125.0):
    return _position("AAA", qty=10, avg_entry=100.0, current_price=price)


def _short(price=75.0):
    return _position("BBB", qty=-10, avg_entry=100.0, current_price=price)


def test_tick_tightens_a_long():
    p, live = _desk()
    out = _tick(p, [_long()])
    assert out["status"] == "ran" and out["orders"] == 1
    assert live["stop"] == 110.0


def test_tick_tightens_a_short_as_the_mirror_of_the_long():
    p, live = _desk(short=True)
    out = _tick(p, [_short()])
    assert out["status"] == "ran" and out["orders"] == 1
    assert live["stop"] == 90.0


def test_tick_never_loosens_a_long_or_a_short():
    # Both stops sit tighter than anything the trail can propose here (the
    # chandelier's 121 / mirrored 79), so the only legal move is none.
    for short, stop in ((False, 124.0), (True, 76.0)):
        p, live = _desk(short=short, stop=stop)
        out = _tick(p, [_short() if short else _long()])
        assert out["orders"] == 0
        p.broker.replace_stop_loss.assert_not_called()
        assert live["stop"] == stop


def test_tick_is_idempotent_at_the_same_price():
    # Started at the trail's own fixed point for this tape (from 95 the
    # existing trail steps 110 then 121 on successive passes -- its ladder,
    # not this wrapper's), a second tick at the same price moves nothing.
    p, live = _desk(stop=110.0)
    assert _tick(p, [_long()])["orders"] == 1
    assert live["stop"] == 121.0
    assert _tick(p, [_long()])["orders"] == 0
    assert p.broker.replace_stop_loss.call_count == 1
    assert live["stop"] == 121.0


def test_unreadable_price_is_skipped_loudly_never_zero(caplog):
    p, _ = _desk()
    trail = MagicMock(return_value=[])
    with caplog.at_level(logging.WARNING, logger="src.pipeline"):
        unreadable = [MagicMock(symbol="AAA", current_price=v) for v in (None, math.nan, 0.0, "n/a")]
        out = _tick(p, unreadable, apply_deterministic_trails=trail)
    trail.assert_not_called()
    assert [s["reason"] for s in out["skipped"]] == ["price_unreadable"] * 4
    assert "price_unreadable" in caplog.text


def test_unreadable_atr_is_skipped_and_a_raised_read_keeps_its_traceback(caplog):
    p, _ = _desk()
    trail = MagicMock(return_value=[])

    def _atr(symbol):
        if symbol == "RAISE":
            raise RuntimeError("bars down")
        return None if symbol == "NONE" else 2.0

    good = _long()
    with caplog.at_level(logging.WARNING, logger="src.pipeline"):
        out = _tick(
            p, [_position("RAISE"), _position("NONE"), good], apply_deterministic_trails=trail, atr_for_symbol=_atr
        )
    assert {s["symbol"]: s["reason"] for s in out["skipped"]} == {"RAISE": "atr_read_raised", "NONE": "atr_unreadable"}
    trail.assert_called_once()
    assert trail.call_args[0][0] == [good]
    assert any(r.exc_info for r in caplog.records), "the swallowed exception lost its traceback"


def test_tick_defers_while_a_session_owns_the_desk_or_the_lock_is_taken():
    p, _ = _desk()
    for overrides in (
        {"preamble_deferred": "a live midday session owns the desk"},
        {"process_lock": lambda: _lock(False)},
        {"blocking_owner_session": lambda: "close"},
        {"blocking_owner_session": lambda: "unreadable"},
    ):
        assert _tick(p, [_long()], **overrides)["status"] == "deferred"
    p.broker.replace_stop_loss.assert_not_called()


def test_session_reads_the_trail_off_the_host_and_reports_unwired_honestly():
    class _State:
        def __init__(self, host):
            self.host = host

        def get(self, name):
            return getattr(self.host, name)

    host = MagicMock(
        spec=[
            "_apply_deterministic_trails",
            "_atr_for_symbol",
            "_intraday_scan_process_lock",
            "_blocking_owner_session",
        ]
    )
    wired = IntradaySession(state=_State(host))._tick_trail_collaborators()
    assert wired["apply_deterministic_trails"] is host._apply_deterministic_trails
    assert wired["split_positions"]([1]) == ([1], None)  # no sweeper -> passthrough
    bare = IntradaySession(state=_State(object()))._tick_trail_collaborators()
    out = trail_on_tick(positions=[_long()], run_id="r", **bare)
    assert out["status"] == "unavailable"


def test_midday_and_close_still_trail_exactly_as_before():
    """The review sessions call the trail directly, untouched by the tick wrapper."""
    from src.sessions import position_review_session as review

    src = inspect.getsource(review)
    assert "self._apply_deterministic_trails(review_positions, run_id=run_id)" in src
    assert "tick_trail" not in src
    assert "def _apply_deterministic_trails" not in inspect.getsource(tick_trail)
