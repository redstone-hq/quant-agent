"""Boundary witnesses: the three parts of the deterministic trail build and run
alone — no pipeline, no broker, no database, bars from a local stub.

`src/risk/trailing.py` keeps the contract (types, TRAIL_CODE_* names, every
ratified number); `trail_structure`, `trail_range_ratchet` and `trail_evaluate`
hold the bodies, moved verbatim. Clause 5 of tests/boundary_harness.py: each
part has a test that imports it and never names the pipeline. Follows
tests/test_entry_stop_boundary.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.risk import trail_evaluate, trail_range_ratchet, trail_structure, trailing
from tests.boundary_harness import check_boundary

PARTS = ["src.risk.trail_structure", "src.risk.trail_range_ratchet", "src.risk.trail_evaluate"]


@dataclass
class _Bar:
    high: float
    low: float
    close: float = 0.0


def _bars(pattern):
    # Closes mid-range: the swing detector's ATR(14) needs a close.
    return [_Bar(high=h, low=lo, close=(h + lo) / 2) for h, lo in pattern]


@pytest.mark.parametrize("module", PARTS)
def test_part_passes_boundary_check(module):
    verdict = check_boundary(module)
    assert verdict.passed, verdict.failures


def test_structure_leg_runs_from_a_stub_bar_alone():
    # 14 flat bars first for the detector's ATR(14) to warm up on (2026-10-15);
    # then a dip to 96, a rally, a pullback to 100 and a further rally. Each
    # bar trades a 2.00 range, so one ATR is ~2.
    rising = _bars([(101, 99)] * 14 + [(lo + 2, lo) for lo in (99, 98, 97, 96, 101, 102, 103, 100, 104, 105, 106)])
    lows = trail_structure._swing_lows(rising)
    assert lows == [96.0, 100.0]
    assert trail_structure._structural_pivot(lows, is_short=False) == 100.0
    assert trail_structure._structural_pivot([95.0, 92.0], is_short=False) is None
    # The fall from 105 to 100 is more than one ATR, so under the reversal
    # rule 105 is a confirmed swing high; the still-rising 108 is not.
    assert trail_structure._swing_highs(rising) == [105.0]


def test_range_ratchets_run_alone_and_never_loosen():
    kw = dict(symbol="aaa", ent=100.0, cur=112.0, stop=90.0, risk_per_share=10.0, is_short=False, setup_type="range")
    first = trail_range_ratchet._range_breakeven_ratchet(**kw)
    assert first.code == trailing.TRAIL_CODE_TRAILED
    assert first.proposal.new_stop == 100.0 and first.proposal.previous_stop == 90.0
    second = trail_range_ratchet._range_second_ratchet(**{**kw, "cur": 121.0})
    assert second.code == trailing.TRAIL_CODE_TRAILED and second.proposal.new_stop == 110.0
    # A stop already at the lock is left where it is: the part proposes no move back.
    held = trail_range_ratchet._range_second_ratchet(**{**kw, "cur": 121.0, "stop": 110.0})
    assert held.proposal is None and held.code == trailing.TRAIL_CODE_RANGE_SECOND_OFF_SIDE


def test_evaluator_runs_alone_and_the_facade_resolves_to_the_same_objects():
    ev = trail_evaluate.evaluate_trailing_stop(
        symbol="AAA",
        setup_type="breakout",
        entry=100.0,
        current_price=105.0,
        current_stop=None,
        reference_target=None,
    )
    assert ev.proposal is None and ev.code == trailing.TRAIL_CODE_NO_LIVE_STOP
    assert trailing.evaluate_trailing_stop is trail_evaluate.evaluate_trailing_stop
    assert trailing.compute_trailing_stop is trail_evaluate.compute_trailing_stop
    assert trailing._swing_lows is trail_structure._swing_lows
    assert trailing._range_second_ratchet is trail_range_ratchet._range_second_ratchet
    with pytest.raises(AttributeError):
        trailing.no_such_name  # noqa: B018


def test_facade_keeps_exactly_one_mirror_block():
    import ast

    tree = ast.parse(open(trailing.__file__, encoding="utf-8").read())
    getattrs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "__getattr__"]
    assert len(getattrs) == 1
    for name in trailing._PART_OF:
        assert not hasattr(trailing, "__dict__") or name not in vars(trailing)
