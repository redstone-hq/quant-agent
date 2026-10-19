"""Morning research stage (split out of src/pipeline_stages.py, step 10).

Moved verbatim in the staged split (board item 210, step 10). No behaviour change:
the class body below is byte-for-byte the text that used to live in
``src/pipeline_stages.py``, and ``src.pipeline_stages`` re-exports it so every
existing import path and every ``src.pipeline_stages.MorningResearchStage`` patch target
still resolves to this same object.
"""

from __future__ import annotations

from concurrent.futures import wait
from typing import TYPE_CHECKING

from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    MacroCoverage,
    NewsIntelligenceReport,
    PaidAnalysisSuspended,
    RunContext,
    TechAnalysisResult,
    ThreadPoolExecutor,
    _check_levels_coverage,
    _classify_earnings_status,
    _collect_seat_nominations,
    _persist_evidence,
    _probe_sale_census,
    _record_pipeline_event,
    agent_log_kwargs,
    compute_indicators,
    copy_context,
    evidence_gate,
    logger,
    missing_stated_falsifier,
    parse_telemetry,
    seat_acceptance_kwargs,
    select_nominations,
)
from src.data.smart_money_cluster import find_cluster_census
from src.nomination_evidence import persist_nomination_summary
from src.stage_risk import _persist_dropped_reasons  # noqa: F401  moved with RiskStage
from src.sentinel.morning_guarded import record_morning_fault

if TYPE_CHECKING:
    from src.agents.earnings_analyst import EarningsAnalystAgent
    from src.agents.macro_analyst import MacroAnalystAgent
    from src.agents.news_analyst import NewsAnalystAgent
    from src.agents.tech_analyst import TechAnalystAgent
    from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
    from src.data.smart_money import SmartMoneySource
    from src.config import AppConfig
    from src.data.earnings import EarningsDataProvider
    from src.data.macro import MacroDataProvider
    from src.data.macro_store import MacroStore
    from src.data.market import MarketDataProvider
    from src.data.news import NewsCoverage, NewsDataProvider
    from src.data.news_store import NewsStore
    from src.data.tech_store import TechStore
    from src.models import TradeDecision
    from src.pipeline import TradingPipeline


