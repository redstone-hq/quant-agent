"""Entry order placement, re-pegging and constructor-drop recording.

Split out of ``src/pipeline_stages.py`` (pipeline split, step 12). Every
function below is the VERBATIM body from that file; nothing was renamed,
reordered or changed. ``src.pipeline_stages`` re-exports each name lazily,
so every existing import path and every ``src.pipeline_stages.<name>``
patch target keeps working.
"""

from __future__ import annotations

from src.recording_accessors import pinned_evidence
from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    CONSTRUCTOR_REFUSED_EVENT_REASON,
    MAX_ENTRY_SLIPPAGE_BPS,
    _book_risk_inputs,
    _record_pipeline_event,
    _target_increase_missing_falsifier,
    logger,
    time,
)

#: Sentinel written into `pending_repegs.new_order_id` BEFORE the replace
#: PATCH goes out, and overwritten with the real id when the broker answers.
#: A row still carrying it at session start means the process died inside the
#: replace window: the drain must ASK THE BROKER what the old order became
#: rather than assume either outcome. Mirrors `_WAL_SELL_SENTINEL`.
_WAL_REPEG_SENTINEL = "__WAL_REPEG_PENDING__"

#: Trades-row fill states that mean "an order on this symbol has been
#: handed to the broker and not yet reconciled" — the same two values
#: `Database.get_symbol_last_buy(include_in_flight=True)` treats as
#: in-flight. A rotation never touches a symbol carrying one.
_IN_FLIGHT_FILL_STATUSES = frozenset({"submitted", "pending_submit"})

from src.pipeline_entry_text import _REPEG_OUTCOME_TEXT  # noqa: F401 (re-export)
from src.sentinel.entry_guard import record_clean_pass, record_swallowed, record_swallowed_here
from src.sizing_refusal import record_price_read_swallowed
from src.entry_orders_observed import (  # noqa: F401  moved out, re-exported
    _alert_owner_entry_cancelled,
    _alert_unmeasurable_symbols,
    _fill_stream_enabled,
    _trade_updates_already_started,
)


def _entry_slippage_bps(pipeline) -> float:
    """Configured entry-limit bound in basis points, or the 40bp default.

    One helper, two sides: BUY uses this as a ceiling above the reference,
    SHORT as a floor below it. MagicMock configs (common in tests) must not
    read as a real bps value — same isinstance convention as `_repeg_settings`.
    """
    raw = getattr(
        getattr(pipeline.config, "execution", None),
        "max_entry_slippage_bps",
        None,
    )
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return MAX_ENTRY_SLIPPAGE_BPS


def _known_entry_submit_budget_s(pipeline) -> float:
    """Programmed waits still ahead of submit. Not a fitted clock.

    Auth is omitted when the kept socket already started during Risk — that
    budget began at hub open. The cash-sweep funding step and its
    timeouts were deleted; auth is the only programmed wait left.

    Auth is also omitted when the socket is switched off entirely
    (`execution.fill_stream_enabled`; on since 2026-09-18, so this branch
    is the configured-off case): there is no handshake ahead of submit, so
    counting one would leave a stale 30s of
    slack in a window that is supposed to be the sum of the waits actually
    programmed. This widens nothing and tightens no existing timeout — it
    stops claiming a wait that cannot happen.
    """
    budget = 0.0
    if _fill_stream_enabled(pipeline) and not _trade_updates_already_started(pipeline):
        contended = getattr(
            getattr(pipeline, "broker", None),
            "trade_updates_lease_contended",
            None,
        )
        lease_held_elsewhere = False
        if callable(contended):
            try:
                lease_held_elsewhere = contended() is True
            except Exception:  # noqa: BLE001
                record_swallowed_here(pipeline, "stream.lease_contended")
                lease_held_elsewhere = False
            else:
                record_clean_pass(pipeline, "stream.lease_contended")
        if not lease_held_elsewhere:
            from src.execution.broker import _ALPACA_STREAM_AUTH_DEADLINE_S

            budget += float(_ALPACA_STREAM_AUTH_DEADLINE_S)
    return budget


def _encode_entry_submit_window(pipeline, ctx) -> None:
    """Pin the submit deadline from known step durations, not an invented timer."""
    budget = _known_entry_submit_budget_s(pipeline)
    ctx.entry_submit_budget_s = budget
    started = time.monotonic()
    ctx.entry_submit_started_mono = started
    ctx.entry_submit_deadline_mono = started + budget if budget > 0 else None


def _submit_window_overrun(ctx) -> bool:
    """True when submitting now would fire a ticket after the encoded window."""
    deadline = getattr(ctx, "entry_submit_deadline_mono", None)
    if not isinstance(deadline, (int, float)):
        return False
    return time.monotonic() > float(deadline)


def _start_trade_updates_early(pipeline, ctx) -> None:
    """Start trade_updates without waiting — overlap handshake with Risk review."""
    start = getattr(getattr(pipeline, "broker", None), "start_trade_updates", None)
    if not callable(start):
        return
    try:
        warmup = start()
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "stream.start_early", exc)
        logger.warning("trade_updates start-during-Risk failed: %s", exc)
        ctx.desk_latency_stall = True
        return
    try:
        from src.execution.broker import TradeStreamWarmup
    except Exception:  # noqa: BLE001
        record_swallowed_here(pipeline, "stream.warmup_type_import")
        return
    if isinstance(warmup, TradeStreamWarmup) and (warmup.handshake_failed or warmup.retried):
        ctx.desk_latency_stall = True


