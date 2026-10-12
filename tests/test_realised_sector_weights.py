"""Board item 224 — the realised `(sector, side)` weights of the orders the
constructor actually built, one durable row per run.

RECORDING ONLY. These tests assert the row's shape, unit and NULL
discipline. Nothing here derives, tunes or proposes a sector cap, and
nothing in the product reads these rows back into a decision.
"""

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from src.models import TradeDecision
from src.storage.db import Database

SRC = Path(__file__).resolve().parents[1] / "src"


def _decision(action, symbol, pct):
    short = action == "SHORT"
    return TradeDecision(
        action=action,
        symbol=symbol,
        allocation_pct=pct,
        entry_price=100.0,
        stop_loss=105.0 if short else 95.0,
        take_profit=80.0 if short else 120.0,
        reasoning="test",
    )


@pytest.fixture()
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    return d


def _row(db):
    cur = db.conn.execute("SELECT * FROM realised_sector_weights")
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


def test_weights_are_grouped_by_sector_and_side(db):
    ok = db.record_realised_sector_weights(
        decisions=[
            _decision("BUY", "AAA", 4.0),
            _decision("BUY", "BBB", 6.0),
            _decision("SHORT", "CCC", 3.0),
            _decision("SELL", "DDD", 50.0),
        ],
        sectors={"AAA": "Technology", "BBB": "Technology", "CCC": "Energy"},
        total_value=100_000.0,
        run_id="run-1",
    )
    assert ok
    rows = _row(db)
    assert len(rows) == 1
    weights = {(w["sector"], w["side"]): w for w in json.loads(rows[0]["weights_json"])}
    assert weights[("Technology", "long")]["weight_pct"] == pytest.approx(10.0)
    assert weights[("Technology", "long")]["orders"] == 2
    assert weights[("Energy", "short")]["weight_pct"] == pytest.approx(3.0)
    # A reduction is never added into a weight, but it is counted.
    assert rows[0]["entry_orders_built"] == 3
    assert rows[0]["reducing_orders_built"] == 1


def test_denominator_is_recorded_explicitly_and_matches_item_222_unit(db):
    db.record_realised_sector_weights(
        decisions=[_decision("BUY", "AAA", 4.0)],
        sectors={"AAA": "Technology"},
        total_value=50_000.0,
        run_id="run-2",
    )
    row = _row(db)[0]
    assert row["denominator"] == Database.REALISED_SECTOR_WEIGHT_DENOMINATOR
    d = row["denominator"]
    assert "total account equity" in d
    assert "before the gross multiplier" in d
    assert row["total_value"] == pytest.approx(50_000.0)


def test_unknown_sector_is_null_never_other(db):
    db.record_realised_sector_weights(
        decisions=[_decision("BUY", "ZZZ", 2.5)],
        sectors={},
        total_value=10_000.0,
        run_id="run-3",
    )
    row = _row(db)[0]
    entries = json.loads(row["weights_json"])
    assert entries[0]["sector"] is None
    assert row["unknown_sector_orders"] == 1
    assert "other" not in row["weights_json"].lower()


def test_a_run_that_built_nothing_records_that_fact_not_zero_concentration(db):
    """Nothing built writes `[]`, NEVER NULL.

    A new NULL could not be told apart from "the recorder failed to write
    its content". `[]` plainly means no orders of either kind, and
    `entry_orders_built` 0 on the same row still separates that from a
    book with zero concentration.
    """
    db.record_realised_sector_weights(
        decisions=[],
        sectors={},
        total_value=10_000.0,
        run_id="run-4",
    )
    row = _row(db)[0]
    assert row["weights_json"] == "[]"
    assert json.loads(row["weights_json"]) == []
    assert row["entry_orders_built"] == 0


def test_no_recording_call_can_ever_write_a_null_weights_row(db):
    """The write path must have no input that yields NULL weights."""
    cases = [
        dict(decisions=[], sectors={}, total_value=None, run_id="n-1"),
        dict(decisions=None, sectors=None, total_value=0.0, run_id="n-2"),
        dict(decisions=[_decision("SELL", "AAA", 1.0)], sectors=None, total_value=1.0, run_id="n-3"),
        dict(decisions=[_decision("BUY", "AAA", 1.0)], sectors={}, total_value=float("nan"), run_id="n-5"),
    ]
    for kw in cases:
        assert db.record_realised_sector_weights(**kw) is True, kw
    rows = _row(db)
    assert len(rows) == len(cases)
    nulls = [r["run_id"] for r in rows if r["weights_json"] is None]
    assert not nulls, nulls


