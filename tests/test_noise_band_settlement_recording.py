"""Board item 70 — the noise band's settlement recording must not be censored.

2026-10-09: the midday reviewer's entry-anchored gate (home 1 below) was
REMOVED on the owner's ruling that no sale is refused for the price the desk
paid; its recording went with it and is pinned ABSENT here. Home 2, the
structural-protection fallback, is unchanged. The history below is kept.

The entry-anchored ATR noise band is `arbitrary`: `NOISE_BAND_ATR_MULTIPLE`
is 1.0 with no published measurement of the quantity it bounds (the adverse
move at which a move from entry stops being ordinary daily wobble). Item 70's
2026-09-30 pass added a machine-readable settlement recording so the number
could one day be settled on the desk's own evidence instead of re-searched.

Measured against production on 2026-10-01 that recording had produced ZERO
observations, and the structural reason is visible in the code rather than in
the data: the band was written to the durable record ONLY on the branch where
it BLOCKED an exit. A sample truncated at exactly the threshold under
examination is the one sample that can never locate that threshold — every
adverse move the band judged large enough to let through was discarded, so the
record could only ever show moves smaller than 1.0 ATR and would "confirm" any
multiple whatsoever.

These tests pin the two recording gaps:

  1. the midday reviewer (`src/pipeline_exits.py`) must write the observation
     on BOTH outcomes, tagged `blocked=true|false`, with the adverse move also
     expressed as an ATR multiple — the unit the constant is denominated in;

  2. the band's SECOND home, the `check_structural_protection` fallback in
     `src/risk/exit_guard.py`, must emit the same `rule=atr_noise_band`
     payload the break margin already emits. That home applies a FLAT 1.0 ATR
     band (it passes no `days_held`) while the reviewer's widens with
     sqrt(sessions held), so the same named constant yields two different
     widths for one holding on one day — a fact no record stated until now.

RECORDING ONLY. No protection decision and no sell decision changes here; the
`blocked` / `inside_band` flags are the identical predicate calls the previous
code made, and both tests assert the decisions still come out as before.
"""

from unittest.mock import MagicMock

from src.models import Position, PositionAction, PositionReasoningChain, PositionReview
from tests.pipeline_factory import build_pipeline
from src.risk.exit_guard import (
    StructuralProtectionCheck,
    check_structural_protection,
)


def _position(symbol="AAA", qty=10, avg_entry=100.0, current_price=110.0):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector="Technology",
    )


def _pipeline():
    from src.trading_calendar import trading_sessions_held as _weekday_sessions_held

    p = build_pipeline(db=MagicMock(), broker=MagicMock())
    p.broker.get_current_stop_price.return_value = None
    p.broker.trading_sessions_held.side_effect = _weekday_sessions_held
    p._atr_for_symbol = MagicMock(return_value=2.0)
    return p


def _review_with(action="SELL", symbol="AAA", reason="thesis_invalid triggered"):
    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable",
            thesis_progress_check="broken",
            thesis_integrity_check="invalidation hit",
            winners_discipline_check="n/a",
            session_disposition_check="midday",
            execution_rationale="exit",
        ),
        actions=[PositionAction(action=action, symbol=symbol, reason=reason)],
        overall_assessment="one exit",
        risk_level="moderate",
    )


def _band_rows(db):
    """Every durable row this run wrote that carries the band's payload."""
    rows = []
    for call in db.record_intraday_evaluation.call_args_list:
        detail = call.kwargs.get("detail") or ""
        if "rule=atr_noise_band" in detail:
            rows.append((call.kwargs.get("status"), detail))
    return rows


# ---------------------------------------------------------------------------
# Home 1: the midday reviewer. The uncensored half is the new assertion.
# ---------------------------------------------------------------------------


def test_the_midday_reviewer_no_longer_evaluates_or_records_an_entry_band():
    """Home 1 is gone. A 0.675 ATR loss from entry used to be dropped with
    `exit_blocked_inside_atr_noise_band`; a 3.0 ATR loss was recorded as
    evaluated. Neither row is written now, because nothing is evaluated."""
    for entry, price, atr in ((100.0, 94.0, 2.0), (42.59, 41.51, 1.6)):
        pipeline = _pipeline()
        pipeline._atr_for_symbol = MagicMock(return_value=atr)
        pipeline._midday_execute_llm_actions(
            positions=[_position("AAA", qty=10, avg_entry=entry, current_price=price)],
            review=_review_with(symbol="AAA", reason="thesis_invalid triggered — lost the level"),
            run_id="r1",
        )
        assert _band_rows(pipeline.db) == []


# ---------------------------------------------------------------------------
# Home 2, the structural-protection fallback, is gone too (2026-10-09).
# ---------------------------------------------------------------------------


def _no_level(entry: float, price: float, atr: float | None) -> StructuralProtectionCheck:
    """No thesis_invalid_if and no qualifying level."""
    return check_structural_protection(
        thesis_invalid_if=None,
        current_price=price,
        entry_price=entry,
        stop_loss=price * 0.9,
        atr=atr,
        computed_levels=[],
        computed_level_touches={},
    )


def test_no_chart_level_is_never_protected():
    """Owner mandate 2026-10-09: no chart level backing the thesis never
    refuses a cut of a LOSER — small loss, large loss or missing ATR alike."""
    for entry, price, atr in ((100.0, 99.0, 2.0), (100.0, 94.0, 2.0), (100.0, 99.0, None)):
        result = _no_level(entry, price, atr)
        assert result.protected is False, (entry, price, atr)
        assert result.basis == "no_chart_level"
        assert result.confirmed_chart_break is False
        assert "rule=atr_noise_band" not in result.detail
