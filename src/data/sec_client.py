"""SEC EDGAR client: rate-limited GET with retry, CIK lookup, recent filings.

Owns its own request pacing and CIK cache.  The opener, the sleep, the
clocks and the lookback window are handed in by value (`build_sec_client`),
so it is constructed and tested with no provider and no network.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from src.sentinel.counted import record_swallowed
from src.trading_calendar import utc_now
from src.util.time import et_now

logger = logging.getLogger(__name__)

SEC_BASE = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
USER_AGENT = "quant-agent research@example.com"  # SEC requires contact info
REQUEST_DELAY = 0.12  # SEC rate limit: 10 req/s


@dataclass
class FilingInfo:
    symbol: str
    form_type: str  # "10-Q" or "10-K"
    filing_date: str
    accession_number: str
    primary_doc: str  # filename of main document


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
        lookback_days: int,
    ):
        self._opener = opener
        self._request = request
        self._http_error = http_error
        self._url_error = url_error
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._lookback_days = lookback_days
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
        """Get recent 10-Q/10-K filings from SEC EDGAR.

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

        cutoff = (self._now() - timedelta(days=self._lookback_days)).strftime("%Y-%m-%d")
        filings = []
        for form, filing_date, accession, primary_doc in zip(
            forms,
            dates,
            accessions,
            primary_docs,
        ):
            if form not in ("10-Q", "10-K"):
                continue
            if filing_date < cutoff:
                continue
            filings.append(
                FilingInfo(
                    symbol=ticker,
                    form_type=form,
                    filing_date=filing_date,
                    accession_number=accession,
                    primary_doc=primary_doc or "",
                )
            )
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
    lookback_days: int = 45,
) -> SecClient:
    return SecClient(
        opener=opener,
        request=request,
        http_error=http_error,
        url_error=url_error,
        sleep=sleep,
        clock=clock,
        now=now,
        lookback_days=lookback_days,
    )