def _stop_trade_updates(pipeline) -> None:
    stop = getattr(getattr(pipeline, "broker", None), "stop_trade_updates", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "stream.stop", exc)
        logger.warning("trade_updates stop failed: %s", exc)
    else:
        record_clean_pass(pipeline, "stream.stop")


def _pin_approved_entry_ceilings(pipeline, ctx, buy_decisions) -> None:
    """Freeze the already-approved slippage cap before a desk stall can move it.

    BUY ceiling / SHORT floor from a live quote (or the approved entry) at
    ExecutionStage start — post-Risk, pre-websocket. Recomputing the cap
    from a later last-trade would raise the ceiling, which is a chase.
    Repeg stays off. Never uses a daily bar close.
    """
    slippage_bps = _entry_slippage_bps(pipeline)
    pinned = dict(getattr(ctx, "approved_entry_ceiling", None) or {})
    for decision in buy_decisions:
        symbol = getattr(decision, "symbol", None)
        if not symbol:
            continue
        live = _today_order_price(pipeline, symbol)
        ref = live if isinstance(live, (int, float)) and live > 0 else None
        if ref is None:
            entry = getattr(decision, "entry_price", None)
            if isinstance(entry, (int, float)) and entry > 0:
                ref = float(entry)
        if ref is None:
            continue
        is_short = getattr(decision, "action", "") == "SHORT"
        if is_short:
            pinned[symbol] = ref * (1 - slippage_bps / 10_000.0)
        else:
            pinned[symbol] = ref * (1 + slippage_bps / 10_000.0)
    ctx.approved_entry_ceiling = pinned


def _adopt_stream_stall(pipeline, ctx) -> None:
    """If the fill wait REST-fell-back because auth never completed, name it."""
    warmup = getattr(getattr(pipeline, "broker", None), "_last_stream_warmup", None)
    try:
        from src.execution.broker import TradeStreamWarmup
    except Exception:  # noqa: BLE001
        record_swallowed_here(pipeline, "stream.warmup_type_import")
        return
    if isinstance(warmup, TradeStreamWarmup) and (warmup.handshake_failed or warmup.retried):
        ctx.desk_latency_stall = True


def _warm_trade_updates(pipeline, ctx) -> None:
    """Start the kept trade_updates socket if Risk did not. Does not wait for auth."""
    ensure = getattr(getattr(pipeline, "broker", None), "ensure_trade_updates", None)
    if not callable(ensure):
        return
    try:
        warmup = ensure()
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "stream.warm", exc)
        logger.warning("trade_updates warmup failed: %s", exc)
        ctx.desk_latency_stall = True
        return
    try:
        from src.execution.broker import TradeStreamWarmup
    except Exception:  # noqa: BLE001
        record_swallowed_here(pipeline, "stream.warmup_type_import")
        return
    if isinstance(warmup, TradeStreamWarmup) and (warmup.handshake_failed or warmup.retried):
        ctx.desk_latency_stall = True


def _today_order_price(pipeline, symbol) -> float | None:
    """A price from TODAY that an order may be placed against, or None.

    Never a daily bar close (owner 2026-09-16), and now never a price the
    provider stamped with an earlier date either. A live quote mid-session is
    a legitimate fill reference, so quotes are allowed — what is refused is
    yesterday's last print on a thin name, or any value whose timestamp
    cannot be read. Unknown freshness returns None, which the callers already
    treat as "no verifiable live price" and skip, rather than pricing an
    order off it.
    """
    broker = getattr(pipeline, "broker", None)
    stamped_getter = getattr(broker, "get_latest_price_stamped", None)
    stamped = None
    if callable(stamped_getter):
        try:
            from src.execution.broker import LivePrice

            candidate = stamped_getter(symbol)
            # isinstance, not truthiness — tests build the pipeline with a
            # MagicMock broker whose auto-attributes answer every call (see
            # tests/test_bounded_repeg.py). A
            # MagicMock must never read as "this price is from today", and
            # must not read as "no price" either, so it falls through to the
            # bare getter below unchanged.
            if isinstance(candidate, LivePrice):
                stamped = candidate
        except Exception:  # noqa: BLE001
            record_price_read_swallowed(pipeline, "price.stamped_read", symbol=symbol)
            return None
    if stamped is not None:
        if not (stamped.price > 0):
            return None
        if not stamped.is_today:
            logger.warning(
                "%s live price $%.2f is not stamped today (source %s) — not pricing an order against it",
                symbol,
                stamped.price,
                stamped.source,
            )
            return None
        return float(stamped.price)
    getter = getattr(broker, "get_latest_price", None)
    if not callable(getter):
        return None
    try:
        live = getter(symbol)
    except Exception:  # noqa: BLE001
        record_swallowed_here(pipeline, "price.bare_read", symbol=symbol)
        return None
    if isinstance(live, (int, float)) and live > 0:
        return float(live)
    return None


def _live_fill_price(pipeline, symbol) -> float | None:
    """Back-compat alias for `_today_order_price`."""
    return _today_order_price(pipeline, symbol)


