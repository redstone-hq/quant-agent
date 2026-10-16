"""Each risk refusal carries its own category; an event_risk block stands only
on a FETCHED macro release / FOMC meeting.

Owner rulings: the risk seat keeps its power to block a buy it judges risky
(2026-10-16), and earnings never resize or block a trade. Measured breach
2026-10-15 17:17 UTC: a buy was put in `rejected_symbols` citing an earnings
date ~3 sessions away under `event_risk`, and the code applied it, because a
`{symbol, reason}` entry gave code no way to tell what caused the block.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from src.data.event_calendar.macro_types import MacroEvent
from src.models import RiskVerdict, TradeDecision
from src.pipeline_stages import RiskStage
from src.trading_calendar import et_today
from tests.test_risk_verdict_per_symbol import _ctx, _events, _rc, _stage_pipeline


def _buy(symbol: str) -> TradeDecision:
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=5.0,
        entry_price=50.0,
        stop_loss=48.0,
        take_profit=56.0,
        reasoning="test setup",
        thesis_invalid_if="closes below support",
    )


def _verdict(rejections, category="event_risk") -> RiskVerdict:
    return RiskVerdict(
        approved=True,
        reasoning_chain=_rc(),
        reason_category=category,
        rejected_symbols=rejections,
        reasoning="test",
    )


def _run(rejections, *, category="event_risk", macro_events=()):
    decisions = [_buy("ZZZ"), _buy("YYY")]
    pipeline = _stage_pipeline(verdict=_verdict(rejections, category), decisions=decisions)
    ctx = _ctx(decisions)
    ctx.macro_events = list(macro_events)
    result = RiskStage(pipeline=pipeline).run(ctx)
    assert result is None
    return [d.symbol for d in ctx.portfolio_decision.decisions], _events(pipeline)


def test_earnings_only_event_risk_block_is_refused_and_the_buy_proceeds():
    in3 = (et_today() + timedelta(days=3)).isoformat()
    survivors, events = _run(
        [
            {
                "symbol": "ZZZ",
                "reason": f"earnings {in3}, ~3 sessions away",
                "category": "event_risk",
                "event": f"ZZZ earnings {in3}",
            }
        ]
    )
    assert survivors == ["ZZZ", "YYY"]
    assert any(e[0] == "ZZZ" and e[2] == "event_refusal_ignored" for e in events)
    assert not any(e[0] == "ZZZ" and e[2] == "rejected" for e in events)


def test_event_risk_block_naming_a_fetched_macro_release_is_applied():
    when = et_today() + timedelta(days=2)
    cpi = MacroEvent(release_id=10, label="CPI", why="inflation print", event_date=when, days_away=2)
    survivors, events = _run(
        [
            {
                "symbol": "ZZZ",
                "reason": "rate-sensitive name into the CPI print",
                "category": "event_risk",
                "event": f"{when.isoformat()} CPI",
            }
        ],
        macro_events=[cpi],
    )
    assert survivors == ["YYY"]
    assert any(e[0] == "ZZZ" and e[2] == "rejected" for e in events)


def test_concentration_block_is_applied():
    survivors, _ = _run(
        [{"symbol": "YYY", "reason": "sector already 30% of book", "category": "concentration"}],
        category="concentration",
    )
    assert survivors == ["ZZZ"]


def test_an_entry_without_category_fails_validation():
    with pytest.raises(ValidationError):
        _verdict([{"symbol": "ZZZ", "reason": "too risky"}])


def test_an_event_risk_entry_without_event_fails_validation():
    with pytest.raises(ValidationError):
        _verdict([{"symbol": "ZZZ", "reason": "macro", "category": "event_risk"}])
