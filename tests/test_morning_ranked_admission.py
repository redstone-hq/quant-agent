"""Morning score-order send (owner ruling 2026-10-14).

Held names, then run-scoped SEC admissions, then the configured universe, then
last night's screened passers best score K first; only the top N screened
names are sent and every other passer is recorded with its reason.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from src import universe_screen as us
from src.config import UniverseScreenConfig
from src.nominations import Nomination, _cap_per_seat
from src.stage_morning_research import order_morning_symbols
from tests.pipeline_factory import build_pipeline

REPO = Path(__file__).resolve().parents[1]


def _ctx(held=(), sec=()):
    return SimpleNamespace(
        run_id="r1",
        decision_id=None,
        positions=[SimpleNamespace(symbol=s, qty=10) for s in held],
        admitted_symbols=set(sec),
        smart_money_admissions={s: {"reason": "material_sec_form4_purchase"} for s in sec},
    )


def _details(scores: dict) -> dict:
    return {
        s: {
            "temporary": True,
            "reason": "universe_screen_admission",
            "k": k,
            "nightly_rank": None,
            "score_reason": None,
        }
        for s, k in scores.items()
    }


@pytest.fixture
def recorded(monkeypatch):
    events: list[dict] = []

    def _capture(owner, ctx, symbol, stage, outcome, reason="", **details):
        events.append({"symbol": symbol, "stage": stage, "outcome": outcome, "reason": reason, **details})

    monkeypatch.setattr("src.intraday.candidates._record_pipeline_event", _capture)
    return events


def test_order_is_held_then_sec_then_configured_then_screened_by_k(recorded):
    ctx = _ctx(held=["HELD"], sec=["SECX"])
    details = _details({"LOWK": 0.01, "HIGHK": 0.09, "MIDK": 0.05})
    order = order_morning_symbols(
        MagicMock(),
        ctx,
        configured_symbols=["CFG1", "CFG2"],
        ranked=["HIGHK", "MIDK", "LOWK"],
        screened_details=details,
        top_n=10,
    )
    assert order == ["HELD", "SECX", "CFG1", "CFG2", "HIGHK", "MIDK", "LOWK"]
    assert {"HIGHK", "MIDK", "LOWK", "SECX"} <= ctx.admitted_symbols
    assert recorded == []


def test_only_top_n_screened_are_sent_and_the_rest_recorded_with_rank(recorded):
    ctx = _ctx()
    ranked = ["AAA", "BBB", "CCC", "DDD"]
    order = order_morning_symbols(
        MagicMock(),
        ctx,
        configured_symbols=["SPY"],
        ranked=ranked,
        screened_details=_details({"AAA": 0.4, "BBB": 0.3, "CCC": 0.2, "DDD": 0.1}),
        top_n=2,
    )
    assert order == ["SPY", "AAA", "BBB"]
    assert ctx.admitted_symbols == {"AAA", "BBB"}
    assert [(e["symbol"], e["reason"], e["rank"]) for e in recorded] == [
        ("CCC", "not_sent_weaker_rank", 3),
        ("DDD", "not_sent_weaker_rank", 4),
    ]
    assert all(e["stage"] == "opportunity" and e["outcome"] == "dropped" for e in recorded)


def test_unscored_passer_is_recorded_and_never_ranked(recorded):
    ctx = _ctx()
    details = _details({"AAA": 0.4, "NOK": None})
    details["NOK"]["score_reason"] = "gross_ceiling_unread"
    order = order_morning_symbols(
        MagicMock(), ctx, configured_symbols=[], ranked=["AAA"], screened_details=details, top_n=5
    )
    assert order == ["AAA"]
    assert "NOK" not in ctx.admitted_symbols
    assert [(e["symbol"], e["reason"], e["score_reason"]) for e in recorded] == [
        ("NOK", "not_sent_unscored", "gross_ceiling_unread")
    ]
    assert "rank" not in recorded[0]


def test_screened_names_are_not_capped_at_three_but_other_seats_still_are(recorded):
    ctx = _ctx()
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    order = order_morning_symbols(
        MagicMock(),
        ctx,
        configured_symbols=[],
        ranked=names,
        screened_details=_details({s: 0.1 for s in names}),
        top_n=UniverseScreenConfig().morning_top_n,
    )
    assert order == names
    settings = yaml.safe_load((REPO / "config" / "settings.yaml").read_text())
    seat_cap = int(settings["nominations"]["max_per_seat_per_run"])
    offered = [Nomination.model_construct(symbol=s, conviction="high") for s in names]
    assert len(_cap_per_seat(offered, seat_cap)) == seat_cap == 3


def test_rank_passers_orders_by_k_and_keeps_unscored_out_of_the_ranking(tmp_path):
    rows = [
        {"symbol": "LOW", "status": "passed", "measured": {"atr_pct": 2.0}, "second_filter": {"k": 0.01, "rank": 2}},
        {"symbol": "HIGH", "status": "passed", "measured": {"atr_pct": 4.0}, "second_filter": {"k": 0.05, "rank": 1}},
        {"symbol": "NOK", "status": "passed", "measured": {}, "second_filter": {"k": None, "reason": "atr_unmeasured"}},
        {"symbol": "GONE", "status": "dropped", "failures": ["price_below_minimum"], "measured": {}},
    ]
    path = tmp_path / "2026-10-13.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    ranked, details = us.rank_passers(path)
    assert ranked == ["HIGH", "LOW"]
    assert set(details) == {"HIGH", "LOW", "NOK"}
    assert details["NOK"]["k"] is None and details["NOK"]["score_reason"] == "atr_unmeasured"
    assert details["HIGH"]["nightly_rank"] == 1 and details["HIGH"]["reason"] == "universe_screen_admission"


def test_admission_reads_last_completed_sessions_record_and_fails_closed_without_one(tmp_path):
    from src.trading_calendar import et_today
    from src.universe_daily import record_dir

    pipeline = build_pipeline(broker=MagicMock(), market=MagicMock(), sec_form4_provider=MagicMock())
    pipeline.config = SimpleNamespace(universe_screen=UniverseScreenConfig(enabled=True, data_dir=str(tmp_path)))
    pipeline.broker.is_trading_day.return_value = True
    pipeline.broker.get_session_close.return_value = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(FileNotFoundError):
        pipeline.admission._admit_screened_universe_symbols([])
    day = et_today().isoformat()
    folder = record_dir(tmp_path)
    folder.mkdir(parents=True)
    line = {"symbol": "AAA", "status": "passed", "measured": {}, "second_filter": {"k": 0.2, "rank": 1}}
    (folder / f"{day}.jsonl").write_text(json.dumps(line) + "\n")
    (folder / f"{day}.summary.json").write_text("{}")
    ranked, details = pipeline.admission._admit_screened_universe_symbols([])
    assert ranked == ["AAA"] and details["AAA"]["k"] == 0.2


def test_n_is_floor_of_budget_over_cost_and_the_budget_is_added_to_the_daily_cap():
    settings = yaml.safe_load((REPO / "config" / "settings.yaml").read_text())
    screen = settings["universe_screen"]
    assert screen["enabled"] is True
    cfg = UniverseScreenConfig(**screen)
    assert cfg.morning_top_n == math.floor(screen["morning_budget_usd"] / screen["morning_cost_per_name_usd"]) == 100
    daily = settings["llm_cost_circuit"]["daily_cost_limit_usd"]
    assert daily == pytest.approx(2.75 + screen["morning_budget_usd"])
