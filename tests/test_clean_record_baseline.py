"""Total P&L baseline starts at the clean record (owner ruling 2026-10-09)
and the dashboard and Telegram readers agree on the same rows."""

from __future__ import annotations

import pytest

from src.api import db_reads
from src.clean_record_baseline import CLEAN_RECORD_DATE, clean_record_baseline
from src.storage.analytics.calibration import CLEAN_RECORD_START_UTC
from src.storage.db import Database


def _row(date, total_value, daily_pnl, equity_close=None):
    return {"date": date, "total_value": total_value, "daily_pnl": daily_pnl, "equity_close": equity_close}


def _seed(db, rows):
    for r in rows:
        db.insert_daily_pnl(
            date=r["date"],
            total_value=r["total_value"],
            daily_pnl=r["daily_pnl"],
            daily_return_pct=0.0,
            equity_close=r["equity_close"],
        )


AUG_TO_OCT = [
    _row("2026-08-14", 9500.00, 12.0),
    _row("2026-09-02", 9862.74, 44.70),
    _row("2026-10-09", 10029.62, 20.0, equity_close=10029.62),
    _row("2026-10-12", 9928.22, -101.40),
    _row("2026-10-13", 9950.00, 21.78),
]
FALLBACK = [
    _row("2026-10-09", 10029.62, 20.0, equity_close=10029.62),
    _row("2026-10-12", 9928.22, 0.0),  # broker returned nothing: last_equity = portfolio_value
]
PRE_ONLY = [_row("2026-08-14", 9500.00, 12.0), _row("2026-10-09", 10029.62, 20.0, equity_close=10029.62)]


def test_clean_record_date_comes_from_the_constant():
    assert CLEAN_RECORD_DATE == CLEAN_RECORD_START_UTC[:10]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [(AUG_TO_OCT, (10029.62, "Fri 9 Oct close")), (FALLBACK, None), (PRE_ONLY, None)],
)
def test_dashboard_and_telegram_agree_and_match_the_rule(tmp_path, monkeypatch, rows, expected):
    db_path = tmp_path / "t.db"
    db = Database(str(db_path))
    db.initialize()
    _seed(db, rows)

    telegram = clean_record_baseline(*db.get_clean_record_baseline_rows())
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(db_path))
    dashboard = clean_record_baseline(*db_reads.get_clean_record_baseline_rows())
    db.close()

    assert telegram == dashboard
    if expected is None:
        assert telegram is None
    else:
        assert telegram[0] == pytest.approx(expected[0])
        assert telegram[1] == expected[1]


def test_previous_row_without_equity_close_falls_back_to_its_total_value():
    prev = _row("2026-10-09", 10029.62, 20.0)
    first = _row("2026-10-12", 9928.22, -101.40)
    assert clean_record_baseline(first, prev)[1] == "Fri 9 Oct close"
    assert clean_record_baseline(first, _row("2026-10-09", 10000.00, 20.0)) is None


def test_no_row_on_or_after_the_date_is_none():
    assert clean_record_baseline(None, _row("2026-10-09", 10029.62, 20.0)) is None
    assert clean_record_baseline(None, None) is None