class MorningResearchStage:
    """Parallel data + LLM fan-out at morning open.

    Produces on ctx:
      macro_summary, macro_analysis, news_intel, analyses, earnings_results,
      symbols_bars, valuations, data_status

    Uses a ThreadPoolExecutor for the research branches: macro, technical,
    earnings and smart money in parallel; news starts once technical answers
    (its per-symbol names come from the chart seat's buy reads).
    Failures are isolated so one bad branch doesn't abort the rest.
    """

    def __init__(
        self,
        *,
        config: "AppConfig",
        db: "Database",
        market: "MarketDataProvider",
        macro: "MacroDataProvider",
        news_provider: "NewsDataProvider",
        news_store: "NewsStore",
        macro_store: "MacroStore",
        tech_store: "TechStore",
        earnings_provider: "EarningsDataProvider",
        macro_analyst: "MacroAnalystAgent",
        news_analyst: "NewsAnalystAgent",
        tech_analyst: "TechAnalystAgent",
        earnings_analyst: "EarningsAnalystAgent",
        has_actionable_signal_fn,
        run_news_update_fn,
        load_earnings_analyses_fn,
        smart_money_provider: "SmartMoneySource | None" = None,
        smart_money_analyst: "SmartMoneyAnalystAgent | None" = None,
        admit_smart_money_candidates_fn=None,
        admit_nominated_candidates_fn=None,
        admit_screened_universe_fn=None,
        live_session_context_fn=None,
    ):
        self.config = config
        self.db = db
        self.market = market
        # (symbols) -> {sym: snapshot | {"live_unavailable": reason}}; {}
        # outside regular hours. Optional so existing construction sites
        # keep working; absent means "no live price" (pre-2026-09-14).
        self._live_session_context = live_session_context_fn
        self.macro = macro
        self.news_provider = news_provider
        self.news_store = news_store
        self.macro_store = macro_store
        self.tech_store = tech_store
        self.earnings_provider = earnings_provider
        self.macro_analyst = macro_analyst
        self.news_analyst = news_analyst
        self.tech_analyst = tech_analyst
        self.earnings_analyst = earnings_analyst
        self.smart_money_provider = smart_money_provider
        self.smart_money_analyst = smart_money_analyst
        self._admit_smart_money_candidates = admit_smart_money_candidates_fn
        # Phase 9 — same shape as admit_smart_money_candidates_fn:
        # (list[str] symbols) -> (admitted: set[str], details: dict[str,dict]).
        # Shares the same deterministic gate under the hood
        # (AdmissionService._evaluate_external_admission_gates); this is a
        # SEPARATE injected callable (not reused directly) because the two
        # callers decide WHICH symbols are worth gating differently — one
        # groups/ranks SEC Form 4 rows, the other consumes an
        # already-capped nomination candidate list.
        self._admit_nominated_candidates = admit_nominated_candidates_fn
        # Universe screen (src/universe_screen.py): (positions) ->
        # (symbols, details) for this session's share of the screened,
        # persisted universe. Returns nothing while the screen is off.
        self._admit_screened_universe = admit_screened_universe_fn
        # Injected callables so we don't duplicate pre-filter / news / earnings
        # orchestration logic. Those still live on TradingPipeline for now
        # because they touch shared state we haven't finished extracting.
        self._has_actionable_signal = has_actionable_signal_fn
        self._run_news_update = run_news_update_fn
        self._load_earnings_analyses = load_earnings_analyses_fn

    def _live_context(self, symbols: list[str]) -> dict[str, dict]:
        """Injected live-session read; {} when not wired or on failure."""
        if self._live_session_context is None:
            return {}
        try:
            return self._live_session_context(symbols) or {}
        except Exception as exc:  # noqa: BLE001
            record_morning_fault(self, "live_session_context", exc)
            return {}

    @staticmethod
    def _live_price_kwarg(live_context: dict, symbol: str) -> dict:
        """`{"live_price": x}` only when a usable live price exists, so an
        injected prefilter with the old 4-argument signature still works
        outside market hours.

        Reads `live_price` — the freshness-RESOLVED number
        `_live_session_context` publishes (docs/WORK.md item 120) — not the
        raw provider `last_price`, which can be a prior session's print.
        """
        ic = live_context.get(symbol) or {}
        price = ic.get("live_price")
        if ic.get("live_unavailable") or not isinstance(price, (int, float)) or price <= 0:
            return {}
        return {"live_price": price}

    def run(self, ctx: RunContext) -> RunContext:
        logger.info("=== Stage: MorningResearch ===")
        data_status: dict[str, str] = {}
        try:
            prior_macro_state = self.macro_store.load_last_state() or {}
        except Exception as e:
            record_morning_fault(self, "prior_macro_state", e)
            prior_macro_state = {}
        try:
            news_narrative = self.news_store.load_macro_narrative()
        except Exception as e:
            record_morning_fault(self, "macro_narrative", e)
            news_narrative = None

        smart_config = getattr(self.config, "smart_money", None)
        smart_money_observations = []
        smart_money_provider_error = None
        if smart_config and smart_config.enabled and self.smart_money_provider:
            try:
                smart_money_observations, smart_money_provider_error = self.smart_money_provider.fetch(
                    self.config.trading.universe
                )
            except Exception as exc:
                record_morning_fault(self, "smart_money_fetch", exc)
                smart_money_provider_error = f"provider_error:{type(exc).__name__}"
        ctx.smart_money_observations = smart_money_observations
        if smart_money_observations and self._admit_smart_money_candidates:
            try:
                admitted, admissions = self._admit_smart_money_candidates(
                    smart_money_observations,
                )
                ctx.admitted_symbols = {str(symbol).strip().upper() for symbol in admitted if str(symbol).strip()}
                ctx.smart_money_admissions = dict(admissions or {})
            except Exception as exc:
                # Admission uncertainty fails closed; the observations can
                # still be rendered as research evidence.
                record_morning_fault(self, "smart_money_admission", exc)
                ctx.admitted_symbols = set()
                ctx.smart_money_admissions = {}
        configured_symbols = [
            str(symbol).strip().upper() for symbol in self.config.trading.universe if str(symbol).strip()
        ]
        ranked, screened_details = [], {}
        if self._admit_screened_universe:
            try:
                ranked, screened_details = self._admit_screened_universe(
                    getattr(ctx, "positions", None) or [],
                )
            except Exception as exc:  # noqa: BLE001
                # Fails closed: no screened name this session, nothing else lost.
                record_morning_fault(self, "screened_admission", exc)
                ranked, screened_details = [], {}
        # Names a non-technical thesis seat already backs (SEC Form-4
        # admissions), captured before the screened names join
        # ctx.admitted_symbols; the news seat's per-symbol slots go to
        # chart-buy names outside this set first.
        sec_admitted_symbols = set(ctx.admitted_symbols)
        effective_symbols = order_morning_symbols(
            self,
            ctx,
            configured_symbols=configured_symbols,
            ranked=ranked,
            screened_details=screened_details,
            top_n=int(self.config.universe_screen.morning_top_n) if screened_details else 0,
        )

        # FULL-BOOK SEARCH THROTTLE (src/research_throttle.py, owner ask
        # 2026-09-30). Hunting for new trades costs paid model and paid
        # search on every session; when the book has no room for a new
        # position that spend buys nothing. When — and only when — the book
        # is full against its OWN ratified ceilings, the research surface
        # narrows to the names already held plus this run's free,
        # deterministic admissions. The REVIEW of every holding keeps its
        # normal cadence, because a holding is judged on the same weighed
        # view as a buy, not only at entry. Fails open: any problem here runs the full hunt.
        ctx.search_throttled_reason = None
        try:
            from src.research_throttle import full_book_reason, narrow_to_held

            positions = getattr(ctx, "positions", None) or []
            equity = float(getattr(ctx, "total_value", 0.0) or 0.0)
            held, deployed_usd, gross_usd = [], 0.0, 0.0
            for pos in positions:
                symbol = getattr(pos, "symbol", None)
                if symbol is None and isinstance(pos, dict):
                    symbol = pos.get("symbol")
                symbol = str(symbol or "").strip().upper()
                if symbol:
                    held.append(symbol)
                value = getattr(pos, "market_value", None)
                if value is None and isinstance(pos, dict):
                    value = pos.get("market_value")
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                deployed_usd += abs(value)
                gross_usd += abs(value)
            risk_cfg = getattr(self.config, "risk", None)
            reason = None
            if equity > 0 and held:
                reason = full_book_reason(
                    deployable_cash=getattr(ctx, "deployable_cash", None),
                    deployed_pct=deployed_usd / equity * 100,
                    gross_pct=gross_usd / equity * 100,
                    max_total_position_pct=getattr(risk_cfg, "max_total_position_pct", None),
                    max_gross_exposure_x=getattr(risk_cfg, "max_gross_exposure_x", None),
                )
            if reason:
                before = len(effective_symbols)
                effective_symbols = narrow_to_held(
                    effective_symbols,
                    held,
                    getattr(ctx, "admitted_symbols", None),
                )
                ctx.search_throttled_reason = reason
                logger.info(
                    "Full-book search throttle: %s — research surface %d -> %d "
                    "symbols (held + free admissions only); holdings review "
                    "unchanged.",
                    reason,
                    before,
                    len(effective_symbols),
                )
        except Exception as exc:  # noqa: BLE001 - never block research
            record_morning_fault(self, "search_throttle", exc)

        for observation in smart_money_observations:
            symbol = str(getattr(observation, "symbol", "") or "").strip().upper()
            for field_name, field_value in (
                (
                    "in_trading_universe",
                    symbol in set(configured_symbols) or symbol in ctx.admitted_symbols,
                ),
                ("transient_admitted", symbol in ctx.admitted_symbols),
            ):
                try:
                    setattr(observation, field_name, field_value)
                except Exception:
                    pass
        for symbol, admission in ctx.smart_money_admissions.items():
            import json as _json

            screened = admission.get("reason") == "universe_screen_admission"
            _persist_evidence(
                self.db,
                run_id=ctx.run_id,
                agent_name="universe_screen" if screened else "smart_money_analyst",
                kind="admission",
                scope="symbol",
                symbol=symbol,
                evidence_json=_json.dumps(admission, sort_keys=True),
            )
            # The deterministic admission record has its own ``reason``
            # field (for example ``material_sec_form4_purchase``).  Keep it
            # as admission detail instead of splatting it over the pipeline
            # event's positional ``reason`` argument.  Passing both used to
            # raise before any research agent ran, aborting a natural morning
            # session precisely when an external candidate qualified.
            admission_details = dict(admission)
            admission_reason = admission_details.pop("reason", None)
            _record_pipeline_event(
                self,
                ctx,
                symbol,
                "opportunity",
                "admitted",
                "universe_screen_admission" if screened else "smart_money_form4_admission",
                admission_reason=admission_reason,
                **admission_details,
            )

        def _run_macro():
            macro_summary = self.macro.get_macro_summary()
            # Side channel, not part of macro_summary's own shape — see
            # MacroCoverage's docstring (src/data/macro.py) for why
            # get_macro_summary() itself still returns a bare dict.
            macro_coverage = self.macro.last_coverage
            logger.info(
                "Macro data: VIX=%s, HY OAS=%sbps, CPI core YoY=%s, UNRATE=%s",
                macro_summary.get("vix", {}).get("current"),
                macro_summary.get("credit_spread", {}).get("current_bps"),
                macro_summary.get("inflation", {}).get("core_cpi_yoy"),
                macro_summary.get("unemployment", {}).get("current"),
            )
            analysis, result = self.macro_analyst.analyze(
                macro_summary=macro_summary,
                universe=effective_symbols,
                last_state=prior_macro_state,
                news_narrative=news_narrative,
                macro_coverage=macro_coverage,
            )
            if analysis is not None and macro_coverage is not None:
                # Board item 119, second criterion. Stamp the fetch record
                # onto the verdict BEFORE anything persists, carries or
                # renders it — `save_last_state` below is the first of those
                # and the reason the stamp has to happen here rather than at
                # any single display site. See
                # `MacroCoverage.verdict_stamp()` for why the run-scoped
                # `data_status["macro"]` a few hundred lines down does not
                # already cover this.
                try:
                    state, note = macro_coverage.verdict_stamp()
                    analysis.coverage_state = state
                    analysis.coverage_note = note
                    if state != "complete":
                        logger.warning(
                            "Macro verdict formed on an incomplete set — "
                            "regime=%s confidence=%s stamped coverage_state=%s (%s)",
                            analysis.regime,
                            analysis.confidence,
                            state,
                            note,
                        )
                except Exception as e:  # noqa: BLE001 — a stamp must never lose the verdict
                    record_morning_fault(self, "macro_coverage_stamp", e)
            if analysis:
                try:
                    from src.data.macro_store import series_prints_from_summary

                    self.macro_store.save_last_state(
                        analysis.model_dump(),
                        series_prints=series_prints_from_summary(
                            macro_summary,
                            freshness=getattr(self.macro, "_run_freshness", None),
                        ),
                    )
                except Exception as e:
                    record_morning_fault(self, "macro_last_state", e)
            return (
                macro_summary,
                analysis,
                result,
                macro_coverage,
            )

        # Filled by `_run_news` below (same thread-pool fan-out), read after
        # the future resolves to file a per-stock record of an unreadable
        # news answer. Board item 152.
        news_symbols_asked: set[str] = set()

        def _run_news(chart_buy_symbols=()):
            # Per-symbol news selection. Runs AFTER the technical seat
            # (2026-10-19 fix): the news seat's capped per-symbol list
            # (news.per_symbol_max_symbols) is held positions first, then
            # the names the chart seat just rated buy/strong_buy that no
            # other thesis seat backs yet (not an SEC Form-4 admission),
            # then the rest of this run's admitted candidates. Before this
            # the list was held + admitted names in research order, chosen
            # before the chart seat answered, so of ~100 screened names the
            # 15 slots went to the top scores whatever the chart said, and a
            # chart-buy name had no thesis seat for the conviction bar
            # (16 Oct: 100 screened, 8 chart-buy, 0 ranked). Every list is in
            # a stable, non-set order (broker snapshot order, then research
            # order), so the selection is reproducible in the rehearsal rig.
            held = [str(getattr(p, "symbol", "")).strip().upper() for p in ctx.positions if getattr(p, "qty", 0)]
            held = [s for s in held if s]
            chart_buys = set(chart_buy_symbols)
            unbacked_buys = [s for s in effective_symbols if s in chart_buys and s not in sec_admitted_symbols]
            admitted = [s for s in effective_symbols if s in ctx.admitted_symbols]
            candidates = list(dict.fromkeys(unbacked_buys + admitted))
            # Board item 152: remember WHICH names this seat was asked about,
            # so that if its answer comes back unreadable the loss can be
            # filed per-stock rather than as a bare log line and a count.
            news_symbols_asked.update(held)
            news_symbols_asked.update(candidates)
            try:
                return self._run_news_update(
                    ctx.run_id,
                    session="morning",
                    universe=effective_symbols,
                    held_symbols=held,
                    candidate_symbols=candidates,
                )
            except TypeError as exc:
                # Test doubles (and any future caller) may inject a
                # run_news_update_fn with a narrower signature than the real
                # method — this pre-dates per-symbol news (see the original
                # 'universe' fallback this generalizes). Any excess-kwarg
                # TypeError here can only come from the CALL SITE not
                # matching the injected callable's signature, never from
                # inside a correctly-implemented _run_news_update, so
                # retrying with the minimal 2-arg call is safe.
                if "unexpected keyword argument" not in str(exc):
                    raise
                return self._run_news_update(ctx.run_id, session="morning")

        def _run_tech():
            all_symbols_data = []
            symbols_bars: dict[str, list] = {}
            # Counted, not just logged (2026-09-02). "No data for %s,
            # skipping" used to be the ONLY trace a bar fetch ever failed —
            # gone the moment the log rotated, and never summed across the
            # run, so a feed outage that silently dropped every symbol read
            # exactly like a quiet market. See ctx.tech_bars_coverage and
            # `_check_levels_coverage` below for where this is used.
            bars_missing_symbols: list[str] = []
            for symbol in effective_symbols:
                bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days)
                if not bars:
                    logger.warning("No data for %s, skipping", symbol)
                    bars_missing_symbols.append(symbol)
                    continue
                indicators = compute_indicators(symbol, bars)
                all_symbols_data.append({"symbol": symbol, "bars": bars, "indicators": indicators})
                symbols_bars[symbol] = bars
            ctx.symbols_bars = symbols_bars
            ctx.tech_bars_coverage = {
                "universe": len(effective_symbols),
                "bars_fetched": len(all_symbols_data),
                "bars_missing": len(bars_missing_symbols),
                "bars_missing_symbols": bars_missing_symbols,
            }
            # Completed bars end at the previous close while the session is
            # open; price-vs-level judgement needs the live price (2026-09-14).
            live_context = self._live_context([s["symbol"] for s in all_symbols_data])
            symbols_data = [
                s
                for s in all_symbols_data
                if (
                    s["symbol"] in ctx.admitted_symbols
                    or self._has_actionable_signal(
                        s["indicators"],
                        s["symbol"],
                        s["bars"],
                        ctx.positions,
                        **self._live_price_kwarg(live_context, s["symbol"]),
                    )
                )
            ]
            logger.info(
                "Tech pre-filter: %d/%d symbols have actionable signals",
                len(symbols_data),
                len(all_symbols_data),
            )
            for candidate in symbols_data:
                _record_pipeline_event(
                    self,
                    ctx,
                    candidate["symbol"],
                    "opportunity",
                    "discovered",
                    "actionable_technical_prefilter",
                )
            if not symbols_data:
                return {}, None
            prior_ratings = self.tech_store.load()
            valuations: dict[str, dict] = {}
            for s in symbols_data:
                sym = s.get("symbol")
                if sym:
                    try:
                        valuations[sym] = self.market.get_valuation_metrics(sym)
                    except Exception as e:
                        record_morning_fault(self, "valuation", e, symbol=sym)
            ctx.valuations = valuations
            # analyses_map is guaranteed to carry every symbol in
            # symbols_data as a key (2026-08-19 Tech batch-response
            # symbol-loss fix) — a TechAnalysisResult on success, or None
            # for a symbol that failed to resolve even after tech_analyst's
            # own bounded retry. Filter before touching real analyses;
            # `analyses_map` itself (None values intact) is still returned
            # so the caller can see and report the failed count instead of
            # it silently vanishing.
            analyses_map, ta_res = self.tech_analyst.analyze_batch(
                symbols_data,
                prior_ratings=prior_ratings,
                valuations=valuations,
                prior_macro_regime=prior_macro_state.get("regime"),
                prior_macro_outlook=prior_macro_state.get("equity_outlook"),
                intraday_context={
                    s["symbol"]: live_context[s["symbol"]] for s in symbols_data if s["symbol"] in live_context
                },
            )
            ctx.tech_unreadable = dict(getattr(self.tech_analyst, "last_unreadable", None) or {})
            ctx.tech_unanswered = set(getattr(self.tech_analyst, "last_unanswered", None) or set())
            ctx.tech_unanswered = set(getattr(self.tech_analyst, "last_unanswered", None) or set())
            resolved = [a for a in analyses_map.values() if a is not None]
            if resolved:
                try:
                    self.tech_store.update(resolved)
                except Exception as e:
                    record_morning_fault(self, "tech_store_update", e)
                ages = self.tech_store.compute_ages([a.symbol for a in resolved])
                for analysis in resolved:
                    if analysis.symbol in ages:
                        analysis.signal_age_days = ages[analysis.symbol]
            return analyses_map, ta_res

        def _load_earnings():
            try:
                return self._load_earnings_analyses(
                    ctx.run_id,
                    session="morning",
                    ctx=ctx,
                    universe=effective_symbols,
                )
            except TypeError as exc:
                if "unexpected keyword argument 'universe'" not in str(exc):
                    raise
                return self._load_earnings_analyses(
                    ctx.run_id,
                    session="morning",
                    ctx=ctx,
                )

        def _run_smart_money():
            if (
                not smart_config
                or not smart_config.enabled
                or not self.smart_money_provider
                or not self.smart_money_analyst
            ):
                return [], None, smart_money_provider_error, None, []
            if not smart_money_observations:
                return [], None, smart_money_provider_error, None, []
            findings, result, analysis_error, gaps = self.smart_money_analyst.analyze_with_gaps(
                smart_money_observations,
            )
            return findings, result, smart_money_provider_error, analysis_error, gaps

        logger.info("Starting parallel: macro + tech + earnings + smart money; news after tech")
        with ThreadPoolExecutor(max_workers=5) as ex:
            # ContextVar values do not automatically flow into executor
            # workers. Give every branch its own copied context so breaker
            # reservations retain this run_id/mode even if another scheduler
            # job overlaps in the parent process.
            macro_future = ex.submit(copy_context().run, _run_macro)
            tech_future = ex.submit(copy_context().run, _run_tech)
            earnings_future = ex.submit(copy_context().run, _load_earnings)
            smart_money_future = ex.submit(copy_context().run, _run_smart_money)
            # News waits for the chart seat (2026-10-19): its per-symbol
            # names are chosen from the chart seat's buy/strong_buy reads.
            # Still ONE news call, overlapping whatever macro / earnings /
            # smart money are still doing. A failed chart seat falls back to
            # the held + admitted selection; its own error surfaces below.
            wait([tech_future])
            chart_buy_symbols: list[str] = []
            if tech_future.exception() is None:
                tech_map = (tech_future.result() or ({}, None))[0] or {}
                chart_buy_symbols = [
                    sym
                    for sym, analysis in tech_map.items()
                    if analysis is not None and getattr(analysis, "rating", None) in ("buy", "strong_buy")
                ]
            news_future = ex.submit(copy_context().run, _run_news, chart_buy_symbols)

        # How much of the watched set the pre-market Form 4 pass actually
        # read, per issuer. No network. The analyst's answer covers only the
        # names read through; below, a clean status is downgraded to
        # `partial` when any watched name still has unread filings, so the
        # seat never reports `ok` / `empty` over filings nobody read.
        sm_coverage = None
        coverage_probe = getattr(self.smart_money_provider, "form4_coverage", None)
        if smart_config and smart_config.enabled and callable(coverage_probe):
            try:
                sm_coverage = coverage_probe()
                if not isinstance(sm_coverage, dict):
                    sm_coverage = None
            except Exception as exc:  # noqa: BLE001
                record_morning_fault(self, "smart_money_coverage", exc)
                sm_coverage = {"known": False, "error": type(exc).__name__}
        # How old the congressional evidence is (newest disclosure, newest
        # trade, each source's copy). No network; None when that feed is off.
        sm_congressional = None
        congress_probe = getattr(self.smart_money_provider, "congressional_freshness", None)
        if smart_config and smart_config.enabled and callable(congress_probe):
            try:
                sm_congressional = congress_probe()
            except Exception as exc:  # noqa: BLE001
                record_morning_fault(self, "congressional_freshness", exc)
                sm_congressional = {"known": False, "error": type(exc).__name__}
        # Board item 126. EDGAR publishes its own count of the Form 4s filed
        # on each day. Until this shipped, a fetch that came back with
        # nothing because it was BROKEN and a day on which genuinely nobody
        # filed produced identical evidence — zero rows, no provider error,
        # status "ok". `verified` is the fetch saying it read EDGAR's own
        # count and walked it; unverified means the desk cannot tell those
        # two apart, which is not the same claim as "no signal found".
        #
        # This deliberately does NOT fire on a scan that spent its own
        # budget or deadline: those are the desk's bounded choices, their
        # residue is already reported through `unread` below, and treating
        # them as failure would make the seat degraded every day — the harm
        # board item 126 names in its own text. See
        # `UNVERIFIED_EDGAR_REASONS` in src/data/smart_money_edgar_coverage.py.
        sm_edgar = sm_coverage.get("edgar") if isinstance(sm_coverage, dict) else None
        sm_edgar_unverified = isinstance(sm_coverage, dict) and not (
            isinstance(sm_edgar, dict) and sm_edgar.get("verified")
        )
        # The market-wide pass read NOTHING while unread candidates were
        # outstanding. Kept apart from `sm_coverage_incomplete` on purpose:
        # that condition is TRUE on any ordinary residue and reports
        # `partial`, which is why the 2026-09-18 discovery regression sat
        # green for five sessions while external insider coverage was zero.
        sm_market_wide_blind = isinstance(sm_coverage, dict) and bool(sm_coverage.get("market_wide_blind"))
        sm_coverage_incomplete = isinstance(sm_coverage, dict) and (
            not sm_coverage.get("known") or bool(sm_coverage.get("unread")) or sm_edgar_unverified
        )
        try:
            findings, sm_result, provider_error, analysis_error, sm_gaps = smart_money_future.result()
            ctx.smart_money_findings = findings
            ctx.smart_money_provider_error = provider_error or analysis_error
            import json as _sm_json

            # Board item 63. The sale side of the insider signal, recorded
            # because nothing else records it: the fetch truncation puts
            # admission-eligible buys first and admission requires a buy,
            # so no sale has ever reached `finding`/`admission` evidence.
            # This row governs nothing -- no gate, no rank, no size reads
            # it -- it exists so the magnitude->sign question can one day
            # be answered from the desk's own data instead of guessed.
            _sale_census = _probe_sale_census(self.smart_money_provider)
            if _sale_census:
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="smart_money_analyst",
                    kind="insider_sale_census",
                    scope="run",
                    evidence_json=_sm_json.dumps(_sale_census),
                )
            _cluster_census = find_cluster_census(self.smart_money_provider)
            if _cluster_census:
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="smart_money_analyst",
                    kind="insider_cluster_census",
                    scope="run",
                    evidence_json=_sm_json.dumps(_cluster_census),
                )
            _persist_evidence(
                self.db,
                run_id=ctx.run_id,
                agent_name="smart_money_analyst",
                kind="scan_summary",
                scope="run",
                evidence_json=_sm_json.dumps(
                    {
                        "source": "SEC Form 4",
                        "observations": len(smart_money_observations),
                        "findings": len(findings),
                        "temporary_admissions": sorted(ctx.admitted_symbols),
                        "state": (
                            "degraded" if provider_error or analysis_error else "material" if findings else "quiet"
                        ),
                        # Watched-name read-through as of the pre-market pass:
                        # {known, as_of, watched, read_through, unread[symbols]}.
                        "coverage": sm_coverage,
                        # Congressional disclosures lag the trade by weeks
                        # (median 60 days measured 2026-09-19); this is how old the
                        # newest one is, so nothing reads it as current news.
                        "congressional": sm_congressional,
                    },
                    sort_keys=True,
                    default=str,
                ),
            )
            if provider_error:
                data_status["smart_money"] = "degraded" if findings else "provider_error"
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="smart_money_analyst",
                    kind="provider_error",
                    scope="run",
                    evidence_json=__import__("json").dumps({"error": provider_error}),
                )
            if sm_result is not None:
                sm_log_kwargs = agent_log_kwargs(sm_result)
                if analysis_error:
                    sm_log_kwargs["status"] = "agent_failure"
                self.db.insert_agent_log(
                    **seat_acceptance_kwargs("agent_failure" if analysis_error else None),
                    agent_name="smart_money_analyst",
                    run_id=ctx.run_id,
                    input_summary=f"{len(findings)} material findings",
                    input_message=sm_result.user_message,
                    output_summary=(
                        f"agent_failure:{analysis_error}"
                        if analysis_error
                        else (", ".join(f"{f.symbol}:{f.stance}" for f in findings) or "no material findings")
                    ),
                    full_response=sm_result.raw_text,
                    model=sm_result.model,
                    tokens_used=sm_result.tokens_used,
                    input_tokens=sm_result.input_tokens,
                    output_tokens=sm_result.output_tokens,
                    cost_usd=sm_result.cost_usd,
                    **sm_log_kwargs,
                )
                if analysis_error:
                    _persist_evidence(
                        self.db,
                        run_id=ctx.run_id,
                        agent_name="smart_money_analyst",
                        kind="agent_failure",
                        scope="run",
                        evidence_json=__import__("json").dumps({"error": analysis_error}),
                    )
                    data_status["smart_money"] = "degraded"
                elif not provider_error:
                    data_status["smart_money"] = "ok"
            # Every presented symbol must be answered (a finding or an explicit
            # no-read). One `unanswered_symbol` row per symbol the first reply
            # left unanswered, with the outcome of the single re-ask, and one
            # `reply_anomaly` row per duplicate, contradictory or never-sent
            # symbol in a reply. These rows carry a partial answer; the seat
            # is failed only when no symbol was answered. A no-read is not a
            # finding: never written as `finding` evidence, never ranked.
            for record in sm_gaps or []:
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="smart_money_analyst",
                    kind=record.get("kind", "unanswered_symbol"),
                    scope="research",
                    symbol=record.get("symbol") or None,
                    evidence_json=_sm_json.dumps(record, sort_keys=True),
                )
            for finding in findings:
                symbol = finding.symbol.upper()
                is_candidate = (
                    symbol in set(configured_symbols)
                    or symbol in ctx.admitted_symbols
                    or any(str(getattr(position, "symbol", "") or "").upper() == symbol for position in ctx.positions)
                )
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="smart_money_analyst",
                    kind="finding",
                    scope="symbol" if is_candidate else "research",
                    symbol=symbol,
                    evidence_json=finding.model_dump_json(),
                )
            if sm_result is None and not provider_error:
                data_status["smart_money"] = "ok" if findings else "empty"
            # A finish_reason=length truncation must never be indistinguishable
            # from "empty" (no signal found) or "ok" (clean run). The model can
            # still emit syntactically valid-but-incomplete JSON when cut off
            # mid-generation, which would otherwise silently pass through as
            # "ok"/"empty" above. Truncation always wins so operators can see
            # it separately from a genuine quiet day or a provider failure.
            if sm_result is not None and getattr(sm_result, "truncated", False):
                data_status["smart_money"] = "truncated"
            # Partial coverage: an answer arrived and was read on this tick,
            # but it cannot speak for watched names whose filings are not
            # all read. `partial` is REPORTED + fresh + counted as degraded
            # (src/evidence_gate.py) — honest on all three.
            if sm_coverage_incomplete and data_status.get("smart_money") in ("ok", "empty"):
                data_status["smart_money"] = "partial"
                logger.warning(
                    "Smart-money seat is partial: %s of %s watched names read "
                    "through (unread: %s); EDGAR coverage verified=%s "
                    "ratio=%s reasons=%s",
                    sm_coverage.get("read_through"),
                    sm_coverage.get("watched"),
                    ", ".join(sm_coverage.get("unread") or []) or "coverage never recorded",
                    bool(isinstance(sm_edgar, dict) and sm_edgar.get("verified")),
                    (sm_edgar or {}).get("ratio") if isinstance(sm_edgar, dict) else None,
                    ", ".join(str(r) for r in ((sm_edgar or {}).get("reasons") or []))
                    if isinstance(sm_edgar, dict)
                    else "no record",
                )
            # The market-wide pass read nothing at all. Wins over `partial`
            # and over `ok`/`empty`, and is deliberately a DISTINCT word:
            # `partial` is what the seat says on a normal day with a normal
            # residue, so the one state that means "the desk is blind to
            # every insider outside its own book" has to be sayable on its
            # own. Classified REPORTED + fresh, and NOT in
            # INTEGRITY_CLEAN_STATUSES, so it pages through the standing
            # DATA QUALITY ALERT (src/notifier.py::maybe_alert_data_quality)
            # exactly as any other degraded seat does — no new channel.
            # It does not override a LOST state (`provider_error`,
            # `truncated`, `degraded`): those are worse and already page.
            if sm_market_wide_blind and data_status.get("smart_money") in ("ok", "empty", "partial"):
                data_status["smart_money"] = "market_wide_blind"
                logger.error(
                    "Smart-money seat read ZERO market-wide Form 4 filings "
                    "with %s unread candidate(s) outstanding — insider "
                    "coverage outside the desk's own %s watched name(s) is "
                    "blind this session",
                    sm_coverage.get("market_wide_pending"),
                    sm_coverage.get("watched"),
                )
        except Exception as e:
            record_morning_fault(self, "smart_money_branch", e)
            ctx.smart_money_provider_error = f"analysis_error:{type(e).__name__}"
            data_status["smart_money"] = "provider_error"

        # Macro
        #
        # Phase 4.2 fix: before this, `data_status["macro"]` was "ok" purely
        # on whether the LLM call parsed — a run where every FRED series
        # timed out (the 2026-08-26 17:01-17:03 UTC incident: all nine
        # series failed) still said "ok" as long as the macro analyst
        # produced valid JSON from all-None inputs. `macro_coverage`
        # (src.data.macro.MacroCoverage) is the deterministic half of the
        # fix, mirroring the 2026-08-28 news fix exactly: it reflects how
        # many of the configured FRED series actually returned data,
        # independent of whether the LLM call on top of them succeeded.
        # Coverage failure dominates parse success below.
        macro_coverage: "MacroCoverage | None" = None
        try:
            (
                macro_summary,
                macro_analysis,
                ma_result,
                macro_coverage,
            ) = macro_future.result()
            # A test double / older caller may hand back something other
            # than a real MacroCoverage (e.g. a bare MagicMock attribute
            # off an unconfigured mock provider) — treat anything that
            # isn't the real dataclass as "coverage not reported" rather
            # than crashing json.dumps() below on non-serializable mock
            # internals. Mirrors how a coverage-less caller is already
            # handled (macro_coverage is None branch further down).
            if not isinstance(macro_coverage, MacroCoverage):
                macro_coverage = None
            # audit round 2: commit the analysis to ctx BEFORE the agent_logs
            # write — a DB lock/timeout on the log write used to discard a
            # fully successful macro run (ctx fields were assigned after it).
            ctx.macro_summary = macro_summary
            ctx.macro_analysis = macro_analysis
            ctx.macro_coverage = macro_coverage
            # Board item 187: durable per-open coverage record, so the item
            # closes on observed rows rather than on someone reading logs.
            try:
                from src.data.fetch_coverage_record import build_row

                self.db.insert_fred_fetch_coverage_run(build_row(ctx.run_id, macro_coverage, None))
            except Exception as e:  # noqa: BLE001 — telemetry never costs the run
                record_morning_fault(self, "fred_coverage_row", e)
            if macro_coverage is not None:
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="macro_provider",
                    kind="coverage",
                    scope="run",
                    evidence_json=__import__("json").dumps(
                        {
                            "configured": macro_coverage.configured,
                            "succeeded": macro_coverage.succeeded,
                            "failed": [{"series_id": f.series_id, "reason": f.reason} for f in macro_coverage.failed],
                            "status": macro_coverage.status,
                        },
                        sort_keys=True,
                    ),
                )
            self.db.insert_agent_log(
                agent_name="macro_analyst",
                run_id=ctx.run_id,
                input_summary=f"VIX={macro_summary.get('vix', {}).get('current')}",
                input_message=ma_result.user_message,
                output_summary=(
                    f"regime={macro_analysis.regime}, outlook={macro_analysis.equity_outlook}"
                    if macro_analysis
                    else "parse_error"
                ),
                full_response=ma_result.raw_text,
                model=ma_result.model,
                tokens_used=ma_result.tokens_used,
                input_tokens=ma_result.input_tokens,
                output_tokens=ma_result.output_tokens,
                cost_usd=ma_result.cost_usd,
                **agent_log_kwargs(ma_result),
            )
            ctx.macro_summary = macro_summary
            ctx.macro_analysis = macro_analysis
            if macro_analysis:
                logger.info(
                    "Macro analysis: regime=%s, outlook=%s, confidence=%s",
                    macro_analysis.regime,
                    macro_analysis.equity_outlook,
                    macro_analysis.confidence,
                )
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="macro_analyst",
                    kind="analysis",
                    scope="run",
                    evidence_json=macro_analysis.model_dump_json(),
                )
            # Coverage is authoritative over parse success: total FRED
            # failure means "failed" even if the model still emitted a
            # technically-valid report on all-None input. A coverage-less
            # result (macro_coverage is None — a caller/test double that
            # hasn't been updated to report it) falls back to the pre-fix
            # ok/parse_error split rather than crashing on a missing value.
            if macro_coverage is None:
                data_status["macro"] = "ok" if macro_analysis else "parse_error"
            elif macro_coverage.status == "failed":
                data_status["macro"] = "failed"
                logger.error(
                    "Macro coverage FAILED this run: %s",
                    macro_coverage.describe(),
                )
            elif not macro_analysis:
                data_status["macro"] = "parse_error"
            elif macro_coverage.status == "partial":
                data_status["macro"] = "partial"
                logger.warning(
                    "Macro coverage PARTIAL this run: %s",
                    macro_coverage.describe(),
                )
            elif getattr(macro_coverage, "overdue", None):
                # Every configured series answered, but at least one of them
                # is sitting on a reading that should already have been
                # superseded — a publication or fetch failure, NOT normal
                # release timing (the freshness test is derived per series
                # from its own cadence and publication lag; see
                # src/data/macro.py::SeriesFreshness). This is the real
                # staleness that survived the removal of the old
                # calendar-day gate, so it must stay visible: "ok" here
                # would be the same "no data, but everything's fine!" gap
                # every other seat's audit closed. A new VALUE on the
                # existing `macro` key, not a new key — so the
                # ">= 2 degraded sources" advisory arithmetic below is
                # untouched.
                data_status["macro"] = "release_overdue"
                logger.error(
                    "Macro prints OVERDUE this run: %s",
                    macro_coverage.describe(),
                )
            else:
                data_status["macro"] = "ok"
            # Self-reported confidence is a second, independent signal from
            # the coverage check above: coverage measures whether FRED
            # actually returned data, confidence is the model's own read on
            # how well it could make sense of what it got. A run can have
            # full coverage and still carry confidence="low" (contradictory
            # or ambiguous indicators) — that combination was reaching "ok"
            # with nothing anywhere to show for it, the same "no data, but
            # everything's fine!" gap flagged for every other seat tonight.
            # Deliberately separate from (and does not touch) the
            # release_overdue check above, which is a deterministic
            # publication fact rather than the model's own read. Only fires on what
            # would otherwise be "ok" — coverage-driven partial/failed/
            # parse_error already say something is wrong and take priority.
            if data_status["macro"] == "ok" and macro_analysis and macro_analysis.confidence == "low":
                data_status["macro"] = "low_confidence"
                logger.warning(
                    "Macro parsed cleanly on sufficient coverage but the "
                    "model self-reported confidence='low' — flagging "
                    "data_status['macro']='low_confidence' instead of 'ok'.",
                )
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            record_morning_fault(self, "macro_analyst", e)
            data_status["macro"] = "failed"

        # News
        #
        # 2026-08-28 fix: before this, `data_status["news"]` was "ok" purely
        # on whether the LLM call parsed — a run where Reuters 404'd and AP
        # 403'd still said "ok" as long as the analyst produced valid JSON
        # from whatever the surviving feeds returned (or from nothing at
        # all). `news_coverage` (src.data.news.NewsCoverage) is the
        # deterministic half of the fix: it reflects how many of the
        # configured wire feeds actually returned data, independent of
        # whether the LLM call on top of them succeeded. Coverage failure
        # dominates parse success below — a cleanly parsed report built on
        # zero real headlines is not "ok" by any honest reading of the word.
        news_intel: NewsIntelligenceReport | None = None
        news_coverage: "NewsCoverage | None" = None
        try:
            news_intel, news_coverage = news_future.result()
            if news_coverage is not None:
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="news_provider",
                    kind="coverage",
                    scope="run",
                    evidence_json=__import__("json").dumps(
                        {
                            "configured": news_coverage.configured,
                            "succeeded": news_coverage.succeeded,
                            "failed": [{"name": f.name, "reason": f.reason} for f in news_coverage.failed],
                            "status": news_coverage.status,
                        },
                        sort_keys=True,
                    ),
                )
            if news_intel:
                logger.info("News briefing: %s", news_intel.pm_briefing[:200])
                _persist_evidence(
                    self.db,
                    run_id=ctx.run_id,
                    agent_name="news_analyst",
                    kind="analysis",
                    scope="run",
                    evidence_json=news_intel.model_dump_json(),
                )
            # Coverage is authoritative over parse success: total feed
            # failure means "failed" even if the model still emitted a
            # technically-valid report on empty input. A coverage-less
            # result (news_coverage is None — a caller/test that hasn't
            # been updated to report it) falls back to the pre-fix
            # ok/parse_error split rather than crashing on a missing value.
            if news_coverage is None:
                data_status["news"] = "ok" if news_intel else "parse_error"
            elif news_coverage.status == "failed":
                data_status["news"] = "failed"
                logger.error(
                    "News coverage FAILED this run: %s",
                    news_coverage.describe(),
                )
            elif not news_intel:
                data_status["news"] = "parse_error"
            elif news_coverage.status == "partial":
                data_status["news"] = "partial"
                logger.warning(
                    "News coverage PARTIAL this run: %s",
                    news_coverage.describe(),
                )
            else:
                data_status["news"] = "ok"
            # Board item 152. An unreadable news answer was paid for and
            # thrown away; until now the only trace was a log line (gone at
            # the next 10MB rotation) and a count. File the loss per STOCK,
            # with its reason, through the same `analysis_drop` writer the
            # technical seat's row drops already use (item 158), so a later
            # reader can ask "why is this name's news seat absent on this
            # run?" and get an answer from the database. Observability only:
            # `_persist_dropped_reasons` never raises and nothing downstream
            # reads these rows to make a decision. `book_symbols=set()`
            # because nothing recovered — the whole answer is gone.
            if data_status["news"] == "parse_error" and news_symbols_asked:
                reason = (
                    "news seat's answer was unreadable after its one paid "
                    "heal retry — this stock has NO news seat on this run "
                    "(absent, not neutral); raw payload in "
                    "data/parse_failures/news_analyst_*.json"
                )
                _persist_dropped_reasons(
                    self.db,
                    ctx.run_id,
                    {("NewsIntelligenceReport", sym): 1 for sym in sorted(news_symbols_asked)},
                    {("NewsIntelligenceReport", sym): reason for sym in news_symbols_asked},
                    set(),
                )
            # PM TEST GATE item 4, second half (2026-09-14). A structural
            # loss — the seat had real headline coverage for a symbol and
            # its answer for that symbol is missing (see
            # `NewsAnalystAgent._find_dropped_news_symbols`) — is worse than
            # a self-reported low confidence and is checked first: it is a
            # confirmed loss, not the model's own honesty signal about
            # thin/ambiguous input. Only fires on what would otherwise be
            # "ok" — coverage-driven partial/failed/parse_error above
            # already say something is wrong and take priority.
            if data_status["news"] == "ok" and news_intel and news_intel.dropped_news_symbols:
                data_status["news"] = "symbol_dropped"
                logger.error(
                    "News parsed cleanly on sufficient coverage but the "
                    "seat's own answer is missing %d symbol(s) it was shown "
                    "real headline coverage for — flagging "
                    "data_status['news']='symbol_dropped' instead of 'ok': %s",
                    len(news_intel.dropped_news_symbols),
                    news_intel.dropped_news_symbols,
                )
            # Board item 152, salvage half. The report parsed and is usable,
            # but `analyze()` had to drop a top-level field the seat sent
            # unreadable (measured: `market_sentiment` carrying "mixed").
            # That is not "ok" — a field is missing — and it is not a lost
            # seat either, so it gets its own word rather than overstating
            # either reading. Checked after `symbol_dropped`, which is a
            # per-STOCK loss and so the worse news, and before
            # `low_confidence`, which is only the model's own self-report.
            if data_status["news"] == "ok" and news_intel and news_intel.unreadable_fields:
                data_status["news"] = "field_unreadable"
                logger.warning(
                    "News seat answered but %d field(s) were unreadable and "
                    "dropped to save the rest of the report: %s — those "
                    "fields read ABSENT, not neutral.",
                    len(news_intel.unreadable_fields),
                    ", ".join(f"{k}={v!r}" for k, v in sorted(news_intel.unreadable_fields.items())),
                )
            # Self-reported confidence is a second, independent signal from
            # the coverage check above: coverage measures whether the wire
            # feeds returned data, confidence is the model's own read on
            # how well it could make sense of what it got. A report can
            # structurally parse clean on feeds that DID return data and
            # still carry confidence="low" (thin/contradictory/ambiguous
            # headlines) — that combination was reaching "ok" with nothing
            # anywhere to show for it, which is exactly the "no data, but
            # everything's fine!" lie the owner flagged. Mirrors how a
            # truncated smart-money response always wins over "ok"/"empty"
            # above: the model's own honesty signal overrides an otherwise-
            # clean status rather than being silently absorbed by it. Only
            # fires on what would otherwise be "ok" — coverage-driven
            # partial/failed/parse_error already say something is wrong and
            # take priority.
            if data_status["news"] == "ok" and news_intel and news_intel.confidence == "low":
                data_status["news"] = "low_confidence"
                logger.warning(
                    "News parsed cleanly on sufficient coverage but the "
                    "model self-reported confidence='low' — flagging "
                    "data_status['news']='low_confidence' instead of 'ok'.",
                )
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            record_morning_fault(self, "news_analyst", e)
            data_status["news"] = "failed"
        ctx.news_intel = news_intel

        # Tech
        analyses: list[TechAnalysisResult] = []
        try:
            analyses_map, ta_result = tech_future.result()
            # analyses_map carries every pre-filtered symbol as a key
            # (2026-08-19 Tech batch-response symbol-loss fix); None marks
            # a symbol tech_analyst could not resolve even after its own
            # bounded retry. Filter before building the real analyses
            # list, and surface the failed count explicitly rather than
            # letting it disappear into a plain "ok".
            analyses = [a for a in analyses_map.values() if a is not None]
            failed_count = len(analyses_map) - len(analyses)
            if not analyses_map:
                data_status["tech"] = "empty"
            elif failed_count == 0:
                # Every submitted symbol resolved, but "resolved" only means
                # the model returned parseable content — it says nothing
                # about whether the model itself trusted that content. A
                # batch can be 100% parsed and still carry a self-reported
                # `conviction="low"` on one or more reads (thin history,
                # ambiguous setup, stale indicators the model flagged on its
                # own). Silently reporting "ok" in that case is the same
                # failure mode this whole data_status design exists to
                # close for earnings/news/macro — a seat claiming "ok" while
                # its own confidence says otherwise. This does not touch
                # ranking/sizing (conviction_ledger.py's "confidence is
                # reported, not applied" stands, 2026-08-31 owner decision);
                # it only makes the distinction visible where data_status
                # already is.
                low_conviction = [a.symbol for a in analyses if a.conviction == "low"]
                if low_conviction:
                    data_status["tech"] = "low_confidence"
                    logger.warning(
                        "Tech batch fully resolved but %d/%d read(s) carry the model's own low conviction: %s",
                        len(low_conviction),
                        len(analyses),
                        ", ".join(low_conviction),
                    )
                else:
                    data_status["tech"] = "ok"
            elif analyses:
                data_status["tech"] = "partial"
                logger.warning(
                    "Tech batch partial: %d/%d symbols resolved, %d failed "
                    "even after retry — proceeding with the resolved subset",
                    len(analyses),
                    len(analyses_map),
                    failed_count,
                )
            else:
                data_status["tech"] = "failed"
                logger.error(
                    "Tech batch: all %d submitted symbol(s) failed even after retry",
                    len(analyses_map),
                )
            if ta_result:
                self.db.insert_agent_log(
                    **seat_acceptance_kwargs("failed" if not analyses else None),
                    agent_name="tech_analyst",
                    run_id=ctx.run_id,
                    input_summary=(
                        f"Batch: {len(analyses)}/{len(analyses_map)} symbols "
                        f"analyzed" + (f", {failed_count} failed" if failed_count else "")
                    ),
                    input_message=ta_result.user_message,
                    output_summary=", ".join(f"{a.symbol}:{a.rating}" for a in analyses),
                    full_response=ta_result.raw_text,
                    model=ta_result.model,
                    tokens_used=ta_result.tokens_used,
                    input_tokens=ta_result.input_tokens,
                    output_tokens=ta_result.output_tokens,
                    cost_usd=ta_result.cost_usd,
                    **agent_log_kwargs(ta_result),
                )
                for analysis in analyses:
                    _persist_evidence(
                        self.db,
                        run_id=ctx.run_id,
                        agent_name="tech_analyst",
                        kind="analysis",
                        scope="symbol",
                        symbol=analysis.symbol,
                        evidence_json=analysis.model_dump_json(),
                    )
                    _record_pipeline_event(
                        self,
                        ctx,
                        analysis.symbol,
                        "specialist",
                        "evaluated",
                        "technical_analysis_validated",
                        specialist="tech_analyst",
                        rating=analysis.rating,
                    )
                for symbol, analysis in analyses_map.items():
                    if analysis is None:
                        _record_pipeline_event(
                            self,
                            ctx,
                            symbol,
                            "specialist",
                            "failed",
                            "technical_analysis_unresolved_after_retry",
                            specialist="tech_analyst",
                        )
            logger.info("Technical analysis complete: %d symbols in 1 LLM call", len(analyses))
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            record_morning_fault(self, "tech_analyst", e)
            data_status["tech"] = "failed"
        ctx.analyses = analyses
        # Outside the try/except above on purpose: it must run whether tech
        # resolved cleanly, partially, or not at all — a bars outage severe
        # enough to crash the batch entirely is exactly the case this exists
        # to catch, not one to skip because the try block above already
        # failed. Never raises (see its own docstring), so it cannot turn a
        # degraded tech stage into a hard research-stage failure.
        _check_levels_coverage(self.db, ctx, analyses)

        # Earnings
        #
        # 2026-09-04 fix: before this, `data_status["earnings"]` was "ok"
        # purely on whether the future returned without raising — see
        # `_classify_earnings_status`'s docstring above for the real
        # incident this closes and why content, not exception-vs-not, is
        # authoritative now.
        earnings_results = []
        try:
            _, earnings_results = earnings_future.result()
            data_status["earnings"] = _classify_earnings_status(earnings_results)
            if data_status["earnings"] == "content_missing":
                logger.error(
                    "Earnings: every analyzed filing this run came back with "
                    "no real figures and/or a self-reported data problem — "
                    "treating as a content failure, not 'ok'."
                )
            elif data_status["earnings"] == "partial":
                logger.warning(
                    "Earnings: at least one analyzed filing this run came "
                    "back with no real figures and/or a self-reported data "
                    "problem alongside others that were clean."
                )
            import json as _json

            for item in earnings_results:
                analysis = item.get("analysis") if isinstance(item, dict) else None
                symbol = item.get("symbol") if isinstance(item, dict) else None
                if analysis and symbol:
                    # `analysis` is already validated_model.model_dump() —
                    # see EarningsAnalystAgent._analyze_new/_load_analysis —
                    # never re-derived from raw filing text here.
                    _persist_evidence(
                        self.db,
                        run_id=ctx.run_id,
                        agent_name="earnings_analyst",
                        kind="analysis",
                        scope="symbol",
                        symbol=symbol,
                        evidence_json=_json.dumps(analysis),
                    )
        except PaidAnalysisSuspended:
            raise
        except Exception as e:
            record_morning_fault(self, "earnings_check", e)
            data_status["earnings"] = "failed"
        ctx.earnings_results = earnings_results

        # Phase 9 (§9.1/§9.2) — the nomination responder pass. Deliberately
        # sequenced HERE, after every parallel-wave future has been
        # resolved and ctx.news_intel / ctx.macro_analysis /
        # ctx.earnings_results / ctx.analyses are all populated: News,
        # Earnings and Macro run CONCURRENTLY with Technical (the
        # ThreadPoolExecutor above), so a nomination they produce cannot
        # be known before Technical's first batch call starts. This is a
        # deliberate second, on-demand Technical call for just the
        # nominated symbols — NOT a second parallel wave; everything above
        # this line is unchanged from before Phase 9.
        self._run_nomination_responder_pass(ctx, prior_macro_state)

        ctx.data_status = data_status
        # Single grep-able summary line. Each agent's failure already logs
        # at ERROR individually, but a downstream operator scanning the
        # journal for "why did morning trade zero today?" wants one row
        # listing all degraded inputs side-by-side. The 2+ failure
        # advisory in RiskStage handles the runtime defensive response;
        # this log handles the postmortem readability.
        # Tech's `low_confidence` is set (see the tech branch above) whenever
        # ANY resolved read carries the model's own conviction="low" — and a
        # `neutral` rating (no view at all) is effectively always
        # low-conviction, because there is nothing for the model to be
        # confident ABOUT. Before this fix that made this line fire ERROR
        # every single morning regardless of whether a single actionable
        # BUY/SELL call was ever shaky, and log-health then reported a
        # perfectly healthy tech seat as "a research desk that could not be
        # reached". `data_status["tech"]` itself, and everything that reads
        # it (RiskStage's `data_degraded` advisory, the Risk Manager's
        # prompt), is UNCHANGED by this — only this operator-facing summary
        # line is corrected to name what actually degraded: a low-conviction
        # read on a symbol the desk was actually weighing, not a no-view
        # neutral read the model was never going to act on either way.
        tech_low_confidence_is_noise = data_status.get("tech") == "low_confidence" and not any(
            a.conviction == "low" and a.rating != "neutral" for a in analyses
        )
        degraded = [
            k
            for k, v in data_status.items()
            if evidence_gate.counts_as_degraded(v) and not (k == "tech" and tech_low_confidence_is_noise)
        ]
        if degraded:
            logger.error(
                "Morning research degraded: %s | full status=%s",
                ",".join(sorted(degraded)),
                data_status,
            )
        # Parse-level losses are recorded ALONGSIDE data_status, not inside
        # it — same reasoning as `macro_coverage` in RunContext: data_status
        # carries the one-word summary per source, this carries the evidence
        # a single word cannot. Deliberately not a data_status key, because
        # every key in that dict moves the `data_degraded` advisory's ">= 2
        # degraded sources" arithmetic and this change must not shift an
        # existing gate's threshold as a side effect.
        #
        # This is the RESEARCH-stage reading, logged here so a postmortem can
        # tell a research-side loss from a PM-side one. RiskStage takes the
        # authoritative reading later, after the Portfolio Manager has also
        # parsed, and that is what reaches the advisory.
        if parse_telemetry.total_dropped():
            logger.error(
                "Analysis items DROPPED at parse during research (%d): %s — "
                "these candidates were researched and never reached the "
                "Portfolio Manager",
                parse_telemetry.total_dropped(),
                parse_telemetry.describe_dropped(),
            )
        if parse_telemetry.total_null_coercions():
            logger.warning(
                "Explicit nulls coerced to defaults during research (%d): %s — "
                "the objects survived, but the model said nothing where the "
                "prompt asked for something",
                parse_telemetry.total_null_coercions(),
                parse_telemetry.describe_null_coercions(),
            )
        if parse_telemetry.total_hygiene_observations() and not (parse_telemetry.total_hygiene_violations()):
            # Item 214 (2026-10-01): a silent clean run used to look
            # identical to the check never running, which is why nobody
            # could read these counters off production. State the
            # denominator so "0 of N, by provider" is a readable fact.
            logger.info(
                "Tech-seat answer hygiene CLEAN this run: 0 violations in %d "
                "checked answer(s), by provider: %s — see docs/WORK.md item 214",
                parse_telemetry.total_hygiene_observations(),
                parse_telemetry.describe_hygiene_observations(),
            )
        if parse_telemetry.total_hygiene_violations():
            # Item 157's runtime check (2026-09-23): whether a schema-
            # enforced route is actually being honoured, surfaced where a
            # human running this desk can see it, since no deployed process
            # ever holds a real GOOGLE_API_KEY for a pytest-based live check
            # to run against (see docs/WORK.md item 157,
            # tests/test_tech_schema_live.py). Never blocks anything — a
            # strict schema is supposed to make fenced markdown and extra
            # keys impossible; when they show up anyway the row still
            # parsed and was still used, so this is evidence, not a gate.
            # Each count is tagged with the ACTUAL provider that answered
            # (`_record_answer_hygiene` in src/agents/tech_answer_hygiene.py) —
            # adversary review, 2026-09-23: only "openrouter"/"google" are
            # ever given a response_format at all (src/agents/base.py); a
            # count against any other provider is not evidence the strict
            # schema failed, since no schema was sent on that call.
            logger.warning(
                "Tech-seat answer hygiene violations this run (%d), by "
                "provider (only openrouter/google were ever sent a strict "
                "schema; any other provider's count reflects no schema "
                "being sent at all, not a schema failing to suppress): "
                "%s — see docs/WORK.md item 157",
                parse_telemetry.total_hygiene_violations(),
                parse_telemetry.describe_hygiene_violations()
                + f" (out of {parse_telemetry.total_hygiene_observations()} "
                + f"checked answer(s): {parse_telemetry.describe_hygiene_observations()})",
            )
        return ctx

    def _run_nomination_responder_pass(self, ctx: RunContext, prior_macro_state: dict) -> None:
        """Phase 9 (§9.1/§9.2) — Technical as RESPONDER, not gatekeeper.

        Collects every seat's nominations, applies the per-seat cap, dedupes
        across seats (a symbol nominated by two seats records both), applies
        the global cap, gates any out-of-universe survivor through the same
        deterministic admission gate the smart-money lane uses, then runs a
        SECOND on-demand Technical call for whatever is left that the first
        batch didn't already cover. Results are merged directly into
        `ctx.analyses` — the exact list `validate_grounding`'s hard gate
        reads — so a responded nomination is indistinguishable from an
        organically-prefiltered symbol by the time PM sees it.

        No nominations -> no second call, full stop: every early-return path
        below exits before `self.tech_analyst.analyze_batch` is ever called.
        """
        import json as _json

        nominations_by_seat = _collect_seat_nominations(
            ctx.news_intel,
            ctx.macro_analysis,
            ctx.earnings_results,
        )
        total_raw = sum(len(v) for v in nominations_by_seat.values())
        from src.conviction_ledger import normalize_seat as _normalize_seat

        for seat, noms in nominations_by_seat.items():
            for nomination in noms:
                _record_pipeline_event(
                    self,
                    ctx,
                    nomination.symbol,
                    "opportunity",
                    "nominated",
                    "research_seat_nomination",
                    seat=seat,
                    conviction=nomination.conviction,
                    observation=nomination.observation,
                    # Item 99: the nominating seat's own falsifier, kept
                    # verbatim, plus an explicit flag when it gave none.
                    # A missing condition is recorded AS missing — never
                    # replaced with a template, because a synthesised
                    # falsifier reads like exit protection the desk does
                    # not actually have.
                    thesis_invalid_if=nomination.thesis_invalid_if,
                    falsifier_missing=missing_stated_falsifier(nomination.thesis_invalid_if),
                )
                # §9.5: keep what the seat DECLARED so DecisionStage can
                # RECORD it on the stance. It is a label, not a multiplier —
                # the conviction weight was removed on 2026-08-31 and the
                # ledger now reports each analyst's record broken down BY the
                # confidence it declared instead. Pure bookkeeping on the
                # context — no stage below reads this field to decide anything.
                ctx.nomination_convictions.setdefault(
                    nomination.symbol.strip().upper(),
                    {},
                )[_normalize_seat(seat)] = {
                    "conviction": nomination.conviction,
                    "observation": nomination.observation,
                }

        nom_cfg = getattr(self.config, "nominations", None)
        max_per_seat = int(getattr(nom_cfg, "max_per_seat_per_run", 3)) if nom_cfg else 3
        candidates = select_nominations(
            nominations_by_seat,
            max_per_seat=max_per_seat,
        )

        if not candidates:
            logger.info(
                "Nomination responder: %d raw nomination(s), 0 candidates after caps — no second Technical call.",
                total_raw,
            )
            persist_nomination_summary(
                self.db,
                run_id=ctx.run_id,
                total_raw=total_raw,
                nominations_by_seat=nominations_by_seat,
                max_per_seat=max_per_seat,
                candidates_selected=0,
                responder_call_made=False,
            )
            return

        configured = {str(s).strip().upper() for s in self.config.trading.universe if str(s).strip()}

        # Out-of-universe candidates must clear the SAME deterministic gate
        # the SEC Form 4 smart-money lane applies — an already-admitted or
        # in-universe symbol needs no gate at all (D3).
        to_gate = [c for c in candidates if c.symbol not in configured and c.symbol not in ctx.admitted_symbols]
        newly_admitted: set[str] = set()
        admission_details: dict[str, dict] = {}
        if to_gate and self._admit_nominated_candidates:
            try:
                newly_admitted, admission_details = self._admit_nominated_candidates(
                    [c.symbol for c in to_gate],
                )
            except Exception as exc:
                # Admission uncertainty fails closed, same posture as the
                # smart-money admission try/except above.
                record_morning_fault(self, "nomination_admission", exc)
                newly_admitted, admission_details = set(), {}

        eligible_candidates = []
        for c in candidates:
            if c.symbol in configured or c.symbol in ctx.admitted_symbols or c.symbol in newly_admitted:
                eligible_candidates.append(c)
            else:
                _record_pipeline_event(
                    self,
                    ctx,
                    c.symbol,
                    "opportunity",
                    "rejected",
                    "nomination_failed_external_admission_gate",
                    seats=c.seats,
                    conviction=c.conviction,
                )

        for symbol, admission in admission_details.items():
            _persist_evidence(
                self.db,
                run_id=ctx.run_id,
                agent_name="pipeline",
                kind="admission",
                scope="symbol",
                symbol=symbol,
                evidence_json=_json.dumps(admission, sort_keys=True),
            )
            admission_reason = {k: v for k, v in admission.items() if k != "reason"}
            _record_pipeline_event(
                self,
                ctx,
                symbol,
                "opportunity",
                "admitted",
                "nomination_external_admission",
                **admission_reason,
            )

        # Widen run-scoped BUY eligibility the SAME way smart-money transient
        # admission does — ctx.admitted_symbols feeds allowed_buy_symbols at
        # the PM call (DecisionStage) and the symbol guard (RiskStage), both
        # of which run strictly after this stage.
        ctx.admitted_symbols = set(ctx.admitted_symbols) | newly_admitted

        already_analyzed = {a.symbol.strip().upper() for a in ctx.analyses}
        for c in eligible_candidates:
            if c.symbol in already_analyzed:
                _record_pipeline_event(
                    self,
                    ctx,
                    c.symbol,
                    "opportunity",
                    "already_covered",
                    "nomination_matched_existing_technical_analysis",
                    seats=c.seats,
                    conviction=c.conviction,
                )
        needing_responder = [c for c in eligible_candidates if c.symbol not in already_analyzed]

        if not needing_responder:
            logger.info(
                "Nomination responder: %d raw nomination(s) -> %d candidate(s) "
                "selected, all already covered by the first Technical batch — "
                "no second call.",
                total_raw,
                len(eligible_candidates),
            )
            persist_nomination_summary(
                self.db,
                run_id=ctx.run_id,
                total_raw=total_raw,
                nominations_by_seat=nominations_by_seat,
                max_per_seat=max_per_seat,
                candidates_selected=sorted(c.symbol for c in eligible_candidates),
                responder_call_made=False,
            )
            return

        symbols_data: list[dict] = []
        for c in needing_responder:
            bars = self.market.get_ohlcv(c.symbol, self.config.trading.lookback_days)
            if not bars:
                logger.warning("Nomination responder: no bars for %s, skipping", c.symbol)
                continue
            indicators = compute_indicators(c.symbol, bars)
            symbols_data.append({"symbol": c.symbol, "bars": bars, "indicators": indicators})
            ctx.symbols_bars[c.symbol] = bars

        if not symbols_data:
            logger.info(
                "Nomination responder: %d candidate(s) needed a call but none "
                "had market data — no second Technical call.",
                len(needing_responder),
            )
            return

        prior_ratings = self.tech_store.load()
        valuations = dict(ctx.valuations)
        for s in symbols_data:
            sym = s["symbol"]
            try:
                valuations[sym] = self.market.get_valuation_metrics(sym)
            except Exception as e:
                record_morning_fault(self, "nomination_valuation", e, symbol=sym)
        ctx.valuations = valuations

        analyses_map, ta_result = self.tech_analyst.analyze_batch(
            symbols_data,
            prior_ratings=prior_ratings,
            valuations=valuations,
            prior_macro_regime=prior_macro_state.get("regime"),
            prior_macro_outlook=prior_macro_state.get("equity_outlook"),
            # Same live-price rule as the main morning Tech pass (2026-09-14).
            intraday_context=self._live_context([s["symbol"] for s in symbols_data]),
        )
        ctx.tech_unreadable = dict(getattr(self.tech_analyst, "last_unreadable", None) or {})
        resolved = [a for a in analyses_map.values() if a is not None]
        if resolved:
            try:
                self.tech_store.update(resolved)
            except Exception as e:
                record_morning_fault(self, "nomination_tech_store", e)
            ages = self.tech_store.compute_ages([a.symbol for a in resolved])
            for a in resolved:
                if a.symbol in ages:
                    a.signal_age_days = ages[a.symbol]

        existing_symbols = {a.symbol.strip().upper() for a in ctx.analyses}
        for a in resolved:
            if a.symbol.strip().upper() not in existing_symbols:
                ctx.analyses.append(a)
                existing_symbols.add(a.symbol.strip().upper())

        if ta_result:
            self.db.insert_agent_log(
                **seat_acceptance_kwargs("failed" if not resolved else None),
                agent_name="tech_analyst",
                run_id=ctx.run_id,
                input_summary=(f"Nomination responder batch: {len(resolved)}/{len(analyses_map)} symbols"),
                input_message=ta_result.user_message,
                output_summary=", ".join(f"{a.symbol}:{a.rating}" for a in resolved),
                full_response=ta_result.raw_text,
                model=ta_result.model,
                tokens_used=ta_result.tokens_used,
                input_tokens=ta_result.input_tokens,
                output_tokens=ta_result.output_tokens,
                cost_usd=ta_result.cost_usd,
                **agent_log_kwargs(ta_result),
            )
        for a in resolved:
            _persist_evidence(
                self.db,
                run_id=ctx.run_id,
                agent_name="tech_analyst",
                kind="analysis",
                scope="symbol",
                symbol=a.symbol,
                evidence_json=a.model_dump_json(),
            )
            _record_pipeline_event(
                self,
                ctx,
                a.symbol,
                "specialist",
                "evaluated",
                "technical_analysis_validated",
                specialist="tech_analyst",
                rating=a.rating,
                origin="nomination_responder",
            )
        for symbol, a in analyses_map.items():
            if a is None:
                _record_pipeline_event(
                    self,
                    ctx,
                    symbol,
                    "specialist",
                    "failed",
                    "technical_analysis_unresolved_after_retry",
                    specialist="tech_analyst",
                    origin="nomination_responder",
                )

        responder_cost = ta_result.cost_usd if ta_result else None
        logger.info(
            "Nomination responder: %d raw nomination(s) from %d seat(s) -> "
            "%d candidate(s) selected -> %d needed a responder Technical "
            "call (%d resolved) -> cost=%s",
            total_raw,
            len([seat for seat, noms in nominations_by_seat.items() if noms]),
            len(eligible_candidates),
            len(symbols_data),
            len(resolved),
            f"${responder_cost:.4f}" if responder_cost is not None else "unknown",
        )
        persist_nomination_summary(
            self.db,
            run_id=ctx.run_id,
            total_raw=total_raw,
            nominations_by_seat=nominations_by_seat,
            max_per_seat=max_per_seat,
            candidates_selected=sorted(c.symbol for c in eligible_candidates),
            responder_symbols=sorted(s["symbol"] for s in symbols_data),
            responder_call_made=True,
            responder_resolved=sorted(a.symbol for a in resolved),
            responder_cost_usd=responder_cost,
        )