def _repeg_settings(pipeline) -> tuple[float, float] | None:
    """(poll_seconds, slippage_bps), or None when re-peg is off.

    Returns None — feature disabled — for anything other than an explicit
    `repeg_enabled is True`. The isinstance guards are the same convention as
    `_entry_slippage_bps`: tests build the pipeline with a MagicMock config
    whose auto-attributes are truthy (see tests/test_bounded_repeg.py), and a
    MagicMock must never read
    as "yes, replace live orders".
    """
    execution_cfg = getattr(pipeline.config, "execution", None)
    if getattr(execution_cfg, "repeg_enabled", None) is not True:
        return None

    raw_poll = getattr(execution_cfg, "repeg_poll_seconds", None)
    poll = (
        float(raw_poll)
        if isinstance(raw_poll, (int, float)) and not isinstance(raw_poll, bool) and 0 < raw_poll <= 30
        else 5.0
    )
    return poll, _entry_slippage_bps(pipeline)


def _repeg_entry_order(pipeline, ctx, spec: dict) -> tuple[str, float]:
    """ONE decisive reprice of a working entry limit — not a ladder.

    Returns ``(order_id_to_protect, shares_filled_under_superseded_ids)`` and
    writes ``spec["attempted_prices"]`` / ``spec["repeg_outcome"]`` so the
    end-of-session cancel alert can say exactly what was tried.

    WHY ONE REPLACE, NOT A LADDER (rebuilt 2026-09-12, owner-approved).
    PR #311 walked the limit up in small steps, confirming each swap before
    the next. Two things from Alpaca's own community and docs retired that:
      * the practice real users converge on for a fast market is submit,
        wait a few seconds, then replace ONCE at a deliberately aggressive
        price that crosses the market — not a sequence of nudges that each
        arrive after the market has moved again;
      * every replace is another `pending_replace` window to get stuck in
        (a real user reported a position left unmanageable that way), so
        the number of replaces is exposure, and one is the minimum.

    WHAT "DECISIVE" MEANS HERE, WITH NO NEW NUMBER. The single reprice goes
    straight to the slippage CEILING — ``reference * (1 + max_entry_slippage
    _bps)`` — the price this entry was already approved to pay when it was
    gated at submission. A buy limit at the ceiling crosses any ask at or
    below it and executes at the ask, not at the limit, so it is the most
    aggressive legal price and costs nothing extra when the market is
    inside it. It is the ONE bound that was already there; no "cross by X
    cents" constant is invented on top of it. The ceiling is computed from
    the reference captured at submission, NOT a fresh quote, because a
    ceiling that follows the market is not a ceiling — and it is absolute:
    nothing here can price above it to force a fill.

    THE OPEN-MARKET DEFECT THIS FIXES. A replace against an order Alpaca has
    accepted but the exchange has not yet acknowledged is REJECTED
    ("unable to replace order, order isn't sent to exchange yet"). At the
    open — slowest acknowledgement, highest volatility, and when this desk
    trades most — the old first nudge at ~5s was the attempt most likely to
    be thrown away. So the reprice is now GATED on the order having left
    Alpaca's not-yet-at-exchange statuses (`accepted`, `pending_new`; see
    `AlpacaBroker._ORDER_PRE_EXCHANGE_STATES` for the sourced list), read
    from the same `trade_updates` websocket as the fill wait. If it never
    gets there inside the window, no replace is sent — that attempt would
    be rejected anyway — and the reason is recorded honestly.

    WHAT HAPPENS TO AN UNFILLED ORDER AFTERWARDS. It is NOT left working.
    `place_entry_protection` cancels any entry still working at the end of
    this session and pages the owner (see that method for the derivation:
    the cancel is bound to the desk's own session boundary, not a timeout).

    TIME. This adds at most ``2 * repeg_poll_seconds`` plus one replace
    round-trip before protection: one window to let the order work, and —
    only if the venue has not acknowledged it yet — one more to wait for
    that acknowledgement.

    THE FOOTGUN (unchanged). Alpaca does not edit an order in place. It
    cancels the old one and creates a NEW one with a NEW id:
      1. `trades.broker_order_id` must be repointed, write-ahead-logged
         (`pending_repegs`) so a crash mid-replace is recoverable.
      2. A partially filled order must NEVER be replaced — fill counters do
         not carry across a replacement. The fill is re-read immediately
         before the replace and any fill at all stops it.
      3. A replacement rejected because the order filled first is the good
         case; the original id stays authoritative.

    Never raises: a re-peg failing must leave the ordinary "protect whatever
    filled" path exactly as it was.
    """
    order_id = str(spec.get("order_id") or "")
    symbol = spec.get("symbol")
    spec.setdefault("attempted_prices", [])
    if not order_id:
        spec["repeg_outcome"] = "unpriced"
        return order_id, 0.0

    settings = _repeg_settings(pipeline)
    if settings is None:
        spec["repeg_outcome"] = "disabled"
        return order_id, 0.0
    poll_seconds, slippage_bps = settings

    reference = spec.get("reference_price")
    limit_price = spec.get("limit_price")
    requested_qty = spec.get("qty")
    trade_row_id = spec.get("trade_row_id")

    if not isinstance(reference, (int, float)) or reference <= 0:
        spec["repeg_outcome"] = "unpriced"
        return order_id, 0.0
    if not isinstance(limit_price, (int, float)) or limit_price <= 0:
        # A market order has no limit to walk.
        spec["repeg_outcome"] = "unpriced"
        return order_id, 0.0

    # SIDE (board item 197). The slippage bound is the worst price this entry
    # was approved to pay, so it sits ABOVE the reference for a buy and BELOW
    # it for a `sell_short`. Everything downstream — the room test, which side
    # of the quote is read, and the direction the limit walks — flips with it.
    # Written before `repeg_enabled` was ever turned on, so no short entry has
    # been through this path.
    is_short = str(spec.get("side", "buy")).lower() != "buy"
    if is_short:
        ceiling = reference * (1 - slippage_bps / 10_000.0)
    else:
        ceiling = reference * (1 + slippage_bps / 10_000.0)
    ceiling = round(ceiling, 2 if ceiling >= 1 else 4)
    spec["ceiling"] = ceiling
    no_room = (limit_price <= ceiling + 1e-9) if is_short else (limit_price >= ceiling - 1e-9)
    if no_room:
        # Expected for most entries: since PR #111 the submitted limit IS the
        # ceiling, so there is nothing to reprice toward. Room exists only
        # when the limit was set inside the ceiling — e.g. the quote was
        # unavailable at submission and the analyst's entry price was used.
        logger.debug(
            "re-peg %s: limit $%.4f is already at the %.0fbp %s $%.4f — nothing to chase",
            symbol,
            limit_price,
            slippage_bps,
            "floor" if is_short else "ceiling",
            ceiling,
        )
        spec["repeg_outcome"] = "no_room"
        return order_id, 0.0

    # 1. Let it work first. A marketable limit usually fills here and the
    #    cheapest reprice is the one never sent.
    try:
        status = pipeline.broker.wait_for_order_terminal(
            order_id,
            timeout_seconds=poll_seconds,
            poll_interval=min(1.0, poll_seconds),
        )
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "repeg.wait_terminal", exc, symbol=symbol)
        logger.warning("re-peg %s: wait failed (%s) — leaving the order as-is", symbol, exc)
        spec["repeg_outcome"] = "wait_failed"
        return order_id, 0.0
    status = str(status or "").lower()
    if status in pipeline.broker._TERMINAL_ORDER_STATES:
        spec["repeg_outcome"] = "terminal_before_reprice"
        return order_id, 0.0

    # 2. Has the EXCHANGE got it yet? A replace before that is rejected.
    #    Only pay this second window when the first one ended with the order
    #    still in a pre-exchange status.
    if status not in pipeline.broker._ORDER_REPLACEABLE_STATES and status != "partially_filled":
        try:
            status = pipeline.broker.wait_for_order_at_exchange(
                order_id,
                timeout_seconds=poll_seconds,
                poll_interval=min(1.0, poll_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            record_swallowed(pipeline, "repeg.wait_exchange", exc, symbol=symbol)
            logger.warning("re-peg %s: exchange-ack wait failed (%s) — leaving the order as-is", symbol, exc)
            spec["repeg_outcome"] = "wait_failed"
            return order_id, 0.0
        status = str(status or "").lower()
        if status in pipeline.broker._TERMINAL_ORDER_STATES:
            spec["repeg_outcome"] = "terminal_before_reprice"
            return order_id, 0.0
        if status not in pipeline.broker._ORDER_REPLACEABLE_STATES and status != "partially_filled":
            logger.info(
                "re-peg %s: order %s still %r after %.1fs — the exchange has "
                "not acknowledged it, so a replace would be rejected; NOT "
                "repricing. It is handed to end-of-session handling as-is.",
                symbol,
                order_id,
                status or "unknown",
                poll_seconds,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                symbol,
                "repeg",
                "not_at_exchange",
                "repeg_not_at_exchange",
                broker_order_id=order_id,
                status=status or "unknown",
                window_seconds=poll_seconds,
            )
            spec["repeg_outcome"] = "not_at_exchange"
            return order_id, 0.0

    # 3. The partial-fill guard, re-read immediately before the replace.
    try:
        info = pipeline.broker.get_order_fill_info(order_id) or {}
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "repeg.fill_read", exc, symbol=symbol)
        logger.warning("re-peg %s: fill read failed (%s) — leaving the order as-is", symbol, exc)
        spec["repeg_outcome"] = "wait_failed"
        return order_id, 0.0
    else:
        record_clean_pass(pipeline, "repeg.fill_read", symbol=symbol)
    if str(info.get("status") or "").lower() in pipeline.broker._TERMINAL_ORDER_STATES:
        spec["repeg_outcome"] = "terminal_before_reprice"
        return order_id, 0.0
    try:
        filled_so_far = float(info.get("filled_qty") or 0)
    except (TypeError, ValueError):
        filled_so_far = 0.0
    if filled_so_far > 0:
        # Partial fill. STOP. Replacing now would re-peg a quantity the
        # broker has already partly executed, and the only failure mode
        # worth being paranoid about on this path is buying twice.
        logger.info(
            "re-peg %s: %.4f share(s) already filled on %s — not "
            "replacing a partially filled order; the working remainder "
            "is handed to entry protection unchanged",
            symbol,
            filled_so_far,
            order_id,
        )
        _record_pipeline_event(
            pipeline,
            ctx,
            symbol,
            "repeg",
            "abandoned_partial_fill",
            "repeg_partial_fill",
            broker_order_id=order_id,
            fill_qty=filled_so_far,
        )
        spec["repeg_outcome"] = "partial_fill"
        return order_id, 0.0

    # 4. Where is the market? Only to decide whether a reprice is needed at
    #    all — the PRICE is the ceiling regardless, never the ask plus
    #    something.
    try:
        quote = pipeline.broker.get_latest_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "repeg.quote", exc, symbol=symbol)
        logger.warning("re-peg %s: quote failed (%s)", symbol, exc)
        spec["repeg_outcome"] = "quote_unavailable"
        return order_id, 0.0
    # A buy fills against the ask; a short sale fills against the bid.
    ask = quote.get("bid_price" if is_short else "ask_price") if isinstance(quote, dict) else None
    if not isinstance(ask, (int, float)) or ask <= 0:
        spec["repeg_outcome"] = "quote_unavailable"
        return order_id, 0.0
    marketable = (float(ask) >= limit_price - 1e-9) if is_short else (float(ask) <= limit_price + 1e-9)
    if marketable:
        # The market is at or inside our limit: the order is marketable as
        # it stands and a replace would only re-queue it. Leave it working.
        logger.info(
            "re-peg %s: ask $%.4f is at/below limit $%.4f — order is marketable as-is, no reprice",
            symbol,
            ask,
            limit_price,
        )
        _record_pipeline_event(
            pipeline,
            ctx,
            symbol,
            "repeg",
            "market_within_limit",
            "repeg_no_reprice_needed",
            broker_order_id=order_id,
            ask=float(ask),
            limit_price=limit_price,
            ceiling=ceiling,
        )
        spec["repeg_outcome"] = "market_within_limit"
        return order_id, 0.0

    # 5. THE ONE REPRICE. Straight to the ceiling — the maximum price this
    #    entry was already approved for. Crosses the market when the ask is
    #    inside the ceiling; when the ask has run past the ceiling this is
    #    still the best legal price and is sent once, not chased.
    target = ceiling
    target = round(target, 2 if target >= 1 else 4)
    assert target >= ceiling - 1e-9 if is_short else target <= ceiling + 1e-9
    crosses = (float(ask) >= target - 1e-9) if is_short else (float(ask) <= target + 1e-9)
    if not crosses:
        logger.info(
            "re-peg %s: ask $%.4f is ABOVE the ceiling $%.4f — the single "
            "reprice cannot cross the market; sending it at the ceiling "
            "anyway as the best legal price",
            symbol,
            ask,
            ceiling,
        )
    spec["attempted_prices"].append(target)
    new_id, carried_fill, outcome = _apply_repeg(
        pipeline,
        ctx,
        symbol=symbol,
        order_id=order_id,
        trade_row_id=trade_row_id,
        target=target,
        requested_qty=requested_qty,
        ceiling=ceiling,
        ask=float(ask),
        crosses_market=crosses,
    )
    spec["repeg_outcome"] = outcome
    return new_id, carried_fill