def test_the_schema_itself_refuses_a_null_weights_row(db):
    """Not a convention — the column is NOT NULL, so NULL is unwritable."""
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO realised_sector_weights "
            "(timestamp, run_id, weights_json, denominator) "
            "VALUES ('2026-10-02 00:00:00', 'direct', NULL, 'd')"
        )


def test_legacy_null_rows_remain_unknown_and_future_nulls_are_refused(tmp_path):
    """Counts cannot recover a missing sector/side/weight payload."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE realised_sector_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            run_id TEXT,
            session_date TEXT,
            weights_json TEXT,
            denominator TEXT NOT NULL,
            total_value REAL,
            entry_orders_built INTEGER,
            reducing_orders_built INTEGER,
            unknown_sector_orders INTEGER,
            UNIQUE (run_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO realised_sector_weights "
        "(timestamp, run_id, weights_json, denominator, entry_orders_built, "
        "reducing_orders_built) "
        "VALUES ('2026-10-01 14:18:07', 'legacy-reductions', NULL, 'd', 0, 6)"
    )
    conn.commit()
    original = conn.execute("SELECT * FROM realised_sector_weights").fetchone()
    conn.close()

    d = Database(str(path))
    d.initialize()
    assert (
        tuple(d.conn.execute("SELECT * FROM realised_sector_weights WHERE run_id = 'legacy-reductions'").fetchone())
        == original
    )
    assert _row(d)[0]["weights_json"] is None
    with pytest.raises(sqlite3.IntegrityError):
        d.conn.execute(
            "INSERT INTO realised_sector_weights "
            "(timestamp, run_id, weights_json, denominator) "
            "VALUES ('2026-10-02 00:00:00', 'x', NULL, 'd')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        d.conn.execute("UPDATE realised_sector_weights SET weights_json = NULL WHERE run_id = 'legacy-reductions'")
    # This is the exact UPDATE the old initializer ran before its table
    # rebuild. Rolling back must fail closed instead of rewriting history.
    with pytest.raises(sqlite3.IntegrityError):
        d.conn.execute("UPDATE realised_sector_weights SET weights_json = '[]' WHERE weights_json IS NULL")
    assert (
        d.conn.execute(
            "SELECT weights_json FROM realised_sector_weights WHERE run_id = 'legacy-reductions'"
        ).fetchone()[0]
        is None
    )
    assert d.record_realised_sector_weights(
        decisions=[_decision("SELL", "AAA", 2.5)],
        sectors={"AAA": "Energy"},
        total_value=10_000,
        run_id="new-reduction",
    )
    new_row = d.conn.execute(
        "SELECT weights_json, reducing_orders_built FROM realised_sector_weights WHERE run_id = 'new-reduction'"
    ).fetchone()
    assert new_row[1] == 1
    assert json.loads(new_row[0]) == [
        {"sector": "Energy", "side": "long", "kind": "reduce", "weight_pct": 2.5, "orders": 1},
    ]
    d.conn.close()

    reopened = Database(str(path))
    reopened.initialize()
    assert (
        tuple(
            reopened.conn.execute("SELECT * FROM realised_sector_weights WHERE run_id = 'legacy-reductions'").fetchone()
        )
        == original
    )
    assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_one_row_per_run(db):
    for _ in range(3):
        db.record_realised_sector_weights(
            decisions=[_decision("BUY", "AAA", 1.0)],
            sectors={"AAA": "Energy"},
            total_value=1.0,
            run_id="run-5",
        )
    assert len(_row(db)) == 1


def test_recording_is_reachable_from_executable_product_code():
    """The write must be reached from the product, not only from a test."""
    # item 210 step 12 moved `_record_realised_sector_weights` verbatim into
    # src/pipeline_entry_orders.py; item 224 moved it again, verbatim, into
    # src/pipeline_sector_weights.py. Read it where it now lives; the
    # assertion below is unchanged.
    stages = (SRC / "pipeline_sector_weights.py").read_text()
    tree = ast.parse(stages)
    helper = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_record_realised_sector_weights"
    )
    assert any(isinstance(n, ast.Attribute) and n.attr == "record_realised_sector_weights" for n in ast.walk(helper))
    # item 210 step 10 moved DecisionStage (the one call site) into
    # src/stage_decision.py; the helper itself still lives in
    # pipeline_stages.py. Scan both so the reachability claim survives
    # the split instead of being weakened by it.
    callers = []
    for module in (
        "pipeline_stages.py",
        "stage_decision.py",
        "pipeline_entry_orders.py",
        "pipeline_rotation_exec.py",
        "pipeline_risk_budget_recording.py",
        "pipeline_sector_weights.py",
    ):
        callers += [
            n
            for n in ast.walk(ast.parse((SRC / module).read_text()))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_record_realised_sector_weights"
        ]
    assert len(callers) == 1, "exactly one product call site"


def test_nothing_reads_the_table_back_into_a_decision():
    """RECORDING ONLY — no product module may SELECT from this table."""
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if "realised_sector_weights" in text and "SELECT" in text.upper():
            for line in text.splitlines():
                up = line.upper()
                if "REALISED_SECTOR_WEIGHTS" in up and "SELECT" in up:
                    offenders.append(f"{path}: {line.strip()}")
    assert not offenders, offenders


def test_the_recording_never_blocks_a_trade(db):
    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("disk gone")

    db.conn = _Boom()
    assert (
        db.record_realised_sector_weights(
            decisions=[_decision("BUY", "AAA", 1.0)],
            sectors={"AAA": "Energy"},
            total_value=1.0,
            run_id="run-6",
        )
        is False
    )


def _analysis(symbol, entry=100.0, stop=95.0, target=135.0):
    from src.models import TechAnalysisResult, TechReasoningChain

    return TechAnalysisResult(
        symbol=symbol,
        rating="buy",
        entry_price=entry,
        stop_loss=stop,
        reference_target=target,
        reasoning="test",
        support_levels=[stop],
        resistance_levels=[target],
        computed_levels=[stop, target],
        atr_14=(entry - stop) / 3.5,
        setup_type="range",
        expected_horizon_sessions=60,
        reasoning_chain=TechReasoningChain(trend="x", momentum="x", volatility="x", volume="x", support_resistance="x"),
        thesis_invalid_if="closes below support",
    )


def test_a_real_construct_orders_run_lands_a_populated_row_in_the_store(
    db,
    monkeypatch,
):
    """THE PROOF THE ROW LANDS: real constructor sizes real orders, the real
    product helper runs on its output, and the row is read back out of a real
    store with real values. Fixture symbols and sectors are synthetic."""
    from types import SimpleNamespace

    import src.sector_reference as sector_reference
    from src.models import TargetPosition
    from src.pipeline_sector_weights import _record_realised_sector_weights
    from src.portfolio_constructor import PortfolioConstructor

    sectors = {"AAA": "SectorOne", "BBB": "SectorOne", "CCC": "SectorTwo"}
    monkeypatch.setattr(
        sector_reference,
        "_get_sector",
        lambda s: sectors.get(s, "Unknown"),
    )
    constructor = PortfolioConstructor()
    names = ["AAA", "BBB", "CCC"]
    decisions = constructor.construct_orders(
        targets=[TargetPosition(symbol=s, target_weight_pct=20.0, conviction="high", thesis="t") for s in names],
        positions=[],
        analyses=[_analysis(s) for s in names],
        total_value=100_000,
        price_map={s: 100.0 for s in names},
        unpriceable_symbols={},
    )
    built = [d for d in decisions if d.action == "BUY"]
    assert built, "fixture must make the constructor build orders"

    pipeline = SimpleNamespace(db=db, portfolio_constructor=constructor)
    _record_realised_sector_weights(
        pipeline,
        SimpleNamespace(run_id="run-e2e"),
        SimpleNamespace(decisions=decisions),
        100_000,
    )

    rows = _row(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "run-e2e"
    assert row["entry_orders_built"] == len(built)
    stored = {(r["sector"], r["side"]): r for r in json.loads(row["weights_json"])}
    expected = {}
    for d in built:
        k = (sectors[d.symbol], "long")
        expected[k] = expected.get(k, 0.0) + d.allocation_pct
    assert set(stored) == set(expected)
    for k, w in expected.items():
        assert stored[k]["weight_pct"] == pytest.approx(w, abs=1e-5)
        assert stored[k]["weight_pct"] > 0
