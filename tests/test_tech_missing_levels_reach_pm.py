"""An actionable tech rating with a missing level reaches the PM (2026-10-12).

Incident 12 Oct 9:35 ET: the tech analyst rated XOM `buy` with no
`entry_price` (the breakout trigger, 163.50, was in the reasoning text only).
The validator raised, the item was dropped as schema-invalid, and XOM never
reached the Portfolio Manager. A missing level is now RECORDED in
`missing_levels`; a stop on the wrong side of a present entry is still a
contradiction and still rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.models import TargetPosition, TechAnalysisResult
from src.portfolio_constructor import PortfolioConstructor

# Recorded shape of the dropped 12 Oct XOM item: actionable, breakout, no entry.
XOM_ITEM = {
    "symbol": "XOM",
    "rating": "buy",
    "conviction": "medium",
    "entry_price": None,
    "stop_loss": None,
    "reference_target": None,
    "setup_type": "breakout",
    "expected_horizon_sessions": 15,
    "thesis_invalid_if": "closes back below 160",
    "support_levels": [158.0],
    "resistance_levels": [163.5],
    "reasoning": "Buy on a breakout above 163.50 on high volume.",
    "reasoning_chain": {
        "trend": "x",
        "momentum": "x",
        "volatility": "x",
        "volume": "x",
        "support_resistance": "x",
    },
}


def _xom(**overrides) -> TechAnalysisResult:
    return TechAnalysisResult.model_validate({**XOM_ITEM, **overrides})


def test_buy_with_null_entry_parses_and_records_missing_levels():
    analysis = _xom()
    assert analysis.rating == "buy"
    assert analysis.missing_levels == ["entry_price", "stop_loss"]


def test_missing_setup_horizon_and_falsifier_are_recorded_not_rejected():
    analysis = _xom(
        entry_price=163.5, stop_loss=158.0, setup_type=None, expected_horizon_sessions=None, thesis_invalid_if=""
    )
    assert analysis.missing_levels == ["setup_type", "expected_horizon_sessions", "thesis_invalid_if"]


def test_null_entry_reaches_pm_input_with_the_missing_levels_line():
    agent = PortfolioManagerAgent(api_key="test", model="test-model")
    msg = agent.build_user_message(analyses=[_xom()], positions=[], cash_balance=1000.0, total_value=1000.0)
    assert "- XOM: buy" in msg
    assert (
        "levels missing: entry_price, stop_loss — the desk will size from the live price "
        "and derive the stop from the bars"
    ) in msg


def test_buy_with_stop_above_entry_is_still_rejected():
    with pytest.raises(ValidationError, match="must be below entry"):
        _xom(entry_price=163.5, stop_loss=165.0)


def test_neutral_is_unchanged():
    analysis = _xom(rating="neutral", entry_price=163.5, stop_loss=158.0)
    assert analysis.entry_price is None and analysis.stop_loss is None and analysis.setup_type is None
    assert analysis.missing_levels == []


def _measured(atr: float) -> TechAnalysisResult:
    analysis = _xom()
    analysis.computed_levels = [158.2, 155.0]
    analysis.levels_coverage = "ok"
    analysis.atr_14 = atr
    analysis.signal_bar_low = 160.1
    analysis.signal_bar_high = 162.9
    analysis.bars_available = 250
    return analysis


def test_null_entry_and_stop_size_from_live_price_and_read_stop_from_bars():
    target = TargetPosition(symbol="XOM", direction="long", target_weight_pct=20.0, conviction="high", thesis="t")
    constructor = PortfolioConstructor()
    decisions = constructor.construct_orders(
        targets=[target], positions=[], analyses=[_measured(2.4)], total_value=100_000, price_map={"XOM": 163.0}
    )
    buys = [d for d in decisions if d.action == "BUY"]
    assert len(buys) == 1 and not constructor.last_data_faults
    assert buys[0].entry_price == 163.0  # the live price, not an analyst number
    assert 0 < buys[0].stop_loss < 160.1  # below the signal bar, read from the instrument
    # The stop moves with the instrument's own volatility: it is derived, not invented.
    _, wider = PortfolioConstructor()._resolve_entry_and_stop(target, _measured(4.8), 163.0)
    assert wider is not None and wider < buys[0].stop_loss
