"""The intraday scan carries the forward macro-release and FOMC calendars onto its context.

Defect (measured): every intra_check risk prompt since 12 Oct said the calendar
was NOT FETCHED, because the scan never set the context's calendar fields.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.data.event_calendar import EventCalendarCoverage, FOMCCoverage
from src.data.event_calendar.macro_types import MacroEvent
from src.pipeline_context import RunContext
from tests.pipeline_factory import build_pipeline
from tests.test_intraday_scan import _intraday_pipeline, _snapshot, _ta_result

_EVENT = MacroEvent(release_id=10, label="CPI", why="inflation", event_date=date(2026, 10, 20), days_away=4)
_COV = EventCalendarCoverage(configured=1, succeeded=1)
_FOMC_COV = FOMCCoverage(status="measured", source="test")


class _Cal:
    def __init__(self, items, coverage, raises=False):
        self.items, self.last_coverage, self.raises = items, coverage, raises

    def get_upcoming_events(self, horizon_days):
        if self.raises:
            raise RuntimeError("boom")
        return self.items

    get_meetings = get_upcoming_events


def _scan(event_cal, fomc_cal):
    p = _intraday_pipeline(universe=["SPY", "AAPL"], atr={"AAPL": 2.0})
    p.event_calendar, p.fomc_calendar = event_cal, fomc_cal
    p.broker.get_intraday_snapshots.return_value = {"AAPL": _snapshot(last=110.0, prev=100.0)}
    p.tech_analyst.analyze_batch.return_value = (
        {"AAPL": _ta_result("AAPL")},
        MagicMock(
            user_message="m", raw_text="{}", tokens_used=1, input_tokens=1, output_tokens=1, cost_usd=0.0, model="t"
        ),
    )
    p.decision_stage.run.side_effect = lambda c: setattr(
        c, "portfolio_decision", SimpleNamespace(decisions=[SimpleNamespace(action="BUY", symbol="AAPL")])
    )
    p.risk_stage.run.return_value = None
    p.execution_stage.run.return_value = []
    ctx = RunContext.start("intra_check")
    with patch("src.pipeline_intraday.compute_indicators"):
        p._run_intraday_opportunity_scan(ctx)
    return ctx


def test_intraday_context_carries_calendars():
    ctx = _scan(_Cal([_EVENT], _COV), _Cal([], _FOMC_COV))
    assert ctx.macro_events == [_EVENT]
    assert ctx.macro_event_coverage is _COV
    assert ctx.fomc_coverage is _FOMC_COV


def test_raising_calendar_degrades_to_not_fetched():
    ctx = _scan(_Cal([], None, raises=True), _Cal([], None, raises=True))
    assert ctx.macro_events == [] and ctx.macro_event_coverage is None
    assert ctx.fomc_meetings == [] and ctx.fomc_coverage is None


def test_scan_body_is_wired_to_the_pipeline_calendars():
    p = build_pipeline()
    body = p._intraday_scan_body()
    assert body.event_calendar is p.event_calendar
    assert body.fomc_calendar is p.fomc_calendar
