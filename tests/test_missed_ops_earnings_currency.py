"""Missed-ops earnings signal obeys the one currency rule (no 90-day cutoff).

An analysis is shown only while its filing is the company's latest 10-Q/10-K
and before its legal deadline, decided by `earnings_stance_is_stale` - the
portfolio manager's rule. Symbols covered by the evening session's already
fetched earnings list use that list's `current_until`; others use the stored
manifest stamp. The builder never fetches and never writes the manifest.
"""

from datetime import timedelta
from unittest.mock import MagicMock

from src.trading_calendar import et_today
from tests.test_missed_opportunities import _pipeline_with


def _signal(tmp_path, symbol, filed_days_ago, *, stored_until=None, session_until=None):
    today = et_today()
    filed = (today - timedelta(days=filed_days_ago)).isoformat()
    path = tmp_path / f"analysis_{symbol}.md"
    path.write_text("Sentiment: bullish\nStrong quarter, revenue up.\n")
    entry = {"filing_date": filed, "analysis_path": str(path)}
    if stored_until is not None:
        entry["current_until"] = (today + timedelta(days=stored_until)).isoformat()
    p = _pipeline_with(earnings_manifest={f"{symbol}_10-Q": entry})
    session = None
    if session_until is not None:
        session = [{"symbol": symbol, "current_until": (today + timedelta(days=session_until)).isoformat()}]
    result = p._missed_ops_earnings_signal(session)
    p.earnings_provider.check_and_fetch.assert_not_called()
    p.earnings_provider.save_manifest.assert_not_called()
    return result


def test_old_but_still_latest_report_is_shown(tmp_path):
    assert "ZZZA" in _signal(tmp_path, "ZZZA", 120, session_until=20)


def test_report_superseded_by_newer_filing_is_not_shown(tmp_path):
    # Session fetch found a newer 10-Q (no analysis yet): symbol absent from
    # the session list; the old entry lost its stored stamp when superseded.
    p_result = _signal(tmp_path, "ZZZB", 60)
    assert "ZZZB" not in p_result


def test_report_past_session_deadline_is_not_shown(tmp_path):
    assert "ZZZC" not in _signal(tmp_path, "ZZZC", 30, session_until=-1)


def test_gainer_outside_session_list_with_valid_stored_stamp_is_shown(tmp_path):
    assert "ZZZD" in _signal(tmp_path, "ZZZD", 100, stored_until=10)


def test_gainer_outside_session_list_with_expired_stored_stamp_is_not_shown(tmp_path):
    assert "ZZZE" not in _signal(tmp_path, "ZZZE", 30, stored_until=-1)


def test_digest_builder_makes_no_provider_fetch_call():
    p = _pipeline_with()
    p.earnings_provider = MagicMock()
    p.earnings_provider.manifest = {}
    p._build_missed_opportunities_digest(session_earnings=[])
    p.earnings_provider.check_and_fetch.assert_not_called()
    p.earnings_provider.save_manifest.assert_not_called()
