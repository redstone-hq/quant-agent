"""The price target is ONE VOTE toward the alignment exit (owner, 2026-10-08).

"Can tip a close when other signals agree. But never sells alone."

Coordinator ruling, 2026-10-09: the 3.0 ATR give-back was deleted and the
exit fires only on a confirmed swing break, so there is no give-back mark
left for the target to choose. The vote is still READ, RECORDED on every
verdict and COUNTED (`target_vote_applied`); it never sells alone and it
does not change the structure verdict either way.
"""

import sqlite3
import threading
import types
from datetime import date, timedelta

from src.exits.alignment_exit import AlignmentExit
from src.risk.alignment_exit import CODE_EXIT, CODE_HOLD, check_alignment_exit
from src.risk.target_vote import target_reached
from src.storage.target_revisions import build_target_revision_records

ATR = 1.0


def _dates(n: int) -> list[date]:
    return [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]


def _bars(closes: list[float]) -> list[types.SimpleNamespace]:
    return [types.SimpleNamespace(low=c - 0.5, high=c + 0.5, close=c) for c in closes]


# LONG: a rising tape with ONE dip to 105 (a confirmed higher low, the
# bounce clears one ATR), last close 120 — structure intact. BROKEN adds a
# close at 104, below that higher low. SHORT / SHORT_BROKEN mirror it.
# 14 flat bars first: swings are a reversal of the stock's own ATR(14)
# (2026-10-15), which needs that warm-up.
WARM = [100.0] * 14
LONG = WARM + [100.0 + i for i in range(10)] + [105.0] + [111.0 + i for i in range(10)]
LONG_BROKEN = LONG + [104.0]
SHORT = WARM + [100.0 - i for i in range(10)] + [95.0] + [89.0 - i for i in range(10)]
SHORT_BROKEN = SHORT + [96.0]


def _read(closes, *, is_short=False, **kw):
    return check_alignment_exit(
        thesis_invalid_if=None,
        closes=closes,
        atr=ATR,
        is_short=is_short,
        bar_dates=_dates(len(closes)),
        bars=_bars(closes),
        **kw,
    )


def test_no_target_is_recorded_loudly_and_structure_decides() -> None:
    for v in (_read(LONG), _read(SHORT, is_short=True)):
        assert v.status == "HOLD" and v.code == CODE_HOLD
        assert v.target_vote_applied is False
        assert "NOT APPLIED, no target to read" in v.target_vote
        assert v.target_vote in v.reason


def test_reached_target_is_recorded_and_counted_but_never_sells_alone() -> None:
    long_v = _read(LONG, target=115.0, target_effective_date=date(2026, 1, 1), target_version="entry record")
    short_v = _read(
        SHORT, is_short=True, target=85.0, target_effective_date=date(2026, 1, 1), target_version="entry record"
    )
    for v in (long_v, short_v):
        assert v.status == "HOLD" and v.code == CODE_HOLD  # reached — and still a hold
        assert v.target_vote_applied is True  # counted
        assert "APPLIED" in v.target_vote and "entry record" in v.target_vote
        assert "2026-01-01" in v.target_vote
        assert v.target_vote in v.reason  # recorded


def test_the_vote_never_changes_a_structure_verdict() -> None:
    """With or without a reached target, the swing break alone decides."""
    for closes, short, tgt in ((LONG_BROKEN, False, 115.0), (SHORT_BROKEN, True, 85.0)):
        bare = _read(closes, is_short=short)
        voted = _read(closes, is_short=short, target=tgt, target_effective_date=date(2026, 1, 1))
        assert bare.status == voted.status == "EXIT" and voted.code == CODE_EXIT
        assert voted.target_vote_applied is True and bare.target_vote_applied is False
        assert (bare.last_mark, bare.breach_atrs) == (voted.last_mark, voted.breach_atrs)
        assert voted.owner_reason and "not because price reached any target" in voted.owner_reason
    for closes, short, tgt in ((LONG, False, 115.0), (SHORT, True, 85.0)):
        bare = _read(closes, is_short=short)
        voted = _read(closes, is_short=short, target=tgt, target_effective_date=date(2026, 1, 1))
        assert bare.status == voted.status == "HOLD"


def test_missing_target_is_loud_and_otherwise_unchanged() -> None:
    today = _read(LONG)
    missing = _read(LONG, target=None, target_version="no target on the opening row")
    assert (today.status, today.last_mark, today.breach_atrs) == (
        missing.status,
        missing.last_mark,
        missing.breach_atrs,
    )
    assert "NOT APPLIED, no target to read (no target on the opening row)" in missing.reason
    undated = _read(LONG, target=115.0, target_effective_date=None)
    assert undated.status == "HOLD" and "no dates" in undated.target_vote


