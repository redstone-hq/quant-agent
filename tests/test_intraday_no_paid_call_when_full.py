"""Owner rulings 2026-10-12: one paid batch per intraday check, and no paid
portfolio-manager call when the account has no room to open any new position.

Measured that day: 17 of 19 paid intraday PM calls answered "no room" at the
2.0x gross ceiling. Room is the SAME headroom execution sizes against and the
owner's minimum risk floor at each mover's own stop; nothing new is tuned.
The below-entry-bar sale (its own PM-free step) still runs on a skipped check.
"""

from __future__ import annotations

import json

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.intraday.buying_room import NO_BUYING_ROOM
from src.models import Position
from src.pipeline_context import RunContext
from src.risk.rules import GrossCeiling
from tests.test_intraday_scan import _held_position, _intraday_pipeline, _snapshot, _ta_result

_EQUITY = 10_000.0


def _ta_log():
    return MagicMock(
        user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
    )


def _ceiling(pipeline, x: float = 2.0):
    ceiling = MagicMock(spec=GrossCeiling)
    ceiling.ceiling_x = x
    ceiling.rung = 0
    pipeline._resolve_gross_ceiling = MagicMock(return_value=ceiling)


def _book(gross_usd: float) -> list[Position]:
    """OLD (a $1,002 hold that can fall below the entry bar) plus BIG, to `gross_usd` in all."""
    old = _held_position("OLD")
    big = gross_usd - old.market_value
    return [
        old,
        Position(
            symbol="BIG",
            qty=1.0,
            avg_entry=big,
            current_price=big,
            market_value=big,
            unrealized_pnl=0.0,
            sector="Technology",
        ),
    ]


def _check(movers: int, held_gross: float, *, below_bar_sale: bool, preview=(100.0, 95.0, 2.0)):
    """One intraday check with `movers` names moving; returns (pipeline, ctx, result, seen).

    `preview` is the constructor's (entry, stop, ratio) for every mover --
    a 5% stop, so the 0.5% floor needs a $1,000 position on $10,000 equity.
    """
    names = [f"M{i:02d}" for i in range(movers)]
    p = _intraday_pipeline(universe=names + ["OLD", "BIG"], atr=2.0)
    p.config.risk.allow_margin = True
    _ceiling(p)
    p.portfolio_constructor.entry_stop_preview = MagicMock(return_value=preview)
    p.broker.get_intraday_snapshots.return_value = {
        s: _snapshot(last=100.0 + 1.0 + i * 0.1, prev=100.0) for i, s in enumerate(names)
    }
    p.db.get_recent_intraday_evaluations.return_value = []
    p._recently_intraday_evaluated = MagicMock(return_value=False)
    p.tech_analyst.analyze_batch.side_effect = lambda data, **kw: (
        {d["symbol"]: _ta_result(d["symbol"]) for d in data},
        _ta_log(),
    )
    seen: dict = {"pm_calls": 0}

    def _decide(ctx):
        # Stand-in for DecisionStage, in its real order: the PM-free
        # below-entry-bar sale is known first, the room is judged on the book
        # after it, the paid PM call happens only when no skip reason is set,
        # and the sale runs either way.
        room_check = getattr(ctx, "pm_room_check", None)
        if not getattr(ctx, "pm_skip_reason", None) and callable(room_check):
            skip = room_check(ctx, ("OLD",) if below_bar_sale else ())
            if skip:
                ctx.pm_room_skip = skip
                ctx.pm_skip_reason = skip["pm_call_skipped"]
        seen["pm_skip_reason"] = getattr(ctx, "pm_skip_reason", None)
        if not seen["pm_skip_reason"]:
            seen["pm_calls"] += 1
        decisions = [SimpleNamespace(action="SELL", symbol="OLD")] if below_bar_sale else []
        ctx.portfolio_decision = SimpleNamespace(decisions=decisions) if decisions else None

    p.decision_stage.run.side_effect = _decide
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = [{"id": "o1", "action": "SELL", "symbol": "OLD"}] if below_bar_sale else []
    ctx = RunContext.start("intra_check")
    ctx.positions = _book(held_gross)
    ctx.total_value = _EQUITY
    ctx.cash = 0.0
    ctx.deployable_cash = 0.0
    events: list[dict] = []
    with (
        patch("src.pipeline_intraday.compute_indicators", return_value=MagicMock()),
        patch("src.pipeline_candidate_records._persist_evidence", side_effect=lambda db, **kw: events.append(kw)),
    ):
        result = p._run_intraday_opportunity_scan(ctx)
    seen["events"] = [
        {"symbol": e.get("symbol"), **json.loads(e["evidence_json"])}
        for e in events
        if e.get("kind") == "pipeline_event"
    ]
    return p, ctx, result, seen


