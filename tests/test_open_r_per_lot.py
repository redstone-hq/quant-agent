"""Open-position R is per lot everywhere: prompt facts, heat block, +1R ratchet.

R = unrealised P&L / sum over open lots of lot qty x |lot fill - that lot's
OWN initial stop|. Never one lot's stop against the broker's average entry.

Worked example (the defect): buy 100 @ $50 stop $45, add 100 @ $60 stop $52,
average entry $55, price $62. Per-lot risk = 500 + 800 = $1300; P&L = $1400;
R = +1.08. 1R per share is $6.50, so the breakeven ratchet fires at >= $61.50.

What origin/main did: the AI prompt path used the LATEST buy's stop ($52),
(62-55)/(55-52) = +2.33R. The trail used the FIRST lot's stop ($45, via the
position's open row), 1R = $10, so the breakeven ratchet fired LATE (>= $65).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.models import Position
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "open_r.db"))
    database.initialize()  # real schema via the schema manager
    yield database
    database.close()


def _open(db, symbol, action, qty, price, stop, setup_type="range", take_profit=None):
    db.insert_trade(
        symbol=symbol,
        action=action,
        qty=qty,
        price=price,
        reasoning="entry",
        run_id="r",
        stop_loss=stop,
        take_profit=take_profit,
        setup_type=setup_type,
        fill_status="filled",
    )


def _pos(symbol, qty, avg_entry, current_price) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector="Technology",
    )


def _facts_and_heat(db, position, live_stop):
    p = build_pipeline(db=db, broker=MagicMock())
    p.broker.get_current_stop_price.return_value = live_stop
    p._atr_for_symbol = MagicMock(return_value=1.0)
    facts = p._build_position_facts(positions=[position], morning_trades=[], total_value=100_000.0)
    heat = p._build_portfolio_heat([position], 100_000.0)
    return facts[position.symbol], heat.per_position[0]


def _trail(db, position, live_stop):
    """Run the real trail pass; return the real evaluation it produced."""
    from src.risk import trailing

    p = build_pipeline(db=db, broker=MagicMock(), market=MagicMock())
    p.broker.get_current_stop_price.return_value = live_stop
    p.market.get_ohlcv.return_value = []
    p._atr_for_symbol = MagicMock(return_value=1.0)
    seen = []
    real = trailing.evaluate_trailing_stop

    def _capture(**kwargs):
        seen.append(real(**kwargs))
        return SimpleNamespace(proposal=None, code="noop", structural_code=None)

    with (
        patch("src.execution.scale_in.pending_protection_symbols", return_value=set()),
        patch("src.risk.trailing.evaluate_trailing_stop", side_effect=_capture),
    ):
        p._apply_deterministic_trails([position], run_id="r1")
    assert len(seen) == 1
    return seen[0]


def _r_ratchet_fired(ev) -> bool:
    return ev.proposal is not None and ev.proposal.source in ("breakeven_ratchet", "second_ratchet")


# ---------------------------------------------------------------- worked example


def test_worked_example_prompt_and_heat_r_is_per_lot(db):
    _open(db, "ABC", "BUY", 100, 50.0, 45.0)
    _open(db, "ABC", "BUY", 100, 60.0, 52.0)
    fact, row = _facts_and_heat(db, _pos("ABC", 200, 55.0, 62.0), live_stop=52.0)
    assert fact["r_multiple"] == pytest.approx(1.08)  # 1400 / 1300, not +2.33
    assert row.r_multiple == pytest.approx(1.08)


def test_worked_example_ratchet_waits_for_per_lot_1r(db):
    _open(db, "ABC", "BUY", 100, 50.0, 45.0, take_profit=90.0)
    _open(db, "ABC", "BUY", 100, 60.0, 52.0, take_profit=90.0)
    # $61: P&L $1200 < $1300 risk — below +1R per lot, must not fire.
    assert not _r_ratchet_fired(_trail(db, _pos("ABC", 200, 55.0, 61.0), live_stop=52.0))
    # $62: P&L $1400 >= $1300 — fires, stop to breakeven (average entry).
    ev = _trail(db, _pos("ABC", 200, 55.0, 62.0), live_stop=52.0)
    assert ev.proposal is not None and ev.proposal.source == "breakeven_ratchet"
    assert ev.proposal.new_stop == pytest.approx(55.0)


def test_short_scale_in_is_mirrored(db):
    # Short 100 @ $50 stop $55, add 100 @ $40 stop $46: risk 500 + 600 = $1100.
    _open(db, "SSS", "SHORT", 100, 50.0, 55.0)
    _open(db, "SSS", "SHORT", 100, 40.0, 46.0)
    fact, row = _facts_and_heat(db, _pos("SSS", -200, 45.0, 38.0), live_stop=46.0)
    assert fact["r_multiple"] == pytest.approx(1.27)  # 1400 / 1100, not +7.0
    assert row.r_multiple == pytest.approx(1.27)


# ---------------------------------------------------------------- single lot unchanged


def test_single_lot_r_and_ratchet_unchanged(db):
    _open(db, "ONE", "BUY", 100, 50.0, 45.0, take_profit=90.0)
    fact, row = _facts_and_heat(db, _pos("ONE", 100, 50.0, 60.0), live_stop=45.0)
    assert fact["r_multiple"] == 2.0 and row.r_multiple == 2.0  # (60-50)/(50-45)
    assert not _r_ratchet_fired(_trail(db, _pos("ONE", 100, 50.0, 54.99), live_stop=45.0))
    ev = _trail(db, _pos("ONE", 100, 50.0, 55.0), live_stop=45.0)
    assert ev.proposal is not None and ev.proposal.source == "breakeven_ratchet"
    assert ev.proposal.new_stop == pytest.approx(50.0)


# ---------------------------------------------------------------- missing stop


def test_lot_without_stop_makes_r_unknown_and_no_r_ratchet(db):
    _open(db, "MIS", "BUY", 100, 50.0, 45.0, take_profit=200.0)
    _open(db, "MIS", "BUY", 100, 60.0, 52.0, take_profit=200.0)
    # Writes now refuse a stopless entry; legacy rows predating that guard exist.
    with db._lock:
        db.conn.execute(
            "UPDATE trades SET stop_loss = 0, initial_stop_loss = NULL WHERE id = (SELECT MAX(id) FROM trades)"
        )
        db.conn.commit()
    fact, row = _facts_and_heat(db, _pos("MIS", 200, 55.0, 80.0), live_stop=45.0)
    assert fact["r_multiple"] is None and row.r_multiple is None
    ev = _trail(db, _pos("MIS", 200, 55.0, 80.0), live_stop=45.0)
    assert not _r_ratchet_fired(ev)


# ---------------------------------------------------------------- one reader, reconcile, no adapter


def test_dashboard_uses_the_same_reader_and_formula_and_skips_unfilled_lots(db, tmp_path, monkeypatch):
    from src.api import db_reads
    from src.storage.analytics.trade_review import open_position_r

    _open(db, "ABC", "BUY", 100, 50.0, 45.0)
    _open(db, "ABC", "BUY", 100, 60.0, 52.0)
    db.insert_trade(
        symbol="ABC",
        action="BUY",
        qty=500,
        price=61.0,
        reasoning="never filled",
        run_id="r",
        stop_loss=60.0,
        fill_status="cancelled",
    )
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(tmp_path / "open_r.db"))
    rows = db_reads.get_open_position_rows(["ABC"])
    assert all(r["fill_status"] != "cancelled" for r in rows)
    dash = open_position_r([{"symbol": "ABC", "qty": 200, "avg_entry": 55.0, "current_price": 62.0}], rows)
    fact, _ = _facts_and_heat(db, _pos("ABC", 200, 55.0, 62.0), live_stop=52.0)
    assert dash["ABC"]["unrealised_r"] == fact["r_multiple"] == pytest.approx(1.08)


def test_unmarked_filled_add_makes_r_unknown_not_smaller_and_warns_once(db, caplog):
    from src.exits_parts import trails

    trails._R_UNKNOWN_WARNED.clear()
    _open(db, "ADD", "BUY", 100, 50.0, 45.0, take_profit=90.0)
    db.insert_trade(
        symbol="ADD",
        action="BUY",
        qty=100,
        price=60.0,
        reasoning="add, fill not yet recorded",
        run_id="r",
        stop_loss=52.0,
        take_profit=90.0,
        setup_type="range",
        fill_status="submitted",
    )
    # Broker holds 200; only 100 are recorded executed. Dropping the add would
    # make 1R $5 and fire breakeven at $60 — it must be "unknown" instead.
    fact, row = _facts_and_heat(db, _pos("ADD", 200, 55.0, 62.0), live_stop=52.0)
    assert fact["r_multiple"] is None and row.r_multiple is None
    with caplog.at_level("WARNING"):
        assert not _r_ratchet_fired(_trail(db, _pos("ADD", 200, 55.0, 62.0), live_stop=52.0))
        assert not _r_ratchet_fired(_trail(db, _pos("ADD", 200, 55.0, 63.0), live_stop=52.0))
    warned = [r for r in caplog.records if "R unknown (lots_do_not_reconcile)" in r.getMessage()]
    assert len(warned) == 1 and "ADD" in warned[0].getMessage()


def test_backtest_uses_the_per_lot_function_and_no_single_stop_adapter_survives():
    import inspect

    from src.backtest import engine
    from src.risk import trail_evaluate

    for fn in (trail_evaluate.compute_trailing_stop, trail_evaluate.evaluate_trailing_stop):
        assert "initial_stop" not in inspect.signature(fn).parameters
    long_pos = SimpleNamespace(direction="long", shares=40.0, entry_price=50.0, stop_initial=45.0)
    short_pos = SimpleNamespace(direction="short", shares=40.0, entry_price=50.0, stop_initial=56.0)
    assert engine._risk_per_share(long_pos, 40.0) == pytest.approx(5.0)
    assert engine._risk_per_share(short_pos, -40.0) == pytest.approx(6.0)
    wrong_side = SimpleNamespace(direction="long", shares=40.0, entry_price=50.0, stop_initial=55.0)
    assert engine._risk_per_share(wrong_side, 40.0) is None
