"""Execution stage (split out of src/pipeline_stages.py, step 10).

Moved verbatim in the staged split (board item 210, step 10). No behaviour change:
the class body below is byte-for-byte the text that used to live in
``src/pipeline_stages.py``, and ``src.pipeline_stages`` re-exports it so every
existing import path and every ``src.pipeline_stages.ExecutionStage`` patch target
still resolves to this same object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.delever.risk_number import _optional_risk_number
from src.entry_evidence import (
    record_scale_in_own_verdict as _record_scale_in_own_verdict,
    resolve_entry_pins as _resolve_entry_pins,
)
from src.entry_record import insert_pending_entry
from src.exit_quote import read_exit_quote
from src.price_feed_preflight import preflight_price_feed, price_feed_session_start
from src.sizing_refusal import classified_no_price, sizing_price_or_refusal
from src.stage_entry_preflight import entry_viability_preflight
from src.stage_execution_parts.cover_loop import await_cover_and_finalize, cover_qty_and_label
from src.stage_execution_parts.entry_geometry import entry_stop_price
from src.stage_execution_parts.entry_order_pricing import price_entry
from src.stage_execution_parts.entry_sizing import entry_qty
from src.stage_execution_parts.protect_entry_stops import protect_pending_entry_stops
from src.stage_execution_parts.sell_loop import (
    await_sell_and_finalize,
    record_rotation_close,
    sell_qty_and_label,
)
from src.stage_execution_parts.state import SKIP, EntryLeg, EntryRun, SellLeg
from src.risk.min_risk import min_risk_shortfall
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    LEVEL_BACKED_STOP_RULES,
    RunContext,
    _ROTATION_SELL_REFUSED,
    _adopt_stream_stall,
    _alert_owner_entry_cancelled,
    _alert_owner_protection_failed,
    _alert_rotation_executed,
    _drop_rotation_buy_if_room_not_freed,
    _encode_entry_submit_window,
    _entry_deployment_budget,
    _entry_slippage_bps,
    _execution_payoff_skip_reason,
    _fmt_shares,
    _fractional_sizing_allowed,
    _live_fill_price,
    _min_position_risk_pct,
    _pin_approved_entry_ceilings,
    _qty_by_risk_budget,
    _record_execution_skip,
    _record_pipeline_event,
    _record_rotation_buy_leg_outcome,
    _record_scale_in_window_closed,
    _repeg_entry_order,
    _risk_budget_pct,
    _rotation_ranked_margin_sell_reason,
    _drop_buys_sold_today_below_bar,
    _rotation_sell_gate,
    _rotation_sell_last,
    _single_name_execution_cap,
    _size_shares,
    _stop_trade_updates,
    _submit_window_overrun,
    _today_sizing_price,
    _warm_trade_updates,
    compute_indicators,
    logger,
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


class ExecutionStage:
    """Record HOLDs → submit SELLs → wait → refresh → submit BUYs.

    Reads:  ctx.portfolio_decision.decisions, ctx.positions, ctx.cash,
            ctx.total_value, ctx.symbols_bars
    Writes: ctx.orders, and on SELL refresh: ctx.positions / .cash / .total_value
    """

    def __init__(self, *, pipeline: "TradingPipeline"):
        self._pipeline = pipeline

    def run(self, ctx: RunContext) -> list[dict]:
        try:
            price_feed_session_start(self._pipeline, ctx)
            return self._run_session(ctx)
        finally:
            _stop_trade_updates(self._pipeline)

    def _run_session(self, ctx: RunContext) -> list[dict]:
        pipeline = self._pipeline
        run_id = ctx.run_id
        # Stage 1 (QAMC correlation plumbing): links every trades row this
        # run produces back to the PM proposal / RM review that led to it.
        # None on any run that never reached a successful PM call (e.g. an
        # early-exit before DecisionStage) — trades rows from such a run
        # simply carry no decision_id, which is correct, not a bug.
        decision_id = ctx.decision_id
        positions = ctx.positions
        total_value = ctx.total_value
        cash = ctx.cash
        portfolio_decision = ctx.portfolio_decision

        orders: list[dict] = []
        sell_decisions = [d for d in portfolio_decision.decisions if d.action == "SELL"]
        # Stage 3 (shorts): SHORT is the entry-side twin of BUY — both open
        # or add to a position and both owe a mandatory protective stop, so
        # they share the entry submission loop below (branching internally
        # on `decision.action` for side / geometry / sizing). COVER is the
        # exit-side twin of SELL and gets its OWN loop further down that
        # reuses `_submit_protected_sell` with side="buy", exactly the
        # plumbing PR #135 built for emergency covers.
        buy_decisions = [d for d in portfolio_decision.decisions if d.action in ("BUY", "SHORT")]
        cover_decisions = [d for d in portfolio_decision.decisions if d.action == "COVER"]
        hold_decisions = [d for d in portfolio_decision.decisions if d.action == "HOLD"]

        # Board item 178 — `ctx.positions`/`.cash`/`.total_value` are the
        # run-OPEN broker snapshot, taken before Research/Decision/Risk ran;
        # by the time this stage submits an ordinary SELL/COVER that
        # snapshot is ~5-10 minutes stale. A refresh already ran for the
        # RANKED-MARGIN rotation close (`_rotation_sell_gate`, which
        # re-reads for its own case below) and for BUYs (post-loop, further
        # down) — this was the one exit path still sizing qty and limit
        # price off the stale open-of-run read. Re-read ONCE, here, before
        # either loop starts, so ordinary SELL/COVER qty and price come from
        # a current book; a no-op when nothing moved between the two reads.
        if sell_decisions or cover_decisions:
            account, positions, price_map = pipeline._refresh_account_state()
            cash = account["cash"]
            total_value = account["portfolio_value"]
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            logger.info(
                "Pre-sell refresh: $%.2f total, $%.2f cash, %d positions",
                total_value,
                cash,
                len(positions),
            )

        # Board item 39 — the RANKED-MARGIN rotation's close goes LAST
        # among this session's exits, so that when its paired BUY is
        # checked (in `_rotation_sell_gate`, immediately before the close
        # is submitted) every other exit has a terminal status and the
        # account can simply be re-read rather than guessed at. A no-op on
        # every session without a ranked-margin rotation, which is every
        # session while `execution.rotation_ranked_margin_enabled` is off.
        sell_decisions = _rotation_sell_last(sell_decisions, ctx)

        for d in hold_decisions:
            try:
                pipeline.db.insert_trade(
                    symbol=d.symbol,
                    action="HOLD",
                    qty=0.0,
                    price=0.0,
                    reasoning=d.reasoning,
                    run_id=run_id,
                    decision_id=decision_id,
                )
            except Exception as e:
                logger.warning("Failed to record HOLD decision for %s: %s", d.symbol, e)

        # Terminal status per SELL order id, filled in by the per-name wait
        # below. Phase 14b reads it to decide whether the rotation's freed
        # room is REAL before the replacement BUY is allowed.
        sell_status_by_id: dict[str, str | None] = {}
        for decision in sell_decisions:
            prot = None
            try:
                # Board item 39. For a ranked-margin rotation close this
                # re-reads the account (measuring what the exits above
                # actually did), projects THIS sale, and runs the
                # replacement BUY through every refusal gate that is
                # knowable before the sale. `cleared` False means the buy
                # would be refused, so the close is not submitted and the
                # desk keeps the position instead of going naked. `None`
                # for every other SELL in the desk's history.
                rotation_gate = _rotation_sell_gate(
                    pipeline,
                    ctx,
                    decision,
                    buy_decisions,
                    positions,
                    total_value,
                    cash,
                    cover_decisions,
                )
                if rotation_gate is not None:
                    cleared, positions, total_value, cash = rotation_gate
                    # Adopt the refreshed book so this close is sized and
                    # priced off the same state the gate cleared against.
                    # `deployable_cash` is DERIVED from cash, so it is
                    # computed BEFORE any of the four is assigned: a raise
                    # part-way through would otherwise be swallowed by this
                    # loop's own handler and leave three fresh fields
                    # beside a stale derivation. All four, or none.
                    refreshed = (
                        positions,
                        cash,
                        total_value,
                        pipeline._compute_deployable_cash(cash, positions),
                    )
                    (ctx.positions, ctx.cash, ctx.total_value, ctx.deployable_cash) = refreshed
                    if not cleared:
                        continue
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty <= 0:
                    continue
                # Board item 39 — the unconditional barrier. A RANKED-MARGIN
                # rotation close may not reach the broker unless
                # `rotation_sell_reason` can build its reason from a real
                # `RotationClearance`, which only the projected post-sale
                # gate above mints. This reads no config: the feature flag
                # decides whether such a close is ever PROPOSED, and cannot
                # decide whether it is permitted to execute. Attempt 1 on
                # this item replaced exactly this kind of structural barrier
                # with a config boolean; it is not a config boolean again.
                rotation_final_reason = _rotation_ranked_margin_sell_reason(
                    pipeline,
                    ctx,
                    decision,
                )
                if rotation_final_reason is _ROTATION_SELL_REFUSED:
                    continue
                resolved = sell_qty_and_label(pipeline, decision, existing, ctx)
                if resolved is SKIP:
                    continue
                qty, action_label = resolved
                sell_price = existing[0].current_price
                # A decided exit is a plain DAY MARKET order (no limit); the
                # live quote is read only to MEASURE what the fill cost.
                # See src/exit_quote.py.
                sell_limit = None
                exit_quote = read_exit_quote(pipeline.broker, decision.symbol)
                position_qty = existing[0].qty
                # Single protected-sell discipline (cancel-WAL → submit →
                # accept → restore-on-failure) lives in one helper so this path
                # can't skip a step; defer reprotect/restore to the post-sell
                # wait below, which resolves the actual fill_qty.
                sale = pipeline._submit_protected_sell(
                    symbol=decision.symbol,
                    qty=qty,
                    limit_price=sell_limit,
                    reference_price=sell_price,
                    position_qty_before_sell=position_qty,
                    label=action_label,
                )
                if sale is None:
                    continue
                order, prot = sale
                orders.append(order)
                pipeline.db.insert_trade(
                    symbol=decision.symbol,
                    action=action_label,
                    qty=qty,
                    price=sell_price,
                    reasoning=decision.reasoning,
                    run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                    decision_id=decision_id,
                )
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    decision.symbol,
                    "order",
                    "submitted",
                    "broker_accepted",
                    broker_order_id=order.get("id"),
                    qty=qty,
                    limit_price=sell_limit,
                    side="sell",
                    quote_bid=exit_quote["bid"],
                    quote_ask=exit_quote["ask"],
                )
                record_rotation_close(
                    pipeline,
                    ctx,
                    SellLeg(decision, qty, sell_price, rotation_final_reason),
                    order,
                )
                logger.info(
                    "Executed: %s %s %s @ %s",
                    action_label.lower(),
                    pipeline._format_qty(qty),
                    decision.symbol,
                    "market",
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)
            if prot is None:
                continue
            await_sell_and_finalize(pipeline, prot, sell_status_by_id, ctx)

        # Stage 3 (shorts): COVER loop — the exit-side twin of the SELL loop
        # just above. Reuses `_submit_protected_sell`'s side="buy" plumbing
        # (PR #135 built this for emergency covers; this is the first
        # decision-path caller). No protective stop is placed afterward —
        # covering REDUCES risk, it doesn't open any.
        for decision in cover_decisions:
            prot = None
            try:
                existing = [p for p in positions if p.symbol == decision.symbol]
                if not existing or existing[0].qty >= 0:
                    continue  # nothing short held — COVER on a long/flat is refused
                held_qty = abs(existing[0].qty)
                resolved = cover_qty_and_label(pipeline, decision, held_qty, ctx)
                if resolved is SKIP:
                    continue
                qty, action_label = resolved
                cover_price = existing[0].current_price
                # The SELL loop's twin: a plain DAY MARKET buy-to-cover, the
                # live quote read only to measure the fill.
                cover_limit = None
                exit_quote = read_exit_quote(pipeline.broker, decision.symbol)
                sale = pipeline._submit_protected_sell(
                    symbol=decision.symbol,
                    qty=qty,
                    limit_price=cover_limit,
                    reference_price=cover_price,
                    position_qty_before_sell=held_qty,
                    label=action_label,
                    side="buy",
                )
                if sale is None:
                    continue
                order, prot = sale
                orders.append(order)
                pipeline.db.insert_trade(
                    symbol=decision.symbol,
                    action=action_label,
                    qty=qty,
                    price=cover_price,
                    reasoning=decision.reasoning,
                    run_id=run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                    decision_id=decision_id,
                )
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    decision.symbol,
                    "order",
                    "submitted",
                    "broker_accepted",
                    broker_order_id=order.get("id"),
                    qty=qty,
                    limit_price=cover_limit,
                    side="buy",
                    quote_bid=exit_quote["bid"],
                    quote_ask=exit_quote["ask"],
                )
                logger.info(
                    "Executed: %s %s %s @ %s",
                    action_label.lower(),
                    pipeline._format_qty(qty),
                    decision.symbol,
                    "market",
                )
            except Exception as e:
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)
            if prot is None:
                continue
            await_cover_and_finalize(pipeline, prot)

        if sell_decisions or cover_decisions:
            account, positions, price_map = pipeline._refresh_account_state()
            cash = account["cash"]
            total_value = account["portfolio_value"]
            ctx.positions = positions
            ctx.cash = cash
            ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
            ctx.total_value = total_value
            logger.info(
                "Post-sell refresh: $%.2f total, $%.2f cash, %d positions",
                total_value,
                cash,
                len(positions),
            )
        else:
            price_map = {p.symbol: p.current_price for p in positions}

        # Refresh the account before BUYs when no SELL fired, so sizing and
        # the entry-staleness guard read a current snapshot rather than the
        # research-stage one from ~10 minutes ago.
        #
        # An account-level daily-loss re-check used to run here too, dropping
        # every remaining BUY when the day's loss crossed the limit. Removed
        # 2026-09-20 on the owner's instruction with the rest of that
        # mechanism (retired item 32, docs/INCIDENT_HISTORY.md).
        if buy_decisions:
            if not sell_decisions:
                # Take the FRESH price_map too (2026-07-16 audit): it was
                # discarded into `_`, leaving `price_map` at research-time
                # position prices from 5-10 minutes earlier. For an ADD to a
                # held name that stale price is what the 5% entry-staleness
                # guard compares the LLM's entry against, and what sizes the
                # order — so the guard could pass a genuinely stale entry (or
                # reject a good one) on exactly the fast-moving tape where it
                # matters. New symbols were unaffected (they miss the map and
                # fall through to a live quote).
                account, positions, fresh_prices = pipeline._refresh_account_state()
                cash = account["cash"]
                total_value = account["portfolio_value"]
                ctx.positions = positions
                ctx.cash = cash
                ctx.deployable_cash = pipeline._compute_deployable_cash(cash, positions)
                ctx.total_value = total_value
                price_map = {**price_map, **fresh_prices}

        # Phase 14b — the rotation's BUY leg may only proceed on room that
        # is REAL. The constructor granted the new candidate its risk on the
        # premise that the held name closes; if that close was refused
        # upstream (Risk Manager, hard rules, protected-sell skip) or was
        # accepted but did not fill, buying anyway would put the book over
        # the portfolio risk ceiling by the new name's risk — a side door
        # around the ceiling this feature must never open. Same
        # `_record_execution_skip` path every other deterministic BUY skip
        # uses, so the funnel and the evening review see it.
        buy_decisions = _drop_rotation_buy_if_room_not_freed(
            pipeline,
            ctx,
            buy_decisions,
            sell_status_by_id,
        )

        # A desk that cannot read price places no new entry: one reference
        # read proves the feed before anything is sized (owner, 2026-10-08).
        buy_decisions = preflight_price_feed(pipeline, ctx, buy_decisions)

        # Anti-churn, the BUY-side mirror of the SELL-side
        # `held_symbol_bought_today` guard: a name this desk closed earlier
        # TODAY for failing its own entry bar is not bought back in the
        # same session. No new number — same exchange-day window.
        buy_decisions = _drop_buys_sold_today_below_bar(
            pipeline,
            ctx,
            buy_decisions,
        )

        # The cheap deterministic entry-viability checks, lifted out verbatim
        # to `src/stage_entry_preflight.py` (2026-10-08). Names with no today
        # print wait there for the batched re-ask; see that module.
        fundable_notional: dict[str, float] = {}
        buy_decisions = entry_viability_preflight(
            pipeline,
            ctx,
            buy_decisions,
            total_value=total_value,
            price_map=price_map,
            fundable_notional=fundable_notional,
        )

        # Cash-sweep funding. `planned_notional` counts BUYs ONLY, at the
        # quantity the submit loop will actually reach — allocation capped by
        # the §11.1 risk budget, quantized by the same helper. A SHORT is
        # excluded outright: it sells borrowed shares and spends no cash (see
        # D11 in the sizing loop), so funding one liquidates the vehicle for
        # cash no order can spend. Both were over-funding, and over-funding
        # is not free: the bookend re-parks
        # the residue minutes later, which is two crossings of the spread
        # for no position (2026-08-27: sold $3,422.61, re-bought $1,007.60
        # 53 seconds later; 2026-08-31: sold $503.47, re-bought $806.40
        # five seconds later).
        #
        # PM/RM/the hard gate size BUYs against
        # `deployable_cash` (raw cash + convertible sweep value), so on any
        # session with meaningful BUYs this sale IS load-bearing — the raw
        # cash on hand is typically just the reserve. The pre-BUY funding
        # sale that used to cover the planned notional was removed in item
        # 190, so nothing converts the vehicle automatically now (the
        # 2026-08-19 loss of a fully-approved plan was a 51s fill outliving
        # a 15s wait, not settlement).
        #
        # Since margin went on (2026-09-02) the sale is no longer what makes
        # a BUY POSSIBLE — the entry budget below is ladder headroom, and a
        # BUY the sale failed to fund now draws a margin loan instead of
        # being skipped. It is still worth doing: borrowing at
        # `margin_interest_rate_pct` against T-bills the desk already owns is
        # a guaranteed negative carry, so the sweep converts first and the
        # loan is what is left over.
        # isinstance guard: stage tests stub `pipeline` with MagicMock.
        if buy_decisions:
            _pin_approved_entry_ceilings(pipeline, ctx, buy_decisions)
            _warm_trade_updates(pipeline, ctx)
            # The cash-sweep funding step (sell the T-bill vehicle before a BUY)
            # was retired with the sweeper (board item 190 step 3); raw cash is
            # the only funding source, so there is nothing to record as freed.
            for d in buy_decisions:
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    d.symbol,
                    "funding",
                    "not_required",
                    "cash_sweep_disabled",
                    raw_cash=cash,
                )
            _adopt_stream_stall(pipeline, ctx)
            # Encode AFTER funding so the fund step's own 180s/30s ceiling
            # cannot sit as leftover slack on a fast no-op. Remaining
            # programmed wait is auth only when the hub did not start.
            _encode_entry_submit_window(pipeline, ctx, will_fund=False)

        # Spec §11.2 — how much NEW exposure this session may still add, and
        # the pool every entry below draws from. Ladder-derived (see
        # `_entry_deployment_budget`); raw cash only when the ladder cannot
        # be read at all. `total_value` and `positions` are the post-sell,
        # post-funding figures adopted above, so the headroom is measured
        # against the book the entries will actually join.
        entry_budget, budget_is_gross, budget_note = _entry_deployment_budget(
            pipeline,
            ctx,
            positions,
            total_value,
            cash,
        )
        single_name_cap = _single_name_execution_cap(pipeline, total_value)
        if buy_decisions:
            logger.info(
                "Entry budget for %d entr%s: $%.2f — %s (single-order ceiling $%.2f)",
                len(buy_decisions),
                "y" if len(buy_decisions) == 1 else "ies",
                entry_budget,
                budget_note,
                single_name_cap,
            )
        pending_entry_stops: list[dict] = []
        # A QUEUE, not the decision list. `entry_budget` is drawn on
        # SUBMISSION and is never given back, so an entry that rests unfilled
        # holds its slice of the §11.2 pool for the whole burst and every
        # LATER candidate sizes against the smaller pool. A name whose
        # displayed quote reads through its own ceiling is therefore moved to
        # the BACK of this queue exactly once (board item 183, see the
        # deferral in the limit-price block): it is still submitted, it is
        # still sized the same way, it simply stops taking the pool ahead of
        # names whose quotes are clean. Nothing here refuses anything and no
        # threshold is involved — the test is the ceiling itself.
        submit_queue = list(buy_decisions)
        original_entry_count = len(submit_queue)
        deferred_far_through: set[str] = set()
        entry_run = EntryRun(
            pipeline,
            ctx,
            submit_queue,
            deferred_far_through,
            original_entry_count,
            budget_is_gross,
            total_value,
        )
        queue_index = 0
        while queue_index < len(submit_queue):
            decision = submit_queue[queue_index]
            queue_index += 1
            if decision.action not in ("BUY", "SHORT"):
                continue
            is_short = decision.action == "SHORT"
            add_prep = None
            buy_accepted = False
            submit_attempted = False
            try:
                from src.execution.scale_in import LongAddPrep

                add_prep = LongAddPrep.not_scale_in()
                # D6 (Stage 3): the borrow gate. Refuse to open a short
                # unless the broker reports it BOTH shortable AND easy to
                # borrow — an API error or an unreadable/unknown symbol
                # reports both False in `get_shortability` (fail closed), so
                # a lookup failure refuses the short rather than guessing it
                # open. This is paper trading against IEX data: a
                # hard-to-borrow name fills unrealistically in paper and its
                # borrow cost is not modeled anywhere in this system, so
                # restricting to easy-to-borrow keeps measured results
                # transferable to live capital.
                if is_short:
                    try:
                        borrow = pipeline.broker.get_shortability(decision.symbol)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "SHORT %s: shortability lookup raised: %s",
                            decision.symbol,
                            e,
                        )
                        borrow = {
                            "shortable": False,
                            "easy_to_borrow": False,
                            "reason": "asset_lookup_failed",
                        }
                    if not (isinstance(borrow, dict) and borrow.get("shortable") and borrow.get("easy_to_borrow")):
                        reason = borrow.get("reason", "not_shortable") if isinstance(borrow, dict) else "not_shortable"
                        logger.warning(
                            "SHORT %s skipped: borrow gate refused (%s)",
                            decision.symbol,
                            reason,
                        )
                        _record_execution_skip(
                            pipeline,
                            ctx,
                            decision.symbol,
                            "borrow_gate",
                            reason,
                        )
                        continue
                    # Short scale-in (adding to an existing short) is now a
                    # real path: it is routed through `prepare_short_add` at
                    # the same post-sizing / post-min-order-floor point the
                    # long add uses, below. It is NOT gated here — the
                    # min-order floor must run first so the buy-stop is never
                    # cancelled for an add that then gets dropped.

                live_price = _live_fill_price(pipeline, decision.symbol)
                if live_price is not None:
                    market_price = live_price
                    price_map[decision.symbol] = live_price
                else:
                    market_price = None
                # MUST NOT freeze a morning/last-bar close into a live fill.

                limit_price = None
                sizing_price = None
                if decision.entry_price > 0:
                    limit_price = decision.entry_price

                if market_price and market_price > 0:
                    if limit_price is not None:
                        deviation = abs(limit_price - market_price) / market_price
                        if deviation > 0.05:
                            # Previously fell back to market order here — that
                            # silently absorbed up to 10% slippage against the
                            # LLM's stated entry. Now we skip: if entry_price
                            # is stale by >5%, the stop_loss computed against
                            # that entry is also stale, and the whole R/R math
                            # is bogus. Better to wait for next session.
                            logger.warning(
                                "%s %s skipped: LLM entry_price $%.2f is %.1f%% "
                                "away from market $%.2f (threshold 5%%). Stop/R/R "
                                "computed against stale entry would be unsafe.",
                                decision.action,
                                decision.symbol,
                                decision.entry_price,
                                deviation * 100,
                                market_price,
                            )
                            _record_execution_skip(
                                pipeline,
                                ctx,
                                decision.symbol,
                                "stale_entry",
                                f"entry ${decision.entry_price:.2f} is "
                                f"{deviation * 100:.1f}% from market "
                                f"${market_price:.2f} (threshold 5%)",
                            )
                            continue
                        elif not is_short and limit_price < market_price:
                            logger.info(
                                "Adjusting limit price for %s: $%.2f → $%.2f (raised to market)",
                                decision.symbol,
                                limit_price,
                                market_price,
                            )
                            limit_price = market_price
                        elif is_short and limit_price > market_price:
                            # Mirror: a resting SHORT limit sitting ABOVE
                            # market is not marketable — you can't sell short
                            # above the market and expect an immediate fill —
                            # so pull it DOWN to market instead of UP.
                            logger.info(
                                "Adjusting limit price for SHORT %s: $%.2f → $%.2f (lowered to market)",
                                decision.symbol,
                                limit_price,
                                market_price,
                            )
                            limit_price = market_price
                    # `sizing_price` is deliberately NOT set from market_price
                    # here: market_price is the FILL reference (a quote mid is
                    # legitimate for the marketable limit) and the SHARE COUNT
                    # must not divide by a mid. It is anchored to a today
                    # print just below (docs/WORK.md item 120).
                else:
                    logger.error(
                        "%s %s skipped: no verifiable price reference "
                        "(broker + bars both unavailable). "
                        "LLM proposed entry $%.2f but cannot be validated.",
                        decision.action,
                        decision.symbol,
                        decision.entry_price,
                    )
                    classified = classified_no_price(pipeline, decision.symbol)
                    if classified:
                        _record_execution_skip(pipeline, ctx, decision.symbol, *classified)
                    else:
                        _record_execution_skip(
                            pipeline,
                            ctx,
                            decision.symbol,
                            "no_price",
                            "no verifiable price reference (broker + bars unavailable)",
                        )
                    continue

                # docs/WORK.md item 120: SIZING vs FILL. `market_price` above
                # is the fill reference and may be a quote mid (a legitimate
                # marketable-limit reference, owner 2026-09-12); it drives the
                # limit price. The SHARE COUNT, however, divides the dollar
                # allocation by the price, so it must be a real TODAY PRINT —
                # never a quote mid, never a prior-session trade. Size off the
                # print, bounded conservatively by the already-approved entry
                # (which passed the 5% freshness check above); refuse the name
                # when no print is available rather than size on a bad price.
                sizing_print, why, detail = sizing_price_or_refusal(
                    _today_sizing_price,
                    pipeline,
                    decision.symbol,
                    "order",
                )
                if sizing_print is None:
                    _record_execution_skip(pipeline, ctx, decision.symbol, why, detail)
                    continue
                if decision.entry_price and decision.entry_price > 0:
                    # Size off a today print, bounded by the approved entry.
                    # The HIGHER divisor is conservative on the ALLOCATION
                    # path for BOTH directions (fewer shares: less capital on
                    # a buy, a smaller short on a short). On the RISK-BUDGET
                    # path it is conservative for a BUY only: there
                    # `risk_per_share = entry - stop` and a higher entry
                    # WIDENS it, shrinking qty_by_risk; for a SHORT
                    # (`stop - entry`) a higher entry NARROWS it and can
                    # INFLATE qty_by_risk when the analyst entry sits above
                    # the today print — item 181, fixed just below via
                    # `risk_sizing_price` (this `sizing_price` stays the
                    # allocation-path divisor, unchanged).
                    # What this line does fix: the short no longer divides by
                    # the below-market `bid_limit` (the over-size bug of
                    # item 120). Never size off a below-market number.
                    sizing_price = max(sizing_print, float(decision.entry_price))
                else:
                    sizing_price = sizing_print
                # item 181: a SHORT's higher divisor NARROWS |price - stop|
                # and inflates qty_by_risk, so it falls back to the print.
                risk_sizing_price = sizing_print if is_short else sizing_price

                # Owner ruling 2026-10-09: entries are plain DAY MARKET orders
                # (src/stage_execution_parts/entry_order_type.py holds the one
                # switch that puts the marketable-limit path back for go-live
                # testing). The quote is read for the RISK DIVISOR — the ask a
                # BUY pays, the bid a SHORT pays — and recorded on the order.
                try:
                    quote = pipeline.broker.get_latest_quote(decision.symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "%s %s quote lookup failed: %s",
                        decision.action,
                        decision.symbol,
                        e,
                    )
                    quote = None
                ask = quote.get("ask_price") if isinstance(quote, dict) else None
                bid = quote.get("bid_price") if isinstance(quote, dict) else None
                if _submit_window_overrun(ctx):
                    ctx.desk_latency_stall = True
                    logger.warning(
                        "%s %s NOT SUBMITTED — latency blew the window (encoded post-Risk budget %.1fs).",
                        decision.action,
                        decision.symbol,
                        float(getattr(ctx, "entry_submit_budget_s", 0.0) or 0.0),
                    )
                    _record_execution_skip(
                        pipeline,
                        ctx,
                        decision.symbol,
                        "latency_window",
                        "latency blew the window",
                    )
                    continue
                priced = price_entry(
                    entry_run,
                    EntryLeg(decision, is_short, queue_index, market_price, ask, bid),
                    limit_price,
                    sizing_price,
                    risk_sizing_price,
                )
                if priced is SKIP:
                    continue
                limit_price, sizing_price, risk_sizing_price = priced

                stop_price = entry_stop_price(ctx, decision, is_short, sizing_price)

                sized = entry_qty(
                    entry_run,
                    decision,
                    is_short,
                    sizing_price,
                    risk_sizing_price,
                    stop_price,
                )
                if sized is SKIP:
                    continue
                qty, fractional, qty_by_alloc, qty_by_risk = sized

                estimated_cost = qty * sizing_price
                # The ceiling THIS order may reach: the batch pool, or the
                # single-name cap, whichever is lower. Two different jobs —
                # the pool stops the SESSION deploying past the ladder rung,
                # the cap stops ONE order draining the pool.
                order_ceiling = min(entry_budget, single_name_cap)
                # D11: a SHORT is never trimmed or refused here. It does not
                # spend settled cash (it sells borrowed shares), and the caps
                # (D9) plus the borrow gate (D6) are the sole control surface
                # for a short. It DOES consume gross exposure, so it draws
                # the ladder pool down after submission below — but it is
                # never sized by it, which keeps the short path exactly as it
                # shipped.
                if not is_short and estimated_cost > order_ceiling:
                    affordable_qty = _size_shares(
                        pipeline,
                        order_ceiling / sizing_price,
                        fractional=fractional,
                    )
                    # The skip reason stays `insufficient_cash` even though
                    # the binding number is no longer always cash: it is a
                    # persisted evidence code the funnel, the trader feed and
                    # the blocked-proposal digest already read, and renaming
                    # it would orphan every historical row. The detail line
                    # carries the truth.
                    if affordable_qty <= 0:
                        logger.warning(
                            "Skipping BUY %s: estimated cost $%.2f exceeds the $%.2f still deployable — %s",
                            decision.symbol,
                            estimated_cost,
                            order_ceiling,
                            budget_note,
                        )
                        _record_execution_skip(
                            pipeline,
                            ctx,
                            decision.symbol,
                            "insufficient_cash",
                            f"estimated cost ${estimated_cost:.2f} exceeds the "
                            f"${order_ceiling:.2f} still deployable "
                            f"({budget_note})",
                        )
                        continue
                    logger.warning(
                        "Resizing BUY %s from %s to %s share(s): only $%.2f is still deployable — %s",
                        decision.symbol,
                        _fmt_shares(qty),
                        _fmt_shares(affordable_qty),
                        order_ceiling,
                        budget_note,
                    )
                    qty = min(qty, affordable_qty)
                    estimated_cost = qty * sizing_price
                    # Owner rule 2026-08-27: the cash re-size is a shrink
                    # AFTER the constructor's floor check, so the floor is
                    # re-applied here to the whole long it leaves (held +
                    # this buy) at the order's own stop. Below it the desk
                    # does not trade.
                    from src.execution.scale_in import held_signed_qty as _held_signed_qty

                    floor_pct = _min_position_risk_pct(pipeline)
                    held_long = max(0.0, _held_signed_qty(positions, decision.symbol))
                    floor_check = min_risk_shortfall(
                        position_notional=(held_long + qty) * sizing_price,
                        entry=sizing_price,
                        stop=_optional_risk_number(stop_price),
                        equity=total_value,
                        floor_pct=floor_pct,
                    )
                    if floor_check.refused:
                        # A buy with no readable stop has unknown risk:
                        # refused, the same as the short side below.
                        detail = (
                            f"re-sized to the ${order_ceiling:.2f} still deployable, the position would "
                            f"risk {floor_check.risk_pct:.2f}% of equity, under the owner's "
                            f"{floor_pct:.2f}% minimum risk per position"
                            if floor_check.readable
                            else f"no readable stop ({stop_price}), so the position's risk is unknown "
                            f"and cannot be shown to meet the owner's {floor_pct:.2f}% minimum risk per position"
                        )
                        logger.warning("Skipping BUY %s: %s", decision.symbol, detail)
                        _record_execution_skip(pipeline, ctx, decision.symbol, "below_owner_min_risk", detail)
                        continue
                    # Fixed 2026-09-24: this used to refuse the re-sized
                    # order outright ("below_min_notional") whenever it fell
                    # under the flat `min_order_usd` floor — an arbitrary
                    # $500 with no broker minimum behind it
                    # (config/number_ledger.yaml), and Alpaca charges no
                    # stock commission. With fractional sizing on,
                    # `affordable_qty` above is already guaranteed nonzero
                    # (the `affordable_qty <= 0` branch already refused as
                    # `insufficient_cash`), so a $3 residue now simply buys
                    # 0.0281 shares rather than being refused for smallness.
                    _record_pipeline_event(
                        pipeline,
                        ctx,
                        decision.symbol,
                        "funding",
                        "resized",
                        "confirmed_cash_partially_funded_order",
                        approved_qty=qty_by_risk
                        if qty_by_risk is not None and qty_by_risk < qty_by_alloc
                        else qty_by_alloc,
                        resized_qty=qty,
                        deployment_budget=entry_budget,
                        order_ceiling=order_ceiling,
                    )

                # Long scale-in path B (owner 2026-09-15): if this BUY adds
                # to a name that already has a resting protective sell,
                # cancel that sell, confirm the cancel via trade_updates,
                # then submit. WAL is written first so a crash cannot leave
                # the position naked without a recovery row. Short adds are
                # blocked above: scale-in is the long path.
                if not is_short:
                    from src.execution.scale_in import prepare_long_add

                    add_prep = prepare_long_add(
                        broker=pipeline.broker,
                        db=pipeline.db,
                        symbol=decision.symbol,
                        positions=positions,
                        intended_stop=stop_price,
                    )
                else:
                    # Short scale-in path (owner-approved). The buy-stop is
                    # cancelled inside prepare_short_add, so the min-order
                    # FLOOR must run FIRST — a below-floor add must be dropped
                    # BEFORE any protection comes off. D11 keeps a short off
                    # the budget-resize floor above (it never spends cash), so
                    # this is where the floor is re-applied for a short add.
                    from src.execution.scale_in import (
                        held_signed_qty,
                        prepare_short_add,
                    )

                    if held_signed_qty(positions, decision.symbol) < 0:
                        # Owner rule 2026-08-27: below the minimum risk
                        # per position the desk does not trade. The add is
                        # judged on the WHOLE short it leaves (held + add)
                        # at the intended stop, which the scale-in moves the
                        # whole position to. Replaced the deleted flat $500
                        # `min_order_usd` notional floor.
                        floor_pct = _min_position_risk_pct(pipeline)
                        held_short = abs(held_signed_qty(positions, decision.symbol))
                        floor_check = min_risk_shortfall(
                            position_notional=(held_short + qty) * sizing_price,
                            entry=sizing_price,
                            stop=_optional_risk_number(stop_price),
                            equity=total_value,
                            floor_pct=floor_pct,
                        )
                        position_risk_pct = floor_check.risk_pct
                        if floor_check.refused:
                            detail = (
                                f"short add would leave a position risking {position_risk_pct:.2f}% of "
                                f"equity, under the owner's {floor_pct:.2f}% minimum risk per position"
                                if floor_check.readable
                                else f"no readable stop ({stop_price}), so the short's risk is unknown "
                                f"and cannot be shown to meet the owner's {floor_pct:.2f}% minimum risk per position"
                            )
                            logger.warning(
                                "Skipping SHORT add %s: %s — dropped before any protective buy-stop is cancelled",
                                decision.symbol,
                                detail,
                            )
                            _record_execution_skip(
                                pipeline,
                                ctx,
                                decision.symbol,
                                "below_owner_min_risk",
                                detail,
                            )
                            _record_pipeline_event(
                                pipeline,
                                ctx,
                                decision.symbol,
                                "funding",
                                "refused",
                                "short_add_below_owner_min_risk",
                                resized_notional=estimated_cost,
                                position_risk_pct=position_risk_pct,
                                min_risk_pct=floor_pct,
                            )
                            continue
                    add_prep = prepare_short_add(
                        broker=pipeline.broker,
                        db=pipeline.db,
                        symbol=decision.symbol,
                        positions=positions,
                        intended_stop=stop_price,
                    )
                if add_prep is not None:
                    if add_prep.skip_reason:
                        _record_execution_skip(
                            pipeline,
                            ctx,
                            decision.symbol,
                            add_prep.skip_reason,
                            add_prep.skip_detail,
                        )
                        _record_pipeline_event(
                            pipeline,
                            ctx,
                            decision.symbol,
                            "scale_in",
                            "skipped",
                            add_prep.skip_reason,
                            detail=add_prep.skip_detail,
                        )
                        continue
                    if add_prep.cancelled:
                        # Persisted event code kept as-is (the refusal-
                        # signature registry and historical rows read it); it
                        # covers a cancelled buy-stop on a short add too.
                        _record_pipeline_event(
                            pipeline,
                            ctx,
                            decision.symbol,
                            "scale_in",
                            "protective_sell_cancelled",
                            "cancel_confirmed_via_trade_updates",
                            wal_row_id=add_prep.wal_row_id,
                            intended_stop=add_prep.intended_stop,
                            held_qty_before=add_prep.held_qty_before,
                        )

                # Write-ahead intent: insert a pending row BEFORE calling
                # the broker. Closes the BUY-side phantom-fill window the
                # audit surfaced — pre-fix, submit_order could return
                # successfully and a SIGKILL before db.insert_trade left
                # the broker with an accepted order and the DB with no
                # row. _reconcile_fills queries by broker_order_id, so
                # there was no recovery path for the phantom. With the
                # pending row pre-inserted, even a crash mid-submit
                # leaves a fill_status='pending_submit' row the operator
                # (or a periodic cleanup) can reconcile against the
                # broker's order list.
                # A market entry's recorded price is the quote side it pays (the
                # risk divisor); the fill's own average lands via reconciliation.
                executed_price = limit_price if limit_price is not None else risk_sizing_price
                # Phase 3.1 — pin the analyst's stated horizon and setup type to
                # the trade row at entry. Everything downstream that asks "is
                # this position on schedule?" must measure against THIS number,
                # not against the system's own rolling average hold time, which
                # shrinks every time the system sells early and thereby makes
                # the next position look stalled. None when the analysis is
                # missing (resume lanes, sweep buys): the reviewer then gets no
                # pace figure at all, which is correct — it never gets a
                # fabricated one.
                entry_analysis = next(
                    (a for a in (ctx.analyses or []) if a.symbol == decision.symbol),
                    None,
                )
                # Item 82: `setup_type` was being classified a SECOND time
                # here, independently of `PortfolioConstructor._build_buy`/
                # `_build_short` (see `TradeDecision.setup_type` in
                # models.py, which exists specifically so execution does not
                # have to re-derive this fact). On a scale-in ADD to an
                # already-held name this second lookup re-read TODAY's
                # technical read and wrote it onto the new row —
                # `get_symbol_last_buy` returns the newest row, so this
                # silently RECLASSIFIED a position whose setup_type was
                # already pinned on its original entry, which is worse than
                # a mere disagreement: pace/progress (disabled for a
                # breakout) could flip back on, or off, on a held position
                # with no new entry decision behind the change. A genuinely
                # new entry has no prior pinned row and reads the single
                # value the constructor already classified, carried on the
                # decision.
                _is_scale_in = add_prep is not None and add_prep.is_scale_in
                # Item 82 scale-in carry-forward lives in src/entry_evidence.py.
                _existing_buy, _setup_type_unused, _ceiling_unused = _resolve_entry_pins(
                    pipeline.db,
                    decision,
                    is_short=is_short,
                    is_scale_in=_is_scale_in,
                )
                pending_row_id, entry_side = insert_pending_entry(
                    db=pipeline.db,
                    decision=decision,
                    add_prep=add_prep,
                    is_short=is_short,
                    qty=qty,
                    executed_price=executed_price,
                    run_id=run_id,
                    stop_price=stop_price,
                    decision_id=decision_id,
                    entry_analysis=entry_analysis,
                    decision_model=ctx.decision_model,
                )

                _record_scale_in_own_verdict(
                    pipeline.db,
                    logger,
                    run_id=run_id,
                    decision_id=decision_id,
                    decision=decision,
                    prior_row=_existing_buy,
                    is_scale_in=_is_scale_in,
                )

                try:
                    # Set BEFORE the call: if submit raises, the broker may
                    # still have accepted the BUY. Restoring the cancelled
                    # stop at the OLD size is the daily-breaker / partial-
                    # fill bug (under-cover a grown position, and a working
                    # BUY plus a restored SELL is the wash-trade block).
                    # Leave the scale-in WAL row; drain rearms at broker qty.
                    submit_attempted = True
                    order = pipeline.broker.submit_order(
                        symbol=decision.symbol,
                        qty=qty,
                        side=entry_side,
                        limit_price=limit_price,
                        # PASSED THROUGH AS-IS (docs/WORK.md item 88). This
                        # used to read `stop_price if stop_price > 0 else
                        # None`, which laundered a garbage stop into the
                        # broker's "no stop was requested" case — so a zero
                        # reaching here submitted an unprotected entry and
                        # the broker never got the chance to refuse it.
                        # Every decision on this loop is a BUY or a SHORT and
                        # therefore OWES a stop, so there is nothing legitimate
                        # to convert to None: `submit_order` judges the value
                        # and returns `rejected_bad_stop` when it is not a
                        # price (surfaced below as `unusable_stop`).
                        stop_loss_price=stop_price,
                        reference_price=market_price,
                        # WORDING ONLY (see `submit_order`'s docstring): the
                        # same measured ATR(14) the constructor sized this
                        # trade against, so a fat-finger refusal can name
                        # the stock's own daily range instead of a bare
                        # percentage. None on the resume/sweep lanes that
                        # carry no analysis — the message then omits the
                        # range rather than inventing one.
                        atr=getattr(entry_analysis, "atr_14", None),
                    )
                except Exception as e:
                    # Submit raised — broker may or may not have the
                    # order. Leave the row as 'pending_submit' so the
                    # next session's orphan sweep
                    # (_reconcile_orphan_pending_submits) can match it
                    # against broker activity by symbol + qty + time
                    # window. Audit 2026-05-27: a prior version called
                    # mark_trade_submit_failed here, but
                    # get_orphaned_pending_submits filters only
                    # fill_status='pending_submit' — flipping it to
                    # submit_failed silently HID the row from the
                    # recovery path it was supposed to be flagged for.
                    _record_pipeline_event(
                        pipeline,
                        ctx,
                        decision.symbol,
                        "order",
                        "submit_unknown",
                        "broker_submit_exception",
                        detail=str(e),
                        trade_row_id=pending_row_id,
                    )
                    raise

                if not pipeline._order_accepted(order, decision.symbol, entry_side):
                    # `order["status"]` distinguishes WHO actually stopped
                    # this — our own pre-flight guards return a status
                    # before the order ever reaches the broker
                    # (rejected_outlier=fat-finger guard, kill_switch_halted)
                    # and both come back with no `id`, same as a real
                    # broker-side rejection. Collapsing all three into one
                    # "broker rejected" skip reason (pre-2026-09-17) read as
                    # if the broker had refused a sane order every time,
                    # when it was usually QAMC's own desk safety check
                    # blocking a bad price before the broker ever saw it
                    # (operator-reported, 2026-09-17: a fat-finger-guard
                    # rejection alerted as "broker rejected short"). Mark
                    # the pending row failed so it doesn't poison
                    # calibration as a "submitted" trade we never tracked.
                    # Distinct from the submit-raised case above: here we
                    # KNOW the order did not go live, so there's no orphan
                    # to sweep.
                    from src.execution.scale_in import restore_after_failed_add

                    restore_after_failed_add(
                        pipeline.broker,
                        pipeline.db,
                        add_prep,
                        decision.symbol,
                    )
                    pipeline.db.mark_trade_submit_failed(pending_row_id)
                    order_status = str((order or {}).get("status") or "")
                    order_detail = (order or {}).get("detail")
                    if order_status == "rejected_outlier":
                        # The plain-word fact only (e.g. "stop $9.66 is 24%
                        # from price $7.79", from broker.py's
                        # _PLAIN_PRICE_LABELS) — WHO blocked it ("desk
                        # safety check, not the broker") is the Telegram
                        # formatter's job (src/trader_feed.py's
                        # `_SKIP_WHO_LABELS`), not repeated here.
                        skip_reason = "fat_finger_guard"
                        skip_detail = order_detail or "price is too far from the market price"
                    elif order_status == "rejected_bad_stop":
                        # Stop-side sanity (non-finite, non-positive, wrong
                        # side of entry) — desk-side, like the fat-finger
                        # guard, not the broker. A SEPARATE reason: folded
                        # into `fat_finger_guard` it would tell the owner a
                        # price was "too far" when the stop could never work.
                        skip_reason = "unusable_stop"
                        skip_detail = order_detail or "the stop price is not usable"
                    elif order_status == "rejected_bad_qty":  # order_gates.py
                        skip_reason = "bad_quantity"
                        skip_detail = order_detail or "the quantity is not usable"
                    elif order_status == "kill_switch_halted":
                        skip_reason = "kill_switch_halted"
                        skip_detail = order_detail or ("the trading kill switch is active")
                    else:
                        skip_reason = "broker_rejected"
                        # Board item 89 clarity defect — "a missing broker
                        # reason on a rejection". The broker's own words are
                        # kept when it gave any; when it did not, the message
                        # says so instead of leaving the owner to wonder.
                        skip_detail = (
                            f"broker rejected {decision.action.lower()} "
                            f"{_fmt_shares(qty)} @ "
                            f"{'limit $%.2f' % limit_price if limit_price else 'market'}"
                            + (
                                f" — broker said: {order_detail}"
                                if order_detail
                                else " — the broker gave no reason the desk recorded"
                            )
                        )
                        # The raw broker status token is not appended to the
                        # owner-facing detail any more (it read
                        # "(status=rejected)"); it stays in the log line and
                        # the pipeline event above.
                    _record_pipeline_event(
                        pipeline,
                        ctx,
                        decision.symbol,
                        "order",
                        "rejected",
                        skip_reason,
                        trade_row_id=pending_row_id,
                        qty=qty,
                    )
                    _record_execution_skip(
                        pipeline,
                        ctx,
                        decision.symbol,
                        skip_reason,
                        skip_detail,
                    )
                    continue

                # Submit accepted — finalize the pending row with the
                # broker's order_id and flip to 'submitted'.
                pipeline.db.confirm_trade_submitted(
                    pending_row_id,
                    broker_order_id=order.get("id"),
                )
                buy_accepted = True
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    decision.symbol,
                    "order",
                    "submitted",
                    "broker_accepted",
                    broker_order_id=order.get("id"),
                    qty=qty,
                    limit_price=executed_price,
                    quote_bid=bid,
                    quote_ask=ask,
                )
                if isinstance(order, dict):
                    order.setdefault("action", decision.action)  # audit F5
                orders.append(order)
                if budget_is_gross or not is_short:
                    # The pool is drawn down by what the order CONSUMES of
                    # it, and the two budgets are consumed by different
                    # things. A ladder budget is GROSS headroom: a short
                    # occupies gross exactly as a long does (`gross_exposure`
                    # sums the magnitude of both), so it must draw the pool
                    # or a batch of shorts would leave the longs behind them
                    # sized against headroom that is already spent. The cash
                    # fallback is a settled-cash pool, which a short does not
                    # touch at all — D11, unchanged.
                    entry_budget -= estimated_cost
                order_type = "limit" if limit_price is not None else "market"
                logger.info(
                    "Executed: %s %s %s @ %s $%.2f",
                    decision.action.lower(),
                    _fmt_shares(qty),
                    decision.symbol,
                    order_type,
                    executed_price,
                )
                # The entry still owes a protective stop: it is placed as a
                # separate GTC order AFTER the fill, because an OTO leg would
                # inherit the parent's DAY tif and be expired by the broker at
                # 16:00 ET the same day (2026-07-16 audit — positions were
                # naked every night). Deferred until all BUYs are submitted so
                # the fill waits don't serialize the submission burst.
                if isinstance(order, dict) and (
                    order.get("pending_stop_price") or (add_prep is not None and add_prep.cancelled)
                ):
                    # Scale-in: the add's own stop is not automatically the
                    # live one. Most-protective for a long is the HIGHEST
                    # trigger (already computed on the prep). Prefer that
                    # over pending_stop_price or a looser cancelled stop
                    # would be replaced by the add's wider number.
                    protect_stop = order.get("pending_stop_price") or 0
                    if add_prep is not None and add_prep.is_scale_in and add_prep.intended_stop > 0:
                        protect_stop = add_prep.intended_stop
                    pending_entry_stops.append(
                        {
                            "symbol": decision.symbol,
                            "side": entry_side,
                            "order_id": order.get("id"),
                            "stop_price": protect_stop,
                            "qty": qty,
                            # Carried for the bounded re-peg (off by default).
                            # `reference_price` is the verified reference the
                            # slippage ceiling was computed from at SUBMISSION —
                            # the re-peg re-uses it rather than re-deriving a
                            # ceiling from a fresh quote, because a ceiling that
                            # follows the market is not a ceiling.
                            "reference_price": market_price,
                            "limit_price": limit_price,
                            "trade_row_id": pending_row_id,
                            "cover_full_position": bool(add_prep is not None and add_prep.is_scale_in),
                            "held_qty_before": (add_prep.held_qty_before if add_prep else 0.0),
                            "wal_row_id": (add_prep.wal_row_id if add_prep else None),
                            "cancelled_specs": (add_prep.specs if add_prep else []),
                            "intended_stop": (add_prep.intended_stop if add_prep else 0.0),
                            # Board item 193: the monotonic instant the BROKER
                            # acknowledged the protective cancel. Carried to the
                            # rearm so the unprotected window is measured end to
                            # end inside one run, from broker acknowledgement to
                            # broker acknowledgement, not from row write times.
                            "cancel_confirmed_at": (add_prep.cancel_confirmed_at if add_prep else None),
                        }
                    )
            except Exception as e:
                if add_prep is not None and add_prep.cancelled and not buy_accepted:
                    if submit_attempted:
                        logger.critical(
                            "scale-in: BUY submit for %s failed after the "
                            "protective sell was cancelled — WAL row %s "
                            "stays so drain rearms at the broker's current "
                            "qty; restoring the old stop size would under-"
                            "cover a fill that may already have landed",
                            decision.symbol,
                            add_prep.wal_row_id,
                        )
                    else:
                        from src.execution.scale_in import restore_after_failed_add

                        restore_after_failed_add(
                            pipeline.broker,
                            pipeline.db,
                            add_prep,
                            decision.symbol,
                        )
                logger.error("Order failed for %s %s: %s", decision.action, decision.symbol, e)

        # Protect every filled entry (GTC stop-limit keyed to the ACTUAL fill).
        protect_pending_entry_stops(pipeline, ctx, run_id, pending_entry_stops)

        # Phase 14b — the rotation's outcome, both legs, recorded durably.
        # A sale that freed room for a BUY that then did not happen is the
        # exact churn the anti-rotation rules exist to prevent, so that
        # case is also paged (`_alert_rotation_buy_leg_missing`).
        _record_rotation_buy_leg_outcome(pipeline, ctx, orders)

        ctx.orders = orders
        return orders
