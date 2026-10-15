"""The chandelier trail is not gated by the structure leg's ATR warm-up span.

LeBeau's chandelier: highest high since entry - k x ATR (k = 3.0). Its high is
read from COMPLETED daily bars dated strictly AFTER the entry day only — never
the fill price, never the entry day's own bar (it holds pre-fill prices), never
a forming bar — and it needs at least ONE such bar. ATR is the caller's current
14-day ATR. The entry day is passed explicitly (`entry_date`, the current
holding's opening broker fill day); unknown means no chandelier reading. Every test
states its entry day.
The structure leg keeps its gate: the ATR(14) warm-up its swing detector
needs (15 bars of history, 2026-10-15). Shorts mirror.

Numbers: entry 100, ATR 2, initial stop 2.5 ATR away (95 long / 105 short).
"""

from __future__ import annotations

from datetime import date, datetime

from src.data.technical import ATR_PERIOD
from src.models.analysis import OHLCV
from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    MIN_BARS_FOR_A_READING,
    TRAIL_CODE_NO_CANDIDATE,
    TRAIL_CODE_TOO_FEW_BARS,
    evaluate_trailing_stop,
)
from src.trading_calendar import ET

ENTRY = 100.0
ATR = 2.0
LONG_STOP = ENTRY - 2.5 * ATR  # 95
SHORT_STOP = ENTRY + 2.5 * ATR  # 105
# Mid-session on Tue 13 Oct 2026: the last completed daily bar is Mon 12 Oct.
MID_SESSION = datetime(2026, 10, 13, 14, 0, tzinfo=ET)


def _bar(d: date, low: float, high: float) -> OHLCV:
    return OHLCV(date=d, open=low, high=high, low=low, close=(low + high) / 2, volume=1000)


def _eval(*, cur, stop, bars, entry_date, qty=1.0, atr=ATR, as_of=None):
    kwargs = dict(
        symbol="TEST",
        setup_type="breakout",  # Type B: no range ratchets, trail only
        entry=ENTRY,
        current_price=cur,
        current_stop=stop,
        reference_target=None,
        bars=bars,
        atr=atr,
        qty=qty,
        entry_date=entry_date,
    )
    if as_of is not None:
        kwargs["as_of"] = as_of
    return evaluate_trailing_stop(**kwargs)


def test_k_is_three():
    assert CHANDELIER_ATR_MULTIPLE == 3.0
    assert MIN_BARS_FOR_A_READING == ATR_PERIOD + 1


# --- 0 bars after the entry day: no reading at all, even under a wide stop


def test_long_zero_bars_gives_no_reading_even_for_wide_stop():
    # 5-ATR stop: a fill-based reading (100 - 6 = 94) would tighten it. It must not.
    ev = _eval(entry_date=date(2026, 10, 5), cur=110.0, stop=90.0, bars=[])
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_TOO_FEW_BARS


def test_short_zero_bars_gives_no_reading_even_for_wide_stop():
    ev = _eval(entry_date=date(2026, 10, 5), cur=90.0, stop=110.0, bars=[], qty=-1.0)
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_TOO_FEW_BARS


def test_entry_day_bar_alone_gives_no_reading():
    # Its high 101 would read 95, tightening the 90 stop, if it counted.
    ev = _eval(entry_date=date(2026, 10, 6), cur=110.0, stop=90.0, bars=[_bar(date(2026, 10, 6), 99.0, 101.0)])
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_TOO_FEW_BARS
    ev = _eval(
        entry_date=date(2026, 10, 6), cur=90.0, stop=110.0, bars=[_bar(date(2026, 10, 6), 99.0, 101.0)], qty=-1.0
    )
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_TOO_FEW_BARS


def test_only_a_forming_bar_after_entry_day_gives_no_reading():
    bars = [_bar(date(2026, 10, 12), 99.5, 100.5), _bar(date(2026, 10, 13), 104.0, 110.0)]  # 2nd still forming
    assert _eval(entry_date=date(2026, 10, 12), cur=110.0, stop=90.0, bars=bars, as_of=MID_SESSION).proposal is None
    bars = [_bar(date(2026, 10, 12), 99.5, 100.5), _bar(date(2026, 10, 13), 90.0, 96.0)]
    assert (
        _eval(entry_date=date(2026, 10, 12), cur=90.0, stop=110.0, bars=bars, qty=-1.0, as_of=MID_SESSION).proposal
        is None
    )


# --- The entry-day bar's pre-fill spike is ignored -------------------------


