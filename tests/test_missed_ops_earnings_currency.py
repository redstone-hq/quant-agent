"""Missed-ops earnings signal obeys the one currency rule (no 90-day cutoff).

An analysis is shown only while its filing is the company's latest 10-Q/10-K
and before the stored legal deadline - decided by the earnings provider and
`earnings_stance_is_stale`, the same rule the portfolio manager uses.
"""

from datetime import timedelta

from src.data.earnings import EarningsDataProvider
from src.data.sec_client import FilingInfo, periodic_report_current_until
from src.trading_calendar import et_today
from tests.test_missed_opportunities import _pipeline_with


class _FakeSec:
    def __init__(self, filings=None, outage=False):
        self.filings, self.outage = filings, outage

    def cik_for(self, symbol):
        if self.outage:
            raise OSError("sec down")
        return "0000001"

    def recent_filings(self, cik, symbol):
        return self.filings


def _provider(tmp_path, sec, symbol, filing_date, *, current_until=None):
    prov = EarningsDataProvider(data_dir=str(tmp_path), sec_client=sec)
    d = tmp_path / symbol
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"analysis_10-Q_{filing_date}.md"
    path.write_text("Sentiment: bullish\nStrong quarter, revenue up.\n")
    entry = {"filing_date": filing_date, "analysis_path": str(path)}
    if current_until:
        entry["current_until"] = current_until
    prov.manifest = {f"{symbol}_10-Q": entry}
    return prov


def _signal(prov):
    p = _pipeline_with()
    p.earnings_provider = prov
    return p._missed_ops_earnings_signal()


def test_old_but_still_latest_report_is_shown(tmp_path):
    today = et_today()
    filed = today - timedelta(days=100)
    period = today - timedelta(days=105)
    fye = f"{period.month:02d}{period.day:02d}"
    assert periodic_report_current_until(period.isoformat(), fye) > today
    sec = _FakeSec([FilingInfo("ZZZA", "10-Q", filed.isoformat(), "a", "d", period.isoformat(), fye)])
    assert "ZZZA" in _signal(_provider(tmp_path, sec, "ZZZA", filed.isoformat()))


def test_report_superseded_by_newer_filing_is_not_shown(tmp_path):
    today = et_today()
    old = (today - timedelta(days=60)).isoformat()
    new = FilingInfo("ZZZB", "10-Q", (today - timedelta(days=1)).isoformat(), "a", "d", "2026-09-30", "1231")
    prov = _provider(tmp_path, _FakeSec([new]), "ZZZB", old)
    prov._download_filing = lambda cik, filing: None
    assert "ZZZB" not in _signal(prov)


def test_report_past_stored_deadline_is_not_shown(tmp_path):
    today = et_today()
    filed = (today - timedelta(days=30)).isoformat()
    prov = _provider(
        tmp_path,
        _FakeSec(outage=True),
        "ZZZC",
        filed,
        current_until=(today - timedelta(days=1)).isoformat(),
    )
    assert "ZZZC" not in _signal(prov)
