"""SEC-native, fail-soft Form 4 smart-money provider.

``refresh`` is the only network path. It discovers Form 4/4-A filings through
the SEC full-text-search index, caches complete submissions by accession, and
parses exact non-derivative P/S rows. ``fetch`` is cache-only and applies the
materiality/cluster reduction before any LLM can see the evidence.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

from src.data.insider_signal import (
    InsiderHistory,
    InsiderPriorTrade,
    InsiderSignalThresholds,
    classify_transaction,
    holdings_fraction,
)
from src.data.smart_money_cluster import (
    MAX_CLUSTER_RESERVED_SLOTS,
    cluster_reservation_census,
    cluster_survivors,
    insider_purchase_clusters,
    observation_key,
    reserve_cluster_symbols,
)
from src.data.smart_money_edgar_coverage import (
    ACCESSION_RE,
    blank_edgar_coverage,
    edgar_coverage,
    edgar_total,
    well_formed_hit,
)
from src.data.smart_money_stores import build_smart_money_stores
from src.models import SmartMoneyObservation
from src.util.time import et_now, et_today

logger = logging.getLogger(__name__)

# Fallback if the provider is constructed without an explicit
# ``insider_history_retention_days`` (every production call site passes one
# from ``config.smart_money.insider_history_retention_days`` — see
# ``src/pipeline.py``). Five years covers the three preceding years the
# default calendar-month routine test needs, with slack for late and
# amended filings; kept in sync with ``SmartMoneyConfig``'s own default in
# ``src/config.py``.
_DEFAULT_HISTORY_RETENTION_DAYS = 5 * 366


def _history_entry_date(entry: str) -> date | None:
    try:
        return date.fromisoformat(str(entry).partition("|")[0])
    except ValueError:
        return None


EFTS_SEARCH = "https://efts.sec.gov/LATEST/search-index"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_EXCHANGE = "https://www.sec.gov/files/company_tickers_exchange.json"
# Per-entity filing history. SEC documents this as "Each entity's current
# filing history", covering "at least one year's of filing or ... 1,000
# (whichever is more) of the most recent filings"
# (https://www.sec.gov/search-filings/edgar-application-programming-interfaces,
# read 2026-09-18). Two properties were MEASURED against the live endpoint
# on 2026-09-18 before this module was allowed to depend on them:
#   * the ISSUER's CIK carries its officers' Form 4s, not just the reporting
#     owner's — AAPL (CIK 320193), NVDA, RSG all list hundreds of form "4"
#     rows under the issuer CIK;
#   * it reflects SAME-DAY filings within minutes — General Dynamics
#     (CIK 40533) Form 4s accepted 13:30 ET on 2026-09-18 were already
#     present when queried that afternoon.
# The second property is why this, and not the daily-index file, answers the
# intraday freshness question: `/Archives/edgar/daily-index/.../form.<date>.idx`
# for the CURRENT day did not exist when measured the same afternoon (HTTP 403,
# identical to a future date; the newest published file was the prior business
# day). An index that cannot see today cannot decide whether today's evidence
# is stale.
SEC_SUBMISSIONS = "https://data.sec.gov/submissions"
DEFAULT_USER_AGENT = "QAMC/1.0 research-intelligence https://github.com/yebof/quant-agent"
_LISTED_EXCHANGES = {"Nasdaq", "NYSE", "CBOE"}
_ET = ZoneInfo("America/New_York")
_ACCEPTED_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})", re.I)
_XML_RE = re.compile(
    r"<XML>\s*((?:<\?xml[^>]*>\s*)?<ownershipDocument>.*?</ownershipDocument>)\s*</XML>",
    re.I | re.S,
)
_NON_EQUITY_SUFFIXES = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT")

# Board item 124, corrected defect axis (2026-09-20): a genuinely clustered
# SYMBOL can lose its whole signal to `max_observations` truncation when
# unrelated, higher-dollar buys on OTHER symbols fill every slot ahead of
# it — cross-symbol crowd-out, not the within-symbol tie-break a previously
# rejected fix targeted (that fix adjusted the shared sort key everything
# else relies on and was found unsafe on review: wrong signal-weight values,
# a nonexistent flag, and it would have overridden dollar-value ordering
# entirely rather than narrowly tie-breaking). The adversary-recommended fix
# instead: guarantee at least one surviving row per genuinely clustered
# symbol via a SMALL, separately-bounded reservation
# (`smart_money_cluster.reserve_cluster_symbols`), leaving the main
# dollar-value sort below untouched for everyone else. The reservation's own
# bound, `smart_money_cluster.MAX_CLUSTER_RESERVED_SLOTS`, is recorded in
# config/number_ledger.yaml — it lives in `smart_money_cluster.py`, not here,
# because that module (not this one) is inside the ledger's scanned scope.

# One process-global limiter covers EFTS, ticker metadata and Archives calls.
# 0.125 seconds is exactly 8 requests/sec, below the SEC's 10 req/s cap.
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_MIN_REQUEST_INTERVAL_S = 0.125


class SmartMoneySource(Protocol):
    def refresh(self, symbols: list[str] | None = None) -> dict: ...
    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]: ...


class _RefreshDeadline(TimeoutError):
    pass


def _text(node: ET.Element, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _number(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _bool(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return None


def _symbol(value: str) -> str:
    return str(value or "").strip().upper().replace(".", "-")


# Board item 63: how many sale rows the census carries forward. The census
# is a recording, not a gate: it governs no buy, no sell and no size, so
# this bound only limits how much JSON one run writes.
MAX_SALE_CENSUS_ROWS = 200


def _sale_census(parsed: list["SmartMoneyObservation"]) -> dict:
    """Count and sample the SALE rows a fetch parsed, by holdings band.

    The forward return is NOT computed here and cannot be: it is forward.
    Each sampled row carries the reference price and transaction date a
    later pass needs to join one on. Until that join exists the recording
    answers only "how many sales, in which band" -- which is already more
    than the desk has ever held.
    """
    sales = [item for item in parsed if item.direction == "sell"]
    bands: dict[str, int] = {}
    for item in sales:
        key = str(item.holdings_fraction_band or "unknown")
        bands[key] = bands.get(key, 0) + 1
    ordered = sorted(
        sales,
        key=lambda item: (item.accepted_at.timestamp() if item.accepted_at else 0.0,),
        reverse=True,
    )[:MAX_SALE_CENSUS_ROWS]
    return {
        "parsed_rows": len(parsed),
        "sale_rows": len(sales),
        "buy_rows": sum(1 for item in parsed if item.direction == "buy"),
        "band_counts": bands,
        "forward_return_joined": False,
        "rows": [
            {
                "symbol": item.symbol,
                "transaction_date": item.transaction_date.isoformat(),
                "holdings_fraction": item.holdings_fraction,
                "holdings_fraction_band": item.holdings_fraction_band,
                "reference_price": item.price_per_share,
                "actor_cik": item.actor_cik,
                "accession_number": item.accession_number,
            }
            for item in ordered
        ],
    }


class SECForm4Provider:
    """Credentialless SEC Form 4 discovery with bounded, resumable caching."""

    def __init__(
        self,
        *,
        search_url: str = EFTS_SEARCH,
        archives_url: str = SEC_ARCHIVES,
        submissions_url: str = SEC_SUBMISSIONS,
        data_dir: str = "data/smart_money",
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_s: float | None = None,
        request_timeout_s: float = 15.0,
        requests_per_second: float = 8.0,
        lookback_days: int = 14,
        # ROW-RETENTION window for `cluster_survivors`, NOT the research cluster
        # (corrected 2026-09-19, board item 124). Alldredge & Blank's abstract
        # (J. Financial Research, 2019) measures SAME-DAY purchases; "within two
        # days" appears only in a secondary summary (IBKR Campus). The
        # research-defined same-day opportunistic purchase cluster is
        # `src.data.smart_money_cluster.insider_purchase_clusters`. Was 14 days
        # with no documented rationale until the 2026-09-04 audit fix.
        cluster_window_days: int = 2,
        min_cluster_owners: int = 2,
        max_observations: int = 40,
        refresh_deadline_s: float = 180,
        # The watched-name drain's OWN budget, separate from the market-wide
        # pass's `refresh_deadline_s`. Derivation lives with the production
        # value at `src/config.py::SmartMoneyConfig.watched_drain_deadline_s`;
        # this default mirrors it.
        watched_drain_deadline_s: float = 859,
        max_filings_per_refresh: int = 1000,
        session: requests.Session | None = None,
        # Routine-versus-opportunistic classification thresholds
        # (`src/data/insider_signal.py`). Defaults mirror
        # `InsiderSignalThresholds`'s own defaults; production wiring
        # (`src/pipeline.py`) passes every one of these explicitly from
        # `config.smart_money.insider_*`, so a settings.yaml edit reaches
        # the classifier without a code change.
        insider_calendar_routine_years: int = 3,
        insider_min_cadence_trades: int = 3,
        insider_cadence_min_mean_gap_days: float = 20.0,
        insider_cadence_max_mean_gap_days: float = 120.0,
        insider_cadence_max_gap_dispersion: float = 0.25,
        insider_history_retention_days: int = _DEFAULT_HISTORY_RETENTION_DAYS,
    ):
        self.data_dir = Path(data_dir)
        self.stores = build_smart_money_stores(self.data_dir)
        # Long-horizon (insider, issuer) trade dates. ``observations.json`` is
        # pruned to the lookback window, which is far too short for the
        # three-year calendar-month routine test; this file is the only place
        # that history survives. It stores dates and direction only.
        self.search_url = search_url.rstrip("/")
        self.archives_url = archives_url.rstrip("/")
        self.submissions_url = submissions_url.rstrip("/")
        self.user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        effective_timeout = request_timeout_s if timeout_s is None else timeout_s
        self.timeout_s = max(1.0, float(effective_timeout))
        self.request_interval_s = 1.0 / min(8.0, max(0.5, float(requests_per_second)))
        self.lookback_days = max(1, int(lookback_days))
        self.cluster_window_days = max(1, int(cluster_window_days))
        self.min_cluster_owners = max(2, int(min_cluster_owners))
        self.max_observations = max(1, int(max_observations))
        # Board item 63. Every parsed SALE row is counted and sampled here
        # BEFORE `max_observations` truncates, because the truncation sort
        # below puts `not transient_admission_eligible` FIRST and admission
        # requires `direction == "buy"`, so with more admission-eligible
        # buys than slots no sale can ever reach the analyst or the
        # evidence store. Measured 2026-10-01: the production observations
        # cache held 16,019 sale rows and 2,384 buy rows while the stored
        # evidence held 828 observation rows, every one of them a buy. This
        # census governs NOTHING -- it is read by no gate, no rank and no
        # size -- it exists only so the sale side of
        # `holdings_fraction_band` becomes readable at all.
        self.last_sale_census: dict | None = None
        # n06: what the cluster reservation did on the last refresh. Record-only.
        self.last_cluster_census: dict | None = None
        self.refresh_deadline_s = max(1.0, float(refresh_deadline_s))
        self.watched_drain_deadline_s = max(1.0, float(watched_drain_deadline_s))
        self.max_filings_per_refresh = max(1, int(max_filings_per_refresh))
        self.session = session or requests.Session()
        self._cache_lock = threading.Lock()
        self.history_retention_days = max(1, int(insider_history_retention_days))
        self._signal_thresholds = InsiderSignalThresholds(
            calendar_routine_years=max(1, int(insider_calendar_routine_years)),
            min_cadence_trades=max(2, int(insider_min_cadence_trades)),
            cadence_min_mean_gap_days=max(0.0, float(insider_cadence_min_mean_gap_days)),
            cadence_max_mean_gap_days=max(0.0, float(insider_cadence_max_mean_gap_days)),
            cadence_max_gap_dispersion=max(0.0, float(insider_cadence_max_gap_dispersion)),
        )

    def _load_history(self) -> InsiderHistory:
        """Read the accumulated (insider, issuer) trade index, fail-soft."""
        raw = self.stores.load_history()
        trades: dict[tuple[str, str], list[InsiderPriorTrade]] = {}
        for key, entries in (raw if isinstance(raw, dict) else {}).items():
            actor_cik, _, symbol = str(key).partition("|")
            if not actor_cik or not symbol or not isinstance(entries, list):
                continue
            parsed: list[InsiderPriorTrade] = []
            for entry in entries:
                day, _, direction = str(entry).partition("|")
                try:
                    parsed.append(
                        InsiderPriorTrade(
                            transaction_date=date.fromisoformat(day),
                            direction=direction,
                        )
                    )
                except ValueError:
                    continue
            if parsed:
                trades[(actor_cik, symbol)] = parsed
        return InsiderHistory(trades)

    def _record_history(self, rows: list[dict]) -> None:
        """Merge this refresh's trades into the long-horizon index.

        Append-only apart from a hard age prune. Entries are ``date|direction``
        strings so the file stays a fraction of the observation cache.
        """
        raw = self.stores.load_history()
        merged: dict[str, set[str]] = {
            str(key): {str(value) for value in values}
            for key, values in (raw if isinstance(raw, dict) else {}).items()
            if isinstance(values, list)
        }
        for row in rows:
            actor_cik = str(row.get("actor_cik") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            direction = str(row.get("direction") or "").strip()
            day = str(row.get("transaction_date") or "")[:10]
            if not actor_cik or not symbol or direction not in {"buy", "sell"}:
                continue
            try:
                date.fromisoformat(day)
            except ValueError:
                continue
            merged.setdefault(f"{actor_cik}|{symbol}", set()).add(f"{day}|{direction}")
        cutoff = et_today() - timedelta(days=self.history_retention_days)
        pruned: dict[str, list[str]] = {}
        for key, values in merged.items():
            kept = sorted(
                value
                for value in values
                if _history_entry_date(value) is not None and _history_entry_date(value) >= cutoff
            )
            if kept:
                pruned[key] = kept
        self.stores.save_history(pruned)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _RefreshDeadline("refresh_deadline_exceeded")
        return remaining

    def _get(self, url: str, *, params: dict | None, deadline: float) -> requests.Response:
        global _LAST_REQUEST_AT
        last_exc: Exception | None = None
        for attempt in range(3):
            remaining = self._remaining(deadline)
            with _RATE_LOCK:
                delay = self.request_interval_s - (time.monotonic() - _LAST_REQUEST_AT)
                if delay > 0:
                    if delay >= remaining:
                        raise _RefreshDeadline("refresh_deadline_exceeded")
                    time.sleep(delay)
                _LAST_REQUEST_AT = time.monotonic()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/json, application/xml, text/plain, */*",
                        "Accept-Encoding": "identity",
                    },
                    timeout=min(self.timeout_s, self._remaining(deadline)),
                )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                    return response
                response.raise_for_status()
            except (requests.RequestException, TimeoutError) as exc:
                last_exc = exc
                if isinstance(exc, requests.HTTPError):
                    code = exc.response.status_code if exc.response is not None else 0
                    if code not in {429, 500, 502, 503, 504}:
                        raise
                if attempt == 2:
                    break
                backoff = min(2**attempt, self._remaining(deadline))
                time.sleep(backoff)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"SEC GET failed without exception: {url}")

    def _listed_map(self, deadline: float) -> dict[str, dict[str, str]]:
        if self.stores.tickers_stale():
            try:
                payload = self._get(
                    SEC_TICKERS_EXCHANGE,
                    params=None,
                    deadline=deadline,
                ).json()
                self.stores.save_tickers(payload)
            except Exception as exc:
                if not self.stores.tickers_cached():
                    raise
                logger.warning("Using stale SEC ticker/exchange cache: %s", exc)
        payload = self.stores.load_tickers()
        fields = payload.get("fields", []) if isinstance(payload, dict) else []
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        try:
            indexes = {name: fields.index(name) for name in ("cik", "ticker", "exchange")}
        except ValueError:
            return {}
        out: dict[str, dict[str, str]] = defaultdict(dict)
        for row in rows:
            try:
                exchange = row[indexes["exchange"]]
                if exchange not in _LISTED_EXCHANGES:
                    continue
                cik = str(int(row[indexes["cik"]]))
                ticker = _symbol(row[indexes["ticker"]])
                if ticker and not ticker.endswith(_NON_EQUITY_SUFFIXES):
                    out[cik][ticker] = str(exchange)
            except (IndexError, TypeError, ValueError):
                continue
        return dict(out)

    def _ciks_for_symbols(
        self,
        listed: dict[str, dict[str, str]],
        symbols: list[str] | None,
    ) -> set[str]:
        """CIKs whose listed tickers include one of ``symbols``."""
        watched = {_symbol(s) for s in (symbols or []) if str(s).strip()}
        if not watched:
            return set()
        out: set[str] = set()
        for cik, tickers in (listed or {}).items():
            if not isinstance(tickers, dict):
                continue
            if any(_symbol(t) in watched for t in tickers):
                out.add(str(cik))
        return out

    def _discover(
        self,
        listed: dict[str, dict[str, str]],
        deadline: float,
        processed: set[str] | None = None,
        priority_ciks: set[str] | None = None,
        stats: dict | None = None,
    ) -> list[dict]:
        """Unread Form 4 candidates, watched names first.

        ``priority_ciks`` does NOT filter and does NOT change when this scan
        stops — it only ORDERS what the scan hands back. Filings on the
        desk's own names are emitted first so the submission downloads that
        follow are spent on them first; everything else follows, because
        external candidate nomination (``max_external_candidates``) reads
        filings on names the desk does not yet watch.

        Watched-name COVERAGE is not this function's responsibility. The
        watched-name drain in ``refresh`` (#539) asks each watched issuer
        for its filing history directly, on its own
        ``watched_drain_deadline_s`` budget, and records read-through per
        issuer. See ``_budget_spent`` for why termination must never again
        be made conditional on the watched bucket filling up.

        ``stats``, when given, is filled with the counts part 3 reports:
        every unread listed candidate seen (``candidates``), the watched
        subset (``watched_candidates``), whether the cap bound
        (``cap_reached``) and whether the refresh deadline truncated the
        scan (``deadline_hit``).
        """
        processed = processed or set()
        watched_ciks = {str(c) for c in (priority_ciks or set())}
        cap = self.max_filings_per_refresh
        priority: dict[str, dict] = {}
        other: dict[str, dict] = {}
        seen: set[str] = set()
        watched_seen: set[str] = set()
        deadline_hit = False
        busiest_day_total = 0
        # ---- EDGAR's own denominator (board item 126) ----------------------
        #
        # `hits.total.value` is EDGAR's count of Form 4s filed on the day
        # being queried. It was read for pagination and discarded, so a
        # silently-broken fetch (HTTP 200, no usable body) produced exactly
        # the same evidence as a genuinely quiet day: zero rows, no error.
        # These structures keep it, per day slice:
        #
        #   `day_total`      day -> EDGAR's own count, ONLY when it parsed
        #                    as an integer. A day missing here is a day
        #                    whose denominator could not be read at all.
        #   `day_rows`       day -> how many rows came back, repeats and
        #                    unusable rows included.
        #   `day_usable`     day -> the DISTINCT accessions this scan could
        #                    actually read. Coverage is counted from this,
        #                    never from `day_rows`, so a repeated page or a
        #                    page of junk lowers the reported fraction
        #                    instead of being counted as coverage.
        #   `coverage_reasons` named, machine-checkable reasons a slice was
        #                    not read to EDGAR's own count. Reported; no
        #                    threshold is derived from any of it.
        day_total: dict[str, int] = {}
        day_rows: dict[str, int] = {}
        day_usable: dict[str, set[str]] = {}
        coverage_reasons: set[str] = set()
        queried_days: set[str] = set()
        exhausted_days: set[str] = set()
        days_in_window = self.lookback_days + 1

        def _budget_spent() -> bool:
            # ONE condition, reachable whether or not watched names were
            # supplied. DO NOT reintroduce a `if watched_ciks: return
            # len(priority) >= cap` branch here.
            #
            # That branch shipped 2026-09-18 (PR #513) and made this scan
            # unterminatable in production. `priority` only ever holds
            # filings from the ~82 watched issuers, who file 13-31 a day
            # [measured: the watched drain's own reads, production log
            # 2026-09-21..23], so `len(priority)` cannot reach a cap of
            # 1,000 and production always supplies watched names. The only
            # remaining terminator was `refresh_deadline_s`, so discovery
            # spent the whole 180 s paginating EDGAR and the read loop below
            # then raised on an already-expired deadline. Market-wide reads
            # went 1,000/run through 2026-09-18 to 0, 31 and 13 on the three
            # runs after it [measured: `processed_filings` in the production
            # log, and 31/13 are the watched drain's, not this pass's].
            #
            # What #539 now guarantees, and why the branch is redundant: the
            # desk's own names have their OWN pass. `refresh` runs a
            # watched-name drain after this one, on its own
            # `watched_drain_deadline_s` budget, asking each watched issuer
            # for its filing history directly and recording read-through per
            # issuer. A watched filing this scan never reaches is read
            # there. Watched-first ORDERING inside this function is kept —
            # `priority` is still emitted ahead of `other` below, so the
            # submission downloads that follow are still spent on the desk's
            # own names first — but watched COVERAGE is no longer this
            # function's job, and must not be bought with its termination.
            #
            # The cap is reachable against real volume: steady-state
            # market-wide inflow is 247 unread listed-issuer Form 4s/day
            # [measured: 21,217 pending / 86 days queried, 2026-09-23 run],
            # and at 0.156 s/filing [measured, see the ledger] a day's worth
            # is 38.5 s inside a 180 s budget.
            return len(other) >= cap

        # Query one day at a time. EFTS caps deep pagination, while 14 days of
        # ownership filings can exceed that cap. Day slices also let repeated
        # refreshes skip the processed head and make progress into a backlog.
        try:
            for days_ago in range(days_in_window):
                filing_date = et_today() - timedelta(days=days_ago)
                day = filing_date.isoformat()
                params = {
                    "forms": "4",
                    "startdt": day,
                    "enddt": day,
                    "from": 0,
                    "size": 100,
                }
                while not _budget_spent():
                    payload = self._get(
                        self.search_url,
                        params=params,
                        deadline=deadline,
                    ).json()
                    # Counted only once a request for this day actually
                    # returned: a day the budget never reached is a day this
                    # scan did not query, and must not count as one it did.
                    queried_days.add(day)
                    day_rows.setdefault(day, 0)
                    day_usable.setdefault(day, set())
                    hits_block = payload.get("hits", {}) if isinstance(payload, dict) else {}
                    hits = hits_block.get("hits", []) if isinstance(hits_block, dict) else []
                    if not isinstance(hits, list):
                        # A 200 whose `hits.hits` is not a list is a body
                        # this code cannot read, not an empty day.
                        hits = []
                        coverage_reasons.add("edgar_hits_unreadable")
                    # EDGAR's own count for this day, read BEFORE the rows
                    # are walked so a body that carries no usable count is
                    # recorded as unreadable rather than silently treated as
                    # a day with nothing on it. `total_value is None` is the
                    # whole point: 0 is a real answer, absent is not.
                    total_value = edgar_total(hits_block)
                    if total_value is None:
                        coverage_reasons.add("edgar_total_unreadable")
                    elif day not in day_total:
                        # The first page's count is the day's count. Later
                        # pages restate it; they must not be able to move it.
                        day_total[day] = total_value
                        if total_value > busiest_day_total:
                            busiest_day_total = total_value
                    elif total_value != day_total[day]:
                        # A later page disagreeing with the first about how
                        # much exists is a source that cannot be paginated
                        # against. Pagination below uses the PINNED first
                        # count, so a shrinking total cannot end the slice
                        # early and unnamed.
                        coverage_reasons.add("edgar_total_changed")
                    pinned_total = day_total.get(day)
                    if not hits:
                        if (pinned_total or 0) > len(day_usable[day]):
                            # EDGAR says there are filings on this day and
                            # handed back none of them. This is the shape a
                            # broken fetch takes, and it used to be
                            # indistinguishable from a quiet day.
                            coverage_reasons.add("edgar_returned_no_hits_for_nonzero_total")
                        break
                    day_rows[day] += len(hits)
                    for hit in hits:
                        source = hit.get("_source", {}) if isinstance(hit, dict) else {}
                        accession = str(source.get("adsh") or "")
                        form = str(source.get("form") or "")
                        # Coverage counts DISTINCT rows this scan could
                        # actually read, not rows it received. Two reasons,
                        # both measured against this code before it shipped:
                        # a row that could not have answered a `forms=4`
                        # query is not coverage of that query, and a cache
                        # or proxy replaying one page for every offset would
                        # otherwise walk to EDGAR's own count and report
                        # complete coverage of filings it never sent.
                        if well_formed_hit(hit):
                            day_usable[day].add(accession)
                        if accession in processed or not ACCESSION_RE.fullmatch(accession) or form not in {"4", "4/A"}:
                            continue
                        ciks: list[str] = []
                        for raw_cik in source.get("ciks", []) or []:
                            try:
                                ciks.append(str(int(raw_cik)))
                            except (TypeError, ValueError):
                                continue
                        listed_ciks = [cik for cik in ciks if cik in listed]
                        if not listed_ciks:
                            continue
                        watched_hit = [c for c in listed_ciks if c in watched_ciks]
                        seen.add(accession)
                        if watched_hit:
                            watched_seen.add(accession)
                        filing = {
                            "accession": accession,
                            "form": form,
                            "cik": (watched_hit or listed_ciks)[-1],
                        }
                        if watched_hit:
                            priority[accession] = filing
                        elif len(other) < cap:
                            other[accession] = filing
                        if _budget_spent():
                            break
                    params["from"] = int(params["from"]) + len(hits)
                    if pinned_total is None:
                        # Nothing says how much is left, so this slice ends
                        # here. Named above; never silently "finished".
                        break
                    if int(params["from"]) >= pinned_total:
                        # Read through EDGAR's own count for this day. This
                        # is the ONLY clean way out of the slice — and the
                        # claim it makes is checked below against how many
                        # of those filings were actually readable.
                        #
                        # Not claimed when the budget ran out inside the
                        # page just walked: the scan stopped reading rows
                        # part-way, so the day was not read through and its
                        # shortfall is the cap's, named as the cap's.
                        if not _budget_spent():
                            exhausted_days.add(day)
                        break
                    if len(hits) < int(params["size"]):
                        # A short page while EDGAR's own count says there is
                        # more: deep pagination was cut off (EFTS caps it),
                        # or the page was truncated. Either way the day is
                        # not read through and the desk must be able to say so.
                        coverage_reasons.add("edgar_page_short_of_total")
                        break
                if _budget_spent():
                    break
        except _RefreshDeadline:
            # A deadline used to discard every filing discovered so far. Hand
            # back the partial, watched-first set instead and let the caller
            # record the truncation — the alternative spends the whole
            # deadline scanning and reads nothing.
            deadline_hit = True

        out = list(priority.values())[:cap]
        out.extend(list(other.values())[: max(0, cap - len(out))])
        if deadline_hit:
            coverage_reasons.add("refresh_deadline_exceeded")
        if len(out) >= cap:
            coverage_reasons.add("scan_cap_reached")
        if len(queried_days) < days_in_window:
            coverage_reasons.add("days_not_queried")
        # A day EDGAR answered with rows, none of which this scan could
        # read. Stated per day rather than per page on purpose: a body
        # dressed up as an answer has to be junk for the whole day to hide
        # here, and slipping one real row in per page no longer buys a clean
        # record — the ratio below counts only readable rows, so it collapses
        # instead. A stray malformed row among good ones lowers the ratio and
        # nothing else, because firing on one oddity would make this seat
        # degraded on any EDGAR hiccup.
        if any(rows and not day_usable.get(day) for day, rows in day_rows.items()):
            coverage_reasons.add("edgar_hits_malformed")
        # A day the scan paged all the way through EDGAR's own count and
        # still could not read that many filings out of it. EXACT, not a
        # cut point: the claim "I read this day through" is checked against
        # the count the day was read against, and nothing here picks a
        # fraction. Only days that ended by exhaustion are held to it — a
        # day the cap or the deadline stopped never claimed to be complete,
        # and its shortfall is named by its own reason instead.
        if any(len(day_usable.get(day) or ()) < (day_total.get(day) or 0) for day in exhausted_days):
            coverage_reasons.add("edgar_rows_unreadable")
        if stats is not None:
            stats["candidates"] = len(seen)
            stats["watched_candidates"] = len(watched_seen)
            stats["cap_reached"] = len(out) >= cap
            stats["deadline_hit"] = deadline_hit
            stats["busiest_day_total"] = busiest_day_total
            # EDGAR's own count of what was there, and how much of it this
            # scan actually walked. `edgar_days_with_total` below
            # `edgar_days_queried` means the denominator itself could not be
            # read on some day — the case this whole record exists for.
            stats["edgar_total"] = sum(day_total.values())
            stats["edgar_enumerated"] = sum(len(v) for v in day_usable.values())
            stats["edgar_rows_received"] = sum(day_rows.values())
            stats["edgar_days_queried"] = len(queried_days)
            stats["edgar_days_in_window"] = days_in_window
            stats["edgar_days_with_total"] = len(day_total)
            stats["edgar_coverage_reasons"] = sorted(coverage_reasons)
        return out

    def _archive_url(self, cik: str, accession: str) -> str:
        return f"{self.archives_url}/{int(cik)}/{accession.replace('-', '')}/{accession}.txt"

    def _submission(self, filing: dict, deadline: float) -> tuple[str, str]:
        accession = filing["accession"]
        url = self._archive_url(filing["cik"], accession)
        cached = self.stores.cached_filing(accession)
        if cached is not None:
            return cached, url
        response = self._get(url, params=None, deadline=deadline)
        self.stores.save_filing(accession, response.content)
        return response.content.decode("utf-8", "replace"), url

    @staticmethod
    def _roles(owner: ET.Element) -> list[str]:
        rel = owner.find("reportingOwnerRelationship")
        if rel is None:
            return []
        roles: list[str] = []
        for element, label in (
            ("isDirector", "director"),
            ("isOfficer", "officer"),
            ("isTenPercentOwner", "ten_percent_owner"),
            ("isOther", "other"),
        ):
            if _bool(_text(rel, element)):
                roles.append(label)
        for value in (_text(rel, "officerTitle"), _text(rel, "otherText")):
            if value:
                roles.append(value)
        return roles

    def _parse_submission(
        self,
        text: str,
        *,
        source_url: str,
        listed: dict[str, dict[str, str]],
    ) -> list[SmartMoneyObservation]:
        accepted_match = _ACCEPTED_RE.search(text)
        xml_match = _XML_RE.search(text)
        if not accepted_match or not xml_match:
            raise ValueError("missing_acceptance_or_ownership_xml")
        accepted_at = datetime.strptime(
            accepted_match.group(1),
            "%Y%m%d%H%M%S",
        ).replace(tzinfo=_ET)
        root = ET.fromstring(xml_match.group(1))
        form = _text(root, "documentType")
        if form not in {"4", "4/A"}:
            raise ValueError("not_form_4")
        try:
            issuer_cik = str(int(_text(root, "issuer/issuerCik")))
        except (TypeError, ValueError):
            raise ValueError("missing_issuer_cik")
        symbol = _symbol(_text(root, "issuer/issuerTradingSymbol"))
        exchange = listed.get(issuer_cik, {}).get(symbol, "")
        if not symbol or not exchange or symbol.endswith(_NON_EQUITY_SUFFIXES):
            return []

        owner_ciks: list[str] = []
        owner_names: list[str] = []
        owner_roles: list[str] = []
        for owner in root.findall("reportingOwner"):
            try:
                owner_cik = str(int(_text(owner, "reportingOwnerId/rptOwnerCik")))
            except (TypeError, ValueError):
                owner_cik = ""
            name = _text(owner, "reportingOwnerId/rptOwnerName")
            if owner_cik:
                owner_ciks.append(owner_cik)
            if name:
                owner_names.append(name)
            for role in self._roles(owner):
                if role not in owner_roles:
                    owner_roles.append(role)
        actor = " / ".join(owner_names) or "Unknown reporting owner"
        accession = source_url.rsplit("/", 1)[-1].removesuffix(".txt")
        is_10b5_1 = _bool(_text(root, "aff10b5One"))

        rows: list[SmartMoneyObservation] = []
        for index, transaction in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
            code = _text(transaction, "transactionCoding/transactionCode").upper()
            if code not in {"P", "S"}:
                continue
            acquired_disposed = _text(
                transaction,
                "transactionAmounts/transactionAcquiredDisposedCode/value",
            ).upper()
            if (code == "P" and acquired_disposed != "A") or (code == "S" and acquired_disposed != "D"):
                logger.warning(
                    "Dropping direction-inconsistent SEC row %s#%d: code=%s A/D=%s",
                    accession,
                    index,
                    code,
                    acquired_disposed,
                )
                continue
            try:
                transaction_date = date.fromisoformat(_text(transaction, "transactionDate/value"))
            except ValueError:
                continue
            shares = _number(
                _text(
                    transaction,
                    "transactionAmounts/transactionShares/value",
                )
            )
            price = _number(
                _text(
                    transaction,
                    "transactionAmounts/transactionPricePerShare/value",
                )
            )
            value = shares * price if shares is not None and price is not None else None
            post_shares = _number(
                _text(
                    transaction,
                    "postTransactionAmounts/sharesOwnedFollowingTransaction/value",
                )
            )
            directness = {
                "D": "direct",
                "I": "indirect",
            }.get(
                _text(
                    transaction,
                    "ownershipNature/directOrIndirectOwnership/value",
                ).upper(),
                "unknown",
            )
            lag_days = max(0, (accepted_at.date() - transaction_date).days)
            age_days = max(0, (et_today() - accepted_at.date()).days)
            freshness = "fresh" if age_days <= 7 else ("delayed" if age_days <= self.lookback_days else "stale")
            rows.append(
                SmartMoneyObservation(
                    symbol=symbol,
                    stream="insider",
                    actor=actor,
                    actor_cik=owner_ciks[0] if owner_ciks else "",
                    actor_roles=owner_roles,
                    joint_owner_ciks=owner_ciks[1:],
                    direction="buy" if code == "P" else "sell",
                    transaction_date=transaction_date,
                    disclosure_date=accepted_at.date(),
                    accepted_at=accepted_at,
                    known_at=accepted_at,
                    source_url=source_url,
                    accession_number=accession,
                    filing_form=form,
                    transaction_code=code,
                    transaction_row=index,
                    security_title=_text(transaction, "securityTitle/value"),
                    shares=shares,
                    price_per_share=price,
                    transaction_value_usd=value,
                    post_transaction_shares=post_shares,
                    ownership_nature=directness,
                    amendment=(form == "4/A"),
                    # Calendar lag spans weekends; only the filing's explicit
                    # timeliness code can truthfully label the transaction late.
                    late_filing=(_text(transaction, "transactionTimeliness/value").upper() == "L"),
                    is_10b5_1=is_10b5_1,
                    listed_exchange=exchange,
                    lag_days=lag_days,
                    disclosure_age_days=age_days,
                    freshness=freshness,
                    economic_role="confirmatory",
                )
            )
        return rows

    def known_accessions(self) -> set[str]:
        """Form 4 accessions already processed or cached. No network."""
        out: set[str] = set()
        manifest = self.stores.load_manifest()
        for raw in (manifest.get("processed_accessions") or []) if isinstance(manifest, dict) else []:
            text = str(raw or "").strip()
            if text:
                out.add(text)
        cached = self.stores.load_observations()
        for row in cached if isinstance(cached, list) else []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("accession_number") or "").strip()
            if text:
                out.add(text)
        return out

    # ---- freshness (question b) and completeness (question a) -------------
    #
    # These are two different questions and the code used to pay the
    # expensive one on every decision tick:
    #
    #   (a) "is there a Form 4 I have not READ?" — a data-completeness fact.
    #       It belongs to the producing step (`refresh`) and to reporting.
    #       Answering it needs the full-text crawl, because "unread" is a
    #       statement about the whole filing stream.
    #   (b) "has anything been FILED on a watched name since my last read?"
    #       — the only question a freshness check needs. It is answered per
    #       watched issuer from that issuer's own filing history, in
    #       O(watched names) plain GETs with no pagination.
    #
    # `form4_freshness` answers (b) and is what a decision tick calls.
    # Question (a) had its own `peek_accessions` path, deleted 2026-09-24
    # (item 159): nothing in `src/` had called it since PR #529 — `refresh`
    # runs its own discovery — so it was dead code kept alive only by tests.

    def _submissions_form4(
        self,
        cik: str,
        deadline: float,
    ) -> list[tuple[str, str]]:
        """(accession, filing_date) for every Form 4/4-A under one CIK.

        One GET, no pagination. Newest first, as SEC serves it. Only the
        `filings.recent` block is read: SEC's own documentation says it holds
        at least a year of filings or the most recent 1,000, whichever is
        more, which covers `lookback_days` at its 365 ceiling. Older
        filings live in the paged `filings.files` references and are
        deliberately NOT followed — a filing older than the lookback window
        is outside what this provider retains at all.
        """
        try:
            numeric = int(str(cik).strip())
        except (TypeError, ValueError):
            return []
        url = f"{self.submissions_url}/CIK{numeric:010d}.json"
        payload = self._get(url, params=None, deadline=deadline).json()
        filings = payload.get("filings", {}) if isinstance(payload, dict) else {}
        recent = filings.get("recent", {}) if isinstance(filings, dict) else {}
        if not isinstance(recent, dict):
            return []
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        accessions = recent.get("accessionNumber", []) or []
        out: list[tuple[str, str]] = []
        # zip() over the parallel arrays, for the same reason
        # `EarningsProvider._get_recent_filings` does: an upstream truncation
        # would otherwise desync them and index access would raise.
        for form, filed, accession in zip(forms, dates, accessions):
            if str(form).strip() not in {"4", "4/A"}:
                continue
            text = str(accession or "").strip()
            if not ACCESSION_RE.fullmatch(text):
                continue
            out.append((text, str(filed or "").strip()[:10]))
        return out

    def recent_filings(
        self,
        symbol: str,
        deadline: float,
        listed: dict | None = None,
    ) -> list[tuple[str, str, str]] | None:
        """(form, filing_date, items) for an issuer's recent filings.

        The universe screen's pending-takeover check
        (`src/universe_screen.py::pending_takeover`). One GET of the issuer's
        `filings.recent` block — at least a year or the last 1,000 filings
        per SEC's own documentation (see `_submissions_form4`). Returns None
        when the symbol maps to no SEC issuer on a listed exchange; raises
        when SEC could not be read.
        """
        wanted = _symbol(symbol)
        if listed is None:  # pass `listed_map()` in when screening many
            listed = self._listed_map(deadline)
        cik = next(
            (c for c, tickers in listed.items() if wanted in tickers),
            None,
        )
        if cik is None:
            return None
        url = f"{self.submissions_url}/CIK{int(cik):010d}.json"
        payload = self._get(url, params=None, deadline=deadline).json()
        filings = payload.get("filings", {}) if isinstance(payload, dict) else {}
        recent = filings.get("recent", {}) if isinstance(filings, dict) else {}
        if not isinstance(recent, dict):
            return []
        forms = recent.get("form", []) or []
        dates = recent.get("filingDate", []) or []
        items = recent.get("items", []) or [""] * len(forms)
        return [
            (str(form or "").strip(), str(filed or "").strip()[:10], str(item or ""))
            for form, filed, item in zip(forms, dates, items)
        ]

    def listed_map(self, deadline: float) -> dict[str, dict[str, str]]:
        """Public read of the cached SEC CIK -> {ticker: exchange} map."""
        return self._listed_map(deadline)

    def watched_form4_index(
        self,
        ciks,
        deadline: float,
    ) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
        """Form 4 history for each watched CIK, plus the CIKs that failed.

        The failure list is returned rather than swallowed. A name whose
        history could not be read is a name this desk cannot make any
        freshness claim about, and every caller here treats that as unknown,
        never as "nothing new".
        """
        index: dict[str, list[tuple[str, str]]] = {}
        failed: list[str] = []
        for cik in sorted({str(c).strip() for c in (ciks or []) if str(c).strip()}):
            try:
                index[cik] = self._submissions_form4(cik, deadline)
            except _RefreshDeadline:
                # Out of budget: every CIK not yet reached is unknown too.
                failed.append(cik)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SEC Form 4 filing history unavailable for CIK %s: %s",
                    cik,
                    exc,
                )
                failed.append(cik)
        return index, failed

    def read_through_date(self) -> str:
        """ET date through which EVERY watched name is read. Report-only.

        Written by `refresh` only when the watched drain left nothing unread
        on any watched issuer. `form4_freshness` no longer reads it: a single
        date could only say "all or nothing", and the per-issuer map below
        is what freshness accepts as coverage.
        """
        manifest = self.stores.load_manifest()
        if not isinstance(manifest, dict):
            return ""
        return str(manifest.get("watched_read_through") or "").strip()[:10]

    def read_through_by_cik(self) -> dict[str, str]:
        """CIK -> ET date of the last pass that left that issuer fully read."""
        manifest = self.stores.load_manifest()
        raw = manifest.get("watched_read_through_by_cik") if isinstance(manifest, dict) else None
        out: dict[str, str] = {}
        for cik, day in raw.items() if isinstance(raw, dict) else []:
            text = str(day or "").strip()[:10]
            if str(cik).strip() and text:
                out[str(cik).strip()] = text
        return out

    def form4_coverage(self) -> dict:
        """How much of the watched set the last pre-market pass read. No network.

        The morning seat reads this to decide whether its answer may be
        called complete. ``known`` is False when no pass has recorded
        coverage at all — which the caller must treat as incomplete, never
        as complete.
        """
        manifest = self.stores.load_manifest()
        blank_edgar = blank_edgar_coverage()
        if not isinstance(manifest, dict) or not manifest.get("coverage_as_of"):
            return {
                "known": False,
                "as_of": "",
                "watched": 0,
                "read_through": 0,
                "unread": [],
                "edgar": blank_edgar,
                "market_wide_blind": False,
                "market_wide_read": 0,
                "market_wide_pending": 0,
            }
        as_of = str(manifest.get("coverage_as_of") or "")[:10]
        recorded = manifest.get("edgar_coverage")
        if not isinstance(recorded, dict):
            recorded = blank_edgar
        elif as_of != et_today().isoformat():
            # A coverage record from an earlier pass describes an earlier
            # fetch. Yesterday's clean read is not a statement about today's,
            # and accepting it would reintroduce this item's defect one day
            # late: nothing fetched today, seat reports clean.
            recorded = {
                **recorded,
                "verified": False,
                "reasons": sorted(
                    {str(r) for r in (recorded.get("reasons") or [])} | {"edgar_coverage_stale"},
                ),
            }
        return {
            "known": True,
            "as_of": str(manifest.get("coverage_as_of") or "")[:10],
            "watched": int(manifest.get("watched_names") or 0),
            "read_through": int(manifest.get("watched_names_read_through") or 0),
            "unread": [str(s) for s in (manifest.get("watched_names_unread") or [])],
            # The market-wide half of coverage, which `unread` above cannot
            # speak for: it counts the desk's OWN names only. Reported ONLY
            # from a record written today, for the same reason the EDGAR
            # record is aged out below — a pass that ran yesterday says
            # nothing about whether today's read anything. A manifest
            # written before this shipped carries neither key and reads as
            # not-blind, which is right: absence of the record is not
            # evidence of a blind pass, and the ordinary `partial` path
            # still covers the day it is first written.
            "market_wide_blind": bool(manifest.get("market_wide_blind") and as_of == et_today().isoformat()),
            "market_wide_read": int(manifest.get("market_wide_read") or 0),
            "market_wide_pending": int(manifest.get("pending_filings") or 0),
            # EDGAR's own denominator, as the last pass recorded it. A
            # manifest written before board item 126 shipped carries no such
            # record, and reads as unverified rather than as complete —
            # never-recorded is not evidence of a clean fetch.
            "edgar": recorded,
        }

    def form4_freshness(self, symbols: list[str] | None = None) -> dict:
        """Is the insider evidence on every watched name current?

        Returns a verdict the caller must read fail-closed:

          ``ok``            every watched name was checked AND every one is
                            read through. False means the desk cannot call
                            the seat current, and must never report it so.
          ``new_filings``   unread accessions on names that ARE read through
                            — necessarily filed after that read, so new.
          ``unread_names``  watched CIKs never read through: their unread
                            filings are backlog the desk has not read, so the
                            seat's answer does not cover them.
          ``unread_filings`` how many unread filings sit on those names.
          ``unchecked``     CIKs whose history could not be read.

        Coverage is per issuer (`watched_read_through_by_cik`). The single
        watermark this replaced excluded any filing dated on or before it as
        "backlog", which had two faults: it was all-or-nothing, so the seat
        could never become current while one issuer was unfinished; and it
        hid every filing made on the watermark's own date AFTER the
        pre-market read — the whole trading day's filings — as backlog. Now
        an issuer the drain left fully read cannot have backlog by
        construction, so ANY unread filing on it inside the window is new.
        """
        relevant = {_symbol(s) for s in (symbols or []) if str(s).strip()}
        verdict = {
            "ok": False,
            "new_filings": [],
            "read_through": "",
            "checked": 0,
            "covered": 0,
            "unread_names": [],
            "unread_filings": 0,
            "unchecked": [],
            "reason": "",
        }
        if not relevant:
            # Nothing watched is not a failure — there is nothing to be
            # stale about, so this answers "nothing new" honestly.
            verdict["ok"] = True
            verdict["reason"] = "no watched names"
            return verdict
        by_cik = self.read_through_by_cik()
        verdict["read_through"] = self.read_through_date()
        if not by_cik:
            verdict["reason"] = (
                "no watched name has ever been confirmed fully read — nothing to measure freshness against"
            )
            return verdict
        try:
            deadline = time.monotonic() + self.refresh_deadline_s
            listed = self._listed_map(deadline)
            ciks = self._ciks_for_symbols(listed, sorted(relevant))
            index, failed = self.watched_form4_index(ciks, deadline)
        except Exception as exc:  # noqa: BLE001
            # FAIL CLOSED, and note the direction deliberately: the old code
            # swallowed this and returned the known set, which the caller
            # read as "nothing new" and REUSED superseded research. A probe
            # that did not run is an unknown, and an unknown loses the seat
            # for THIS TICK only; the next tick re-probes.
            verdict["reason"] = f"freshness probe failed: {type(exc).__name__}: {exc}"
            logger.warning("SEC Form 4 freshness probe failed: %s", exc)
            return verdict
        verdict["checked"] = len(index)
        verdict["unchecked"] = sorted(failed)
        known = self.known_accessions()
        horizon = (et_today() - timedelta(days=self.lookback_days)).isoformat()
        new_filings: set[str] = set()
        unread_names: list[str] = []
        unread_filings = 0
        for cik, rows in index.items():
            unread = [accession for accession, filed in rows if accession not in known and filed and filed >= horizon]
            if cik in by_cik:
                new_filings.update(unread)
            elif not unread:
                # Never recorded, but nothing inside the window is unread —
                # e.g. a name added to the universe since the morning pass.
                # Every filing there has been read, so it is covered.
                continue
            else:
                unread_names.append(cik)
                unread_filings += len(unread)
        verdict["covered"] = len(index) - len(unread_names)
        verdict["new_filings"] = sorted(new_filings)
        verdict["unread_names"] = sorted(unread_names)
        verdict["unread_filings"] = unread_filings
        problems: list[str] = []
        if failed:
            # PARTIAL is still unknown. The one name that errored is exactly
            # the one that might have filed.
            problems.append(f"{len(failed)} watched name(s) could not be checked")
        if unread_names:
            problems.append(
                f"{len(unread_names)} of {len(index)} watched name(s) not yet "
                f"fully read ({unread_filings} filing(s) unread)"
            )
        if problems:
            verdict["reason"] = "; ".join(problems)
            return verdict
        verdict["ok"] = True
        verdict["reason"] = (
            f"{len(new_filings)} new filing(s) on names read through"
            if new_filings
            else "every watched name read through; nothing new"
        )
        return verdict

    def refresh(self, symbols: list[str] | None = None) -> dict:
        """Network refresh with a JSON-safe status/result summary.

        ``symbols`` are the names the desk watches. They do not filter what
        is cached — they decide the ORDER the `max_filings_per_refresh`
        budget is spent in, so a watched filing can only go unread if
        watched names alone exhaust the cap.
        """
        deadline = time.monotonic() + self.refresh_deadline_s
        manifest = self.stores.load_manifest()
        processed = set(manifest.get("processed_accessions", []))
        cached_rows = self.stores.load_observations()
        observations: dict[str, dict] = {}
        for raw in cached_rows if isinstance(cached_rows, list) else []:
            key = f"{raw.get('accession_number', '')}:{raw.get('transaction_row', '')}"
            observations[key] = raw
        new_count = 0
        processed_count = 0
        # The market-wide pass's OWN read count. `processed_count` is shared
        # with the watched-name drain below, which is exactly why the
        # 2026-09-18 discovery regression was invisible: on 2026-09-22 and
        # 2026-09-23 `processed_filings` read 31 and 13, every one of them
        # the drain's, while this pass read nothing at all.
        market_wide_read = 0
        market_wide_ran = False
        watched_processed = 0
        errors: list[str] = []
        discovery: dict = {}
        discovered_count = 0
        # Bound before the try: the watched drain below needs it, and a
        # failed ticker-map fetch must leave it empty rather than unbound.
        listed: dict[str, dict[str, str]] = {}
        try:
            listed = self._listed_map(deadline)
            priority = self._ciks_for_symbols(listed, symbols)
            market_wide_ran = True
            discovered = self._discover(listed, deadline, processed, priority, discovery)
            discovered_count = len(discovered)
            if discovery.get("deadline_hit"):
                errors.append("refresh_deadline_exceeded")
            for filing in discovered:
                accession = filing["accession"]
                if accession in processed:
                    continue
                try:
                    body, source_url = self._submission(filing, deadline)
                    for row in self._parse_submission(
                        body,
                        source_url=source_url,
                        listed=listed,
                    ):
                        key = f"{row.accession_number}:{row.transaction_row}"
                        if key not in observations:
                            new_count += 1
                        observations[key] = row.model_dump(mode="json")
                    processed.add(accession)
                    processed_count += 1
                    market_wide_read += 1
                    if str(filing.get("cik") or "") in priority:
                        watched_processed += 1
                except _RefreshDeadline:
                    raise
                except Exception as exc:
                    logger.warning("SEC Form 4 failed for %s: %s", accession, exc)
                    errors.append(f"{accession}:{type(exc).__name__}")
        except _RefreshDeadline:
            errors.append("refresh_deadline_exceeded")
        except Exception as exc:
            logger.warning("SEC Form 4 refresh failed: %s", exc)
            errors.append(f"refresh:{type(exc).__name__}")

        # ---- watched-name drain ------------------------------------------
        #
        # The market-wide pass above spends a budget sized against
        # market-wide filing volume, so its residue is unbounded by anything
        # the desk controls. This pass is bounded by the desk's OWN names:
        # it asks each watched issuer for its filing history directly and
        # reads whatever is still unread inside the lookback window.
        #
        # Three properties, each the fix for a way this pass used to fail:
        #
        # 1. It has its OWN deadline, started here, after the market-wide
        #    pass has finished with `refresh_deadline_s`. Sharing one 180 s
        #    budget with a pass that runs first and measured ~153 s meant the
        #    drain could never finish (2026-09-18).
        # 2. Completion is recorded PER ISSUER, in `watched_read_through_by_cik`,
        #    the moment that issuer has nothing unread left. The old single
        #    watermark was all-or-nothing, so one unfinished issuer threw away
        #    the work done on every other one and the next morning started
        #    from the same place.
        # 3. Issuers are read fewest-unread first. The per-issuer record makes
        #    "how many issuers are fully read" the unit of progress, and
        #    reading the smallest residues first maximises it for any budget.
        #
        # `form4_freshness` treats an issuer as current ONLY if it appears in
        # that map, and treats EVERY unread filing on such an issuer as new.
        drain_residue: int | None = None
        drain_read = 0
        drain_unchecked: list[str] = []
        drain_ran = False
        drain_deadline_hit = False
        watched_ciks: set[str] = set()
        read_through_by_cik = self.read_through_by_cik()
        residue_by_cik: dict[str, int] = {}
        today_iso = et_today().isoformat()
        try:
            if symbols and isinstance(listed, dict) and listed:
                drain_ran = True
                drain_deadline = time.monotonic() + self.watched_drain_deadline_s
                watched_ciks = self._ciks_for_symbols(listed, symbols)
                index, drain_unchecked = self.watched_form4_index(
                    watched_ciks,
                    drain_deadline,
                )
                horizon = (et_today() - timedelta(days=self.lookback_days)).isoformat()
                outstanding: dict[str, list[dict]] = {}
                for cik, rows in index.items():
                    outstanding[cik] = []
                    for accession, filed in rows:
                        if accession in processed:
                            continue
                        if not filed or filed < horizon:
                            # Outside the retention window: this provider
                            # would prune the observation anyway, so an
                            # unread filing older than the lookback is not
                            # residue, it is out of scope by design.
                            continue
                        outstanding[cik].append(
                            {"accession": accession, "form": "4", "cik": cik},
                        )
                    residue_by_cik[cik] = len(outstanding[cik])
                order = sorted(outstanding, key=lambda c: (len(outstanding[c]), c))
                try:
                    for cik in order:
                        issuer_failed = False
                        for filing in outstanding[cik]:
                            try:
                                body, source_url = self._submission(
                                    filing,
                                    drain_deadline,
                                )
                                for row in self._parse_submission(
                                    body,
                                    source_url=source_url,
                                    listed=listed,
                                ):
                                    key = f"{row.accession_number}:{row.transaction_row}"
                                    if key not in observations:
                                        new_count += 1
                                    observations[key] = row.model_dump(mode="json")
                                processed.add(filing["accession"])
                                processed_count += 1
                                watched_processed += 1
                                drain_read += 1
                                residue_by_cik[cik] -= 1
                            except _RefreshDeadline:
                                raise
                            except Exception as exc:  # noqa: BLE001
                                issuer_failed = True
                                logger.warning(
                                    "SEC Form 4 watched drain failed for %s: %s",
                                    filing["accession"],
                                    exc,
                                )
                                errors.append(
                                    f"drain:{filing['accession']}:{type(exc).__name__}",
                                )
                        if not issuer_failed and residue_by_cik[cik] == 0:
                            # This issuer, and only this issuer, is now read
                            # through. Recorded immediately so a deadline on
                            # a LATER issuer cannot take it back.
                            read_through_by_cik[cik] = today_iso
                except _RefreshDeadline:
                    drain_deadline_hit = True
                    errors.append("watched_drain_deadline_exceeded")
                drain_residue = sum(residue_by_cik.values())
        except _RefreshDeadline:
            # Only the filing-history index can land here — the read loop
            # above catches its own. Every issuer not yet indexed is unknown.
            drain_deadline_hit = True
            errors.append("watched_drain_deadline_exceeded")
            drain_residue = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("SEC Form 4 watched drain failed: %s", exc)
            errors.append(f"drain:{type(exc).__name__}")
            drain_residue = None
        cutoff = et_today() - timedelta(
            days=self.lookback_days + self.cluster_window_days,
        )
        kept: list[dict] = []
        for raw in observations.values():
            try:
                if date.fromisoformat(str(raw.get("disclosure_date"))[:10]) >= cutoff:
                    kept.append(raw)
            except (TypeError, ValueError):
                continue
        # Backlog depth: unread listed Form 4 candidates this pass saw and did
        # NOT read, and the watched-name subset of them. `refresh` only runs
        # pre-market, so a residue cannot drain until the next morning — and
        # until 2026-09-18 nothing measured whether it was draining at all.
        # `candidates` is what the scan saw; when watched names are named the
        # scan covers the whole lookback window, so it is the real residue.
        # When `_discover` is stubbed the counts are absent and the residue
        # falls back to what was discovered but not read.
        # EDGAR's own count of what was filed, against what this scan walked
        # (board item 126). Computed from the DISCOVERY stats, before the
        # recorded DURING the fetch, never from the pruned cache above — a
        # coverage figure taken after this refresh's own retention prune
        # measures the prune, not the fetch.
        edgar = edgar_coverage(discovery)
        seen_candidates = int(discovery.get("candidates", discovered_count) or 0)
        pending_filings = max(0, seen_candidates - processed_count)
        # ---- the market-wide pass read NOTHING (the 2026-09-18 regression) --
        #
        # This is the alarm that did not exist. Between 2026-09-18 and
        # 2026-09-23 `_discover` could not terminate on its own cap, spent
        # the whole refresh deadline paginating, and the read loop raised on
        # an expired deadline — so market-wide insider coverage fell from
        # ~180-210 distinct symbols a day to 2-8 and the backlog grew
        # 10,229 -> 16,782 -> 21,217 [measured: production log and the Form 4
        # manifest, 2026-09-21..23]. Every health signal stayed green: the
        # seat reported `partial`, which is what it reports on ANY ordinary
        # residue, and the watched drain went on reporting 82/82.
        #
        # So the condition is stated as its own fact, separate from residue
        # depth: this pass ran, read zero, and there was something to read.
        # No threshold and no new constant — zero is zero, and the backlog
        # is the pass's own `pending_filings`. A pass that read zero because
        # there was genuinely nothing unread is NOT this, and does not fire.
        market_wide_blind = bool(market_wide_ran and market_wide_read == 0 and pending_filings > 0)
        if market_wide_blind:
            logger.error(
                "SEC Form 4 market-wide pass read ZERO filings with %s unread "
                "candidate(s) outstanding — external insider coverage is "
                "blind this session (discovery: cap_reached=%s deadline_hit=%s; "
                "the watched-name drain read %s separately)",
                pending_filings,
                bool(discovery.get("cap_reached", False)),
                bool(discovery.get("deadline_hit", False)),
                drain_read,
            )
        watched_pending = max(
            0,
            int(discovery.get("watched_candidates", 0) or 0) - watched_processed,
        )
        # The drain is the authoritative watched-residue number ONLY when it
        # actually reached every watched name: it then asked each issuer
        # directly instead of inferring the residue from a market-wide scan
        # the cap may have truncated. A drain that could not check some names
        # must never LOWER the reported residue — "I checked nothing, so
        # nothing is outstanding" is the silent-degradation shape this whole
        # change exists to remove, and it is how a stub that answers no
        # question at all would otherwise report a clean desk.
        if drain_ran and drain_residue is not None and not drain_unchecked:
            watched_pending = drain_residue
        elif drain_ran and drain_residue is not None:
            watched_pending = max(watched_pending, drain_residue)
        # Per-issuer coverage as of the end of this pass. An issuer is
        # CURRENT only if it is in the read-through map AND this pass
        # indexed it AND nothing on it is left unread. An issuer read through
        # on an earlier morning that has since gained filings this pass could
        # not finish is not current — `form4_freshness` will report those
        # filings as new, which is what they are.
        watched_symbols = {_symbol(s) for s in (symbols or []) if str(s).strip()}
        current_ciks = sorted(
            cik
            for cik in watched_ciks
            if cik in read_through_by_cik and cik in residue_by_cik and residue_by_cik[cik] == 0
        )
        unread_symbols: list[str] = []
        for cik in sorted(watched_ciks):
            if cik in current_ciks:
                continue
            tickers = listed.get(cik) if isinstance(listed, dict) else None
            names = sorted(_symbol(t) for t in (tickers or {}) if _symbol(t) in watched_symbols) or [cik]
            unread_symbols.append(names[0])
        # The single date is kept for the pre-open check and for anything
        # that reads the old key. It is a CLAIM that EVERY watched issuer is
        # read through, so it only advances when that is true; freshness no
        # longer reads it — it reads the per-issuer map.
        watermark = str(
            (self.stores.load_manifest() or {}).get(
                "watched_read_through",
                "",
            )
            or "",
        ).strip()[:10]
        drain_clean = bool(
            drain_ran
            and drain_residue == 0
            and not drain_unchecked
            and not drain_deadline_hit
            and len(current_ciks) == len(watched_ciks)
            and not any(e.startswith("drain") for e in errors)
        )
        if drain_clean:
            watermark = today_iso
        with self._cache_lock:
            # History is recorded from every row seen this refresh, including
            # those the lookback prune is about to drop — that prune is what
            # makes a separate long-horizon index necessary.
            try:
                self._record_history(list(observations.values()))
            except OSError as exc:
                logger.warning("Insider history index write failed: %s", exc)
            self.stores.save_observations(kept)
            self.stores.save_manifest(
                {
                    "processed_accessions": sorted(processed),
                    "last_refresh_at": et_now().isoformat(),
                    # Durable record of the backlog, so successive mornings can
                    # be compared without re-crawling EDGAR.
                    "pending_filings": pending_filings,
                    "watched_pending_filings": watched_pending,
                    # The market-wide pass's own read count and the blind flag
                    # derived from it. Persisted for the same reason the EDGAR
                    # coverage record is: the morning seat reads this without
                    # the network, through `form4_coverage`.
                    "market_wide_read": market_wide_read,
                    "market_wide_blind": market_wide_blind,
                    "discovery_cap_reached": bool(discovery.get("cap_reached", False)),
                    # Date through which EVERY watched name is read. Advanced
                    # only by a clean drain; reported, no longer read by freshness.
                    "watched_read_through": watermark,
                    # Per issuer (CIK -> ET date): the last pre-market pass that
                    # left nothing unread on that issuer. The ONLY thing
                    # `form4_freshness` accepts as coverage. Merged across runs,
                    # so progress on one issuer is never lost to another.
                    "watched_read_through_by_cik": dict(sorted(read_through_by_cik.items())),
                    # Coverage as of this pass, for the morning seat status and
                    # the pre-open check, which must not need the network.
                    "coverage_as_of": today_iso if drain_ran else "",
                    "watched_names": len(watched_ciks),
                    "watched_names_read_through": len(current_ciks),
                    "watched_names_unread": unread_symbols,
                    # EDGAR's own denominator for this pass. Persisted so the
                    # morning seat can read it without the network, exactly as
                    # it reads the watched-name coverage above.
                    "edgar_coverage": edgar,
                }
            )
        error = None
        if errors:
            error = ("provider_partial_error" if kept else "provider_error") + ":" + ",".join(errors[:20])
        return {
            "status": ("provider_error" if error and not kept else "partial" if error else "ok"),
            "new_observations": new_count,
            "processed_filings": processed_count,
            "discovered_filings": discovered_count,
            "pending_filings": pending_filings,
            "watched_pending_filings": watched_pending,
            # How much the MARKET-WIDE pass read, and whether it read
            # nothing at all while unread candidates were outstanding.
            # `processed_filings` above cannot answer either question: the
            # watched drain shares it.
            "market_wide_read": market_wide_read,
            "market_wide_blind": market_wide_blind,
            "discovery_cap_reached": bool(discovery.get("cap_reached", False)),
            "cached_observations": len(kept),
            # The drain's own outcome, for the pre-open check. `read_through`
            # not equal to today is the single fact that tells the desk,
            # BEFORE the open, that every intraday tick will refuse today.
            "watched_drain_ran": drain_ran,
            "watched_drain_read": drain_read,
            "watched_unchecked_names": sorted(drain_unchecked),
            "watched_drain_deadline_hit": drain_deadline_hit,
            "watched_read_through": watermark,
            "watched_names": len(watched_ciks),
            "watched_names_read_through": len(current_ciks),
            "watched_names_unread": unread_symbols,
            # What EDGAR said was there, and how much of it this pass read.
            # `verified: false` is the fetch saying it cannot account for
            # itself — the state that used to be reported as a quiet day.
            "edgar_coverage": edgar,
            "error": error,
        }

    def _merged_history(self, raw_rows) -> InsiderHistory:
        """Long-horizon index plus the trades in the current cache window."""
        merged = self._load_history().as_mapping()
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            actor_cik = str(raw.get("actor_cik") or "").strip()
            symbol = str(raw.get("symbol") or "").strip().upper()
            direction = str(raw.get("direction") or "").strip()
            if not actor_cik or not symbol or direction not in {"buy", "sell"}:
                continue
            try:
                day = date.fromisoformat(str(raw.get("transaction_date"))[:10])
            except (TypeError, ValueError):
                continue
            entry = InsiderPriorTrade(transaction_date=day, direction=direction)
            bucket = merged.setdefault((actor_cik, symbol), [])
            if entry not in bucket:
                bucket.append(entry)
        return InsiderHistory(merged)

    # Kept as a thin alias: several tests and call sites still spell this
    # `provider._observation_key(...)`. The real implementation now lives in
    # `src.data.smart_money_cluster` so `CongressionalTradingProvider` can
    # share it exactly rather than growing its own copy.
    _observation_key = staticmethod(observation_key)

    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]:
        """Cache-only broad fetch; ``symbols`` marks core but does not filter."""
        core = {_symbol(s) for s in symbols if str(s).strip()}
        raw_rows = self.stores.load_observations()
        # The long-horizon index carries the three-year calendar history; the
        # current cache window contributes this refresh's own trades so a
        # cadence inside the window is still visible on the first ever run.
        history = self._merged_history(raw_rows)
        parsed: list[SmartMoneyObservation] = []
        invalid = 0
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            try:
                item = SmartMoneyObservation(**raw)
            except Exception:
                invalid += 1
                continue
            if item.stream != "insider" or item.disclosure_age_days > self.lookback_days:
                continue
            verdict = classify_transaction(item, history, self._signal_thresholds)
            fraction, band = holdings_fraction(item)
            item = item.model_copy(
                update={
                    "signal_class": verdict.label,
                    "signal_class_reason": verdict.reason,
                    "signal_class_detail": verdict.detail,
                    "signal_weight": verdict.weight,
                    "holdings_fraction": fraction,
                    "holdings_fraction_band": band,
                }
            )
            age_days = max(0, (et_today() - item.disclosure_date).days)
            freshness = "fresh" if age_days <= 7 else ("delayed" if age_days <= self.lookback_days else "stale")
            if age_days > self.lookback_days:
                continue
            # Board item 52: no published study supports single-transaction
            # dollar SIZE as an insider-buy predictor (the closest, Cziraki &
            # Gider 2019, finds size inversely related), so size gates
            # nothing here. The two flat cutoffs (`min_transaction_value_usd`,
            # `external_min_transaction_value_usd`) that used to sit in this
            # admission test and in the `cluster_survivors` call below were
            # deleted rather than re-derived.
            admission = (
                item.symbol not in core
                and not item.amendment
                and item.direction == "buy"
                and item.transaction_code == "P"
                # A routine purchase has no predictive power (Cohen/Malloy/
                # Pomorski); it must never be the reason a symbol enters the
                # trading surface. Strictly narrows the existing gate.
                and verdict.label != "routine"
                and item.transaction_value_usd is not None
                and item.freshness != "stale"
            )
            parsed.append(
                item.model_copy(
                    update={
                        "in_core_universe": item.symbol in core,
                        "in_trading_universe": item.symbol in core,
                        "admission_eligible": admission,
                        "transient_admission_eligible": admission,
                        "economic_role": "actionable" if admission else "confirmatory",
                        "disclosure_age_days": age_days,
                        "freshness": freshness,
                    }
                )
            )

        # The research-defined purchase cluster (board item 124) is computed
        # over EVERY parsed row, before the materiality filter and the
        # observation cap, then stamped on each row of that symbol — so it
        # reaches the seat whichever of the symbol's rows survive. Configured
        # universe only; it changes neither admission nor sort order.
        clusters = insider_purchase_clusters(
            parsed,
            universe=core,
            today=et_today(),
        )
        if clusters:
            parsed = [
                item.model_copy(
                    update={
                        "purchase_cluster": clusters[item.symbol],
                    }
                )
                if item.symbol in clusters
                else item
                for item in parsed
            ]

        survivors = cluster_survivors(
            parsed,
            # Board item 52: no dollar floor gates row retention any more —
            # `cluster_survivors` still keeps its generic materiality
            # parameter (shared with `CongressionalTradingProvider`, which
            # keeps its own separate, unaffected floor), but this caller
            # passes zero so every parsed row clears it on size alone.
            threshold_fn=lambda symbol: 0.0,
            cluster_window_days=self.cluster_window_days,
            min_cluster_owners=self.min_cluster_owners,
        )

        ordered = sorted(
            survivors.values(),
            key=lambda item: (
                not item.transient_admission_eligible,
                not item.in_core_universe,
                # Routine rows stay visible (the operator still sees them and
                # their reason) but sort last, so they are the first cut when
                # ``max_observations`` binds and a real signal is competing.
                -item.signal_weight,
                -(item.transaction_value_usd or 0),
                -(item.accepted_at.timestamp() if item.accepted_at else 0),
            ),
        )
        # Board item 124: reserve a slot for any genuinely clustered symbol
        # that the dollar-value sort above would otherwise drop entirely from
        # `max_observations` because of unrelated, higher-dollar buys on
        # OTHER symbols. This narrowly displaces individual overflow rows; it
        # never reorders the main list.
        final = reserve_cluster_symbols(
            ordered,
            set(clusters),
            max_observations=self.max_observations,
            max_reserved_slots=MAX_CLUSTER_RESERVED_SLOTS,
        )
        self.last_cluster_census = cluster_reservation_census(
            ordered,
            final,
            clusters,
            max_observations=self.max_observations,
            max_reserved_slots=MAX_CLUSTER_RESERVED_SLOTS,
        )
        self.last_sale_census = _sale_census(parsed)
        error = f"cache_partial_error:{invalid}_invalid_rows" if invalid else None
        return final, error
