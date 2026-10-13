"""Which sessions audit broker stop coverage -- a dependency-free declaration.

The "PROTECTION UNDETERMINED" page (src/trader_feed/naked.py) must fire only
when a session that was SUPPOSED to audit stops at the broker did not. This
module is the one place that fact is written down for runtime readers, and
it imports NOTHING: src/api reaches src/trader_feed, and the import guards
(tests/test_api_cannot_trade.py) forbid any route from there to the session
classes, which can trade.

Two declarations, both read here and by the page:
  * MODE_SESSION: every `--mode` / scheduler job name -> the session class
    name it runs, or None for a mode that is not a trading session at all
    (the scheduler loop, the P&L CSV export).
  * SESSION_AUDITS_BROKER_STOPS: session class name -> does it audit.

Each session class carries the same fact as `AUDITS_BROKER_STOPS`, beside
the code that writes `stop_coverage_gaps`. tests/test_protection_undetermined.py
enumerates every mode and fails when the class attribute, this table and the
audit code disagree in any direction, so none of the three can drift alone.
"""

from __future__ import annotations

STOP_COVERAGE_KEY = "stop_coverage_gaps"

MODE_SESSION: dict[str, str | None] = {
    "live": None,  # the scheduler loop itself; its jobs run the modes below
    "daily": None,  # src/pipeline_daily_export.py: a CSV, no broker stop audit
    "honour_stop": None,  # main._honour_stop_only: owner Stop pickup + cancel, never a session
    "once": "MorningSession",
    "morning": "MorningSession",
    "midday": "PositionReviewSession",
    "close": "PositionReviewSession",
    "evening": "EveningSession",
    "intra_check": "IntradaySession",
    "intra_safety": "IntradaySafety",
    "earnings_preprocess": "EarningsPreprocessSession",
    "meta": "QuarterlyMetaReflectionSession",
}

SESSION_AUDITS_BROKER_STOPS: dict[str, bool] = {
    "MorningSession": True,
    "PositionReviewSession": True,
    "EveningSession": True,
    "IntradaySession": True,
    "IntradaySafety": True,
    "EarningsPreprocessSession": False,
    "QuarterlyMetaReflectionSession": False,
}


def session_name_for_mode(mode: str | None) -> str | None:
    """The session class name a mode runs, or None for a non-session mode.

    Raises KeyError for a mode nobody has registered: an unknown mode must
    fail loudly in the enumeration test, not quietly decide the page.
    """
    return MODE_SESSION[mode]


def mode_audits_broker_stops(mode: str | None) -> bool:
    """True when the session behind `mode` is declared to audit stops at the broker.

    Fail closed: a mode that is None or unregistered is treated as auditing,
    so a caller that cannot name its mode gets the page rather than silence.
    """
    if mode is None:
        return True
    try:
        name = session_name_for_mode(mode)
    except KeyError:
        return True
    return name is not None and SESSION_AUDITS_BROKER_STOPS[name]
