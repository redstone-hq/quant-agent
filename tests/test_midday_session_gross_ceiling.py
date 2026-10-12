"""Midday/close margin headroom is resolved from the REAL pipeline's ladder.

Defect (seen 4 times since 2026-10-01): `PositionReviewSession` handed
`self` to `_entry_deployment_budget` / `_session_gross_ceiling`. The session
has no `_resolve_gross_ceiling`, so every midday run logged "gross-exposure
ceiling could not be resolved ... raw-cash clamp" and told the reviewer its
headroom was unknown. The fix passes the pipeline in at construction and
refuses, at BUILD time, a pipeline that cannot resolve the ladder.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.pipeline_parts.review import _run_position_review_body
from src.risk.gross_ladder import GrossCeiling
from src.sessions.position_review_session import PositionReviewSession

_EQUITY = 10_000.0
_HELD = 1_000.0


class _Reached(Exception):
    """Raised by the last stubbed exit step so the test stops right after it."""


def _ceiling() -> GrossCeiling:
    return GrossCeiling(
        ceiling_x=2.0,
        base_x=2.0,
        drawdown_pct=None,
        alert_owner=False,
        rung="base",
        reason="test",
    )


def _pipeline(resolve) -> SimpleNamespace:
    """The attributes the §11.2 helpers read off a real TradingPipeline."""
    return SimpleNamespace(
        _resolve_gross_ceiling=resolve,
        _sweep_symbol=lambda: None,
        config=SimpleNamespace(risk=SimpleNamespace(allow_margin=True)),
        broker=MagicMock(name="pipeline.broker"),
    )


def _session_kwargs(pipeline) -> dict:
    params = inspect.signature(PositionReviewSession).parameters
    kw = {name: MagicMock(name=name) for name in params}
    position = SimpleNamespace(
        symbol="AAA",
        qty=10,
        market_value=_HELD,
        side="long",
        avg_entry_price=100.0,
        current_price=100.0,
        unrealized_pl=0.0,
    )
    kw["broker"].get_account.return_value = {
        "cash": 5_000.0,
        "portfolio_value": _EQUITY,
        "last_equity": _EQUITY,
    }
    kw["broker"].get_positions.return_value = [position]
    kw["broker"].get_session_close.return_value = None
    kw["position_reviewer"].review.return_value = (None, MagicMock(name="md_result"))
    kw.update(
        is_trading_day=lambda: True,
        kill_switch_halt_result=lambda run_id, positions=0: None,
        total_pnl_since_reset=lambda tv: (0.0, 0.0, None),
        cost_circuit_status=lambda: {},
        run_news_update=lambda *a, **k: ([], None),
        load_earnings_analyses=lambda *a, **k: (None, []),
        sweeper=lambda: None,
        symbols_already_trimmed_today=lambda: set(),
        force_delever=lambda ctx: [],
        handle_ex_dividends=lambda *a, **k: [],
        apply_deterministic_trails=MagicMock(name="trails", return_value=[]),
        risk_review_exits=MagicMock(name="risk_exits", return_value=(set(), None)),
        midday_execute_llm_actions=MagicMock(name="reviewer_exits", side_effect=_Reached),
        pipeline=pipeline,
    )
    return kw


def _run_midday(pipeline) -> dict:
    kw = _session_kwargs(pipeline)
    with pytest.raises(_Reached):
        PositionReviewSession(**kw).run("midday")
    return kw


def test_midday_headroom_is_ladder_backed_with_a_real_resolver():
    kw = _run_midday(_pipeline(lambda ctx: _ceiling()))

    review_kwargs = kw["position_reviewer"].review.call_args.kwargs
    assert review_kwargs["margin_ladder_backed"] is True
    assert review_kwargs["margin_ladder_multiple"] == 2.0
    assert review_kwargs["margin_ladder_rung"] == "base"
    assert review_kwargs["margin_headroom_usd"] == pytest.approx(2.0 * _EQUITY - _HELD)


def test_pipeline_without_a_resolver_is_refused_at_construction():
    kw = _session_kwargs(SimpleNamespace(_sweep_symbol=lambda: None))
    with pytest.raises(TypeError, match="_resolve_gross_ceiling"):
        PositionReviewSession(**kw)


def test_builder_hands_the_session_the_pipeline_itself():
    """The production builder wires the pipeline object, so its resolver is
    what the session sees — and a pipeline without one fails the build."""

    class _Host:
        def _collab(self, name):
            if name == "_is_trading_day":
                return lambda: False
            return MagicMock(name=name)

        def _resolve_gross_ceiling(self, ctx):
            return _ceiling()

    assert _run_position_review_body(_Host(), "midday")["status"] == "market_holiday"

    class _HostWithoutResolver:
        def _collab(self, name):
            return MagicMock(name=name)

    with pytest.raises(TypeError, match="_resolve_gross_ceiling"):
        _run_position_review_body(_HostWithoutResolver(), "midday")


def test_the_real_pipeline_class_carries_the_resolver():
    from src.pipeline import TradingPipeline

    assert callable(getattr(TradingPipeline, "_resolve_gross_ceiling", None))


def test_resolver_raising_mid_run_still_runs_trails_and_exits():
    def _boom(ctx):
        raise RuntimeError("ladder read failed")

    kw = _run_midday(_pipeline(_boom))

    review_kwargs = kw["position_reviewer"].review.call_args.kwargs
    assert review_kwargs["margin_ladder_backed"] is False
    assert review_kwargs["margin_ladder_multiple"] is None
    kw["apply_deterministic_trails"].assert_called_once()
    kw["risk_review_exits"].assert_called_once()
    kw["midday_execute_llm_actions"].assert_called_once()
