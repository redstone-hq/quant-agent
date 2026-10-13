"""The alignment exit: the per-holding verdict, the scan over the book and the reading record (the per-run memo stays with the pipeline that owns it).

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


class AlignmentExit:
    """The alignment exit: the per-holding verdict, the scan over the book and the reading record (the per-run memo stays with the pipeline that owns it)."""

    def __init__(self, *, alignment_exit_cached, structural_protection_for_holding, config, db, market) -> None:
        self._alignment_exit_cached = alignment_exit_cached
        self._structural_protection_for_holding = structural_protection_for_holding
        self.config = config
        self.db = db
        self.market = market

    def _alignment_exit_scan(
        self,
        positions,
        best_by_symbol: dict,
        *,
        run_id: str,
        position_facts: dict | None,
        priority: dict,
        displaced: dict | None = None,
    ) -> None:
        """Read EVERY held position's own chart and raise a sale on the ones
        the chart says are finished — whether or not any model mentioned them.

        THE DEFECT THIS CLOSES. `check_alignment_exit` shipped wired only as
        a CONFIRMER: it ran solely on positions the review had already named,
        and only GATED the ones whose prose already claimed the alignment
        exit. Nothing ever asked the question of a position the models were
        silent about, so the owner-ratified "sell when the chart says the
        trend is over" rule could never START a sale, and the desk still had
        no sanctioned way to bank a gain on its own.

        HOW IT REACHES THE SELL PATH. It does not open one. A cleared verdict
        becomes an ordinary action item in `best_by_symbol` — the same dict
        the review's own actions land in, resolved by the same
        SELL/COVER > REDUCE > TRAIL_STOP > HOLD priority — so it is then
        subject, unchanged and in order, to every protection an LLM-proposed
        exit gets: the same-day-trim discipline, the spent-trigger layer, the
        named-trigger phrase gate, the exit guard's metric-contradiction
        veto, the AI Risk seat's veto, the qty-sign gate, and the alignment
        verdict itself re-read as the confirmer. A sale this scan raises can
        be refused by any one of them.

        ONE PROTECTION IS DELIBERATELY BYPASSED, and only one: the
        entry-anchored noise band, which asks how far price has travelled
        from WHAT THE DESK PAID. Under the owner's 2026-09-30 ruling the
        alignment exit sells because the move ended on the chart, and what
        the desk paid says nothing about that, so a chart-verified
        alignment sale is not judged against it. Every other layer applies
        unchanged. (Chosen over keeping the band for scan-raised sales
        because a band anchored to the entry would silently veto exactly
        the exits the ruling exists to allow — the ones taken at a gain.)

        WHEN A REFUSAL HAPPENS, THE DISPLACED ACTION COMES BACK. Raising a
        sale overwrites whatever the review proposed for that symbol; if
        that was a TRAIL_STOP and the sale is then refused downstream, the
        position would end the session neither sold nor re-protected —
        strictly worse than the state the scan found. The displaced item is
        therefore kept in `displaced` and re-queued by the executor when a
        scan-raised sale produces no order.

        A model action of EQUAL OR HIGHER priority always wins: the scan
        never overwrites a SELL, COVER or REDUCE the review asked for, and
        never rewrites its reason. It supersedes only HOLD and TRAIL_STOP,
        because under the owner's ruling the END OF A MOVE is decided by
        reading the chart rather than by whether a model mentioned it — and
        a stop adjustment on a position being closed is moot. The prose is
        not irrelevant: when a thesis names an average the desk computes,
        that average is the first mark. It is no longer REQUIRED — a thesis
        naming none falls back to the chart's own averages, so coverage no
        longer depends on model wording.

        A POSITION OPENED IN TODAY'S SESSION IS NOT ELIGIBLE. Nothing in the
        entry path requires a candidate to be above any average, so a name
        can be bought while already below one, and without this the scan
        could close it the same session on a chart the entry seats had
        already read. Date equality only, on the recorded buy.

        FAIL CLOSED. Only `status == "EXIT"` raises a sale. Missing bars, a
        missing ATR, an unresolvable chart mark and every other degraded
        state come back HOLD or UNPARSEABLE and raise nothing, exactly as
        today. Per-symbol failures are swallowed so one unreadable name
        cannot suppress the others — swallowing means NOT selling.

        No new number, no new threshold and no extra agreement requirement:
        what counts as the end of a move is entirely
        `check_alignment_exit`'s decision, read as given.
        """
        from src.risk.exit_trigger import ExitTrigger

        for position in positions or []:
            try:
                symbol = (getattr(position, "symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                try:
                    qty = float(getattr(position, "qty", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if qty == 0:
                    continue
                is_short = qty < 0
                # COVER is the only lever the exit path has on a held short
                # (the executor's qty-sign gate rejects a SELL on one).
                act = "COVER" if is_short else "SELL"
                existing = best_by_symbol.get(symbol)
                # ITEM 75 RECORDING, AND IT BUYS NOTHING IT DOES NOT ALREADY
                # HAVE. A chart read is a live `yfinance` download
                # (`market.get_ohlcv`, uncached), so reading the chart for a
                # position this scan would have skipped is NEW network work,
                # not free evidence — the record is therefore written from
                # what the scan ALREADY computes, and a position the scan
                # skips gets an explicit "not evaluated this session" row
                # with a NULL reading rather than a chart read bought to
                # fill it. A later reader needs the skipped rows to know its
                # own denominator; it must not mistake them for health.
                if existing is not None and priority.get(
                    existing.get("action"),
                    99,
                ) <= priority.get(act, 99):
                    self._record_alignment_reading(
                        symbol=symbol,
                        verdict=None,
                        run_id=run_id,
                        is_short=is_short,
                        not_evaluated_reason=(
                            "the review already proposed a "
                            f"{existing.get('action')} for this name, which "
                            "the scan does not override, so its chart was "
                            "not read this session"
                        ),
                    )
                    continue
                facts = (position_facts or {}).get(symbol, {}) or {}
                verdict = self._alignment_exit_cached(
                    symbol=symbol,
                    thesis_invalid_if=getattr(position, "thesis_invalid_if", None) or facts.get("thesis_invalid_if"),
                    is_short=is_short,
                    entry_price=getattr(position, "avg_entry", None),
                    stop_loss=getattr(position, "stop_loss", None) or facts.get("stop_loss"),
                    run_id=run_id,
                )
                # The reading the scan itself acts on, memoised, written
                # whether or not it fires — the sessions it does NOT fire are
                # the whole point. Nothing reads these rows back into any
                # decision; see `db.record_alignment_exit_reading`.
                self._record_alignment_reading(
                    symbol=symbol,
                    verdict=verdict,
                    run_id=run_id,
                    is_short=is_short,
                )
                if not verdict.exit_cleared:
                    continue
                if self._position_opened_today(symbol, is_short=is_short):
                    logger.info(
                        "Alignment scan: %s was opened in today's session — no same-session close is raised for it",
                        symbol,
                    )
                    continue
                if existing is not None and isinstance(displaced, dict):
                    displaced[symbol] = existing
                # The reason NAMES the trigger in the desk's own accepted
                # wording, and the structured trigger says the same thing, so
                # the confirmer downstream recognises the claim and appends
                # the chart's own owner-facing sentence
                # (`AlignmentExitCheck.owner_reason`) to it. The sentence is
                # not pasted here as well, or the owner would read it twice.
                best_by_symbol[symbol] = {
                    "symbol": symbol,
                    "action": act,
                    "reason": (
                        "Trend alignment over — raised by the desk's own scan of this position's chart, not by a model."
                    ),
                    "exit_trigger": ExitTrigger.TREND_ALIGNMENT_OVER.value,
                    "trigger_evidence": (verdict.reason or "")[:2000],
                    # Read by the executor: if this item produces no order,
                    # the action it displaced is put back on the queue.
                    "_alignment_scan_raised": True,
                }
                logger.info(
                    "Alignment scan: raising %s %s — %s",
                    act,
                    symbol,
                    verdict.reason,
                )
            except Exception as e:  # noqa: BLE001 — a failure here HOLDS
                logger.exception(
                    "Alignment scan: %s could not be evaluated (%s) — no sale is raised for it",
                    getattr(position, "symbol", "?"),
                    e,
                )
                # LOUD, IN THE DATA. A swallowed failure here used to leave
                # the session indistinguishable from one where the scan
                # never ran: the warning above is not stored anywhere a
                # reader of the readings table can see. The position now
                # gets an explicit not-evaluated row naming the failure, so
                # "the scan broke on this name" and "the scan never ran"
                # stop looking identical. The row is the same shape the
                # skip branch already writes — no chart read is bought.
                _failed_symbol = (getattr(position, "symbol", "") or "").strip().upper()
                if _failed_symbol:
                    try:
                        _failed_qty = float(getattr(position, "qty", 0) or 0)
                    except (TypeError, ValueError):
                        _failed_qty = 0.0
                    self._record_alignment_reading(
                        symbol=_failed_symbol,
                        verdict=None,
                        run_id=run_id,
                        is_short=_failed_qty < 0,
                        not_evaluated_reason=(
                            "the scan raised an error for this position, so "
                            f"its chart reading is unknown: {type(e).__name__}: {e}"
                        ),
                    )

    def _record_alignment_reading(
        self,
        *,
        symbol: str,
        verdict,
        run_id: str,
        is_short: bool,
        not_evaluated_reason: str | None = None,
    ) -> None:
        """Item 75 recording. Never raises, never blocks a sale, never
        buys a chart read to fill itself."""
        try:
            self.db.record_alignment_exit_reading(
                symbol=symbol,
                verdict=verdict,
                run_id=run_id,
                is_short=is_short,
                not_evaluated_reason=not_evaluated_reason,
            )
        except Exception as e:  # noqa: BLE001 — a recording never blocks
            # `exception`, not `warning`: this is the OTHER place an empty
            # readings table can come from, and a bare one-line warning left
            # nothing to tell the two apart. The traceback names which layer
            # refused the write.
            logger.exception(
                "alignment-exit reading for %s was NOT recorded (%s: %s) — "
                "the readings table will under-report this session",
                symbol,
                type(e).__name__,
                e,
            )

    def _target_for_holding(
        self,
        *,
        symbol: str,
        is_short: bool,
    ) -> tuple[float | None, str | None, str]:
        """The CURRENT target, the date it took effect and which record set it.

        The value is `trades.take_profit` on the position's opening row —
        the only column `update_open_take_profit` moves. The effective date
        is the position's own open timestamp, or the newest APPLIED target
        revision filed since that open (`db.get_target_revisions`), because
        a revision resets the question of whether the target was reached.
        Never raises: an unreadable target is (None, None, why), which the
        exit reads as today's rule and says so in its verdict.
        """
        opening = "SHORT" if is_short else "BUY"
        try:
            row = self.db.get_symbol_last_buy(symbol, action=opening) or {}
            target = row.get("take_profit")
            if target is None or not float(target) > 0:
                return None, None, "no target on the opening row"
            opened = self.db.get_position_open_timestamp(row) or row.get("timestamp") or ""
            opened = str(opened).replace("T", " ")[:19]
            if not opened:
                return None, None, "opening row carries no timestamp"
            effective, version = opened[:10], f"entry record of {opened}"
            revisions = (self.db.get_target_revisions([symbol]) or {}).get(
                symbol.upper(),
                [],
            )
            for rev in revisions:  # newest first
                stamp = str(rev.get("timestamp") or "").replace("T", " ")[:19]
                if not rev.get("applied") or rev.get("new_price") is None:
                    continue
                if stamp < opened:
                    break  # filed for an earlier round-trip of this name
                effective = stamp[:10]
                version = (
                    f"applied revision of {stamp} (run {rev.get('run_id')}, "
                    f"{rev.get('code')}, {rev.get('prior_price')} -> {rev.get('new_price')})"
                )
                if float(rev.get("new_price")) != float(target):
                    version += f"; the row now reads {float(target):.4f}"
                break
            return float(target), effective, version
        except Exception as e:  # noqa: BLE001 — unreadable target is today's rule
            logger.warning(
                "alignment exit: could not read %s's target (%s: %s) — the target vote is not applied this session",
                symbol,
                type(e).__name__,
                e,
            )
            return None, None, f"target read failed: {type(e).__name__}: {e}"

    def _position_open_date(self, symbol: str, *, is_short: bool) -> tuple[str | None, str]:
        """The date this POSITION opened — its first opening fill since flat — or (None, why).

        The same lookup `_target_for_holding` reads (`db.get_position_open_timestamp`
        on the latest opening row), WITHOUT its fallback to that row's own
        timestamp: the latest row may be an add, and an add is not when the
        position opened. Unknown is returned as None with the reason, never a
        guessed date. Never raises.
        """
        opening = "SHORT" if is_short else "BUY"
        try:
            row = self.db.get_symbol_last_buy(symbol, action=opening) or {}
            if not row:
                return None, f"no executed {opening} row on record for {symbol}"
            opened = self.db.get_position_open_timestamp(row)
        except Exception as e:  # noqa: BLE001 — unknown open is None, never a guess
            return None, f"position-open read failed: {type(e).__name__}: {e}"
        # Only a stored timestamp string counts; anything else is "unknown".
        stamp = (opened if isinstance(opened, str) else "").replace("T", " ")[:10]
        if not stamp:
            return None, f"the latest {opening} row carries no position id, so the position's open is unknown"
        return stamp, f"position opened {stamp}"

    def _position_opened_today(self, symbol: str, *, is_short: bool) -> bool:
        """Was this POSITION opened in TODAY's session? DATE EQUALITY ONLY.

        Reads the position's OPEN (first opening fill since flat), not the
        latest buy: an add today must not shield a weeks-old position from
        today's check, and a position opened today is not checked today.
        When the open is unknown the latest opening row's date stands in —
        it can only be on or after the open, so "not today" stays true and
        "today" holds rather than sells.

        No recorded opening row at all means the position predates the desk's
        own record (or was opened outside it), which cannot be evidence that
        it was opened today, so it stays eligible. A FAILED read is different:
        the age is unknown, and an unknown age holds rather than sells, which
        is the same fail-closed posture the rest of this path takes.
        """
        opening = "SHORT" if is_short else "BUY"
        try:
            row = self.db.get_symbol_last_buy(symbol, action=opening) or {}
        except Exception as e:  # noqa: BLE001 — unknown age HOLDS
            logger.warning(
                "alignment scan: could not read %s's entry date (%s) — it is "
                "treated as opened today, so no sale is raised",
                symbol,
                e,
            )
            return True
        if not row:
            return False
        try:
            opened = self.db.get_position_open_timestamp(row)
        except Exception:  # noqa: BLE001 — the latest row's date stands in
            opened = None
        if not isinstance(opened, str):  # only a stored timestamp string counts
            opened = None
        ts = str(opened or row.get("timestamp") or "").replace("T", " ")[:10]
        if not ts:
            return False
        return ts == str(et_today())

    def _alignment_exit_for_holding(
        self,
        *,
        symbol: str,
        thesis_invalid_if: str | None,
        is_short: bool,
        entry_price: float | None,
        stop_loss: float | None,
        run_id: str,
    ):
        """Read this holding's own chart and return the alignment verdict.

        Supplies `src.risk.alignment_exit.check_alignment_exit` with real
        numbers off the SAME deterministic, no-LLM machinery
        `_structural_protection_for_holding` uses (`compute_indicators` for
        ATR, `find_structural_levels` for levels), on the same
        `config.trading.lookback_days` window, and on the latest COMPLETED
        daily closes — never a live quote.

        The structural mark is admitted ONLY when the ratified structural
        check has already returned `structural_level_broken`, i.e. its
        cross-day confirmation gate passed. This method neither re-derives
        nor shortcuts that gate: it reads the verdict (`persist=False`, so
        consulting it here can never file a break and let a future
        confirmation land a day early) and takes the level THAT CHECK
        NAMED as broken (`StructuralProtectionCheck.broken_level`); no
        level is ever chosen by nearness to the close. Never raises; a
        failure degrades to UNPARSEABLE, which callers treat as HOLD.
        """
        from src.risk.alignment_exit import (
            CODE_NO_CLOSES,
            AlignmentExitCheck,
            check_alignment_exit,
        )

        try:
            bars = self.market.get_ohlcv(symbol, self.config.trading.lookback_days) or []
            sorted_bars = sorted(bars, key=lambda b: b.date)
            closes = [float(b.close) for b in sorted_bars]
            atr = None
            broken_level = None
            # The target is read ONLY when there are closes to judge it
            # against: a book with no bars gets UNPARSEABLE before the vote
            # is asked, and a hold-only review must buy no entry-context
            # reads (`tests/test_holding_discipline_intraday.py`).
            target, effective, version = None, None, "no completed closes to date a target"
            # THE POSITION'S OPEN, not the latest add: a lower low only ends
            # this position's trend if it printed while the position was on.
            opened_on = None
            if sorted_bars:
                from src.data.technical import compute_indicators

                target, effective, version = self._target_for_holding(
                    symbol=symbol,
                    is_short=is_short,
                )
                opened_on, opened_why = self._position_open_date(symbol, is_short=is_short)
                if opened_on is None:
                    logger.warning(
                        "alignment exit: %s — the lower-%s test is not evaluated this session",
                        opened_why,
                        "high" if is_short else "low",
                    )
                atr = compute_indicators(symbol, bars).atr_14
                protection = self._structural_protection_for_holding(
                    symbol=symbol,
                    thesis_invalid_if=thesis_invalid_if,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    is_short=is_short,
                    run_id=run_id,
                    persist=False,
                )
                if getattr(protection, "basis", "") == "structural_level_broken":
                    # THE LEVEL THAT ACTUALLY BROKE, as named by the check
                    # that confirmed it. An earlier draft instead pooled
                    # every support AND resistance and took the nearest
                    # price on the far side of the close — which could
                    # admit an overhead resistance that never broke as
                    # "the confirmed-broken structural level". Nothing is
                    # re-derived and nothing is guessed by proximity: when
                    # the check does not name a level there is no
                    # structural mark.
                    broken_level = getattr(protection, "broken_level", None)
            return check_alignment_exit(
                thesis_invalid_if=thesis_invalid_if,
                closes=closes,
                atr=atr,
                broken_structural_level=broken_level,
                is_short=is_short,
                target=target,
                target_effective_date=effective,
                target_version=version,
                bar_dates=[getattr(b, "date", None) for b in sorted_bars],
                bars=sorted_bars,
                position_open_date=opened_on,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "alignment exit: chart read failed for %s (%s) — the verdict "
                "is UNPARSEABLE, which callers treat as HOLD",
                symbol,
                e,
            )
            return AlignmentExitCheck(
                "UNPARSEABLE",
                CODE_NO_CLOSES,
                (),
                None,
                None,
                None,
                f"chart read failed: {e}",
            )
