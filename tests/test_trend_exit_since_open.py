"""The trend exit's lower-low test counts only swings the POSITION lived through.

Defect (2026-10-13, VLO): the bars run over the whole lookback, so the two
most recent confirmed swing lows — 375.10 (14 Sep) and 364.96 (23 Sep) — both
printed BEFORE the first buy on 24 Sep. With price at ~436 the desk read
EXIT `alignment_exit_confirmed` and would have sold the whole position on its
first check. A trend the position never rode cannot break on it.

The fix: the lower-low (short: higher-high) test fires only when the NEWER
swing's bar date is on or after the position's OPEN (first opening fill since
flat, never the latest add); the close-through-the-last-swing test still reads
the full history; an unknown open never fires the lower-low test and says so.
Every test here calls the real functions and fails on main.
"""

from datetime import date, timedelta
from types import SimpleNamespace

from src.exits.alignment_exit import AlignmentExit
from src.models.analysis import OHLCV
from src.risk.alignment_exit import CODE_EXIT, check_alignment_exit
from src.trading_calendar import et_today

D0 = date(2026, 8, 3)

# VLO's shape. A decline prints swing low 374.5 (bar 5), then a LOWER swing
# low 364.5 (bar 13); the position opens on bar 14 and price rises straight
# to 436 with no swing confirmed since.
PRE = [400.0, 395, 390, 385, 380, 375, 380, 385, 390, 385, 380, 375, 370, 365]
RALLY = [370.0 + 3 * i for i in range(23)]  # 370 .. 436
VLO = PRE + RALLY
OPEN_BAR = len(PRE)  # the first bar after the lower low printed


def _ohlcv(closes: list[float]) -> list[OHLCV]:
    return [
        OHLCV(date=D0 + timedelta(days=i), open=c, high=c + 0.5, low=c - 0.5, close=c, volume=1_000_000)
        for i, c in enumerate(closes)
    ]


def _stamp(bar: int) -> str:
    return f"{D0 + timedelta(days=bar)} 14:30:00"


def _desk(closes: list[float], *, open_ts: str | None, last_buy_ts: str | None = None) -> AlignmentExit:
    """The live caller, with only its data sources stubbed."""
    bars = _ohlcv(closes)
    db = SimpleNamespace(
        get_symbol_last_buy=lambda sym, **kw: {"timestamp": last_buy_ts or open_ts or _stamp(0)},
        get_position_open_timestamp=lambda row: open_ts,
        get_target_revisions=lambda syms: {},
    )
    return AlignmentExit(
        alignment_exit_cached=None,
        structural_protection_for_holding=lambda **kw: SimpleNamespace(basis="intact"),
        config=SimpleNamespace(trading=SimpleNamespace(lookback_days=200)),
        db=db,
        market=SimpleNamespace(get_ohlcv=lambda sym, days: bars),
    )


def _live(closes: list[float], *, is_short: bool = False, **kw):
    return _desk(closes, **kw)._alignment_exit_for_holding(
        symbol="VLO",
        thesis_invalid_if="close above the MA20" if is_short else "close below the MA20",
        is_short=is_short,
        entry_price=None,
        stop_loss=None,
        run_id="r",
    )


def _pure(closes: list[float], *, opened, is_short: bool = False):
    bars = _ohlcv(closes)
    return check_alignment_exit(
        thesis_invalid_if="close above the MA20" if is_short else "close below the MA20",
        closes=closes,
        atr=1.0,
        is_short=is_short,
        bar_dates=[b.date for b in bars],
        bars=bars,
        position_open_date=opened,
    )


def test_a_lower_low_printed_before_the_open_does_not_sell_a_rising_position() -> None:
    v = _live(VLO, open_ts=_stamp(OPEN_BAR))
    assert v.status == "HOLD", v.reason
    assert "before the position opened" in v.reason


def test_b_a_lower_low_printed_after_the_open_still_exits() -> None:
    v = _pure(VLO, opened=D0)  # opened on bar 0: the position lived through both swings
    assert v.status == "EXIT" and v.code == CODE_EXIT
    assert "lower low" in v.reason


def test_c_closing_through_a_pre_open_higher_low_still_exits() -> None:
    """The close-through test reads the full history: a higher low printed
    before the open is still the line, and closing under it sells."""
    rise = [100.0 + i for i in range(10)] + [105.0] + [111.0 + i for i in range(10)]
    opened = D0 + timedelta(days=len(rise))  # after every swing in the tape
    v = _pure(rise + [104.0], opened=opened)
    assert v.status == "EXIT" and v.code == CODE_EXIT
    assert "higher low" in v.reason


def test_d_short_mirror_a_higher_high_before_the_open_does_not_cover() -> None:
    v = _live([800.0 - c for c in VLO], is_short=True, open_ts=_stamp(OPEN_BAR))
    assert v.status == "HOLD", v.reason
    assert "higher high" in v.reason and "before the position opened" in v.reason


def test_e_an_add_today_does_not_shield_a_position_opened_weeks_ago() -> None:
    desk = _desk(VLO, open_ts="2026-09-01 14:30:00", last_buy_ts=f"{et_today()} 14:30:00")
    assert desk._position_opened_today("VLO") is False
    fresh = _desk(VLO, open_ts=f"{et_today()} 14:30:00")
    assert fresh._position_opened_today("VLO") is True


def test_f_unknown_open_date_never_fires_the_lower_low_and_says_why() -> None:
    # The latest buy is bar 0, so even "latest buy" as a stand-in would fire.
    v = _live(VLO, open_ts=None, last_buy_ts=_stamp(0))
    assert v.status == "HOLD", v.reason
    assert "open date is unknown" in v.reason
