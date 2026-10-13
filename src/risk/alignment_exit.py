"""The ALIGNMENT EXIT — the desk's only sanctioned way to realise a gain.

Owner ruling, 2026-09-30: *"exit on ALIGNMENT, never on a target"*, and the
same day's correction: *"it doesn't have to be all three in alignment.
Structure, volatility and trend don't all have to agree. Just agree in
alignment. The chart alone can tell us what to do."*

So this is NOT a three-way vote and NOT a quorum. It is ONE READING.

  * STRUCTURE and TREND supply MARKS — prices the chart says matter. A
    confirmed-broken structural level is a mark. The moving average the
    position's own thesis names is a mark, as is the next longer average
    the desk already computes; when the thesis names no average the desk
    computes, the chart's OWN averages are the marks instead, so no
    position is left unreadable by how a model happened to word its
    thesis. Same kind of thing, read off the same
    bars, so they go into one set rather than into separate ballots.
  * VOLATILITY supplies the UNIT and the TOLERANCE. It is not a condition;
    it is how the single distance below the last mark is judged real.

ONE comparison decides, and it is STRUCTURE, not a give-back: the trend
is broken when the chart has confirmed it. For a long that is either the
most recent confirmed swing low printing BELOW the one before it (a lower
low), or a completed close BELOW the last confirmed higher low; a short
mirrors both against swing highs. The swings are the trailing stop's own
(`src.risk.trail_structure._swing_lows` / `_swing_highs`), confirmed with
its existing `src.risk.trailing.PIVOT_WINDOW` (3 bars each side). No new
number is introduced. When no swing is confirmed yet the rule does not
fire and says so ("no swing reference"); the trailing stop still protects.

THE LOWER LOW MUST BELONG TO THIS POSITION (2026-10-13). The bars run over
the whole lookback, so the two most recent swings can both predate the
entry: VLO's lower low (364.96 on 23 Sep, under 375.10 on 14 Sep) printed
the day before the first buy on 24 Sep, and with price at ~436 the desk
read EXIT on its first check. A trend the position never rode cannot break
on it. So the lower-low (higher-high) test fires only when the NEWER swing's
BAR DATE — the session it printed, not the one that confirmed it — is on or
after the position's OPEN date (first opening fill since flat, never the
latest add). The close-through-the-last-swing test is unchanged and still
reads the full history, so it works from day one. An unknown open date, or
an undated swing, does not fire the lower-low test and the reason says so:
the stop and the close-through test still protect.

The marks (structure and trend) are still read and recorded, because they
are what the review and the owner report show; they no longer decide.

It never exits on a price TARGET alone, never summarises the instrument's
past into a statistic, and is never anchored to what the desk paid. The
target is ONE VOTE (owner, 2026-10-08): it is read, recorded on every
verdict and counted, and it never sells alone. Since 2026-10-09 it no
longer picks a give-back mark, because there is no give-back mark.

WHY THE 3.0 ATR GIVE-BACK WAS DELETED (2026-10-09)
--------------------------------------------------
The old trigger sold when the close sat more than 3.0 x ATR(14) below the
last lost chart mark. That 3.0 was never measured: the published 2.5-3.5
band (Wilder 1978; Le Beau's Chandelier) is calibrated from a running
EXTREME on ATR(22), not from a moving average on ATR(14), so the citation
did not transfer, and its ledger row sat `arbitrary` waiting on a
measurement that was never run. The exit was KEPT, not deleted, because
it can fire before the 3.0 ATR Chandelier trail in at least four cases;
only its made-up trigger was replaced by the structure test above.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from src.risk.chart_averages import _finite, exponential_moving_average
from src.risk.chart_averages import moving_average, simple_moving_average
from src.risk.exit_guard import _MA_REF_RE
from src.risk.target_vote import target_vote
from src.risk.trail_structure import _swing_highs_dated, _swing_lows_dated
from src.risk.trailing import PIVOT_WINDOW

__all__ = [
    "AlignmentExitCheck",
    "CHART_MA_PERIODS",
    "SMA_LADDER",
    "ChartMark",
    "check_alignment_exit",
    "CODE_NO_SWING",
    "exponential_moving_average",
    "moving_average",
    "simple_moving_average",
    "thesis_ma_period",
    "thesis_ma_ref",
]


#: thesis-named period -> the next longer period the pipeline computes.
#: Not a tunable: 20/50/200 are the only averages computed anywhere
#: upstream, so "the next longer one" has exactly one answer for 20 and 50
#: and none for 200.
SMA_LADDER: dict[int, int] = {20: 50, 50: 200}

#: Every average the desk already computes upstream, derived from the
#: ladder above rather than written out again. These are the marks the
#: CHART supplies when the thesis prose names no average the desk can
#: compute — see `check_alignment_exit`. No new number: the set is exactly
#: the periods already named by `SMA_LADDER`.
CHART_MA_PERIODS: tuple[int, ...] = tuple(sorted(set(SMA_LADDER) | set(SMA_LADDER.values())))

#: Durable, machine-readable verdict codes. Every read writes one of these
#: per symbol, INCLUDING the states where the desk could not read the chart
#: — the closed first attempt recorded nothing on those paths, which left
#: the desk unable to tell a held position from an unreadable one.
CODE_EXIT = "alignment_exit_confirmed"
CODE_HOLD = "alignment_intact"
CODE_NO_MARK = "alignment_unreadable_no_mark"
CODE_NO_ATR = "alignment_unreadable_no_atr"
CODE_NO_CLOSES = "alignment_unreadable_no_closes"
#: No confirmed swing low (high, for a short) in the bars handed in, so the
#: structure test has nothing to break. A HOLD, recorded as its own code so
#: the desk can tell "trend intact" from "nothing to judge it against".
CODE_NO_SWING = "alignment_no_swing_reference"


@dataclass(frozen=True)
class ChartMark:
    """One price the chart says matters for this position, and where it
    came from."""

    price: float
    source: str


@dataclass(frozen=True)
class AlignmentExitCheck:
    """Verdict on whether the chart says this position's move is over.

    "EXIT"        - the chart confirmed the trend broken: a lower swing
                    low, or a close below the last confirmed higher low
                    (mirrored for a short).
    "HOLD"        - structure intact, or no confirmed swing to judge it
                    against (`CODE_NO_SWING`).
    "UNPARSEABLE" - no live mark, no ATR, or no closes. Treated as HOLD by
                    callers; a separate value so the desk can report the
                    true state instead of a silent hold.
    """

    status: Literal["EXIT", "HOLD", "UNPARSEABLE"]
    code: str
    marks: tuple[ChartMark, ...]
    last_mark: ChartMark | None
    breach_atrs: float | None
    #: Always None since 2026-10-09: the 3.0 ATR give-back tolerance was
    #: deleted. Kept so the stored reading and its readers stay unchanged.
    band_atrs: float | None
    reason: str
    #: The MA period PARSED OUT OF THE POSITION'S THESIS PROSE at the moment
    #: this verdict was formed, or None. Recorded because the prose is
    #: model-written and unversioned: a reworded thesis silently changes
    #: which price decides a sale, and without this field the record of the
    #: sale would not say which average actually decided it.
    thesis_ma_period: int | None = None
    #: "SMA" or "EMA" — WHICH KIND of average the thesis named, recorded
    #: because the two are different prices and the thesis only ever named
    #: one of them. "" when the thesis named no average.
    thesis_ma_kind: str = ""
    #: Verbatim thesis text the period was parsed from, truncated. Pins the
    #: input so a later reword is visible as a difference.
    thesis_text: str = ""
    sessions_since_mark_lost: int | None = None
    owner_reason: str | None = None
    #: Whether the target's vote applied (`src.risk.target_vote`), and the
    #: plain-words account: value, version, effective date — or why none.
    target_vote_applied: bool = False
    target_vote: str = ""

    @property
    def exit_cleared(self) -> bool:
        return self.status == "EXIT"


def thesis_ma_ref(thesis_invalid_if: str | None) -> tuple[int, str] | None:
    """The average this position's OWN thesis names: (period, "SMA"|"EMA").

    Same regex `exit_guard.check_thesis_invalid_if` reads, so the alignment
    exit and the invalidation check can never disagree about which average
    a position is riding. The KIND is read from the same match, because
    "close below the EMA50" and "close below the SMA50" are different
    prices and only one of them is the thesis.
    """
    m = _MA_REF_RE.search(thesis_invalid_if or "")
    if not m:
        return None
    period = int(m.group(1) or m.group(2))
    if period not in (20, 50, 200):
        return None
    return period, ("EMA" if "ema" in m.group(0).lower() else "SMA")


def thesis_ma_period(thesis_invalid_if: str | None) -> int | None:
    """The period of `thesis_ma_ref`, or None. See `thesis_ma_ref`."""
    ref = thesis_ma_ref(thesis_invalid_if)
    return ref[0] if ref else None


def _sessions_since_mark_lost(
    series: list[float],
    mark: ChartMark,
    ma: tuple[int, str] | None,
    *,
    is_short: bool,
) -> int:
    """How many completed sessions price has been on the wrong side of this
    mark.

    THE FIX FOR THE CLOSED ATTEMPT'S SIXTH DEFECT. That version walked
    historical closes backwards and compared every one of them against
    TODAY's moving-average value. An average that has fallen since is a
    lower bar than the one those closes actually faced, so closes that were
    above their own average at the time were counted as "already lost",
    inflating the count.

    For a moving-average mark, each close is compared against the average
    AS IT STOOD on that session. A structural level does not move, so it is
    compared against itself.
    """
    n = len(series)
    sessions = 0
    for i in range(n - 1, -1, -1):
        if ma is None:
            ref: float | None = mark.price
        else:
            ref = moving_average(series[: i + 1], ma[0], ma[1])
            if ref is None:
                break  # series too short to know; stop counting rather than guess
        close = series[i]
        held = (close <= ref) if is_short else (close >= ref)
        if held:
            break
        sessions += 1
    return sessions


def check_alignment_exit(
    *,
    thesis_invalid_if: str | None,
    closes: list[float],
    atr: float | None,
    broken_structural_level: float | None = None,
    is_short: bool = False,
    target: float | None = None,
    target_effective_date=None,
    target_version: str = "",
    bar_dates: list | None = None,
    bars: list | None = None,
    position_open_date=None,
) -> AlignmentExitCheck:
    """Read one position's chart and say whether its move is over.

    `closes` are that symbol's daily closes in ascending date order, ending
    on the latest COMPLETED session — the same close-based basis
    `pipeline._structural_protection_for_holding` insists on, because a
    level, an average and a swing all have to be read off a finished
    bar or a routine intraday wick reads as the end of a trend.

    `broken_structural_level` is the level `check_structural_protection`
    has already CONFIRMED broken for this position, if any. This function
    does not re-derive levels, touch counts or the cross-day confirmation
    gate; None simply means the chart offered no structural mark.

    `target`, `target_effective_date` and `target_version` are the CURRENT
    take-profit, when it took effect and which record set it; `bar_dates`
    run parallel to `closes`. See `src.risk.target_vote`: one vote, never
    a sale alone, and a missing target is today's rule, said so.

    `bars` are the same sessions as `closes`, as OHLCV objects; only their
    `low` / `high` are read, to find confirmed swings. None or too few bars
    means no swing reference and no exit on this rule.

    `position_open_date` is when the POSITION opened (first opening fill
    since flat — a date, datetime or ISO string). The lower-low / higher-high
    test fires only on a newer swing that printed on or after it; None means
    unknown, and an unknown open never fires that test (see module docstring).
    """
    a = _finite(atr)
    ref = thesis_ma_ref(thesis_invalid_if)
    fast = ref[0] if ref else None
    kind = ref[1] if ref else ""
    thesis_text = (thesis_invalid_if or "")[:400]
    series = [v for v in (_finite(c) for c in closes) if v is not None]
    if not series:
        return AlignmentExitCheck(
            "UNPARSEABLE",
            CODE_NO_CLOSES,
            (),
            None,
            None,
            None,
            "no completed closes to read the chart from",
            thesis_ma_period=fast,
            thesis_ma_kind=kind,
            thesis_text=thesis_text,
        )
    last = series[-1]

    # --- the marks the chart actually presents --------------------------
    marks: list[ChartMark] = []
    mark_periods: dict[str, tuple[int, str] | None] = {}
    lvl = _finite(broken_structural_level)
    if lvl is not None:
        m = ChartMark(lvl, "confirmed-broken structural level")
        marks.append(m)
        mark_periods[m.source] = None
    # THE CHART CAN SPEAK WITHOUT THE PROSE. When the thesis names an
    # average the desk computes, that average (of the KIND THE THESIS
    # NAMED) and the next longer one on the ladder are the marks — the
    # position's own stated line comes first. When the thesis names no
    # average, or names one the desk does not compute, the chart still
    # supplies its own: the simple averages the pipeline already computes
    # for every name. Coverage is therefore NOT set by how a model worded
    # its thesis; prose only chooses WHICH average, never WHETHER there is
    # one. No new number is introduced — `CHART_MA_PERIODS` is derived
    # from the same ladder.
    if fast is not None:
        ma_pairs: tuple[tuple[int | None, str], ...] = (
            (fast, kind),
            (SMA_LADDER.get(fast), "SMA"),
        )
    else:
        ma_pairs = tuple((p, "SMA") for p in CHART_MA_PERIODS)
    for period, k in ma_pairs:
        if period is None:
            continue
        v = moving_average(series, period, k)
        if v is not None:
            label = (
                f"{k}{period} (thesis rides the {kind}{fast})"
                if fast is not None
                else f"SMA{period} (the chart's own average — the thesis named none the desk computes)"
            )
            m = ChartMark(v, label)
            marks.append(m)
            mark_periods[m.source] = (period, k)

    if not marks:
        return AlignmentExitCheck(
            "UNPARSEABLE",
            CODE_NO_MARK,
            (),
            None,
            None,
            None,
            "the chart presents no mark for this position — no confirmed "
            "structural break, and too few closes for any average the desk "
            "computes — so there is nothing to read; refusing to invent one",
            thesis_ma_period=fast,
            thesis_ma_kind=kind,
            thesis_text=thesis_text,
        )
    if a is None or a <= 0:
        return AlignmentExitCheck(
            "UNPARSEABLE",
            CODE_NO_ATR,
            tuple(marks),
            None,
            None,
            None,
            "no ATR for this name — the distance through a swing cannot be stated in this name's own unit",
            thesis_ma_period=fast,
            thesis_ma_kind=kind,
            thesis_text=thesis_text,
        )

    reached, vote = target_vote(
        series,
        bar_dates,
        target,
        target_effective_date,
        target_version,
        is_short=is_short,
    )
    common = dict(
        thesis_ma_period=fast,
        thesis_ma_kind=kind,
        thesis_text=thesis_text,
        target_vote_applied=bool(reached),
        target_vote=vote,
    )
    return _structure_verdict(
        series,
        bars,
        a,
        tuple(marks),
        common,
        is_short=is_short,
        vote=vote,
        position_open_date=position_open_date,
    )


def _as_date(value) -> date | None:
    """A calendar date from a date, datetime, pandas Timestamp or ISO string; None if unreadable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_date = getattr(value, "date", None)
    if callable(to_date):
        try:
            got = to_date()
        except Exception:  # noqa: BLE001 — unreadable is None, never a guess
            return None
        return got if isinstance(got, date) else None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _lower_swing_since_open(
    dated: list[tuple[object, float]],
    position_open_date,
    *,
    is_short: bool,
) -> tuple[bool, str]:
    """Did the position live through a confirmed lower low (higher high)?

    Returns (fires, note). It fires only when the newer of the two latest
    swings is beyond the older AND printed on or after the position's open.
    The note explains every case where the test was not allowed to fire.
    """
    if len(dated) < 2:
        return False, ""
    (newer_on, newer), (_, older) = dated[-1], dated[-2]
    name = "higher high" if is_short else "lower low"
    opened = _as_date(position_open_date)
    if opened is None:
        return False, (
            f"the {name} test was not evaluated: the position's open date is unknown, so it "
            f"cannot be told whether the position lived through that swing"
        )
    beyond = newer > older if is_short else newer < older
    if not beyond:
        return False, ""
    printed = _as_date(newer_on)
    if printed is None:
        return False, f"the {name} test was not evaluated: the newer swing's bar carries no date"
    if printed < opened:
        return False, (
            f"the {name} {newer:.4f} ({'over' if is_short else 'under'} {older:.4f}) printed "
            f"{printed}, before the position opened {opened}, so it is not this position's trend break"
        )
    return True, ""


