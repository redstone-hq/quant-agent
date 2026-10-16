"""Owner decision 2026-10-15 (q07): the stock-swap rule's brake is the desk's
own measured score noise, not a typed 25%. The rule stays switched off."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from src.models import AnalystVerdict, VerdictEvidence
from src.rotation import evaluate_rotation
from src.rotation_parts.score_noise import evidence_key, measure_score_noise
from src.rotation_parts.score_noise_store import measure_score_noise_from_db
from src.verdicts import rank_verdicts

REPO = Path(__file__).resolve().parents[1]


def _cand(symbol: str, magnitude: float, conviction: str = "low", as_of: date | None = None):
    evidence = [VerdictEvidence(label="trend", value=1.0)]
    if as_of is not None:
        evidence.append(VerdictEvidence(label="filing", as_of=as_of))
    verdict = AnalystVerdict(
        seat="technical",
        symbol=symbol,
        direction="bullish",
        magnitude=magnitude,
        conviction=conviction,
        evidence=evidence,
        invalidation="breaks support",
    )
    return rank_verdicts([verdict])[0]


def _run(new, held, brake):
    return evaluate_rotation(
        ranked=[new, held],
        blocked={},
        held_symbols={held.symbol},
        headroom_pct=0.1,
        floor_pct=0.5,
        noise_brake=brake,
    )


def test_gap_equal_to_measured_noise_does_not_swap_and_larger_does():
    new, held = _cand("NEW", 1.0, "high"), _cand("OLD", 0.75, "medium")
    gap = new.score - held.score
    equal = _run(new, held, gap)
    assert equal.opportunity is None
    assert equal.refusal.point == "full_composite_margin_not_cleared"
    larger = _run(new, held, gap / 2)
    assert larger.opportunity is not None
    assert larger.opportunity.tier == "ranked_margin"
    assert larger.opportunity.noise_brake == gap / 2


def test_unmeasured_noise_refuses_with_a_recorded_reason():
    outcome = _run(_cand("NEW", 1.0, "high"), _cand("OLD", 0.1, "medium"), None)
    assert outcome.opportunity is None
    row = outcome.refusal.event_kwargs()
    assert row["reason"] == "score_noise_unmeasured"
    assert row["noise_brake"] is None
    assert "unmeasured" in row["detail"]
    assert row["held_evidence_key"] and row["new_evidence_key"]


def test_prompt_shows_the_measured_value():
    from src.agents.portfolio_manager import PortfolioManagerAgent

    kwargs = dict(
        ranked=[_cand("NEW", 1.0, "high"), _cand("HELD", 0.75, "medium")],
        blocked={},
        held_symbols={"HELD"},
        existing_risk_pct={"HELD": 24.9},
        ceiling_pct=25.0,
    )
    precheck = PortfolioManagerAgent.rotation_precheck(score_noise_brake=0.3125, **kwargs)
    text = PortfolioManagerAgent._render_rotation_section(precheck=precheck, **kwargs)
    assert "0.3125" in text
    assert "25%" not in text


def test_brake_is_the_largest_no_news_change_and_news_pairs_are_excluded():
    quiet = evidence_key(_cand("A", 0.5))
    news = evidence_key(_cand("A", 0.5, as_of=date(2026, 10, 13)))
    rows = [
        ("2026-10-12 14:00:00", "r1", "A", 1.0, quiet),
        ("2026-10-12 15:00:00", "r2", "A", 1.3, quiet),  # no news: 0.3
        ("2026-10-13 14:00:00", "r3", "A", 2.9, news),  # new dated fact: excluded
        ("2026-10-13 15:00:00", "r4", "A", 2.8, news),  # no news: 0.1
    ]
    brake = measure_score_noise(rows)
    assert brake.value == 0.3 and brake.pairs == 2
    assert measure_score_noise(rows[:1]).value is None


def test_db_measurement_ignores_rows_before_the_clean_record():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE specialist_evidence (run_id TEXT, kind TEXT, evidence_json TEXT, timestamp TEXT)")
    key = evidence_key(_cand("A", 0.5))

    def put(ts, run, score):
        payload = {"stage": "rotation", "held_symbol": "A", "held_score": score, "held_evidence_key": key}
        conn.execute(
            "INSERT INTO specialist_evidence VALUES (?, 'pipeline_event', ?, ?)",
            (run, json.dumps(payload, sort_keys=True), ts),
        )

    put("2026-10-09 14:00:00", "old1", 1.0)
    put("2026-10-09 15:00:00", "old2", 3.0)
    assert measure_score_noise_from_db(conn).value is None
    put("2026-10-12 14:00:00", "r1", 1.0)
    put("2026-10-12 15:00:00", "r2", 1.2)
    brake = measure_score_noise_from_db(conn)
    assert brake.pairs == 1 and abs(brake.value - 0.2) < 1e-9


def test_no_typed_25_percent_margin_remains():
    hits = subprocess.run(
        ["git", "grep", "-l", "ROTATION_MARGIN_PCT", "--", "src", "config"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert hits == ""