def test_long_entry_day_pre_fill_spike_is_ignored():
    bars = [_bar(date(2026, 10, 5), 99.0, 120.0), _bar(date(2026, 10, 6), 99.0, 101.0)]
    ev = _eval(entry_date=date(2026, 10, 5), cur=110.0, stop=90.0, bars=bars)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 101.0 - 3 * ATR  # 95, not 120 - 6 = 114


def test_short_entry_day_pre_fill_spike_is_ignored():
    bars = [_bar(date(2026, 10, 5), 80.0, 101.0), _bar(date(2026, 10, 6), 99.0, 101.0)]
    ev = _eval(entry_date=date(2026, 10, 5), cur=90.0, stop=110.0, bars=bars, qty=-1.0)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 99.0 + 3 * ATR  # 105, not 80 + 6 = 86


# --- 1 bar after the entry day: the textbook chandelier may tighten a wide stop


def test_long_one_bar_tightens_wide_stop():
    bars = [_bar(date(2026, 10, 5), 99.0, 100.5), _bar(date(2026, 10, 6), 99.0, 101.0)]
    ev = _eval(entry_date=date(2026, 10, 5), cur=110.0, stop=90.0, bars=bars)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 101.0 - 3 * ATR  # 95: the bar high, not the fill


def test_short_one_bar_tightens_wide_stop():
    bars = [_bar(date(2026, 10, 5), 99.5, 101.0), _bar(date(2026, 10, 6), 99.0, 101.0)]
    ev = _eval(entry_date=date(2026, 10, 5), cur=90.0, stop=110.0, bars=bars, qty=-1.0)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 99.0 + 3 * ATR  # 105: the bar low, not the fill


# --- 2 bars after the entry day with a new extreme: the stop moves --------


def test_long_two_bars_new_high_moves_stop_up():
    bars = [
        _bar(date(2026, 10, 5), 99.0, 100.5),
        _bar(date(2026, 10, 6), 99.0, 101.0),
        _bar(date(2026, 10, 7), 101.0, 104.0),
    ]
    ev = _eval(entry_date=date(2026, 10, 5), cur=108.0, stop=LONG_STOP, bars=bars)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.source == "chandelier"
    assert ev.proposal.new_stop == 104.0 - 3 * ATR  # 98


def test_short_two_bars_new_low_moves_stop_down():
    bars = [
        _bar(date(2026, 10, 5), 99.5, 101.0),
        _bar(date(2026, 10, 6), 99.0, 101.0),
        _bar(date(2026, 10, 7), 96.0, 99.0),
    ]
    ev = _eval(entry_date=date(2026, 10, 5), cur=92.0, stop=SHORT_STOP, bars=bars, qty=-1.0)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.source == "chandelier"
    assert ev.proposal.new_stop == 96.0 + 3 * ATR  # 102


def test_long_reading_below_stop_never_loosens():
    bars = [_bar(date(2026, 10, 6), 99.0, 100.5), _bar(date(2026, 10, 7), 99.5, 100.5)]
    # Reading 100.5 - 6 = 94.5 is below the 95 stop: no move.
    assert _eval(entry_date=date(2026, 10, 6), cur=102.0, stop=LONG_STOP, bars=bars).proposal is None
    # Stop already tighter than the reading (98 vs 96): no move.
    bars = [_bar(date(2026, 10, 6), 99.0, 100.0), _bar(date(2026, 10, 7), 99.0, 102.0)]
    assert _eval(entry_date=date(2026, 10, 6), cur=106.0, stop=98.0, bars=bars).proposal is None


def test_short_reading_above_stop_never_loosens():
    bars = [_bar(date(2026, 10, 6), 99.5, 101.0), _bar(date(2026, 10, 7), 99.5, 101.0)]
    # Reading 99.5 + 6 = 105.5 is above the 105 stop: no move.
    assert _eval(entry_date=date(2026, 10, 6), cur=98.0, stop=SHORT_STOP, bars=bars, qty=-1.0).proposal is None
    # Stop already tighter than the reading (102 vs 104): no move.
    bars = [_bar(date(2026, 10, 6), 100.0, 101.0), _bar(date(2026, 10, 7), 98.0, 101.0)]
    assert _eval(entry_date=date(2026, 10, 6), cur=94.0, stop=102.0, bars=bars, qty=-1.0).proposal is None


# --- A forming bar is never used ------------------------------------------


def test_long_forming_bar_is_ignored():
    bars = [
        _bar(date(2026, 10, 9), 99.0, 101.0),
        _bar(date(2026, 10, 12), 100.0, 102.0),
        _bar(date(2026, 10, 13), 104.0, 110.0),  # today, still forming
    ]
    ev = _eval(entry_date=date(2026, 10, 9), cur=108.0, stop=LONG_STOP, bars=bars, as_of=MID_SESSION)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 102.0 - 3 * ATR  # 96, not 110 - 6


