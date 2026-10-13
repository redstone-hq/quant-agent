"""The chandelier trail is not gated by the structure leg's 7-bar pivot span.

LeBeau's chandelier: highest high since entry - k x ATR (k = 3.0). Its high is
read from COMPLETED daily bars since entry only — never the fill price, never a
forming bar — and it needs at least ONE such bar.
The structure leg keeps its 7-bar gate. Shorts mirror.

Numbers: entry 100, ATR 2, initial stop 2.5 ATR away (95 long / 105 short).
"""

from __future__ import annotations

from datetime import date, datetime

from src.models.analysis import OHLCV
from src.risk.trailing import (
    CHANDELIER_ATR_MULTIPLE,
    MIN_BARS_FOR_A_READING,
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


def _eval(*, cur, stop, bars, qty=1.0, atr=ATR, as_of=None):
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
        initial_stop=stop,
    )
    if as_of is not None:
        kwargs["as_of"] = as_of
    return evaluate_trailing_stop(**kwargs)


def test_k_is_three():
    assert CHANDELIER_ATR_MULTIPLE == 3.0
    assert MIN_BARS_FOR_A_READING == 7


# --- 0 completed bars: no reading at all, even under a wide stop ---------


def test_long_zero_bars_gives_no_reading_even_for_wide_stop():
    # 5-ATR stop: a fill-based reading (100 - 6 = 94) would tighten it. It must not.
    ev = _eval(cur=110.0, stop=90.0, bars=[])
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_TOO_FEW_BARS


def test_short_zero_bars_gives_no_reading_even_for_wide_stop():
    ev = _eval(cur=90.0, stop=110.0, bars=[], qty=-1.0)
    assert ev.proposal is None
    assert ev.code == TRAIL_CODE_TOO_FEW_BARS


def test_only_a_forming_bar_gives_no_reading():
    bars = [_bar(date(2026, 10, 13), 104.0, 110.0)]  # today, still forming
    assert _eval(cur=110.0, stop=90.0, bars=bars, as_of=MID_SESSION).proposal is None
    bars = [_bar(date(2026, 10, 13), 90.0, 96.0)]
    assert _eval(cur=90.0, stop=110.0, bars=bars, qty=-1.0, as_of=MID_SESSION).proposal is None


# --- 1 completed bar: the textbook chandelier may tighten a wide stop ----


def test_long_one_bar_tightens_wide_stop():
    bars = [_bar(date(2026, 10, 6), 99.0, 101.0)]
    ev = _eval(cur=110.0, stop=90.0, bars=bars)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 101.0 - 3 * ATR  # 95: the bar high, not the fill


def test_short_one_bar_tightens_wide_stop():
    bars = [_bar(date(2026, 10, 6), 99.0, 101.0)]
    ev = _eval(cur=90.0, stop=110.0, bars=bars, qty=-1.0)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 99.0 + 3 * ATR  # 105: the bar low, not the fill


# --- 2 completed bars with a new extreme: the stop moves -----------------


def test_long_two_bars_new_high_moves_stop_up():
    bars = [_bar(date(2026, 10, 6), 99.0, 101.0), _bar(date(2026, 10, 7), 101.0, 104.0)]
    ev = _eval(cur=108.0, stop=LONG_STOP, bars=bars)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.source == "chandelier"
    assert ev.proposal.new_stop == 104.0 - 3 * ATR  # 98


def test_short_two_bars_new_low_moves_stop_down():
    bars = [_bar(date(2026, 10, 6), 99.0, 101.0), _bar(date(2026, 10, 7), 96.0, 99.0)]
    ev = _eval(cur=92.0, stop=SHORT_STOP, bars=bars, qty=-1.0)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.source == "chandelier"
    assert ev.proposal.new_stop == 96.0 + 3 * ATR  # 102


