"""The SELL loop's per-name pieces, lifted out of `ExecutionStage._run_session` (src/stage_execution.py).

Bodies are unchanged apart from dedenting and a one-line unpack of the
per-name facts at the top of `record_rotation_close`; where the loop body
said `continue`, the lifted body returns `SKIP` and the caller continues. The
sell itself is a plain DAY MARKET order (src/exit_quote.py; the old 0.995
limit pad is gone), and the rotation refusal gate
(`if rotation_final_reason is _ROTATION_SELL_REFUSED: continue`) stays in
`_run_session`, where tests/test_rotation_sequencing.py reads it. Live-money exit code: behaviour
is identical.
"""

from __future__ import annotations

from src.pipeline_stages import (
    _alert_rotation_executed,
    _record_pipeline_event,
    logger,
)
from src.stage_execution_parts.state import SKIP, SellLeg


def await_sell_and_finalize(pipeline, prot, sell_status_by_id, ctx=None) -> None:
    """Wait for this sell's order, then rebuild this name's stop coverage."""
    # Wait for THIS sell and rebuild THIS name's stop coverage on its
    # actual fill before the loop cancels the next name's stops —
    # the per-name discipline the de-lever loops got (docs/WORK.md
    # item 111). Submitting every SELL first and waiting/finalizing
    # the batch afterwards left every earlier name with no
    # protective stop while later names were cancelled, submitted
    # and waited on. Runs even when the ledger write above raised:
    # the stops are off and the order is live. Which names are sold,
    # how much and at what limit are unchanged.
    order_id = prot["order_id"]
    # ExecutionStage was the lone SELL path missing this guard
    # — every other SELL path (force_delever / midday_emergency /
    # midday_llm / intra_check / take_profit) wraps the wait in
    # try/except. An uncaught exception here (broker 5xx, DNS
    # blip mid-poll) would propagate past the finalize loop
    # below. The audit F1 write-ahead row already covers a hard
    # process kill; this try/except additionally keeps the
    # in-process finalize path alive so coverage is rebuilt now
    # rather than waiting for the next session's drain.
    try:
        status = pipeline.broker.wait_for_order_terminal(order_id)
    except Exception as e:
        logger.warning(
            "ExecutionStage: wait_for_order_terminal failed for %s: %s "
            "— treating as unknown status so finalize still runs",
            order_id,
            e,
        )
        status = None
    sell_status_by_id[order_id] = status
    if status != "filled":
        logger.warning(
            "Sell order %s did not fill before buy phase (status=%s); buys will use current cash only",
            order_id,
            status or "unknown",
        )
    # The wait above returned, so the broker's fill_info is final.
    # Reprotect on actual residual (filled) or restore originals
    # (no-fill terminal). wait=False: this order was just waited on.
    pipeline._finalize_pending_protections(
        [prot],
        context="ExecutionStage",
        wait=False,
    )
    if ctx is not None:
        alert_rotation_close_outcome(pipeline, ctx, prot, status)


def book_exit(
    pipeline, ctx, orders, decision, action_label, price, exit_quote, side, rotation_reason, order, qty
) -> None:
    """The record of one decided SELL or COVER the broker accepted.

    Written when the sale is sent: at once, or later by the deciding run's
    re-check of a sale put off on an unconfirmed stop cancel
    (src/protection/put_off_sales.py), so both carry the same decision id,
    pipeline event and rotation-close record. `qty` is what was sent."""
    orders.append(order)
    pipeline.db.insert_trade(
        symbol=decision.symbol,
        action=action_label,
        qty=qty,
        price=price,
        reasoning=decision.reasoning,
        run_id=ctx.run_id,
        broker_order_id=order.get("id"),
        fill_status="submitted",
        decision_id=ctx.decision_id,
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
        limit_price=None,
        side=side,
        quote_bid=exit_quote["bid"],
        quote_ask=exit_quote["ask"],
    )
    if side == "sell":
        record_rotation_close(pipeline, ctx, SellLeg(decision, qty, price, rotation_reason), order)
    logger.info("Executed: %s %s %s @ %s", action_label.lower(), pipeline._format_qty(qty), decision.symbol, "market")