def test_short_forming_bar_is_ignored():
    bars = [
        _bar(date(2026, 10, 9), 99.0, 101.0),
        _bar(date(2026, 10, 12), 98.0, 100.0),
        _bar(date(2026, 10, 13), 90.0, 96.0),  # today, still forming
    ]
    ev = _eval(entry_date=date(2026, 10, 9), cur=92.0, stop=SHORT_STOP, bars=bars, qty=-1.0, as_of=MID_SESSION)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 98.0 + 3 * ATR  # 104, not 90 + 6


def test_after_close_todays_bar_is_completed_and_used():
    after_close = datetime(2026, 10, 13, 16, 30, tzinfo=ET)
    bars = [_bar(date(2026, 10, 12), 100.0, 102.0), _bar(date(2026, 10, 13), 104.0, 110.0)]
    ev = _eval(entry_date=date(2026, 10, 12), cur=112.0, stop=LONG_STOP, bars=bars, as_of=after_close)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 110.0 - 3 * ATR


# --- The structure leg is still gated at 7 --------------------------------

# 14 flat September bars first: the swing detector's ATR(14) warms up on
# history from before the entry (2026-10-15); the V itself is after entry.
_WARM_DATES = [date(2026, 9, d) for d in range(10, 24)]
_DATES = _WARM_DATES + [date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8, 9)]
_LONG_LOWS = [99.0] * 14 + [99.0, 98.0, 97.0, 96.5, 97.0, 98.0, 99.0]
_SHORT_HIGHS = [101.0] * 14 + [101.0, 102.0, 103.0, 103.5, 103.0, 102.0, 101.0]
_SHORT_OF_WARM_UP = MIN_BARS_FOR_A_READING - 1


def test_long_structure_gated_below_the_atr_warm_up():
    bars = [_bar(d, lo, lo + 2.0) for d, lo in zip(_DATES, _LONG_LOWS)]
    full = _eval(entry_date=date(2026, 10, 1), cur=108.0, stop=LONG_STOP, bars=bars, atr=None)
    assert full.proposal is not None and full.proposal.source == "structure"
    short = _eval(
        entry_date=date(2026, 10, 1), cur=108.0, stop=LONG_STOP, bars=bars[-_SHORT_OF_WARM_UP:], atr=None
    )
    assert short.proposal is None and short.code == TRAIL_CODE_TOO_FEW_BARS


def test_short_structure_gated_below_the_atr_warm_up():
    bars = [_bar(d, hi - 2.0, hi) for d, hi in zip(_DATES, _SHORT_HIGHS)]
    full = _eval(entry_date=date(2026, 10, 1), cur=92.0, stop=SHORT_STOP, bars=bars, qty=-1.0, atr=None)
    assert full.proposal is not None and full.proposal.source == "structure"
    short = _eval(
        entry_date=date(2026, 10, 1),
        cur=92.0,
        stop=SHORT_STOP,
        bars=bars[-_SHORT_OF_WARM_UP:],
        qty=-1.0,
        atr=None,
    )
    assert short.proposal is None and short.code == TRAIL_CODE_TOO_FEW_BARS


# --- The entry day comes from the caller, never from the bars ---------------


def test_unknown_entry_date_gives_no_chandelier_reading():
    # Three bars that would read 104 - 6 = 98 (long) / 96 + 6 = 102 (short).
    bars = [
        _bar(date(2026, 10, 5), 99.0, 100.5),
        _bar(date(2026, 10, 6), 99.0, 101.0),
        _bar(date(2026, 10, 7), 96.0, 104.0),
    ]
    for entry_date in (None, "", "not-a-date"):
        assert _eval(cur=108.0, stop=LONG_STOP, bars=bars, entry_date=entry_date).proposal is None
        assert _eval(cur=92.0, stop=SHORT_STOP, bars=bars, qty=-1.0, entry_date=entry_date).proposal is None


def test_entry_date_not_first_bar_sets_the_window():
    # Unsliced history: the caller's entry day (7 Oct), not the oldest bar,
    # decides which bars count. The 5 Oct spike (120) predates the holding.
    bars = [
        _bar(date(2026, 10, 5), 99.0, 120.0),
        _bar(date(2026, 10, 7), 99.0, 100.0),
        _bar(date(2026, 10, 8), 99.0, 101.0),
    ]
    ev = _eval(cur=110.0, stop=90.0, bars=bars, entry_date="2026-10-07T14:31:02+00:00")
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 101.0 - 3 * ATR  # 95: only the 8 Oct bar


# --- The caller's entry day: the CURRENT holding's opening broker fill -------


