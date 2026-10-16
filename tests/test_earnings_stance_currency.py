"""An earnings stance is current while its filing is the company's LATEST
periodic report in the SEC submissions list fetched at decision time, and
stale once a newer 10-Q/10-K exists or the next report's latest legal filing
date (Form 10-K Gen. Instr. A(2) / Form 10-Q Gen. Instr. A.1 plus the 17 CFR
240.12b-25(b) extension) has passed. Replaces a made-up 90-day age."""

import json
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pandas as pd
from dateutil.relativedelta import relativedelta
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

from src.agents.portfolio_manager import PortfolioManagerAgent
from src.data.earnings import EarningsDataProvider
from src.data.sec_client import build_sec_client, periodic_report_current_until
from src.trading_calendar import et_today

SYM = "ZZZE"


def _provider(tmp_path, filings: list[dict], *, fiscal_year_end: str | None, with_report_date: bool = True):
    payload = {
        "filings": {
            "recent": {
                "form": [f["form"] for f in filings],
                "filingDate": [f["filed"].isoformat() for f in filings],
                "accessionNumber": [f"0000-{i}" for i, _ in enumerate(filings)],
                "primaryDocument": [f"doc{i}.htm" for i, _ in enumerate(filings)],
            }
        }
    }
    if with_report_date:
        payload["filings"]["recent"]["reportDate"] = [f["period"].isoformat() for f in filings]
    if fiscal_year_end is not None:
        payload["fiscalYearEnd"] = fiscal_year_end
    state = {"outage": False}

    def opener(req, timeout):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if state["outage"]:
            raise URLError("SEC unreachable")
        if "/submissions/" not in url:
            raise URLError("no network in tests")  # a filing download fails

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return _Resp()

    sec = build_sec_client(
        opener=opener, request=Request, http_error=HTTPError, url_error=URLError, sleep=lambda _s: None
    )
    sec.cik_for = lambda _t: "1"
    provider = EarningsDataProvider(data_dir=str(tmp_path), sec_client=sec)
    provider.outage_state = state
    return provider


def _analysed(provider, form: str, filed: date) -> None:
    d = provider.data_dir / SYM
    d.mkdir(parents=True, exist_ok=True)
    (d / f"analysis_{form}_{filed.isoformat()}.md").write_text("# analysis\n")
    provider.manifest[f"{SYM}_{form}"] = {"filing_date": filed.isoformat(), "failed_attempts": 0}


def _stale(reports, asof: date, sentiment: str = "bullish") -> bool:
    """Stale = not served at all, or served but not counted by the PM gate."""
    rows = [
        {
            "symbol": r.symbol,
            "analysis": {"investment_implications": {"sentiment": sentiment}},
            "filing_date": r.filing_date,
            "current_until": getattr(r, "current_until", ""),
        }
        for r in reports
    ]
    if not rows:
        return True
    return SYM in PortfolioManagerAgent.stale_evidence_sources(earnings_analyses=rows, asof=asof)


def _fye_after(period: date) -> str:
    """Fiscal year end one quarter after `period`, so `period` is Q3."""
    return (period + relativedelta(months=3)).strftime("%m%d")


def test_latest_report_is_current_even_at_120_days_old(tmp_path):
    today = et_today()
    filed = today - timedelta(days=120)
    period = filed - timedelta(days=10)
    filings = [{"form": "10-Q", "filed": filed, "period": period}]
    provider = _provider(tmp_path, filings, fiscal_year_end=_fye_after(period))
    _analysed(provider, "10-Q", filed)
    reports = provider.check_and_fetch([SYM])
    assert [r.filing_date for r in reports] == [filed.isoformat()]
    assert not _stale(reports, today)


def test_stale_the_day_after_a_newer_10q_appears(tmp_path):
    today = et_today()
    old_filed = today - timedelta(days=30)
    new_filed = today - timedelta(days=1)
    old_period = old_filed - timedelta(days=30)
    provider = _provider(
        tmp_path,
        [
            {"form": "10-Q", "filed": new_filed, "period": old_period + relativedelta(months=3)},
            {"form": "10-Q", "filed": old_filed, "period": old_period},
        ],
        fiscal_year_end=(old_period + relativedelta(months=9)).strftime("%m%d"),
    )
    _analysed(provider, "10-Q", old_filed)
    reports = provider.check_and_fetch([SYM])
    # The newer filing's download fails here; the superseded stance must not stand in for it.
    assert old_filed.isoformat() not in [r.filing_date for r in reports]
    assert _stale(reports, today)


