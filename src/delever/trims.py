"""src.delever.trims -- ceiling trim submission, the by-conviction variant and the deferred discharge.

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


class DeleverTrims:
    """Ceiling trim submission, the by-conviction variant and the deferred discharge; standalone, built from explicit
    collaborators."""

    def __init__(
        self,
        *,
        alert_owner_delever_incomplete=None,
        compute_deployable_cash=None,
        conviction_cut_order=None,
        enforce_gross_ceiling=None,
        finalize_pending_protections=None,
        format_qty=None,
        full_sell_qty=None,
        live_delever_price=None,
        record_delever_shortfall=None,
        resolve_gross_ceiling=None,
        submit_protected_sell=None,
        broker=None,
        config=None,
        db=None,
    ) -> None:
        self._alert_owner_delever_incomplete = alert_owner_delever_incomplete
        self._compute_deployable_cash = compute_deployable_cash
        self._conviction_cut_order = conviction_cut_order
        self._enforce_gross_ceiling = enforce_gross_ceiling
        self._finalize_pending_protections = finalize_pending_protections
        self._format_qty = format_qty
        self._full_sell_qty = full_sell_qty
        self._live_delever_price = live_delever_price
        self._record_delever_shortfall = record_delever_shortfall
        self._resolve_gross_ceiling = resolve_gross_ceiling
        self._submit_protected_sell = submit_protected_sell
        self.broker = broker
        self.config = config
        self.db = db

    def _submit_gross_ceiling_trims(
        self,
        ctx: RunContext,
        ceiling,
        outcome,
    ) -> list[dict]:
        """Submit, protect, refresh and record a resolved gross-ceiling trim.

        The shared body of both §11.2 de-lever paths — the preamble margin
        floor (`_enforce_gross_ceiling`) and the morning post-decision
        conviction pass (`_enforce_gross_ceiling_by_conviction`). ONE
        implementation of the live-price submit, per-symbol stop re-coverage,
        broker refresh, incomplete-de-lever alert and durable shortfall
        record, so the two paths can never diverge on how a trim is placed —
        only on which names `apply_gross_ceiling` chose and in what order.
        """
        if not outcome.trims:
            return []
        logger.warning(
            "GROSS-EXPOSURE DE-LEVER: the book owns $%.0f against a $%.0f ceiling (%.2fx equity). %s",
            outcome.held_gross,
            outcome.ceiling_usd,
            ceiling.ceiling_x,
            ceiling.reason,
        )
        # A resting entry order would deepen the breach the moment it fills.
        try:
            from src.execution.broker_parts.cancel_confirm import settle_legs

            entries = self.broker.cancel_open_entry_orders()
            # Wait for the broker to confirm them dead before anything sells.
            settle_legs(self.broker, list(getattr(entries, "legs", ()) or ()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("gross-exposure de-lever: entry-order cancel failed: %s", exc)

        positions_by_symbol = {p.symbol: p for p in ctx.positions}
        # The equity the ceiling was measured against, kept for the item-112
        # shortfall record (ctx.total_value is overwritten by the refresh).
        equity_before = ctx.total_value
        orders: list[dict] = []
        pending_protections: list[dict] = []
        for trim in outcome.trims:
            position = positions_by_symbol.get(trim.symbol)
            if position is None or not position.current_price:
                continue
            held_qty = abs(position.qty)
            qty = held_qty * (trim.allocation_pct / 100.0)
            if float(position.qty).is_integer():
                qty = float(int(qty))
                if qty <= 0:
                    qty = 1.0
            if qty >= held_qty:
                qty = self._full_sell_qty(held_qty)
            if qty is None or qty <= 0:
                continue
            # Price the trim off the LIVE quote at submit time, not a fixed %
            # of a possibly-stale mark. This is emergency risk reduction: the
            # book already exceeds its gross ceiling and the whole point of the
            # trim is to shed that exposure NOW, regardless of how far a name
            # has gapped. A fixed % of a stale price rests ABOVE the falling
            # market on a gap day — precisely the conditions that trigger the
            # ladder — leaving the book OVER its ceiling exactly when it must
            # come down (docs/WORK.md item 118).
            #
            # `_live_delever_price` reads the CURRENT bid/ask and returns a
            # MARKETABLE limit that crosses it — a SELL at the live bid, a
            # COVER (BUY) at the live ask — so the order fills at ANY gap size,
            # because the gap is already IN the quote. When no live quote is
            # available it returns a limit of None, and the order becomes a
            # MARKET order: the guaranteed fill, and the correct fallback when
            # there is no live price to cross. No new % constant is introduced
            # on either branch, and the must-fill requirement (fill NOW over a
            # few bps of price) is met without resting on a stale reference.
            # The reference passed to the broker is the live mid, so the
            # fat-finger guard passes a legitimately gapped fill instead of
            # rejecting it for deviating from yesterday's mark.
            #
            # `FORCE_DELEVER` is already an EITHER-SIDE exit action in the
            # ledger (`_EITHER_SIDE_EXIT_ACTIONS`, src/storage/db.py) — "a
            # deterministic de-lever fires against whatever position is
            # open" — so the same label correctly retires a short chain
            # without inventing a second action name.
            is_cover = trim.action == "COVER"
            side = "buy" if is_cover else "sell"
            limit_price, quote_ref = self._live_delever_price(trim.symbol, side)
            exec_ref = quote_ref if quote_ref is not None else position.current_price
            sale = self._submit_protected_sell(
                symbol=trim.symbol,
                qty=qty,
                limit_price=limit_price,
                reference_price=exec_ref,
                position_qty_before_sell=abs(position.qty),
                label="FORCE_DELEVER",
                side=side,
                escalate_to_market_on_reject=True,
            )
            if sale is None:
                continue
            order, protection = sale
            pending_protections.append(protection)
            orders.append(order)
            logger.info(
                "GROSS-EXPOSURE DE-LEVER %s %s qty=%s @ limit=%s (%s)",
                trim.action,
                trim.symbol,
                self._format_qty(qty),
                (f"${limit_price:.2f}" if limit_price is not None else "market"),
                ceiling.reason,
            )
            try:
                self.db.insert_trade(
                    symbol=trim.symbol,
                    action="FORCE_DELEVER",
                    qty=qty,
                    price=position.current_price,
                    reasoning=trim.reasoning[:500],
                    run_id=ctx.run_id,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "GROSS-EXPOSURE DE-LEVER: trade row for %s failed: %s — the order may still be live at the broker",
                    trim.symbol,
                    e,
                )
            # Rebuild THIS symbol's stop coverage on its actual fill before
            # the loop cancels the next symbol's stops (docs/WORK.md item
            # 111). Finalizing the batch once after the loop left every
            # earlier symbol with no protective stop while the later ones were
            # cancelled, submitted and waited on — and the ladder only fires
            # in a drawdown. The trims themselves (which names, how much, at
            # what limit) were all fixed by `apply_gross_ceiling` before the
            # loop began and are unchanged.
            protection["trim_action"] = trim.action
            protection["trim_qty"] = qty
            self._finalize_pending_protections(
                [protection],
                context="GROSS-EXPOSURE DE-LEVER",
            )
        # ASYNC-FILL RACE: any trim that did not reach a terminal state is
        # already in `_unsettled_exit_orders` (registered centrally by
        # `_finalize_pending_protections`, so every exit path is covered),
        # and the next gross re-measure nets its open quantity out.
        try:
            account = self.broker.get_account()
            ctx.positions = self.broker.get_positions()
            ctx.cash = account["cash"]
            ctx.deployable_cash = self._compute_deployable_cash(ctx.cash, ctx.positions)
            ctx.total_value = account["portfolio_value"]
            ctx.last_equity = account.get("last_equity", ctx.total_value)
            # Re-measure so the alert and the dashboard report the book that
            # now exists, not the one that triggered the de-lever.
            self._resolve_gross_ceiling(ctx)
            self._alert_owner_delever_incomplete(ctx)
            self._record_delever_shortfall(
                ctx,
                held_gross_before=outcome.held_gross,
                ceiling_usd_before=outcome.ceiling_usd,
                equity_before=equity_before,
                protections=pending_protections,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("GROSS-EXPOSURE DE-LEVER: broker refresh failed: %s", e)
        return orders

    def _enforce_gross_ceiling_by_conviction(self, ctx: RunContext) -> list[dict]:
        """Morning post-decision §11.2 de-lever, WEAKEST-conviction cut first.

        Runs after the risk stage and before execution, on the morning lane
        only. It ranks the held book with `_conviction_cut_order` — this
        session's fresh per-seat read as a yes/no, then the desk's own
        `rank_verdicts` candidate ordering — so a conviction-dead winner is
        sold before an intact-thesis loser.

        Absent registry (every PM-less lane) means nothing fresh was read, so
        it returns [] and the `finally` discharge enforces the ordinary
        ceiling with the unchanged biggest-loser ordering. It DELEGATES to
        `_enforce_gross_ceiling`, the single owner of held-book trimming.

        It does NOT touch `ctx.gross_ceiling_deferred`. Only the delegate
        clears that, and only after it has actually measured and acted on
        the full ordinary ceiling — an earlier version cleared it here
        unconditionally, including when the delegate had bailed, which
        marked the debt paid without anything being enforced.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None:
            return []
        if not getattr(ctx, "evidence_registry", None):
            return []
        decision = getattr(ctx, "portfolio_decision", None)
        planned_exits = [
            d
            for d in (getattr(decision, "decisions", None) or [])
            if getattr(d, "action", None) in ("SELL", "COVER") and float(getattr(d, "allocation_pct", 0.0) or 0.0) > 0
        ]
        return self._enforce_gross_ceiling(
            ctx,
            conviction_rank=self._conviction_cut_order(ctx),
            planned_exits=planned_exits,
        )

    def _discharge_deferred_gross_ceiling(self, ctx: RunContext) -> None:
        """Pay the `floor_only` debt on any lane the conviction pass missed.

        Item 112 defect 2: scoping the morning preamble to the margin floor
        left the ORDINARY §11.2 ceiling unenforced on every lane that never
        reaches the Portfolio Manager — the nine early returns, the resume
        lane, and an exception exit. That was a regression against the
        behaviour where the preamble always enforced the full ceiling.

        Called from the morning body's `finally`, which is the ONE place
        every exit path passes through, so a lane added later inherits the
        enforcement instead of silently losing it. A no-op when the
        conviction pass already discharged the debt, and it re-measures, so
        a book that came under its ceiling meanwhile sheds nothing. Never
        raises: it runs while another exception may be propagating.

        It does NOT clear the flag itself. The delegate clears it only when
        the full ordinary ceiling was actually measured and acted on, so a
        discharge that bails — or raises — leaves the debt visibly OWED
        rather than silently paid.
        """
        if not getattr(ctx, "gross_ceiling_deferred", False):
            return
        try:
            self._enforce_gross_ceiling(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Deferred gross-ceiling enforcement failed: %s — the book may "
                "remain over its §11.2 ceiling until the next session",
                exc,
            )
