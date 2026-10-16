"""SEC EDGAR client: rate-limited GET with retry, CIK lookup, recent filings.

Owns its own request pacing and CIK cache.  The opener, the sleep, the
clocks are handed in by value (`build_sec_client`),
so it is constructed and tested with no provider and no network.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from dateutil.relativedelta import relativedelta

from src.sentinel.counted import record_swallowed
from src.trading_calendar import utc_now
from src.util.time import et_now

logger = logging.getLogger(__name__)

SEC_BASE = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
USER_AGENT = "quant-agent research@example.com"  # SEC requires contact info
REQUEST_DELAY = 0.12  # SEC rate limit: 10 req/s

# Statutory filing deadlines for the NEXT periodic report, used as the backstop
# for how long an earnings stance stays current when a company files nothing
# newer (`periodic_report_current_until`). Each is the LATEST legal deadline —
# the non-accelerated-filer one plus the Rule 12b-25 extension — so a stance is
# never called stale while the next report could still lawfully be on time.
#: Form 10-K General Instruction A(2): non-accelerated filers file within 90 days after fiscal year end.
TEN_K_DUE_DAYS = 90
#: 17 CFR 240.12b-25(b)(2)(ii)(A): a late 10-K is deemed timely if filed within 15 calendar days after its due date.
TEN_K_EXTENSION_DAYS = 15
#: Form 10-Q General Instruction A.1: non-accelerated filers file within 45 days after quarter end.
TEN_Q_DUE_DAYS = 45
#: 17 CFR 240.12b-25(b)(2)(ii)(B): a late 10-Q is deemed timely if filed within 5 calendar days after its due date.
TEN_Q_EXTENSION_DAYS = 5
_QUARTER = relativedelta(months=3)


def periodic_report_current_until(report_date: str, fiscal_year_end: str) -> date | None:
    """Last day an earnings stance on this periodic report can be current
    when the company files nothing newer: the latest legal filing date of
    the NEXT periodic report. Next period end = this report's period of
    report + one quarter; the next report is the 10-K when the company's
    fiscal-year-end anniversary is nearer that period end than either
    neighbouring quarter end (52/53-week filers end a few days off the
    calendar date), else a 10-Q. None — stale, fail closed — when the
    period of report or the fiscal year end is missing or unparseable.
    """
    try:
        next_end = date.fromisoformat(str(report_date)) + _QUARTER
        fye = str(fiscal_year_end)
        month, day = int(fye[:2]), int(fye[2:4])
        anniversaries = []
        for year in (next_end.year - 1, next_end.year, next_end.year + 1):
            try:
                anniversaries.append(date(year, month, day))
            except ValueError:  # 0229 in a non-leap year
                anniversaries.append(date(year, month, day - 1))
    except (TypeError, ValueError):
        return None
    fy_end = min(anniversaries, key=lambda d: abs((d - next_end).days))
    gap = abs((fy_end - next_end).days)
    is_10k = gap < abs((fy_end - (next_end - _QUARTER)).days) and gap < abs((fy_end - (next_end + _QUARTER)).days)
    days = TEN_K_DUE_DAYS + TEN_K_EXTENSION_DAYS if is_10k else TEN_Q_DUE_DAYS + TEN_Q_EXTENSION_DAYS
    return next_end + timedelta(days=days)


@dataclass
class FilingInfo:
    symbol: str
    form_type: str  # "10-Q" or "10-K"
    filing_date: str
    accession_number: str
    primary_doc: str  # filename of main document
    report_date: str = ""  # submissions `reportDate`: the period the report covers
    fiscal_year_end: str = ""  # submissions `fiscalYearEnd`, "MMDD"


class SecClient:
    def __init__(
        self,
        *,
        opener: Callable,
        request: Callable,
        http_error: type[Exception],
        url_error: type[Exception],
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        now: Callable,
    ):
        self._opener = opener
        self._request = request
        self._http_error = http_error
        self._url_error = url_error
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._ticker_to_cik: dict[str, str] | None = None

    def get(
        self,
        url: str,
        max_retries: int = 3,
        total_timeout_s: float = 45.0,
    ) -> bytes:
        """GET with SEC-required headers, rate limiting, and retry on
        transient SEC errors.

        SEC enforces 10 req/sec via 429 (rate-limited) and returns 503
        when the service is overloaded. Before this retry loop, both
        errors raised HTTPError uncaught — caller's broad `except
        Exception` turned them into a silent empty filing list, which
        propagated to evening's `thesis_health_review` as missing 10-Q
        context (the core input for value-investing thesis decisions).

        Retries 429 / 503 / transient URLError with exponential backoff
        (1s, 2s, 4s). 404 / 400 / other 4xx-5xx propagate immediately —
        those mean the URL itself is wrong (bad CIK, missing filing),
        not a transient rate-limit, and retrying wastes the budget.

        `total_timeout_s` caps the worst-case time the loop can spend.
        Without it, 3 retries on a sustained SEC outage could burn
        REQUEST_DELAY(0.12s) + urlopen(15s) + backoff(1+2+4s) = ~21s × 3
        = ~63s per URL. With 77 stocks × 2 calls (submissions + filing
        body) that's hours of session time on a bad SEC day. 45s default
        keeps any single URL's worst-case bounded and lets the outer
        per-symbol `try: except Exception` move on.
        """
        start = self._clock()
        req = self._request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            elapsed = self._clock() - start
            if elapsed > total_timeout_s:
                logger.warning(
                    "SEC fetch exceeded total_timeout_s=%.0fs for %s after %d attempts (elapsed=%.1fs)",
                    total_timeout_s,
                    url,
                    attempt,
                    elapsed,
                )
                if last_exc is not None:
                    raise last_exc
                raise TimeoutError(f"SEC fetch exceeded {total_timeout_s}s for {url}")
            self._sleep(REQUEST_DELAY)
            try:
                with self._opener(req, timeout=15) as resp:
                    return resp.read()
            except self._http_error as e:
                last_exc = e
                if e.code in (429, 503):
                    backoff = 1.0 * (2**attempt)  # 1s → 2s → 4s
                    logger.warning(
                        "SEC %d on attempt %d/%d for %s — backing off %.1fs",
                        e.code,
                        attempt + 1,
                        max_retries,
                        url,
                        backoff,
                    )
                    self._sleep(backoff)
                    continue
                # Non-transient HTTP error: don't retry, surface immediately.
                raise
            except self._url_error as e:
                # Network blip (DNS / connection reset / timeout). Retry
                # since these are typically transient.
                last_exc = e
                backoff = 1.0 * (2**attempt)
                logger.warning(
                    "SEC URLError on attempt %d/%d for %s: %s — backing off %.1fs",
                    attempt + 1,
                    max_retries,
                    url,
                    e,
                    backoff,
                )
                self._sleep(backoff)
                continue
        # All retries exhausted; surface the last exception so caller
        # (currently inside a broad except Exception) can log it.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"SEC fetch failed for {url} without exception")

    def cik_for(self, ticker: str) -> str | None:
        """Look up CIK number for a ticker symbol."""
        if self._ticker_to_cik is None:
            try:
                data = json.loads(self.get(SEC_TICKERS_URL))
                self._ticker_to_cik = {}
                for entry in data.values():
                    t = entry.get("ticker", "").upper()
                    cik = str(entry.get("cik_str", ""))
                    if t and cik:
                        self._ticker_to_cik[t] = cik
            except Exception as e:
                record_swallowed("data.sec_client.ticker_map", e, log=logger)
                logger.warning("Failed to fetch SEC ticker map: %s", e)
                self._ticker_to_cik = {}
        return self._ticker_to_cik.get(ticker.upper())

    def recent_filings(self, cik: str, ticker: str) -> list[FilingInfo]:
        """Every 10-Q/10-K in the company's SEC submissions list, newest first.

        No age cut: the first entry is the company's LATEST periodic report,
        which is what decides whether an earnings stance is still current
        (`periodic_report_current_until`). Amendments (10-Q/A, 10-K/A) are
        excluded — they restate a period already reported, not a newer one.

        Note on MLPs (master limited partnerships, e.g. EPD): they are SEC
        registrants and DO file 10-Q/10-K via the partnership entity —
        no special handling required. The Schedule K-1 some operators
        associate with MLPs is a tax document mailed to unit holders, not
        a substitute for the corporate filing. EPD shows up on EDGAR with
        regular quarterly 10-Qs that this method will pick up.
        """
        padded_cik = cik.zfill(10)
        url = f"{SEC_BASE}/submissions/CIK{padded_cik}.json"
        try:
            data = json.loads(self.get(url))
        except Exception as e:
            record_swallowed("data.sec_client.submissions", e, log=logger, symbol=ticker)
            logger.warning("Failed to fetch submissions for %s (CIK %s): %s", ticker, cik, e)
            return []

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        report_dates = recent.get("reportDate") or [""] * len(forms)
        fiscal_year_end = str(data.get("fiscalYearEnd") or "")

        # SEC's submissions JSON returns parallel arrays; in practice they
        # always align, but an upstream truncation or partial response
        # would silently desync them. Index-based access on the previous
        # version checked only forms vs dates length and could IndexError
        # on accessions / primary_docs if those came up short. zip()
        # tolerates whichever array is shortest and exits cleanly — at
        # worst we miss a trailing filing rather than crash mid-scan.
        if not (len(forms) == len(dates) == len(accessions) == len(primary_docs)):
            logger.warning(
                "SEC submissions arrays misaligned for %s (CIK %s): "
                "forms=%d dates=%d accessions=%d primary_docs=%d — "
                "iterating over the shortest",
                ticker,
                cik,
                len(forms),
                len(dates),
                len(accessions),
                len(primary_docs),
            )

        filings = []
        for form, filing_date, accession, primary_doc, report_date in zip(
            forms,
            dates,
            accessions,
            primary_docs,
            report_dates,
        ):
            if form not in ("10-Q", "10-K"):
                continue
            filings.append(
                FilingInfo(
                    symbol=ticker,
                    form_type=form,
                    filing_date=filing_date,
                    accession_number=accession,
                    primary_doc=primary_doc or "",
                    report_date=report_date or "",
                    fiscal_year_end=fiscal_year_end,
                )
            )
        filings.sort(key=lambda f: f.filing_date, reverse=True)
        return filings


def build_sec_client(
    *,
    opener: Callable,
    request: Callable,
    http_error: type[Exception],
    url_error: type[Exception],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = lambda: utc_now().timestamp(),
    now: Callable = et_now,
) -> SecClient:
    return SecClient(
        opener=opener,
        request=request,
        http_error=http_error,
        url_error=url_error,
        sleep=sleep,
        clock=clock,
        now=now,
    )