def test_revision_after_the_reach_resets_reached() -> None:
    """A target revised on the last session was never reached since it
    took effect, so the vote does not apply even though earlier closes
    stood beyond it."""
    pulled_long, pulled_short = LONG + [118.0], SHORT + [82.0]  # 120 / 80 stood beyond earlier
    last_day = _dates(len(pulled_long))[-1]
    v = _read(pulled_long, target=119.5, target_effective_date=last_day, target_version="applied revision")
    assert v.status == "HOLD" and v.target_vote_applied is False
    assert "not reached" in v.target_vote
    s = _read(pulled_short, is_short=True, target=80.5, target_effective_date=last_day)
    assert s.status == "HOLD" and s.target_vote_applied is False


def test_target_reached_is_close_only_on_or_after_the_effective_date() -> None:
    closes = [100.0, 118.0, 105.0]
    days = _dates(3)
    assert target_reached(closes, days, 118.0, days[0]) is True
    assert target_reached(closes, days, 118.0, days[1]) is True  # on the date
    assert target_reached(closes, days, 118.0, days[2]) is False
    assert target_reached(closes, days, 118.01, days[0]) is False  # no tolerance
    assert target_reached(closes, days, 100.0, days[0], is_short=True) is True
    assert target_reached(closes, days, 99.0, days[0], is_short=True) is False
    assert target_reached(closes, days, None, days[0]) is None
    assert target_reached(closes, [], 118.0, days[0]) is None
    assert target_reached(closes, days, 118.0, "2026-01-02 15:00:00") is True


# --- the caller: which target, and since when ---------------------------


def _exit(db) -> AlignmentExit:
    return AlignmentExit(
        alignment_exit_cached=None,
        structural_protection_for_holding=None,
        config=None,
        db=db,
        market=None,
    )


def _db(row, revisions=None, open_ts=None):
    return types.SimpleNamespace(
        get_symbol_last_buy=lambda sym, action="BUY": row,
        get_position_open_timestamp=lambda r: open_ts,
        get_target_revisions=lambda syms, **kw: {"AAA": revisions or []},
    )


def test_caller_dates_the_target_from_entry_or_the_latest_applied_revision() -> None:
    row = {"take_profit": 118.0, "timestamp": "2026-01-05 15:00:00"}
    t, eff, ver = _exit(_db(row))._target_for_holding(symbol="AAA", is_short=False)
    assert (t, eff) == (118.0, "2026-01-05") and "entry record" in ver
    # the position's OWN open (a scale-in does not move it) wins over the last buy
    t, eff, _ = _exit(_db(row, open_ts="2026-01-02T10:00:00"))._target_for_holding(
        symbol="AAA",
        is_short=False,
    )
    assert eff == "2026-01-02"
    revs = [
        {"timestamp": "2026-01-09 16:00:00", "applied": False, "new_price": 130.0},
        {
            "timestamp": "2026-01-08 16:00:00",
            "applied": True,
            "new_price": 118.0,
            "run_id": "r8",
            "code": "TRIGGER_X",
            "prior_price": 110.0,
        },
        {"timestamp": "2026-01-01 16:00:00", "applied": True, "new_price": 90.0},
    ]
    t, eff, ver = _exit(_db(row, revs))._target_for_holding(symbol="AAA", is_short=False)
    assert (t, eff) == (118.0, "2026-01-08")
    assert "applied revision of 2026-01-08" in ver and "r8" in ver
    # only a revision filed BEFORE this open exists: it belonged to an earlier trade
    t, eff, ver = _exit(_db(row, revs[2:]))._target_for_holding(symbol="AAA", is_short=False)
    assert eff == "2026-01-05" and "entry record" in ver


def test_caller_reports_a_missing_or_unreadable_target_loudly() -> None:
    assert _exit(_db({"timestamp": "2026-01-05"}))._target_for_holding(
        symbol="AAA",
        is_short=False,
    ) == (None, None, "no target on the opening row")
    boom = types.SimpleNamespace(
        get_symbol_last_buy=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")),
    )
    t, eff, why = _exit(boom)._target_for_holding(symbol="AAA", is_short=True)
    assert t is None and eff is None and "target read failed" in why


def test_revision_limit_is_per_symbol() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE specialist_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_id TEXT, decision_id TEXT, agent_name TEXT, kind TEXT, scope TEXT, "
        "symbol TEXT, evidence_json TEXT, "
        "timestamp TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    revs = build_target_revision_records(conn=conn, lock=threading.Lock())
    for i in range(3):
        revs.record_target_revision(run_id=f"a{i}", symbol="AAA", code=f"A{i}", seat="risk", evidence="e")
    revs.record_target_revision(run_id="b", symbol="BBB", code="B0", seat="risk", evidence="e")
    out = revs.get_target_revisions(["AAA", "BBB", "aaa"], limit=2)
    assert len(out["AAA"]) == 2 and out["AAA"][0]["code"] == "A2"
    assert [r["code"] for r in out["BBB"]] == ["B0"]  # not crowded out
