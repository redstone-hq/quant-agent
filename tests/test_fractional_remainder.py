"""Owner ruling 2026-10-15: whole shares only — a sale never leaves a NEW fraction.

A held position is sold whole or not at all: a PARTIAL_SELL decision is
refused, and a full SELL of a position carrying a sub-share remainder sells
the whole quantity, remainder included. The drawdown ladder cuts whole
positions too (tests/test_drawdown_cuts_whole.py). So with entries floored to
whole shares, no sale can mint a fraction.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.stage_execution_parts.sell_loop import sell_qty_and_label
from src.stage_execution_parts.state import SKIP


def _pipeline():
    pipeline = MagicMock()
    pipeline._full_sell_qty = lambda q: float(q) if q > 0 else None
    return pipeline


@pytest.mark.parametrize("held", [36.0, 36.688])
@pytest.mark.parametrize("pct", [10.0, 50.0, 99.0])
def test_a_trim_is_refused_so_it_can_never_leave_a_new_fraction(held, pct):
    decision = SimpleNamespace(symbol="XOM", allocation_pct=pct)
    existing = [SimpleNamespace(qty=held)]

    assert sell_qty_and_label(_pipeline(), decision, existing, MagicMock()) is SKIP


def test_a_full_sell_takes_the_sub_share_remainder_with_it():
    decision = SimpleNamespace(symbol="XOM", allocation_pct=100.0)
    existing = [SimpleNamespace(qty=36.688)]

    qty, label = sell_qty_and_label(_pipeline(), decision, existing, MagicMock())

    assert label == "SELL"
    assert qty == pytest.approx(36.688)
