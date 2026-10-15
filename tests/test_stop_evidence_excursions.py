"""Stop-floor evidence: a position that closes carries its excursions.

RECORDING ONLY (docs/WORK.md items 90 and 199). These tests prove the
evidence a future pass needs is durably on the row — entry ATR, entry price,
the stop placed at entry, whether that stop sat on a COMPUTED structural
level or on the ATR band, the worst and best excursion while open, the
realised outcome and whether the stop was hit. They assert nothing about
what the floor SHOULD be and nothing here may be optimised against.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.portfolio_constructor import (
    LEVEL_BACKED_STOP_RULES,
    STOP_RULE_ATR_BAND,
    STOP_RULE_LEVEL_HONOURED,
)
from src.data.levels import (
    describe_stop_level_basis,
    level_zone_halfwidth,
)
from src.data.technical import ATR_PERIOD
from src.storage.db import Database, _categorize_exit_reason


def _snapshot(symbol, qty, avg_entry, current_price):
    return SimpleNamespace(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=(current_price - avg_entry) * qty,
        sector="Tech",
    )


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "evidence.db"))
    database.initialize()
    yield database
    database.conn.close()


def _row(db, trade_id):
    return dict(db.conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())


def test_closed_long_keeps_its_adverse_excursion(db):
    """The load-bearing one: a position that OPENS, runs against the desk
    and then CLOSES still has the worst excursion it reached stored."""
    trade_id = db.insert_trade(
        symbol="AAA",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=94.0,
        entry_atr=2.0,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    # Two sessions open: 96 then a recovery to 99. Monotonic — the recovery
    # must not erase the excursion that preceded it.
    db.sync_positions([_snapshot("AAA", 10, 100.0, 96.0)])
    db.sync_positions([_snapshot("AAA", 10, 100.0, 99.0)])
    # The position closes: the broker snapshot no longer carries it.
    db.sync_positions([])

    row = _row(db, trade_id)
    assert row["max_adverse_excursion"] == pytest.approx(4.0)
    assert row["entry_atr"] == pytest.approx(2.0)
    assert row["price"] == pytest.approx(100.0)
    assert row["initial_stop_loss"] == pytest.approx(94.0)
    assert row["stop_basis"] == STOP_RULE_ATR_BAND
    assert row["stop_basis"] not in LEVEL_BACKED_STOP_RULES
    # Stop distance in ATR multiples is RECOMPUTED from the stored facts,
    # never stored a second time.
    atr_multiple = (row["price"] - row["initial_stop_loss"]) / row["entry_atr"]
    assert atr_multiple == pytest.approx(3.0)


def test_favourable_excursion_is_recorded_alongside_the_adverse_one(db):
    trade_id = db.insert_trade(
        symbol="BBB",
        action="BUY",
        qty=5,
        price=50.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=47.0,
        entry_atr=1.5,
        stop_basis=STOP_RULE_LEVEL_HONOURED,
    )
    db.sync_positions([_snapshot("BBB", 5, 50.0, 58.0)])  # +8 in favour
    db.sync_positions([_snapshot("BBB", 5, 50.0, 48.0)])  # -2 against
    db.sync_positions([_snapshot("BBB", 5, 50.0, 55.0)])  # back up, no widen

    row = _row(db, trade_id)
    assert row["max_favourable_excursion"] == pytest.approx(8.0)
    assert row["max_adverse_excursion"] == pytest.approx(2.0)
    assert row["stop_basis"] in LEVEL_BACKED_STOP_RULES


def test_short_excursions_use_the_other_side(db):
    trade_id = db.insert_trade(
        symbol="CCC",
        action="SHORT",
        qty=-4,
        price=20.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=23.0,
        entry_atr=1.0,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    db.sync_positions([_snapshot("CCC", -4, 20.0, 22.5)])  # against a short
    db.sync_positions([_snapshot("CCC", -4, 20.0, 17.0)])  # in its favour

    row = _row(db, trade_id)
    assert row["max_adverse_excursion"] == pytest.approx(2.5)
    assert row["max_favourable_excursion"] == pytest.approx(3.0)


def test_stop_hit_is_readable_from_the_exit_row(db):
    """Whether the stop was HIT is not a new field: the exit row's
    deterministic `exit_reason_category` already says so, and the realised
    outcome sits on the same row."""
    db.insert_trade(
        symbol="DDD",
        action="BUY",
        qty=3,
        price=10.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=8.0,
        entry_atr=0.8,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    exit_id = db.insert_trade(
        symbol="DDD",
        action="STOP_OUT",
        qty=3,
        price=8.0,
        reasoning="broker stop filled",
        run_id="r2",
        fill_status="filled",
    )
    # A confirmed broker stop fill categorises as a stop, and nothing else
    # does — this is the fact a later pass joins to the entry evidence.
    assert _categorize_exit_reason("STOP_OUT", None, "filled", 3) == "broker_stop_fill"
    assert _categorize_exit_reason("SELL", "trend over", "filled", 3) != "broker_stop_fill"
    assert "exit_reason_category" in _row(db, exit_id)
    with db._lock:
        db.conn.execute(
            "UPDATE trades SET realized_pnl = ? WHERE id = ?",
            (-6.0, exit_id),
        )
        db.conn.commit()
    assert _row(db, exit_id)["realized_pnl"] == pytest.approx(-6.0)


def test_missing_values_are_stored_null_never_guessed(db):
    """An entry whose ATR was unavailable stores NULL, not a substitute."""
    trade_id = db.insert_trade(
        symbol="EEE",
        action="BUY",
        qty=1,
        price=5.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=4.0,
        entry_atr=None,
        stop_basis=None,
    )
    row = _row(db, trade_id)
    assert row["entry_atr"] is None
    assert row["stop_basis"] is None
    # No entry ATR means no evidence row to widen — excursions stay NULL
    # rather than being accumulated against an unusable baseline.
    db.sync_positions([_snapshot("EEE", 1, 5.0, 4.5)])
    assert _row(db, trade_id)["max_adverse_excursion"] is None


# ---------------------------------------------------------------------------
# Item 55 — what the stop was BASED on, and what price did to that level.
#
# Same store, same rows, same limit: RECORDING ONLY and FALSIFICATION ONLY.
# These tests prove the facts land and stay raw. They assert nothing about
# how many bars should make a swing point or how wide a zone should be, and
# nothing here may be swept for either number.
# ---------------------------------------------------------------------------


def _basis(**over):
    record = describe_stop_level_basis(
        level_price=95.0,
        stop_loss=94.5,
        entry_price=100.0,
        computed_levels=[95.0, 110.0],
        computed_level_touches={95.0: 3},
    )
    record.update(over)
    return json.dumps(record, sort_keys=True)


def test_stop_level_basis_records_the_level_identity_not_a_verdict():
    record = describe_stop_level_basis(
        level_price=95.0,
        stop_loss=94.5,
        entry_price=100.0,
        computed_levels=[95.0, 110.0],
        computed_level_touches={95.0: 3},
    )
    assert record["level_backed"] is True
    assert record["level_price"] == pytest.approx(95.0)
    assert record["level_kind"] == "support"
    # How many turns made the level, and how a swing point was confirmed —
    # the two halves of the open question, recorded as they stood. Since
    # 2026-10-15 a swing is a reversal of the stock's own ATR(14), not a
    # bar-count window (schema version 2).
    assert record["level_touches"] == 3
    assert record["pivot_rule"] == "zigzag_reversal_atr"
    assert record["pivot_atr_period"] == ATR_PERIOD
    assert "pivot_window_bars" not in record and "pivot_confirm_bars" not in record
    # The zone, read off the live definition rather than restated here.
    half = level_zone_halfwidth(95.0)
    assert record["zone_low"] == pytest.approx(95.0 - half)
    assert record["zone_high"] == pytest.approx(95.0 + half)
    assert record["zone_width"] == pytest.approx(half * 2)
    assert record["stop_to_level"] == pytest.approx(0.5)
    assert record["entry_to_level"] == pytest.approx(5.0)
    # No classification of the outcome is produced anywhere in the record.
    for banned in ("respected", "pierced", "broken", "verdict", "quality"):
        assert banned not in record


def test_a_stop_with_no_level_behind_it_is_still_recorded_as_the_control():
    record = describe_stop_level_basis(
        level_price=None,
        stop_loss=94.5,
        entry_price=100.0,
        computed_levels=[],
        computed_level_touches={},
    )
    assert record["level_backed"] is False
    assert record["level_price"] is None
    # Unavailable is NULL, never substituted.
    for field in (
        "level_kind",
        "level_touches",
        "zone_low",
        "zone_high",
        "zone_width",
        "stop_to_level",
        "stop_inside_zone",
    ):
        assert record[field] is None


def test_short_side_records_resistance_and_flips_the_distance_sign():
    record = describe_stop_level_basis(
        level_price=105.0,
        stop_loss=105.5,
        entry_price=100.0,
        computed_levels=[105.0],
        computed_level_touches={105.0: 2},
        is_short=True,
    )
    assert record["level_kind"] == "resistance"
    assert record["stop_to_level"] == pytest.approx(0.5)
    assert record["entry_to_level"] == pytest.approx(5.0)


def test_an_unmatched_touch_count_is_null_rather_than_guessed():
    record = describe_stop_level_basis(
        level_price=95.0,
        stop_loss=94.5,
        entry_price=100.0,
        computed_levels=[95.0],
        computed_level_touches={110.0: 4},
    )
    assert record["level_backed"] is True
    assert record["level_touches"] is None


def test_what_price_did_to_the_level_is_stored_as_raw_distances(db):
    """The afterwards half: price enters the zone, goes through it, recovers.

    All three facts survive as raw distances, so a later reader can call it
    respected, pierced or broken against a cutoff IT states — none is baked
    in here.
    """
    half = level_zone_halfwidth(95.0)
    trade_id = db.insert_trade(
        symbol="BBB",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=94.5,
        entry_atr=2.0,
        stop_basis=STOP_RULE_LEVEL_HONOURED,
        stop_level_basis=_basis(),
    )
    # Above the zone, then inside it, then clean through the far edge, then
    # a recovery that must NOT erase what came before.
    db.sync_positions([_snapshot("BBB", 10, 100.0, 99.0)])
    db.sync_positions([_snapshot("BBB", 10, 100.0, 95.0)])
    db.sync_positions([_snapshot("BBB", 10, 100.0, 93.0)])
    db.sync_positions([_snapshot("BBB", 10, 100.0, 99.5)])
    db.sync_positions([])

    row = _row(db, trade_id)
    assert row["level_max_penetration"] == pytest.approx((95.0 - half) - 93.0)
    assert row["level_closest_approach"] == pytest.approx(93.0 - (95.0 + half))
    # The pinned half is still joinable to the running half on the same row.
    stored = json.loads(row["stop_level_basis"])
    assert stored["level_touches"] == 3
    assert stored["zone_width"] == pytest.approx(half * 2)


def test_a_level_never_reached_records_no_penetration(db):
    trade_id = db.insert_trade(
        symbol="CCC",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=94.5,
        entry_atr=2.0,
        stop_basis=STOP_RULE_LEVEL_HONOURED,
        stop_level_basis=_basis(),
    )
    db.sync_positions([_snapshot("CCC", 10, 100.0, 104.0)])
    db.sync_positions([])
    row = _row(db, trade_id)
    assert row["level_max_penetration"] is None
    assert row["level_closest_approach"] == pytest.approx(104.0 - (95.0 + level_zone_halfwidth(95.0)))


def test_a_trade_with_no_level_record_keeps_both_distances_null(db):
    trade_id = db.insert_trade(
        symbol="DDD",
        action="BUY",
        qty=10,
        price=100.0,
        reasoning="entry",
        run_id="r1",
        stop_loss=94.0,
        entry_atr=2.0,
        stop_basis=STOP_RULE_ATR_BAND,
    )
    db.sync_positions([_snapshot("DDD", 10, 100.0, 90.0)])
    db.sync_positions([])
    row = _row(db, trade_id)
    assert row["stop_level_basis"] is None
    assert row["level_max_penetration"] is None
    assert row["level_closest_approach"] is None


def test_the_recording_is_declared_falsification_only_in_the_code():
    """The limit must be IN the code, not only in a commit message."""
    root = Path(__file__).resolve().parent.parent
    source = (root / "src/data/levels.py").read_text()
    assert "FALSIFICATION ONLY" in source
    assert "NEVER BE SWEPT" in source.upper()
    schema_source = (root / "src/storage/schema/manager.py").read_text()
    db_source = (root / "src/storage/db.py").read_text()
    # the declarations moved with the schema code into the schema manager
    combined = db_source + schema_source
    assert "stop_level_basis" in combined
    assert "no fitting, only reading" in combined.lower()
    assert "NO CLASSIFICATION IS STORED" in combined
