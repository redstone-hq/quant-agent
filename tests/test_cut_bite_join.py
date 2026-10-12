"""The cut-bite observation is assembled from two points in time, by run id.

Three of the four fields a cut-bite ledger row asks for are known where the
cut happens; the seat's verdict is formed later in the same session. These
tests pin that the cut-site record is DURABLY written, that the verdict joins
to it on `run_id`, that a run with no verdict yields NO observation rather
than a three-of-four one, and that none of the recording changes the prompt
text the seat actually reads.

Everything here runs against a real sqlite connection carrying the real
schema, so a green test means the value was written and read back, not that a
column name happened to match a guard's word list.
"""

import sqlite3

import pytest

from src.prompt_facts.review.blocked import CUT_SITE, ReviewBlocked
from src.storage.analytics.cut_bite import (
    complete_cut_bite_observations,
    read_cut_bite,
    record_cut_bite,
)
from src.storage.schema.manager import DatabaseSchema
from src.storage.schema.sentinel_tables import ensure_sentinel_tables


class _Db:
    """The smallest thing the builder and the reader both accept: a connection."""

    def __init__(self, conn, funnel):
        self.conn = conn
        self._funnel = funnel

    def get_proposal_funnel_rows(self, since):
        return self._funnel


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DatabaseSchema(conn=conn)._create_tables()
    ensure_sentinel_tables(conn=conn)
    return conn


def _funnel():
    """Four never-filled symbols proposed 3,3,2,1 times, plus one that filled.

    At min_proposals=3 the repeat list is AAA and BBB, so that cut's edge is
    4 -> 2. It is read off this fixture by hand, not off the code.
    """
    evidence = []
    for sym, times in (("AAA", 3), ("BBB", 3), ("CCC", 2), ("DDD", 1), ("EEE", 1)):
        for i in range(times):
            did = f"{sym}-{i}"
            evidence.append(
                {
                    "kind": "target",
                    "decision_id": did,
                    "symbol": sym,
                    "timestamp": f"2026-09-{10 + i:02d}T14:00:00",
                    "evidence_json": '{"risk_allocation_pct": 1.0}',
                }
            )
    trades = [{"decision_id": "EEE-0", "symbol": "EEE", "fill_status": "filled"}]
    return {"evidence": evidence, "trades": trades}


def test_cut_site_record_is_durably_written_and_read_back():
    conn = _conn()
    db = _Db(conn, _funnel())
    ReviewBlocked(db=db)._build_blocked_proposals(
        lookback_days=365,
        min_proposals=3,
        run_id="RUN-1",
    )
    # Read the raw table, not the helper, so the helper cannot pass by
    # returning something it never persisted.
    raw = conn.execute("SELECT kind, run_id, detail FROM reconciliation_runs").fetchall()
    assert len(raw) == 1
    assert raw[0]["kind"] == f"cut_bite:{CUT_SITE}"
    assert raw[0]["run_id"] == "RUN-1"
    assert '"before": 4' in raw[0]["detail"]

    seen = read_cut_bite(db=db, site=CUT_SITE)
    assert len(seen) == 1
    assert seen[0]["cuts"]["min_proposals"] == {"before": 4, "survived": 2}
    assert seen[0]["oldest_surviving_age_days"] is not None


def test_a_run_with_no_verdict_yields_no_observation():
    """Three of four fields is not an observation and must not count as one."""
    conn = _conn()
    db = _Db(conn, _funnel())
    ReviewBlocked(db=db)._build_blocked_proposals(
        lookback_days=365,
        min_proposals=3,
        run_id="RUN-1",
    )
    assert read_cut_bite(db=db, site=CUT_SITE)[0]["complete"] is False
    assert complete_cut_bite_observations(db=db, site=CUT_SITE) == []


def test_the_verdict_joins_on_run_id_and_only_on_its_own_run():
    conn = _conn()
    db = _Db(conn, _funnel())
    ReviewBlocked(db=db)._build_blocked_proposals(
        lookback_days=365,
        min_proposals=3,
        run_id="RUN-1",
    )
    # A verdict belonging to a DIFFERENT session must not complete this one.
    conn.execute(
        "INSERT INTO agent_logs (agent_name, run_id, output_summary) VALUES (?,?,?)",
        ("portfolio_manager", "RUN-2", "someone else's verdict"),
    )
    conn.commit()
    assert complete_cut_bite_observations(db=db, site=CUT_SITE) == []

    conn.execute(
        "INSERT INTO agent_logs (agent_name, run_id, output_summary) VALUES (?,?,?)",
        ("portfolio_manager", "RUN-1", "BUY AAA"),
    )
    conn.commit()
    done = complete_cut_bite_observations(db=db, site=CUT_SITE)
    assert len(done) == 1
    assert done[0]["verdict"] == "BUY AAA"
    assert done[0]["cuts"]["min_proposals"] == {"before": 4, "survived": 2}
    assert done[0]["oldest_surviving_age_days"] is not None


def test_recording_does_not_change_the_prompt_text():
    """The seat must read exactly what it read before the recording existed."""
    with_run = ReviewBlocked(db=_Db(_conn(), _funnel()))._build_blocked_proposals(
        lookback_days=365,
        min_proposals=3,
        run_id="RUN-1",
    )
    without = ReviewBlocked(db=_Db(_conn(), _funnel()))._build_blocked_proposals(
        lookback_days=365,
        min_proposals=3,
    )
    assert with_run == without
    assert "AAA" in with_run


def test_a_record_with_no_run_id_is_refused_not_orphaned():
    """An unjoinable row would look like evidence and could never become one."""
    conn = _conn()
    db = _Db(conn, _funnel())
    assert (
        record_cut_bite(
            db=db,
            run_id=None,
            site=CUT_SITE,
            cuts={"min_proposals": {"before": 4, "survived": 2}},
            oldest_surviving_age_days=1.0,
        )
        is False
    )
    assert conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0] == 0


@pytest.mark.parametrize("empty", [{"evidence": [], "trades": []}])
def test_no_proposals_records_nothing(empty):
    """No candidates means no cut happened; an empty row is not an observation."""
    conn = _conn()
    db = _Db(conn, empty)
    ReviewBlocked(db=db)._build_blocked_proposals(run_id="RUN-1")
    assert conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0] == 0
