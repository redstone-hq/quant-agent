"""The AI seats' memory of the desk's own record starts at the clean-record start.

Owner ruling 9 Oct: trades before 2026-10-12 13:30 UTC are not evidence. The
memory builders used guessed windows (21d, 14d, 45d); they now share the one
clean-record constant. Every test here seeds one row before and one after.
"""

import json
from datetime import datetime, timezone

from src.prompt_facts.review.blocked import ReviewBlocked
from src.prompt_facts.review.exits import ReviewExits
from src.storage.analytics.calibration import CLEAN_RECORD_START_UTC
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline

PRE = "2026-10-05"  # before the clean start
POST = "2026-10-13"  # after it
NOW = datetime(2027, 1, 31, 15, 0, tzinfo=timezone.utc)  # frozen clock, >45d after the start


class _FunnelDb:
    """Serves proposal rows filtered lexically on `since`, as the real query does."""

    def __init__(self, rows):
        self.rows = rows
        self.since = None

    def get_proposal_funnel_rows(self, since):
        self.since = since
        return {"evidence": [r for r in self.rows if r["timestamp"] >= since], "trades": []}


def _target(did, sym, ts):
    return {
        "kind": "target",
        "decision_id": did,
        "symbol": sym,
        "timestamp": ts,
        "evidence_json": json.dumps({"symbol": sym, "risk_allocation_pct": 1.0}),
    }


def test_blocked_window_starts_at_clean_start_and_shows_n_one():
    db = _FunnelDb([_target("d0", "ZZZ", f"{PRE} 14:00:00"), _target("d1", "ZZZ", f"{POST} 14:00:00")])
    out = ReviewBlocked(db=db)._build_blocked_proposals()
    assert db.since == CLEAN_RECORD_START_UTC.replace("T", " ")
    assert PRE not in out
    assert "ZZZ: n=1" in out  # a single proposal is visibly n=1, not a pattern
    assert POST in out


def test_blocked_shows_first_and_last_date_for_repeats():
    db = _FunnelDb([_target("d1", "ZZZ", f"{POST} 14:00:00"), _target("d2", "ZZZ", "2026-10-14 14:00:00")])
    out = ReviewBlocked(db=db)._build_blocked_proposals()
    assert "ZZZ: n=2" in out
    assert "first 2026-10-13, last 2026-10-14" in out


class _InsightDb:
    def __init__(self, rows):
        self.rows = rows

    def get_recent_insights(self, limit=7, since_date=None):
        return self.rows


def _miss(date):
    return {
        "date": date,
        "missed_opportunities_json": json.dumps(
            [{"symbol": "ZZZ", "miss_category": "trend_timing_miss", "theme_if_any": "t", "lesson": "l"}]
        ),
    }


def _grade(date):
    return {
        "date": date,
        "buy_grades_json": json.dumps(
            [{"symbol": "ZZZ", "grade": "wrong", "loss_root_cause": "chased", "pct_move_since_buy": -3.0}]
        ),
    }


def test_loss_pits_drop_pre_clean_rows_and_show_dates():
    rows = [_grade(POST), _grade("2026-10-14"), _grade(PRE), _grade("2026-10-04")]
    out = ReviewExits(db=_InsightDb(rows))._build_recent_loss_pits()
    assert "chased × 2" in out  # the two pre-clean rows are not counted
    assert PRE not in out and "2026-10-04" not in out
    assert POST in out


def test_missed_lessons_drop_pre_clean_rows_and_show_dates():
    rows = [_miss(POST), _miss("2026-10-20"), _miss(PRE), _miss("2026-10-04")]
    out = ReviewExits(db=_InsightDb(rows))._build_recent_missed_lessons()
    assert "ZZZ" in out
    assert PRE not in out and "2026-10-04" not in out
    assert "flagged 2026-10-13 to 2026-10-20" in out


class _ExitDb:
    def get_trades(self, limit=120):
        def row(day):
            return {
                "symbol": "ZZZ",
                "action": "SELL",
                "fill_status": "filled",
                "price": 10.0,
                "fill_price": 10.0,
                "timestamp": f"{day}T14:00:00+00:00",
            }

        return [row(POST), row(PRE)]


class _Broker:
    def get_latest_price(self, sym):
        return 11.0


def test_post_exit_reality_ignores_pre_clean_exits(monkeypatch):
    monkeypatch.setattr("src.trading_calendar.utc_now", lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    exits = ReviewExits(db=_ExitDb(), broker=_Broker(), sweeper=lambda: None, exit_audit_actions={"SELL"})
    out = exits._build_post_exit_reality()
    assert out["n"] == 1
    assert [m["date"] for m in out["worst"]] == [POST]


def _stamp_last_trade(db, ts):
    db.conn.execute("UPDATE trades SET timestamp = ? WHERE id = (SELECT MAX(id) FROM trades)", (ts,))
    db.conn.commit()


def _roundtrip(db, sym, open_day, close_day):
    db.insert_trade(sym, "BUY", 100, 50, "m", "r", stop_loss=45.0)
    _stamp_last_trade(db, f"{open_day} 14:00:00")
    db.insert_trade(sym, "SELL", 100, 55, "t", "r")
    _stamp_last_trade(db, f"{close_day} 14:00:00")


def test_clean_trade_closed_over_45_days_after_start_still_counts(tmp_path, monkeypatch):
    monkeypatch.setattr("src.storage.analytics.calibration.utc_now", lambda: NOW)
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    for sym in ("ZZZ", "ZZA", "ZZB"):
        _roundtrip(db, sym, "2026-10-13", "2026-11-20")  # closed 72 days before NOW
    _roundtrip(db, "ZZP", PRE, "2026-10-20")  # opened pre-clean: never counts
    note = build_pipeline(db=db)._build_calibration_note()
    assert "3 closed BUYs" in note
    assert "2026-10-12" in note
