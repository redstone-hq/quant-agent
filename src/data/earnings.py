"""SEC EDGAR earnings data provider.

Downloads 10-Q and 10-K filings, extracts text, and tracks what's been fetched
via a local manifest so filings are only downloaded once.
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.util.time import et_now, et_today
from pathlib import Path
from urllib.error import HTTPError, URLError  # noqa: F401 — handed to the SEC client
from urllib.request import urlopen, Request

from src.data.filing_text import extract_text
from src.data.sec_client import (  # noqa: F401 — re-exported for existing callers
    REQUEST_DELAY,
    SEC_ARCHIVES,
    SEC_BASE,
    SEC_TICKERS_URL,
    USER_AGENT,
    FilingInfo,
    SecClient,
    build_sec_client,
    periodic_report_current_until,
)
from src.data.xbrl_facts import fetch_xbrl_raw, format_xbrl_text, xbrl_comparable_values
from src.sentinel.counted import record_swallowed

logger = logging.getLogger(__name__)


# ETFs don't have SEC 10-Q/10-K filings — skip them at the entry point to
# avoid wasting CIK lookups + retry budget on something that will always
# fail. Keep this list in sync with `config/settings.yaml:trading.universe`
# whenever a new ETF is added there.
ETFS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "XLF",
    "XLE",
    "XLV",
    "XLI",
    "XLP",
    "XLY",
    "XLU",
    "XLRE",
    "XLB",
    "SMH",
    "SOXX",
    "DRAM",
    "CHPX",
    "SH",
    "SDS",
    "PSQ",
    "SQQQ",
}


@dataclass
class EarningsReport:
    symbol: str
    form_type: str
    filing_date: str
    filing_path: str  # local path to raw HTML
    analysis_path: str | None  # local path to analysis markdown
    text_excerpt: str  # extracted text for LLM (truncated)
    is_new: bool  # True if just downloaded this run
    # Parsed (not text-block) SEC XBRL values for the small set of fields
    # that map ONE-TO-ONE onto an `EarningsAnalysis` field — see
    # `EarningsDataProvider._xbrl_comparable_values` for which fields and
    # why only those. Used by `_classify_earnings_status`
    # (src/pipeline_stages.py) to cross-check the analyst's own reported
    # figures against the real filed numbers, catching a confident but
    # FABRICATED figure that isn't empty and isn't self-flagged. Empty dict
    # (not None) both when XBRL had nothing for this filer/period (fails
    # open, same as the text block) and for the cached-analysis path below,
    # which never re-fetches XBRL — either way there is nothing to cross-
    # check against, which the mismatch check already treats as "not a
    # mismatch" for an absent value.
    xbrl_facts: dict = field(default_factory=dict)
    # Last day (ISO) the stance on this report is current, decided from the
    # SEC submissions list fetched THIS session: the report is the company's
    # latest periodic report, and this is the latest legal filing date of the
    # next one (`periodic_report_current_until`). "" = unknown = stale.
    current_until: str = ""
    # True when this run is the first read of a filing filed BEFORE the
    # overnight window (see `is_catch_up_read`). "New" only means first
    # downloaded within `lookback_days`, so a name first checked today can
    # bring a weeks-old filing (AVO/ZUMZ/LSAK, 8-10 Sep filings read on
    # 19 Oct); this keeps it from being shown as overnight news.
    catch_up_read: bool = False

    def evidence_fields(self) -> dict:
        """Structured read facts that travel with the analysis to the seat and the owner message."""
        return {
            "filing_date": self.filing_date,
            "first_read_today": bool(self.is_new),
            "catch_up_read": bool(self.catch_up_read),
        }


def is_catch_up_read(filing_date: str, today: date) -> bool:
    """A filing dated before the previous weekday is not overnight news.

    The overnight window opens on the previous weekday (Monday's opens on
    Friday). Holidays are not skipped, so a filing from the last session
    before a holiday reads as catch-up — the cautious side.
    """
    prior = today - timedelta(days=1)
    while prior.weekday() >= 5:
        prior -= timedelta(days=1)
    return str(filing_date) < prior.isoformat()


class EarningsDataProvider:
    def __init__(self, data_dir: str = "data/earnings", lookback_days: int = 45, sec_client: SecClient | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.data_dir / "manifest.json"
        self._manifest_lock = threading.Lock()
        self.manifest = self._load_manifest()
        self.lookback_days = lookback_days
        # The opener resolves `urlopen` in THIS module on every call, so the
        # rehearsal recording that rebinds `src.data.earnings.urlopen` still
        # intercepts SEC traffic.
        self.sec = sec_client or build_sec_client(
            opener=lambda req, timeout: urlopen(req, timeout=timeout),
            request=lambda *a, **k: Request(*a, **k),
            http_error=HTTPError,
            url_error=URLError,
        )

    def _load_manifest(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Corrupt manifest, starting fresh: %s", e)
        return {}

    def save_manifest(self):
        with self._manifest_lock:
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.manifest, indent=2))
            os.replace(str(tmp), str(self.manifest_path))

    def prune(self, keep_days: int = 1000) -> int:
        """Delete raw filing HTML (``{FORM}_{YYYY-MM-DD}.html``) older than
        keep_days. The file-stores had no prune (design-review finding) — raw
        10-K/10-Q HTML (often multiple MB each) accreted per symbol per quarter
        forever.

        Conservative + safe: raw HTML is read ONLY at analysis time; once a
        filing is analyzed, the cached ``analysis_{FORM}_{date}.md`` is the read
        target (``_check_symbol`` globs the LATEST analysis per symbol). So this
        prunes only old raw HTML and leaves ALL analysis markdown untouched
        (small, and the actual money-relevant read target for evening's
        thesis_health). keep_days=1000 retains ~2.7 years of filings; only
        stale orphans go. Best-effort. Returns the count removed.
        """
        cutoff = et_today() - timedelta(days=keep_days)
        removed = 0
        try:
            symbol_dirs = [d for d in self.data_dir.iterdir() if d.is_dir()]
        except OSError as exc:
            logger.warning("earnings prune: cannot list %s: %s", self.data_dir, exc)
            return 0
        for sdir in symbol_dirs:
            for f in sdir.glob("*.html"):
                # filename: '{FORM}_{YYYY-MM-DD}.html' e.g. '10-Q_2026-03-15.html'
                datestr = f.stem.rsplit("_", 1)[-1]
                try:
                    d = date.fromisoformat(datestr)
                except (ValueError, TypeError):
                    continue  # unrecognized name — leave it alone
                if d < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError as exc:
                        logger.warning("earnings prune: failed to rm %s: %s", f, exc)
        if removed:
            logger.info(
                "earnings prune: removed %d raw filing HTML older than %s",
                removed,
                cutoff,
            )
        return removed

    def confirm_filing(self, report: "EarningsReport"):
        """Mark a filing as processed in the manifest. Call after analysis file is written."""
        with self._manifest_lock:
            manifest_key = f"{report.symbol}_{report.form_type}"
            self.manifest[manifest_key] = {
                "filing_date": report.filing_date,
                "form_type": report.form_type,
                "local_path": report.filing_path,
                "analysis_path": report.analysis_path,
                "current_until": report.current_until,
                "failed_attempts": 0,
            }
        self.save_manifest()

    def record_failure(self, report: "EarningsReport", max_attempts: int = 3) -> bool:
        """Track a failed LLM analysis attempt. Abandon after `max_attempts`.

        Without bounded retries, a filing whose analysis consistently fails
        (parse error, rate limit, model overloaded) would be re-queued every
        session forever — wasting tokens indefinitely. After max_attempts we
        mark the filing abandoned so _check_symbol skips it and falls back to
        any prior analysis.

        Filing-date scoping: the manifest is keyed by symbol+form_type, but
        a single key spans multiple quarters of 10-Qs. When the entry's
        stored filing_date differs from the incoming report, this is a NEW
        filing — reset failed_attempts and abandoned flag so a one-off
        parse failure on Q1 doesn't pre-abandon Q2 on its first attempt.
        Codex r11 P2: previously the prior quarter's abandoned/attempts
        carried forward, so Q2's first transient failure landed at
        attempts=4 (abandon immediately).

        Returns True when the filing has just been abandoned (caller should
        stop queueing it).
        """
        abandoned = False
        with self._manifest_lock:
            key = f"{report.symbol}_{report.form_type}"
            entry = dict(self.manifest.get(key, {}))
            prior_filing_date = entry.get("filing_date")
            if prior_filing_date and prior_filing_date != report.filing_date:
                # Different filing_date → this is a new quarter. Reset the
                # retry budget; previous failure history doesn't apply.
                entry["failed_attempts"] = 0
                entry.pop("abandoned", None)
                entry.pop("abandoned_at", None)
                logger.info(
                    "Earnings retry budget reset for %s %s: prior filing %s → new filing %s",
                    report.symbol,
                    report.form_type,
                    prior_filing_date,
                    report.filing_date,
                )
            attempts = int(entry.get("failed_attempts", 0)) + 1
            entry["filing_date"] = report.filing_date
            entry["form_type"] = report.form_type
            entry["local_path"] = report.filing_path
            entry["failed_attempts"] = attempts
            if attempts >= max_attempts:
                entry["abandoned"] = True
                entry["abandoned_at"] = et_now().isoformat()
                abandoned = True
                logger.error(
                    "Abandoning earnings analysis for %s %s (%s) after %d attempts",
                    report.symbol,
                    report.form_type,
                    report.filing_date,
                    attempts,
                )
            else:
                logger.warning(
                    "Earnings analysis for %s %s failed (attempt %d/%d); will retry next session",
                    report.symbol,
                    report.form_type,
                    attempts,
                    max_attempts,
                )
            self.manifest[key] = entry
        self.save_manifest()
        return abandoned

    def _download_filing(self, cik: str, filing: FilingInfo) -> str | None:
        """Download filing HTML and save to local file. Returns local path."""
        # The download window: a latest report first seen more than
        # `lookback_days` after it was filed is not downloaded and analysed
        # now. It decides only what gets newly analysed, never whether an
        # analysis already on disk is current.
        if filing.filing_date < (et_today() - timedelta(days=self.lookback_days)).isoformat():
            return None
        symbol_dir = self.data_dir / filing.symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)

        accession_clean = filing.accession_number.replace("-", "")
        url = f"{SEC_ARCHIVES}/{cik}/{accession_clean}/{filing.primary_doc}"

        local_path = symbol_dir / f"{filing.form_type}_{filing.filing_date}.html"
        if local_path.exists():
            return str(local_path)

        try:
            content = self.sec.get(url)
            local_path.write_bytes(content)
            logger.info("Downloaded %s %s (%s) → %s", filing.symbol, filing.form_type, filing.filing_date, local_path)
            return str(local_path)
        except Exception as e:
            record_swallowed("data.earnings.download", e, log=logger, symbol=filing.symbol)
            return None

    def _get_analysis_path(self, symbol: str, form_type: str, filing_date: str) -> str:
        """Return path for the analysis markdown file."""
        symbol_dir = self.data_dir / symbol
        symbol_dir.mkdir(parents=True, exist_ok=True)
        return str(symbol_dir / f"analysis_{form_type}_{filing_date}.md")

    def check_and_fetch(self, symbols: list[str]) -> list[EarningsReport]:
        """Check for new filings for all symbols. Download new ones, return reports.

        Returns EarningsReport for each symbol that has:
        - A newly downloaded filing (is_new=True), or
        - An existing analysis from a previous run (is_new=False)
        """
        reports: list[EarningsReport] = []
        stocks = [s for s in symbols if s not in ETFS]

        for symbol in stocks:
            try:
                report = self._check_symbol(symbol)
                if report:
                    reports.append(report)
            except Exception as e:
                logger.warning("Error checking earnings for %s: %s", symbol, e)
        self.save_manifest()  # persists `current_until` recorded this pass

        logger.info(
            "Earnings check: %d reports (%d new) from %d stocks",
            len(reports),
            sum(1 for r in reports if r.is_new),
            len(stocks),
        )
        return reports

    def _check_symbol(self, symbol: str) -> EarningsReport | None:
        """The earnings report for a symbol's LATEST periodic report, or None.

        An earnings stance is current only while its filing is the company's
        latest 10-Q/10-K in the submissions list fetched now; an analysis of
        any older report is superseded and is never served. A ticker absent
        from SEC's map, or a company with no periodic report, gets None.

        SEC outage (the ticker map or the submissions list cannot be fetched):
        serve the analysis the last good fetch recorded as current, with its
        stored `current_until`, unless that deadline has passed today. An
        outage must not erase a stance — dropping a dissenting one can lift a
        refusal and let a trade through.
        """
        cik, outage = self._lookup_cik(symbol)
        if not cik:
            return self._stored_current_analysis(symbol, outage=outage)

        filings = self.sec.recent_filings(cik, symbol)
        if not filings:
            # None = the fetch failed (outage); [] = no periodic report at all.
            return self._stored_current_analysis(symbol, outage=filings is None)

        latest = filings[0]
        until = periodic_report_current_until(latest.report_date, latest.fiscal_year_end)
        current_until = until.isoformat() if until else ""
        self._record_currency(symbol, latest, current_until)
        manifest_key = f"{symbol}_{latest.form_type}"
        entry = self.manifest.get(manifest_key, {})
        last_known = entry.get("filing_date")

        # Honor the abandoned flag: after N failed analysis attempts we stop
        # re-queueing this specific filing. Serve this filing's own analysis
        # if one is on disk; never an older report's (it is superseded).
        if entry.get("abandoned") and last_known == latest.filing_date:
            logger.info(
                "Skipping %s %s (%s) — previously abandoned after repeated LLM failures",
                symbol,
                latest.form_type,
                latest.filing_date,
            )
            return self._get_existing_analysis(symbol, latest, current_until)

        # "Already processed" must mean SUCCEEDED, not merely attempted.
        #
        # 2026-07-16 audit: record_failure() writes the new filing_date into
        # the manifest alongside failed_attempts=1 and logs "will retry next
        # session" — but this gate then saw last_known == latest.filing_date,
        # found the PRIOR quarter's analysis on disk, and returned it with
        # is_new=False. The pipeline only re-queues is_new reports, so the
        # filing was never re-analyzed, record_failure never ticked again, the
        # 3-strike budget never reached `abandoned`, and PM was served last
        # quarter's numbers labelled "[from cache]" as if they were current.
        # One transient LLM failure permanently dropped that quarter's filing —
        # for every symbol that already had a same-form analysis on disk, i.e.
        # the whole universe in steady state.
        # confirm_filing() writes failed_attempts=0 on success, so a genuinely
        # processed filing still short-circuits here. Attempts 1-2 now fall
        # through to re-download → is_new=True → re-analysis; on the 3rd
        # failure the `abandoned` branch above takes over as designed.
        try:
            prior_failures = int(entry.get("failed_attempts", 0) or 0)
        except (TypeError, ValueError):
            prior_failures = 0
        if last_known == latest.filing_date and not prior_failures:
            # Already processed this filing — return existing analysis matching this form_type
            existing = self._get_existing_analysis(symbol, latest, current_until)
            if existing:
                return existing
            # Analysis file missing (e.g. killed mid-analysis) — re-download
        elif last_known == latest.filing_date and prior_failures:
            logger.info(
                "%s %s (%s): retrying after %d failed analysis attempt(s)",
                symbol,
                latest.form_type,
                latest.filing_date,
                prior_failures,
            )

        # New filing — download it
        local_path = self._download_filing(cik, latest)
        if not local_path:
            return self._get_existing_analysis(symbol, latest, current_until)

        text = extract_text(local_path)
        xbrl_raw = fetch_xbrl_raw(self.sec.get, cik, symbol, latest.filing_date)
        xbrl_block = format_xbrl_text(xbrl_raw)
        if xbrl_block:
            text = xbrl_block + "\n" + text
        analysis_path = self._get_analysis_path(symbol, latest.form_type, latest.filing_date)

        return EarningsReport(
            symbol=symbol,
            form_type=latest.form_type,
            filing_date=latest.filing_date,
            filing_path=local_path,
            analysis_path=analysis_path,
            text_excerpt=text,
            is_new=True,
            xbrl_facts=xbrl_comparable_values(xbrl_raw),
            current_until=current_until,
            catch_up_read=is_catch_up_read(latest.filing_date, et_today()),
        )

    def _lookup_cik(self, symbol: str) -> tuple[str | None, bool]:
        """(CIK, outage): (None, True) when SEC's ticker map could not be
        fetched; (None, False) when it was and the ticker is not in it."""
        try:
            return self.sec.cik_for(symbol), False
        except Exception as e:
            record_swallowed("data.earnings.ticker_map", e, log=logger, symbol=symbol)
            return None, True

    def _record_currency(self, symbol: str, latest: FilingInfo, current_until: str) -> None:
        """Persist, from a good SEC fetch, which filing is current and until
        when: the latest report's manifest entry carries `current_until`;
        every other entry of this symbol loses it (superseded). Read back by
        `_stored_current_analysis` when a later fetch fails."""
        with self._manifest_lock:
            for key, entry in self.manifest.items():
                if not isinstance(entry, dict) or not key.startswith(f"{symbol}_"):
                    continue
                if key == f"{symbol}_{latest.form_type}" and entry.get("filing_date") == latest.filing_date:
                    entry["current_until"] = current_until
                else:
                    entry.pop("current_until", None)

    def _stored_current_analysis(self, symbol: str, *, outage: bool) -> EarningsReport | None:
        """SEC outage: serve the analysis the last good fetch recorded as
        current, with its stored `current_until`, if that deadline has not
        passed today. Dropping it instead would remove a dissenting stance
        too, which can lift a refusal — an outage must not let a trade through."""
        if not outage:
            return None
        entries = [
            (k[len(symbol) + 1 :], e)
            for k, e in self.manifest.items()
            if isinstance(e, dict) and k.startswith(f"{symbol}_") and e.get("current_until")
        ]
        if not entries:
            return None
        form_type, entry = max(entries, key=lambda ke: str(ke[1].get("filing_date") or ""))
        try:
            if et_today() > date.fromisoformat(str(entry["current_until"])):
                return None
            filing = FilingInfo(symbol, form_type, str(entry["filing_date"]), "", "")
        except (KeyError, TypeError, ValueError):
            return None
        return self._get_existing_analysis(symbol, filing, str(entry["current_until"]))

    def _get_existing_analysis(self, symbol: str, latest: FilingInfo, current_until: str) -> EarningsReport | None:
        """The analysis already on disk for exactly `latest` — the company's
        latest periodic report — or None.

        Never an older report's analysis: once a newer 10-Q/10-K exists, the
        stance on the previous one is superseded, so a symbol whose latest
        report has not been analysed looks exactly like one with no earnings
        coverage yet, which every consumer already treats as ordinary.
        """
        path = self.data_dir / symbol / f"analysis_{latest.form_type}_{latest.filing_date}.md"
        if not path.exists():
            return None
        return EarningsReport(
            symbol=symbol,
            form_type=latest.form_type,
            filing_date=latest.filing_date,
            filing_path="",
            analysis_path=str(path),
            text_excerpt="",  # No text needed — analysis already exists
            is_new=False,
            current_until=current_until,
        )