def order_morning_symbols(owner, ctx, *, configured_symbols, ranked, screened_details, top_n) -> list[str]:
    """The morning research order (owner ruling 2026-10-14) and the screened send.

    Order: held names, then this run's SEC admissions, then the configured
    universe, then screened names best score K first. The Tech seat splits its
    batch in this order, so a budget cut always falls on the weakest screened
    names. Of the screened passers (``ranked`` = scored, best first;
    ``screened_details`` = every passer): a held passer is coverage and is
    admitted uncapped; the top ``top_n`` others by K are admitted and sent;
    every other passer is recorded with `record_dropped_name` —
    ``not_sent_weaker_rank`` with its rank, or ``not_sent_unscored`` with the
    record's reason when K is None (never ranked on a guess).
    """
    from src.intraday.candidates import record_dropped_name

    held_symbols = [
        str(getattr(p, "symbol", "")).strip().upper()
        for p in (getattr(ctx, "positions", None) or [])
        if getattr(p, "qty", 0)
    ]
    held_symbols = [s for s in held_symbols if s]
    sec_symbols = sorted(ctx.admitted_symbols)
    held_passers, screened_order, dropped = choose_screened_names(
        ranked=ranked,
        screened_details=screened_details,
        already=set(configured_symbols) | set(ctx.admitted_symbols),
        held=set(held_symbols),
        top_n=top_n,
    )
    for symbol in held_passers + screened_order:
        ctx.admitted_symbols.add(symbol)
        ctx.smart_money_admissions[symbol] = screened_details.get(symbol, {})
    for symbol, reason, fields in dropped:
        record_dropped_name(owner, ctx, symbol, reason, **fields)
    return list(dict.fromkeys(held_symbols + sec_symbols + list(configured_symbols) + screened_order))


