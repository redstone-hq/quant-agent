"""The intraday scan ranks movers by move / own ATR, has no flat move
trigger, and sends ONE paid batch per check: the strongest-ranked five
(owner ruling 2026-10-12). The paid-analysis budget is a backstop.

ATR is expressed as a percent of the prior close (prev=100 here, so an ATR
of 8.0 is an 8%/day name). No name has been looked at this session, so
every move is measured from the prior close.
"""

import json
from unittest.mock import MagicMock, patch

from src.cost_circuit import PaidAnalysisSuspended
from src.pipeline_context import RunContext
from tests.test_intraday_scan import _intraday_pipeline, _snapshot

_DROP_REASONS = {
    "no_atr",
    "budget_refused",
    "over_name_cap",
    "below_move_threshold",
    "not_sent_weaker_rank",
}


def _scan(moves: dict, atrs: dict, *, require_paid=None):
    """Run one scan; return (submitted symbols in order, {symbol: dropped event})."""
    p = _intraday_pipeline(universe=list(moves))
    p.broker.get_intraday_snapshots.return_value = {s: _snapshot(last=100.0 + m, prev=100.0) for s, m in moves.items()}
    p.db.get_session_reference_price.return_value = None
    p._atr_for_symbol = MagicMock(side_effect=lambda s: atrs.get(s))
    if require_paid is not None:
        p._require_paid_analysis = require_paid
    p.tech_analyst.analyze_batch.return_value = ({}, None)
    events = []
    with (
        patch("src.pipeline_intraday.compute_indicators", return_value=MagicMock()),
        patch("src.pipeline_candidate_records._persist_evidence", side_effect=lambda db, **kw: events.append(kw)),
    ):
        try:
            p._run_intraday_opportunity_scan(RunContext.start("intra_check"))
        except PaidAnalysisSuspended:
            # Nothing sent: the suspension is the scan's outcome, as before.
            pass
    calls = [[d["symbol"] for d in c.args[0]] for c in p.tech_analyst.analyze_batch.call_args_list]
    submitted = [s for batch in calls for s in batch]
    dropped = {}
    for e in events:
        if e.get("kind") != "pipeline_event":
            continue
        ev = json.loads(e["evidence_json"])
        if ev.get("reason") in _DROP_REASONS:
            dropped[e["symbol"]] = ev
    _scan.last_calls = calls
    return submitted, dropped


def test_ranked_by_move_over_own_atr_not_raw_move():
    # A: 4% on an 8%/day name = 0.5x; B: 2% on 1% = 2x; C: 1% on 0.25% = 4x.
    submitted, _ = _scan({"A": 4.0, "B": 2.0, "C": 1.0}, {"A": 8.0, "B": 1.0, "C": 0.25})
    assert submitted == ["C", "B", "A"]


def test_low_move_high_relative_name_is_sent_and_outranks_big_mover():
    # 1% is below the old flat 3% trigger, but it is 4 ATRs on a quiet name.
    submitted, dropped = _scan({"QUIET": 1.0, "WILD": 6.0}, {"QUIET": 0.25, "WILD": 12.0})
    assert submitted[0] == "QUIET"
    assert "QUIET" not in dropped


def test_one_paid_batch_per_check_strongest_first():
    # Was test_no_cap_at_five (pinned the loop of batches until the cap).
    # Owner ruling 2026-10-12: later batches are weaker moves; send one.
    moves = {f"S{i}": 1.0 + i for i in range(8)}
    atrs = {s: 2.0 for s in moves}
    submitted, dropped = _scan(moves, atrs)
    assert [len(batch) for batch in _scan.last_calls] == [5]
    assert submitted == ["S7", "S6", "S5", "S4", "S3"]
    for rank, sym in ((6, "S2"), (7, "S1"), (8, "S0")):
        assert dropped[sym]["reason"] == "not_sent_weaker_rank" and dropped[sym]["rank"] == rank


def test_budget_refusal_before_the_one_call_records_every_mover():
    """Was test_budget_refusal_ends_sending_in_rank_order (pinned a refusal
    before a SECOND call). The cap stays as a backstop before the one call:
    a refusal sends nothing and records every mover budget_refused by rank."""

    def require_paid(agent_name):
        raise PaidAnalysisSuspended("daily cost limit reached", {"suspended": True})

    moves = {f"S{i}": 1.0 for i in range(7)}
    atrs = {f"S{i}": 1.0 / (i + 1) for i in range(7)}  # S0 lowest multiple ... S6 highest
    submitted, dropped = _scan(moves, atrs, require_paid=require_paid)
    assert submitted == []
    assert dropped["S6"]["reason"] == "budget_refused" and dropped["S6"]["rank"] == 1
    assert dropped["S0"]["reason"] == "budget_refused" and dropped["S0"]["rank"] == 7


def test_name_without_atr_is_dropped_with_no_atr_reason():
    submitted, dropped = _scan({"OK": 2.0, "NOATR": 9.0}, {"OK": 1.0, "NOATR": None})
    assert submitted == ["OK"]
    assert dropped["NOATR"]["reason"] == "no_atr"