def _session_candidate_ranking(pipeline) -> list[str] | None:
    """This session's candidate symbols, BEST FIRST, or None if there is no
    ranking — retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14).

    Reads `PortfolioManagerAgent.last_candidate_ranking`, the exact
    `rank_verdicts` output the PM's own prompt was rendered from. Same
    pattern, and same reason, as `last_rotation_precheck` above: the desk
    must ration the budget against the numbers the model was actually shown,
    not against a second evaluation.

    Returns None — never `[]` — when there is no ranking, because an EMPTY
    ranking and an ABSENT one mean the same thing to the allocator (fall back
    to the pre-decision ordering) and conflating them with a real, empty list
    would be indistinguishable from "every candidate ranked last".
    """
    ranked = getattr(
        getattr(pipeline, "portfolio_manager", None),
        "last_candidate_ranking",
        None,
    )
    if not ranked:
        return None
    symbols: list[str] = []
    for candidate in ranked:
        symbol = str(getattr(candidate, "symbol", "") or "").strip().upper()
        if symbol:
            symbols.append(symbol)
    return symbols or None


def _dropped_since_proposal(portfolio_decision) -> list[str]:
    """Symbols the PM proposed that are no longer in the order list.

    Targets minus decisions, and deliberately nothing cleverer: whatever
    removed the symbol, the fact the Risk Manager needs is the same one —
    "the narrative below argues for a name that is not in the list above,
    and that is expected."

    Earlier refusals that removed a target itself (never-blank soft-exit
    before the constructor) are kept: union with the existing drop list
    so a name refused before tickets were built is not silently deleted
    from what Risk is told.

    WHY THIS IS A FUNCTION AND NOT A LINE
    --------------------------------------
    It used to be computed once, immediately after `construct_orders`, and
    then left alone. Between that point and the Risk Manager's review the
    decision list is filtered at least three more times — the symbol guard,
    the queued-earnings clamp, and the hard-risk gate — and each of those
    can remove SOME names while letting the rest through. A symbol removed
    by one of them was gone from the order list and absent from the frozen
    drop list, so the Risk Manager saw a plan arguing for a name it could
    not find and nothing telling it why. That is exactly the failure the
    drop list was written to prevent (docs/INCIDENT_HISTORY.md,
    2026-08-31), reappearing on a path the original fix did not cover.

    So it is recomputed right before the review instead. HOLD still counts
    as kept: the symbol survived, it just is not being traded today.
    """
    kept = {d.symbol.upper() for d in portfolio_decision.decisions}
    dropped = [t.symbol.upper() for t in portfolio_decision.targets if t.symbol.upper() not in kept]
    seen = set(dropped)
    for symbol in getattr(portfolio_decision, "constructor_dropped", None) or []:
        upper = str(symbol).upper()
        if upper and upper not in kept and upper not in seen:
            dropped.append(upper)
            seen.add(upper)
    return dropped


