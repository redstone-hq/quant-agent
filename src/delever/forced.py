"""src.delever.forced -- the forced de-lever against a margin deficit.

Bodies moved verbatim from src/pipeline_delever.py (`DeleverMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body (e.g. `_live_delever_price`) are the HOST's shim, handed in,
never a body this part owns, so no recursion guard is needed.
"""

import logging

from src.pipeline_context import RunContext

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class DeleverForced:
    """The forced de-lever against a margin deficit; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        alert_owner_force_delever_incomplete=None,
        compute_deployable_cash=None,
        finalize_pending_protections=None,
        format_qty=None,
        full_sell_qty=None,
        live_delever_price=None,
        submit_protected_sell=None,
        sweeper=None,
        broker=None,
        config=None,
        db=None,
    ) -> None:
        self._alert_owner_force_delever_incomplete = alert_owner_force_delever_incomplete
        self._compute_deployable_cash = compute_deployable_cash
        self._finalize_pending_protections = finalize_pending_protections
        self._format_qty = format_qty
        self._full_sell_qty = full_sell_qty
        self._live_delever_price = live_delever_price
        self._submit_protected_sell = submit_protected_sell
        self._sweeper = sweeper
        self.broker = broker
        self.config = config
        self.db = db

    def _force_delever(self, ctx: RunContext) -> list[dict]:
        """Safety net for `allow_margin=False` accounts: clear the measured
        cash deficit deterministically, without waiting on an LLM.

        When cash is meaningfully negative at session start we do NOT trust
        the LLM to pick which positions to cut — we force-sell worst-
        performing first (most negative unrealized P&L, largest size as
        tiebreaker), hedges last, until projected cash is ≥ 0. This runs
        BEFORE any decision / review stage, so the rest of the session
        operates on a clean, cash-only snapshot.

        The ordering is a biggest-loser-first P&L rule (owner-ratified), NOT
        a risk measure: the goal is to CLEAR THE MEASURED CASH DEFICIT, and
        cutting the worst performers first is simply how the desk chooses
        what to give up to do it. The only risk-shaped rule in the sort is
        keeping inverse-ETF hedges in the last tier (see the ordering block
        below), so raising the cash does not strip directional protection off
        the longs that remain.

        Rationale: the DE-LEVER MANDATE in the PM / midday prompts is
        advisory — if the LLM emits only HOLDs, margin sits. Users who opt
        in to `allow_margin=False` want structural enforcement, not an LLM
        nudge. Speed and safety > LLM judgment here.

        The sell is priced off the LIVE quote at submit time, not a fixed % of
        a possibly-stale mark: a marketable limit AT the live bid (crosses the
        spread, so it fills however far the name has gapped — the gap is in the
        quote), and a MARKET order when no live quote is available (the
        guaranteed fill). We prioritize fill over price when clearing an
        unintended margin position (docs/WORK.md item 118): a fixed % of a
        stale price rests ABOVE the falling market on a gap day and leaves the
        deficit uncleared. See `_live_delever_price`. Note the contrast with
        the deleted daily-loss liquidator (docs/WORK.md item 32): this path
        clears a MEASURED cash deficit of known size, not a whole book on a gap
        day, and it is reached only when `allow_margin` is false.

        Returns the submitted orders list (empty when no de-lever is needed).
        ctx.cash / positions / total_value are refreshed from broker after
        fills so downstream stages see truth.
        """
        # `config` may be missing in tests that bypass __init__ via
        # TradingPipeline.__new__. Treat that as "not configured for cash-only
        # policy" and skip — the full-init pipeline always has config.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None or bool(getattr(risk_cfg, "allow_margin", False)):
            return []
        from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD

        if ctx.cash >= -MARGIN_DEFICIT_FLOOR_USD:
            return []

        deficit = -ctx.cash
        logger.warning(
            "FORCE DE-LEVER: cash=$%.2f, deficit=$%.2f — auto-selling to restore cash ≥ 0 (allow_margin=False)",
            ctx.cash,
            deficit,
        )
        # A resting entry BUY would deepen the very deficit this sweep exists
        # to clear the moment it fills — cancel entries before selling.
        try:
            from src.execution.broker_parts.cancel_confirm import settle_legs

            entries = self.broker.cancel_open_entry_orders()
            # Wait for the broker to confirm them dead before anything sells.
            settle_legs(self.broker, list(getattr(entries, "legs", ()) or ()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("force de-lever: entry-order cancel failed: %s", exc)

        sellable = [p for p in ctx.positions if p.qty > 0]
        if not sellable:
            logger.error(
                "FORCE DE-LEVER: cash=$%.2f deficit=$%.2f but no long positions "
                "to sell — account stuck on margin until cash arrives externally",
                ctx.cash,
                deficit,
            )
            # Loud, not silent: no long to sell IS a residual miss (the sibling
            # of the gross-ceiling path's incompleteness alert) — the account
            # stays on margin, so page the owner rather than only logging.
            self._alert_owner_force_delever_incomplete(
                deficit=deficit,
                projected_proceeds=0.0,
                failed_symbols=[],
            )
            return []

        # Two-tier ordering: prefer longs over inverse-ETF hedges before
        # falling back to the loss-magnitude rule.
        #
        # Inverse ETFs (SH / SDS / PSQ / SQQQ) have `_effective_multiplier < 0`
        # — they HEDGE long exposure. A "biggest-loser-first" pass that
        # ignores direction can pick a hedge in any market where the long
        # book is profitable (the hedge tends to lose precisely when the
        # rest is winning). Selling the hedge first leaves the remaining
        # longs naked, AMPLIFYING directional exposure. This sweep's job is
        # to clear the measured cash deficit, and a hedge raises that cash
        # just as well as a long does — so the ONLY reason to order the two
        # is to avoid stripping directional protection off the book while
        # doing it. Cash-flow-wise both raise cash equally; hedges sort last
        # so the deficit is cleared without gratuitously un-hedging the book.
        #
        # Tier key (lower = sells earlier):
        #  -1 → cash-sweep vehicle (parked T-bills ARE cash — always the
        #       first thing to liquidate; selling anything else first would
        #       realize market risk to cover a deficit that parked cash
        #       can cover for free)
        #   0 → long (effective_mul > 0)
        #   1 → inverse-ETF hedge (effective_mul < 0)
        # Within each tier, classic biggest-loser-first ordering:
        #   - most negative unrealized_pnl
        #   - then larger market_value (clear deficit in fewer orders)
        #   - then symbol alphabetical (deterministic across runs)
        from src.risk.rules import _effective_multiplier

        sweeper = self._sweeper()
        sweep_symbol = sweeper.symbol if sweeper is not None else None

        def _tier(p):
            if sweep_symbol is not None and p.symbol == sweep_symbol:
                return -1
            return 0 if _effective_multiplier(p.symbol) > 0 else 1

        targets = sorted(
            sellable,
            key=lambda p: (_tier(p), p.unrealized_pnl, -p.market_value, p.symbol),
        )

        orders: list[dict] = []
        projected_proceeds = 0.0
        failed_symbols: list[str] = []
        for p in targets:
            if projected_proceeds >= deficit:
                break
            is_sweep = sweep_symbol is not None and p.symbol == sweep_symbol
            # Price the must-fill exit off the LIVE quote at submit time: a
            # marketable limit AT the live bid, or a MARKET order (limit=None)
            # when no live quote is available. See `_live_delever_price`. The
            # reference is the live mid so the broker's fat-finger guard passes
            # a legitimately gapped fill; it falls back to the mark only when
            # there is no quote, in which case the order is a MARKET order the
            # guard skips anyway.
            #
            # Priced BEFORE the quantity is chosen (board item 182): the
            # partial sweep sale below sizes itself off this limit, and it
            # cannot do that if the limit is only established afterwards.
            sell_limit, quote_ref = self._live_delever_price(p.symbol, "sell")
            exec_ref = quote_ref if quote_ref is not None else p.current_price
            # The price floor each share of a partial sweep sale is
            # guaranteed to raise. A SELL limit fills AT OR ABOVE its limit
            # or it does not fill, so the live limit IS that floor, and a
            # partial sale can be sized off it with no cushion at all.
            #
            # WITH NO LIVE QUOTE THERE IS NO FLOOR, so there is no partial
            # size to justify and the loop sells the whole position, exactly
            # as every non-sweep de-lever target already does. An earlier
            # draft of this change sized that branch off the mark less
            # the (since deleted) 3% stop-limit buffer and called it "an existing
            # number, not a new one". It was neither safe nor a no-op:
            # dividing by 0.97 is a 3.09% pad on the same possibly-stale
            # mark, i.e. LARGER than the flat 2% pad the change claimed to be
            # removing, and 3% is a stop-limit through-buffer picked
            # (`status: arbitrary`) for a different job. Borrowing a constant
            # at the wrong tightness for a new job is not sourcing it.
            sizing_price = sell_limit if (sell_limit is not None and sell_limit > 0) else None
            if is_sweep and sizing_price is not None:
                # audit round 2: only unpark what the deficit needs —
                # full-liquidating an $80k T-bill balance for a $200 deficit
                # forced a full re-park at the session bookend, a pointless
                # round-trip. Real positions keep whole-position sells
                # (partial de-levers of losers re-review next session).
                #
                # THE SHARE COUNT IS COMPUTED, NOT PADDED (board item 182,
                # 2026-09-30). This used to divide the remaining deficit by
                # the possibly-stale `current_price` and then multiply by a
                # flat 1.02 — a 2% guess at how far the fill would land under
                # the mark, chosen by nobody and read off nothing. The guess
                # is unnecessary on THIS branch, because the order this loop
                # is about to place already carries its own worst case: the
                # live SELL limit it will rest at. Dividing the remaining
                # deficit by that floor gives the smallest share count that
                # clears it, and `ceil` supplies the whole-share rounding.
                # This extends to the QUANTITY exactly what
                # `_live_delever_price` already states for the PRICE.
                #
                # WHAT THIS DOES NOT COVER: the limit bounds the PRICE of the
                # shares that fill, not HOW MANY fill. The limit is placed at
                # the live bid, whose displayed size is finite, and Alpaca
                # documents `partially_filled` as an order status, so a short
                # fill is a real state on this path. Nothing below reads a
                # filled quantity — `_submit_protected_sell` returns on
                # broker ACCEPTANCE — so a partial fill still leaves a
                # residual deficit this loop will not see. That gap predates
                # this change and is reported, not fixed, here.
                import math as _math

                remaining = deficit - projected_proceeds
                qty = min(
                    float(_math.ceil(remaining / sizing_price)),
                    p.qty,
                )
                if qty >= p.qty:
                    qty = self._full_sell_qty(p.qty)
            else:
                qty = self._full_sell_qty(p.qty)
            if qty is None or qty <= 0:
                continue
            limit_str = f"${sell_limit:.2f}" if sell_limit is not None else "market"
            # The sweep vehicle's exit is recorded as SWEEP_SELL, not
            # FORCE_DELEVER (audit round 2): action names are the sweep's
            # ledger-isolation mechanism — a FORCE_DELEVER row on SGOV leaks
            # into evening sell-grading and calibration as if it were a
            # trading decision.
            sale = self._submit_protected_sell(
                symbol=p.symbol,
                qty=qty,
                limit_price=sell_limit,
                reference_price=exec_ref,
                position_qty_before_sell=p.qty,
                label="SWEEP_SELL" if is_sweep else "FORCE_DELEVER",
                escalate_to_market_on_reject=True,
            )
            if sale is None:
                # Even the MARKET escalation could not place (stop-clear failed,
                # or the market order itself was rejected). This name is a real
                # residual miss — record it so the sweep is reported incomplete
                # below rather than silently skipped.
                failed_symbols.append(p.symbol)
                continue
            order, prot = sale
            try:
                # Count the proceeds BEFORE the ledger write: the SELL is
                # already live at the broker, so its cash is coming whether or
                # not we manage to record it. Booking it only after a
                # successful insert_trade meant a DB hiccup left
                # projected_proceeds short, and the loop force-sold the NEXT
                # position to cover a deficit the in-flight order had already
                # covered — liquidating real holdings over a bookkeeping
                # failure (2026-07-16 audit).
                # Conservative proceeds estimate for the break-early guard:
                # mark × 0.97, i.e. assume the marketable fill lands up to 3%
                # below the mark (its own arbitrary number since the 3%
                # stop-limit buffer it was copied from was deleted). Under-
                # counting proceeds is the safe error here — it never stops the
                # sweep one position too early and leaves a residual deficit.
                #
                # ON THE SHARES ACTUALLY SOLD (2026-09-30). `market_value`
                # is the position's FULL value, and every branch here sells
                # the whole position EXCEPT the sweep slice above — which
                # credited the whole park anyway. The sweep vehicle sorts
                # FIRST and the loop breaks at `projected_proceeds >=
                # deficit`, and the completeness alert at the bottom reads
                # the same figure, so an $80k park sold down by $500
                # credited ~$78k and a short sale produced neither a second
                # sale NOR an owner alert.
                #
                # The slice is credited at the limit it was SIZED off, which
                # is the same arithmetic floor and keeps the two consistent:
                # counting a correctly-sized slice at a lower figure than it
                # was sized to raise would make the loop believe it fell
                # short and sell the next position on top of it.
                if qty < p.qty:
                    projected_proceeds += float(qty) * float(sizing_price)
                else:
                    projected_proceeds += p.market_value * 0.97
                orders.append(order)
                logger.info(
                    "FORCE DE-LEVER SELL %s qty=%s @ limit=%s (unrealized_pnl=$%.2f, mkt_value=$%.2f)",
                    p.symbol,
                    self._format_qty(qty),
                    limit_str,
                    p.unrealized_pnl,
                    p.market_value,
                )
                self.db.insert_trade(
                    symbol=p.symbol,
                    action="SWEEP_SELL" if is_sweep else "FORCE_DELEVER",
                    qty=qty,
                    price=p.current_price,
                    reasoning=(
                        f"cash-only auto de-lever: session opened with "
                        f"cash=${ctx.cash:.2f} (deficit ${deficit:.2f}); "
                        f"biggest-loser-first sweep"
                    ),
                    run_id=ctx.run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:
                logger.error(
                    "FORCE DE-LEVER SELL %s failed: %s — the order may still be "
                    "live at the broker; its proceeds are already counted so the "
                    "sweep will not over-liquidate",
                    p.symbol,
                    e,
                )
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops (docs/WORK.md item
            # 111). Finalizing the whole batch after the loop left every
            # earlier symbol with no protective stop while later symbols were
            # being cancelled, submitted and waited on. Which positions are
            # sold, and how much, is unchanged: `projected_proceeds` above is
            # booked at submit time, never from the fill.
            self._finalize_pending_protections(
                [prot],
                context="FORCE DE-LEVER",
            )

        # Refresh ctx so downstream stages see post-sell truth.
        try:
            account = self.broker.get_account()
            ctx.positions = self.broker.get_positions()
            ctx.cash = account["cash"]
            ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
            ctx.total_value = account["portfolio_value"]
            ctx.last_equity = account.get("last_equity", ctx.total_value)
            logger.info(
                "FORCE DE-LEVER complete: %d orders, post-refresh cash=$%.2f, positions=%d",
                len(orders),
                ctx.cash,
                len(ctx.positions),
            )
        except Exception as e:
            logger.error("FORCE DE-LEVER: broker refresh failed: %s", e)

        # Report parity with the gross-ceiling path's
        # `_alert_owner_delever_incomplete`: if the sweep could not raise
        # enough to cover the deficit (projected proceeds fell short) or a name
        # could not be sold even at market, the account is still on margin and
        # the owner must hear it — never a silent skip.
        if failed_symbols or projected_proceeds < deficit:
            self._alert_owner_force_delever_incomplete(
                deficit=deficit,
                projected_proceeds=projected_proceeds,
                failed_symbols=failed_symbols,
            )

        return orders
