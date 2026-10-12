"""`GET /analysts/scorecard` — shape, read-only posture, isolation contract.

The endpoint projects the conviction ledger's persisted `conviction_credit`
and `seat_stance` evidence rows (spec §9.5) into the analyst scorecard panel's
contract. These tests seed a real temp SQLite database through the ordinary
writer (`src.storage.db.Database` — fine for a test, never for `src/api/`) and
point `db_reads.get_db_path` at it, the same pattern
`tests/test_api_research.py` uses.

The rows written here are byte-identical in shape to the ones
`Database.record_seat_stances` / `Database.resolve_conviction_ledger` write on
`feat/conviction-ledger-recording`: same table (`specialist_evidence`), same
`kind` values, same JSON payload keys. That is deliberate — this endpoint has
to keep working against rows this test file does not itself define.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.api.db_reads as db_reads
from src.api.routes_scorecard import build_scorecard
from src.api.server import app
from src.storage.db import Database

REPO_ROOT = Path(__file__).parent.parent


def _credit(**kw) -> str:
    """A `conviction_credit` payload as the ledger writes one TODAY: raw
    signed R, no `weight` key (owner decision, 2026-08-31). `credit` is set
    to match `r_multiple`/`side` for readability — the read path derives it
    either way, which is what makes a pre-2026-08-31 weighted row readable
    without a migration (see `test_a_weighted_legacy_row_...` below)."""
    payload = {
        "seat": "technical",
        "symbol": "AAPL",
        "side": "supported",
        "stance": "buy",
        "conviction": "high",
        "r_multiple": 2.0,
        "credit": 2.0,
        "resolved_at": "2026-06-10 15:00:00",
        "position_id": "pos-1",
        "decision_id": "d-1",
        "direction": "long",
        "nominated": True,
    }
    payload.update(kw)
    return json.dumps(payload, sort_keys=True)


def _stance(**kw) -> str:
    payload = {
        "seat": "technical",
        "symbol": "AAPL",
        "stance": "buy",
        "conviction": "high",
        "nominated": True,
        "observation": "Broke out of a six-week base on heavy volume.",
    }
    payload.update(kw)
    return json.dumps(payload, sort_keys=True)


def _insert(
    db: Database,
    *,
    agent: str,
    kind: str,
    symbol: str,
    payload: str,
    decision_id: str = "d-1",
    timestamp: str = "2026-06-10 15:00:00",
) -> None:
    db.conn.execute(
        "INSERT INTO specialist_evidence (run_id,decision_id,agent_name,kind,scope,"
        "symbol,evidence_json,timestamp) VALUES (?,?,?,?,?,?,?,?)",
        ("run-1", decision_id, agent, kind, "symbol", symbol, payload, timestamp),
    )


def _seed(tmp_path, monkeypatch, *, populated: bool = True) -> Path:
    path = tmp_path / "scorecard.db"
    db = Database(str(path))
    db.initialize()
    if populated:
        # One winning idea (AAPL, +2R) and one losing idea (MSFT, -1R) where
        # the loser's dissenter is paid for having been right to object.
        _insert(db, agent="technical", kind="seat_stance", symbol="AAPL", payload=_stance())
        _insert(
            db,
            agent="news",
            kind="seat_stance",
            symbol="AAPL",
            payload=_stance(
                seat="news", stance="bearish", conviction="low", nominated=False, observation="Guidance reads soft."
            ),
        )
        _insert(db, agent="technical", kind="conviction_credit", symbol="AAPL", payload=_credit())
        _insert(
            db,
            agent="news",
            kind="conviction_credit",
            symbol="AAPL",
            payload=_credit(
                seat="news", side="opposed", stance="bearish", conviction="low", credit=-2.0, nominated=False
            ),
        )

        _insert(
            db,
            agent="technical",
            kind="seat_stance",
            symbol="MSFT",
            decision_id="d-2",
            payload=_stance(symbol="MSFT"),
            timestamp="2026-07-14 15:00:00",
        )
        _insert(
            db,
            agent="technical",
            kind="conviction_credit",
            symbol="MSFT",
            decision_id="d-2",
            timestamp="2026-07-14 15:00:00",
            payload=_credit(
                symbol="MSFT",
                r_multiple=-1.0,
                credit=-1.0,
                position_id="pos-2",
                decision_id="d-2",
                resolved_at="2026-07-14 15:00:00",
            ),
        )
        _insert(
            db,
            agent="news",
            kind="conviction_credit",
            symbol="MSFT",
            decision_id="d-2",
            timestamp="2026-07-14 15:00:00",
            payload=_credit(
                seat="news",
                symbol="MSFT",
                side="opposed",
                stance="bearish",
                conviction="medium",
                r_multiple=-1.0,
                credit=1.0,
                position_id="pos-2",
                decision_id="d-2",
                nominated=False,
                resolved_at="2026-07-14 15:00:00",
            ),
        )
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))
    return path


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_scorecard_returns_per_analyst_record_with_raw_counts(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    body = TestClient(app).get("/analysts/scorecard").json()

    assert body["state"] == "populated"
    assert body["resolved_calls_total"] == 4
    assert body["risk_dollars_per_call"] == 100.0
    assert body["months"] == ["2026-06", "2026-07"]

    by_name = {a["analyst"]: a for a in body["analysts"]}
    assert set(by_name) == {"technical", "news"}

    tech = by_name["technical"]
    assert tech["resolved_calls"] == 2
    assert tech["calls_right"] == 1
    assert tech["hit_rate_pct"] == 50.0
    assert tech["avg_win"] == 2.0
    # Returned negative, not as a magnitude.
    assert tech["avg_loss"] == -1.0
    assert tech["cumulative_credit"] == 1.0
    assert [p["cumulative"] for p in tech["cumulative"]] == [2.0, 1.0]
    assert tech["peak"] == 2.0
    assert tech["below_best"] == 1.0
    assert tech["below_best_since"] == "2026-06-10 15:00:00"
    assert tech["calls_since_peak"] == 1

    news = by_name["news"]
    # The dissenter on the losing trade is paid for having been right, and
    # charged in full for having been wrong on the winner: -2.0 then +1.0.
    assert [p["cumulative"] for p in news["cumulative"]] == [-2.0, -1.0]
    assert news["cumulative_credit"] == -1.0
    assert news["calls_right"] == 1
    # Its own best is the 0.0 it started from.
    assert news["peak"] == 0.0
    assert news["below_best"] == 1.0

    # Ranked best-first by money made.
    assert [a["analyst"] for a in body["analysts"]] == ["technical", "news"]


def test_credit_is_raw_r_with_confidence_reported_not_applied(tmp_path, monkeypatch):
    """Owner decision, 2026-08-31. Two analysts, same idea, same side, one
    "high" and one "low" — identical credit, and the confidence surfaced as
    its own breakdown row instead of as a multiplier."""
    _seed(tmp_path, monkeypatch)
    body = TestClient(app).get("/analysts/scorecard").json()

    aapl = next(i for i in body["ideas"] if i["symbol"] == "AAPL")
    backer = aapl["supported"][0]
    objector = aapl["opposed"][0]
    assert backer["conviction"] == "high" and objector["conviction"] == "low"
    assert abs(backer["credit"]) == abs(objector["credit"]) == 2.0
    assert backer["credit"] == -objector["credit"]
    # The multiplier is gone from the wire contract entirely.
    assert "weight" not in backer


def test_per_confidence_breakdown_splits_the_same_calls(tmp_path, monkeypatch):
    """news declared "low" on the AAPL winner it opposed (-2.0) and "medium"
    on the MSFT loser it opposed (+1.0). Hand-computed, high-first order."""
    _seed(tmp_path, monkeypatch)
    body = TestClient(app).get("/analysts/scorecard").json()
    news = next(a for a in body["analysts"] if a["analyst"] == "news")

    assert [b["conviction"] for b in news["by_confidence"]] == ["medium", "low"]
    medium, low = news["by_confidence"]
    assert (medium["resolved_calls"], medium["calls_right"]) == (1, 1)
    assert medium["avg_win"] == 1.0 and medium["avg_loss"] is None
    assert medium["cumulative_credit"] == 1.0
    assert medium["hit_rate_pct"] == 100.0
    assert (low["resolved_calls"], low["calls_right"]) == (1, 0)
    assert low["avg_win"] is None and low["avg_loss"] == -2.0
    assert low["cumulative_credit"] == -2.0

    # A partition of the same rows, so it sums back to the headline figures.
    assert sum(b["resolved_calls"] for b in news["by_confidence"]) == news["resolved_calls"]
    assert sum(b["cumulative_credit"] for b in news["by_confidence"]) == news["cumulative_credit"]

    # technical declared "high" on both of its calls: one bucket, not two.
    tech = next(a for a in body["analysts"] if a["analyst"] == "technical")
    assert [b["conviction"] for b in tech["by_confidence"]] == ["high"]
    assert tech["by_confidence"][0]["resolved_calls"] == 2


def test_a_weighted_legacy_row_is_read_back_unweighted(tmp_path, monkeypatch):
    """A row written before 2026-08-31 stored `credit = r_multiple x weight`.
    Nothing migrates it; the read path recomputes from the stored unweighted
    `r_multiple` and `side`, so one series never mixes two scales."""
    path = tmp_path / "legacy.db"
    db = Database(str(path))
    db.initialize()
    _insert(
        db,
        agent="macro",
        kind="conviction_credit",
        symbol="NVDA",
        payload=json.dumps(
            {
                "seat": "macro",
                "symbol": "NVDA",
                "side": "supported",
                "stance": "buy",
                "conviction": "low",
                "weight": 0.3,
                "r_multiple": 2.0,
                "credit": 0.6,  # the old weighted scale
                "resolved_at": "2026-06-10 15:00:00",
                "position_id": "pos-old",
                "decision_id": "d-old",
                "direction": "long",
                "nominated": False,
            },
            sort_keys=True,
        ),
    )
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))

    body = TestClient(app).get("/analysts/scorecard").json()
    macro = body["analysts"][0]
    assert macro["cumulative_credit"] == 2.0, "the stored 0.6 was the old scale"
    assert macro["by_confidence"][0]["conviction"] == "low"


def test_a_short_reads_exactly_like_a_long(tmp_path, monkeypatch):
    """A profitable short is a positive number for its backer and a negative
    one for its objector — the same convention, wording and side of zero a
    long uses. `direction` is carried for description and inverts nothing."""
    path = tmp_path / "short.db"
    db = Database(str(path))
    db.initialize()
    _insert(
        db,
        agent="technical",
        kind="conviction_credit",
        symbol="TSLA",
        payload=_credit(
            seat="technical",
            symbol="TSLA",
            stance="sell",
            direction="short",
            r_multiple=2.0,
            credit=2.0,
            position_id="pos-s",
        ),
    )
    _insert(
        db,
        agent="news",
        kind="conviction_credit",
        symbol="TSLA",
        payload=_credit(
            seat="news",
            symbol="TSLA",
            side="opposed",
            stance="positive",
            conviction="low",
            direction="short",
            r_multiple=2.0,
            credit=-2.0,
            position_id="pos-s",
            nominated=False,
        ),
    )
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))

    body = TestClient(app).get("/analysts/scorecard").json()
    idea = body["ideas"][0]
    assert idea["direction"] == "short"
    assert idea["r_multiple"] == 2.0
    assert idea["supported"][0]["credit"] == 2.0
    assert idea["opposed"][0]["credit"] == -2.0

    by_name = {a["analyst"]: a for a in body["analysts"]}
    assert by_name["technical"]["calls_right"] == 1
    assert by_name["technical"]["cumulative_credit"] == 2.0
    assert by_name["news"]["calls_right"] == 0
    assert by_name["news"]["cumulative_credit"] == -2.0


def test_monthly_waterfall_steps_sum_to_the_running_total(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    body = TestClient(app).get("/analysts/scorecard").json()
    tech = next(a for a in body["analysts"] if a["analyst"] == "technical")

    assert [(m["month"], m["credit"], m["cumulative"]) for m in tech["monthly"]] == [
        ("2026-06", 2.0, 2.0),
        ("2026-07", -1.0, 1.0),
    ]
    assert [m["hit_rate_pct"] for m in tech["monthly"]] == [100.0, 50.0]
    assert tech["monthly"][-1]["cumulative"] == tech["cumulative_credit"]


def test_idea_trace_carries_both_sides_and_each_analysts_own_reason(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    body = TestClient(app).get("/analysts/scorecard").json()

    # Newest resolved idea first.
    assert [i["symbol"] for i in body["ideas"]] == ["MSFT", "AAPL"]
    aapl = next(i for i in body["ideas"] if i["symbol"] == "AAPL")
    assert aapl["r_multiple"] == 2.0
    assert aapl["direction"] == "long"
    assert [p["analyst"] for p in aapl["supported"]] == ["technical"]
    assert aapl["supported"][0]["nominated"] is True
    assert aapl["supported"][0]["reason"].startswith("Broke out of a six-week base")
    assert [p["analyst"] for p in aapl["opposed"]] == ["news"]
    assert aapl["opposed"][0]["credit"] == -2.0
    assert aapl["opposed"][0]["reason"] == "Guidance reads soft."

    # A credit with no surviving stance row gets no invented reason.
    msft = next(i for i in body["ideas"] if i["symbol"] == "MSFT")
    assert msft["opposed"][0]["reason"] == ""


def test_idea_limit_is_bounded_and_validated(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    assert len(client.get("/analysts/scorecard?idea_limit=1").json()["ideas"]) == 1
    assert client.get("/analysts/scorecard?idea_limit=0").status_code == 422
    assert client.get("/analysts/scorecard?idea_limit=9999").status_code == 422


def test_no_minimum_sample_gate_hides_an_analyst(tmp_path, monkeypatch):
    """Spec §9.5 item 8: show raw counts, never hide a score behind a floor."""
    path = tmp_path / "one_call.db"
    db = Database(str(path))
    db.initialize()
    _insert(
        db,
        agent="macro",
        kind="conviction_credit",
        symbol="NVDA",
        payload=_credit(seat="macro", symbol="NVDA", credit=0.3, r_multiple=0.3),
    )
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))

    body = TestClient(app).get("/analysts/scorecard").json()
    assert body["analysts"][0]["analyst"] == "macro"
    assert body["analysts"][0]["resolved_calls"] == 1


# ---------------------------------------------------------------------------
# Degradation — an unreadable ledger is never presented as a quiet desk
# ---------------------------------------------------------------------------


def test_empty_ledger_is_empty_not_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, populated=False)
    body = TestClient(app).get("/analysts/scorecard").json()
    assert body["state"] == "empty"
    assert body["read_error"] is None
    assert body["analysts"] == []
    assert body["ideas"] == []
    assert body["resolved_calls_total"] == 0


def test_unreadable_database_is_a_typed_error_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(tmp_path / "missing.db"))
    response = TestClient(app).get("/analysts/scorecard")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "error"
    assert body["read_error"] == "conviction ledger unavailable"
    assert body["analysts"] == []


def test_malformed_credit_row_is_skipped_not_counted_as_break_even(tmp_path, monkeypatch):
    path = tmp_path / "malformed.db"
    db = Database(str(path))
    db.initialize()
    _insert(db, agent="technical", kind="conviction_credit", symbol="AAPL", payload="{not json")
    _insert(
        db,
        agent="technical",
        kind="conviction_credit",
        symbol="AAPL",
        payload=json.dumps({"seat": "technical", "symbol": "AAPL"}),
    )
    _insert(db, agent="technical", kind="conviction_credit", symbol="AAPL", payload=_credit())
    db.conn.commit()
    db.close()
    monkeypatch.setattr(db_reads, "get_db_path", lambda: str(path))

    body = TestClient(app).get("/analysts/scorecard").json()
    assert body["resolved_calls_total"] == 1
    assert body["analysts"][0]["resolved_calls"] == 1


# ---------------------------------------------------------------------------
# Read-only / isolation contract
# ---------------------------------------------------------------------------


def test_endpoint_writes_nothing_to_the_database(tmp_path, monkeypatch):
    path = _seed(tmp_path, monkeypatch)
    before = path.read_bytes()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as probe:
        rows_before = probe.execute("SELECT COUNT(*) FROM specialist_evidence").fetchone()[0]

    for _ in range(3):
        assert TestClient(app).get("/analysts/scorecard").status_code == 200

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as probe:
        rows_after = probe.execute("SELECT COUNT(*) FROM specialist_evidence").fetchone()[0]
    assert rows_after == rows_before
    assert path.read_bytes() == before


def test_scorecard_route_rejects_every_non_read_method(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    client = TestClient(app)
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/analysts/scorecard").status_code == 405


def test_scorecard_module_imports_no_trading_or_risk_module():
    """The Stage 2 isolation invariant, asserted for this file specifically.

    `tests/test_api_safety.py` already parametrizes over every file in
    `src/api/`; this is the same assertion stated where the scorecard's own
    reason for tension lives — `src.conviction_ledger` computes exactly the
    aggregation this route mirrors, and importing it would drag `src.risk`
    into the API package.
    """
    tree = ast.parse((REPO_ROOT / "src" / "api" / "routes_scorecard.py").read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = ("src.risk", "src.pipeline", "src.conviction_ledger", "src.storage")
    assert not [m for m in imported if any(m == p or m.startswith(p + ".") for p in forbidden)], imported


def test_projection_matches_the_ledgers_own_aggregate_when_it_is_available():
    """Drift guard for the aggregation this route deliberately mirrors.

    `src.conviction_ledger` lives on the trading side and `src/api` may not
    import it (see the route's module docstring). This test may — so whenever
    that module exists, the two implementations are compared against identical
    input and must agree on every field they both produce. It skips on a
    checkout where the ledger has not landed yet.
    """
    ledger = pytest.importorskip(
        "src.conviction_ledger",
        reason="conviction ledger not present on this checkout",
    )
    rows = [
        {
            "analyst": "technical",
            "symbol": "AAPL",
            "side": "supported",
            "stance": "buy",
            "conviction": "high",
            "r_multiple": 2.0,
            "credit": 2.0,
            "resolved_at": "2026-06-10 15:00:00",
            "position_id": "pos-1",
            "decision_id": "d-1",
            "direction": "long",
            "nominated": True,
        },
        {
            "analyst": "technical",
            "symbol": "MSFT",
            "side": "supported",
            "stance": "buy",
            "conviction": "low",
            "r_multiple": -1.0,
            "credit": -1.0,
            "resolved_at": "2026-07-14 15:00:00",
            "position_id": "pos-2",
            "decision_id": "d-2",
            "direction": "long",
            "nominated": False,
        },
        {
            "analyst": "news",
            "symbol": "TSLA",
            "side": "opposed",
            "stance": "bullish",
            "conviction": "medium",
            "r_multiple": -1.0,
            "credit": 1.0,
            "resolved_at": "2026-07-14 15:00:00",
            "position_id": "pos-3",
            "decision_id": "d-3",
            "direction": "short",
            "nominated": False,
        },
    ]
    mine = {a.analyst: a for a in build_scorecard({"read_error": None, "credits": rows, "stances": []}).analysts}
    theirs = ledger.aggregate_seat_records(
        [
            ledger.SeatCredit(
                seat=r["analyst"],
                symbol=r["symbol"],
                side=r["side"],
                stance=r["stance"],
                conviction=r["conviction"],
                r_multiple=r["r_multiple"],
                credit=r["credit"],
                resolved_at=r["resolved_at"],
                position_id=r["position_id"],
                decision_id=r["decision_id"],
                direction=r["direction"],
                nominated=r["nominated"],
            )
            for r in rows
        ]
    )

    assert set(mine) == set(theirs)
    for name, record in theirs.items():
        item = mine[name]
        assert item.resolved_calls == record.resolved_calls
        assert item.calls_right == record.calls_right
        assert item.avg_win == record.avg_win
        assert item.avg_loss == record.avg_loss
        assert item.cumulative_credit == record.cumulative_credit
        assert item.peak == record.peak
        assert item.below_best == record.current_drawdown
        assert item.hit_rate_pct == record.win_rate_pct
        assert [(p.resolved_at, p.cumulative) for p in item.cumulative] == record.cumulative
        # ...including the per-confidence breakdown, which both sides build
        # independently and which must not drift apart either.
        assert [
            (b.conviction, b.resolved_calls, b.calls_right, b.avg_win, b.avg_loss, b.cumulative_credit)
            for b in item.by_confidence
        ] == [
            (b.conviction, b.resolved_calls, b.calls_right, b.avg_win, b.avg_loss, b.cumulative_credit)
            for b in record.by_confidence
        ]
