"""The ex-dividend stop shift and the stop-coverage repair, lifted verbatim out of
`src/pipeline_protection.py` (2026-10-09, ceiling split). Behaviour is unchanged.

ONE line of `ExDividends._handle_ex_dividends` changed: `et_today()` became
`self._today()`, a keyword-only collaborator. Tests patch `src.pipeline_protection.et_today`,
and importing that module back from here would be an import cycle, so the builder there
hands in a callable that reads its own `et_today` AT CALL TIME: every such patch still
reaches this code. Built without one, the part reads the trading-calendar clock, which is
what that name resolves to when nothing patches it.
"""

import logging

from src.pipeline_protection_record import record_protection_fault
from src.trading_calendar import et_today

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class CoverageRepair:
    """The blind stop-coverage repair and the kill-switch block recorder; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        broker=None,
        db=None,
        alert_owner_kill_switch_blocked=None,
    ) -> None:
        self.broker = broker
        self.db = db
        if alert_owner_kill_switch_blocked is not None:
            self._alert_owner_kill_switch_blocked = alert_owner_kill_switch_blocked  # else: this part's own body

    def _repair_stop_coverage(
        self,
        symbol: str,
        uncovered_qty: float,
        *,
        is_short: bool,
        outcome: dict | None = None,
        resting_stops: list | None = None,
    ) -> bool:
        """Best-effort: re-place protective stop coverage on an uncovered
        position using the stop level recorded on its last opening row
        (BUY for a long, SHORT for a short). Returns True when the gap
        was actually closed.

        `outcome`, when given, is the caller's gap dict: a refusal stamps
        `repair_refusal` on it with a plain sentence saying WHY nothing was
        placed (docs/WORK.md item 88). Before that, the caller received a
        bare False and the owner alert could only say the repair "could not
        restore one" — a corrupt recorded stop, a level already through the
        tape and three exhausted broker retries all read identically.

        THE BODY MOVED to `src.execution.stop_repair.repair_stop_coverage`
        and this is now a delegate — see that module for the whole design,
        including why the recorded opening level is not a policy invention and
        why the fractional split needs no special case here. It moved
        because `src/coverage_watchdog.py` needs the SAME re-placement when
        it finds an uncovered sub-share remainder while the trading timers
        are stopped (docs/INCIDENT_HISTORY.md, item 53), and a second copy
        of an order-placement path is how one behaviour ends up with two
        homes. The caller still owns the decision of WHETHER to call this at
        the current hour.
        """
        from src.execution.stop_repair import repair_stop_coverage
        from src.protection.breached_stop_closer import build_breached_stop_closer
        from src.protection.fill_reconciler import FillReconciler
        from src.protection.owner_alerts import OwnerAlerts

        opening = "SHORT" if is_short else "BUY"
        # A stop the open already gapped through is executed through the
        # protected sell path; the execution layer cannot import it, so it is
        # injected from here (src/protection/breached_stop_closer.py).
        close_breached = build_breached_stop_closer(
            broker=self.broker,
            db=self.db,
            order_accepted=FillReconciler._order_accepted,
            alert_exit_declined=OwnerAlerts._alert_owner_exit_declined,
        )
        return repair_stop_coverage(
            broker=self.broker,
            # include_in_flight: a same-session open still at fill_status=
            # 'submitted' is the row whose stop we want — under the strict
            # executed predicate the repair either no-op'd or read a months-
            # old prior row's stop level (audit round 2).
            last_buy=lambda sym, action=opening: self.db.get_symbol_last_buy(
                sym,
                include_in_flight=True,
                action=action,
            ),
            symbol=symbol,
            uncovered_qty=uncovered_qty,
            is_short=is_short,
            db=self.db,
            outcome=outcome,
            resting_stops=resting_stops,
            caller="session_coverage_reconcile",
            close_breached=close_breached,
        )

    def _wire_protective_stop_block_recorder(self) -> None:
        """The broker holds no database, so a protective stop its kill
        switch refuses is recorded through this pipeline's one
        (`kind='protective_stop_blocked'`, `src/execution/exit_path_records.py`).
        Also pages the owner (`_alert_owner_kill_switch_blocked`) — a
        recorded row nobody reads is not an alert, and until this was
        wired a kill-switch refusal left the position naked with no owner
        notice at all. `self.db` is read at call time, not captured, so a
        later swap of the handle is honoured."""
        from src.execution.exit_path_records import record_protective_stop_blocked

        def _on_blocked(**facts) -> None:
            record_protective_stop_blocked(self.db, **facts)
            self._alert_owner_kill_switch_blocked(**facts)

        self.broker.protective_stop_block_recorder = _on_blocked

    @staticmethod
    def _alert_owner_kill_switch_blocked(
        *,
        symbol: str,
        qty: float = 0.0,
        stop_price: float = 0.0,
        side: str = "",
        kill_switch_path: str = "",
        **_ignored,
    ) -> None:
        """Page the owner the first time today the desk's own kill switch
        blocks a protective stop for `symbol`. Never raises — see
        `AlpacaBroker._submit_stop_limit_order`, which already swallows
        whatever this callback does.

        Deduped per symbol per trading day (`claim_kill_switch_block_alert`)
        the same way the repair-failure and elected-unfilled alerts are: a
        kill switch left on all session would otherwise page once per
        retry of every symbol it touches.
        """
        try:
            from src import notifier as _notifier
            from src.coverage_watchdog import claim_kill_switch_block_alert
            from src.execution.exit_path_records import kill_switch_blocked_text

            fresh = claim_kill_switch_block_alert([symbol])
            if not fresh:
                return
            _notifier.send_owner_alert(
                "🔴 KILL SWITCH BLOCKED A PROTECTIVE STOP\n"
                f"{kill_switch_blocked_text(symbol)}\n"
                f"qty={qty} side={side} stop=${stop_price}\n"
                "Nothing was sent to the broker, so nothing is standing "
                "watch over this position right now. Turn off the kill "
                "switch and place the stop by hand, or flatten the "
                "position. Reported at most once per trading day.",
                symbols=fresh,
            )
        except Exception as exc:  # noqa: BLE001
            record_protection_fault(None, "coverage_repair.kill_switch_alert", exc, symbol=symbol)
            logger.error(
                "kill-switch-block owner alert failed for %s: %s",
                symbol,
                exc,
            )


class ExDividends:
    """The ex-dividend stop shift; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        broker=None,
        db=None,
        market=None,
        repair_stop_coverage=None,
        today=None,
    ) -> None:
        self.broker = broker
        self.db = db
        self.market = market
        self._repair_stop_coverage = repair_stop_coverage
        self._today = today if today is not None else et_today  # the exchange-day clock

    def _handle_ex_dividends(self, positions, run_id: str) -> list[dict]:
        """Lower stops by the upcoming dividend amount the day before ex-div.

        On ex-div day, the stock's open drops by approximately the dividend
        per share — a mechanical move, not a thesis break. A tight stop set
        against normal price action can trigger for no real reason and kick
        us out of a winner. This runs at midday the day BEFORE ex-div and
        lowers each relevant position's stop by the dividend amount so the
        mechanical gap doesn't touch it.

        Idempotent per ET date: if we already adjusted this symbol today
        (tagged 'ex-div' in reasoning), skip. Detects "tomorrow is ex-div"
        in ET.
        """
        from datetime import timedelta as _td

        orders: list[dict] = []
        today = self._today()
        # NEXT TRADING day, not calendar tomorrow (2026-07-16 audit): sessions
        # only run Mon-Fri, so `today + 1 day` can never BE a Monday — every
        # Monday ex-div silently went unadjusted, and Friday's sessions (the
        # last chance to act) computed Saturday. Same hole for any ex-div the
        # day after a holiday. Fall back to calendar+1 if the calendar lookup
        # fails — degrading to today's behavior beats crashing the session.
        next_trading_day = today + _td(days=1)
        for _ in range(7):
            try:
                if self.broker.is_trading_day(next_trading_day):
                    break
            except Exception as e:  # noqa: BLE001
                record_protection_fault(self, "exdiv.is_trading_day", e)
                logger.warning("ex-div: is_trading_day failed (%s) — falling back to calendar+1", e)
                next_trading_day = today + _td(days=1)
                break
            next_trading_day += _td(days=1)

        for p in positions:
            # Deliberately long-only, not just "not yet generalised" — a
            # short OWES the dividend to the share lender (a cash liability)
            # rather than receiving it, so there is no mechanical gap-down
            # here for a stop-shift to absorb. See broker.shift_stops_down's
            # docstring for the fuller reasoning (shorts-safe, Stage 2).
            if p.qty <= 0:
                continue
            # Check today's trades for a prior ex-div adjustment — idempotent
            try:
                today_trades = self.db.get_trades(
                    symbol=p.symbol,
                    today_only=True,
                    limit=20,
                )
            except Exception as e:
                record_protection_fault(self, "exdiv.trades_lookup", e, symbol=p.symbol)
                logger.warning("ex-div: today trades lookup failed for %s: %s", p.symbol, e)
                continue
            already = any(
                (t.get("action") or "").upper() == "TRAIL_STOP" and "ex-div" in (t.get("reasoning") or "").lower()
                for t in today_trades
            )
            if already:
                continue

            try:
                div = self.market.get_upcoming_ex_dividend(p.symbol)
            except Exception as e:
                record_protection_fault(self, "exdiv.fetch", e, symbol=p.symbol)
                logger.warning("ex-div: fetch failed for %s: %s", p.symbol, e)
                continue
            if not div:
                continue
            div_date = div.get("date")
            if not (div_date and today < div_date <= next_trading_day):
                # Only act on the session BEFORE ex-div. On ex-div day itself
                # the gap has already happened at open — adjustment is too
                # late — and "day after" is wrong (the stock is re-pricing
                # back to normal vol). The window is (today, next_trading_day]
                # so a Monday ex-div is caught by Friday's sessions.
                continue
            amount = div.get("amount") or 0
            if amount <= 0:
                continue

            from src.execution.stop_read import read_stop, repair_for
            from src.execution.exit_path_records import (
                record_shift_outcome,
                record_stop_shift_legs,
            )
            from src.execution.stop_records import record_unprotected_windows

            stop_read = read_stop(
                self.broker,
                p.symbol,
                db=self.db,
                run_id=run_id,
                context="ex-div shift",
                establish=repair_for(self._repair_stop_coverage, p),
            )
            if stop_read.unreadable or stop_read.absent:
                continue  # unreadable was recorded+alerted; absent = nothing to adjust
            current_stop = stop_read.price
            new_stop = round(current_stop - amount, 2)
            if new_stop <= 0 or new_stop >= p.current_price:
                logger.warning(
                    "ex-div: %s skipped — new_stop $%.2f not protective vs current $%.2f",
                    p.symbol,
                    new_stop,
                    p.current_price,
                )
                continue
            try:
                # Shift EVERY stop down by the dividend, preserving per-lot
                # levels/qty (audit round 2: with per-BUY GTC stops a
                # consolidating replace could TIGHTEN a wide lot's stop to
                # the tightest lot's level minus the dividend).
                order = self.broker.shift_stops_down(p.symbol, amount)
            except Exception as e:
                record_protection_fault(self, "exdiv.stop_shift", e, symbol=p.symbol)
                logger.error("ex-div: stop shift failed for %s: %s", p.symbol, e)
                continue
            finally:
                record_unprotected_windows(self.broker, self.db, p.symbol, run_id=run_id, caller="exdiv_shift")
            from src.execution.stop_records import accepted_stop_order, write_back_stop_loss

            if isinstance(order, dict):
                record_shift_outcome(
                    self.db,
                    p.symbol,
                    amount,
                    order,
                    run_id,
                    lambda stage, exc: record_protection_fault(self, stage, exc, symbol=p.symbol),
                    record_stop_shift_legs,
                )
            if not order or (isinstance(order, dict) and not accepted_stop_order(order)):
                # A partial, a refusal or an unknown carries no order id, so no
                # stop level is written back and no TRAIL_STOP row is filed —
                # the desk must not record a stop it did not confirm moving.
                continue
            try:
                write_back_stop_loss(self.db, p.symbol, new_stop, is_short=False)
            except Exception as e:  # noqa: BLE001
                record_protection_fault(self, "exdiv.write_back", e, symbol=p.symbol)
                logger.warning("ex-div: stop write-back failed for %s: %s", p.symbol, e)
            try:
                self.db.insert_trade(
                    symbol=p.symbol,
                    action="TRAIL_STOP",
                    qty=p.qty,
                    price=new_stop,
                    reasoning=(
                        f"ex-div adjustment: ex-div {div['date']}, div ${amount:.4f}/share. "
                        f"Shifted {order.get('shifted', '?')} stop(s) down by the dividend "
                        f"(highest {current_stop:.2f} → {new_stop:.2f}) to absorb the "
                        f"mechanical open gap."
                    ),
                    run_id=run_id,
                    stop_loss=new_stop,
                    broker_order_id=order.get("id"),
                    fill_status="submitted",
                )
            except Exception as e:
                record_protection_fault(self, "exdiv.audit_log", e, symbol=p.symbol)
                logger.warning("ex-div: audit log failed for %s: %s", p.symbol, e)
            if isinstance(order, dict):
                order.setdefault("action", "TRAIL_STOP")  # audit F5
            orders.append(order)
            logger.info(
                "Ex-div adjust: %s ex-div %s div $%.4f → stop $%.2f → $%.2f",
                p.symbol,
                div["date"],
                amount,
                current_stop,
                new_stop,
            )
        return orders