def test_amendment_does_not_supersede(tmp_path):
    today = et_today()
    filed = today - timedelta(days=100)
    period = filed - timedelta(days=10)
    provider = _provider(
        tmp_path,
        [
            {"form": "10-Q/A", "filed": today - timedelta(days=1), "period": period},
            {"form": "10-Q", "filed": filed, "period": period},
        ],
        fiscal_year_end=_fye_after(period),
    )
    _analysed(provider, "10-Q", filed)
    reports = provider.check_and_fetch([SYM])
    assert [r.filing_date for r in reports] == [filed.isoformat()]
    assert not _stale(reports, today)


def test_backstop_q3_10q_goes_stale_at_period_end_plus_quarter_plus_105_days(tmp_path):
    """Q3 10-Q, nothing filed after it: the next report is the 10-K, due 90
    days after fiscal year end plus the 15-day Rule 12b-25 extension."""
    today = et_today()
    period = today - timedelta(days=140)
    filed = period + timedelta(days=30)
    filings = [{"form": "10-Q", "filed": filed, "period": period}]
    provider = _provider(tmp_path, filings, fiscal_year_end=_fye_after(period))
    _analysed(provider, "10-Q", filed)
    reports = provider.check_and_fetch([SYM])
    # 17 CFR 240.0-3: a weekend/holiday deadline rolls to the next business day.
    raw_deadline = period + relativedelta(months=3) + timedelta(days=105)
    deadline = CustomBusinessDay(calendar=USFederalHolidayCalendar()).rollforward(pd.Timestamp(raw_deadline)).date()
    assert [r.filing_date for r in reports] == [filed.isoformat()]
    assert not _stale(reports, deadline)
    assert _stale(reports, deadline + timedelta(days=1))


def test_missing_report_date_is_stale(tmp_path):
    today = et_today()
    filed = today - timedelta(days=5)
    provider = _provider(
        tmp_path,
        [{"form": "10-Q", "filed": filed, "period": filed - timedelta(days=30)}],
        fiscal_year_end="1231",
        with_report_date=False,
    )
    _analysed(provider, "10-Q", filed)
    reports = provider.check_and_fetch([SYM])
    assert _stale(reports, today)


def test_sec_outage_keeps_a_stored_in_deadline_bearish_stance(tmp_path):
    """A failed SEC fetch must not erase a dissenting stance: dropping a
    bearish view can lift a refusal, so an outage could let a trade through."""
    today = et_today()
    filed = today - timedelta(days=20)
    period = filed - timedelta(days=30)
    filings = [{"form": "10-Q", "filed": filed, "period": period}]
    provider = _provider(tmp_path, filings, fiscal_year_end=_fye_after(period))
    _analysed(provider, "10-Q", filed)
    assert not _stale(provider.check_and_fetch([SYM]), today, "bearish")  # good fetch records currency
    provider.outage_state["outage"] = True
    reports = provider.check_and_fetch([SYM])
    assert [r.filing_date for r in reports] == [filed.isoformat()]
    assert not _stale(reports, today, "bearish")


def test_sec_outage_past_the_stored_deadline_drops_the_stance(tmp_path):
    today = et_today()
    filed = today - timedelta(days=150)
    period = filed - timedelta(days=30)
    filings = [{"form": "10-Q", "filed": filed, "period": period}]
    provider = _provider(tmp_path, filings, fiscal_year_end=_fye_after(period))
    _analysed(provider, "10-Q", filed)
    provider.manifest[f"{SYM}_10-Q"]["current_until"] = (today - timedelta(days=1)).isoformat()
    provider.outage_state["outage"] = True
    assert _stale(provider.check_and_fetch([SYM]), today, "bearish")


def test_backstop_steps_month_end_to_month_end():
    """Sep 30 + one quarter is Dec 31 (not Dec 30); Feb 28 -> May 31."""
    assert periodic_report_current_until("2026-09-30", "0630") == date(2026, 12, 31) + timedelta(days=50)
    assert periodic_report_current_until("2026-02-28", "1130") == date(2026, 5, 31) + timedelta(days=50)


def test_backstop_deadline_on_a_weekend_rolls_to_the_next_business_day():
    """17 CFR 240.0-3: Jun 26 + 50 days is Sat 15 Aug 2026 -> Mon 17 Aug."""
    assert periodic_report_current_until("2026-03-26", "1231") == date(2026, 8, 17)
