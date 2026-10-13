"""Which sessions audit broker stop coverage -- declared ON the session, read by the page.

The "PROTECTION UNDETERMINED" page (src/trader_feed/naked.py) must fire only
when a session that was SUPPOSED to audit stops at the broker did not. The
knowledge "this session audits stops" lives here as a class attribute on the
session class itself (`AUDITS_BROKER_STOPS`), next to the code that writes
`stop_coverage_gaps`, so a session cannot be added or changed without the
declaration sitting beside the audit. tests/test_protection_undetermined.py
enumerates every run mode and fails when a declaration and the audit
disagree in either direction.

A mode is resolved to its session class here; `None` means the mode is not
a trading session at all (the scheduler loop, the P&L CSV export) and can
never be asked whether anything is unprotected.
"""

from __future__ import annotations

STOP_COVERAGE_KEY = "stop_coverage_gaps"


def session_class_for_mode(mode: str | None):
    """The session class a `--mode` / scheduler job name runs, or None for a non-session mode.

    Raises KeyError for a mode nobody has registered: an unknown mode must
    fail loudly in the enumeration test, not quietly decide the page.
    """
    from src.intraday.safety import IntradaySafety
    from src.intraday.session import IntradaySession
    from src.sessions.earnings_preprocess_session import EarningsPreprocessSession
    from src.sessions.evening_session import EveningSession
    from src.sessions.morning_session import MorningSession
    from src.sessions.position_review_session import PositionReviewSession
    from src.sessions.quarterly_meta_session import QuarterlyMetaReflectionSession

    registry = {
        "live": None,  # the scheduler loop itself; its jobs run the modes below
        "daily": None,  # src/pipeline_daily_export.py: a CSV, no broker stop audit
        "once": MorningSession,
        "morning": MorningSession,
        "midday": PositionReviewSession,
        "close": PositionReviewSession,
        "evening": EveningSession,
        "intra_check": IntradaySession,
        "intra_safety": IntradaySafety,
        "earnings_preprocess": EarningsPreprocessSession,
        "meta": QuarterlyMetaReflectionSession,
    }
    return registry[mode]


def mode_audits_broker_stops(mode: str | None) -> bool:
    """True when the session behind `mode` declares that it audits stops at the broker.

    Fail closed: a mode that is None or unregistered is treated as auditing,
    so a caller that cannot name its mode gets the page rather than silence.
    """
    if mode is None:
        return True
    try:
        cls = session_class_for_mode(mode)
    except KeyError:
        return True
    return cls is not None and bool(getattr(cls, "AUDITS_BROKER_STOPS", False))