class _Owner:
    def __init__(self, rows):
        self.db = self
        self._rows = rows

    def get_position_chain_rows(self, _opening_row):
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


def _row(i, action, qty, filled_at):
    return {"id": i, "symbol": "TEST", "action": action, "qty": qty, "filled_at": filled_at}


def test_caller_entry_day_is_the_live_holdings_first_fill_in_et():
    from src.exits_parts.trails import _entry_fill_day

    rows = [
        _row(1, "BUY", 10, "2026-09-01 14:00:00.000000"),  # older, closed holding
        _row(2, "SELL", 10, "2026-09-05 15:00:00.000000"),
        _row(3, "BUY", 5, "2026-10-08 02:30:00.000000"),  # 22:30 ET on 7 Oct
        _row(4, "BUY", 5, "2026-10-09 14:00:00.000000"),  # a scale-in
    ]
    assert _entry_fill_day(_Owner(rows), {}, 10, "TEST") == date(2026, 10, 7)


def test_caller_entry_day_unknown_is_none_never_a_guess():
    from src.exits_parts.trails import _entry_fill_day

    assert _entry_fill_day(_Owner([_row(1, "BUY", 10, None)]), {}, 10, "TEST") is None
    assert _entry_fill_day(_Owner(None), {}, 10, "TEST") is None
    assert _entry_fill_day(_Owner(RuntimeError("db down")), {}, 10, "TEST") is None
    # Lots that do not reconcile to the broker's held quantity: unknown.
    assert _entry_fill_day(_Owner([_row(1, "BUY", 10, "2026-10-07 14:00:00")]), {}, 3, "TEST") is None


# --- One clock: both legs read "since entry" from the SAME entry day ---------


def test_structure_leg_uses_the_same_entry_day():
    bars = [_bar(d, lo, lo + 2.0) for d, lo in zip(_DATES, _LONG_LOWS)]
    # Entry 1 Oct: the 6 Oct swing low printed after it, the structure leg reads.
    assert _eval(entry_date=date(2026, 10, 1), cur=108.0, stop=LONG_STOP, bars=bars, atr=None).proposal is not None
    # Entry 7 Oct: the 6 Oct swing low predates the holding, so it is not
    # used, even though the caller handed the whole history in.
    ev = _eval(entry_date=date(2026, 10, 7), cur=108.0, stop=LONG_STOP, bars=bars, atr=None)
    assert ev.proposal is None and ev.code == TRAIL_CODE_NO_CANDIDATE
    # Unknown entry day: neither leg reads a guessed window.
    for atr in (None, ATR):
        ev = _eval(entry_date=None, cur=108.0, stop=LONG_STOP, bars=bars, atr=atr)
        assert ev.proposal is None and ev.code == TRAIL_CODE_TOO_FEW_BARS


def test_live_caller_slices_and_passes_one_entry_day():
    import inspect

    from src.exits_parts import trails

    src = inspect.getsource(trails)
    # The opening-record timestamp no longer sets the bar window.
    assert "get_position_open_timestamp" not in src
    assert src.count("_entry_fill_day(owner, buy, position.qty, symbol)") == 1
    assert "entry_date=entry_day," in src


# --- The backtest passes its simulated fill day, so its chandelier still moves


def test_backtest_passes_its_fill_day_and_the_chandelier_moves(monkeypatch):
    import src.backtest.engine as engine
    from tests.test_backtest import _build_long_win_series, _run

    real = engine.compute_trailing_stop
    calls = []

    def spy(**kwargs):
        proposal = real(**kwargs)
        calls.append((kwargs, proposal))
        return proposal

    monkeypatch.setattr(engine, "compute_trailing_stop", spy)
    result = _run(_build_long_win_series())[2]
    trades = result.trades
    assert trades, "fixture sanity: the long-win series must open a trade"
    assert calls, "fixture sanity: the trade must be held long enough to trail"
    for kwargs, _ in calls:
        assert kwargs["entry_date"] == trades[0].entry_date
    # The fixture is a range trade whose R ratchet answers first, so isolate
    # the chandelier on the engine's OWN bars, ATR and entry day: a Type B
    # trade with a wide stop and no R. With the engine's entry day it reads;
    # with none (what the backtest passed before this fix) it cannot.
    moved = unknown = 0
    for kwargs, _ in calls:
        probe = dict(kwargs, setup_type="breakout", risk_per_share=None, current_stop=kwargs["entry"] * 0.5)
        got = real(**probe)
        moved += got is not None and got.source == "chandelier"
        got = real(**dict(probe, entry_date=None))
        unknown += got is not None and got.source == "chandelier"
    assert moved > 0
    assert unknown == 0