def _record_constructor_drops(pipeline, ctx, portfolio_decision) -> dict[str, dict]:
    """Persist WHY each PM target the constructor dropped was dropped — and
    file the two classes of "no order" under different names.

    Funnel-queue item 2 (2026-09-03): a target the constructor drops before
    ever building a `proposed_order` row previously left NOTHING in the
    database — no verdict (RM never saw it), no execution_skip (execution
    never saw it either), just an aggregate log line.
    `blocked_proposals_census.py` counted every one as `no_order_built`,
    its largest unexplained bucket. `last_drop_reasons` (see
    `PortfolioConstructor.construct_orders`) recovers the constructor's
    OWN log line from the same call that just ran, so every dropped symbol
    gets a terminal, real-reason evidence row instead of silence.

    2026-09-12: the constructor now also reports DATA FAULTS separately
    (`PortfolioConstructor.drain_data_faults`). A symbol it could not
    MEASURE — no price, no ATR, no usable bars, no analysis — is filed as
    (stage='deterministic_gate', outcome='unmeasurable',
    reason='data_fault'), never as `constructor_dropped`, because it is not
    a trade the desk judged and must not be counted as one. Faults on
    symbols the PM never targeted (found by the eligibility preview, which
    runs over every analysed name) are recorded the same way with
    `targeted=False`, so a symbol that silently became unanalysable before
    the PM ever saw it still leaves a durable row. Returns the faults so
    the caller can page the owner.

    Best-effort like every evidence write here: never raises.
    """
    faults: dict[str, dict] = {}
    try:
        constructor = pipeline.portfolio_constructor
        drain = getattr(constructor, "drain_data_faults", None)
        faults = dict(drain() if callable(drain) else {})
        dropped = [str(s).upper() for s in (portfolio_decision.constructor_dropped or [])]
        drop_reasons = getattr(constructor, "last_drop_reasons", {})
        # 2026-09-12 (docs/WORK.md item 54): a refusal the constructor
        # recorded AS DATA (`PortfolioConstructor.last_refusals` — today a
        # stop wider than the instrument's reach, or too little history) is
        # filed under its own reason with the code beside it, never through
        # the log-text regex, whose pattern several messages miss. A data
        # fault is checked FIRST: it is not a judgement at all.
        drain_refusals = getattr(constructor, "drain_refusals", None)
        refusals = dict(drain_refusals() if callable(drain_refusals) else {})
        existing_risk_pct, _ = _book_risk_inputs(
            ctx,
            getattr(ctx, "total_value", 0.0) or 0.0,
        )
        for sym in dropped:
            fault = faults.get(sym)
            if fault:
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    sym,
                    "deterministic_gate",
                    "unmeasurable",
                    "data_fault",
                    fault=fault.get("fault", ""),
                    detail=fault.get("detail", ""),
                    targeted=True,
                )
                continue
            refusal = refusals.get(sym)
            if refusal:
                _record_pipeline_event(
                    pipeline,
                    ctx,
                    sym,
                    "deterministic_gate",
                    "blocked",
                    CONSTRUCTOR_REFUSED_EVENT_REASON,
                    refusal=refusal.get("refusal", ""),
                    detail=refusal.get("detail", ""),
                    targeted=True,
                )
                continue
            target = next(
                (t for t in list(getattr(portfolio_decision, "targets", None) or []) if str(t.symbol).upper() == sym),
                None,
            )
            if target is not None and _target_increase_missing_falsifier(
                target,
                positions=getattr(ctx, "positions", None),
                total_value=getattr(ctx, "total_value", 0.0) or 0.0,
                existing_risk_pct=existing_risk_pct,
            ):
                # Already recorded as SOFT_EXIT_MISSING_AFTER_RETRY
                # before the constructor ran. Do not re-file as a
                # generic drop. A checkable size-down with a blank
                # falsifier is NOT this skip — it was admitted so the
                # constructor can stamp a mechanical warrant.
                continue
            # Falls back to a generic label only if a future refactor adds
            # a new drop path the capture's log-message pattern doesn't
            # match — never nothing, even then.
            _record_pipeline_event(
                pipeline,
                ctx,
                sym,
                "deterministic_gate",
                "blocked",
                "constructor_dropped",
                detail=drop_reasons.get(sym, "no matching constructor log line captured"),
            )
        # Faults and refusals on names the PM never targeted come from the
        # eligibility preview over every analysed symbol; recorded so "why
        # was X never even proposed" has a durable, named answer.
        for sym, fault in faults.items():
            if sym in dropped:
                continue
            _record_pipeline_event(
                pipeline,
                ctx,
                sym,
                "deterministic_gate",
                "unmeasurable",
                "data_fault",
                fault=fault.get("fault", ""),
                detail=fault.get("detail", ""),
                targeted=False,
            )
        for sym, refusal in refusals.items():
            if sym in dropped or sym in faults:
                continue
            _record_pipeline_event(
                pipeline,
                ctx,
                sym,
                "deterministic_gate",
                "blocked",
                CONSTRUCTOR_REFUSED_EVENT_REASON,
                refusal=refusal.get("refusal", ""),
                detail=refusal.get("detail", ""),
                targeted=False,
            )
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "constructor.drop_recording", exc)
        logger.error("constructor drop/fault recording failed: %s", exc)
    else:
        record_clean_pass(pipeline, "constructor.drop_recording")
    return faults


