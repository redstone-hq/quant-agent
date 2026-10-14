"""A re-look ranks by its move since the desk last looked, in its own ATR.

The 3h re-look cooldown is deleted (2026-10-13). A name the desk already
looked at this session is measured from the price recorded at that look
(`intraday_evaluations.ref_price`); a name it has not looked at today is
measured from the prior close, which is the last price the desk saw for it.
Both compete in ONE ranking by |last - reference| / ATR.
"""

from datetime import timedelta

from src.pipeline_context import RunContext
from src.storage.db import Database
from tests.session_clock import todays_session_stamp
from tests.test_intraday_scan import _intraday_pipeline, _snapshot


def _real_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "relook.db"))
    db.initialize()
    return db


def _look(db: Database, symbol: str, *, at, ref_price=None, run_id="intra_check-earlier") -> None:
    """One evaluation row for `symbol`, stamped `at` (UTC stamp text)."""
    kwargs = {} if ref_price is None else {"ref_price": ref_price}
    db.record_intraday_evaluation(symbol=symbol, run_id=run_id, status="selected", detail="", **kwargs)
    db.conn.execute(
        "UPDATE intraday_evaluations SET timestamp=? WHERE symbol=? AND run_id=?",
        (at, symbol.upper(), run_id),
    )
    db.conn.commit()


def _stamp(when) -> str:
    from src.trading_calendar import sql_utc_stamp

    return sql_utc_stamp(when)


def _rank(tmp_path, snapshots: dict, looks) -> tuple[list[tuple[str, float]], dict]:
    """Rank `snapshots` (ATR 2.0 for every name) after writing `looks` to a real ledger."""
    p = _intraday_pipeline(universe=list(snapshots), atr=2.0)
    p.db = _real_db(tmp_path)
    for look in looks:
        _look(p.db, **look)
    p.broker.get_intraday_snapshots.return_value = snapshots
    ctx = RunContext.start("intra_check")
    candidates, _ = p._intraday_scan_mover_candidates(ctx)
    return candidates, ctx.__dict__.get("_mover_rank_basis") or {}


def test_relook_that_moved_two_atr_since_the_look_outranks_a_fresh_one_atr_mover(tmp_path):
    """A looked at 1h ago at 100.0, now 104.0: 2.0 ATR since the look.
    Fresh B is 1.0 ATR off its prior close. A ranks first.

    A's prior close (103.0) would put it at only 0.5 ATR, so A leads ONLY
    because it is measured from the look, not from yesterday's close."""
    one_hour_ago = _stamp(todays_session_stamp() - timedelta(hours=1))
    candidates, basis = _rank(
        tmp_path,
        {"A": _snapshot(last=104.0, prev=103.0), "B": _snapshot(last=102.0, prev=100.0)},
        [{"symbol": "A", "at": one_hour_ago, "ref_price": 100.0}],
    )

    assert [s for s, _ in candidates] == ["A", "B"]
    moves = dict(candidates)
    # The move that flows downstream is the RANKED one: since the look, in % of the look price.
    assert abs(moves["A"] - 4.0) < 1e-9
    assert basis["A"] == {"last": 104.0, "ref_price": 100.0, "move_ref": "last_look"}
    assert basis["B"]["move_ref"] == "prev_close"


def test_relook_that_barely_moved_since_the_look_loses_to_a_half_atr_fresh_mover(tmp_path):
    """A looked at 1h ago at 100.0, now 100.1: 0.05 ATR since the look.
    Fresh B is 0.5 ATR off its prior close. B ranks first.

    A's prior close (96.0) would put it at 2.05 ATR; what it did before the
    look is already priced, so it does not buy A a second look."""
    one_hour_ago = _stamp(todays_session_stamp() - timedelta(hours=1))
    candidates, _ = _rank(
        tmp_path,
        {"A": _snapshot(last=100.1, prev=96.0), "B": _snapshot(last=101.0, prev=100.0)},
        [{"symbol": "A", "at": one_hour_ago, "ref_price": 100.0}],
    )

    assert [s for s, _ in candidates] == ["B", "A"]


def test_record_intraday_evaluation_persists_ref_price(tmp_path):
    """The look's price is stored and read back as this session's reference;
    a later price-less write for the same run (an exit-path row) keeps it."""
    db = _real_db(tmp_path)
    db.record_intraday_evaluation(
        symbol="aapl", run_id="intra_check-1", status="selected", detail="x", ref_price=187.25
    )

    row = db.conn.execute("SELECT symbol, ref_price FROM intraday_evaluations WHERE run_id='intra_check-1'").fetchone()
    assert (row[0], row[1]) == ("AAPL", 187.25)
    assert db.get_session_reference_price("AAPL") == 187.25

    db.record_intraday_evaluation(symbol="AAPL", run_id="intra_check-1", status="exit_refused", detail="y")
    assert db.get_session_reference_price("AAPL") == 187.25

    # A look from a PRIOR session is not this session's reference.
    db.record_intraday_evaluation(symbol="MSFT", run_id="intra_check-0", status="selected", detail="", ref_price=400.0)
    db.conn.execute(
        "UPDATE intraday_evaluations SET timestamp=? WHERE run_id='intra_check-0'",
        (_stamp(todays_session_stamp() - timedelta(days=1)),),
    )
    db.conn.commit()
    assert db.get_session_reference_price("MSFT") is None


def test_old_row_with_no_price_ranks_from_prev_close(tmp_path):
    """A row written today before the price was stored (no ref_price) falls
    back to the prior close, exactly like a never-looked-at name. It is not
    locked out for the rest of the session.

    Stamped one REAL hour ago, so under the deleted 3h clock it would have
    been dropped `cooling_down` whenever this runs; a price-less row is not a
    reference whatever its date."""
    from src.trading_calendar import sql_utc_stamp_ago

    one_hour_ago = sql_utc_stamp_ago(hours=1)
    candidates, basis = _rank(
        tmp_path,
        {"A": _snapshot(last=106.0, prev=100.0), "B": _snapshot(last=102.0, prev=100.0)},
        [{"symbol": "A", "at": one_hour_ago}],
    )

    assert [s for s, _ in candidates] == ["A", "B"]
    assert abs(dict(candidates)["A"] - 6.0) < 1e-9
    assert basis["A"]["move_ref"] == "prev_close"