def record_rotation_close(pipeline, ctx, leg, order) -> None:
    """Record and page the desk's own rotation close, when this SELL is one."""
    decision, qty, sell_limit, rotation_final_reason = (
        leg.decision,
        leg.qty,
        leg.sell_limit,
        leg.rotation_final_reason,
    )
    # Phase 14b — this SELL is the desk's own rotation close.
    # Record the submit durably NOW; the owner is paged at the
    # sale's terminal state (`alert_rotation_close_outcome`), so a
    # position sold automatically is never silent and an unfilled
    # one is never reported as closed.
    rotation = ctx.rotation
    if isinstance(rotation, dict) and decision.symbol.upper() == rotation.get("held_symbol"):
        rotation["sell_order_id"] = order.get("id")
        rotation["sell_qty"] = float(qty)
        rotation["sell_limit"] = float(sell_limit)
        if isinstance(rotation_final_reason, str):
            # A SECOND durable fact, not an edit of the first.
            # The proposal the Risk Manager reviewed and the
            # clearance the sale executed under are two
            # different things that happened at two different
            # times; overwriting one with the other leaves the
            # ledger disagreeing with the alert about what was
            # said when. Written once, never edited.
            rotation["cleared_reason"] = rotation_final_reason
            _record_pipeline_event(
                pipeline,
                ctx,
                decision.symbol,
                "rotation",
                "sell_cleared_reason",
                rotation_final_reason,
                broker_order_id=order.get("id"),
                new_symbol=rotation.get("new_symbol"),
            )
        _record_pipeline_event(
            pipeline,
            ctx,
            decision.symbol,
            "rotation",
            "sell_submitted",
            rotation.get("reason", ""),
            broker_order_id=order.get("id"),
            qty=qty,
            limit_price=sell_limit,
            new_symbol=rotation.get("new_symbol"),
        )
        # The owner alert is NOT sent here: broker acceptance sells
        # nothing. `await_sell_and_finalize` pages the owner once the
        # order is terminal, worded from what actually filled.


def alert_rotation_close_outcome(pipeline, ctx, prot, status) -> None:
    """Page the owner the TRUE outcome of the desk's own rotation close.

    Runs after the wait and the finalize, so the fill quantity and the
    restore result are final. Silent for any sell that is not this
    session's rotation close; sent at most once. Never raises.
    """
    try:
        rotation = getattr(ctx, "rotation", None)
        order_id = prot.get("order_id")
        if not isinstance(rotation, dict) or not order_id:
            return
        if rotation.get("sell_order_id") != order_id or rotation.get("sell_alert_sent"):
            return
        rotation["sell_alert_sent"] = True
        filled_qty = avg_price = None
        try:
            info = pipeline.broker.get_order_fill_info(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rotation alert: fill read failed for %s: %s", order_id, exc)
            info = None
        if isinstance(info, dict) and info.get("filled_qty") is not None:
            filled_qty = float(info["filled_qty"])
            avg_price = float(info.get("filled_avg_price") or 0.0) or None
        _alert_rotation_executed(
            rotation=rotation,
            qty=float(rotation.get("sell_qty") or 0.0),
            limit_price=float(rotation.get("sell_limit") or 0.0),
            order_id=order_id,
            terminal_status=status,
            filled_qty=filled_qty,
            avg_price=avg_price,
            stops_restored=prot.get("coverage_confirmed"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("rotation close outcome alert failed: %s", exc)


#: Refusal reason for a partial SELL. Owner ruling 2026-10-09: a held
#: position is kept whole or sold whole; the only partial cut of a holding is
#: the gross-ceiling de-lever, which has its own order path and never reaches
#: this loop.
PARTIAL_SELL_REFUSED = "partial_sell_refused_whole_exits_only"


def sell_qty_and_label(pipeline, decision, existing, ctx):
    """Resolve the SELL quantity and label; `SKIP` where the loop skipped."""
    if decision.allocation_pct == 0:
        logger.warning(
            "Skipping SELL %s with allocation_pct=0 (ambiguous — use 100 for full exit)",
            decision.symbol,
        )
        return SKIP
    if 0 < decision.allocation_pct < 100:
        sell_fraction = decision.allocation_pct / 100
        qty = existing[0].qty * sell_fraction
        if float(existing[0].qty).is_integer():
            qty = max(1.0, float(int(qty)))
        if qty <= 0:
            return SKIP
        if qty >= existing[0].qty:
            qty = pipeline._full_sell_qty(existing[0].qty)
            if qty is None:
                return SKIP
            action_label = "SELL"
        else:
            logger.error(
                "Refusing PARTIAL_SELL %s (%.0f%%): a held position is kept whole or sold whole",
                decision.symbol,
                decision.allocation_pct,
            )
            try:
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    decision.symbol,
                    "order",
                    "refused",
                    PARTIAL_SELL_REFUSED,
                    allocation_pct=decision.allocation_pct,
                )
            except Exception as exc:  # noqa: BLE001 — a record write never changes the refusal
                logger.error("partial-sell refusal record failed for %s: %s", decision.symbol, exc)
            return SKIP
    else:
        qty = pipeline._full_sell_qty(existing[0].qty)
        if qty is None:
            return SKIP
        action_label = "SELL"
    return qty, action_label