def _record_constructor_side_flips(pipeline, ctx) -> None:
    """One durable per-symbol row for every target the constructor collapsed
    from a side flip to a close-only leg (`PortfolioConstructor`, rule D3).

    Board item 164 (2026-09-19). The seat asked to turn a long into a short
    (or back); the constructor refuses the flip and emits only the closing
    leg. The symbol still produces an order, so it never counts as a drop,
    and the only trace of the refused half was a log line. The record states
    the held weight, the weight asked for and the weight emitted (0: flat).
    Never raises.
    """
    try:
        flips = getattr(pipeline.portfolio_constructor, "last_side_flips", None)
        if not isinstance(flips, dict):
            return
        for sym, flip in flips.items():
            held = flip.get("held_weight_pct")
            asked = flip.get("requested_weight_pct")
            _record_pipeline_event(
                pipeline,
                ctx,
                sym,
                "deterministic_gate",
                "modified",
                "side_flip_refused",
                gate="side_flip_refused",
                held_weight_pct=held,
                requested_weight_pct=asked,
                emitted_weight_pct=flip.get("emitted_weight_pct"),
                detail=(
                    f"{sym}: target asked to flip the position from "
                    f"{held:+.2f}% to {asked:+.2f}% of the book in one "
                    f"session; a single order that crosses sides is "
                    f"unprotected between legs, so only the closing leg "
                    f"(to 0%) was built. The other side can open in a later "
                    f"session once the book is flat."
                ),
            )
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "constructor.side_flips", exc)
        logger.warning("constructor side-flip recording failed: %s", exc)
    else:
        record_clean_pass(pipeline, "constructor.side_flips")


