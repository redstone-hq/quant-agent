"""Record-only (checklist n06): the two undersettled caps leave a durable record of what they cut.

* `_NAMES_PER_PAID_CALL`: each intraday check records the movers sent and those
  left out, with ranks (and the call's cost).
* cluster census: each insider refresh records clusters found
  reserved and cut. Selection itself is unchanged.
"""

import json
from unittest.mock import MagicMock, patch

from src.data.smart_money_cluster import cluster_census
from src.pipeline_context import RunContext
from tests.test_insider_purchase_cluster import _buy
from tests.test_intraday_scan import _intraday_pipeline, _snapshot


def test_each_check_records_sent_and_not_sent_with_ranks():
    moves = {f"S{i}": 1.0 + i for i in range(8)}
    p = _intraday_pipeline(universe=list(moves))
    p.broker.get_intraday_snapshots.return_value = {s: _snapshot(last=100.0 + m, prev=100.0) for s, m in moves.items()}
    p.db.get_session_reference_price.return_value = None
    p._atr_for_symbol = MagicMock(return_value=2.0)
    p.tech_analyst.analyze_batch.return_value = ({}, None)
    events = []
    with (
        patch("src.pipeline_intraday.compute_indicators", return_value=MagicMock()),
        patch("src.pipeline_candidate_records._persist_evidence", side_effect=lambda db, **kw: events.append(kw)),
    ):
        p._run_intraday_opportunity_scan(RunContext.start("intra_check"))
    rows = [json.loads(e["evidence_json"]) for e in events if e.get("kind") == "pipeline_event"]
    (batch,) = [r for r in rows if r.get("outcome") == "batch_selected"]
    assert [(n["symbol"], n["rank"]) for n in batch["sent"]] == [(f"S{7 - i}", i + 1) for i in range(5)]
    assert [(n["symbol"], n["rank"]) for n in batch["not_sent"]] == [("S2", 6), ("S1", 7), ("S0", 8)]
    sent_calls = [[d["symbol"] for d in c.args[0]] for c in p.tech_analyst.analyze_batch.call_args_list]
    assert sent_calls == [[n["symbol"] for n in batch["sent"]]]  # selection unchanged


def test_each_refresh_records_clusters_found():
    census = cluster_census({"CLUSTER1", "CLUSTER0"}, rows_in=47)
    assert census == {"clusters_found": 2, "cluster_symbols": ["CLUSTER0", "CLUSTER1"], "rows_in": 47}
