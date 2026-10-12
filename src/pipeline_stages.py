"""Pipeline stages — explicit, composable, per-responsibility units.

Phase 4 #1 of the architecture work. `TradingPipeline` was a 2600-line
god object whose three `run_*` methods each did data-fetching, LLM
orchestration, risk filtering, order execution, and audit logging
inline. Nothing could be tested in isolation; nothing could be reused
across sessions.

Here we extract the logical phases into stand-alone stages that take a
`RunContext` (explicit shared state), read/write specific fields on it,
and return it (or an early-exit dict) for the next stage.

Morning composes four stages:
  1. MorningResearchStage — parallel macro/news/tech/earnings fan-out
  2. DecisionStage         — L2..L8 memory + PM + Constructor
  3. RiskStage             — hard filter + correlation + RM review + mods
  4. ExecutionStage        — HOLD audit → SELLs → wait fills → BUYs

Midday and evening are *themselves* single-stage workflows (account
snapshot → review/report → log). They have no internal sub-pipeline
to compose, so they stay as TradingPipeline methods rather than being
wrapped in an artificial "stage of one".

Dependency injection pattern: research stage takes each provider/agent
by hand (demonstrates the pure form). Decision/Risk/Execution each take
a `pipeline` reference for the large surface of helpers they share with
TradingPipeline (_build_* memory layers, _filter_* risk helpers,
_order_accepted, _full_sell_qty, etc.). The pragmatic tradeoff: no
tangled re-plumbing of 15+ helpers just to say "zero coupling." Those
helpers are the right extraction boundary for a later phase.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from types import SimpleNamespace
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from src import evidence_gate
from src.refusal_errors import PriceReadFailed, SizingPriceUnavailable
from src.sentinel.entry_guard import record_swallowed
from src.soft_exit_never_blank import (
    record_refusal_count,
    soft_exit_heal_detail as _soft_exit_heal_detail,
)
from src.pipeline_stage_helpers import (  # noqa: F401
    _live_stops_from_heat,
    _macro_regime,
    _persist_evidence,
    record_stage,
)
from src.pipeline_candidate_records import _record_pipeline_event  # noqa: F401
from src.sentinel.order_attempts import record_order_attempt_from_event
from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs
from src.agents.portfolio_manager import PortfolioManagerAgent
from src.cost_circuit import PaidAnalysisSuspended
from src.data.macro import MacroCoverage
from src.data.event_calendar import (
    EventCalendarCoverage,
    FOMCCoverage,
    fetch_earnings_proximity,
    format_event_risk_block,
)
from src.data.levels import FAULT_NO_PRICE, FAULT_STALE_PRICE
from src.data.live_price import ONLY_STALE, resolve_live_price
from src.data.technical import compute_indicators
from src.models import (
    ANALYSIS_DROP_KIND as _ANALYSIS_DROP_KIND,
    DROP_CODE_UNSPECIFIED,
    NewsIntelligenceReport,
    Nomination,
    TechAnalysisResult,
    TechnicalIndicators,
    missing_stated_falsifier,
    open_target_missing_falsifier,
    parse_telemetry,
    SOFT_EXIT_HEAL_EVENT_REASON,
    SOFT_EXIT_MISSING_AFTER_RETRY,
)
from src.nominations import select_nominations
from src.portfolio_constructor import (
    CONSTRUCTOR_REFUSED_EVENT_REASON,
    LEVEL_BACKED_STOP_RULES,
)
from src.pipeline_context import RunContext
from src.storage.event_journal import DatabaseEventJournal, pipeline_event_fields

# Step 11 of docs/PIPELINE_SPLIT_PLAN.md (board item 210): these bodies moved
# out verbatim and are re-exported here so every original import path and
# every `monkeypatch.setattr(pipeline_stages, ...)` patch target is unchanged.
from src.pipeline_earnings_quality import (  # noqa: F401
    _EARNINGS_DATA_QUALITY_RED_FLAGS,
    _EARNINGS_NOT_DISCLOSED,
    _EARNINGS_XBRL_COMPARABLE_FIELDS,
    _EARNINGS_XBRL_DOLLAR_FLOOR,
    _EARNINGS_XBRL_EPS_FLOOR,
    _EARNINGS_XBRL_TOLERANCE_PCT,
    _FIGURE_RE,
    _UNIT_MULTIPLIERS,
    _classify_earnings_status,
    _earnings_analysis_has_real_figures,
    _earnings_data_quality_flags_problem,
    _earnings_field_disclosed,
    _earnings_xbrl_mismatch_fields,
    _parse_reported_figure,
)
from src.pipeline_gross_ceiling import _session_gross_ceiling  # noqa: F401
from src.pipeline_sizing import (  # noqa: F401
    _DEFAULT_RISK_BUDGET_PCT,
    _entry_deployment_budget,
    _execution_payoff_skip_reason,
    _fmt_shares,
    _fractional_sizing_allowed,
    _min_position_risk_pct,
    _qty_by_risk_budget,
    _risk_budget_pct,
    _single_name_execution_cap,
    _size_shares,
)

# A test that patches a moved name on THIS module must reach the object the
# moved code actually calls, which lives in the owning module. Without the
# write-through below the patch would rebind only this module's copy and
# silently no-op (e.g. `_size_shares`, called from `_qty_by_risk_budget`).
import sys as _sys  # noqa: E402
import types as _types  # noqa: E402

from src import pipeline_earnings_quality as _pipeline_earnings_quality  # noqa: E402
from src import pipeline_sizing as _pipeline_sizing  # noqa: E402

_MOVED_NAME_OWNERS = {
    **{n: _pipeline_earnings_quality for n in vars(_pipeline_earnings_quality) if not n.startswith("__")},
    **{n: _pipeline_sizing for n in vars(_pipeline_sizing) if not n.startswith("__")},
}
for _n in ("logging", "math", "re", "annotations", "logger"):
    _MOVED_NAME_OWNERS.pop(_n, None)

# The single `__getattr__`/write-through mirror lives at the END of this module
# and covers BOTH move sets. Defining a second one here would be shadowed by it
# and would silently stop mirroring the sizing / earnings-quality names.
from src.risk.constants import (
    REWARD_RISK_FLOOR,
    STARTER_POSITION_RISK_PCT,
)

if TYPE_CHECKING:
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.agents.macro_analyst import MacroAnalystAgent
    from src.agents.news_analyst import NewsAnalystAgent
    from src.agents.tech_analyst import TechAnalystAgent
    from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
    from src.data.smart_money import SmartMoneySource
    from src.config import AppConfig
    from src.data.earnings import EarningsDataProvider
    from src.data.event_calendar import (
        FOMCCalendarProvider,
        MacroEventCalendarProvider,
    )
    from src.data.macro import MacroDataProvider
    from src.data.macro_store import MacroStore
    from src.data.market import MarketDataProvider
    from src.data.news import NewsCoverage, NewsDataProvider
    from src.data.news_store import NewsStore
    from src.data.tech_store import TechStore
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline
    from src.storage.db import Database

logger = logging.getLogger(__name__)

#: Adverse-excursion bound on an entry limit, in basis points from the
#: verified reference. A BUY ceiling sits this far *above* the reference; a
#: SHORT floor sits this far *below* it — the same number, opposite side.
#: This is fillability parity, not a second risk budget. Raised from a
#: hardcoded 25 to a configurable 40 on 2026-08-27 after VLO proved 25bp is
#: tighter than a normal market open: the ask was 28bp above the reference
#: within seconds of 09:30, so the cap produced an unfillable limit and no
#: trade. 40bp still refuses to pay through a genuinely abnormal book — a
#: gap, a halt reopen, a fat spread — while tolerating ordinary opening
#: drift. Override in `settings.yaml` under `execution.max_entry_slippage_bps`.
MAX_ENTRY_SLIPPAGE_BPS = 40.0

#: Share of resolved tech analyses (or of the fetch universe, for the bars
#: variant) coming back with NO usable signal — empty `computed_levels`, or
#: no bars at all — above which a run is treated as a DATA FAILURE rather
#: than a quiet market, and pushed to the owner outside the session summary.
#: 1.0 is the unambiguous case: literally every symbol came back empty. That
#: can never be a legitimate market reading — `find_structural_levels`
#: refusing on EVERY name in one run means the thing that varies per-symbol
#: (each symbol's own chart) produced the same null result, which is a
#: property of the feed, not of 60-100 unrelated instruments.
LEVELS_BLIND_RUN_EMPTY_SHARE = 1.0

#: 0.5 is deliberately coarse, NOT a fitted percentile — see the long
#: comment on `_persist_levels_coverage` for why only one clean baseline run
#: exists to derive it from (2026-09-02: 1/64 resolved symbols, 1.6%, came
#: back with no computed level) and why n=1 cannot support a statistically
#: fit threshold. 50% sits roughly 30x that single observed baseline, far
#: above anything ordinary per-symbol noise (a thin IPO, a rangebound name)
#: should ever produce across a whole run, while still catching a partial
#: outage — a feed serving short/stale history to MOST but not literally
#: ALL requests — that the 1.0 rule alone would miss.
LEVELS_DEGRADED_RUN_EMPTY_SHARE = 0.5

#: Below this many resolved symbols (or fetch attempts), a share is noise —
#: one empty result out of three is 33% and means nothing. Production's
#: universe is 100+ symbols; this only guards small/degenerate universes
#: (tests, a misconfigured owner universe) from a false alarm, and is well
#: below anything the real desk runs.
LEVELS_COVERAGE_MIN_SAMPLE = 10


def _book_risk_inputs(ctx, total_value: float):
    """Per-symbol budget risk (% of equity) and correlation clusters.

    Spec §2.2. Both are already computed for the PM's own facts block — the
    heat roll-up in `src/risk/metrics.py` and the clusters in
    `src/data/correlation.py` — so the constructor rations the plan against
    precisely the numbers the plan was made against, rather than a second
    view assembled a moment later.

    Never returns `None` for the book (defect 2026-10-09: `None` made the
    constructor SKIP the 25% ceiling). When the book cannot be read — no PM
    facts, no account value, no heat — it is either charged holding by
    holding at full market value, or returned as an UNKNOWN book
    (`BookRiskPct.unknown` non-empty), on which the constructor refuses every
    risk-adding order by name while closes, trims, exits and stops pass.
    `clusters` is `None` whenever correlation clusters are unavailable.
    """
    from src.risk.metrics import BookRiskPct, full_value_charge

    facts = getattr(ctx, "facts", None)
    if facts is None or not (total_value > 0):
        logger.error(
            "risk budget: the book cannot be read (%s) — its risk is UNKNOWN; every new buy is refused this run",
            "no PM facts" if facts is None else f"account value {total_value!r}",
        )
        clusters = getattr(facts, "correlation_clusters", None) if facts is not None else None
        return (BookRiskPct({}, unknown={"*BOOK*"}), list(clusters) if clusters else None)
    heat = getattr(facts, "heat", None)
    clusters = getattr(facts, "correlation_clusters", None)
    existing: dict[str, float]
    try:
        if heat is None:
            raise RuntimeError("portfolio heat unavailable")
        existing = BookRiskPct(
            {row.symbol: row.budget_risk_dollars / total_value * 100 for row in heat.per_position},
            unknown=set(getattr(heat, "unknown_risk", ()) or ()),
        )
    except Exception as e:  # noqa: BLE001 — degrade to the worst case, never skip the ceiling
        # Defect 2026-10-09: this used to return None, which SKIPPED the
        # 25% ceiling for every trade. The worst case is still a view of
        # the book: each holding charged at its full market value, and a
        # holding with no price marked UNKNOWN (buys refused while so).
        record_stage(ctx, "book_risk_map", e)
        existing = _full_value_book(getattr(ctx, "positions", None), total_value, BookRiskPct, full_value_charge)
    else:
        record_stage(ctx, "book_risk_map")
    return (existing, list(clusters) if clusters else None)


def _full_value_book(positions, total_value: float, book_cls, charge_fn):
    """Every holding charged at full market value; unreadable positions → UNKNOWN."""
    if positions is None:
        logger.error("risk budget: the held book could not be read at all — every new buy is refused this run")
        return book_cls({}, unknown={"*BOOK*"})
    out: dict[str, float] = {}
    unknown: set[str] = set()
    for p in positions:
        sym = str(getattr(p, "symbol", "") or "").upper()
        if not sym:
            continue
        charge, is_unknown = charge_fn(
            getattr(p, "qty", 0.0), getattr(p, "current_price", None), getattr(p, "avg_entry", None)
        )
        out[sym] = charge / total_value * 100
        if is_unknown:
            logger.error("risk budget: %s has no usable price — its risk is UNKNOWN; new buys refused", sym)
            unknown.add(sym)
    return book_cls(out, unknown=unknown)


def _sizing_read(pipeline, read, arg, symbol: str, what: str):
    """``read(arg)``, or `SizingPriceUnavailable` when it raises: fail closed.

    A typed `PriceReadFailed` is the read failing after its own retry (the
    caller classifies it: this name or the feed). Any OTHER error is not a
    price fact at all -- a bug or an unexpected client error -- so it is
    recorded loudly (durable counted row with the traceback) before it
    refuses the sizing the same way; it is never silently treated as absence.
    """
    try:
        return read(arg)
    except Exception as exc:  # noqa: BLE001 -- every failure refuses; unexpected ones are recorded
        if not isinstance(exc, PriceReadFailed):
            logger.error(
                "%s sizing %s read raised an UNEXPECTED error -- refused: %s", symbol, what, exc, exc_info=True
            )
            record_swallowed(pipeline, f"sizing_price.{what.replace(' ', '_')}", exc, symbol=symbol)
        raise SizingPriceUnavailable(f"{symbol}: {what} read failed") from exc


def _today_sizing_price(pipeline, symbol) -> float | None:
    """A price the SHARE COUNT may divide the dollar allocation by, or None.

    docs/WORK.md item 120. The number of shares an entry buys is
    `dollars / price`, so this price is the DIVISOR of the allocation and a
    wrong one mis-sizes the position PROPORTIONALLY. It must therefore be a
    real TODAY PRINT (`LivePrice.is_today_print`), never a quote MID and
    never a prior-session last trade.

    This is deliberately stricter than `_today_order_price`, the FILL
    reference, where a live quote mid IS a legitimate marketable-limit
    reference mid-session (owner 2026-09-12) — bounding an order you are
    about to cross against the current book is a different act from setting
    how many shares to buy. `_today_order_price` accepts a quote mid; this
    refuses it. None is MEASURED absence; a read that FAILED raises
    `SizingPriceUnavailable` -- callers go through `src.sizing_refusal`.

    It accepts the SAME today prices the constructor sizes off, so the two
    stages agree: a real last-trade print, and — when there is none — today's
    forming SESSION or minute bar via `resolve_live_price` (a real intraday
    price on the entitled venue, never a quote mid). Without this second
    branch an IEX-thin name with a today bar but no print — item 120's exact
    population — would be sized and approved by the paid PM/Risk seats and
    then silently skipped here, wasting those seats.

    Test compatibility: when the broker is a MagicMock whose
    `get_latest_price_stamped` does not return a real `LivePrice` and whose
    `get_intraday_snapshots` does not return a usable payload, this falls
    through to the bare `get_latest_price` exactly as `_today_order_price`
    does, so the many MagicMock-broker execution tests keep their behaviour.
    The freshness gate only bites on a real stamped price / real snapshot.
    """
    broker = getattr(pipeline, "broker", None)

    # 1. A real today PRINT from the stamped getter is the best sizing ref.
    stamped_getter = getattr(broker, "get_latest_price_stamped", None)
    stamped_is_real = False
    if callable(stamped_getter):
        from src.execution.broker import LivePrice

        candidate = _sizing_read(pipeline, stamped_getter, symbol, symbol, "stamped")
        if isinstance(candidate, LivePrice):
            stamped_is_real = True
            if candidate.price and candidate.price > 0 and candidate.is_today_print:
                return float(candidate.price)

    # 2. No today print: accept today's forming SESSION/minute bar through the
    #    same resolver the constructor uses (never a quote mid), so both
    #    stages agree on a thin name that has a bar but no print.
    snap_getter = getattr(broker, "get_intraday_snapshots", None)
    if callable(snap_getter):
        snap_is_real = False
        snaps = _sizing_read(pipeline, snap_getter, [symbol], symbol, "snapshot")
        if isinstance(snaps, dict):
            snap_is_real = True
            resolved = resolve_live_price(snaps.get(symbol))
            if resolved.is_today_print:
                return float(resolved.price)
        # A REAL stamped price (real broker) that was a quote mid or stale,
        # and no usable today bar either: refuse rather than fall through to
        # the mid-capable bare getter.
        if stamped_is_real or snap_is_real:
            if stamped_is_real:
                logger.warning(
                    "%s sizing price refused: no today print and no today "
                    "session/minute bar — a quote mid or stale price cannot "
                    "set the share count",
                    symbol,
                )
            return None
    elif stamped_is_real:
        return None

    # 3. Test / back-compat: a MagicMock broker that returned neither a real
    #    LivePrice nor a real snapshot dict. Keep existing behaviour.
    getter = getattr(broker, "get_latest_price", None)
    if not callable(getter):
        return None
    live = _sizing_read(pipeline, getter, symbol, symbol, "bare price")
    if isinstance(live, (int, float)) and not isinstance(live, bool) and live > 0:
        return float(live)
    return None


# AUTO-RESUBMISSION IS DELIBERATELY NOT IMPLEMENTED — a live owner decision,
# not an oversight. The owner raised it as a possibility ("maybe there's
# other options like just wait again and resubmit... it could all be
# automatic"), which is a possibility to consider, not a ratified
# instruction, so the safe half is what ships: one bounded automatic
# reprice with no human in the loop, an end-of-session cancel, then an
# alert.
#
# Why the line is drawn exactly there. Repricing an EXISTING order is bounded
# by construction — one order stays one order, it can never cross the
# slippage ceiling the entry was approved against, and Alpaca's own replace
# semantics cap the blast radius. Submitting a BRAND NEW order after the
# original is gone is a different act with different failure modes: the
# original may fill a moment later (now two positions in one idea), the
# analysis the entry was approved on is by then minutes stale, and the
# resubmission is not covered by any budget the session already counted.
# Getting that wrong buys the same idea twice, which is the one failure this
# whole module is written to avoid.
#
# What it would need before shipping: an owner decision on whether a stale
# entry thesis may be re-entered at all without fresh analysis, and an
# idempotency key or equivalent so a resubmission cannot race the original.
# Neither exists today. Left as a follow-up.


def _check_levels_coverage(db: "Database", ctx: RunContext, analyses: list["TechAnalysisResult"]) -> None:
    """Record this run's structural-level coverage; alert if it looks blind.

    2026-09-02, closing a hole found while checking whether 2026-09-01's
    zero-trade day could recur through a silent data failure. It could not
    have BEEN that day: this run's own persisted evidence shows
    `TechAnalysisResult.computed_levels` did not exist in the code that ran
    that morning (every one of that day's 59 tech_analyst evidence rows
    lacks the key entirely, not just an empty list), and the true cause was
    the R/R-geometry defect in docs/OUTCOME.md — a widened ATR stop dividing
    into a target that was never derived from structure, unrelated to
    whether structure existed. **Do not cite this function as what happened
    2026-09-01.**

    What IS true: the fix for that defect, shipped the same night, made
    `computed_levels` load-bearing. `derive_structural_target`
    (src/data/levels.py) now hard-refuses any trade when it is empty
    (REFUSAL_NO_STRUCTURE) — correctly; a stop needs a level to sit on. But
    empty-because-the-bar-feed-is-dead and empty-because-the-chart-really-
    has-no-structure produce the identical `[]`, and until now neither this
    run's per-symbol coverage nor its bars-fetch success was kept anywhere
    a postmortem could read after the fact — only a per-symbol WARNING log
    line for the latter, gone at the next log rotation, and the former not
    even that. (2026-09-12: the per-symbol half is now also carried on
    `TechAnalysisResult.levels_coverage`, and the dead-feed case is a DATA
    fault — `FAULT_NO_STRUCTURE`, `data_fault` row, owner alert — rather
    than a refusal. This run-level check is unchanged and complementary.)

    `analyses` is scoped to RESOLVED symbols only (never-resolved symbols —
    an LLM parse failure — are `data_status["tech"]` partial/failed and the
    `analysis_parse_loss` advisory's job already; mixing that failure mode
    into a levels-coverage number would double-count it under a misleading
    label). The two share thresholds below are module constants —
    `LEVELS_BLIND_RUN_EMPTY_SHARE` / `LEVELS_DEGRADED_RUN_EMPTY_SHARE` — see
    their own comments for how they were derived and why 50% is a coarse
    line, not a fitted percentile.

    Best-effort and NEVER raises or blocks the research stage, matching
    `_persist_evidence`'s contract (which this calls): a coverage-tracking
    bug must never be able to stop a trading session, whatever it decides
    about alerting.
    """
    try:
        bars = ctx.tech_bars_coverage or {}
        universe = int(bars.get("universe") or 0)
        bars_missing = int(bars.get("bars_missing") or 0)

        resolved = len(analyses)
        levels_empty_symbols = sorted(a.symbol for a in analyses if not a.computed_levels)
        levels_empty = len(levels_empty_symbols)

        import json as _json

        _persist_evidence(
            db,
            run_id=ctx.run_id,
            agent_name="tech_analyst",
            kind="levels_coverage",
            scope="run",
            evidence_json=_json.dumps(
                {
                    "universe": universe,
                    "bars_fetched": int(bars.get("bars_fetched") or 0),
                    "bars_missing": bars_missing,
                    "bars_missing_symbols": bars.get("bars_missing_symbols") or [],
                    "resolved": resolved,
                    "levels_present": resolved - levels_empty,
                    "levels_empty": levels_empty,
                    "levels_empty_symbols": levels_empty_symbols,
                },
                sort_keys=True,
            ),
        )

        blind, degraded = [], []
        if universe >= LEVELS_COVERAGE_MIN_SAMPLE:
            share = bars_missing / universe
            if share >= LEVELS_BLIND_RUN_EMPTY_SHARE:
                blind.append(
                    f"bar fetch returned NOTHING for all {universe} "
                    f"universe symbol(s) — the data feed, not the market, "
                    f"is down"
                )
            elif share >= LEVELS_DEGRADED_RUN_EMPTY_SHARE:
                degraded.append(f"bar fetch failed for {bars_missing}/{universe} universe symbols ({share:.0%})")
        if resolved >= LEVELS_COVERAGE_MIN_SAMPLE:
            share = levels_empty / resolved
            if share >= LEVELS_BLIND_RUN_EMPTY_SHARE:
                blind.append(
                    f"every one of {resolved} analyzed symbol(s) came back "
                    f"with NO structural level — a live feed does not put "
                    f"every chart in a batch into that state at once"
                )
            elif share >= LEVELS_DEGRADED_RUN_EMPTY_SHARE:
                degraded.append(
                    f"{levels_empty}/{resolved} analyzed symbols came back with NO structural level ({share:.0%})"
                )
        if not (blind or degraded):
            return

        from src import notifier as _notifier

        header = "🔴 TECH DATA BLIND SPOT\n" if blind else "🟠 TECH DATA DEGRADED\n"
        _notifier.send_owner_alert(
            header + "; ".join(blind + degraded) + ".\n"
            "Every trade needs a structural level to set a stop against, so "
            "today's refusals may be a dead data feed wearing the costume "
            "of a quiet market rather than a genuine absence of setups. "
            "Check the market data provider before trusting a no-trade day."
        )
    except Exception as exc:  # noqa: BLE001
        record_stage(SimpleNamespace(db=db), "levels_coverage", exc)
    else:
        record_stage(SimpleNamespace(db=db), "levels_coverage")


def _alert_owner_protection_failed(pipeline, spec: dict, protection, entry_order_id: str) -> None:
    """Spec §11.1 guard 2 — a stop that did not land ALERTS THE OWNER.

    Fires on two states, and says which:

      * no stop at all — `place_entry_protection` exhausted its retries
        (guard 1) and returned nothing;
      * a partial cover — the broker took a stop for fewer shares than are
        held (today: the whole-share fallback for a fractional fill whose
        exact quantity the broker refused).

    Silent on the third state — protection placed, nothing uncovered — which
    is the overwhelmingly common one. An alert channel that fires on success
    is a channel the owner learns to swipe away.

    Deliberately does NOT fire when the entry filled zero shares: there is no
    position, so there is nothing to protect, and `place_entry_protection`
    returns None for that too. Waking a human for a BUY that simply did not
    fill is exactly how guard 2 gets turned off.

    Never raises.
    """
    try:
        symbol = spec.get("symbol", "?")
        stop_price = spec.get("stop_price")
        uncovered = 0.0
        if isinstance(protection, dict):
            try:
                uncovered = float(protection.get("uncovered_qty") or 0)
            except (TypeError, ValueError):
                uncovered = 0.0
            if uncovered <= 0:
                return
        if protection is None:
            # Distinguish "no stop" from "no fill". Only the first is an
            # emergency; the second is a normal, uneventful non-event.
            filled = None
            try:
                info = pipeline.broker.get_order_fill_info(entry_order_id) or {}
                filled = float(info.get("filled_qty") or 0)
            except Exception as exc:  # noqa: BLE001
                record_stage(pipeline, "protection_fill_info", exc)
                filled = None
            else:
                record_stage(pipeline, "protection_fill_info")
            if filled is not None and filled <= 0:
                return
            held = _fmt_shares(filled) if filled is not None else "an unknown number of"
            is_short = str(spec.get("side", "buy")).lower() != "buy"
            is_scale_in = bool(spec.get("cover_full_position"))
            if is_scale_in:
                remedy = (
                    "The resting protective sell was cancelled so this add "
                    "could submit. Place a stop over the FULL position now "
                    "or flatten. The coverage sweep will also try to repair "
                    "from the write-ahead recovery row."
                )
                body = (
                    "STOP NOT REARMED AFTER A SCALE-IN\n"
                    f"{symbol}: the entry filled ({held} share(s)) but the "
                    "protective sell covering the full position could not be "
                    "placed after every immediate retry. The position is "
                    "open at the broker with NOTHING standing watch.\n"
                    f"Intended stop: {stop_price}\n"
                    f"Entry order: {entry_order_id}\n"
                    f"{remedy}"
                )
            else:
                remedy = (
                    "An IMMEDIATE market cover is being submitted — a naked short "
                    "has unbounded loss and is not left to a sweep. Confirm it "
                    "landed."
                    if is_short
                    else "Place a stop manually or flatten the position. The 30-minute "
                    "coverage sweep will also attempt an automatic repair."
                )
                body = (
                    "🛑🛑🛑 NO STOP AT ALL\n"
                    f"{symbol}: the entry filled ({held} share(s)) but the "
                    "protective stop could not be placed after every immediate "
                    "retry. The position is open at the broker with NOTHING "
                    "standing watch.\n"
                    f"Intended stop: {stop_price}\n"
                    f"Entry order: {entry_order_id}\n"
                    f"{remedy}"
                )
        else:
            covered = protection.get("covered_qty")
            body = (
                "⚠️ STOP PARTIALLY COVERS THE POSITION\n"
                f"{symbol}: a protective stop was placed for "
                f"{_fmt_shares(covered)} share(s), but {_fmt_shares(uncovered)} "
                "share(s) of the fill are NOT covered by it — the broker "
                "refused a stop for the exact filled quantity.\n"
                f"Stop: {stop_price}\n"
                f"Entry order: {entry_order_id}\n"
                "The uncovered remainder is under one share. If this recurs, "
                "turn `execution.fractional_enabled` off."
            )
        from src import notifier as _notifier

        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "protection_alert", exc)
    else:
        record_stage(pipeline, "protection_alert")


def _alert_holding_discipline_block(
    pipeline,
    *,
    symbol: str,
    action: str,
    reasons: tuple[str, ...] | list[str],
) -> None:
    """Standalone owner alert: an exit was BLOCKED because the justification
    the Risk Manager gave for it is contradicted by the desk's own data.

    Own message, never bundled into the run summary, and severity carried in
    plain words — not colour and not an emoji standing in as the only
    signal (item 21, owner's alert-design rule). The leading emoji here is
    decoration on top of a plain-text header that already says everything,
    exactly as `_alert_protection_failure` above does.

    Deliberately NOT deduplicated, for the same reason
    `maybe_alert_data_quality` is not: the whole point of wiring this alert
    in alongside the 2026-09-04 escalation to a real veto is to MEASURE how
    often a stated exit reason turns out to be false. Suppressing repeats
    would destroy the count the owner asked for.

    Never raises — an alerting bug must not break the trading path it
    reports on.
    """
    try:
        detail = "\n".join(f"- The reasoning {r}." for r in reasons)
        body = (
            "🛑 TRADE BLOCKED — THE STATED REASON FOR SELLING WAS NOT TRUE\n"
            f"{symbol}: a {action} was proposed on a position whose thesis is "
            "still structurally intact, and the reason given for it does not "
            "match what actually happened today:\n"
            f"{detail}\n"
            "The trade was BLOCKED and no order was sent. This is not a "
            "judgement about whether selling is right — only that the "
            "specific justification given is contradicted by the data on "
            "record, so it cannot stand on that reason. If there is a real "
            "reason to exit, it can be proposed again next cycle citing it."
        )
        from src import notifier as _notifier

        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        record_stage(pipeline, "discipline_alert", exc)
    else:
        record_stage(pipeline, "discipline_alert")


def _record_scale_in_window_closed(pipeline, ctx, spec: dict, *, covered: bool) -> None:
    """Close the scale-in unprotected window with a measured duration.

    Board item 193. A scale-in cancels the resting protective stop so the add
    can reach the broker, which leaves the WHOLE held position — not just the
    add — with no stop until the rearm lands. This emits one event per cancel
    at the moment the rearm attempt returns, carrying:

      * `window_seconds` — broker cancel acknowledgement to broker rearm
        acknowledgement, both `time.monotonic()` inside the one run, so it
        bounds real exposure and never reflects a row's write time;
      * `held_qty_before` and `exposed_notional` — the size of what was naked;
      * `covered` — False when the rearm did NOT land, which means the window
        is still open when the event is written and the fail-closed owner
        alert below it is the thing that matters.
    Nothing is emitted when no cancel happened: a naked add has no window.
    """
    from src.execution.scale_in import unprotected_window_seconds

    seconds = unprotected_window_seconds(spec.get("cancel_confirmed_at"))
    if seconds is None:
        return
    held = abs(float(spec.get("held_qty_before") or 0.0))
    price = float(spec.get("reference_price") or 0.0)
    _record_pipeline_event(
        pipeline,
        ctx,
        spec.get("symbol"),
        "scale_in",
        "unprotected_window_closed" if covered else "unprotected_window_still_open",
        "rearm_acknowledged" if covered else "rearm_did_not_land",
        window_seconds=seconds,
        held_qty_before=held,
        exposed_notional=round(held * price, 2) if price > 0 else None,
        wal_row_id=spec.get("wal_row_id"),
        stop_price=spec.get("stop_price"),
    )


import sys as _sys
import types as _types

# --- Re-exports: the four stage classes moved to src/stage_*.py (item 210, step 10).
# Resolved lazily through module ``__getattr__`` so that importing a stage module
# first does not create a cycle, and so that a test patching
# ``src.pipeline_stages.RiskStage`` replaces the very object this module hands out.
_STAGE_CLASS_MODULES = {
    "MorningResearchStage": "src.stage_morning_research",
    "DecisionStage": "src.stage_decision",
    "RiskStage": "src.stage_risk",
    "ExecutionStage": "src.stage_execution",
    # The five RiskStage private helpers and their two constants moved with it.
    "_record_queued_earnings_refusals": "src.stage_risk",
    "_apply_sector_unresolved_alert": "src.stage_risk",
    "_reconcile_parse_loss": "src.stage_risk",
    "_parse_loss_advisories": "src.stage_risk",
    "_persist_dropped_reasons": "src.stage_risk",
    "ANALYSIS_DROP_KIND": "src.stage_risk",
    "UNIDENTIFIED_DROP_KEY": "src.stage_risk",
    "_ROTATION_SELL_REFUSED": "src.pipeline_rotation_exec",
    "_rotation_execution_enabled": "src.pipeline_rotation_exec",
    "_rotation_ranked_margin_enabled": "src.pipeline_rotation_exec",
    "_rotation_skip": "src.pipeline_rotation_exec",
    "_record_rotation_precheck": "src.pipeline_rotation_exec",
    "_apply_rotation_execution": "src.pipeline_rotation_exec",
    "_projected_sale_qty": "src.pipeline_rotation_exec",
    "_projected_post_sale_book": "src.pipeline_rotation_exec",
    "_scaled_position": "src.pipeline_rotation_exec",
    "_projected_post_sale_cash": "src.pipeline_rotation_exec",
    "_projected_entry_cost": "src.pipeline_rotation_exec",
    "_rotation_buy_leg_projected_refusal": "src.pipeline_rotation_exec",
    "_pending_cover_symbols": "src.pipeline_rotation_exec",
    "_rotation_sell_last": "src.pipeline_rotation_exec",
    "_rotation_sell_gate": "src.pipeline_rotation_exec",
    "_rotation_ranked_margin_sell_reason": "src.pipeline_rotation_exec",
    "_alert_rotation_executed": "src.pipeline_rotation_exec",
    "_drop_buys_sold_today_below_bar": "src.pipeline_rotation_exec",
    "_drop_rotation_buy_if_room_not_freed": "src.pipeline_rotation_exec",
    "_record_rotation_buy_leg_outcome": "src.pipeline_rotation_exec",
    "_alert_rotation_buy_leg_missing": "src.pipeline_rotation_exec",
    "_WAL_REPEG_SENTINEL": "src.pipeline_entry_orders",
    "_IN_FLIGHT_FILL_STATUSES": "src.pipeline_entry_orders",
    "_REPEG_OUTCOME_TEXT": "src.pipeline_entry_orders",
    "_entry_slippage_bps": "src.pipeline_entry_orders",
    "_trade_updates_already_started": "src.pipeline_entry_orders",
    "_fill_stream_enabled": "src.pipeline_entry_orders",
    "_known_entry_submit_budget_s": "src.pipeline_entry_orders",
    "_encode_entry_submit_window": "src.pipeline_entry_orders",
    "_submit_window_overrun": "src.pipeline_entry_orders",
    "_start_trade_updates_early": "src.pipeline_entry_orders",
    "_stop_trade_updates": "src.pipeline_entry_orders",
    "_pin_approved_entry_ceilings": "src.pipeline_entry_orders",
    "_adopt_stream_stall": "src.pipeline_entry_orders",
    "_warm_trade_updates": "src.pipeline_entry_orders",
    "_today_order_price": "src.pipeline_entry_orders",
    "_live_fill_price": "src.pipeline_entry_orders",
    "_repeg_settings": "src.pipeline_entry_orders",
    "_repeg_entry_order": "src.pipeline_entry_orders",
    "_alert_owner_entry_cancelled": "src.pipeline_entry_orders",
    "_alert_unmeasurable_symbols": "src.pipeline_entry_orders",
    "_session_candidate_ranking": "src.pipeline_entry_orders",
    "_dropped_since_proposal": "src.pipeline_entry_orders",
    "_record_constructor_drops": "src.pipeline_entry_orders",
    "_record_constructor_side_flips": "src.pipeline_entry_orders",
    "_record_realised_sector_weights": "src.pipeline_sector_weights",
    # The seat-evidence block moved to src/pipeline_seat_evidence.py.
    "_link_nominations_to_decision": "src.pipeline_seat_evidence",
    "_record_seat_stances": "src.pipeline_seat_evidence",
    "_collect_seat_nominations": "src.pipeline_seat_evidence",
    "_macro_analysis_as_dict": "src.pipeline_seat_evidence",
    "_stash_macro_parse_failure": "src.pipeline_seat_evidence",
    "_RISK_EDITABLE_FIELDS": "src.pipeline_seat_evidence",
    "_risk_edit_snapshot": "src.pipeline_seat_evidence",
    "_risk_event_for": "src.pipeline_seat_evidence",
    "_record_scale_advisory": "src.pipeline_seat_evidence",
    "_probe_sale_census": "src.pipeline_seat_evidence",
    "_apply_repeg": "src.pipeline_entry_orders",
    "_repoint_trade": "src.pipeline_entry_orders",
    "_delete_repeg_wal": "src.pipeline_entry_orders",
    # The candidate-accounting / heal-record block moved to src/pipeline_candidate_records.py.
    "_record_execution_skip": "src.pipeline_candidate_records",
    "_record_pipeline_event": "src.pipeline_candidate_records",
    "_PM_ACCOUNTING_SEAT": "src.pipeline_candidate_records",
    "_record_accounted_candidate": "src.pipeline_candidate_records",
    "_account_for_pm_candidates": "src.pipeline_candidate_records",
    "_record_heal_safely": "src.pipeline_candidate_records",
    # The soft-exit records block moved to src/pipeline_soft_exit_records.py.
    "_target_increase_missing_falsifier": "src.pipeline_soft_exit_records",
    "_targets_admitted_to_book": "src.pipeline_soft_exit_records",
    "_record_mechanical_soft_exit_restores": "src.pipeline_soft_exit_records",
    "_record_soft_exit_heals": "src.pipeline_soft_exit_records",
    "_record_soft_exit_missing_after_retry": "src.pipeline_soft_exit_records",
    "_record_soft_exit_refusal_count": "src.pipeline_soft_exit_records",
    "_isolate_empty_soft_exit_entries": "src.pipeline_soft_exit_records",
}

# Step 11's moved names (sizing + earnings quality) join the SAME dict, so one
# `__getattr__` and one write-through `__setattr__` serve every moved name.
_STAGE_CLASS_MODULES.update({_name: _owner.__name__ for _name, _owner in _MOVED_NAME_OWNERS.items()})


def __getattr__(name: str):
    module_path = _STAGE_CLASS_MODULES.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    obj = getattr(importlib.import_module(module_path), name)
    globals()[name] = obj
    return obj


def __dir__():
    return sorted(set(globals()) | set(_STAGE_CLASS_MODULES))


# --- Patch mirroring. The stage classes above moved to `src/stage_*.py` and
# import their shared helpers and module-level names FROM HERE, so each stage
# module holds its own binding. Tests patch names on `src.pipeline_stages`
# (`compute_indicators`, `_persist_evidence`, `_size_shares`,
# `_record_pipeline_event`, ...) and expect the moved code to call the patched
# object. Mirroring the assignment into any stage module that already holds
# that name keeps the patched object the SAME object on both sides, which is
# what the move is required to preserve. Nothing else about the name changes.
# The pieces lifted out of `ExecutionStage._run_session` into
# `src/stage_execution_parts/` import the same names from here, so they are
# mirrored exactly like the stage modules themselves.
# Listed literally (not discovered from sys.modules) so the patch-target audit
# can read the mirror's reach from the source.
_MIRRORED_PART_MODULES = {
    "src.stage_execution_parts.cover_loop": "src.stage_execution_parts.cover_loop",
    "src.stage_execution_parts.entry_geometry": "src.stage_execution_parts.entry_geometry",
    "src.stage_execution_parts.entry_quote": "src.stage_execution_parts.entry_quote",
    "src.stage_execution_parts.entry_sizing": "src.stage_execution_parts.entry_sizing",
    "src.stage_execution_parts.protect_entry_stops": "src.stage_execution_parts.protect_entry_stops",
    "src.stage_execution_parts.sell_loop": "src.stage_execution_parts.sell_loop",
    "src.stage_execution_parts.state": "src.stage_execution_parts.state",
}


class _StageMirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for module_path in set(_STAGE_CLASS_MODULES.values()) | set(_MIRRORED_PART_MODULES.values()):
            stage_module = _sys.modules.get(module_path)
            if stage_module is not None and name in vars(stage_module):
                setattr(stage_module, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for module_path in set(_STAGE_CLASS_MODULES.values()) | set(_MIRRORED_PART_MODULES.values()):
            stage_module = _sys.modules.get(module_path)
            if stage_module is not None and name in vars(stage_module):
                delattr(stage_module, name)


_sys.modules[__name__].__class__ = _StageMirroringModule
