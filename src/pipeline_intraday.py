"""The intra-check session and the intraday opportunity scan: shims plus the scan body.

Step 8 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210), clusters R and W, now as
constructed parts. Bodies live in src/intraday/ (`IntradaySession`, `IntradayGating`,
`IntradayCandidates`); `IntradayMixin` keeps same-named thin shims built per call, so
`TradingPipeline` still exposes every one of these as its own attribute and a
collaborator swapped after construction is what the body sees. The one body that
stays HERE is `_intraday_opportunity_scan_body` (399 lines): it is a single function,
so it cannot be carved to fit the 400-line floor for a new file without changing it;
it is still a constructed part (`IntradayScanBody`) with every collaborator handed in.

Names resolved HERE after the first move (plan S5, silent-behaviour risk 1) and
still patched on this module by tests: `compute_indicators`, `agent_log_kwargs`,
`seat_acceptance_kwargs`, `RunContext`, `PaidAnalysisSuspended`, `_persist_evidence`
and `_record_pipeline_event`. The moved bodies resolve their own names in their own
modules.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import inspect
import logging

from src.agents.base import agent_log_kwargs, seat_acceptance_kwargs
from src.cost_circuit import PaidAnalysisSuspended
from src.data.technical import compute_indicators
from src.intraday.buying_room import NO_BUYING_ROOM, assess_buying_room
from src.intraday.candidates import IntradayCandidates, record_dropped_name
from src.intraday.gating import IntradayGating
from src.intraday.safety import IntradaySafety
from src.intraday.session import IntradaySession
from src.pipeline_context import RunContext
from src.sentinel.guarded_site import record_site as _site
from src.pipeline_stages import _persist_evidence, _record_pipeline_event

# --- ONE mirror block: names the moved bodies used to resolve through this module ---
import contextlib  # noqa: F401 -- re-exported
from pathlib import Path  # noqa: F401 -- re-exported

from src.intraday_scan_outcome import failed_scan_result  # noqa: F401 -- re-exported
from src.trading_calendar import session_date_key  # noqa: F401 -- re-exported
# --- end mirror block ---

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class _HostState:
    """Live get/set view of the host attributes a part reads AND assigns (never a copy)."""

    def __init__(self, host) -> None:
        self._host = host

    def get(self, name: str):
        return getattr(self._host, name)

    def set(self, name: str, value) -> None:
        setattr(self._host, name, value)


#: The ranked movers sent in a check's ONE paid call (owner ruling
#: 2026-10-12: at most one batch per check, the strongest-ranked). 5 is the
#: batch size the desk has always sent (the old per-scan name cap). The
#: spent-money cap (`llm_cost_circuit`) is checked before the call and can be
#: overshot by at most that one call: the most expensive intraday call on
#: record is a portfolio_manager decision at $0.389885 [measured: production
#: agent_logs, intraday runs 2026-08-25..2026-10-09, 117 calls]; the largest
#: intraday tech batch was $0.1237169 [same source, 139 calls]. Ledgered in
#: config/number_ledger.yaml.
_NAMES_PER_PAID_CALL = 5


def _shim(fn):
    """Mark a mixin method as this module's own shim so a live collaborator can tell it apart."""
    fn._intraday_shim = fn.__name__
    return fn