def _apply_repeg(
    pipeline,
    ctx,
    *,
    symbol,
    order_id: str,
    trade_row_id,
    target: float,
    requested_qty,
    ceiling: float,
    ask: float | None = None,
    crosses_market: bool = True,
) -> tuple[str, float, str]:
    """The one write-ahead-logged replacement.

    Returns ``(order_id_now_authoritative, superseded_filled_qty, outcome)``
    where `outcome` is a `_REPEG_OUTCOME_TEXT` key.

    The WAL row is the whole point of this function. Between the PATCH
    landing at Alpaca and `repoint_trade_broker_order_id` committing, the
    broker holds a working order under an id this system has written down
    nowhere. A SIGKILL there used to be unrecoverable: the trades row points
    at an order that will report status 'replaced' forever (a status neither
    terminal set in `_reconcile_fills` covers), and the live order is
    untracked. With the row written first, `_drain_pending_repegs` at the next
    session start re-reads the old id, follows Alpaca's `replaced_by` link,
    and repoints the trades row.

    There is no "confirm the swap before the next replace" step any more:
    this is the only replace, and nothing is sent after it. Alpaca's
    one-replace-at-a-time rule (`await_replacement_confirmed`) therefore has
    nothing to protect here.
    """
    try:
        wal_row_id = pipeline.db.insert_pending_repeg(
            trade_row_id=trade_row_id,
            symbol=symbol,
            old_order_id=order_id,
            new_order_id=_WAL_REPEG_SENTINEL,
            run_id=pinned_evidence(ctx, "run_id"),
        )
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "repeg.wal_insert", exc, symbol=symbol)
        # No durable intent ⇒ no crash-safe window ⇒ do not open one.
        logger.error(
            "re-peg %s: could not write the WAL row (%s) — NOT replacing "
            "order %s. An unlogged replacement is an untrackable order.",
            symbol,
            exc,
            order_id,
        )
        return order_id, 0.0, "wal_refused"

    else:
        record_clean_pass(pipeline, "repeg.wal_insert", symbol=symbol)
    result = pipeline.broker.replace_entry_limit(
        order_id,
        target,
        qty=requested_qty if isinstance(requested_qty, (int, float)) else None,
    )
    new_id = (result or {}).get("id")

    if not new_id:
        # The broker did not hand us an id. Either it refused outright (the
        # order filled first — the good case) or the call failed in a way that
        # leaves the outcome genuinely unknown (timeout). Do not guess: ASK.
        resolved = pipeline.broker.resolve_replacement_chain(order_id)
        if resolved is None:
            # Broker unreadable. Leave the WAL row standing; the drain owns it
            # from here.
            logger.error(
                "re-peg %s: replacement of %s failed AND the order could not "
                "be re-read — leaving WAL row %s for the session-start drain",
                symbol,
                order_id,
                wal_row_id,
            )
            return order_id, 0.0, "replace_unknown"
        if resolved == order_id:
            # Nothing was minted; the original order is still the only one.
            _delete_repeg_wal(pipeline, wal_row_id)
            logger.info(
                "re-peg %s: broker refused the replacement of %s (%s) — the original order remains authoritative",
                symbol,
                order_id,
                (result or {}).get("status", "unknown"),
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                symbol,
                "repeg",
                "replace_rejected",
                "repeg_replace_rejected",
                broker_order_id=order_id,
                detail=str((result or {}).get("detail") or (result or {}).get("status") or ""),
            )
            return order_id, 0.0, "replace_rejected"
        # The PATCH actually landed even though the response was lost.
        logger.warning(
            "re-peg %s: replacement of %s reported failure but the broker "
            "shows it replaced by %s — adopting the real id",
            symbol,
            order_id,
            resolved,
        )
        new_id = resolved

    # Record the minted id, THEN repoint the trades row, THEN drop the WAL.
    try:
        pipeline.db.resolve_pending_repeg(wal_row_id, str(new_id))
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "repeg.wal_resolve", exc, symbol=symbol)
        logger.warning("re-peg %s: WAL resolve failed: %s", symbol, exc)
    else:
        record_clean_pass(pipeline, "repeg.wal_resolve", symbol=symbol)
    repointed = _repoint_trade(pipeline, trade_row_id, order_id, str(new_id), symbol)
    if repointed:
        _delete_repeg_wal(pipeline, wal_row_id)

    _record_pipeline_event(
        pipeline,
        ctx,
        symbol,
        "repeg",
        "replaced",
        "repeg_replaced",
        broker_order_id=str(new_id),
        replaces_order_id=order_id,
        limit_price=target,
        ceiling=ceiling,
        ask=ask,
        crosses_market=bool(crosses_market),
    )
    logger.info(
        "re-peg %s: ONE reprice, order %s → %s at $%.4f (ceiling $%.4f, ask $%s, crosses market: %s)",
        symbol,
        order_id,
        new_id,
        target,
        ceiling,
        f"{ask:.4f}" if isinstance(ask, (int, float)) else "?",
        crosses_market,
    )

    # THE RACE. The order could have filled between the zero-fill read above
    # and the PATCH being applied. Alpaca would then have replaced only the
    # remainder — but the shares the old order took are real, and the new
    # order's own counters know nothing about them. Chasing further from here
    # is how a partial becomes a double position, so: cancel the replacement
    # immediately and carry the ancestor's fill into entry protection so the
    # stop covers it.
    try:
        ancestor = pipeline.broker.get_order_fill_info(order_id) or {}
        ancestor_filled = float(ancestor.get("filled_qty") or 0)
    except Exception:  # noqa: BLE001
        record_swallowed_here(pipeline, "repeg.ancestor_fill", symbol=symbol)
        ancestor_filled = 0.0
    if ancestor_filled > 0:
        logger.warning(
            "re-peg %s: superseded order %s filled %.4f share(s) in the "
            "replace window — cancelling replacement %s rather than risk "
            "buying the same idea twice; the stop will cover the %.4f "
            "already acquired",
            symbol,
            order_id,
            ancestor_filled,
            new_id,
            ancestor_filled,
        )
        from src.execution.broker import now_iso, settle_legs

        sent_at = now_iso()
        pipeline.broker.cancel_entry_order(str(new_id))
        # The replacement can fill while its cancel lands: wait for the broker
        # to confirm it dead, and count what it bought.
        settled = settle_legs(pipeline.broker, [{"id": str(new_id), "side": "buy", "cancel_sent_at": sent_at}])
        if not settled.settled:
            logger.warning(
                "re-peg %s: replacement %s cancel not confirmed (%s) — still tracked; reconciliation and "
                "entry protection follow it",
                symbol,
                new_id,
                settled.reason,
            )
            return str(new_id), ancestor_filled, "replacement_unconfirmed"
        ancestor_filled += sum(leg.filled_qty for leg in settled.legs)
        _record_pipeline_event(
            pipeline,
            ctx,
            symbol,
            "repeg",
            "raced_partial_fill",
            "repeg_ancestor_filled",
            broker_order_id=str(new_id),
            replaces_order_id=order_id,
            fill_qty=ancestor_filled,
        )
        return str(new_id), ancestor_filled, "partial_fill"

    return str(new_id), 0.0, "replaced"