def _structure_verdict(
    series: list[float],
    bars: list | None,
    a: float,
    marks: tuple[ChartMark, ...],
    common: dict,
    *,
    is_short: bool,
    vote: str,
    position_open_date=None,
) -> AlignmentExitCheck:
    """The structure test on its own: EXIT on a confirmed swing break, HOLD
    otherwise, and HOLD with `CODE_NO_SWING` when nothing is confirmed."""
    last = series[-1]
    side = "high" if is_short else "low"
    dated = (_swing_highs_dated if is_short else _swing_lows_dated)(bars or [])
    pivots = [price for _, price in dated]
    if not pivots:
        return AlignmentExitCheck(
            "HOLD",
            CODE_NO_SWING,
            marks,
            None,
            None,
            None,
            f"no swing reference — no swing {side} is confirmed ({PIVOT_WINDOW} bars "
            f"each side) in the {len(bars or [])} bar(s) read, so structure cannot "
            f"call the trend broken; the trailing stop still protects; {vote}",
            **common,
        )

    # THE STRUCTURE TEST. The most recent confirmed swing is the reference;
    # the trend is broken when it printed beyond the one before it (a lower
    # low / higher high) ON OR AFTER THE POSITION OPENED, or when the latest
    # close has gone through it (full history — that test needs no date).
    latest = pivots[-1]
    previous = pivots[-2] if len(pivots) > 1 else None
    swing_broken, note = _lower_swing_since_open(dated, position_open_date, is_short=is_short)
    vote = f"{note}; {vote}" if note else vote
    closed_through = (last > latest) if is_short else (last < latest)
    ref = ChartMark(latest, f"last confirmed swing {side}")
    breach = (last - latest) if is_short else (latest - last)
    breach_atrs = breach / a
    if not (swing_broken or closed_through):
        return AlignmentExitCheck(
            "HOLD",
            CODE_HOLD,
            marks,
            ref,
            breach_atrs,
            None,
            f"structure intact — close {last:.4f} is still "
            f"{'below' if is_short else 'above'} the last confirmed swing {side} "
            f"{latest:.4f} and no {'higher high' if is_short else 'lower low'} is "
            f"confirmed since the position opened; {vote}",
            **common,
        )
    if swing_broken:
        why = (
            f"the latest confirmed swing {side} {latest:.4f} is "
            f"{'above' if is_short else 'below'} the one before it ({previous:.4f}) "
            f"— a {'higher high' if is_short else 'lower low'}"
        )
    else:
        why = (
            f"close {last:.4f} is {'above' if is_short else 'below'} the last "
            f"confirmed {'lower high' if is_short else 'higher low'} {latest:.4f}"
        )
    sessions = _sessions_since_mark_lost(series, ref, None, is_short=is_short)
    return AlignmentExitCheck(
        "EXIT",
        CODE_EXIT,
        marks,
        ref,
        breach_atrs,
        None,
        f"trend broken on structure: {why} ({breach_atrs:.2f} ATR from it, {sessions} session(s) through it); {vote}",
        sessions_since_mark_lost=sessions,
        owner_reason=(
            f"Trend over on the chart: {why}. Selling because the structure "
            f"the position was riding has broken, not because price reached "
            f"any target."
        ),
        **common,
    )
