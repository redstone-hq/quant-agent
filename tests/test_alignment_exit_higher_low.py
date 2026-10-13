"""The trend exit fires on a confirmed swing break, not a 3.0 ATR give-back.

Decided 2026-10-09: the exit is kept (it can fire before the 3.0 ATR
Chandelier trail), but its unmeasured give-back trigger is replaced by
structure. A long's trend is broken when price closes below the last
confirmed higher low, or when a lower swing low is confirmed; a short
mirrors both. Swings are the trailing stop's own (`_swing_lows` /
`_swing_highs`, `PIVOT_WINDOW` = 3 bars each side). Every test here fails on
main, where `check_alignment_exit` takes no `bars`.
"""

from datetime import date, timedelta
from types import SimpleNamespace

from src.risk.alignment_exit import CODE_EXIT, CODE_HOLD, CODE_NO_SWING, check_alignment_exit

ATR = 1.0
OPENED = date(2026, 1, 2)


def _bars(closes: list[float]) -> list[SimpleNamespace]:
    # Dated from OPENED: every swing here printed while the position was on.
    return [SimpleNamespace(date=OPENED + timedelta(days=i), low=c - 0.5, high=c + 0.5) for i, c in enumerate(closes)]


def _read(closes: list[float], *, is_short: bool = False, bars=None):
    return check_alignment_exit(
        thesis_invalid_if="close below the MA20" if not is_short else "close above the MA20",
        closes=closes,
        atr=ATR,
        is_short=is_short,
        bars=_bars(closes) if bars is None else bars,
        position_open_date=OPENED,
    )


# A rising tape with ONE dip to 105: a confirmed higher low (three rising
# bars on each side), the tape then runs to 120.
LONG = [100.0 + i for i in range(10)] + [105.0] + [111.0 + i for i in range(10)]
# The mirror: a falling tape with one pop to 95, a confirmed lower high.
SHORT = [100.0 - i for i in range(10)] + [95.0] + [89.0 - i for i in range(10)]


def test_long_closing_below_the_last_higher_low_exits() -> None:
    v = _read(LONG + [104.0])
    assert v.status == "EXIT" and v.code == CODE_EXIT
    assert v.last_mark is not None and v.last_mark.price == 104.5  # the dip bar's low
    assert "higher low" in v.reason
    assert v.band_atrs is None  # no give-back tolerance exists any more
    assert v.owner_reason and "not because price reached any target" in v.owner_reason


def test_long_giving_back_three_atr_but_holding_the_higher_low_does_not_exit() -> None:
    """From 120 to 110 is ten ATR off the high and far below the MA20-era
    marks: the deleted rule would have sold. The higher low (104.5) holds."""
    v = _read(LONG + [110.0])
    assert v.status == "HOLD" and v.code == CODE_HOLD
    assert v.last_mark is not None and v.last_mark.price == 104.5
    assert v.breach_atrs is not None and v.breach_atrs < 0  # still above it


def test_lower_swing_low_confirmed_exits_even_above_it() -> None:
    """The other half of a broken trend: a newer swing low BELOW the prior
    one, confirmed, while today's close sits above both."""
    second_dip = [121.0, 122.0, 123.0, 103.0, 124.0, 125.0, 126.0]
    v = _read(LONG + second_dip)
    assert v.status == "EXIT" and v.code == CODE_EXIT
    assert "lower low" in v.reason


def test_short_mirror() -> None:
    broken = _read(SHORT + [96.0], is_short=True)
    assert broken.status == "EXIT" and broken.code == CODE_EXIT
    assert broken.last_mark is not None and broken.last_mark.price == 95.5
    assert "lower high" in broken.reason
    held = _read(SHORT + [90.0], is_short=True)  # gave back 10 from the low, under 95.5
    assert held.status == "HOLD" and held.code == CODE_HOLD


def test_no_swing_reference_does_not_exit_and_is_recorded() -> None:
    """A straight climb never confirms a swing low. A close far below every
    mark still does not exit on this rule; the trailing stop protects."""
    climb = [100.0 + i for i in range(30)] + [60.0]
    v = _read(climb)
    assert v.status == "HOLD" and v.code == CODE_NO_SWING
    assert "no swing reference" in v.reason and "trailing stop" in v.reason
    none_given = _read(LONG + [104.0], bars=[])
    assert none_given.status == "HOLD" and none_given.code == CODE_NO_SWING