class IntradayScanBody:
    """The paid intraday scan body; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        config=None,
        db=None,
        market=None,
        tech_store=None,
        macro_store=None,
        tech_analyst=None,
        decision_stage=None,
        risk_stage=None,
        execution_stage=None,
        await_paid_scan_slot=None,
        carry_forward_earnings=None,
        carry_forward_insider=None,
        carry_forward_macro=None,
        carry_forward_news=None,
        compute_deployable_cash=None,
        evidence_gate_skip=None,
        heal_lost_research_seats=None,
        intraday_buying_room=None,
        intraday_held_tech_symbols=None,
        intraday_open_overlap_skip=None,
        intraday_paid_scan_skip=None,
        intraday_scan_mover_candidates=None,
        record_intraday_trigger_atr_context=None,
        refresh_account_state=None,
        require_paid_analysis=None,
        resolve_live_context=None,
        sync_positions_from_broker=None,
        state=None,
    ) -> None:
        self.config = config
        self.db = db
        self.market = market
        self.tech_store = tech_store
        self.macro_store = macro_store
        self.tech_analyst = tech_analyst
        self.decision_stage = decision_stage
        self.risk_stage = risk_stage
        self.execution_stage = execution_stage
        self._await_paid_scan_slot = await_paid_scan_slot
        self._carry_forward_earnings = carry_forward_earnings
        self._carry_forward_insider = carry_forward_insider
        self._carry_forward_macro = carry_forward_macro
        self._carry_forward_news = carry_forward_news
        self._compute_deployable_cash = compute_deployable_cash
        self._evidence_gate_skip = evidence_gate_skip
        self._heal_lost_research_seats = heal_lost_research_seats
        self._intraday_buying_room = intraday_buying_room
        self._intraday_held_tech_symbols = intraday_held_tech_symbols
        self._intraday_open_overlap_skip = intraday_open_overlap_skip
        self._intraday_paid_scan_skip = intraday_paid_scan_skip
        self._intraday_scan_mover_candidates = intraday_scan_mover_candidates
        self._record_intraday_trigger_atr_context = record_intraday_trigger_atr_context
        self._refresh_account_state = refresh_account_state
        self._require_paid_analysis = require_paid_analysis
        self._resolve_live_context = resolve_live_context
        self._sync_positions_from_broker = sync_positions_from_broker
        self._state = state

    @property
    def _paid_scan_waited(self):
        return self._state.get("_paid_scan_waited")

    @_paid_scan_waited.setter
    def _paid_scan_waited(self, value) -> None:
        self._state.set("_paid_scan_waited", value)

    @property
    def _paid_scan_waited_for(self):
        return self._state.get("_paid_scan_waited_for")

    @_paid_scan_waited_for.setter
    def _paid_scan_waited_for(self, value) -> None:
        self._state.set("_paid_scan_waited_for", value)

    @property
    def broker(self):
        return self._state.get("broker")

    @broker.setter
    def broker(self, value) -> None:
        self._state.set("broker", value)

    def _intraday_opportunity_scan_body(self, ctx: RunContext) -> dict:
        """Bounded intraday opportunity discovery (2026-08-19 fix).

        Runs on the existing intra_check cadence — no new systemd timer,
        no full morning research stack. One cheap bulk current-session
        snapshot call flags symbols that moved materially since the last
        close; those movers (capped, cooldown-deduped against repeat churn)
        PLUS currently held investable names get real daily bars/indicators
        and a real tech_analyst call. Held names join the batch so an
        increase on a quiet hold has current-run Technical and can ground;
        they do not consume the mover cap or the mover cooldown. Then the
        SAME DecisionStage -> RiskStage -> ExecutionStage chain morning
        uses — no separate/duplicated decision logic, so PM's sizing rules,
        RM's veto authority and the deterministic gate all apply exactly
        as they do in the morning run. Bullish AND bearish setups both
        surface: the universe already includes the approved inverse ETFs
        (SH/SDS/PSQ/SQQQ), so a broad-market decline shows up as a
        qualifying move in those symbols the same way a rally shows up in
        a long candidate — no separate bearish code path needed.

        Returns a status dict at every early-exit point — never a bare
        None (2026-08-31 visibility fix; see `_run_intraday_opportunity_scan`
        for the full rationale). "intraday_scan_open_overlap" when morning
        released the owner lock on this same 09:30-shared tick (item 121 —
        still the open, not INTRADAY); "intraday_scan_lock_contended" when
        `_await_paid_scan_slot` cannot free the owner lock before this
        tick's calendar window ends; "intraday_scan_no_opportunity" for
        every other early return (no
        snapshots, no qualifying moves, no ledgerable symbols, no usable
        bars). A tech seat that is fully LOST (every submitted symbol
        failed, or the batch call raised) is NOT folded into that status —
        item 20 (board) — it is classified `data_status["tech"]="failed"`
        and routed through the same evidence-gate skip + unsuppressible
        alert morning uses. Past that point, a real result dict
        mirroring the shape callers of run_morning already expect
        (status/orders/run_id). Best-effort: any failure degrades to a
        status dict, never raises (the caller also wraps this
        defensively) — a scan miss costs a possible trade; a scan crash
        must never cost the loss-protection check that already ran this
        tick.

        The enabled-check and the process-level lock live in the
        `_run_intraday_opportunity_scan` wrapper; this is the body.
        """
        cfg = self.config.intraday_scan

        # Identify movers first (cheap snapshot) so a morning/midday owner
        # lock cannot vanish paid discovery. Then wait. If the lock is
        # still held at window end, skip with a durable reason that names
        # the movers — never sleep the scan away, never drop them silently.
        candidates, snapshots = self._intraday_scan_mover_candidates(ctx)
        mover_names = [s for s, _ in candidates]
        if self._await_paid_scan_slot(ctx.run_id):
            if getattr(self, "_paid_scan_waited_for", None) == "morning":
                return self._intraday_open_overlap_skip(ctx, mover_names)
            return self._intraday_paid_scan_skip(ctx, mover_names)
        # The 09:30/13:00 wait must not size against the pre-fill snapshot
        # taken before morning finished. Refresh after the lock releases.
        if getattr(self, "_paid_scan_waited", False):
            try:
                account, positions, _ = self._refresh_account_state()
                ctx.account = account
                ctx.positions = positions
                ctx.cash = account["cash"]
                ctx.deployable_cash = self._compute_deployable_cash(
                    ctx.cash,
                    positions,
                )
                ctx.total_value = account.get("portfolio_value", ctx.total_value)
                self._sync_positions_from_broker(positions)
            except Exception as exc:  # noqa: BLE001
                _site(self, "post_wait_refresh", exc, log=logger)
                return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
            candidates, snapshots = self._intraday_scan_mover_candidates(ctx)

        if not snapshots or not candidates:
            # Owner rulings 2026-10-01 ("multiple times a day") and
            # 2026-10-12: with no movers there is no new idea to price, but
            # every held name is still re-read and the below-entry-bar sale
            # still runs - with NO portfolio-manager call.
            return self._intraday_scan_call(ctx, [], snapshots or {}, include_holds=True)

        # Owner ruling 2026-10-12: "Later batches are weaker moves. So why
        # send them at all?" ONE paid call per check: the strongest-ranked
        # `_NAMES_PER_PAID_CALL` movers (largest move in the name's own ATR
        # first). The spent-money cap stays as a backstop: a refusal records
        # every mover `budget_refused` with its rank and keeps the suspension
        # as the scan's status. Names past the batch are recorded
        # `not_sent_weaker_rank`, never dropped silently.
        try:
            self._require_paid_analysis("intraday_tech_analyst")
        except PaidAnalysisSuspended as exc:
            for rank, (late, late_move) in enumerate(candidates, start=1):
                record_dropped_name(self, ctx, late, "budget_refused", rank=rank, move_pct=late_move, detail=str(exc))
            raise
        for rank, (late, late_move) in enumerate(candidates[_NAMES_PER_PAID_CALL:], start=_NAMES_PER_PAID_CALL + 1):
            record_dropped_name(
                self,
                ctx,
                late,
                "not_sent_weaker_rank",
                rank=rank,
                move_pct=late_move,
                detail=f"one paid call per check; only the top {_NAMES_PER_PAID_CALL} ranked movers are sent",
            )
        return self._intraday_scan_call(ctx, candidates[:_NAMES_PER_PAID_CALL], snapshots, include_holds=True)

    def _intraday_scan_call(self, ctx: RunContext, candidates, snapshots, *, include_holds: bool) -> dict:
        """One paid call's worth of ranked movers through Tech -> PM -> RM -> execution.

        Held-book coverage rides only on the first call, so a hold is read once.
        """
        cfg = self.config.intraday_scan
        symbols = [s for s, _ in candidates]
        # No movers: a held-book check only (below-entry-bar sale, no PM).
        held_only = not symbols
        logger.info(
            "Intraday scan: %d symbol(s) ranked by move/ATR, outside the %.1fh cooldown, within budget: %s",
            len(symbols),
            cfg.cooldown_hours,
            symbols,
        )
        move_by_symbol = dict(candidates)
        # The cooldown row is written only once the PM has actually seen the
        # movers (adversary review 2026-10-12): a check that skipped the PM
        # for no room, or stopped before it, must not hide them for
        # `cooldown_hours` unseen. See `_record_mover_cooldown`.
        for symbol in symbols:
            _record_pipeline_event(
                self,
                ctx,
                symbol,
                "opportunity",
                "discovered",
                "intraday_move_threshold",
                move_pct=move_by_symbol[symbol],
            )

        # Produce Technical for quiet holds on the same paid call. Discovery
        # stays mover-capped; coverage for names the PM can increase does
        # not compete with that cap and does not consume mover cooldown.
        # Dropping an ungrounded hold is not the product for missing Tech.
        held_for_tech = (
            [s for s in self._intraday_held_tech_symbols(ctx) if s not in {x.upper() for x in symbols}]
            if include_holds
            else []
        )
        if held_only and not held_for_tech:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
        if held_for_tech:
            logger.info(
                "Intraday scan: producing Technical for %d held name(s) the mover list did not cover: %s",
                len(held_for_tech),
                held_for_tech,
            )
        tech_symbols = list(symbols) + held_for_tech
        # Item 177: the mover set, so the ATR context below is stamped only
        # on names the flat `move_threshold_pct` trigger actually selected —
        # held-book coverage never went through that trigger and must not be
        # mixed into the measurement that will answer for it.
        symbols_set = {s.upper() for s in symbols}

        symbols_data = []
        symbols_bars: dict[str, list] = {}
        for symbol in tech_symbols:
            try:
                bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days)
            except Exception as e:  # noqa: BLE001
                _site(self, "bar_fetch", e, context={"symbol": symbol}, log=logger)
                _record_pipeline_event(
                    self,
                    ctx,
                    symbol,
                    "specialist",
                    "failed",
                    "market_data_exception",
                    detail=str(e),
                    specialist="tech_analyst",
                )
                continue
            if not bars:
                _record_pipeline_event(
                    self,
                    ctx,
                    symbol,
                    "specialist",
                    "failed",
                    "market_data_unavailable",
                    specialist="tech_analyst",
                )
                continue
            indicators = compute_indicators(symbol, bars)
            self._record_intraday_trigger_atr_context(
                ctx,
                symbol,
                symbols_set,
                move_by_symbol,
                snapshots,
                indicators,
            )
            symbols_data.append({"symbol": symbol, "bars": bars, "indicators": indicators})
            symbols_bars[symbol] = bars
        if not symbols_data:
            return {"status": "intraday_scan_no_opportunity", "run_id": ctx.run_id}
        ctx.symbols_bars = symbols_bars

        prior_macro_state: dict = {}
        try:
            prior_macro_state = self.macro_store.load_last_state() or {}
        except Exception as e:  # noqa: BLE001
            _site(self, "macro_state_load", e)
        prior_ratings: dict = {}
        try:
            prior_ratings = self.tech_store.load()
        except Exception as e:  # noqa: BLE001
            _site(self, "tech_store_load", e)
        else:
            _site(self, "tech_store_load")

        # Truthful current-session evidence for exactly the names being
        # analyzed (2026-08-19): the scan detects on live prices, so Tech
        # must see those same live prices — not just daily bars ending at
        # yesterday's close, which is what triggered the scan being
        # invisible to the analyst that had to judge it. Rendered by
        # `build_user_message` as an explicit INCOMPLETE-session block,
        # never as a completed daily bar. Held names already in the
        # universe snapshot are included; a hold outside that snapshot
        # still gets bars, just no live-session block.
        # item 120: resolved through the SAME freshness rule the morning
        # pass uses. This used to hand Tech the raw snapshot, so a name
        # whose last trade was a prior session's could be rendered to the
        # intraday seat as "CURRENT SESSION (TODAY)".
        intraday_context, _missing, _stale, _rescued = self._resolve_live_context(
            snapshots,
            [s for s in tech_symbols if s in snapshots],
        )
        if _missing or _stale:
            logger.warning(
                "Intraday scan: no today print for %d symbol(s) handed to Tech "
                "(no price: %s; no today print: %s) — labelled as a lost price "
                "seat, never replaced by a prior session's number",
                len(_missing) + len(_stale),
                _missing[:10],
                _stale[:10],
            )
        self._require_paid_analysis("intraday_tech_analyst")
        try:
            analyses_map, ta_result = self.tech_analyst.analyze_batch(
                symbols_data,
                prior_ratings=prior_ratings,
                valuations={},
                intraday_context=intraday_context,
                prior_macro_regime=prior_macro_state.get("regime"),
                prior_macro_outlook=prior_macro_state.get("equity_outlook"),
            )
        except PaidAnalysisSuspended:
            raise
        except Exception as e:  # noqa: BLE001 — mirrors morning's tech
            # try/except (pipeline_stages.py): a bare call here had no
            # guard at all, so a batch-level raise (provider outage,
            # unparseable response) crashed the whole intraday tick
            # instead of being recorded as a LOST tech seat like every
            # other failure mode this scan already handles.
            _site(self, "tech_batch", e, log=logger)
            analyses_map, ta_result = {}, None
        # analyses_map carries every candidate symbol as a key (2026-08-19
        # Tech batch-response symbol-loss fix) — None marks a symbol
        # tech_analyst could not resolve even after its own bounded retry.
        # Filter before treating entries as real analyses.
        analyses = [a for a in analyses_map.values() if a is not None]
        failed_count = len(analyses_map) - len(analyses)
        if failed_count:
            logger.warning(
                "Intraday scan: %d/%d candidate symbol(s) failed to resolve even after retry: %s",
                failed_count,
                len(analyses_map),
                sorted(sym for sym, a in analyses_map.items() if a is None),
            )
        if ta_result:
            try:
                self.db.insert_agent_log(
                    **seat_acceptance_kwargs("failed" if not analyses else None),
                    agent_name="tech_analyst",
                    run_id=ctx.run_id,
                    input_summary=(
                        f"Intraday scan batch: {len(analyses)}/{len(analyses_map)} "
                        f"symbols analyzed" + (f", {failed_count} failed" if failed_count else "")
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
            except Exception as e:  # noqa: BLE001
                _site(self, "tech_agent_log", e)
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
        if analyses:
            try:
                self.tech_store.update(analyses)
                ages = self.tech_store.compute_ages([a.symbol for a in analyses])
                for analysis in analyses:
                    if analysis.symbol in ages:
                        analysis.signal_age_days = ages[analysis.symbol]
            except Exception as e:  # noqa: BLE001
                _site(self, "tech_store_update", e)

        # Item 20 (board): deliberately no early "no analyses" return here.
        # `symbols_data` was already confirmed non-empty above, so zero
        # usable analyses at this point is a LOST tech seat, not a quiet
        # tick — it must fall through to the shared `data_status`/gate path
        # below (classification just before `ctx.data_status`), not return
        # "intraday_scan_no_opportunity" indistinguishably from a real
        # empty candidate set.

        # Same shared chain morning uses — no separate PM/RM/gate logic.
        #
        # Macro/news/earnings are still NOT re-fetched this tick — that is the
        # expensive research stack this scan exists to avoid rerunning, and
        # the saving is the whole point. But "not re-run" was previously
        # implemented as "not shown", and those are different things. This
        # session was handing the Portfolio Manager a technical-only view
        # while THIS MORNING'S macro regime and news sat on disk, already
        # paid for. The PM was blindfolded, not economical: `intra_check`
        # measured at $0.222/run against `morning`'s $0.221 over the 10 days
        # to 2026-08-27, ~99% of it the PM call, deciding on a fraction of
        # the evidence.
        #
        # So: carry the morning's results forward, and label them as carried.
        # The grounding property that mattered is preserved — nothing is
        # presented as having run this tick — while the PM stops reasoning
        # about an intraday move with no idea what regime it is happening in.
        ctx.analyses = analyses
        carried_macro = self._carry_forward_macro()
        carried_news = self._carry_forward_news(ctx)
        carried_earnings = self._carry_forward_earnings(ctx)
        carried_insider = self._carry_forward_insider(ctx)
        # Item 20 (board): three-way, matching morning's classification.
        # `symbols_data` was non-empty going in, so `analyses` empty here
        # means every submitted symbol failed (or the batch call raised,
        # caught above) — a LOST seat, not an ordinary quiet tick. A
        # partial batch (some resolved) stays REPORTED, exactly as before —
        # this must not start blocking intraday trading on one bad symbol.
        if analyses:
            tech_status = "partial" if failed_count else "ok"
        else:
            tech_status = "failed"
            logger.error(
                "Intraday scan: tech seat LOST — %d/%d submitted symbol(s) resolved to a usable analysis this tick",
                len(analyses),
                len(analyses_map),
            )
        ctx.data_status = {
            "tech": tech_status,
            # Status comes from the kind+event helpers, not from payload
            # truthiness. Same-session GOOD reuse is `carried_from_morning`
            # (PR #430). Cross-day GOOD macro is `remembered`. Empty/failed
            # carry still refuses BEFORE the Portfolio Manager. Earnings
            # without a provider on this object stays the intentional skip.
            "macro": carried_macro.status,
            "news": carried_news.status,
            "earnings": carried_earnings.status,
            "smart_money": carried_insider.status,
        }
        ctx.macro_analysis = carried_macro.payload
        ctx.news_intel = carried_news.payload
        ctx.earnings_results = list(carried_earnings.payload or [])
        ctx.smart_money_findings = list(carried_insider.payload or [])
        self._heal_lost_research_seats(ctx)

        # Same owner rule as morning: a decision on incomplete evidence is
        # fabricated. Applied here now that empty/failed carry-forward is
        # distinguishable from the intentional earnings skip.
        gate_skip = self._evidence_gate_skip(
            ctx,
            ctx.run_id,
            session=ctx.session,
        )
        if gate_skip is not None:
            gate_skip["candidates"] = symbols
            return gate_skip

        if held_only:
            ctx.pm_skip_reason = "intraday_no_movers"
        # Owner ruling 2026-10-12 (approved): no paid PM buy call when the
        # account has no room to open any new position. Same headroom
        # execution sizes against, same minimum-risk floor at each mover's
        # own stop; the below-entry-bar sale still runs without the PM.
        # The room is judged INSIDE the decision step, after the PM-free
        # below-entry-bar sale is known, on the post-sale book: a sale that
        # frees room can fund a buy in the same check.
        if not held_only:
            ctx.pm_room_check = lambda c, sold: self._intraday_no_buying_room(c, analyses, symbols, sold)
        self.decision_stage.run(ctx)
        no_room = getattr(ctx, "pm_room_skip", None)
        if not held_only and not getattr(ctx, "pm_skip_reason", None):
            self._record_mover_cooldown(ctx, symbols, move_by_symbol)
        if not ctx.portfolio_decision and (held_only or no_room is not None):
            return {
                "status": NO_BUYING_ROOM if no_room is not None else "intraday_scan_no_opportunity",
                "held_book_checked": True,
                **({"candidates": symbols, **no_room} if no_room is not None else {}),
                "run_id": ctx.run_id,
            }
        if not ctx.portfolio_decision:
            logger.error(
                "Intraday scan: PM failed (%s): %s",
                ctx.analysis_failure_status,
                ctx.analysis_failure_error,
            )
            return {
                "status": "intraday_analysis_error",
                "failure_status": ctx.analysis_failure_status or "pm_agent_failure",
                "error": ctx.analysis_failure_error or "no valid PM decision",
                "candidates": symbols,
                "run_id": ctx.run_id,
            }
        if not ctx.portfolio_decision.decisions:
            logger.info("Intraday scan: PM produced no actionable decisions")
            return {
                "status": "intraday_no_trades",
                "candidates": symbols,
                "run_id": ctx.run_id,
            }

        early_exit = self.risk_stage.run(ctx)
        if early_exit is not None:
            early_exit["candidates"] = symbols
            early_exit.update(no_room or {})
            stop_updates = getattr(
                getattr(self, "broker", None),
                "stop_trade_updates",
                None,
            )
            if callable(stop_updates):
                try:
                    stop_updates()
                except Exception as exc:  # noqa: BLE001
                    _site(self, "stop_updates", exc, log=logger)
            return early_exit

        orders = self.execution_stage.run(ctx)
        return {
            "status": "intraday_executed" if orders else "intraday_no_trades",
            "candidates": symbols,
            "orders": orders,
            **(no_room or {}),
            "run_id": ctx.run_id,
        }

    def _record_mover_cooldown(self, ctx: RunContext, symbols, move_by_symbol) -> None:
        """Start each mover's cooldown, now that the PM has seen it. Every PM
        outcome (a decision, no target, or a model failure) consumes it."""
        stamped = ctx.__dict__.get("_mover_ledger_detail") or {}
        for symbol in symbols:
            try:
                self.db.record_intraday_evaluation(
                    symbol=symbol,
                    run_id=ctx.run_id,
                    status="selected",
                    detail=stamped.get(symbol.upper()) or f"move_pct={move_by_symbol[symbol]:.4f}",
                )
            except Exception as exc:  # noqa: BLE001
                _site(self, "evaluation_ledger", exc, context={"symbol": symbol}, log=logger)

    def _intraday_no_buying_room(self, ctx: RunContext, analyses, symbols, sold=()) -> dict | None:
        """The durable skip record when no mover can be opened, else None (the PM is called).

        An unreadable assessment, or none (ranked-margin rotation switched
        on can free room), calls the PM as before.
        """
        try:
            room = self._intraday_buying_room(ctx, analyses, symbols, sold)
        except Exception as exc:  # noqa: BLE001
            _site(self, "buying_room", exc, log=logger)
            return None
        if room is None or room.has_room:
            return None
        record = room.as_record()
        logger.info(
            "Intraday scan: no paid PM call -- no room to open any mover (%s; candidates %s)",
            record["limit"],
            record["candidates_considered"],
        )
        # One durable row per mover per skipped check, in the pipeline-event stream.
        for row in record["candidates_considered"]:
            record_dropped_name(
                self,
                ctx,
                row["symbol"],
                NO_BUYING_ROOM,
                headroom_usd=record["headroom_usd"],
                min_notional_usd=row["min_notional_usd"],
                limit=record["limit"],
            )
        return record


class IntradayMixin:
    """Intra-check session (cluster R) and intraday opportunity scan (cluster W); shims only.

    Each `_intraday_*` builder reads the host's collaborators at call time. A body a
    part reads through `self.` that lives on the SAME part is handed in through
    `_intraday_live`: it honours an instance- or class-level swap on the host and runs
    the part's own body while the host still carries this module's shim, so nothing
    recurses and nothing is snapshotted."""

    def _intraday_live(self, name: str, build):
        """Collaborator read off the host at each call; own body when the host still holds our shim."""

        def collaborator(*args, **kwargs):
            swapped = self.__dict__.get(name)
            if swapped is not None:
                return swapped(*args, **kwargs)
            on_class = inspect.getattr_static(type(self), name, None)
            if getattr(on_class, "_intraday_shim", None) == name:
                part = build()
                return getattr(type(part), name)(part, *args, **kwargs)
            return getattr(self, name)(*args, **kwargs)

        collaborator.__name__ = name
        return collaborator

    def _intraday_safety(self) -> IntradaySafety:
        """The IntradaySafety part, wired to this host live; bodies in src/intraday/safety.py."""
        return IntradaySafety(
            db=getattr(self, "db", None),
            is_trading_day=getattr(self, "_is_trading_day", None),
            kill_switch_halt_result=getattr(self, "_kill_switch_halt_result", None),
            blocking_owner_session=getattr(self, "_blocking_owner_session", None),
            drain_pending_protection_restores=getattr(self, "_drain_pending_protection_restores", None),
            drain_pending_repegs=getattr(self, "_drain_pending_repegs", None),
            intraday_scan_process_lock=getattr(self, "_intraday_scan_process_lock", None),
            reconcile_fills=getattr(self, "_reconcile_fills", None),
            reconcile_orphan_pending_submits=getattr(self, "_reconcile_orphan_pending_submits", None),
            reconcile_stop_coverage=getattr(self, "_reconcile_stop_coverage", None),
            reconcile_stop_out_fills=getattr(self, "_reconcile_stop_out_fills", None),
            release_retired_cash_park=getattr(self, "_release_retired_cash_park", None),
            surface_reconcile_outcomes=getattr(self, "_surface_reconcile_outcomes", None),
            run_intra_safety_preamble=self._intraday_live("_run_intra_safety_preamble", self._intraday_safety),
            state=_HostState(self),
        )

    def _intraday_session(self) -> IntradaySession:
        """The IntradaySession part, wired to this host live; bodies in src/intraday/session.py."""
        return IntradaySession(
            db=getattr(self, "db", None),
            broker=getattr(self, "broker", None),
            install_sigterm_unwind=getattr(self, "_install_sigterm_unwind", None),
            repair_stops_on_kill=getattr(self, "_repair_stops_on_kill", None),
            restore_sigterm=getattr(self, "_restore_sigterm", None),
            is_trading_day=getattr(self, "_is_trading_day", None),
            kill_switch_halt_result=getattr(self, "_kill_switch_halt_result", None),
            run_intra_safety_preamble=getattr(self, "_run_intra_safety_preamble", None),
            attach_evidence_freshness=getattr(self, "_attach_evidence_freshness", None),
            attach_pnl=getattr(self, "_attach_pnl", None),
            activate_cost_session=getattr(self, "_activate_cost_session", None),
            compute_deployable_cash=getattr(self, "_compute_deployable_cash", None),
            record_account_snapshot=getattr(self, "_record_account_snapshot", None),
            run_intraday_opportunity_scan=getattr(self, "_run_intraday_opportunity_scan", None),
            sync_positions_from_broker=getattr(self, "_sync_positions_from_broker", None),
            total_pnl_since_reset=getattr(self, "_total_pnl_since_reset", None),
            persist_intra_check_report=self._intraday_live("_persist_intra_check_report", self._intraday_session),
            run_intra_check_body=self._intraday_live("_run_intra_check_body", self._intraday_session),
            state=_HostState(self),
        )

    def _intraday_gating(self) -> IntradayGating:
        """The IntradayGating part, wired to this host live; bodies in src/intraday/gating.py."""
        return IntradayGating(
            db=getattr(self, "db", None),
            config=getattr(self, "config", None),
            blocking_owner_session=self._intraday_live("_blocking_owner_session", self._intraday_gating),
            intra_window_remaining_s=self._intraday_live("_intra_window_remaining_s", self._intraday_gating),
            state=_HostState(self),
        )

    def _intraday_candidates(self) -> IntradayCandidates:
        """The IntradayCandidates part, wired to this host live; bodies in src/intraday/candidates.py."""
        return IntradayCandidates(
            config=getattr(self, "config", None),
            broker=getattr(self, "broker", None),
            db=getattr(self, "db", None),
            sweeper=getattr(self, "_sweeper", None),
            retired_cash_park_symbol=getattr(self, "_retired_cash_park_symbol", None),
            intraday_opportunity_scan_body=getattr(self, "_intraday_opportunity_scan_body", None),
            intraday_scan_process_lock=getattr(self, "_intraday_scan_process_lock", None),
            recently_intraday_evaluated=getattr(self, "_recently_intraday_evaluated", None),
            track_intraday_snapshot_ok=getattr(self, "_track_intraday_snapshot_ok", None),
            track_intraday_snapshot_miss=getattr(self, "_track_intraday_snapshot_miss", None),
            blocking_owner_session=getattr(self, "_blocking_owner_session", None),
            atr_for_symbol=getattr(self, "_atr_for_symbol", None),
        )

    def _intraday_buying_room(self, ctx: RunContext, analyses, symbols, sold=()):
        """Buying room for this check's movers (src/intraday/buying_room.py); None while
        ranked-margin rotation is switched on, since it can free room."""
        from src.pipeline_rotation_exec import _rotation_ranked_margin_enabled

        if _rotation_ranked_margin_enabled(self):
            return None
        return assess_buying_room(self, ctx, analyses, symbols, sold)

    def _intraday_scan_body(self) -> IntradayScanBody:
        """The IntradayScanBody part, wired to this host live; bodies in src/pipeline_intraday.py (IntradayScanBody)."""
        return IntradayScanBody(
            config=getattr(self, "config", None),
            db=getattr(self, "db", None),
            market=getattr(self, "market", None),
            tech_store=getattr(self, "tech_store", None),
            macro_store=getattr(self, "macro_store", None),
            tech_analyst=getattr(self, "tech_analyst", None),
            decision_stage=getattr(self, "decision_stage", None),
            risk_stage=getattr(self, "risk_stage", None),
            execution_stage=getattr(self, "execution_stage", None),
            await_paid_scan_slot=getattr(self, "_await_paid_scan_slot", None),
            carry_forward_earnings=getattr(self, "_carry_forward_earnings", None),
            carry_forward_insider=getattr(self, "_carry_forward_insider", None),
            carry_forward_macro=getattr(self, "_carry_forward_macro", None),
            carry_forward_news=getattr(self, "_carry_forward_news", None),
            compute_deployable_cash=getattr(self, "_compute_deployable_cash", None),
            evidence_gate_skip=getattr(self, "_evidence_gate_skip", None),
            heal_lost_research_seats=getattr(self, "_heal_lost_research_seats", None),
            intraday_buying_room=getattr(self, "_intraday_buying_room", None),
            intraday_held_tech_symbols=getattr(self, "_intraday_held_tech_symbols", None),
            intraday_open_overlap_skip=getattr(self, "_intraday_open_overlap_skip", None),
            intraday_paid_scan_skip=getattr(self, "_intraday_paid_scan_skip", None),
            intraday_scan_mover_candidates=getattr(self, "_intraday_scan_mover_candidates", None),
            record_intraday_trigger_atr_context=getattr(self, "_record_intraday_trigger_atr_context", None),
            refresh_account_state=getattr(self, "_refresh_account_state", None),
            require_paid_analysis=getattr(self, "_require_paid_analysis", None),
            resolve_live_context=getattr(self, "_resolve_live_context", None),
            sync_positions_from_broker=getattr(self, "_sync_positions_from_broker", None),
            state=_HostState(self),
        )

    @_shim
    def run_intra_safety(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/safety.py."""
        return self._intraday_safety().run_intra_safety(*args, **kwargs)

    @_shim
    def _run_intra_safety_preamble(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/safety.py."""
        return self._intraday_safety()._run_intra_safety_preamble(*args, **kwargs)

    @_shim
    def run_intra_check(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/session.py."""
        return self._intraday_session().run_intra_check(*args, **kwargs)

    @_shim
    def _persist_intra_check_report(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/session.py."""
        return self._intraday_session()._persist_intra_check_report(*args, **kwargs)

    @_shim
    def _run_intra_check_body(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/session.py."""
        return self._intraday_session()._run_intra_check_body(*args, **kwargs)

    @_shim
    def _recently_intraday_evaluated(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/gating.py."""
        return self._intraday_gating()._recently_intraday_evaluated(*args, **kwargs)

    @_shim
    def _blocking_owner_session(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/gating.py."""
        return self._intraday_gating()._blocking_owner_session(*args, **kwargs)

    @_shim
    def _intra_window_remaining_s(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/gating.py."""
        return self._intraday_gating()._intra_window_remaining_s(*args, **kwargs)

    @_shim
    def _await_paid_scan_slot(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/gating.py."""
        return self._intraday_gating()._await_paid_scan_slot(*args, **kwargs)

    @_shim
    def _another_session_recently_active(self, run_id: str, within_minutes: float = 15.0) -> bool:
        """Thin shim: body moved to src/intraday/gating.py; the default stays here for the ledger."""
        return self._intraday_gating()._another_session_recently_active(run_id, within_minutes=within_minutes)

    @_shim
    def _intraday_scan_process_lock(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/gating.py."""
        return self._intraday_gating()._intraday_scan_process_lock(*args, **kwargs)

    @_shim
    def _track_intraday_snapshot_ok(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/gating.py."""
        return self._intraday_gating()._track_intraday_snapshot_ok(*args, **kwargs)

    @_shim
    def _track_intraday_snapshot_miss(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/gating.py."""
        return self._intraday_gating()._track_intraday_snapshot_miss(*args, **kwargs)

    @_shim
    def _run_intraday_opportunity_scan(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/candidates.py."""
        return self._intraday_candidates()._run_intraday_opportunity_scan(*args, **kwargs)

    @_shim
    def _intraday_held_tech_symbols(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/candidates.py."""
        return self._intraday_candidates()._intraday_held_tech_symbols(*args, **kwargs)

    @_shim
    def _intraday_scan_mover_candidates(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/candidates.py."""
        return self._intraday_candidates()._intraday_scan_mover_candidates(*args, **kwargs)

    @staticmethod
    def _intraday_move_in_atr(*args, **kwargs):
        """Thin shim: body moved to src/intraday/candidates.py."""
        return IntradayCandidates._intraday_move_in_atr(*args, **kwargs)

    @_shim
    def _record_intraday_trigger_atr_context(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/candidates.py."""
        return self._intraday_candidates()._record_intraday_trigger_atr_context(*args, **kwargs)

    @_shim
    def _intraday_paid_scan_skip(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/candidates.py."""
        return self._intraday_candidates()._intraday_paid_scan_skip(*args, **kwargs)

    @_shim
    def _intraday_open_overlap_skip(self, *args, **kwargs):
        """Thin shim: body moved to src/intraday/candidates.py."""
        return self._intraday_candidates()._intraday_open_overlap_skip(*args, **kwargs)

    @_shim
    def _intraday_opportunity_scan_body(self, *args, **kwargs):
        """Thin shim: body moved to IntradayScanBody above."""
        return self._intraday_scan_body()._intraday_opportunity_scan_body(*args, **kwargs)
