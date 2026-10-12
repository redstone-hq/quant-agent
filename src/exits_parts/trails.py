"""Deterministic trailing-stop pass, lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py).

The body is unchanged apart from `self` -> `owner` (the pipeline instance is
now the first argument); `ExitEngineMixin._apply_deterministic_trails` is a one-line shim with the
same signature. Sell-side code: behaviour is identical.
"""

import logging

from src.sentinel.guarded_exit import record_exit_guard

logger = logging.getLogger("src.pipeline")


def _apply_deterministic_trails(owner, positions, *, run_id: str) -> list[dict]:
    """Raise stops arithmetically, before the LLM is asked anything — 3.7.

    Trailing is arithmetic. Running it here means the reviewer's
    discretionary `TRAIL_STOP` becomes an override for the unusual case
    rather than the only mechanism, and the stop a winner rides up behind
    no longer depends on a model remembering to propose it.

    Every proposal is bounded by `src/risk/trailing.py`: ratchet upward
    only, a minimum move worth an order, and never inside one ordinary
    day's range. Returns the broker orders placed.

    Every evaluation also names WHY its position did or did not trail,
    and that reason is written to `specialist_evidence`
    (`kind='trail_state'`) whenever it differs from the last one on file
    for that stock — so a stop that has never trailed has a findable
    reason, without a row per stock per tick. Recording only: nothing
    here reads the record back to decide anything but whether to write.
    """
    from src.execution.stop_records import (
        recorded_initial_stop,
        replace_stop_and_record,
    )
    from src.execution.exit_path_records import (
        last_trail_states,
        record_trail_code_census,
        record_trail_state_if_changed,
    )
    from src.risk.trailing import TRAIL_CODE_TRAILED, evaluate_trailing_stop

    orders: list[dict] = []
    # Item 196: the per-stock record above is deduplicated by code, so
    # it cannot answer how OFTEN an outcome occurs. This counts every
    # evaluation this run, written once at the end of the pass.
    from collections import Counter as _Counter

    code_census: _Counter = _Counter()
    last_codes = last_trail_states(
        owner.db,
        [getattr(p, "symbol", "") for p in positions],
    )

    def _note(symbol: str, code: str, detail: str = "", **facts) -> None:
        code_census[str(code)] += 1
        record_trail_state_if_changed(
            owner.db,
            last_codes,
            run_id=run_id,
            symbol=symbol,
            code=code,
            detail=detail,
            **facts,
        )

    try:
        from src.execution.scale_in import pending_protection_symbols

        pending_syms = pending_protection_symbols(owner.db)
        record_exit_guard(owner, "trail.pending_protection_symbols")
    except Exception as exc:  # noqa: BLE001
        record_exit_guard(
            owner,
            "trail.pending_protection_symbols",
            exc,
            logger,
            effect="treated as no in-flight restore rows",
        )
        pending_syms = set()
    for position in positions:
        symbol = position.symbol
        if symbol in pending_syms:
            logger.info(
                "trail: skipping %s — a protection-restore WAL row is "
                "in flight (scale-in or sell); replacing the stop now "
                "would race the cancel/rearm sequence",
                symbol,
            )
            _note(symbol, "protection_restore_in_flight")
            continue
        try:
            buy = owner.db.get_symbol_last_buy(symbol)
            record_exit_guard(owner, "trail.last_buy_lookup")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                owner,
                "trail.last_buy_lookup",
                e,
                logger,
                symbol=symbol,
                effect="symbol skipped this pass",
            )
            _note(symbol, "opening_row_lookup_failed", str(e))
            continue
        if not buy:
            _note(symbol, "no_opening_buy_row")
            continue

        # Every field read off `buy` below is pinned AT ENTRY — the
        # reference target (`take_profit`), the denominator of R
        # (`initial_stop_loss`, via `recorded_initial_stop`), the
        # setup label and the measured breakout verdict — and a
        # scale-in writes a SECOND opening row. Reading them off the
        # newest add let the reference target sit above current price
        # and measured R from a stop this trade never opened with.
        # Item 195 fixed only the bar window; these read the same
        # wrong row. `get_position_open_row` resolves the chain by
        # `position_id` and returns None when it cannot, so an
        # unchainable or legacy row keeps exactly today's behaviour.
        try:
            _open_row = owner.db.get_position_open_row(buy)
            # Same `isinstance` discipline the bar-window lookup above
            # uses: anything that is not a real row leaves `buy` alone.
            if isinstance(_open_row, dict):
                buy = _open_row
            record_exit_guard(owner, "trail.position_open_row")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                owner,
                "trail.position_open_row",
                e,
                logger,
                symbol=symbol,
                effect="falls back to last opening row",
            )
        from src.execution.stop_read import read_stop, repair_for

        _stop_read = read_stop(
            owner.broker,
            symbol,
            db=owner.db,
            context="deterministic trail",
            establish=repair_for(owner._repair_stop_coverage, position),
        )
        if _stop_read.unreadable:
            _note(symbol, "live_stop_lookup_failed", _stop_read.reason)
            continue
        current_stop = None if _stop_read.absent else _stop_read.price

        # Only bars SINCE ENTRY matter: a swing low from before the
        # position existed is not a level this trade ever defended.
        #
        # "Since entry" means since the POSITION opened, not since the
        # most recent add. `get_symbol_last_buy` returns the LATEST
        # opening row, so slicing from it made a scale-in erase the
        # trade's whole bar history — while the entry PRICE handed to
        # the trail below is `position.avg_entry`, blended across every
        # add. The window and the price disagreed by construction.
        #
        # Measured 2026-09-30 against the live DB: the structural pivot
        # has produced ZERO of the 9 deterministic stops ever placed
        # (all 9 came from the chandelier or the breakeven ratchet),
        # and in all 11 recorded `no_structure_and_no_usable_chandelier`
        # refusals the window held 0-6 bars against the 7 that
        # `src/risk/trailing.py::_swing_lows` needs before it can
        # confirm a single pivot. MRVL on 2026-09-23 is the clearest
        # case: a position opened 2026-09-17 was evaluated with zero
        # bars because it had been added to that morning.
        #
        # This is NOT a risk-free change, and an earlier version of
        # this comment claimed it was. A longer window can only RAISE
        # `highest`, which raises `chandelier = highest - 3*ATR`; a
        # higher candidate can rise THROUGH the noise floor, and
        # `evaluate_trailing_stop` then refuses OUTRIGHT
        # (`inside_noise_band`) rather than falling back to a lower
        # candidate the shorter window would have accepted. Worked
        # case: price 100, ATR 4, live stop 90. A window whose high is
        # 106 proposes 94 and the stop tightens 90 -> 94; a longer
        # window that sees a pre-add high of 108 proposes 96, which is
        # above the 95 noise floor, so nothing is placed and the stop
        # stays at 90. The wider window LOSES a tighten the narrower
        # one took.
        #
        # The justification is therefore consistency, not safety: the
        # old window disagreed BY CONSTRUCTION with the entry price the
        # same call uses (`position.avg_entry`, blended across every
        # add). Measured 2026-09-30 against all 21 recorded refusals,
        # the exposure is currently zero — see `_swing_lows` in
        # `src/risk/trailing.py` for that measurement. No new constant.
        bars = []
        try:
            all_bars = owner.market.get_ohlcv(symbol, 120) or []
            try:
                opened_ts = owner.db.get_position_open_timestamp(buy)
                record_exit_guard(owner, "trail.position_open_timestamp")
            except Exception as e:  # noqa: BLE001
                record_exit_guard(
                    owner,
                    "trail.position_open_timestamp",
                    e,
                    logger,
                    symbol=symbol,
                    effect="falls back to opening row date",
                )
                opened_ts = None
            if not isinstance(opened_ts, str):
                opened_ts = None
            entry_ts = opened_ts or (buy or {}).get("timestamp") or ""
            entry_day = entry_ts[:10]
            bars = [b for b in all_bars if not entry_day or str(getattr(b, "date", ""))[:10] >= entry_day]
            record_exit_guard(owner, "trail.bar_fetch")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                owner,
                "trail.bar_fetch",
                e,
                logger,
                symbol=symbol,
                effect="trail evaluated without bars",
            )

        # Item 82: the MEASURED half of the breakout verdict, pinned at
        # entry alongside `setup_type` (stored 0/1/NULL). Present → this
        # path reaches construction's OWN verdict so a measured breakout
        # the analyst mislabelled "range" is trailed as Type B, not Type
        # A; NULL (legacy row, or a pre-item-82 entry) → `is_trend_trade`
        # inside `evaluate_trailing_stop` falls back to the label alone,
        # exactly the pre-item-82 `!= "breakout"` behaviour. Read the
        # SAME way the pace/progress path does (#652).
        _sc_raw = (buy or {}).get("structural_ceiling")
        structural_ceiling = None if _sc_raw is None else bool(_sc_raw)

        evaluation = evaluate_trailing_stop(
            symbol=symbol,
            setup_type=(buy or {}).get("setup_type"),
            structural_ceiling=structural_ceiling,
            entry=position.avg_entry,
            current_price=position.current_price,
            current_stop=current_stop,
            # THE ENTRY TARGET, never the live `take_profit` (item
            # 194, 2026-10-01). The comment above says every field read
            # off `buy` is pinned at entry; `take_profit` stopped being
            # so the moment `update_open_take_profit` existed. That
            # mattered once the re-derivation swept the whole book: a
            # target revised DOWN crosses a range trade from the
            # below-target breakeven/+2R ratchets into the structural
            # trail, the trail only ever ratchets toward price, and so
            # restoring the target on the next session does NOT give
            # the stop back — the tightening accumulated instead of
            # cancelling.
            #
            # THIS MOVES PROTECTION IN BOTH DIRECTIONS AND BOTH ARE
            # INTENDED. It removes a ratchet that could never be given
            # back; it also LOOSENS the boundary case, because where a
            # revised target sits below the entry target, a fall to
            # just under the old boundary used to hand the stop to the
            # structural trail and now leaves it in the earlier
            # ratchets. Less tightening there is a real loosening of
            # future protection, accepted because the ratchet it
            # replaces was irreversible and this one is not.
            #
            # "PINNED" IS THE INTENT OF THE COLUMN, NOT A VERIFIED
            # PROPERTY OF EVERY ROW: the `initial_take_profit`
            # migration backfilled it FROM `take_profit` for every
            # legacy row carrying a target, so a row revised before
            # that migration ran was backfilled with an already-revised
            # number. Whether any such row exists is UNVERIFIED. The
            # null fallback below is safe either way — it only applies
            # to rows the migration left empty.
            reference_target=((buy or {}).get("initial_take_profit") or (buy or {}).get("take_profit")),
            bars=bars,
            atr=owner._atr_for_symbol(symbol),
            # Shorts-safe (Stage 2): `qty` supplies only the side so a
            # short's trail mirrors instead of running the long formula
            # backwards. `get_symbol_last_buy` above only ever returns a
            # BUY row, so a short is filtered out before this point
            # regardless — this is forward-compatible plumbing, not a
            # behaviour change on today's long-only book.
            qty=position.qty,
            # Fix #3 (2026-09-04 audit): the ENTRY stop, never the live
            # one. `initial_stop_loss` is frozen at insert / first
            # write-back; `stop_loss` itself is the live recorded level
            # after a trail. Powers the Type A +1R breakeven ratchet.
            initial_stop=recorded_initial_stop(buy),
        )
        proposal = evaluation.proposal
        if proposal is None:
            _note(
                symbol,
                evaluation.code,
                # Item 212 follow-up: on a range name the R-ratchet leg
                # supplies the code, so the STRUCTURAL leg's own refusal
                # reason would otherwise never be recorded again. It is
                # both a recorded field and part of the dedupe identity.
                structural_code=evaluation.structural_code,
                current_stop=current_stop,
                current_price=position.current_price,
                entry=position.avg_entry,
                setup_type=(buy or {}).get("setup_type"),
            )
            continue
        code_census[TRAIL_CODE_TRAILED] += 1
        logger.info("Deterministic trail: %s", proposal.reason)
        try:
            from src.execution.stop_records import accepted_stop_order

            order = replace_stop_and_record(
                owner.broker,
                owner.db,
                symbol,
                proposal.new_stop,
                run_id=run_id,
                caller="deterministic_trail",
            )
            record_exit_guard(owner, "trail.replace_stop")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                owner,
                "trail.replace_stop",
                e,
                logger,
                symbol=symbol,
                effect="old stop remains in force",
            )
            _note(
                symbol,
                "replace_raised",
                str(e),
                proposed_stop=proposal.new_stop,
                current_stop=current_stop,
            )
            continue
        if isinstance(order, dict) and order.get("legs"):
            # Item 201: the trailing path now amends every resting leg in
            # place, so the per-leg outcome is recorded here too. This is
            # the evidence that settles whether a fractional position's two
            # hybrid legs both amend — the ex-dividend shift alone would
            # never produce it (0 of 80 production trades between
            # 2026-09-02 and 2026-09-30 were ex-dividend shifts).
            from src.execution.exit_path_records import record_stop_shift_legs

            _legs = order.get("legs") or []
            _ok = [l for l in _legs if l.get("outcome") == "amended"]
            # `amend_status` is the AMEND's own verdict. The broker status
            # on a live replacement is "new"/"accepted"/..., so reading
            # that would call an ordinary success a failure.
            _astatus = str(order.get("amend_status") or ("accepted" if len(_ok) == len(_legs) else "partial"))
            record_stop_shift_legs(
                owner.db,
                symbol=symbol,
                amount=0.0,
                mode="trail_amend",
                status=_astatus,
                shifted=len(_ok),
                total=len(_legs),
                legs=_legs,
                run_id=run_id,
            )
            if _astatus in ("partial", "refused", "unknown", "naked", "market_closed"):
                # Telegram is muted, so this row and this alert are the
                # whole evidence that a leg did not move.
                try:
                    from src.notifier import send_owner_alert
                    from src.execution.exit_path_records import (
                        stop_shift_incomplete_text,
                    )

                    send_owner_alert(
                        stop_shift_incomplete_text(symbol, _astatus, len(_ok), len(_legs)),
                        symbols=[symbol],
                    )
                    record_exit_guard(owner, "trail.owner_alert")
                except Exception as e:  # noqa: BLE001
                    record_exit_guard(
                        owner,
                        "trail.owner_alert",
                        e,
                        logger,
                        symbol=symbol,
                        effect="owner not told of incomplete stop shift",
                    )
        if not order or (isinstance(order, dict) and not accepted_stop_order(order)):
            _detail = str((order or {}).get("status") or "") if isinstance(order, dict) else ""
            if isinstance(order, dict) and order.get("legs"):
                # `record_trail_state_if_changed` writes nothing when the
                # code repeats, and a bare status names no leg, no level
                # and no order id. Spell the outcome out here as well.
                _detail = "; ".join(
                    [_detail or "amend did not fully land"]
                    + [
                        f"leg {l.get('id')} qty {l.get('qty')} "
                        f"{l.get('old_stop')}->{l.get('new_stop')} "
                        f"{l.get('outcome')}"
                        + (f" (new id {l.get('new_id')})" if l.get("new_id") else "")
                        + (f": {l.get('detail')}" if l.get("detail") else "")
                        for l in (order.get("legs") or [])
                    ]
                )
            _note(
                symbol,
                "replace_not_accepted",
                _detail,
                proposed_stop=proposal.new_stop,
                current_stop=current_stop,
            )
            continue
        _note(
            symbol,
            TRAIL_CODE_TRAILED,
            proposal.reason,
            # Carried on the SUCCESS branch too: the case this field
            # exists for is the R-ratchet leg winning, which is a
            # trailed row, not a refusal row.
            structural_code=evaluation.structural_code,
            proposed_stop=proposal.new_stop,
            current_stop=current_stop,
        )
        if isinstance(order, dict):
            order.setdefault("action", "TRAIL_STOP")
        orders.append(order)
        try:
            owner.db.insert_trade(
                symbol=symbol,
                action="TRAIL_STOP",
                qty=position.qty,
                price=proposal.new_stop,
                reasoning=proposal.reason,
                run_id=run_id,
                stop_loss=proposal.new_stop,
            )
            record_exit_guard(owner, "trail.trade_row_write")
        except Exception as e:  # noqa: BLE001
            record_exit_guard(
                owner,
                "trail.trade_row_write",
                e,
                logger,
                symbol=symbol,
                effect="trail not recorded in trade rows",
            )

    record_trail_code_census(
        owner.db,
        run_id=run_id,
        counts=dict(code_census),
    )
    return orders
