"""Target-revision adjudication for a held position: the ratification of a seat's target-revision flags against the chart and the record it files.

Lifted verbatim out of `ExitEngineMixin` (src/pipeline_exits.py) as a
standalone class: every collaborator is an explicit keyword-only
constructor argument, so it is built and exercised without a pipeline.
The mixin keeps a thin method of the same name that builds this object
and calls it, so every existing caller and patch target is unchanged.
"""

import logging
from src.trading_calendar import et_today

#: Logs under `src.pipeline`, as the bodies did before the move;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class TargetRevision:
    """Target-revision adjudication for a held position: the ratification of a seat's target-revision flags against the chart and the record it files."""

    def __init__(self, *, file_target_revision, broker, config, db, market, risk_engine) -> None:
        self._file_target_revision = file_target_revision
        self.broker = broker
        self.config = config
        self.db = db
        self.market = market
        self.risk_engine = risk_engine

    def _adjudicate_target_revision_flags(
        self,
        review,
        positions,
        *,
        run_id: str,
        seat: str,
    ) -> list[dict]:
        """Re-measure every open position's take-profit, every session.

        THE WAY IN IS THE OPEN BOOK, NOT A FLAG (item 194, 2026-10-01).
        Every held position is adjudicated on every run. A seat raising
        `src.models.TargetRevisionFlag` no longer decides WHETHER a symbol
        is measured, only the seat label and prose evidence recorded
        against it; a flag for a symbol the broker does not show as held is
        still filed as its own finding. Widening the way in added no
        number: the flag carries symbol and evidence and no price, so it
        never fed the arithmetic, and the derivation itself is unchanged.

        A seat raises `src.models.TargetRevisionFlag` — SYMBOL AND EVIDENCE,
        no price; the schema has no price field. This method supplies
        `src.risk.target_revision.assess_target_revision` with real numbers
        recomputed straight from bars, using the same deterministic, no-LLM
        machinery `_structural_protection_for_holding` uses
        (`compute_indicators` for ATR, `find_structural_levels` for levels,
        `levels_coverage_for_bars` for whether an empty result is a fault or
        a reading), and writes the outcome.

        DELIBERATELY runs AFTER `_midday_execute_llm_actions`. Every exit
        decision this session makes has already been made and vetoed against
        `metric_deltas` built before this point, so a revision cannot reach
        them even in principle. That is the second of two independent
        defences; the first is that progress/pace are measured against the
        entry `initial_take_profit` (see `_build_position_facts`), so a
        revision cannot move a guarded metric at all. It can still reach
        a LATER session's reviewing model as prose, through
        `distance_to_target_pct`; what no deterministic gate does is read
        it.

        EVERY flag produces a durable row — a re-derivation, a named refusal,
        or a named data fault. Never a silent no-op and never a blank.
        Returns the outcome payloads for the session result / cockpit.

        Never raises: a failure here must not take down a review that has
        already executed its orders.
        """
        from src.data.levels import (
            BREAKOUT_PROJECTION_ATR_MULTIPLE,
            CLUSTER_TOLERANCE_PCT,
            COVERAGE_UNKNOWN,
            MAX_HORIZON_SESSIONS,
            MAX_REACH_ATR_MULTIPLE,
            MIN_TARGET_ATR_MULTIPLE,
        )
        from src.risk.target_revision import (
            SEAT_STRUCTURAL_SWEEP,
            SWEEP_EVIDENCE,
            assess_target_revision,
            raw_trigger_flags,
            level_backing_target,
        )
        from src.trading_calendar import et_today

        # Direction comes from BROKER TRUTH (the sign of the held qty), never
        # from the flag — the seat names a symbol, not a side.
        held: dict[str, object] = {}
        for p in positions or []:
            _sym = str(getattr(p, "symbol", "") or "").strip().upper()
            if _sym:
                held[_sym] = p

        # THE WAY IN (item 194). Every OPEN POSITION is adjudicated every
        # session, not only the symbols a seat happened to raise. A seat
        # flag carries symbol and evidence and no price, so it contributes
        # nothing to the arithmetic below and widening the way in
        # introduces NO new number: the sweep makes the identical call with
        # the identical ratified bars. What the flag-only gate produced was
        # not safety but arbitrary coverage — a position that quietly grew
        # a wall between its entry and its stored target kept quoting a
        # target aimed past that wall for as long as nobody mentioned the
        # ticker, and the stored target decides which trailing-stop regime
        # a range trade is in (`src/risk/trailing.py`), so the stale number
        # was already governing a live stop.
        work: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for flag in list(getattr(review, "target_revision_flags", None) or []):
            sym = str(getattr(flag, "symbol", "") or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            work.append((sym, seat, str(getattr(flag, "evidence", "") or "")))
        for sym in sorted(held):
            if sym in seen:
                continue
            seen.add(sym)
            work.append((sym, SEAT_STRUCTURAL_SWEEP, SWEEP_EVIDENCE))
        if not work:
            return []

        # ONE batched bar read for the whole sweep (item 194 fault 3). The
        # seat flag used to ration a serial per-name fetch; removing the
        # gate without removing the serialism would have turned one or two
        # round trips a session into one per held name. `get_ohlcv_batch`
        # already exists and is what the rest of the desk uses for a
        # multi-name read. A miss falls through to the per-name fetch
        # below, so a provider that cannot batch is degraded, not broken.
        # NO timeout number is introduced: any seconds value would be an
        # invented constant, and batching removes the serial exposure that
        # motivated one.
        batched_bars: dict[str, list] = {}
        serial_bar_read = False
        try:
            batched_bars = dict(
                self.market.get_ohlcv_batch(
                    [sym for sym, _, _ in work],
                    self.config.trading.lookback_days,
                )
                or {}
            )
        except Exception as exc:  # noqa: BLE001
            serial_bar_read = True
            logger.warning(
                "target revision: batched bar read failed (%s) — falling back to a per-name fetch",
                exc,
            )

        # FAULT 6: an unchanged, unapplied, fully recomputable outcome is
        # not re-filed every session. Same intent as the trail's own
        # `record_trail_state_if_changed`: the row says WHEN a thing
        # changed, and a book of eleven names filing an identical
        # no-pinned-horizon refusal daily is storage of recomputable state.
        prior_codes: dict[str, str] = {}
        try:
            for _sym, _rows in (self.db.get_target_revisions([sym for sym, _, _ in work]) or {}).items():
                if _rows:
                    prior_codes[str(_sym).upper()] = str(_rows[0].get("code") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "target revision: prior-outcome read failed (%s) — every outcome is filed this session",
                exc,
            )

        risk_cfg = getattr(getattr(self, "risk_engine", None), "config", None)
        target_cfg = {
            "min_target_atr_multiple": getattr(risk_cfg, "min_target_atr_multiple", MIN_TARGET_ATR_MULTIPLE),
            "breakout_projection_atr_multiple": getattr(
                risk_cfg, "breakout_projection_atr_multiple", BREAKOUT_PROJECTION_ATR_MULTIPLE
            ),
            "max_reach_atr_multiple": getattr(risk_cfg, "max_target_reach_atr_multiple", MAX_REACH_ATR_MULTIPLE),
            "max_horizon_sessions": getattr(risk_cfg, "max_target_horizon_sessions", MAX_HORIZON_SESSIONS),
        }

        # FAULT 5 (item 194): the batch fallback must not degrade
        # silently. When the batched read failed, every outcome this
        # session carries the fact in its durable detail text.
        _serial_note = (
            (
                " [the batched bar read was unavailable this session, so this "
                "position's bars were fetched one name at a time]"
            )
            if serial_bar_read
            else ""
        )

        outcomes: list[dict] = []
        for sym, flag_seat, evidence in work:
            # FAULT 4 (item 194): every name is adjudicated inside its own
            # guard. Before this, one unexpected exception anywhere in the
            # body unwound to the single try at the call site, which logs
            # and returns an empty list — and because the work list is
            # sorted, the SAME tail of the book was silently dropped every
            # time, each dropped name keeping a stored target the record
            # did not mark as unmeasured. A failure now costs exactly one
            # name, and that name gets a durable row saying so.
            try:
                position = held.get(sym)
                if position is None:
                    # The seat flagged something not held. Filed, not silently
                    # dropped, because a flag on a symbol that is not in the
                    # book is itself a finding about the seat's view of the book.
                    outcomes.append(
                        self._file_target_revision(
                            run_id=run_id,
                            symbol=sym,
                            seat=flag_seat,
                            evidence=evidence,
                            code="REFUSAL_NOT_HELD",
                            applied=False,
                            detail=(
                                "the seat flagged a take-profit revision for a symbol the broker does not show as held"
                            ),
                        )
                    )
                    continue

                is_short = float(getattr(position, "qty", 0) or 0) < 0
                try:
                    buy = (
                        self.db.get_symbol_last_buy(
                            sym,
                            action="SHORT" if is_short else None,
                        )
                        if is_short
                        else self.db.get_symbol_last_buy(sym)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "target revision: opening-row lookup failed for %s (%s)",
                        sym,
                        exc,
                    )
                    buy = None
                buy = buy or {}

                # Same bars, same window, same helpers as
                # `_structural_protection_for_holding` — and the same rule that
                # the price fed to a break test is the latest COMPLETED DAILY
                # CLOSE, never a live quote.
                levels: list[float] = []
                atr = close_price = bar_date = None
                coverage = None
                try:
                    bars = batched_bars.get(sym)
                    if bars is None:
                        bars = (
                            self.market.get_ohlcv(
                                sym,
                                self.config.trading.lookback_days,
                            )
                            or []
                        )
                    from src.data.levels import (
                        find_structural_levels,
                        structure_coverage,
                    )
                    from src.data.technical import compute_indicators

                    # What the bar history behind `levels` was, so an empty list
                    # from a dead feed is a DATA fault and one from a measured,
                    # structureless chart is a refusal — the same distinction
                    # `_derive_target` passes at entry.
                    coverage = structure_coverage(bars)
                    if bars:
                        last_bar = sorted(bars, key=lambda b: b.date)[-1]
                        close_price = float(last_bar.close)
                        bar_date = str(last_bar.date)
                        atr = compute_indicators(sym, bars).atr_14
                        supports, resistances = find_structural_levels(bars)
                        levels = sorted(lv.price for lv in (*supports, *resistances))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "target revision: bars/indicator fetch failed for %s (%s) "
                        "— the flag is filed as a data fault, not judged",
                        sym,
                        exc,
                    )

                stored_target = None
                try:
                    stored_target = float(buy.get("take_profit") or 0) or None
                except (TypeError, ValueError):
                    stored_target = None

                # Which level this target was measured against, recovered by the
                # same identity test the stop side uses. None for a measured-move
                # target, which is correct: it never sat on a level.
                target_level = level_backing_target(
                    stored_target=stored_target,
                    computed_levels=levels,
                    # NOT a knob — the exact constant `find_structural_levels`
                    # clustered these zones with (docs/WORK.md item 46).
                    level_cluster_tolerance_pct=CLUSTER_TOLERANCE_PCT,
                )

                # Cross-day confirmation, keyed off THIS READ's own bar_date so
                # several intraday cycles re-reading one close are never
                # miscounted as two confirming days.
                effective_bar_date = bar_date or str(et_today())
                #
                # ALL THREE triggers are confirmed the same way (item 194):
                # the reach and wall triggers used to fire on one session's
                # reading, so a target near a bound flipped session to
                # session, and because a target moving down used to cross a
                # range trade into a tighter trailing regime the flip was a
                # one-way ratchet. The regime boundary now reads the pinned
                # entry target, and this is the second brake.
                break_seen_prior_close = False
                reach_seen_prior_close = False
                wall_seen_prior_close = False
                try:
                    for _flag, _name in (
                        ("raw_broken", "break_seen_prior_close"),
                        ("raw_reach", "reach_seen_prior_close"),
                        ("raw_wall", "wall_seen_prior_close"),
                    ):
                        _prior = self.db.get_prior_target_level_break(
                            [sym],
                            today_bar_date=effective_bar_date,
                            exclude_run_id=run_id,
                            flag=_flag,
                        )
                        if _name == "break_seen_prior_close":
                            break_seen_prior_close = bool(_prior.get(sym, False))
                        elif _name == "reach_seen_prior_close":
                            reach_seen_prior_close = bool(_prior.get(sym, False))
                        else:
                            wall_seen_prior_close = bool(_prior.get(sym, False))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "target revision: prior-close read failed for %s (%s) — "
                        "today's triggers, if any, start unconfirmed",
                        sym,
                        exc,
                    )

                # Sessions this position has already spent out of its pinned
                # horizon — the HOLIDAY-AWARE broker count (item 165), the same
                # `broker.trading_sessions_held` the reviewer's own facts and
                # the exit guard's noise band read; never a calendar-day count
                # and never a default. It is what lets a target the price has
                # run past be re-anchored on the close over the REMAINING
                # horizon (item 114); a None here simply means no re-anchor is
                # attempted and the existing refusal stands.
                sessions_held: int | None = None
                entry_ts = (buy.get("timestamp") or "")[:10]
                if entry_ts:
                    try:
                        from datetime import date as _date

                        sessions_held = self.broker.trading_sessions_held(
                            _date.fromisoformat(entry_ts),
                            et_today(),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "target revision: sessions-held read failed for %s "
                            "(%s) — no remaining horizon, so a target behind "
                            "price is refused rather than re-anchored",
                            sym,
                            exc,
                        )
                        sessions_held = None

                outcome = assess_target_revision(
                    symbol=sym,
                    direction="short" if is_short else "long",
                    entry_price=float(getattr(position, "avg_entry", 0) or 0) or None,
                    stored_target=stored_target,
                    target_level=target_level,
                    pinned_horizon_sessions=buy.get("expected_horizon_sessions"),
                    setup_type=buy.get("setup_type") or None,
                    levels=levels,
                    atr=atr,
                    close_price=close_price,
                    levels_coverage=coverage or COVERAGE_UNKNOWN,
                    break_seen_prior_close=break_seen_prior_close,
                    reach_seen_prior_close=reach_seen_prior_close,
                    wall_seen_prior_close=wall_seen_prior_close,
                    sessions_held=sessions_held,
                    # The same ratified derivation bars the constructor passes at
                    # entry, read off `risk_engine.config` (what
                    # `ConstructorConfig` itself mirrors). Read defensively
                    # because this method must survive a lightweight pipeline
                    # double in unit tests that never built a real risk_engine;
                    # the fallbacks are `src.data.levels`' own module constants,
                    # not a second invented set of numbers.
                    **target_cfg,
                )

                # File today's raw break state for the NEXT trading day to
                # confirm against — the same read/persist shape as
                # `_structural_protection_for_holding`. A `None` from the break
                # test means the question could not be asked; nothing is filed,
                # so a missing input can never become half of a confirmation.
                raw_flags = raw_trigger_flags(
                    entry_price=float(getattr(position, "avg_entry", 0) or 0) or None,
                    stored_target=stored_target,
                    target_level=target_level,
                    atr=atr,
                    close_price=close_price,
                    horizon_sessions=buy.get("expected_horizon_sessions"),
                    levels=levels,
                    is_short=is_short,
                    # Every bar `raw_trigger_flags` reads EXCEPT the
                    # breakout projection, which is a derivation input and
                    # not a trigger test.
                    **{k: v for k, v in target_cfg.items() if k != "breakout_projection_atr_multiple"},
                )
                raw_broken = raw_flags["raw_broken"]
                if bar_date and any(v is not None for v in raw_flags.values()):
                    try:
                        self.db.save_target_level_break(
                            run_id=run_id,
                            symbol=sym,
                            bar_date=bar_date,
                            raw_broken=raw_broken,
                            raw_reach=raw_flags["raw_reach"],
                            raw_wall=raw_flags["raw_wall"],
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "target revision: failed to persist %s break state "
                            "(%s) — tomorrow's read starts unconfirmed",
                            sym,
                            exc,
                        )

                applied = False
                if outcome.revised and outcome.new_price:
                    try:
                        applied = bool(
                            self.db.update_open_take_profit(
                                sym,
                                outcome.new_price,
                                action="SHORT" if is_short else "BUY",
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "target revision: write-back failed for %s (%s) — the stored target stands",
                            sym,
                            exc,
                        )
                        applied = False
                    if applied:
                        logger.info(
                            "Target revised: %s $%.2f -> $%.2f (%s, %s) — "
                            "progress/pace stay measured against the pinned "
                            "entry target",
                            sym,
                            outcome.prior_price or 0.0,
                            outcome.new_price,
                            outcome.basis,
                            outcome.trigger,
                        )
                if not applied and outcome.revised:
                    # The derivation succeeded but the row did not move. Recorded
                    # as its own outcome so the record can never claim a revision
                    # the trade row does not carry.
                    outcomes.append(
                        self._file_target_revision(
                            run_id=run_id,
                            symbol=sym,
                            seat=flag_seat,
                            evidence=evidence,
                            code="FAULT_REVISION_WRITE_FAILED",
                            applied=False,
                            trigger=outcome.trigger,
                            prior_price=outcome.prior_price,
                            detail=(
                                f"{outcome.trigger} fired and re-derived "
                                f"${outcome.new_price:,.2f}, but the opening row could "
                                f"not be updated — the stored target stands"
                            ),
                        )
                    )
                    continue

                outcomes.append(
                    self._file_target_revision(
                        run_id=run_id,
                        symbol=sym,
                        seat=flag_seat,
                        evidence=evidence,
                        code=outcome.code,
                        applied=applied,
                        trigger=outcome.trigger,
                        prior_price=outcome.prior_price,
                        new_price=outcome.new_price,
                        basis=outcome.basis,
                        level_used=outcome.level_used,
                        detail=(outcome.detail + _serial_note) if _serial_note else outcome.detail,
                        # A degraded session is never deduped away: the whole
                        # point of recording it is that somebody measuring a
                        # slow session later can see WHY it was slow.
                        prior_code=None if serial_bar_read else prior_codes.get(sym),
                        bar_date=bar_date,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "target revision: %s could not be adjudicated (%s) — filed as unmeasured; the stored target stands",
                    sym,
                    exc,
                )
                try:
                    outcomes.append(
                        self._file_target_revision(
                            run_id=run_id,
                            symbol=sym,
                            seat=flag_seat,
                            evidence=evidence,
                            code="FAULT_POSITION_NOT_MEASURED",
                            applied=False,
                            detail=(
                                "this position could not be adjudicated this "
                                "session, so its stored target is unverified "
                                "rather than confirmed" + _serial_note
                            ),
                        )
                    )
                except Exception as exc2:  # noqa: BLE001
                    # The filing itself sat unguarded inside this handler,
                    # so a failure HERE unwound the remaining names after
                    # all — the sorted-tail truncation, one layer deeper.
                    logger.error(
                        "target revision: could not even file %s as unmeasured (%s); the sweep continues",
                        sym,
                        exc2,
                    )
        return outcomes
