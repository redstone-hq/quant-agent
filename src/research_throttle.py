"""Stop paying to hunt for new trades when the book has no room for one.

Owner ask, 2026-09-30: the desk runs several sessions a day looking for new
trades; when the portfolio is already full that hunting is largely wasted
paid model and paid search spend. It must still be "a balancing act" —
every holding must keep justifying its place on the NORMAL cadence.

So this module throttles the SEARCH, never the REVIEW:

* When the book is full, the research fan-out is narrowed to the names the
  desk already holds (plus this run's free, deterministic admissions —
  see below). Every holding still gets its full five-seat read on every
  normal session, because a holding is kept or sold on the same weighed
  view as a buy, not only judged at entry.
* The midday / close position-review sessions and the intraday check are
  untouched by this module. They do not hunt.

"Full" is NOT a picked number. It is read off the desk's own ratified
ceilings, so the throttle turns itself off the moment the book stops being
full — no schedule, no cadence dial, nothing to tune:

* `deployable_cash <= 0`   — nothing to buy with without borrowing.
* `deployed_pct >= risk.max_total_position_pct` — the invested ceiling.
* `gross_pct >= risk.max_gross_exposure_x * 100` — the §11.2 gross ceiling.

THE URGENCY LANE. A throttled session is not a blind session:

1. Every deterministic safety step — stop-coverage audit, protection
   restores, forced de-lever, gross-ceiling enforcement, fill and stop-out
   reconciliation — runs BEFORE the paid boundary and is not touched here.
2. Held names keep their full research, so a holding whose thesis has
   broken is still found, voiced and sold on the normal cadence.
3. Run-scoped admissions (`ctx.admitted_symbols`: SEC Form 4 smart-money
   and the universe screen) survive the narrowing. Those come from free,
   deterministic signals, so a genuinely urgent NEW name still reaches the
   seats even while the hunt is throttled.
4. The moment anything frees capital — a stop-out, a de-lever, a sale from
   the review — the ceilings above stop being breached and the very next
   session hunts normally again.

Fails OPEN: any uncertainty here returns "not full" and the full hunt runs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _f(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def full_book_reason(
    *,
    deployable_cash,
    deployed_pct,
    gross_pct,
    max_total_position_pct,
    max_gross_exposure_x,
) -> str | None:
    """Plain-English reason the book is full, or None if it has room.

    Every threshold is an existing ratified ceiling read from config. This
    function invents no number of its own.
    """
    cash = _f(deployable_cash)
    if cash is not None and cash <= 0:
        return "no deployable cash — a new position would have to be borrowed"

    deployed = _f(deployed_pct)
    invested_cap = _f(max_total_position_pct)
    if deployed is not None and invested_cap is not None and deployed >= invested_cap:
        return f"invested {deployed:.1f}% of the account against its own {invested_cap:.1f}% ceiling"

    gross = _f(gross_pct)
    gross_x = _f(max_gross_exposure_x)
    if gross is not None and gross_x is not None and gross >= gross_x * 100:
        return f"gross exposure {gross / 100:.2f}x against its own {gross_x:.2f}x ceiling"

    return None


def narrow_to_held(effective_symbols, held_symbols, admitted_symbols=None):
    """Research surface for a full book: held names + free admissions.

    Order is preserved from `effective_symbols` so the existing chunking and
    recovery budget behave exactly as before on the names that remain.
    """
    keep = {str(s).strip().upper() for s in (held_symbols or []) if str(s).strip()}
    keep |= {str(s).strip().upper() for s in (admitted_symbols or []) if str(s).strip()}
    if not keep:
        # Nothing held and nothing admitted cannot be a full book; refuse to
        # hand the seats an empty universe.
        return list(effective_symbols)
    return [s for s in effective_symbols if s in keep]


# ---------------------------------------------------------------------------
# Board item 177: do not ask the technical seat again when nothing it reads
# has moved.
#
# MEASURED 2026-10-01 against the production DB, read-only: of 723 stock-days
# the technical seat read more than once, 457 (63%) ended the day with the
# same rating AND the same conviction it opened with — 1,056 paid re-reads
# that returned an answer the desk already held. The other 266 stock-days DID
# change verdict intraday, which is exactly how a position that turns bad gets
# caught, so re-reading is not pointless and must not be rationed.
#
# Hence: NO timer, NO counter, NO cooldown, NO sampling rate. The only thing
# that may license skipping a read is the INPUTS not having moved.
#
# WHAT THE VERDICT IS A FUNCTION OF. The seat is a model call; its answer is a
# function of the user message and nothing else. `TechAnalystAgent.
# build_user_message` builds that message out of exactly six things, per
# symbol: the bar series and indicators (`symbols_data`), the prior-rating
# context line (`prior_ratings`), the valuation line (`valuations`), the
# live/forming-session block (`intraday_context`), and the two run-level macro
# strings. Those six ARE the input set, and this fingerprint covers all six.
#
# INPUTS vs OUTPUTS on the evidence row, settled by reading where each field
# is assigned in `_analyze_chunk`:
#   * OUTPUTS (the model emits them; they are the verdict or part of it):
#     rating, conviction, entry_price, reference_target, stop_loss,
#     support_levels, resistance_levels, setup_type, reasoning*, risk_reward.
#     `entry_price` on the row is the seat's PROPOSED entry, not an input —
#     the entry that IS an input is the prior position's, and it reaches the
#     seat through `prior_ratings`, which is fingerprinted.
#   * INPUT-DERIVED (set in Python from the bars, never asked of the model):
#     computed_levels, computed_level_touches, computed_level_bars,
#     levels_coverage, signal_bar_low, signal_bar_high, bars_available,
#     atr_14. These are deterministic functions of the bars, so the bars
#     themselves are fingerprinted instead and these come along for free.
# Only inputs key this cache.
#
# WHY NOT key on those recorded input-derived fields alone: replayed against
# the retained record (2026-10-01), a key built only from them matches on
# 1,652 re-reads — and 269 of those re-reads had a CHANGED verdict. They are
# all bar-derived, the bars do not move intraday, and so they cannot see the
# one input that does: the live price. A cache keyed on them would blind the
# desk to precisely the changes it exists to catch. The fingerprint below
# therefore covers the live/forming-session block too.
#
# FAILS OPEN, everywhere: anything unreadable, unrecognised, missing or
# ambiguous returns None, and a None fingerprint can never match anything, so
# the seat is asked. An extra call costs pennies; a stale verdict is a
# position that should have been sold.
#
# NO TOLERANCE, anywhere: floats are compared by their exact repr, which
# round-trips, so two prices are "the same" only when they are bit-identical.
# No rounding, no band, no "close enough".

import hashlib
import json

from src.util.time import et_today


class _Unfingerprintable(Exception):
    """Raised internally when some input cannot be canonicalised exactly."""


def _plain(value, depth: int = 0):
    """Canonicalise to exactly-comparable JSON primitives, or raise.

    Deliberately total-or-raise rather than best-effort: a silently dropped
    field is an input the cache would stop watching.
    """
    if depth > 12:
        raise _Unfingerprintable("too deep")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # repr round-trips a Python float exactly; no rounding is introduced
        # and therefore no tolerance band is created.
        if value != value or value in (float("inf"), float("-inf")):
            raise _Unfingerprintable("non-finite float")
        return "f:" + repr(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    for attr in ("isoformat",):
        fn = getattr(value, attr, None)
        if callable(fn):
            return str(fn())
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump(mode="json"), depth + 1)
    as_dict = getattr(value, "__dict__", None)
    if isinstance(as_dict, dict) and as_dict:
        return _plain(dict(as_dict), depth + 1)
    raise _Unfingerprintable(f"cannot canonicalise {type(value).__name__}")


#: The only fields of a stored prior rating that `TechAnalystAgent.
#: build_user_message` renders (its `_prior_line`). Everything else on the
#: stored entry (history, last_rating_date, and the fingerprint and verdict
#: stored beside it) is NOT an input: hashing it would make the fingerprint
#: depend on itself, so it could never match a stored one.
_PRIOR_FIELDS_THE_PROMPT_READS = (
    "rating",
    "conviction",
    "first_seen_date",
    "entry_price",
    "stop_loss",
    "reference_target",
)


def _prompt_read_prior(prior_rating):
    if prior_rating is None:
        return None
    if not isinstance(prior_rating, dict):
        raise _Unfingerprintable("prior rating is not a mapping")
    return _plain({k: prior_rating.get(k) for k in _PRIOR_FIELDS_THE_PROMPT_READS})


def tech_input_fingerprint(
    symbol: str,
    *,
    symbol_data,
    prior_rating=None,
    valuation=None,
    intraday=None,
    prior_macro_regime=None,
    prior_macro_outlook=None,
) -> str | None:
    """Fingerprint everything the technical seat reads about one symbol.

    Returns None — meaning ASK — whenever the inputs cannot be pinned down
    exactly, including when there are no bars at all.
    """
    try:
        if not symbol or not isinstance(symbol_data, dict):
            return None
        bars = symbol_data.get("bars")
        if not bars:
            # No bars is a fault, not a steady state. Ask.
            return None
        payload = {
            "symbol": str(symbol),
            # The whole submitted payload, not a chosen subset: a field added
            # to the prompt later is fingerprinted the day it is added.
            "symbol_data": _plain(symbol_data),
            "prior_rating": _prompt_read_prior(prior_rating),
            # The prompt renders the prior rating's AGE against today, so the
            # same stored entry reads differently tomorrow.
            "today": str(et_today()),
            "valuation": _plain(valuation),
            "intraday": _plain(intraday),
            "macro_regime": _plain(prior_macro_regime),
            "macro_outlook": _plain(prior_macro_outlook),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (_Unfingerprintable, TypeError, ValueError, RecursionError) as exc:
        logger.info(
            "tech re-read cache: %s has no usable input fingerprint (%s) — asking the seat",
            symbol,
            exc,
        )
        return None
    except Exception as exc:  # pragma: no cover - fail open on anything
        logger.warning(
            "tech re-read cache: unexpected fingerprint failure for %s (%s) — asking the seat",
            symbol,
            exc,
        )
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def carry_unchanged_tech_reads(fingerprints, prior_entries) -> dict[str, dict]:
    """{symbol: stored verdict} for symbols whose every input is unchanged.

    A symbol appears here only when all of: this run produced a fingerprint,
    the store holds one, the two are equal, and the stored verdict is
    readable. Anything else is left out, which means the seat is asked.
    """
    out: dict[str, dict] = {}
    if not isinstance(fingerprints, dict) or not isinstance(prior_entries, dict):
        return out
    for symbol, fingerprint in fingerprints.items():
        if not isinstance(fingerprint, str) or not fingerprint:
            continue
        entry = prior_entries.get(symbol)
        if not isinstance(entry, dict):
            continue
        stored = entry.get("input_fingerprint")
        if not isinstance(stored, str) or stored != fingerprint:
            continue
        verdict = entry.get("last_result")
        if not isinstance(verdict, dict):
            continue
        if verdict.get("symbol") != symbol:
            continue
        if not verdict.get("rating") or not verdict.get("conviction"):
            continue
        out[symbol] = dict(verdict)
    return out
