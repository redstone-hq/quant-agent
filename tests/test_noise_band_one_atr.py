"""The trailing-stop noise band is one ATR, by owner ruling (Rex, 2026-10-09).

"A stop is never closer than one normal day's move", and the normal day's
move is the stock's own 14-day Wilder ATR. The old 1.25 had no recorded
derivation for its extra 0.25. The multiple is defined once
(`src.risk.trailing.NOISE_BAND_ATR_MULTIPLE`); the midday TRAIL_STOP clamp and
the position reviewer's prompt both read it. The two midday skips that used to
reach the log only now write a durable per-symbol refusal row.
"""

from unittest.mock import MagicMock

import pytest

from src.models import Position, PositionAction, PositionReasoningChain, PositionReview
from src.risk import trailing
from src.risk.exit_refusal import (
    CODE_NOISE_BAND,
    CODE_TRAIL_BELOW_MIN_RATCHET,
    CODE_TRAIL_STOP_AT_OR_ABOVE_PRICE,
)
from tests.pipeline_factory import build_pipeline

PRICE = 360.0
ATR = 8.0
OLD_STOP = 330.0

GE = Position(
    symbol="GE",
    qty=26.0,
    avg_entry=316.0,
    current_price=PRICE,
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


def _pipeline():
    pipeline = build_pipeline(broker=MagicMock(), db=MagicMock())
    pipeline.broker.replace_stop_loss.return_value = {"id": "stop-1", "status": "accepted"}
    pipeline.broker.get_current_stop_price.return_value = OLD_STOP
    pipeline.db.get_trades.return_value = []
    pipeline._format_qty = lambda q: str(q)
    pipeline._atr_for_symbol = lambda s: ATR
    pipeline._record_exit_refusal = MagicMock(name="record_exit_refusal")
    return pipeline


def _run(new_stop: float):
    pipeline = _pipeline()
    orders = pipeline._midday_execute_llm_actions(positions=[GE], run_id="r-nb", review=_review(new_stop))
    return pipeline, orders


def test_the_multiple_is_one_atr():
    assert trailing.NOISE_BAND_ATR_MULTIPLE == 1.0


def test_tighten_to_1_1_atr_below_price_is_now_allowed():
    new_stop = round(PRICE - 1.1 * ATR, 2)  # 351.20; the old 1.25 floor was 350
    pipeline, orders = _run(new_stop)
    assert len(orders) == 1
    pipeline.broker.replace_stop_loss.assert_called_once_with("GE", new_stop)
    pipeline._record_exit_refusal.assert_not_called()


def test_tighten_to_0_9_atr_below_price_is_refused_with_a_durable_row():
    new_stop = round(PRICE - 0.9 * ATR, 2)  # 352.80, inside the 352 floor
    pipeline, orders = _run(new_stop)
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()
    pipeline._record_exit_refusal.assert_called_once()
    kw = pipeline._record_exit_refusal.call_args.kwargs
    assert kw["symbol"] == "GE"
    assert kw["code"] == CODE_NOISE_BAND
    assert kw["dropped"] is True
    assert "floor $352.00" in kw["detail"]


def test_midday_clamp_reads_the_constant_not_a_literal(monkeypatch):
    monkeypatch.setattr(trailing, "NOISE_BAND_ATR_MULTIPLE", 0.5)
    # 0.9 ATR below price clears a 0.5-ATR band, so it is applied.
    pipeline, orders = _run(round(PRICE - 0.9 * ATR, 2))
    assert len(orders) == 1
    pipeline._record_exit_refusal.assert_not_called()


def test_stop_at_or_above_price_writes_a_refusal_row():
    pipeline, orders = _run(PRICE + 1.0)
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()
    kw = pipeline._record_exit_refusal.call_args.kwargs
    assert kw["symbol"] == "GE"
    assert kw["code"] == CODE_TRAIL_STOP_AT_OR_ABOVE_PRICE
    assert kw["dropped"] is True


def test_min_ratchet_floor_skip_writes_a_refusal_row():
    pipeline, orders = _run(OLD_STOP)  # does not clear the live stop by a tick
    assert orders == []
    pipeline.broker.replace_stop_loss.assert_not_called()
    kw = pipeline._record_exit_refusal.call_args.kwargs
    assert kw["symbol"] == "GE"
    assert kw["code"] == CODE_TRAIL_BELOW_MIN_RATCHET
    assert kw["dropped"] is True


@pytest.mark.parametrize("value, shown", [(1.0, "1×ATR14"), (1.7, "1.7×ATR14")])
def test_ai_prompt_shows_the_constants_value(monkeypatch, value, shown):
    from src.agents.position_reviewer import PositionReviewerAgent

    monkeypatch.setattr(trailing, "NOISE_BAND_ATR_MULTIPLE", value)
    text = object.__new__(PositionReviewerAgent).system_prompt
    assert shown in text
    assert "{{" not in text
    assert "1.25" not in text
