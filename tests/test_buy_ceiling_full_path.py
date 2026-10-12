"""Board item 218: a BUY written by a full morning session carries the
constructor's MEASURED `structural_ceiling` into the `trades` row.

The guard in `test_buy_insert_evidence_guard.py` proves the insert site PASSES
the kwarg and the ledger test proves the ledger STORES it. Neither proves the
value survives every hop between them (constructor -> PM stage -> risk stage
-> execution -> insert). This drives the real stages end to end on the
rehearsal broker and reads the row back from the real database.

`stop_basis` is deliberately NOT asserted non-null: the constructor ships
`stop_rule=None` for a stop with no level behind it, and recording a
substitute there would be a fabricated audit value.
"""

from __future__ import annotations

from src.models import TradeDecision
from src.storage.db import Database
from tests.test_e2e_morning_session import _run_session


def test_buy_row_carries_the_constructors_measured_ceiling(tmp_path, monkeypatch):
    built: list[tuple[str, bool | None]] = []
    rows: list[tuple] = []
    real_init = TradeDecision.__init__
    real_insert = Database.insert_trade

    def _init(self, *a, **k):
        real_init(self, *a, **k)
        if self.action in ("BUY", "SHORT"):
            built.append((self.symbol, self.structural_ceiling))

    def _insert(self, *a, **k):
        row_id = real_insert(self, *a, **k)
        rows.append(
            tuple(
                self.conn.execute(
                    "SELECT symbol, action, fill_status, structural_ceiling, entry_atr FROM trades WHERE id = ?",
                    (row_id,),
                ).fetchone()
            )
        )
        return row_id

    monkeypatch.setattr(TradeDecision, "__init__", _init)
    monkeypatch.setattr(Database, "insert_trade", _insert)
    result, _trace, _trading = _run_session(tmp_path, monkeypatch)

    assert result["status"] == "executed", result.get("status")
    buys = [r for r in rows if r[1] in ("BUY", "SHORT") and r[2] == "pending_submit"]
    assert len(buys) == 1, f"expected one entry row, got {rows}"
    symbol, _action, _status, stored, entry_atr = buys[0]
    constructed = [v for s, v in built if s == symbol]
    assert constructed and all(isinstance(v, bool) for v in constructed), (
        f"the constructor must emit a real boolean verdict, got {constructed}"
    )
    assert stored is not None, "structural_ceiling was lost between the constructor and the trade row"
    assert bool(stored) == constructed[0]
    assert entry_atr is not None, "entry_atr must reach the same row"


def _add_to_held_name(monkeypatch, prior_row):
    """Drive the real submit loop for a BUY that is an ADD to a held name whose
    original entry row is `prior_row`; return the kwargs of the entry insert."""
    from types import SimpleNamespace  # noqa: F401
    import src.execution.scale_in as scale_in
    from tests.test_item_120_buy_price_sizing import (
        _exec_pipeline_with_print,
        _run_exec,
    )

    monkeypatch.setattr(
        scale_in,
        "prepare_long_add",
        lambda **k: scale_in.LongAddPrep(
            is_scale_in=True,
            intended_stop=k["intended_stop"],
        ),
    )
    pipeline = _exec_pipeline_with_print(100.0)
    pipeline.db.get_symbol_last_buy.return_value = prior_row
    pipeline.broker.list_open_orders_checked.return_value = (True, [])  # listing read, nothing open
    buy = TradeDecision(
        action="BUY",
        symbol="TSLA",
        allocation_pct=10,
        entry_price=100.0,
        stop_loss=94.0,
        take_profit=118.0,
        reasoning="add",
        setup_type="range",
        structural_ceiling=True,
    )
    _run_exec(pipeline, buy, monkeypatch)
    calls = [
        c.kwargs for c in pipeline.db.insert_trade.call_args_list if c.kwargs.get("fill_status") == "pending_submit"
    ]
    assert len(calls) == 1
    return calls[0]


def test_an_add_to_a_legacy_unmeasured_position_records_null_not_a_made_up_verdict(
    monkeypatch,
):
    """Characterises the one path on which a real constructor verdict does NOT
    reach the row, by design: an ADD re-pins the position's OWN stored verdict
    (see `TradeDecision.structural_ceiling`). A position whose entry row
    predates the column carries NULL, so every later add records NULL even
    though this add's own decision holds a measurement. Recording the add's
    value instead would silently reclassify a held position, so NULL is the
    honest record."""
    kwargs = _add_to_held_name(
        monkeypatch,
        {"setup_type": "range", "structural_ceiling": None},
    )
    assert kwargs["structural_ceiling"] is None


def test_an_add_to_a_measured_position_carries_the_positions_own_verdict(
    monkeypatch,
):
    kwargs = _add_to_held_name(
        monkeypatch,
        {"setup_type": "range", "structural_ceiling": 0},
    )
    assert kwargs["structural_ceiling"] is False