def _repoint_trade(pipeline, trade_row_id, old_order_id: str, new_order_id: str, symbol) -> bool:
    """Point the trades row at the replacement id. True when it stuck."""
    if not trade_row_id:
        logger.error(
            "re-peg %s: no trades row id for order %s — cannot repoint to "
            "%s; fill reconciliation would follow a dead order",
            symbol,
            old_order_id,
            new_order_id,
        )
        return False
    try:
        rows = pipeline.db.repoint_trade_broker_order_id(
            trade_row_id,
            old_order_id=old_order_id,
            new_order_id=new_order_id,
        )
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "repeg.repoint", exc, symbol=symbol)
        logger.error(
            "re-peg %s: repointing trades row %s from %s to %s FAILED: %s — "
            "the WAL row is left for the session-start drain",
            symbol,
            trade_row_id,
            old_order_id,
            new_order_id,
            exc,
        )
        return False
    else:
        record_clean_pass(pipeline, "repeg.repoint", symbol=symbol)
    if not rows:
        logger.warning(
            "re-peg %s: trades row %s no longer pointed at %s — leaving the WAL row for the drain to adjudicate",
            symbol,
            trade_row_id,
            old_order_id,
        )
        return False
    return True


def _delete_repeg_wal(pipeline, wal_row_id) -> None:
    if not wal_row_id:
        return
    try:
        pipeline.db.delete_pending_repeg(wal_row_id)
    except Exception as exc:  # noqa: BLE001
        record_swallowed(pipeline, "repeg.wal_delete", exc)
        logger.warning("re-peg: could not clear WAL row %s: %s", wal_row_id, exc)
