"""Protect every filled entry, lifted verbatim out of `ExecutionStage._run_session` (src/stage_execution.py).

The `for spec in pending_entry_stops` loop body is unchanged, dedented one
level; `_run_session` now calls `protect_pending_entry_stops` at the exact
point the loop used to run. Live-money protection code: behaviour is identical.
"""

from __future__ import annotations

from src.pipeline_stages import (
    _alert_owner_entry_cancelled,
    _alert_owner_protection_failed,
    _record_pipeline_event,
    _record_scale_in_window_closed,
    _repeg_entry_order,
    logger,
)


def protect_pending_entry_stops(pipeline, ctx, run_id, pending_entry_stops) -> None:
    """Place the GTC stop-limit keyed to the ACTUAL fill for every pending entry."""
    for spec in pending_entry_stops:
        if not spec.get("order_id"):
            continue
        try:
            # Single-shot reprice FIRST, protection second, always. The
            # reprice may hand back a different order id (Alpaca mints one
            # per replacement) plus any shares an ancestor order filled;
            # both feed straight into the stop so no filled share is left
            # without one. With `execution.repeg_enabled` off — the
            # default — this returns the same id and 0.0 without making a
            # single broker call.
            try:
                entry_order_id, superseded_fill = _repeg_entry_order(
                    pipeline,
                    ctx,
                    spec,
                )
            except Exception as repeg_exc:  # noqa: BLE001
                # Protection must run even if the chase blows up. Fall
                # back to the original id: at worst the re-peg did
                # nothing, which is the failure direction we want.
                logger.error(
                    "re-peg raised for %s: %s — protecting the ORIGINAL order %s unchanged",
                    spec["symbol"],
                    repeg_exc,
                    spec["order_id"],
                )
                entry_order_id, superseded_fill = spec["order_id"], 0.0
            entry_side = spec.get("side", "buy")
            # End-of-session cancel of a still-unfilled entry lives
            # inside `place_entry_protection` (see its docstring for the
            # derivation); the callback is how the owner gets told, with
            # the prices this stage tried, which the broker does not know.
            protection = pipeline.broker.place_entry_protection(
                symbol=spec["symbol"],
                order_id=entry_order_id,
                stop_price=spec["stop_price"],
                requested_qty=spec["qty"],
                superseded_filled_qty=superseded_fill,
                side=entry_side,
                on_unfilled_cancel=(lambda info, _spec=spec: _alert_owner_entry_cancelled(pipeline, _spec, info)),
                cover_full_position=bool(spec.get("cover_full_position")),
                held_qty_before=float(spec.get("held_qty_before") or 0),
                persist_restore=pipeline._persist_orphaned_protection_restore,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                spec["symbol"],
                "protection",
                "placed" if protection else "not_placed",
                "protective_stop_result",
                entry_order_id=entry_order_id,
                stop_price=spec["stop_price"],
                protective_order_id=(protection or {}).get("id") if isinstance(protection, dict) else None,
            )
            # Board item 193 — close the measured unprotected window.
            # Every scale-in cancel that reached the broker emits exactly
            # one of these, carrying the same `wal_row_id` as its
            # `protective_sell_cancelled` event, so an unpaired cancel is
            # visible as a missing partner rather than inferred from row
            # ids. `held_qty_before` is the WHOLE position the cancel
            # exposed, not the size of the add.
            _record_scale_in_window_closed(
                pipeline,
                ctx,
                spec,
                covered=bool(protection),
            )
            # Spec §11.1 guard 2. The broker has already retried hard and
            # immediately (guard 1) by the time this is reached, so a
            # falsy `protection` means a position is open at the broker
            # with NO stop on it, and a non-zero `uncovered_qty` means
            # part of one is. Neither may be reported as a log line: a log
            # line is read after the fact, and the whole reason fractional
            # sizing is acceptable is that the unprotected window is brief
            # — which is only true if a HUMAN is told the moment it stops
            # being brief. Never lets an alerting failure abort the
            # session.
            _alert_owner_protection_failed(
                pipeline,
                spec,
                protection,
                entry_order_id,
            )
            if spec.get("cover_full_position") or spec.get("wal_row_id") is not None:
                from src.execution.scale_in import (
                    discharge_scale_in_wal,
                    restore_cancelled_stops,
                )

                filled_here = 0.0
                try:
                    info = (
                        pipeline.broker.get_order_fill_info(
                            entry_order_id,
                        )
                        or {}
                    )
                    filled_here = float(info.get("filled_qty") or 0)
                except Exception:  # noqa: BLE001
                    filled_here = 0.0
                uncovered = 0.0
                if isinstance(protection, dict):
                    try:
                        uncovered = float(protection.get("uncovered_qty") or 0)
                    except (TypeError, ValueError):
                        uncovered = 0.0
                # A short add's protection is a BUY-stop; restore and
                # write-back must both use the short side.
                _spec_is_short = str(spec.get("side", "buy")).lower() != "buy"
                if protection is None and filled_here <= 0:
                    if restore_cancelled_stops(
                        pipeline.broker,
                        spec["symbol"],
                        spec.get("cancelled_specs") or [],
                        side="buy" if _spec_is_short else "sell",
                    ):
                        discharge_scale_in_wal(
                            pipeline.db,
                            spec.get("wal_row_id"),
                        )
                elif protection is not None and uncovered <= 0:
                    from src.execution.stop_records import (
                        accepted_stop_order,
                        write_back_stop_loss,
                    )

                    if accepted_stop_order(protection) or not isinstance(
                        protection,
                        dict,
                    ):
                        write_back_stop_loss(
                            pipeline.db,
                            spec["symbol"],
                            spec["stop_price"],
                            is_short=_spec_is_short,
                        )
                    discharge_scale_in_wal(
                        pipeline.db,
                        spec.get("wal_row_id"),
                    )
                # else: fill happened and rearm did not fully cover.
                # WAL stays. Guard 2 already paged the owner.
            # D7 (Stage 3): MANDATORY escalation for a SHORT. A long's
            # loss is bounded at -100%; a naked short's is not, so
            # relying on the next session's coverage-reconcile belt (the
            # long behaviour, unchanged above) is not an acceptable
            # exposure window here. If the protective stop could not be
            # placed after the entry actually filled shares, submit an
            # IMMEDIATE market COVER for the filled quantity and log it
            # loudly — this is not a normal exit, it is damage control.
            if protection is None and entry_side == "sell_short":
                try:
                    fill_info = pipeline.broker.get_order_fill_info(entry_order_id) or {}
                    filled_qty = float(fill_info.get("filled_qty") or 0)
                except Exception as fill_exc:  # noqa: BLE001
                    logger.critical(
                        "SHORT %s: could not even determine the filled "
                        "quantity after protection failed (%s) — treating "
                        "as the full requested qty %.4f to force a cover "
                        "attempt rather than leaving a possibly-naked "
                        "short untouched",
                        spec["symbol"],
                        fill_exc,
                        spec["qty"],
                    )
                    filled_qty = float(spec.get("qty") or 0)
                # H1: on a SHORT SCALE-IN the protective buy-stop that
                # covered the PRE-EXISTING short leg was already cancelled
                # in prep, so covering only the add's fill (filled_qty)
                # would leave that older leg naked — exactly the unbounded
                # exposure D7 exists to prevent. Cover the ENLARGED short:
                # the broker's current qty (magnitude), the same authority
                # cover_qty_for_rearm uses, with the |fill|+|held| fallback
                # when the broker cannot be read. For a NEW short this
                # equals filled_qty, so the non-scale-in path is unchanged.
                if spec.get("cover_full_position"):
                    from src.execution.scale_in import cover_qty_for_rearm

                    cover_qty = cover_qty_for_rearm(
                        pipeline.broker,
                        symbol=spec["symbol"],
                        filled_qty=filled_qty,
                        held_qty_before=float(spec.get("held_qty_before") or 0),
                    )
                    if cover_qty < filled_qty:
                        # Never cover LESS than what we know filled.
                        cover_qty = filled_qty
                else:
                    cover_qty = filled_qty
                if cover_qty > 0:
                    logger.critical(
                        "SHORT %s: PROTECTIVE STOP FAILED after %.4f "
                        "share(s) filled — a naked short has UNBOUNDED "
                        "loss. Submitting an IMMEDIATE market COVER of the "
                        "full short (%.4f) instead of waiting for the next "
                        "reconcile pass.",
                        spec["symbol"],
                        filled_qty,
                        cover_qty,
                    )
                    try:
                        cover_order = pipeline.broker.submit_order(
                            symbol=spec["symbol"],
                            qty=cover_qty,
                            side="buy",
                        )
                        cover_id = cover_order.get("id") if isinstance(cover_order, dict) else None
                        # Board item 183 follow-up (2026-09-30).
                        # `AlpacaBroker.submit_order` no longer RAISES on
                        # a rejection the broker's own response calls
                        # terminal — it returns
                        # `{"id": None, "status": "rejected_by_broker"}`.
                        # Every other `submit_order` caller in this repo
                        # already tests the RESULT via `_order_accepted`;
                        # this one only read `.get("id")`, so a rejected
                        # emergency cover would have written a
                        # `fill_status="submitted"` EMERGENCY_COVER row
                        # and filed a SUCCESS event for an order that
                        # does not exist — on the one path that runs
                        # when a SHORT has filled and its protective stop
                        # did NOT place, i.e. a naked short with
                        # unbounded loss and nobody paged. Raising here
                        # puts a non-accept back on EXACTLY the path a
                        # raised submit took before #786: the CRITICAL
                        # operator page and the `emergency_cover_failed`
                        # event in the `except` branch below, and no
                        # trade row, because the raise precedes
                        # `insert_trade`. `_order_accepted` also catches
                        # the desk's OWN pre-flight refusals (the
                        # fat-finger guard, the kill switch), which reach
                        # here identically id-less and are equally not a
                        # cover.
                        if not pipeline._order_accepted(
                            cover_order,
                            spec["symbol"],
                            "buy",
                        ):
                            raise RuntimeError(f"broker did not accept the emergency cover order: {cover_order!r}")
                        pipeline.db.insert_trade(
                            symbol=spec["symbol"],
                            action="EMERGENCY_COVER",
                            qty=cover_qty,
                            price=0.0,
                            reasoning=(
                                "protective stop failed to place after a "
                                "SHORT entry filled — immediate market "
                                "cover of the full (enlarged) short to bound "
                                "an otherwise naked short"
                            ),
                            run_id=run_id,
                            broker_order_id=cover_id,
                            fill_status="submitted",
                        )
                        _record_pipeline_event(
                            pipeline,
                            ctx,
                            spec["symbol"],
                            "protection",
                            "emergency_cover",
                            "naked_short_protection_failed",
                            qty=cover_qty,
                            broker_order_id=cover_id,
                        )
                    except Exception as cover_exc:  # noqa: BLE001
                        logger.critical(
                            "SHORT %s: EMERGENCY COVER ALSO FAILED (%s) — "
                            "%.4f share(s) are NAKED SHORT with NO "
                            "protective stop and NO cover in flight. "
                            "REQUIRES IMMEDIATE OPERATOR INTERVENTION.",
                            spec["symbol"],
                            cover_exc,
                            cover_qty,
                        )
                        _record_pipeline_event(
                            pipeline,
                            ctx,
                            spec["symbol"],
                            "protection",
                            "emergency_cover_failed",
                            "naked_short_no_protection_no_cover",
                            qty=cover_qty,
                            detail=str(cover_exc),
                        )
        except Exception as e:  # noqa: BLE001 — never abort the session here
            logger.error(
                "entry protection raised for %s: %s — position may be unprotected until the next coverage reconcile",
                spec["symbol"],
                e,
            )
            _record_pipeline_event(
                pipeline,
                ctx,
                spec["symbol"],
                "protection",
                "failed",
                "protective_stop_exception",
                detail=str(e),
                entry_order_id=spec["order_id"],
            )
            _record_scale_in_window_closed(
                pipeline,
                ctx,
                spec,
                covered=False,
            )