def test_long_reading_below_stop_never_loosens():
    bars = [_bar(date(2026, 10, 6), 99.0, 100.5), _bar(date(2026, 10, 7), 99.5, 100.5)]
    # Reading 100.5 - 6 = 94.5 is below the 95 stop: no move.
    assert _eval(cur=102.0, stop=LONG_STOP, bars=bars).proposal is None
    # Stop already tighter than the reading (98 vs 96): no move.
    bars = [_bar(date(2026, 10, 6), 99.0, 102.0)]
    assert _eval(cur=106.0, stop=98.0, bars=bars).proposal is None


def test_short_reading_above_stop_never_loosens():
    bars = [_bar(date(2026, 10, 6), 99.5, 101.0), _bar(date(2026, 10, 7), 99.5, 101.0)]
    # Reading 99.5 + 6 = 105.5 is above the 105 stop: no move.
    assert _eval(cur=98.0, stop=SHORT_STOP, bars=bars, qty=-1.0).proposal is None
    bars = [_bar(date(2026, 10, 6), 98.0, 101.0)]
    assert _eval(cur=94.0, stop=102.0, bars=bars, qty=-1.0).proposal is None


# --- A forming bar is never used ------------------------------------------


def test_long_forming_bar_is_ignored():
    bars = [
        _bar(date(2026, 10, 9), 99.0, 101.0),
        _bar(date(2026, 10, 12), 100.0, 102.0),
        _bar(date(2026, 10, 13), 104.0, 110.0),  # today, still forming
    ]
    ev = _eval(cur=108.0, stop=LONG_STOP, bars=bars, as_of=MID_SESSION)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 102.0 - 3 * ATR  # 96, not 110 - 6


def test_short_forming_bar_is_ignored():
    bars = [
        _bar(date(2026, 10, 9), 99.0, 101.0),
        _bar(date(2026, 10, 12), 98.0, 100.0),
        _bar(date(2026, 10, 13), 90.0, 96.0),  # today, still forming
    ]
    ev = _eval(cur=92.0, stop=SHORT_STOP, bars=bars, qty=-1.0, as_of=MID_SESSION)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 98.0 + 3 * ATR  # 104, not 90 + 6


def test_after_close_todays_bar_is_completed_and_used():
    after_close = datetime(2026, 10, 13, 16, 30, tzinfo=ET)
    bars = [_bar(date(2026, 10, 12), 100.0, 102.0), _bar(date(2026, 10, 13), 104.0, 110.0)]
    ev = _eval(cur=112.0, stop=LONG_STOP, bars=bars, as_of=after_close)
    assert ev.proposal is not None, ev.code
    assert ev.proposal.new_stop == 110.0 - 3 * ATR


# --- The structure leg is still gated at 7 --------------------------------

_DATES = [date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8, 9)]
_LONG_LOWS = [99.0, 98.0, 97.0, 96.5, 97.0, 98.0, 99.0]
_SHORT_HIGHS = [101.0, 102.0, 103.0, 103.5, 103.0, 102.0, 101.0]


def test_long_structure_gated_below_seven():
    bars = [_bar(d, lo, lo + 2.0) for d, lo in zip(_DATES, _LONG_LOWS)]
    seven = _eval(cur=108.0, stop=LONG_STOP, bars=bars, atr=None)
    assert seven.proposal is not None and seven.proposal.source == "structure"
    six = _eval(cur=108.0, stop=LONG_STOP, bars=bars[:6], atr=None)
    assert six.proposal is None and six.code == TRAIL_CODE_TOO_FEW_BARS


def test_short_structure_gated_below_seven():
    bars = [_bar(d, hi - 2.0, hi) for d, hi in zip(_DATES, _SHORT_HIGHS)]
    seven = _eval(cur=92.0, stop=SHORT_STOP, bars=bars, qty=-1.0, atr=None)
    assert seven.proposal is not None and seven.proposal.source == "structure"
    six = _eval(cur=92.0, stop=SHORT_STOP, bars=bars[:6], qty=-1.0, atr=None)
    assert six.proposal is None and six.code == TRAIL_CODE_TOO_FEW_BARS