def test_full_book_makes_no_pm_call_records_why_and_still_sells_below_bar():
    # Held gross above 2.0x equity: even after selling OLD ($1,002) below the
    # entry bar, $502 is left -- under the $1,000 a 0.5% risk at a 5% stop needs.
    p, ctx, result, seen = _check(3, 2 * _EQUITY + 500.0, below_bar_sale=True)
    assert seen["pm_calls"] == 0
    assert seen["pm_skip_reason"] == NO_BUYING_ROOM
    # The below-entry-bar sale still went through risk and execution.
    p.risk_stage.run.assert_called_once_with(ctx)
    assert result["status"] == "intraday_executed"
    assert result["orders"][0]["symbol"] == "OLD"
    # Durable reason: which limit, the headroom, the candidates considered.
    assert result["pm_call_skipped"] == NO_BUYING_ROOM
    assert result["headroom_usd"] == 502.0
    assert "2.00x" in result["limit"] and "after selling OLD" in result["limit"]
    assert [c["symbol"] for c in result["candidates_considered"]] == ["M02", "M01", "M00"]


def test_full_book_with_nothing_to_sell_reports_a_skip_not_a_pm_failure():
    p, _ctx, result, seen = _check(3, 2 * _EQUITY, below_bar_sale=False)
    assert seen["pm_calls"] == 0
    assert result["status"] == NO_BUYING_ROOM
    assert result["status"] != "intraday_analysis_error"
    assert result["pm_call_skipped"] == NO_BUYING_ROOM
    p.execution_stage.run.assert_not_called()


def test_headroom_below_the_smallest_floor_sized_position_skips_the_call():
    # $1 of headroom cannot hold a 0.5%-of-equity risk at a 5% stop ($1,000).
    _p, _ctx, result, seen = _check(2, 2 * _EQUITY - 1.0, below_bar_sale=False)
    assert seen["pm_calls"] == 0
    assert result["headroom_usd"] == 1.0
    assert all(c["min_notional_usd"] > 1.0 for c in result["candidates_considered"])


def test_room_available_makes_exactly_one_pm_call_even_with_twenty_movers():
    p, _ctx, result, seen = _check(20, 0.5 * _EQUITY, below_bar_sale=False)
    assert seen["pm_calls"] == 1
    assert p.decision_stage.run.call_count == 1
    assert p.tech_analyst.analyze_batch.call_count == 1
    sent = [d["symbol"] for d in p.tech_analyst.analyze_batch.call_args.args[0]]
    assert sent[:5] == ["M19", "M18", "M17", "M16", "M15"]
    assert "pm_call_skipped" not in result


def test_a_mover_with_no_computable_stop_cannot_make_the_call_worthwhile():
    _p, _ctx, result, seen = _check(2, 0.5 * _EQUITY, below_bar_sale=False, preview=None)
    assert seen["pm_calls"] == 0
    assert result["headroom_usd"] > 0
    assert [c["min_notional_usd"] for c in result["candidates_considered"]] == [None, None]


def test_the_real_decision_stage_sells_below_bar_without_the_pm_on_a_no_room_skip():
    """The skip reason this change sets reaches the real DecisionStage: no
    PM call, recorded as a deliberate skip, and the below-entry-bar sale is
    still proposed (PR 1690's PM-free step)."""
    from tests.test_earn_its_place_without_pm import _below_bar_precheck, _pipeline, _run_recorded

    pipeline = _pipeline(_below_bar_precheck())
    ctx, events = _run_recorded(pipeline, pm_skip_reason=NO_BUYING_ROOM)
    assert pipeline.portfolio_manager.decide.call_count == 0
    assert [(e[2], e[3]) for e in events if e[1] == "portfolio_manager"] == [("skipped", NO_BUYING_ROOM)]
    assert ctx.rotation is not None and ctx.rotation["held_symbol"] == "OLD"


