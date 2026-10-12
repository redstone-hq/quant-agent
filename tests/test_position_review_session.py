"""Boundary witness: PositionReviewSession runs from stubs alone.

Every collaborator is an explicit constructor argument, so the session can be
built from stubs alone. `pipeline` is one of them (the §11.2 ladder resolver's
owner); a MagicMock carries a callable `_resolve_gross_ceiling`, so the
construction-time check passes. These tests exercise the two earliest return
paths.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from src.sessions.position_review_session import PositionReviewSession


def _build(**overrides) -> PositionReviewSession:
    params = inspect.signature(PositionReviewSession).parameters
    kwargs = {name: MagicMock(name=name) for name in params}
    kwargs.update(overrides)
    return PositionReviewSession(**kwargs)


def test_constructible_from_stubs_and_skips_on_holiday():
    session = _build(is_trading_day=lambda: False)
    result = session.run("midday")
    assert result["status"] == "market_holiday"
    assert result["positions"] == 0 and result["orders"] == []
    assert result["run_id"]


def test_kill_switch_halt_is_returned_as_is():
    halt = {"status": "halted", "reason": "kill_switch"}
    session = _build(
        is_trading_day=lambda: True,
        kill_switch_halt_result=lambda run_id, positions=0: halt,
    )
    assert session.run("close") is halt
