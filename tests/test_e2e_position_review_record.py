"""End-to-end: the close position review's DURABLE record, hermetic.

`test_e2e_close_existing_book.py` proves what `run_close` DOES to the book
(stop moved, sold, left alone). This file proves what it LEAVES BEHIND, and
that the thing left behind is the thing later surfaces read — this desk
has shipped recorders that ran perfectly and reached no human.

Through the same harness (`_run_close`), from the same recorded inputs:

- the `session_reports` row for ('close', today) exists, is read back
  through the read-only replay reader, and carries the SAME money-visible
  payload the run returned: status, coverage gaps, orders, run id, and the
  reviewer's per-position decision WITH its reason;
- the replay renderer turns that stored row into the owner's message and
  the message names the symbol and the decision taken on it;
- a reviewer SELL that survived substantiation and the risk seat lands in
  the `trades` ledger with its reason, under this run's id, as a fill —
  the row the dashboard's history and the next review's "own recent
  decisions" both read;
- the per-position review metrics snapshot for the next session was
  written for the held symbol under this run's id.

Numbers come from the close file's constants (seeded trade and fixture
bars); nothing here is picked. No network, no broker, no live provider,
no production database: `tmp_path/desk.db` is the whole world.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.trader_feed.stored import (
    read_stored_session_report,
    render_stored_session_report,
)
from tests.test_e2e_close_existing_book import (
    HOLD,
    INITIAL_STOP,
    ONE_R_PRICE,
    QTY,
    SYMBOL,
    _assert_hermetic,
    _assert_untouched,
    _assert_whole_position_sold_after_clearing_its_stop,
    _bars,
    _run_close,
)

SELL = [
    {
        "action": "SELL",
        "symbol": SYMBOL,
        "reason": "thesis invalidated: closed below the support the entry was measured on",
        "exit_trigger": "thesis_invalid",
        "trigger_evidence": f"thesis_invalid_if was 'closes below {INITIAL_STOP}'; "
        f"the structure that held the range is gone",
    }
]


def _stored_close_row(tmp_path: Path) -> dict:
    """The row a later surface reads: read-only, by mode, from the file."""
    record = read_stored_session_report("close", db_path=tmp_path / "desk.db")
    assert record is not None, "run_close left no session_reports row behind"
    payload = record.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, dict), record
    record["payload"] = payload
    return record


def _money_visible(payload: dict) -> dict:
    return {
        k: payload.get(k)
        for k in (
            "status",
            "session",
            "positions",
            "orders",
            "run_id",
            "stop_coverage_gaps",
            "leverage",
        )
    }


def _recorded_action(payload: dict) -> dict:
    review = payload.get("review")
    assert isinstance(review, dict), payload.get("review")
    actions = [a for a in review.get("actions", []) if a.get("symbol") == SYMBOL]
    assert len(actions) == 1, review.get("actions")
    return actions[0]


def _ledger_rows(tmp_path: Path, action: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{tmp_path / 'desk.db'}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT symbol, action, qty, reasoning, run_id, fill_status FROM trades WHERE action=? ORDER BY id",
            (action,),
        ).fetchall()
    finally:
        con.close()


def _metric_snapshots(tmp_path: Path) -> list[tuple]:
    """The per-symbol review metrics the NEXT review reads as its 'prior'."""
    con = sqlite3.connect(f"file:{tmp_path / 'desk.db'}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT symbol, run_id, evidence_json FROM specialist_evidence "
            "WHERE agent_name='position_reviewer' AND kind='review_metrics' "
            "ORDER BY id",
        ).fetchall()
    finally:
        con.close()


def _assert_stored_row_matches_the_run(record: dict, result: dict) -> None:
    payload = record["payload"]
    assert _money_visible(payload) == _money_visible(json.loads(json.dumps(result, default=str))), (
        _money_visible(payload),
        _money_visible(result),
    )
    assert record.get("mode", "close") == "close", record
    assert payload["status"] == "reviewed" and payload["session"] == "close"
    assert payload["stop_coverage_gaps"] == [], payload["stop_coverage_gaps"]
    assert payload["run_id"] == result["run_id"] and payload["run_id"]


def test_a_hold_review_is_stored_verbatim_with_its_reason_and_rereadable(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE - 1.0)
    result, trace, trading, attempts = _run_close(
        tmp_path,
        monkeypatch,
        bars=bars,
        actions=HOLD,
    )
    _assert_hermetic(result, trace, trading, attempts)
    _assert_untouched(trading, INITIAL_STOP)

    record = _stored_close_row(tmp_path)
    _assert_stored_row_matches_the_run(record, result)
    assert record["payload"]["orders"] == [], record["payload"]["orders"]
    action = _recorded_action(record["payload"])
    assert action["action"] == "HOLD" and action["reason"] == HOLD[0]["reason"], action

    # The stored book travels with the row (a replay weeks later must not
    # print today's holdings under an old date).
    book = record.get("positions")
    if isinstance(book, str):
        book = json.loads(book)
    assert [(p.get("symbol"), float(p.get("qty"))) for p in (book or [])] == [(SYMBOL, QTY)], book

    text = render_stored_session_report("close", record)
    assert SYMBOL in text and "HOLD" in text, text

    # The metrics snapshot the NEXT review compares against, under this run.
    snapshots = _metric_snapshots(tmp_path)
    assert [(s, r) for s, r, _ in snapshots] == [(SYMBOL, result["run_id"])], snapshots
    assert json.loads(snapshots[0][2]), snapshots[0]


def test_a_substantiated_sell_is_recorded_as_a_fill_with_its_reason(tmp_path, monkeypatch):
    bars = _bars(end=ONE_R_PRICE - 1.0)
    result, trace, trading, attempts = _run_close(
        tmp_path,
        monkeypatch,
        bars=bars,
        actions=SELL,
        tech_rating="sell",  # today's chart read is against the holding: the gate lets the sale through
    )
    _assert_hermetic(result, trace, trading, attempts)
    _assert_whole_position_sold_after_clearing_its_stop(trading)

    record = _stored_close_row(tmp_path)
    _assert_stored_row_matches_the_run(record, result)
    orders = record["payload"]["orders"]
    assert len(orders) == 1 and orders[0]["status"] == "filled", orders
    assert orders[0]["symbol"] == SYMBOL and float(orders[0]["qty"]) == QTY, orders
    action = _recorded_action(record["payload"])
    assert action["action"] == "SELL" and action["reason"] == SELL[0]["reason"], action
    assert action.get("exit_trigger") == "thesis_invalid", action

    text = render_stored_session_report("close", record)
    assert SYMBOL in text and "SELL" in text, text

    # The ledger row the dashboard history and "own recent decisions" read.
    sells = _ledger_rows(tmp_path, "SELL")
    assert len(sells) == 1, sells
    symbol, act, qty, reasoning, run_id, fill_status = sells[0]
    assert (symbol, act, float(qty)) == (SYMBOL, "SELL", QTY), sells[0]
    assert run_id == result["run_id"], sells[0]
    assert fill_status == "filled", sells[0]
    assert reasoning and "thesis invalidated" in reasoning, reasoning
    # The seeded entry is still there: a sale records, never rewrites.
    assert len(_ledger_rows(tmp_path, "BUY")) == 1
