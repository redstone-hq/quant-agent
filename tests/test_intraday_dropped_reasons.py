"""Every name the intraday scan drops leaves a recorded reason (pipeline event)."""

import json
from unittest.mock import patch

from src.pipeline_context import RunContext
from tests.test_intraday_scan import _intraday_pipeline, _snapshot


def test_dropped_names_each_record_their_reason():
    p = _intraday_pipeline(universe=["NOATR", "HOT", "A", "B"])
    p._atr_for_symbol.side_effect = lambda sym: None if sym == "NOATR" else 2.0
    p.broker.get_intraday_snapshots.return_value = {
        "NOATR": _snapshot(last=101.0, prev=100.0),
        "HOT": _snapshot(last=110.0, prev=100.0),
        "A": _snapshot(last=108.0, prev=100.0),
        "B": _snapshot(last=105.0, prev=100.0),
    }
    # HOT was looked at earlier this session at today's price: it has not
    # moved since, so it ranks last, but no clock drops it.
    p.db.get_session_reference_price.side_effect = lambda sym: 110.0 if sym == "HOT" else None
    ctx = RunContext.start("intra_check")
    events = []
    with patch("src.pipeline_candidate_records._persist_evidence", side_effect=lambda db, **kw: events.append(kw)):
        with (
            patch.object(type(p), "_await_paid_scan_slot", return_value=True),
            patch.object(type(p), "_intraday_paid_scan_skip", return_value={}),
        ):
            p._run_intraday_opportunity_scan(ctx)
    got = {e["symbol"]: json.loads(e["evidence_json"]) for e in events if e["kind"] == "pipeline_event"}
    assert got["NOATR"]["reason"] == "no_atr" and got["NOATR"]["move_pct"] == 1.0
    assert got["NOATR"]["move_ref"] == "prev_close"
    assert "HOT" not in got  # no re-look clock: a name looked at this session is ranked, not dropped
    assert "A" not in got and "B" not in got  # no name cap: both ranked names are kept
