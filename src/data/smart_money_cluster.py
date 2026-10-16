"""Two different things share the word "cluster" here; keep them apart.

1. ``cluster_survivors`` — a ROW-RETENTION rule. Both `SECForm4Provider`
   (SEC Form 4, `stream="insider"`, src/data/smart_money.py) and
   `CongressionalTradingProvider` (Congress disclosures,
   `stream="congressional"`, src/data/congressional_trading.py) reduce their
   candidate observations the same way: keep any single row that alone
   clears a materiality threshold, plus every row of an independent-actor
   group, in either direction, whose members fall within
   ``cluster_window_days`` of some anchor row and whose combined value clears
   that threshold. It decides which rows the seat gets to SEE as context. It
   is not the research-defined cluster and has no effect on conviction.

2. ``insider_purchase_clusters`` — the RESEARCH-DEFINED purchase cluster
   (board item 124, owner ask 2026-09-19): two or more distinct insiders
   making OPPORTUNISTIC open-market purchases (Form 4 code P) in the same
   symbol on the SAME transaction date. This is the fact that may lift the
   seat's conviction (see `src.models._purchase_cluster_lift`).

Sources, as checked 2026-09-19. Alldredge & Blank, "Do Insiders Cluster
Trades with Colleagues? Evidence from Daily Insider Trading", J. Financial
Research 42(2):331-360, 2019 (SSRN 2781761; the SSRN and Wiley pages both
returned HTTP 403 to a fetch, so this rests on the abstract as quoted in
search results): "around 23% of insider purchases occur on the same day as
another insider purchase at the same company", and "clustered insider
purchases are followed by abnormal returns in excess of 2% during the
subsequent month". The abstract's measure is SAME-DAY. A "within two days"
window, "~2.1%" and "0.9 percentage points above solitary purchases" appear
only in a secondary summary (IBKR Campus, "What Corporate Insider Buying Can
Tell Investors"), not in the abstract; they could not be checked against the
paper. Cohen, Malloy & Pomorski, "Decoding Inside Information" (NBER w16454):
opportunistic trades earn "value-weight abnormal returns of 82 basis points
per month" while routine trades' abnormal returns "are essentially zero".

Why the retention rule was NOT changed to same-day/opportunistic: it serves
a different purpose (what context the seat sees, for sells and for Congress
as well as insider buys), and narrowing it would silently drop rows —
routine purchases the seat is told to discount, sells, cross-day groups —
that the seat and the operator currently see. Its two-day window is the
figure from the secondary summary above, not the paper's abstract; it is a
retention parameter, and nothing trade-governing now reads it as the
research cluster.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import Callable, Iterable

from src.models import InsiderPurchaseCluster, SmartMoneyObservation

logger = logging.getLogger(__name__)

#: A cluster is a purchase "on the same day as another insider purchase at the
#: same company" (Alldredge & Blank 2019, abstract) — the insider plus at
#: least one other, so two distinct insiders.
MIN_PURCHASE_CLUSTER_INSIDERS = 2

# Board item 124, corrected defect axis (2026-09-20): a genuinely clustered
# SYMBOL was losing its whole signal to `SECForm4Provider.fetch`'s
# `max_observations` truncation when unrelated, higher-dollar buys on OTHER
# symbols filled every slot ahead of it -- cross-symbol crowd-out, not the
# within-symbol tie-break a previously reviewed and rejected fix targeted
# (adjusting the shared row-level sort key: found unsafe, since it cited
# wrong signal-weight values, a nonexistent flag, and would have overridden
# dollar-value ordering entirely rather than narrowly tie-breaking). The
# adversary-recommended fix instead: `reserve_cluster_symbols` guarantees at
# least one surviving row per genuinely clustered symbol, via a SMALL,
# separately-bounded reservation that leaves the dollar-value sort untouched
# for everyone else. 5 is an undderived round number -- see this constant's
# entry in config/number_ledger.yaml for the open question.
MAX_CLUSTER_RESERVED_SLOTS = 5


def observation_key(item: SmartMoneyObservation) -> tuple:
    return (
        item.accession_number,
        item.transaction_row,
        item.symbol,
        item.actor_cik,
        item.transaction_date,
        item.transaction_code,
    )


def cluster_survivors(
    items: list[SmartMoneyObservation],
    *,
    threshold_fn: Callable[[str], float],
    cluster_window_days: int,
    min_cluster_owners: int,
) -> dict[tuple, SmartMoneyObservation]:
    """Return the subset of ``items`` that clears materiality alone or as a
    cluster of independent actors, keyed by :func:`observation_key`.

    ``threshold_fn(symbol)`` returns the USD materiality threshold for that
    symbol (core vs. external universes may use different thresholds — see
    each provider's own ``fetch``). An amendment is retained in the raw
    cache for provenance but cannot independently clear either gate, so an
    original filing and its correction are never counted as two actions.
    """
    by_group: dict[tuple[str, str], list[SmartMoneyObservation]] = defaultdict(list)
    for item in items:
        if item.amendment:
            continue
        by_group[(item.symbol, item.direction)].append(item)

    survivors: dict[tuple, SmartMoneyObservation] = {}
    for (symbol, _direction), group in by_group.items():
        threshold = threshold_fn(symbol)
        for item in group:
            if item.transaction_value_usd is not None and item.transaction_value_usd >= threshold:
                survivors[observation_key(item)] = item
        for anchor in group:
            window = [
                item
                for item in group
                if abs((item.transaction_date - anchor.transaction_date).days) <= cluster_window_days
                and item.transaction_value_usd is not None
            ]
            independent = {item.actor_cik for item in window if item.actor_cik}
            total = sum(item.transaction_value_usd or 0 for item in window)
            if len(independent) >= min_cluster_owners and total >= threshold:
                for item in window:
                    survivors[observation_key(item)] = item
    return survivors


def reserve_cluster_symbols(
    ordered: list[SmartMoneyObservation],
    clustered_symbols: set[str],
    *,
    max_observations: int,
    max_reserved_slots: int,
) -> list[SmartMoneyObservation]:
    """Guarantee at least one row per symbol in ``clustered_symbols`` survives
    truncation to ``max_observations``, board item 124's cross-symbol
    crowd-out fix.

    ``ordered`` is already sorted best-first by the caller's dollar-value
    sort (`SECForm4Provider.fetch`); this only decides which rows survive the
    cutoff, never their relative order. A symbol already present in the top
    ``max_observations`` needs nothing — the fact already reaches the seat.
    A missing clustered symbol is granted one slot by displacing the
    LOWEST-priority row currently kept that does not itself belong to a
    clustered symbol (so one reservation can never silently undo another),
    up to ``max_reserved_slots`` reservations total. If every kept row
    already belongs to a clustered symbol, no further reservation is
    possible and the remaining clustered symbols stay excluded — the bound
    is what keeps a busy cluster day from crowding out everything else in
    turn.

    Deliberately does NOT help a clustered symbol whose OWN non-cluster rows
    already occupy the cap (that symbol is already "present" and nothing is
    reserved) — that is the within-symbol tie-break the previously reviewed
    sort-key fix was rejected for, and this mechanism does not attempt it.
    """
    main = list(ordered[:max_observations])
    if not clustered_symbols or len(ordered) <= len(main):
        return main
    present = {item.symbol for item in main}
    missing = [s for s in clustered_symbols if s not in present]
    if not missing:
        return main
    overflow = ordered[max_observations:]
    reserved = 0
    for symbol in missing:
        if reserved >= max_reserved_slots:
            break
        candidate = next((item for item in overflow if item.symbol == symbol), None)
        if candidate is None:
            continue
        evict_at = next(
            (i for i in range(len(main) - 1, -1, -1) if main[i].symbol not in clustered_symbols),
            None,
        )
        if evict_at is None:
            break
        main[evict_at] = candidate
        present.add(symbol)
        reserved += 1
    return main


def _cluster_member(item: SmartMoneyObservation) -> bool:
    return (
        item.stream == "insider"
        and not item.amendment
        and item.direction == "buy"
        and item.transaction_code == "P"
        # Reuses the routine/opportunistic verdict `SECForm4Provider.fetch`
        # stamps from `src.data.insider_signal.classify_transaction`. Only an
        # affirmative OPPORTUNISTIC label counts: "routine" carries no signal
        # and "indeterminate" means the filing lacked the amounts to test.
        and item.signal_class == "opportunistic"
    )


def insider_purchase_clusters(
    items: Iterable[SmartMoneyObservation],
    *,
    universe: set[str],
    today: date,
) -> dict[str, InsiderPurchaseCluster]:
    """Most recent same-day opportunistic purchase cluster per symbol.

    Only symbols in ``universe`` (the configured trading universe) are
    considered: a cluster must never be a way for a name outside it to gain
    weight, and admission belongs to the universe screen, not to this.
    Distinct insiders are counted by reporting-owner CIK (the name when a CIK
    is missing), so one insider filing twice on one day is not a cluster.
    Amendments are excluded so an original and its correction never count as
    two purchases.
    """
    by_day: dict[tuple[str, date], list[SmartMoneyObservation]] = defaultdict(list)
    for item in items:
        if item.symbol in universe and _cluster_member(item):
            by_day[(item.symbol, item.transaction_date)].append(item)

    latest: dict[str, InsiderPurchaseCluster] = {}
    for (symbol, day), members in by_day.items():
        insiders = sorted({(m.actor_cik or m.actor.strip().casefold()) for m in members})
        if len(insiders) < MIN_PURCHASE_CLUSTER_INSIDERS:
            continue
        if symbol in latest and latest[symbol].transaction_date >= day:
            continue
        disclosed = max(m.disclosure_date for m in members)
        latest[symbol] = InsiderPurchaseCluster(
            transaction_date=day,
            distinct_insiders=len(insiders),
            insider_ciks=insiders,
            combined_value_usd=round(
                sum(m.transaction_value_usd or 0 for m in members),
                2,
            ),
            latest_disclosure_date=disclosed,
            filing_age_days=max(0, (today - disclosed).days),
        )
    return latest


def cluster_reservation_census(
    ordered: list[SmartMoneyObservation],
    final: list[SmartMoneyObservation],
    clusters: Iterable[str],
    *,
    max_observations: int,
    max_reserved_slots: int,
) -> dict:
    """Record-only: what ``MAX_CLUSTER_RESERVED_SLOTS`` did on one refresh.

    ``found`` distinct clustered symbols; ``already_kept`` made the plain
    top-``max_observations`` cut on their own; ``reserved`` got in only
    through a reserved slot; ``cut`` stayed out. Governs nothing -- read by no
    gate, rank or size -- so the bound can later be judged from data.
    """
    found = sorted(set(clusters))
    natural = {item.symbol for item in ordered[:max_observations]}
    kept = {item.symbol for item in final}
    reserved = [s for s in found if s not in natural and s in kept]
    cut = [s for s in found if s not in kept]
    return {
        "clusters_found": len(found),
        "already_kept": len([s for s in found if s in natural]),
        "reserved": len(reserved),
        "cut": len(cut),
        "reserved_symbols": reserved,
        "cut_symbols": cut,
        "max_reserved_slots": max_reserved_slots,
        "max_observations": max_observations,
    }


def _cut_class(item: SmartMoneyObservation) -> str:
    """The sort class a row ranked under (mirrors the insider sort's leading keys)."""
    if item.transient_admission_eligible:
        return "admission-qualifying"
    if item.in_core_universe:
        return "core-universe"
    return "other"


def cut_record(
    ordered: list[SmartMoneyObservation],
    final: list[SmartMoneyObservation],
    *,
    stream: str,
    max_observations: int,
) -> dict:
    """Record-only: every row ``max_observations`` dropped on one refresh.

    ``cut`` lists each dropped row (symbol, stream, direction, sort class,
    dollar value, and whether the symbol had ANY row kept); ``by_class`` counts
    them. Empty when nothing was cut. Governs nothing -- read by no gate, rank
    or size. Emits one structured log line per refresh.
    """
    kept_keys = {observation_key(item) for item in final}
    kept_symbols = {item.symbol for item in final}
    cut = [
        {
            "symbol": item.symbol,
            "stream": stream,
            "direction": item.direction,
            "class": _cut_class(item),
            "value_usd": item.transaction_value_usd or 0,
            "symbol_kept": item.symbol in kept_symbols,
        }
        for item in ordered
        if observation_key(item) not in kept_keys
    ]
    by_class: dict[str, int] = {}
    for row in cut:
        by_class[row["class"]] = by_class.get(row["class"], 0) + 1
    record = {
        "stream": stream,
        "max_observations": max_observations,
        "rows_in": len(ordered),
        "rows_kept": len(final),
        "rows_cut": len(cut),
        "by_class": by_class,
        "symbols_fully_cut": sorted({r["symbol"] for r in cut if not r["symbol_kept"]}),
        "cut": cut,
    }
    logger.info(
        "smart_money_cut_record stream=%s in=%d kept=%d cut=%d by_class=%s fully_cut_symbols=%d",
        stream,
        len(ordered),
        len(final),
        len(cut),
        by_class,
        len(record["symbols_fully_cut"]),
    )
    return record


def find_cluster_census(provider: object) -> dict | None:
    """The SEC provider's last cluster census, however the provider is wrapped
    (duck-typed, same probe shape as the sale census). None when none exists."""
    candidates: list[object] = [provider]
    nested = getattr(provider, "providers", None)
    if isinstance(nested, (list, tuple)):
        candidates.extend(nested)
    if hasattr(provider, "__dict__"):
        candidates.extend(vars(provider).values())
    census: dict | None = None
    cut_records: dict[str, dict] = {}
    for candidate in candidates:
        found = getattr(candidate, "last_cluster_census", None)
        if census is None and isinstance(found, dict):
            census = found
        record = getattr(candidate, "last_cut_record", None)
        if isinstance(record, dict) and record.get("stream"):
            cut_records.setdefault(record["stream"], record)
    if cut_records:
        # Persisted with the existing cluster-census evidence row (one per run).
        census = {**(census or {}), "cut_records": cut_records}
    return census