def choose_screened_names(*, ranked, screened_details, already, held, top_n):
    """Which of last night's screened passers this morning admits (pure; no recording).

    The ONE ranking rule, shared by the morning stage (`order_morning_symbols`,
    which records the dropped names) and the pre-market earnings pass
    (`src.pipeline_parts.pnl_gaps._screened_morning_symbols`, which ignores
    them), so the two can never pick different names. Names in ``already``
    (configured universe, SEC admissions) are skipped without using a rank
    slot. A held passer is coverage and is admitted uncapped; the top
    ``top_n`` other scored passers by K are sent; every other passer is
    dropped with its reason.

    Returns ``(held_passers, sent, dropped)``; ``dropped`` is a list of
    ``(symbol, reason, fields)`` in the order the morning records them.
    """
    held_passers: list[str] = []
    dropped: list[tuple[str, str, dict]] = []
    for symbol, details in screened_details.items():
        if symbol in already or symbol in held_passers:
            continue
        if symbol in held:
            held_passers.append(symbol)
        elif details.get("k") is None:
            dropped.append(
                (
                    symbol,
                    "not_sent_unscored",
                    {
                        "score_reason": details.get("score_reason"),
                        "detail": "no score K in last night's record; never ranked on a guess",
                    },
                )
            )
    taken = set(already) | set(held_passers)
    sent: list[str] = []
    rank = 0
    for symbol in ranked:
        if symbol in taken:
            continue
        rank += 1
        details = screened_details.get(symbol, {})
        if rank <= top_n:
            taken.add(symbol)
            sent.append(symbol)
            continue
        dropped.append(
            (
                symbol,
                "not_sent_weaker_rank",
                {
                    "rank": rank,
                    "k": details.get("k"),
                    "nightly_rank": details.get("nightly_rank"),
                    "detail": f"only the top {top_n} screened passers by score K are sent each morning",
                },
            )
        )
    return held_passers, sent, dropped
