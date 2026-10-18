"""Scheduled economic releases never shape a buy or risk decision.

Owner ruling 2026-10-18: CPI, the jobs report, FOMC and the like must not
reach the risk seat or the macro seat, and a risk refusal citing one is not a
valid block ("Every stock earns its right to be there"; stops cover event
risk). Measured: prices swing ~11% more only on release day; two or more days
before is normal (258 stocks, 86 releases). Company earnings dates STAY,
information only (owner ruling k60).
"""

from __future__ import annotations

from datetime import timedelta

from src.agents.macro_analyst import MacroAnalystAgent
from src.agents.risk_manager import RiskManagerAgent
from src.data.event_calendar import EventCalendarCoverage, FOMCCoverage, FOMCMeeting
from src.data.event_calendar.macro_types import MacroEvent
from src.pipeline_context import RunContext
from src.pipeline_stages import RiskStage
from src.trading_calendar import et_today
from tests.test_event_risk_calendar import _decision, _FakeMarket, _StubPipeline
from tests.test_risk_rejection_reasons import _run

_WHEN = et_today() + timedelta(days=2)
_CPI = MacroEvent(release_id=10, label="CPI", why="zzz-scheduled-print", event_date=_WHEN, days_away=2)
_FOMC = FOMCMeeting(start_date=_WHEN, end_date=_WHEN + timedelta(days=1))
_FOMC_COV = FOMCCoverage(status="measured", source="test")
_MACRO_MARKERS = (
    "Scheduled Macro Releases",
    "Scheduled US macro releases",
    "FOMC meetings",
    "zzz-scheduled-print",
    _WHEN.isoformat(),
)


def _ctx_with_calendar(symbols=("ZZZA",)) -> RunContext:
    """A run whose calendar DID return a release and a meeting."""
    ctx = RunContext(run_id="r1", session="morning")
    ctx.portfolio_decision = _decision(symbols)
    ctx.macro_events = [_CPI]
    ctx.macro_event_coverage = EventCalendarCoverage(configured=7, succeeded=7, failed=[])
    ctx.fomc_meetings = [_FOMC]
    ctx.fomc_coverage = _FOMC_COV
    return ctx


def _assert_no_macro(text: str) -> None:
    for marker in _MACRO_MARKERS:
        assert marker not in text, f"scheduled-release text {marker!r} reached a seat"


def test_risk_ai_input_has_no_macro_release_even_when_the_calendar_has_events():
    block = RiskStage._build_event_risk_block(_StubPipeline(_FakeMarket({"ZZZA": 3})), _ctx_with_calendar())
    message = RiskManagerAgent.__new__(RiskManagerAgent).build_user_message(
        portfolio_decision=_decision(("ZZZA",)),
        positions=[],
        macro_summary={},
        rule_violations=[],
        event_risk_block=block,
    )
    _assert_no_macro(block)
    _assert_no_macro(message)


def test_macro_seat_input_has_no_macro_release_even_when_events_are_passed():
    message = MacroAnalystAgent.__new__(MacroAnalystAgent).build_user_message(
        macro_summary={},
        universe=["SPY"],
        macro_events=[_CPI],
        event_coverage=EventCalendarCoverage(configured=7, succeeded=7, failed=[]),
        fomc_meetings=[_FOMC],
        fomc_coverage=_FOMC_COV,
    )
    _assert_no_macro(message)


def test_earnings_dates_still_render_for_the_risk_ai():
    block = RiskStage._build_event_risk_block(
        _StubPipeline(_FakeMarket({"ZZZA": 3, "ZZZB": None})), _ctx_with_calendar(("ZZZA", "ZZZB"))
    )
    assert "ZZZA: next earnings ~3 sessions away (fetched)" in block
    assert "Earnings proximity is UNKNOWN for: ZZZB" in block


def test_a_refusal_citing_a_fetched_macro_release_is_not_event_backed():
    survivors, events = _run(
        [
            {
                "symbol": "ZZZ",
                "reason": "rate-sensitive name into the CPI print",
                "category": "event_risk",
                "event": f"{_WHEN.isoformat()} CPI",
            }
        ],
        macro_events=[_CPI],
    )
    assert survivors == ["ZZZ", "YYY"]
    assert any(e[0] == "ZZZ" and e[2] == "event_refusal_ignored" for e in events)


def test_a_refusal_for_other_evidence_still_blocks():
    survivors, _ = _run(
        [{"symbol": "ZZZ", "reason": "stop sits under no defended level", "category": "other"}],
        category="other",
    )
    assert survivors == ["YYY"]
