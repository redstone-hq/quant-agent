"""The made-up 4-day trail-tighten lock is gone, and a refused tighten is recorded.

Until 2026-10-15 the midday AI trailing-stop branch skipped any new stop when
a TRAIL_STOP had landed in the last 4 calendar days -- a number with no
measurement behind it, refused with a log line only. Now the only gate on
that branch's tighten is the stock's own 14-day-ATR noise band, and a
tighten it refuses is written as a durable per-symbol refusal row.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.models import Position, PositionAction, PositionReasoningChain, PositionReview
from src.risk.exit_refusal import CODE_NOISE_BAND
from tests.pipeline_factory import build_pipeline

ROOT = Path(__file__).resolve().parents[1]

GE = Position(
    symbol="GE",
    qty=26.0,
    avg_entry=316.0,
    current_price=360.0,
    market_value=9360.0,
    unrealized_pnl=1144.0,
    unrealized_intraday_pnl=0.0,
    sector="Industrials",
)


def _review(new_stop: float) -> PositionReview:
    rc = PositionReasoningChain(
        macro_continuity_check="stable",
        thesis_progress_check="on pace",
        thesis_integrity_check="intact",
        winners_discipline_check="no flags",
        session_disposition_check="patient",
        execution_rationale="n/a",
    )
    return PositionReview(
        reasoning_chain=rc,
        actions=[
            PositionAction(
                action="TRAIL_STOP",
                symbol="GE",
                reason="TARGET_BREACH — trail up",
                new_stop_price=new_stop,
            )
        ],
        overall_assessment="trail",
        risk_level="low",
    )


def _pipeline_tightened_yesterday():
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.broker.replace_stop_loss.return_value = {"id": "stop-1", "status": "accepted"}
    pipeline.broker.get_current_stop_price.return_value = 330.0
    pipeline.db.get_trades.return_value = [
        {
            "action": "TRAIL_STOP",
            "fill_status": "submitted",
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }
    ]
    pipeline._format_qty = lambda q: str(q)
    pipeline._atr_for_symbol = lambda s: 8.0  # noise floor = 360 - 1.25 * 8 = 350
    pipeline._record_exit_refusal = MagicMock(name="record_exit_refusal")
    return pipeline


def test_tighten_one_day_after_a_prior_tighten_is_applied_when_it_clears_the_atr_band():
    pipeline = _pipeline_tightened_yesterday()
    orders = pipeline._midday_execute_llm_actions(positions=[GE], run_id="r-1", review=_review(344.0))
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", 344.0)
    pipeline._record_exit_refusal.assert_not_called()


def test_clamp_refused_tighten_writes_a_durable_per_symbol_refusal_row():
    pipeline = _pipeline_tightened_yesterday()
    orders = pipeline._midday_execute_llm_actions(positions=[GE], run_id="r-1", review=_review(355.0))
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()
    pipeline._record_exit_refusal.assert_called_once()
    kw = pipeline._record_exit_refusal.call_args.kwargs
    assert kw["symbol"] == "GE"
    assert kw["code"] == CODE_NOISE_BAND
    assert kw["layer"] == "midday_trail_noise_band"
    assert kw["action"] == "TRAIL_STOP"
    assert "$355.00" in kw["detail"]


def test_no_trail_tightened_recently_remains_in_src():
    from src.exits.exit_records import ExitRecords
    from src.pipeline import TradingPipeline

    assert not hasattr(ExitRecords, "_trail_tightened_recently")
    assert not hasattr(TradingPipeline, "_trail_tightened_recently")
    # No definition or call anywhere in src (a historical prose mention is not code).
    hits = [str(p) for p in (ROOT / "src").rglob("*.py") if "_trail_tightened_recently(" in p.read_text()]
    assert hits == []
