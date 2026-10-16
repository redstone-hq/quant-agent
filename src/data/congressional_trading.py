"""Congress (House + Senate) trading-disclosure smart-money provider.

Two independent, free, credentialless sources are cross-checked against
each other, matching the exact fail-open posture `SECForm4Provider` already
uses for SEC Form 4 (`src/data/smart_money.py`): a source going unreachable,
timing out or returning malformed data must never block a refresh or crash
the run, and this stream never gates a trade on its own (see
`SmartMoneyFinding`'s "conservative congressional contract" in
`src/models.py`, unchanged by this file).

Sources
-------
Primary   kadoa-org/congress-trading-monitor (GitHub, MIT licensed). Static
          JSON, no auth, no rate limit. Covers House Clerk PTRs, Senate eFD
          and OGE executive-branch filings, and carries a real
          ``filing_date`` per trade.
Secondary congresswatch.us (OpenSourcePatents LLC). No auth, ~8,000 records,
          House + Senate. Carries the official PTR link for provenance, but
          its live schema has NO filing/disclosure-date field at all (see
          `_normalize_congresswatch` below), and at least one observed
          record has a future-dated transaction — every date is sanity-
          checked (`_date_verdict`) before use, dropping rather than
          silently repairing an implausible one.

Reading incrementally
---------------------
Neither source can be asked for only what is new; both answer "unchanged"
to a conditional request. So a file is downloaded only when it changed, and
each row in it is processed once, ever. The evidence, and what is persisted
under ``data_dir``, are in the block above `_SOURCES` and on the class.

Both are single-operator, young projects with no track record: treat as
best-effort, never load-bearing, exactly like the free Form 4 discovery
path this seat already relies on.

Dedup and disagreement
-----------------------
The same real trade can appear in both feeds. Rows are grouped by
``(ticker, normalized filer name, transaction_date)`` — a "rough amount"
match is deliberately not part of the group key, because a genuine
disagreement about the dollar bracket is exactly the case this cross-check
exists to surface, not to hide by splitting into two rows. Within a group:

* one source only -> ``cross_source_agreement="single_source"``.
* both sources, same direction and overlapping amount bracket ->
  ``"agreement"``.
* both sources, but direction or amount bracket disagree -> ``"discrepancy"``,
  with `cross_source_note` stating what disagreed. The row is still kept
  (never dropped for disagreeing) and the analyst sees the flag.

Cluster/materiality reduction reuses the exact same window and helper as
SEC Form 4 (`src.data.smart_money_cluster.cluster_survivors`) — see that
module's docstring for why this is a shared function rather than two
independent implementations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Literal

import requests

from src.data.smart_money import DEFAULT_USER_AGENT, SmartMoneySource, _symbol
from src.data.smart_money_cluster import cluster_survivors, cut_record
from src.models import SmartMoneyObservation
from src.util.time import et_now, et_today

logger = logging.getLogger(__name__)

KADOA_TRADES_URL = "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"
CONGRESSWATCH_TRADES_URL = "https://congresswatch.us/data/trades.json"

_AMOUNT_RE = re.compile(r"\$?([\d,]+)")
_NAME_PREFIXES = ("rep.", "rep", "sen.", "sen", "hon.", "hon", "dr.", "dr", "mr.", "mr", "ms.", "ms", "mrs.", "mrs")
# Any transaction more than this many years old is treated as implausible
# junk rather than real history — the STOCK Act (2012) is the practical
# floor for electronic congressional disclosure data of this kind.
_MAX_PLAUSIBLE_AGE_YEARS = 20


def _normalize_actor_name(raw: str) -> str:
    """Fold naming variants ("Rep. Kevin Hern" / "Kevin Hern") to one key."""
    cleaned = re.sub(r"[.,]", "", str(raw or "").strip().casefold())
    parts = cleaned.split()
    while parts and parts[0] in {p.rstrip(".") for p in _NAME_PREFIXES}:
        parts = parts[1:]
    return " ".join(parts)


# Real STOCK Act transaction-type values, as they actually appear across the
# two feeds and the systems they derive from. Verified 2026-09-04 against the
# House Ethics Committee's PTR instructions and the Senate Select Committee on
# Ethics' PTR instructions, which define exactly three reportable transaction
# kinds — purchase, sale, exchange — and the House PTR form's own short codes:
#
#   P            Purchase
#   S            Sale (full)
#   S (partial)  Partial sale (only part of a holding sold)
#   E            Exchange (rare; e.g. share swap in a merger)
#
# Senate eFD and the House Clerk's own web export render the same three kinds
# as full words, and the widely-mirrored House-Clerk-derived JSON schema both
# our sources ultimately descend from uses the snake_case forms
# ``purchase`` / ``sale_full`` / ``sale_partial`` / ``exchange``.
#
# This is an EXPLICIT allowlist on purpose. The previous implementation
# prefix-matched full words only, so a row carrying the form's short code fell
# through to "unknown" and was silently lost — for a feed whose entire point is
# buy/sell direction, that is real data loss, not a cosmetic gap. It is
# deliberately NOT a loose single-letter prefix test either: a bare
# ``startswith("s")`` would wrongly read "Stock Split" or "Stock Dividend" as a
# sale. Short codes match only as an exact whole token.
_BUY_TYPES = frozenset({"p", "purchase", "purchased", "buy"})
_SELL_TYPES = frozenset(
    {
        "s",
        "s (partial)",
        "s (full)",
        "s(partial)",
        "s(full)",
        "sale",
        "sold",
        "sell",
        "sale (full)",
        "sale (partial)",
        "sale_full",
        "sale_partial",
        "sale full",
        "sale partial",
        "partial sale",
    }
)
_EXCHANGE_TYPES = frozenset({"e", "exchange", "exchanged"})

#: Distinct raw transaction-type values that matched nothing above. Populated
#: (and warned about, once per distinct value) by `_direction` so a future
#: unrecognized upstream format is VISIBLE rather than silently swallowed as
#: "unknown". Tests clear this; production only ever reads it.
_UNRECOGNIZED_TRANSACTION_TYPES: set[str] = set()
_UNRECOGNIZED_LOCK = threading.Lock()


def _direction(transaction_type: str) -> Literal["buy", "sell", "exchange", "unknown"]:
    """Map a raw STOCK Act transaction-type value to a trade direction.

    Handles both the full-word forms and the House PTR form's short codes
    (``P``/``S``/``S (partial)``/``E``). Anything that still matches nothing is
    returned as "unknown" AND recorded/logged, never dropped quietly.
    """
    raw = str(transaction_type or "").strip()
    # Collapse internal whitespace so "S  (partial)" and "S (Partial)" fold to
    # the same key as the canonical form.
    label = " ".join(raw.lower().split())
    if not label:
        return "unknown"
    if label in _BUY_TYPES:
        return "buy"
    if label in _SELL_TYPES:
        return "sell"
    if label in _EXCHANGE_TYPES:
        return "exchange"
    # Full-word prefix fallback, preserved from the original implementation so
    # trailing qualifiers we have not enumerated (e.g. "purchase (partial)")
    # still resolve. Only ever applied to whole words, never to a short code.
    if label.startswith("purchase"):
        return "buy"
    if label.startswith("sale"):
        return "sell"
    if label.startswith("exchange"):
        return "exchange"
    with _UNRECOGNIZED_LOCK:
        first_time = raw not in _UNRECOGNIZED_TRANSACTION_TYPES
        _UNRECOGNIZED_TRANSACTION_TYPES.add(raw)
    if first_time:
        logger.warning(
            "congressional_trading: unrecognized transaction_type %r — row "
            "kept with direction='unknown'. If this is a real STOCK Act "
            "transaction code, add it to _BUY_TYPES/_SELL_TYPES/"
            "_EXCHANGE_TYPES in src/data/congressional_trading.py.",
            raw,
        )
    return "unknown"


def _amount_bracket(low, high, label: str) -> tuple[float | None, float | None]:
    """Prefer explicit low/high fields (kadoa); else parse the label string
    (both sources use identical STOCK Act bracket text, e.g.
    "$1,001 - $15,000")."""
    try:
        if low is not None:
            return float(low), (float(high) if high is not None else None)
    except (TypeError, ValueError):
        pass
    numbers = _AMOUNT_RE.findall(str(label or ""))
    if not numbers:
        return None, None
    parsed = [float(n.replace(",", "")) for n in numbers]
    lo = parsed[0]
    hi = parsed[1] if len(parsed) > 1 else None
    return lo, hi


def _date_verdict(raw) -> tuple[date | None, str | None]:
    """Parse one disclosure date, or say why it was refused.

    Reject (never repair) an implausible date rather than silently fabricating
    a corrected one — congresswatch.us carries at least one record with a
    transaction dated months in the future (SONY, 2026-12-26, still in the
    live file on 2026-09-19). Reasons: ``future_dated`` or ``bad_date``
    (missing, unparseable, or older than any electronic STOCK Act record).
    """
    try:
        parsed = date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None, "bad_date"
    today = et_today()
    if parsed > today:
        return None, "future_dated"
    if parsed < today - timedelta(days=365 * _MAX_PLAUSIBLE_AGE_YEARS):
        return None, "bad_date"
    return parsed, None


def _sane_transaction_date(raw: str) -> date | None:
    """The parsed date, or None when `_date_verdict` refuses it."""
    return _date_verdict(raw)[0]


def _brackets_overlap(a: tuple[float | None, float | None], b: tuple[float | None, float | None]) -> bool:
    a_lo, a_hi = a
    b_lo, b_hi = b
    if a_lo is None or b_lo is None:
        return False
    a_hi = a_hi if a_hi is not None else a_lo
    b_hi = b_hi if b_hi is not None else b_lo
    return a_lo <= b_hi and b_lo <= a_hi


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Incremental reading — what each source offers, with the evidence.
# ---------------------------------------------------------------------------
#
# Neither source can be asked for "only records newer than X". Checked live,
# read-only, 2026-09-19:
#
#   kadoa          A static file on GitHub. The repository's data directory
#                  (GitHub contents API) holds only whole-file JSON
#                  (trades.json, filers.json, tickers.json, ...) and per-filer /
#                  per-ticker folders — nothing per date, no query interface.
#                  trades.json is the NEWEST 5,000 rows by filing date
#                  (filing dates 2026-07-01..2026-09-17 on 2026-09-19), so
#                  rows older than that slice fall out of the file entirely.
#                  Every row carries a stable `id`.
#   congresswatch  A static file on Vercel. `/api`, `/api/trades` and
#                  `/api/trades?since=` all 404; `trades.json?since=2026-09-01`
#                  returns the identical 8,231 rows. Rows carry NO id and NO
#                  filing date — only the transaction date, and new
#                  disclosures arrive with transaction dates weeks in the past
#                  (kadoa's real filing dates: median lag 60 days over 3,147
#                  rows, 2026-09-19), so no date on the row can serve as a
#                  skip-watermark.
#
# What both DO honour is an HTTP conditional request: sent back its own
# `ETag` (If-None-Match) or `Last-Modified` (If-Modified-Since), each
# answered `304 Not Modified` with zero bytes on 2026-09-19. So:
#
#   1. FETCH only when the file changed (conditional GET, both sources). An
#      unchanged morning downloads and processes nothing.
#   2. When it did change, the whole file comes down (it is one file; that
#      is all either source offers — congresswatch ~250 KB compressed; both
#      together under a second, measured 2026-09-19) but
#      each row is PROCESSED at most once: every row is keyed by a fingerprint
#      of the fields this module reads (plus kadoa's own `id`), the keys
#      already processed are persisted, and only unseen keys are parsed,
#      matched and turned into observations. A row dropped for a reason
#      (future-dated, no ticker, ...) is also remembered, so it is not
#      re-judged every day either.
#
# The per-source watermark recorded alongside is the newest date processed:
# kadoa's newest FILING date, congresswatch's newest TRANSACTION date. It is
# reported, never used to skip — the processed-key set is what skips, because
# a late filing can carry an older date than one already read.

_SOURCES: tuple[str, ...] = ("kadoa", "congresswatch")

# The fields each source is fingerprinted on: exactly what this module reads,
# and nothing that changes without the disclosure changing. kadoa rows also
# carry return columns (`ret_since`, `excess_since`, `ret_30d`, `ret_1y`) that
# are recomputed as prices move; fingerprinting the whole row would make every
# row look new every morning — the very re-reading this exists to stop.
_FINGERPRINT_FIELDS: dict[str, tuple[str, ...]] = {
    "kadoa": (
        "id",
        "ticker",
        "transaction_date",
        "filing_date",
        "filer_name",
        "filer_id",
        "transaction_type",
        "amount_range_low",
        "amount_range_high",
        "amount_range_label",
        "doc_url",
        "chamber",
    ),
    "congresswatch": (
        "ticker",
        "transaction_date",
        "member_name",
        "bioguide_id",
        "type",
        "amount",
        "ptr_link",
        "chamber",
        "owner",
        "asset_description",
    ),
}

_WATERMARK_FIELD = {"kadoa": "disclosure_date", "congresswatch": "transaction_date"}

#: Every reason a row can be refused, in the order the log line prints them.
_DROP_REASONS: tuple[str, ...] = (
    "future_dated",
    "bad_date",
    "no_ticker",
    "no_filer",
    "outside_lookback",
    "malformed_row",
)


def _disclosure_keys(source: str, rows: list) -> list[str]:
    """One stable key per raw row: a fingerprint of the fields read, plus the
    row's occurrence number among identical rows in the same file.

    congresswatch carries genuinely identical rows (84 fingerprints appear
    more than once, 2026-09-19) — the occurrence number keeps them distinct
    without inventing an id the source does not have.
    """
    fields = _FINGERPRINT_FIELDS[source]
    seen: Counter[str] = Counter()
    keys: list[str] = []
    for raw in rows:
        values = [raw.get(f) for f in fields] if isinstance(raw, dict) else raw
        digest = hashlib.sha1(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()
        keys.append(f"{digest}#{seen[digest]}")
        seen[digest] += 1
    return keys


def _group_key(row: dict) -> str:
    """Same real trade across both feeds: ticker, filer, transaction date."""
    return f"{row['symbol']}|{row['actor_key']}|{str(row['transaction_date'])[:10]}"


def _header(response, name: str) -> str | None:
    value = getattr(response, "headers", {}).get(name) if hasattr(response, "headers") else None
    return value if isinstance(value, str) and value else None


def _hours_since(then: str, now: datetime) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(then))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=now.tzinfo)
    return round((now - stamp) / timedelta(hours=1), 1)


class _Unreadable(ValueError):
    """The source answered, but not with a list of trades."""


class CongressionalTradingProvider:
    """Credentialless, dual-sourced Congress trading-disclosure provider.

    Implements the same `SmartMoneySource` protocol as `SECForm4Provider`
    (``refresh()`` does the network work and caches; ``fetch()`` is
    cache-only and applies the shared materiality/cluster reduction).

    Files under ``data_dir`` (same convention as the Form 4 cache):

    manifest.json      per source: HTTP validators, last attempt / success,
                       outcome, watermark, processed-key set; plus the last
                       refresh's counts. Written LAST, so a crash mid-refresh
                       only ever re-processes, never skips.
    disclosures.json   every kept normalized row, per source, by key.
    observations.json  merged (cross-source) rows, what ``fetch`` reads.
    """

    def __init__(
        self,
        *,
        kadoa_url: str = KADOA_TRADES_URL,
        congresswatch_url: str = CONGRESSWATCH_TRADES_URL,
        data_dir: str = "data/smart_money/congressional",
        user_agent: str = DEFAULT_USER_AGENT,
        request_timeout_s: float = 15.0,
        refresh_deadline_s: float = 60.0,
        max_trades_per_source: int = 10_000,
        assumed_max_disclosure_lag_days: int = 45,
        # 180, not 30 — see `SmartMoneyConfig.congress_lookback_days` in
        # src/config.py for the full reasoning (45-day STOCK Act deadline,
        # filing-at-the-deadline behaviour in practice, and a comparable free
        # tool's documented 180-day default). Intentionally unrelated to
        # `SECForm4Provider`'s much tighter window, which tracks Form 4's
        # ~2-business-day deadline instead.
        lookback_days: int = 180,
        min_transaction_value_usd: float = 100_000,
        external_min_transaction_value_usd: float = 250_000,
        cluster_window_days: int = 2,
        min_cluster_owners: int = 2,
        max_observations: int = 40,
        session: requests.Session | None = None,
    ):
        self.urls = {"kadoa": kadoa_url, "congresswatch": congresswatch_url}
        self.kadoa_url = kadoa_url
        self.congresswatch_url = congresswatch_url
        self.data_dir = Path(data_dir)
        self.observations_path = self.data_dir / "observations.json"
        self.manifest_path = self.data_dir / "manifest.json"
        self.store_path = self.data_dir / "disclosures.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent.strip() or DEFAULT_USER_AGENT
        self.request_timeout_s = max(1.0, float(request_timeout_s))
        self.refresh_deadline_s = max(1.0, float(refresh_deadline_s))
        self.max_trades_per_source = max(1, int(max_trades_per_source))
        self.assumed_max_disclosure_lag_days = max(1, int(assumed_max_disclosure_lag_days))
        self.lookback_days = max(1, int(lookback_days))
        self.min_transaction_value_usd = max(0.0, float(min_transaction_value_usd))
        self.external_min_transaction_value_usd = max(
            self.min_transaction_value_usd,
            float(external_min_transaction_value_usd),
        )
        self.cluster_window_days = max(1, int(cluster_window_days))
        self.min_cluster_owners = max(2, int(min_cluster_owners))
        self.max_observations = max(1, int(max_observations))
        self.last_cut_record: dict | None = None
        self.session = session or requests.Session()
        self._cache_lock = threading.Lock()

    def _load_json(self, path: Path, fallback):
        try:
            return json.loads(path.read_text()) if path.exists() else fallback
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Congressional-trading cache unreadable at %s: %s", path, exc)
            return fallback

    # ---- normalization: one raw row -> (row, None) or (None, reason) -------

    def _normalize_kadoa(self, row: dict) -> tuple[dict | None, str | None]:
        transaction_date, why = _date_verdict(row.get("transaction_date"))
        if why:
            return None, why
        filing_date, why = _date_verdict(row.get("filing_date"))
        if why:
            return None, why
        ticker = _symbol(row.get("ticker", ""))
        if not ticker:
            return None, "no_ticker"
        actor = str(row.get("filer_name") or "").strip()
        if not actor:
            return None, "no_filer"
        low, high = _amount_bracket(
            row.get("amount_range_low"),
            row.get("amount_range_high"),
            row.get("amount_range_label", ""),
        )
        chamber = str(row.get("chamber") or "").strip().lower()
        return {
            "source": "kadoa",
            "symbol": ticker,
            "actor": actor,
            "actor_key": _normalize_actor_name(actor),
            "actor_id": str(row.get("filer_id") or ""),
            "direction": _direction(row.get("transaction_type")),
            "amount_low": low,
            "amount_high": high,
            "amount_label": str(row.get("amount_range_label") or ""),
            "transaction_date": transaction_date.isoformat(),
            "disclosure_date": filing_date.isoformat(),
            "disclosure_date_estimated": False,
            "source_url": str(row.get("doc_url") or ""),
            "chamber": chamber,
        }, None

    def _normalize_congresswatch(self, row: dict) -> tuple[dict | None, str | None]:
        transaction_date, why = _date_verdict(row.get("transaction_date"))
        if why:
            return None, why
        ticker = _symbol(row.get("ticker", ""))
        if not ticker:
            return None, "no_ticker"
        actor = str(row.get("member_name") or "").strip()
        if not actor:
            return None, "no_filer"
        low, high = _amount_bracket(None, None, row.get("amount", ""))
        # congresswatch.us carries no filing/disclosure-date field at all
        # (verified against the live feed, 2026-09-04 and 2026-09-19) — see
        # module docstring. Estimate conservatively at the statutory ceiling
        # rather than assuming the trade was just disclosed.
        estimated = min(
            et_today(),
            transaction_date + timedelta(days=self.assumed_max_disclosure_lag_days),
        )
        chamber = str(row.get("chamber") or "").strip().lower()
        return {
            "source": "congresswatch",
            "symbol": ticker,
            "actor": actor,
            "actor_key": _normalize_actor_name(actor),
            "actor_id": str(row.get("bioguide_id") or ""),
            "direction": _direction(row.get("type")),
            "amount_low": low,
            "amount_high": high,
            "amount_label": str(row.get("amount") or ""),
            "transaction_date": transaction_date.isoformat(),
            "disclosure_date": estimated.isoformat(),
            "disclosure_date_estimated": True,
            "source_url": str(row.get("ptr_link") or ""),
            "chamber": chamber,
        }, None

    @staticmethod
    def _merge_group(rows: list[dict]) -> dict:
        """Merge one (ticker, actor, transaction_date) group into a single
        normalized record, flagging any disagreement rather than resolving
        it silently."""
        by_source = {row["source"]: row for row in rows}
        kadoa = by_source.get("kadoa")
        congresswatch = by_source.get("congresswatch")
        primary = kadoa or congresswatch
        assert primary is not None

        if kadoa and congresswatch:
            direction_agrees = kadoa["direction"] == congresswatch["direction"]
            amount_agrees = _brackets_overlap(
                (kadoa["amount_low"], kadoa["amount_high"]),
                (congresswatch["amount_low"], congresswatch["amount_high"]),
            )
            notes = []
            if not direction_agrees:
                notes.append(
                    f"direction disagreement: kadoa={kadoa['direction']} congresswatch={congresswatch['direction']}"
                )
            if not amount_agrees:
                notes.append(
                    f"amount bracket disagreement: kadoa={kadoa['amount_label']!r} "
                    f"congresswatch={congresswatch['amount_label']!r}"
                )
            agreement = "agreement" if (direction_agrees and amount_agrees) else "discrepancy"
            note = "; ".join(notes)
            # kadoa is primary: it carries a real filing_date and is the
            # more structured of the two feeds. Its direction/amount are
            # kept as canonical on a disagreement; the disagreement itself
            # is never hidden — it is recorded in cross_source_note.
            merged = dict(kadoa)
            merged["source_url"] = kadoa["source_url"] or congresswatch["source_url"]
            lows = [v for v in (kadoa["amount_low"], congresswatch["amount_low"]) if v is not None]
            merged["amount_low"] = min(lows) if lows else None
            merged["cross_source_agreement"] = agreement
            merged["cross_source_note"] = note
            merged["group_key"] = _group_key(merged)
            return merged

        merged = dict(primary)
        merged["cross_source_agreement"] = "single_source"
        merged["cross_source_note"] = ""
        merged["group_key"] = _group_key(merged)
        return merged

    # ---- persisted state ---------------------------------------------------

    def _load_state(self) -> tuple[dict, dict, dict]:
        """(manifest, store, observations-by-group), mutually consistent.

        If the manifest says rows were processed but the saved rows are gone
        or unreadable, skipping those rows again would lose them for good. So
        the processed record is discarded and both sources are read in full
        once — the only case in which this provider re-reads everything.
        """
        manifest = self._load_json(self.manifest_path, {})
        manifest = manifest if isinstance(manifest, dict) else {}
        sources = manifest.get("sources")
        manifest["sources"] = sources if isinstance(sources, dict) else {}
        store = self._load_json(self.store_path, None)
        observations = self._load_json(self.observations_path, None)
        claims_progress = any(
            (state or {}).get("processed_keys") for state in manifest["sources"].values() if isinstance(state, dict)
        )
        if not isinstance(store, dict) or not isinstance(observations, list):
            if claims_progress:
                logger.warning(
                    "Congressional saved copy missing or unreadable at %s; "
                    "its processed record is discarded and both sources will "
                    "be read in full once",
                    self.data_dir,
                )
            for state in manifest["sources"].values():
                if isinstance(state, dict):
                    for key in ("processed_keys", "etag", "last_modified", "complete"):
                        state.pop(key, None)
            store, observations = {}, []
        store = {s: dict(store.get(s) or {}) for s in _SOURCES}
        by_group: dict[str, dict] = {}
        for row in observations:
            if isinstance(row, dict) and row.get("symbol"):
                by_group[str(row.get("group_key") or _group_key(row))] = row
        return manifest, store, by_group

    # ---- one source --------------------------------------------------------

    def _refresh_source(
        self,
        source: str,
        state: dict,
        store_rows: dict,
        touched: set[str],
        deadline: float,
        cutoff: date,
        now: datetime,
    ) -> dict:
        """Fetch one source (conditionally) and process only unseen rows.

        Mutates ``state`` (this source's manifest entry), ``store_rows`` and
        ``touched`` in place; returns this source's counts. Never raises.
        """
        started = time.monotonic()
        processed = set(state.get("processed_keys") or [])
        watermark_before = str(state.get("watermark") or "")
        result = {
            "outcome": "",
            "fetched": 0,
            "already_seen": 0,
            "new": 0,
            "processed": 0,
            "dropped": 0,
            "dropped_by_reason": {reason: 0 for reason in _DROP_REASONS},
            "unknown_type_kept": 0,
            "truncated": 0,
            "complete": True,
            "watermark_before": watermark_before,
            "watermark_after": watermark_before,
            "watermark_field": _WATERMARK_FIELD[source],
            "processed_total": len(processed),
            "error": None,
            "last_success_at": str(state.get("last_success_at") or ""),
            "cache_age_hours": None,
            "duration_s": 0.0,
        }
        state["last_attempt_at"] = now.isoformat()
        headers = {"User-Agent": self.user_agent, "Accept": "application/json, */*"}
        # Only ask "has it changed?" when everything in the last copy was
        # processed; otherwise a 304 would strand the unprocessed rows.
        if processed and state.get("complete"):
            if state.get("etag"):
                headers["If-None-Match"] = state["etag"]
            if state.get("last_modified"):
                headers["If-Modified-Since"] = state["last_modified"]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("refresh deadline reached before this source")
            response = self.session.get(
                self.urls[source],
                headers=headers,
                timeout=min(self.request_timeout_s, remaining),
            )
            not_modified = getattr(response, "status_code", None) == 304
            rows: list = []
            if not not_modified:
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise _Unreadable(f"not JSON: {exc}") from exc
                rows = (
                    payload
                    if isinstance(payload, list)
                    else (payload.get("trades") if isinstance(payload, dict) else None)
                )
                if not isinstance(rows, list):
                    raise _Unreadable("payload is not a list of trades")
        except _Unreadable as exc:
            return self._source_failed(source, state, result, "unreadable", exc, now, started)
        except Exception as exc:  # noqa: BLE001 — fail open, labelled
            return self._source_failed(source, state, result, "unreachable", exc, now, started)

        state["last_success_at"] = now.isoformat()
        state["last_status"] = "not_modified" if not_modified else "fetched"
        result["last_success_at"] = state["last_success_at"]
        result["cache_age_hours"] = 0.0
        if not_modified:
            result["outcome"] = "not_modified"
            result["duration_s"] = round(time.monotonic() - started, 3)
            self._log_source(source, result)
            return result

        result["outcome"] = "fetched"
        result["fetched"] = len(rows)
        result["truncated"] = max(0, len(rows) - self.max_trades_per_source)
        rows = rows[: self.max_trades_per_source]
        keys = _disclosure_keys(source, rows)
        normalize = self._normalize_kadoa if source == "kadoa" else self._normalize_congresswatch
        newly: set[str] = set()
        newest = watermark_before
        watermark_field = _WATERMARK_FIELD[source]
        for raw, key in zip(rows, keys):
            if key in processed:
                result["already_seen"] += 1
                continue
            if time.monotonic() >= deadline:
                # Progress so far is kept; the rest is read next time.
                result["complete"] = False
                break
            newly.add(key)
            row, why = normalize(raw) if isinstance(raw, dict) else (None, "malformed_row")
            if row is None:
                result["dropped_by_reason"][why] += 1
                continue
            newest = max(newest, row[watermark_field])
            if date.fromisoformat(row["disclosure_date"]) < cutoff:
                result["dropped_by_reason"]["outside_lookback"] += 1
                continue
            if row["direction"] == "unknown":
                result["unknown_type_kept"] += 1
            store_rows[key] = row
            touched.add(_group_key(row))
            result["new"] += 1
        current = set(keys)
        # Bounded by the file itself: keys no longer in the source are
        # forgotten, keys never seen before are added.
        processed = (processed & current) | newly
        result["processed"] = len(newly)
        result["dropped"] = sum(result["dropped_by_reason"].values())
        result["processed_total"] = len(processed)
        result["watermark_after"] = newest
        state["processed_keys"] = sorted(processed)
        state["watermark"] = newest
        state["complete"] = bool(result["complete"])
        # Validators are kept only for a copy that was processed in full.
        state["etag"] = _header(response, "ETag") if result["complete"] else None
        state["last_modified"] = _header(response, "Last-Modified") if result["complete"] else None
        result["duration_s"] = round(time.monotonic() - started, 3)
        self._log_source(source, result)
        return result

    def _source_failed(self, source, state, result, kind, exc, now, started) -> dict:
        state["last_status"] = f"{kind}:{type(exc).__name__}"
        last = str(state.get("last_success_at") or "")
        age = _hours_since(last, now) if last else None
        result.update(
            {
                "outcome": kind,
                "error": f"{kind}:{type(exc).__name__}",
                "cache_age_hours": age,
                "complete": bool(state.get("complete", True)),
                "duration_s": round(time.monotonic() - started, 3),
            }
        )
        served = (
            f"serving the saved copy last refreshed {age}h ago ({last})"
            if last
            else "no saved copy exists; this source contributes nothing"
        )
        logger.warning(
            "Congressional source %s: source=%s error=%s: %s — %s",
            kind,
            source,
            type(exc).__name__,
            exc,
            served,
        )
        return result

    @staticmethod
    def _log_source(source: str, r: dict) -> None:
        reasons = " ".join(f"{k}={v}" for k, v in r["dropped_by_reason"].items())
        logger.info(
            "Congressional refresh: source=%s outcome=%s fetched=%d "
            "already_seen=%d processed=%d new=%d dropped=%d (%s) "
            "unknown_type_kept=%d truncated=%d complete=%s "
            "watermark(%s)=%s->%s duration_s=%.3f",
            source,
            r["outcome"],
            r["fetched"],
            r["already_seen"],
            r["processed"],
            r["new"],
            r["dropped"],
            reasons,
            r["unknown_type_kept"],
            r["truncated"],
            r["complete"],
            r["watermark_field"],
            r["watermark_before"] or "none",
            r["watermark_after"] or "none",
            r["duration_s"],
        )

    # ---- refresh -------------------------------------------------------------

    def refresh(self, symbols: list[str] | None = None) -> dict:
        """Network refresh with a JSON-safe status/result summary.

        ``symbols`` is accepted for the `SmartMoneySource` signature and
        ignored: both feeds are single whole files with no per-name
        discovery budget to order.

        Incremental — see the block above `_SOURCES`: a source is downloaded
        only when its file changed, and within a changed file only rows never
        processed before are parsed, matched and merged. Only the merged
        groups those rows belong to are rebuilt.

        Fail-open per source: a feed being unreachable, timed out or
        malformed leaves its previously processed rows in place — labelled
        in the result, the log and ``fetch`` with how old they are — and
        never prevents the other feed, or the rest of the smart-money
        pipeline, from proceeding.
        """
        started = time.monotonic()
        deadline = started + self.refresh_deadline_s
        now = et_now()
        today = now.date()
        # Retain enough history for the cluster window on top of the
        # lookback used at read time, same shape as SECForm4Provider.
        cutoff = today - timedelta(days=self.lookback_days + self.cluster_window_days)
        with self._cache_lock:
            manifest, store, observations = self._load_state()
            touched: set[str] = set()
            per_source: dict[str, dict] = {}
            errors: list[str] = []
            for source in _SOURCES:
                state = manifest["sources"].setdefault(source, {})
                per_source[source] = self._refresh_source(
                    source,
                    state,
                    store[source],
                    touched,
                    deadline,
                    cutoff,
                    now,
                )
                if per_source[source]["error"]:
                    errors.append(f"{source}:{per_source[source]['error']}")

            # Rebuild only the merged groups new rows landed in.
            if touched:
                members: dict[str, list[dict]] = defaultdict(list)
                for source in _SOURCES:
                    for row in store[source].values():
                        group = _group_key(row)
                        if group in touched:
                            members[group].append(row)
                for group in touched:
                    if members.get(group):
                        observations[group] = self._merge_group(members[group])
            # Age out whole groups past the retention cutoff, with their rows.
            expired = {
                group
                for group, row in observations.items()
                if date.fromisoformat(str(row["disclosure_date"])[:10]) < cutoff
            }
            for group in expired:
                observations.pop(group, None)
            if expired:
                for source in _SOURCES:
                    store[source] = {key: row for key, row in store[source].items() if _group_key(row) not in expired}

            rows_out = sorted(observations.values(), key=lambda r: r["group_key"])
            freshness = self._freshness(rows_out, manifest, now)
            duration = round(time.monotonic() - started, 3)
            manifest["last_refresh_at"] = now.isoformat()
            manifest["last_refresh"] = {
                "sources": {s: dict(r) for s, r in per_source.items()},
                "observations": len(rows_out),
                "expired_groups": len(expired),
                "duration_s": duration,
            }
            _atomic_json(self.store_path, store)
            _atomic_json(self.observations_path, rows_out)
            _atomic_json(self.manifest_path, manifest)

        logger.info(
            "Congressional refresh summary: observations=%d new=%d processed=%d "
            "expired_groups=%d newest_disclosure=%s (%s days old) "
            "newest_transaction=%s (%s days old) reporting_lag_days=%s "
            "stale_sources=%s duration_s=%.3f",
            len(rows_out),
            sum(r["new"] for r in per_source.values()),
            sum(r["processed"] for r in per_source.values()),
            len(expired),
            freshness["newest_disclosure_date"] or "none",
            freshness["newest_disclosure_age_days"],
            freshness["newest_transaction_date"] or "none",
            freshness["newest_transaction_age_days"],
            freshness["reporting_lag_days"],
            freshness["stale_sources"] or "none",
            duration,
        )

        error = None
        if errors:
            error = ("provider_partial_error" if rows_out else "provider_error") + ":" + ",".join(errors)
        return {
            "status": ("provider_error" if error and not rows_out else "partial" if error else "ok"),
            "kadoa_raw_count": per_source["kadoa"]["fetched"],
            "congresswatch_raw_count": per_source["congresswatch"]["fetched"],
            "merged_count": len(rows_out),
            "cached_observations": len(rows_out),
            "new_disclosures": sum(r["new"] for r in per_source.values()),
            "processed_disclosures": sum(r["processed"] for r in per_source.values()),
            "discrepancy_count": sum(row.get("cross_source_agreement") == "discrepancy" for row in rows_out),
            "congressional_sources": per_source,
            "congressional_freshness": freshness,
            "duration_s": duration,
            "error": error,
        }

    # ---- freshness -----------------------------------------------------------

    def _freshness(self, rows: list[dict], manifest: dict, now: datetime) -> dict:
        """How old the newest evidence is, and how old each source's copy is.

        Congressional disclosures are NOT current news: on 2026-09-19 the
        lag from trade to filing, over the 3,147 kept kadoa rows that carry a
        real filing date, was median 60 days (0 to 880). So this reports the newest
        disclosure's age AND the newest trade's age, and the lag measured on
        rows that carry a real filing date, so nothing downstream mistakes a
        newly filed disclosure for a new trade. congresswatch-only dates are
        estimates (transaction + the statutory ceiling, capped at today) and
        are used for the newest disclosure only when no real one exists.
        """
        today = now.date()
        real = [r for r in rows if not r.get("disclosure_date_estimated")]
        pool = real or rows
        newest_disclosure = max((str(r["disclosure_date"])[:10] for r in pool), default="")
        newest_transaction = max((str(r["transaction_date"])[:10] for r in rows), default="")
        lags = sorted(
            (
                date.fromisoformat(str(r["disclosure_date"])[:10]) - date.fromisoformat(str(r["transaction_date"])[:10])
            ).days
            for r in real
        )
        sources: dict[str, dict] = {}
        stale: list[str] = []
        for source in _SOURCES:
            state = manifest.get("sources", {}).get(source) or {}
            last = str(state.get("last_success_at") or "")
            last_day = last[:10]
            failed = str(state.get("last_status") or "").startswith(("unreachable", "unreadable"))
            # Stale: the last attempt failed, or the last good copy is from an
            # earlier day than today (the refresh runs once a day pre-market).
            is_stale = (not last) or failed or last_day < today.isoformat()
            if is_stale:
                stale.append(source)
            sources[source] = {
                "last_success_at": last,
                "cache_age_hours": _hours_since(last, now) if last else None,
                "last_status": str(state.get("last_status") or ""),
                "stale": is_stale,
                "watermark": str(state.get("watermark") or ""),
                "watermark_field": _WATERMARK_FIELD[source],
                "processed_total": len(state.get("processed_keys") or []),
            }
        return {
            "known": bool(manifest.get("sources")),
            "as_of": now.isoformat(),
            "observations": len(rows),
            "newest_disclosure_date": newest_disclosure,
            "newest_disclosure_age_days": (
                (today - date.fromisoformat(newest_disclosure)).days if newest_disclosure else None
            ),
            "newest_disclosure_estimated": bool(rows) and not real,
            "newest_transaction_date": newest_transaction,
            "newest_transaction_age_days": (
                (today - date.fromisoformat(newest_transaction)).days if newest_transaction else None
            ),
            "reporting_lag_days": (
                {"min": lags[0], "median": median(lags), "max": lags[-1], "rows": len(lags)} if lags else None
            ),
            "sources": sources,
            "stale_sources": stale,
        }

    def congressional_freshness(self) -> dict:
        """The freshness report from the saved copy. No network."""
        manifest = self._load_json(self.manifest_path, {})
        rows = self._load_json(self.observations_path, [])
        return self._freshness(
            [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [],
            manifest if isinstance(manifest, dict) else {},
            et_now(),
        )

    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]:
        """Cache-only. Congressional data is confirmatory context for the
        already-configured trading universe only — unlike SEC Form 4's
        external-purchase admission lane, it never grows the universe (the
        seat's acceptance contract ties symbol admission to an exact SEC
        Form 4 open-market `P`, not to congressional disclosures).

        A stale copy is served LABELLED: the returned error names every
        stale source and how old its last good copy is, and a warning is
        logged, so the seat's status reads degraded rather than clean.
        """
        core = {_symbol(s) for s in symbols if str(s).strip()}
        raw_rows = self._load_json(self.observations_path, [])
        report = self.congressional_freshness()
        stale_labels: list[str] = []
        for source in report["stale_sources"]:
            info = report["sources"][source]
            if info["last_success_at"]:
                label = f"{source}:age_h={info['cache_age_hours']}:last_success={info['last_success_at']}"
                logger.warning(
                    "Congressional cache served stale: source=%s last refreshed %sh ago (%s) last_status=%s",
                    source,
                    info["cache_age_hours"],
                    info["last_success_at"],
                    info["last_status"] or "none",
                )
            else:
                label = f"{source}:never_refreshed"
                logger.warning(
                    "Congressional cache served stale: source=%s has never been refreshed successfully last_status=%s",
                    source,
                    info["last_status"] or "none",
                )
            stale_labels.append(label)
        parsed: list[SmartMoneyObservation] = []
        invalid = 0
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            try:
                symbol = _symbol(raw.get("symbol", ""))
                if symbol not in core:
                    continue
                transaction_date = date.fromisoformat(str(raw["transaction_date"]))
                disclosure_date = date.fromisoformat(str(raw["disclosure_date"]))
                age_days = max(0, (et_today() - disclosure_date).days)
                if age_days > self.lookback_days:
                    continue
                # Freshness is the age of the TRADE, not of the filing. A
                # disclosure filed yesterday about a trade made two months ago
                # is two-month-old information; labelling it by its filing
                # age called it "fresh" and invited the seat to read it as
                # current news (median filing lag 60 days, measured 2026-09-19).
                trade_age_days = max(0, (et_today() - transaction_date).days)
                freshness = (
                    "fresh" if trade_age_days <= 7 else ("delayed" if trade_age_days <= self.lookback_days else "stale")
                )
                lag_days = max(0, (disclosure_date - transaction_date).days)
                item = SmartMoneyObservation(
                    symbol=symbol,
                    stream="congressional",
                    actor=raw.get("actor", ""),
                    actor_cik=raw.get("actor_id", ""),
                    direction=raw.get("direction", "unknown"),
                    amount_range=raw.get("amount_label", ""),
                    transaction_date=transaction_date,
                    disclosure_date=disclosure_date,
                    known_at=datetime.combine(disclosure_date, datetime.min.time()),
                    source_url=raw.get("source_url") or "https://congresswatch.us/",
                    transaction_value_usd=raw.get("amount_low"),
                    in_core_universe=True,
                    in_trading_universe=True,
                    # Congressional disclosures never independently admit a
                    # new symbol or trigger the transient lane — see
                    # `smart_money_analyst.py`'s module docstring.
                    admission_eligible=False,
                    transient_admission_eligible=False,
                    lag_days=lag_days,
                    disclosure_age_days=age_days,
                    freshness=freshness,
                    economic_role="confirmatory",
                    cross_source_agreement=raw.get("cross_source_agreement", ""),
                    cross_source_note=raw.get("cross_source_note", ""),
                    disclosure_date_estimated=bool(raw.get("disclosure_date_estimated", False)),
                )
            except Exception:
                invalid += 1
                continue
            parsed.append(item)

        survivors = cluster_survivors(
            parsed,
            threshold_fn=lambda symbol: (
                self.min_transaction_value_usd if symbol in core else self.external_min_transaction_value_usd
            ),
            cluster_window_days=self.cluster_window_days,
            min_cluster_owners=self.min_cluster_owners,
        )
        full_order = sorted(
            survivors.values(),
            key=lambda item: (
                -(item.transaction_value_usd or 0),
                item.disclosure_age_days,
                item.actor,
            ),
        )
        ordered = full_order[: self.max_observations]
        self.last_cut_record = cut_record(
            full_order, ordered, stream="congress", max_observations=self.max_observations
        )
        errors: list[str] = []
        if stale_labels:
            errors.append("congressional_stale_cache:" + ",".join(stale_labels))
        if invalid:
            errors.append(f"cache_partial_error:{invalid}_invalid_rows")
        return ordered, ("; ".join(errors) or None)


def _form4_drain_summary(results) -> dict:
    """Top-level drain outcome across Form 4 sub-provider refresh results.

    Empty when no sub-provider ran a drain, so a wrapper holding only
    congressional providers adds nothing. The combined read-through date is
    the EARLIEST any Form 4 provider reports — it is a claim that every
    watched name is read, so it is only as late as the least-read provider.
    """
    form4 = [r for r in results if isinstance(r, dict) and "watched_drain_ran" in r]
    if not form4:
        return {}
    dates = [str(r.get("watched_read_through") or "")[:10] for r in form4]
    # EDGAR's own denominator (board item 126), lifted out of the per-provider
    # dict for the same reason the counts above are: nested, it reaches only
    # the log line, and the pre-open check and the session record read the
    # top level. Verified only if every Form 4 provider verified its own.
    # A Form 4 provider that recorded NO coverage is substituted with the
    # blank record, not skipped. Skipping it let a provider that answered
    # the coverage question not at all be carried by one that did — the
    # same read-one-fact-in-two-directions defect this change fixed one
    # layer up, in `_alert_form4_backlog_before_open`.
    from src.data.smart_money_edgar_coverage import blank_edgar_coverage

    edgars = [
        r.get("edgar_coverage") if isinstance(r.get("edgar_coverage"), dict) else blank_edgar_coverage() for r in form4
    ]
    edgar_total = sum(int(e.get("edgar_total") or 0) for e in edgars)
    enumerated = sum(int(e.get("enumerated") or 0) for e in edgars)
    days_queried = min((int(e.get("days_queried") or 0) for e in edgars), default=0)
    days_in_window = max((int(e.get("days_in_window") or 0) for e in edgars), default=0)
    edgar_coverage = {
        "known": bool(edgars) and all(bool(e.get("known")) for e in edgars),
        "verified": bool(edgars) and all(bool(e.get("verified")) for e in edgars),
        "reasons": sorted(
            {str(x) for e in edgars for x in (e.get("reasons") or []) if str(x).strip()}
            or ({"never_recorded"} if not edgars else set())
        ),
        "edgar_total": edgar_total,
        "enumerated": enumerated,
        "rows_received": sum(int(e.get("rows_received") or 0) for e in edgars),
        "ratio": round(enumerated / edgar_total, 4) if edgar_total > 0 else None,
        # A window, not a quantity — see `form4_coverage` below.
        "days_queried": days_queried,
        "days_in_window": days_in_window,
        "window_fraction": (round(days_queried / days_in_window, 4) if days_in_window > 0 else None),
        # Also a day count, so also min — summing it beside a min'd
        # `days_queried` produced merged records claiming more days with a
        # readable count than days queried at all.
        "days_with_total": min(
            (int(e.get("days_with_total") or 0) for e in edgars),
            default=0,
        ),
    }
    return {
        "edgar_coverage": edgar_coverage,
        "watched_read_through": "" if any(not d for d in dates) else min(dates),
        "watched_unchecked_names": sorted({str(n) for r in form4 for n in (r.get("watched_unchecked_names") or [])}),
        "watched_drain_ran": any(bool(r.get("watched_drain_ran")) for r in form4),
        "watched_drain_read": sum(int(r.get("watched_drain_read") or 0) for r in form4),
        "watched_drain_deadline_hit": any(bool(r.get("watched_drain_deadline_hit")) for r in form4),
        "watched_names": sum(int(r.get("watched_names") or 0) for r in form4),
        "watched_names_read_through": sum(int(r.get("watched_names_read_through") or 0) for r in form4),
        "watched_names_unread": sorted({str(n) for r in form4 for n in (r.get("watched_names_unread") or [])}),
    }


def _congressional_summary(results) -> dict:
    """The congressional refresh's counts, lifted to the top level.

    Empty when no congressional provider ran, so the Form 4-only wiring (the
    switch off) adds nothing. Nested per sub-provider these counts reach only
    the log line; at the top level the pipeline can record them.
    """
    for r in results:
        if isinstance(r, dict) and "congressional_sources" in r:
            return {
                "congressional": {
                    key: r.get(key)
                    for key in (
                        "status",
                        "new_disclosures",
                        "processed_disclosures",
                        "cached_observations",
                        "discrepancy_count",
                        "congressional_sources",
                        "congressional_freshness",
                        "duration_s",
                        "error",
                    )
                }
            }
    return {}


class CombinedSmartMoneyProvider:
    """Fans one `SmartMoneySource` call out to several, concatenating
    results. Each sub-provider's failure is isolated: one raising or timing
    out never prevents the others' evidence, or the run, from proceeding —
    same fail-open posture each sub-provider already applies internally.
    """

    def __init__(self, providers: list[SmartMoneySource]):
        self.providers = [p for p in providers if p is not None]

    def refresh(self, symbols: list[str] | None = None) -> dict:
        """``symbols`` are the names the desk watches; sub-providers that
        accept them spend their discovery budget on those names first.
        """
        # Keyed by index+class name, not class name alone: two providers of
        # the same class (or two test doubles that happen to share one)
        # must not collide and silently drop one result from `results`.
        results: dict[str, dict] = {}
        errors: list[str] = []
        for index, provider in enumerate(self.providers):
            name = f"{index}:{type(provider).__name__}"
            try:
                try:
                    results[name] = provider.refresh(symbols)
                except TypeError:
                    results[name] = provider.refresh()
                if results[name].get("error"):
                    errors.append(f"{name}:{results[name]['error']}")
            except Exception as exc:
                logger.warning("Smart-money sub-provider refresh failed (%s): %s", name, exc)
                results[name] = {"status": "provider_error", "error": str(exc)}
                errors.append(f"{name}:refresh_exception:{type(exc).__name__}")
        return {
            "status": (
                "ok"
                if not errors
                else "provider_error"
                if all(r.get("status") == "provider_error" for r in results.values())
                else "partial"
            ),
            "providers": results,
            # Surfaced at the top level, not only nested per sub-provider, so
            # the unread-Form-4 backlog is a number the session payload and
            # the pre-market log line can actually read.
            "pending_filings": sum(int(r.get("pending_filings") or 0) for r in results.values()),
            "watched_pending_filings": sum(int(r.get("watched_pending_filings") or 0) for r in results.values()),
            "discovery_cap_reached": any(bool(r.get("discovery_cap_reached")) for r in results.values()),
            # The drain's outcome, surfaced for the pre-open check. Until
            # 2026-09-19 only the three counts above were lifted out of the
            # per-provider dict, so the pre-open check always read an empty
            # `watched_read_through` from this wrapper — the production
            # wiring — and would have alerted every morning whatever the
            # drain did. Only Form 4 providers carry these keys.
            **_form4_drain_summary(results.values()),
            **_congressional_summary(results.values()),
            "error": "; ".join(errors) or None,
        }

    def fetch(self, symbols: list[str]) -> tuple[list[SmartMoneyObservation], str | None]:
        observations: list[SmartMoneyObservation] = []
        errors: list[str] = []
        for index, provider in enumerate(self.providers):
            name = f"{index}:{type(provider).__name__}"
            try:
                rows, error = provider.fetch(symbols)
                observations.extend(rows)
                if error:
                    errors.append(f"{name}:{error}")
            except Exception as exc:
                logger.warning("Smart-money sub-provider fetch failed (%s): %s", name, exc)
                errors.append(f"{name}:fetch_exception:{type(exc).__name__}")
        error = "; ".join(errors) or None
        return observations, error

    def form4_freshness(self, symbols: list[str] | None = None) -> dict:
        """Combined "anything filed since our last read?" verdict.

        Fail-closed by construction: the result is ``ok`` only if EVERY
        sub-provider that can answer did answer. A sub-provider that raised,
        or one that cannot answer at all when none can, leaves the verdict
        not-ok — the caller must read that as unknown freshness, never as
        "nothing new". Congressional providers have no Form 4 history and
        are skipped rather than counted as failures.
        """
        verdict = {
            "ok": False,
            "new_filings": [],
            "read_through": "",
            "checked": 0,
            "covered": 0,
            "unread_names": [],
            "unread_filings": 0,
            "unchecked": [],
            "reason": "no Form 4 provider",
        }
        answered = False
        covered = 0
        unread_names: list[str] = []
        unread_filings = 0
        new_filings: set[str] = set()
        unchecked: list[str] = []
        reasons: list[str] = []
        read_through = ""
        checked = 0
        for index, provider in enumerate(self.providers):
            probe = getattr(provider, "form4_freshness", None)
            if not callable(probe):
                continue
            name = f"{index}:{type(provider).__name__}"
            try:
                result = probe(symbols) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("Form 4 freshness probe failed (%s): %s", name, exc)
                answered = True
                unchecked.append(name)
                reasons.append(f"{name}:{type(exc).__name__}")
                continue
            answered = True
            new_filings.update(str(a).strip() for a in (result.get("new_filings") or []) if str(a).strip())
            unchecked.extend(str(c) for c in (result.get("unchecked") or []))
            checked += int(result.get("checked") or 0)
            covered += int(result.get("covered") or 0)
            unread_names.extend(str(c) for c in (result.get("unread_names") or []))
            unread_filings += int(result.get("unread_filings") or 0)
            read_through = read_through or str(result.get("read_through") or "")
            if not result.get("ok"):
                reasons.append(f"{name}:{result.get('reason') or 'not ok'}")
        verdict["new_filings"] = sorted(new_filings)
        verdict["unchecked"] = unchecked
        verdict["checked"] = checked
        verdict["covered"] = covered
        verdict["unread_names"] = sorted(set(unread_names))
        verdict["unread_filings"] = unread_filings
        verdict["read_through"] = read_through
        if not answered:
            return verdict
        if reasons:
            verdict["reason"] = "; ".join(reasons)
            return verdict
        verdict["ok"] = True
        verdict["reason"] = (
            f"{len(new_filings)} new filing(s) on names read through"
            if new_filings
            else "every watched name read through; nothing new"
        )
        return verdict

    def congressional_freshness(self) -> dict | None:
        """The congressional freshness report, or None when that feed is off.

        No network. Isolated like every other call here: a failure is logged
        and reported as unknown, never raised.
        """
        for provider in self.providers:
            probe = getattr(provider, "congressional_freshness", None)
            if not callable(probe):
                continue
            try:
                return probe()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Congressional freshness read failed: %s", exc)
                return {"known": False, "error": type(exc).__name__}
        return None

    def form4_coverage(self) -> dict:
        """Coverage of the watched set, from the Form 4 sub-provider(s). No network.

        Unknown unless at least one sub-provider recorded coverage; with
        several, the result is only as complete as the least complete.
        """
        from src.data.smart_money_edgar_coverage import blank_edgar_coverage

        blank_edgar = blank_edgar_coverage()
        merged = {
            "known": False,
            "as_of": "",
            "watched": 0,
            "read_through": 0,
            "unread": [],
            "edgar": dict(blank_edgar),
            # Market-wide blindness merges as OR, the same posture as
            # `unread` and `verified`: one sub-provider that read none
            # of the wider market makes the merged view blind, because
            # the coverage it did not get is not supplied by another.
            "market_wide_blind": False,
            "market_wide_read": 0,
            "market_wide_pending": 0,
        }
        found = False
        # EDGAR coverage across sub-providers is only as verified as the
        # least verified one, and its reasons are the union — the same
        # posture as `unread` above. A wrapper holding no Form 4 provider
        # at all reports never_recorded, which reads as unverified.
        edgar_reasons: set[str] = set()
        edgar_verified: list[bool] = []
        edgar_days_queried: list[int] = []
        edgar_days_in_window: list[int] = []
        edgar_days_with_total: list[int] = []
        for provider in self.providers:
            probe = getattr(provider, "form4_coverage", None)
            if not callable(probe):
                continue
            try:
                result = probe() or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("Form 4 coverage read failed: %s", exc)
                return {**merged, "known": False, "edgar": dict(blank_edgar)}
            if not result.get("known"):
                return {**merged, "known": False, "edgar": dict(blank_edgar)}
            found = True
            merged["as_of"] = merged["as_of"] or str(result.get("as_of") or "")
            merged["watched"] += int(result.get("watched") or 0)
            merged["read_through"] += int(result.get("read_through") or 0)
            merged["unread"].extend(str(s) for s in (result.get("unread") or []))
            merged["market_wide_blind"] = bool(merged["market_wide_blind"] or result.get("market_wide_blind"))
            try:
                merged["market_wide_read"] += int(result.get("market_wide_read") or 0)
                merged["market_wide_pending"] += int(
                    result.get("market_wide_pending") or 0,
                )
            except (TypeError, ValueError):
                pass
            sub_edgar = result.get("edgar")
            if not isinstance(sub_edgar, dict):
                sub_edgar = dict(blank_edgar)
            edgar_verified.append(bool(sub_edgar.get("verified")))
            edgar_reasons.update(str(r) for r in (sub_edgar.get("reasons") or []) if str(r).strip())
            for key in ("edgar_total", "enumerated", "rows_received"):
                try:
                    merged["edgar"][key] += int(sub_edgar.get(key) or 0)
                except (TypeError, ValueError):
                    pass
            # Days are a WINDOW, not a quantity: summing them across
            # providers makes the queried/window fraction meaningless, and
            # summing `days_with_total` beside a min'd `days_queried` made
            # merged records claiming more days with a readable count than
            # days queried at all. Collected and reduced after the loop, so
            # a provider that legitimately queried ZERO days is min'd like
            # any other rather than read as "nothing recorded yet".
            for key, sink in (
                ("days_queried", edgar_days_queried),
                ("days_in_window", edgar_days_in_window),
                ("days_with_total", edgar_days_with_total),
            ):
                try:
                    sink.append(int(sub_edgar.get(key) or 0))
                except (TypeError, ValueError):
                    sink.append(0)
        # The merged fraction is a lower bound: the fewest days any provider
        # reached, over the widest window any of them meant.
        merged["edgar"]["days_queried"] = min(edgar_days_queried, default=0)
        merged["edgar"]["days_in_window"] = max(edgar_days_in_window, default=0)
        merged["edgar"]["days_with_total"] = min(edgar_days_with_total, default=0)
        merged["known"] = found
        merged["unread"] = sorted(set(merged["unread"]))
        merged["edgar"]["known"] = bool(edgar_verified)
        merged["edgar"]["verified"] = bool(edgar_verified) and all(edgar_verified)
        merged["edgar"]["reasons"] = sorted(edgar_reasons or ({"never_recorded"} if not edgar_verified else set()))
        total = int(merged["edgar"]["edgar_total"] or 0)
        merged["edgar"]["ratio"] = round(int(merged["edgar"]["enumerated"] or 0) / total, 4) if total > 0 else None
        window = int(merged["edgar"]["days_in_window"] or 0)
        merged["edgar"]["window_fraction"] = (
            round(int(merged["edgar"]["days_queried"] or 0) / window, 4) if window > 0 else None
        )
        return merged