_SKIP_RECORD = {
    "status": NO_BUYING_ROOM,
    "pm_call_skipped": NO_BUYING_ROOM,
    "headroom_usd": 0.0,
    "gross_ceiling_x": 2.0,
    "candidates_considered": [{"symbol": "M00", "direction": "long", "min_notional_usd": 1000.0}],
    "run_id": "intra_check-full",
}


def test_the_skip_sentence_says_why_in_plain_words():
    from src.intraday.buying_room import no_room_sentence

    assert no_room_sentence(_SKIP_RECORD) == (
        "No paid buy check: no room to open even a minimum-size trade "
        "(headroom $0 under the 2x limit; the smallest needed $1,000)."
    )


def test_the_hourly_telegram_summary_carries_the_skip_sentence(tmp_path, monkeypatch):
    from src import trader_feed
    from tests.test_trader_feed import _TOP_OF_HOUR_TIME, _make_db, _pin_clock

    _make_db(tmp_path, monkeypatch)
    _pin_clock(monkeypatch, _TOP_OF_HOUR_TIME)
    outer = {"status": "ok", "run_id": "intra_check-full", "daily_pnl": 1.0, "intraday_scan": dict(_SKIP_RECORD)}
    msg = trader_feed.format_session_result("intra_check", outer, 4.0)
    assert msg is not None
    assert "No paid buy check: no room to open even a minimum-size trade (headroom $0 under the 2x limit" in msg
    assert "FAILED" not in msg


def test_a_below_bar_sale_that_frees_room_lets_the_pm_fund_a_buy_in_the_same_check():
    """Adversary 2026-10-12: room is judged AFTER the PM-free sale. At exactly
    2.0x, selling OLD frees $1,002 -- enough for the $1,000 minimum trade."""
    _p, _ctx, result, seen = _check(3, 2 * _EQUITY, below_bar_sale=True)
    assert seen["pm_calls"] == 1
    assert "pm_call_skipped" not in result


def test_a_skipped_check_does_not_start_the_movers_cooldown():
    p, _ctx, _result, seen = _check(3, 2 * _EQUITY, below_bar_sale=False)
    assert seen["pm_calls"] == 0
    p.db.record_intraday_evaluation.assert_not_called()


def test_a_check_the_pm_saw_starts_the_cooldown_for_exactly_the_sent_movers():
    p, _ctx, _result, seen = _check(8, 0.5 * _EQUITY, below_bar_sale=False)
    assert seen["pm_calls"] == 1
    cooled = [c.kwargs["symbol"] for c in p.db.record_intraday_evaluation.call_args_list]
    assert cooled == ["M07", "M06", "M05", "M04", "M03"]


def test_every_mover_gets_its_own_durable_row_on_a_skipped_check():
    _p, _ctx, _result, seen = _check(3, 2 * _EQUITY, below_bar_sale=False)
    rows = {e["symbol"]: e for e in seen["events"] if e.get("reason") == NO_BUYING_ROOM}
    assert sorted(rows) == ["M00", "M01", "M02"]
    assert all(r["headroom_usd"] == 0.0 and r["min_notional_usd"] == 1000.0 for r in rows.values())


def test_the_real_decision_stage_judges_room_after_the_below_bar_sale():
    """The real DecisionStage hands the room check the holding its PM-free
    sale will sell, before deciding whether to call the PM."""
    import tests.test_earn_its_place_without_pm as earn
    from tests.test_earn_its_place_without_pm import _below_bar_precheck, _pipeline, _run_recorded

    pipeline = _pipeline(_below_bar_precheck())
    seen_sold = []

    def _room_check(_ctx, sold):
        seen_sold.append(tuple(sold))
        return {"pm_call_skipped": NO_BUYING_ROOM}

    real_start = earn.RunContext.start

    def _start(*a, **kw):
        ctx = real_start(*a, **kw)
        ctx.pm_room_check = _room_check
        return ctx

    with patch.object(earn.RunContext, "start", side_effect=_start):
        ctx, _events = _run_recorded(pipeline)
    assert seen_sold == [("OLD",)]
    assert pipeline.portfolio_manager.decide.call_count == 0
    assert ctx.rotation is not None and ctx.rotation["held_symbol"] == "OLD"
