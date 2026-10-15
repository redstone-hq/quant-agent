"""Protective stops and broker reconciliation: everything that places, cancels,
restores or reconciles a protective stop or a sell against the broker.

Step 2 of `docs/PIPELINE_SPLIT_PLAN.md` (board item 210). Moved verbatim out of
`src/pipeline.py` as a mixin, so `TradingPipeline` keeps every one of these as
its own attribute and every test that patches or calls them is untouched.

This is the live-money code: the stop-coverage reconciler, the repair path, the
protected sell, the write-ahead cancel/restore legs, the repeg and restore
drains, the residual re-protection after a partial sell, the fill/stop-out
reconcilers, and `_handle_ex_dividends` -- which the plan REASSIGNED here out of
step 1's prompt-facts cluster because it shifts live stops down by the dividend
(plan S1 correction, 2026-10-01). A stop-moving method does not belong in a
module advertised as read-only.

The module-level helpers travel with it: `_WAL_SELL_SENTINEL`,
`_market_is_open_now`, `_price_is_through_stop`, `_position_notional`,
`_classify_coverage_gap`, `_reconciled_exit_action` and the broker-fill float
coercion `_finite_float_or_none`. All are re-exported from `src.pipeline` so
`from src.pipeline import ...` keeps working -- but a test that PATCHES one of
them on `src.pipeline` no longer reaches this module's code and must patch it
here instead (plan S5, silent-behaviour risk 1).

Nothing here may import `src.pipeline`: this module is one of its bases.

Five pieces now live as standalone classes under `src/protection/` (owner alerts,
sell finalisation, fill reconciliation, the repeg drain, coverage election),
each built from explicit keyword-only collaborators; the mixin keeps a thin
same-named shim per method, so callers and patch targets are unchanged. The
helpers `_price_is_through_stop`, `_position_notional`, `_finite_float_or_none`,
`_reconciled_exit_action` and `_WAL_SELL_SENTINEL` moved with them and are
imported back here, so `src.pipeline_protection.<name>` still resolves. What
stayed: everything that imports the broker seam (`src.execution`, a frozen
importer list), everything reading `_market_is_open_now`/`et_today` that tests
patch on this module, and the protected sell with its write-ahead cancel, which
hands `_last_stop_clear_refusal` between two methods through the pipeline.

2026-10-04: those followed too. `ProtectedSell` and `ReprotectRecords` live under
`src/protection/`. `CoverageRepair`, `ExitRelief`, `RestoreDrain` and `ExDividends`
import the broker seam (`src.execution`, a frozen importer list) so they stay HERE
as standalone classes built the same way, as does `ReprotectResidual` (over the
400-line ceiling for a new file). Every builder reads the host LIVE per call; host
attributes a body assigns or reads with a default go through `_HostState`, never a
copy. `_reconcile_stop_coverage` keeps its body on the mixin under its original
identity until PR 1223 (which uncrams one of its lines) lands; the statement-cram
ratchet keys by class.method and would read the move as a new crammed line.

2026-10-09: `CoverageRepair`, `RestoreDrain`, `ExDividends` and `ReprotectResidual` moved
verbatim to `src/protection_parts/` (that residual method's existing-stops loop to
`reprotect_scan.py`) to bring this file under its line ceiling; their lazy broker-seam
imports moved with them. The builders and `ProtectionMixin` stay here and import them back,
so `src.pipeline_protection.<Class>` still resolves. `ExDividends` now takes its clock as a
`today` collaborator, which `_build_ex_dividends` reads from this module at call time.
"""

import json as _json
import logging
import math

from src.protection.protected_sell import ProtectedSell
from src.protection.reprotect_records import ReprotectRecords
from src.protection.coverage_book_read import read_positions_with_retry
from src.protection.coverage_book_read import unverified_book_sweep
from src.pipeline_protection_record import record_protection_fault
from src.sentinel.reconciliation import record_guarded_outcome, record_reconciliation
from src.execution.broker import AlpacaBroker, _split_protective_qty
from src.models import TradeDecision
from src.pipeline_context import RunContext
from src.storage.db import Database
from src.trading_calendar import et_now, et_today
from src.protection.coverage_election import _position_notional, _price_is_through_stop  # noqa: F401
from src.protection.fill_reconciler import _finite_float_or_none, _reconciled_exit_action  # noqa: F401
from src.protection.sell_finalization import _WAL_SELL_SENTINEL  # noqa: F401
from src.protection.collaborator_builders import (
    _build_coverage_election,
    _build_fill_reconciler,
    _build_owner_alerts,
    _build_repeg_drain,
    _build_sell_finalization,
    _collab_of,
)
from src.execution.stop_repair import drain_owed_stop_levels
from src.protection_parts.ex_dividends_repair import CoverageRepair, ExDividends
from src.protection_parts.reprotect_residual import ReprotectResidual
from src.protection_parts.restore_drain import RestoreDrain

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


def _market_is_open_now(broker) -> bool:
    """Is the regular cash session open RIGHT NOW?

    Spec §11.1 hybrid fractional stops. This is the discriminator the
    whole alerting distinction rests on: a fractional DAY stop that is
    absent while the market is SHUT is the design working — it lapsed at
    16:00 ET exactly as intended and the next session re-places it. The
    same stop absent while the market is OPEN is a placement failure and
    must wake somebody.

    Delegates to `src.market_session.market_open_verdict` — the single answer
    shared with the coverage watchdog. FAILS TOWARD "OPEN" ON PURPOSE. Every way this can be wrong has an
    asymmetric cost: believing the market is shut when it is open would
    SUPPRESS a real naked-position alert, which is the one failure this
    desk cannot absorb. Believing it is open when it is shut costs a
    redundant banner. So anything unknown, unreadable or unexpected
    answers True, and only a confident, positively-established "outside
    the session" answers False.

    The session-window table (`trading_calendar.SESSION_WINDOWS`) is the
    weekday 09:30-16:00 ET baseline; `broker.get_session_close()`
    tightens it on early-close days (Thanksgiving Friday 13:00, July 3),
    and is best-effort — a calendar failure leaves the baseline answer
    rather than inventing a closed market.

    Note the callers all sit behind `_is_trading_day()`, so a holiday
    never reaches here; the weekday check is belt-and-braces for a
    direct call.
    """
    from src.market_session import market_open_now

    return market_open_now(broker, et_now)


FRACTIONAL_REMAINDER_CLEANUP = "fractional_remainder_cleanup"


def _sell_fractional_remainder(host, p, symbol: str, held: float) -> bool:
    """Owner ruling 2026-10-15, whole shares only: sell a long's sub-share remainder.

    A remainder can only be stopped by a DAY order, so it has no stop
    overnight. Runs from the coverage sweep, per position, and only when
    fractional sizing is explicitly OFF, the session is POSITIVELY open (the
    sweep's own verdict fails toward open, which is right for a stop and
    wrong for a sale), and no non-stop order is working on the symbol — an
    open exit or an earlier cleanup still in flight. Sold as a trim ("REDUCE")
    through the protected sell, so only the DAY sliver stop is cancelled and
    the whole-share GTC stop is kept and finalized to the whole-share qty.
    Decided from the broker's held qty every pass, so it is idempotent.
    Returns True when a sale was sent (the caller skips this pass's audit of
    the symbol; the next pass audits the re-sized book).
    """
    from decimal import Decimal

    from src.market_session import market_open_verdict
    from src.trading_calendar import in_regular_session

    execution_cfg = getattr(getattr(host, "config", None), "execution", None)
    if getattr(execution_cfg, "fractional_enabled", None) is not False:
        return False
    try:
        frac = float(Decimal(repr(float(held))) % 1)
    except (ArithmeticError, TypeError, ValueError):
        return False
    if frac <= 1e-6:
        return False
    try:
        now = et_now()
        if not (in_regular_session(now) and market_open_verdict(host.broker, now)[0]):
            return False
        ok, orders = host.broker.list_open_orders_checked()
    except Exception as exc:  # noqa: BLE001
        record_protection_fault(host, "coverage.fraction_cleanup_read", exc, symbol=symbol)
        return False
    if ok is not True:
        return False
    def _sym(value) -> str:
        return str(value).strip().upper().replace("/", "")

    def _is_stop(order) -> bool:
        kind = getattr(order, "order_type", None) or getattr(order, "type", "")
        return "stop" in str(getattr(kind, "value", kind)).lower()

    working = [o for o in orders or [] if _sym(getattr(o, "symbol", "")) == _sym(symbol)]
    if any(not _is_stop(o) for o in working):
        logger.info("fractional remainder %s %s left: an order is already working on it", symbol, frac)
        return False
    if symbol in {v.get("symbol") for v in (getattr(host, "_unsettled_exit_orders", None) or {}).values()}:
        return False
    run_id = f"coverage-sweep-{et_today().isoformat()}"
    price = _finite_float_or_none(getattr(p, "current_price", None)) or 0.0

    def book(order, qty):
        host.db.insert_trade(
            symbol=symbol,
            action="REDUCE",
            qty=qty,
            price=price,
            reasoning=FRACTIONAL_REMAINDER_CLEANUP,
            run_id=run_id,
            broker_order_id=(order or {}).get("id"),
            fill_status="submitted",
        )

    sale = host._submit_protected_sell(
        symbol=symbol,
        qty=frac,
        limit_price=None,
        reference_price=price,
        position_qty_before_sell=held,
        label="REDUCE",
        side="sell",
        book=book,
    )
    host._record_exit_refusal(
        symbol=symbol,
        run_id=run_id,
        action="REDUCE",
        code=FRACTIONAL_REMAINDER_CLEANUP,
        dropped=sale is None,
        detail=f"sell the {frac} sub-share remainder of {held}: whole shares only (owner ruling 2026-10-15)",
        layer="coverage_sweep",
    )
    if sale is None:
        return False
    order, prot = sale
    book(order, frac)
    if prot is not None:
        host._finalize_pending_protections([prot], context="Fractional remainder cleanup")
    return True


def _classify_coverage_gap(*, held: float, covered: float) -> tuple[str, float]:
    """Name the shortfall between held shares and stop-covered shares.

    Returns ``(coverage, frac_uncovered)`` where `coverage` is one of:

    ``'none'``       zero protective coverage on a position that should
                     have some. Guard 3's worst condition; escalates.
    ``'partial'``    some coverage, but the WHOLE-SHARE part of the
                     position is under-covered. Guard 3's milder
                     condition; banner, not escalation.
    ``'fractional'`` the ONLY thing missing is the sub-share remainder —
                     the durable GTC leg over floor(held) is intact.

    Spec §11.1 hybrid fractional stops. The third value is the whole
    point: under the hybrid design a sub-share remainder loses its DAY
    stop at every close, so classifying that as 'none' (which is what a
    bare `covered <= 0` test does for a position under one share) would
    fire the NO-STOP-AT-ALL owner alert every single night on a state
    that is expected, bounded and deliberate. An alert that cries wolf
    nightly is worse than no alert, because it trains the owner to swipe
    away the one message that must never be ignored.

    Market hours are deliberately NOT an input here. This answers only
    "what is missing"; the caller decides what that means at this hour.
    Keeping the two apart is what makes the overnight suppression
    auditable — it can only ever soften a gap already known to be
    'fractional', and it is one branch at one call site rather than a
    condition smeared through the classifier.
    """
    whole_held, frac_held = _split_protective_qty(held)
    shortfall = max(0.0, held - covered)
    # The durable leg is intact iff the covered qty reaches the whole-share
    # floor of the position. Anything less means a GTC stop is missing,
    # which is never the expected overnight state.
    durable_leg_intact = covered + 1e-6 >= whole_held
    only_sub_share_missing = shortfall <= frac_held + 1e-6
    if frac_held > 0 and durable_leg_intact and only_sub_share_missing:
        return "fractional", shortfall
    return ("none" if covered <= 1e-6 else "partial"), 0.0


class _HostState:
    """Live get/set view of the host attributes a part reads with a default or assigns (never a copy)."""

    def __init__(self, host) -> None:
        self._host = host

    def get(self, name: str):
        return getattr(self._host, name)

    def set(self, name: str, value) -> None:
        setattr(self._host, name, value)


from src.pipeline_protection_exit_relief import ExitRelief  # noqa: E402,F401


def _build_coverage_repair(host):
    """Builds the standalone CoverageRepair from the host pipeline's collaborators, read LIVE at each call."""
    return CoverageRepair(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        alert_owner_kill_switch_blocked=_collab_of(host, "_alert_owner_kill_switch_blocked"),
    )


def _build_protected_sell(host):
    """Builds the standalone ProtectedSell from the host pipeline's collaborators, read LIVE at each call."""
    return ProtectedSell(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        alert_owner_exit_declined=_collab_of(host, "_alert_owner_exit_declined"),
        order_accepted=_collab_of(host, "_order_accepted"),
        write_ahead_protection_restore=_collab_of(host, "_write_ahead_protection_restore"),
        cancel_stops_with_write_ahead=_collab_of(host, "_cancel_stops_with_write_ahead"),
        state=_HostState(host),
        trim_book=AlpacaBroker,  # this module may import the broker seam; src.protection may not
    )


def _build_exit_relief(host):
    """Builds the standalone ExitRelief from the host pipeline's collaborators, read LIVE at each call."""
    return ExitRelief(
        broker=_collab_of(host, "broker"),
        state=_HostState(host),
        terminal_states=AlpacaBroker._ORDER_TERMINAL_STATES,
    )


def _build_restore_drain(host):
    """Builds the standalone RestoreDrain from the host pipeline's collaborators, read LIVE at each call."""
    return RestoreDrain(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        terminal_order_statuses=_collab_of(host, "_TERMINAL_ORDER_STATUSES"),
        finalize_protection_after_sell=_collab_of(host, "_finalize_protection_after_sell"),
        resolve_wal_row_side=_collab_of(host, "_resolve_wal_row_side"),
        restore_after_unconfirmed_sell=_collab_of(host, "_restore_after_unconfirmed_sell"),
    )


def _build_reprotect_records(host):
    """Builds the standalone ReprotectRecords from the host pipeline's collaborators, read LIVE at each call."""
    return ReprotectRecords(
        record_exit_refusal=_collab_of(host, "_record_exit_refusal"),
    )


def _build_ex_dividends(host):
    """Builds the standalone ExDividends from the host pipeline's collaborators, read LIVE at each call."""
    return ExDividends(
        broker=_collab_of(host, "broker"),
        db=_collab_of(host, "db"),
        market=_collab_of(host, "market"),
        repair_stop_coverage=_collab_of(host, "_repair_stop_coverage"),
        # Read THIS module's `et_today` at each call, so a patch on src.pipeline_protection.et_today bites.
        today=lambda: et_today(),
    )


def _build_reprotect_residual(host):
    """Builds the standalone ReprotectResidual from the host pipeline's collaborators, read LIVE at each call."""
    return ReprotectResidual(
        broker=_collab_of(host, "broker"),
        format_qty=_collab_of(host, "_format_qty"),
        alert_owner_reprotect_left_naked=_collab_of(host, "_alert_owner_reprotect_left_naked"),
        alert_owner_stop_pending_acceptance=_collab_of(host, "_alert_owner_stop_pending_acceptance"),
        alert_owner_unreadable_stop=_collab_of(host, "_alert_owner_unreadable_stop"),
        record_reprotect_identity_gap=_collab_of(host, "_record_reprotect_identity_gap"),
        state=_HostState(host),
    )


class ProtectionMixin:
    """See the module docstring. Methods are the moved text, byte-for-byte."""

    # The broker-side trim methods (src/execution/broker_parts/trim_book.py),
    # injected into ProtectedSell / SellFinalization so src.protection never
    # imports src.execution; called unbound on whatever broker the host holds.
    _TRIM_BOOK = AlpacaBroker

    # Statuses Alpaca uses for terminal/non-terminal orders. Kept as a
    # class attribute so tests can introspect the exact set the
    # finalizer treats as "done".
    _TERMINAL_ORDER_STATUSES = {
        "filled",
        "canceled",
        "cancelled",
        "expired",
        "rejected",
        "done_for_day",
        "replaced",
    }

    def _current_position_qty_for_finalize(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._current_position_qty_for_finalize(_build_sell_finalization(self), *args, **kwargs)

    def _reconcile_stop_coverage(self) -> list[dict]:
        """Broker-truth stop-coverage audit, independent of the WAL queue.

        At session entry, enumerate every held position — long or short —
        and compare its held qty against the qty actually covered by its
        open protective stops at the broker (SELL-stops for a long,
        BUY-stops for a short). Flag (log + return) any position whose
        covered qty is materially below its held qty.

        Why this exists (design review's strongest finding): the whole
        naked-protection guarantee otherwise rests on some code path having
        successfully persisted a WAL recovery row. A position that goes naked
        WITHOUT a row — a best-effort persist that silently failed, a manual
        broker action, a future SELL path that skips a step — is never
        re-detected, because the WAL is a log of INTENDED operations, not an
        audit of ACTUAL broker coverage. This reconciler closes that gap by
        reading broker truth directly.

        Auto-repairing for longs AND shorts (see `_repair_stop_coverage` —
        it reconstructs the stop from the recorded opening row: BUY for a
        long, SHORT for a short). Inventing a level is still refused when
        that row has none; the SHORT row stores `stop_loss` the same way
        BUY does, so the old "no recorded entry for a short" objection is
        false.

        Symbols already queued for WAL recovery are skipped — the drain owns
        them. Returns the list of under-covered ``{symbol, held_qty,
        covered_qty, coverage, repaired}`` for the caller to surface to the
        operator.

        SPEC §11.1 HYBRID FRACTIONAL STOPS — this sweep is also the
        re-placement mechanism, and the alerting distinction lives here.

        A fractional position is covered by two orders: a durable GTC stop
        over floor(qty) and a DAY stop over the sub-share remainder, which
        the broker expires at 16:00 ET by design. That means "held qty
        exceeds covered qty" is now THREE different situations, not one, and
        reporting them identically would be the worst possible outcome — a
        nightly red banner on an expected state teaches the owner to ignore
        the banner that must never be ignored:

          (a) the durable GTC leg is intact, only the sub-share remainder is
              uncovered, and the market is SHUT. Expected. Stamped
              ``coverage='fractional_overnight'`` with ``uncovered_qty`` and
              ``unprotected_value`` so the exposure is a NUMBER the owner can
              read. No repair (a DAY order into a shut market is a rejection
              at best), no banner, no escalation.
          (b) the same shortfall while the market is OPEN. A placement
              failure. Repaired in place; if the repair lands it is stamped
              ``'fractional_replaced'`` — this is the ordinary start-of-
              session heartbeat and stays quiet — and if it does NOT land it
              falls back onto guard 3's existing ladder and alerts exactly as
              before.
          (c) the whole-share GTC leg is missing or short. Never suppressed,
              never reclassified, market hours irrelevant: 'none' escalates
              to the owner, 'partial' banners. Unchanged from guard 3.

        The three §11.1 guards are extended by this, not replaced: the retry
        burst (guard 1) now runs over each hybrid leg, the owner alert (guard
        2) still fires on a genuine partial cover, and this sweep still
        separates NO STOP AT ALL from STOP MIS-SIZED (guard 3).
        """
        try:
            pending_syms = {r.get("symbol") for r in self.db.get_pending_protection_restores()}
        except Exception as exc:  # noqa: BLE001
            record_protection_fault(self, "coverage.pending_syms", exc)
            pending_syms = set()
        # A failed positions read used to `return []`, which every caller
        # reads as all-clear. See src/protection/coverage_book_read.py.
        positions, read_error = read_positions_with_retry(self.broker)
        if positions is None:
            return unverified_book_sweep(self, read_error or "", pending_syms)

        # Spec §11.1 hybrid fractional stops. Read ONCE per pass, not per
        # position: every gap in this sweep must be judged against the same
        # clock, or a sweep straddling 16:00 ET could call one symbol's
        # lapse expected and the next symbol's identical lapse a failure.
        market_open = _market_is_open_now(self.broker)
        if market_open:
            # owed out-of-hours stop levels land FIRST
            drain_owed_stop_levels(self.broker, self.db)
        gaps: list[dict] = []
        # Positions whose protective stop has been elected and has not
        # filled. Kept OUT of `gaps`: every consumer of that list buckets a
        # row as "no stop at all" or "stop mis-sized", and this is neither —
        # the stop is present and correctly sized, it simply did not fill.
        elected_unfilled: list[dict] = []
        # Positions whose protective stops could NOT BE READ at the broker
        # (board item 172). Kept in their own list and appended to `gaps`
        # only at the very end, after every classifier, repair attempt and
        # escalation filter has run over the measured rows — so an unknown
        # can never be repaired against a guessed level, counted into the
        # overnight dollar total, or reclassified as a fractional lapse.
        unreadable: list[dict] = []
        # Symbols whose coverage gap this pass actually CLOSED. A red alarm
        # that later resolves has to say so: before this, "COULD NOT PUT THE
        # PROTECTIVE STOP BACK" was the owner's last word on a position the
        # desk itself re-covered fifteen minutes later, and he was left with
        # an instruction to place a stop by hand that already existed
        # (2026-09-23, RSG). Collected here and retracted once at the end
        # rather than one Telegram per symbol per sweep.
        repaired_symbols: list[str] = []
        longs_checked = 0
        shorts_checked = 0
        sweeper = self._sweeper()
        # A DISABLED sweep's vehicle is still exempt while it is held: it is
        # a deliberately stopless cash-park holding, not naked.
        sweep_symbol = sweeper.symbol if sweeper is not None else self._retired_cash_park_symbol()
        for p in positions:
            symbol = getattr(p, "symbol", None)
            try:
                qty = float(getattr(p, "qty", 0) or 0)
            except (TypeError, ValueError):
                continue
            # A short carries a negative qty (Alpaca convention) and is a
            # real, currently-unreachable-but-possible position (shorts-safe,
            # Stage 2). `qty <= 0` used to exempt every short from this audit
            # outright — the "a SELL-stop can't protect a short" reasoning
            # was true, but the fix is to check the OTHER side's stops, not
            # to skip the check. Inverse-ETF hedges have their own handling.
            # Skip symbols the drain already owns.
            if not symbol or qty == 0 or symbol in pending_syms:
                continue
            # The cash-sweep vehicle is deliberately stopless (cash-equivalent;
            # see src/execution/cash_sweep.py) — flagging it every session
            # would train the operator to ignore the 🔴 banner.
            if sweep_symbol is not None and symbol == sweep_symbol:
                continue
            is_short = qty < 0
            if is_short:
                shorts_checked += 1
            else:
                longs_checked += 1
            held = abs(qty)
            # ---- CANNOT BE ASKED, board item 172 --------------------------
            # This used to `continue` on a warning, which dropped the symbol
            # out of `gaps` entirely — so a position whose stops the broker
            # refused to describe was reported to nobody and read downstream
            # exactly like a position confirmed covered. Removing the
            # account-level halt made per-position stops the desk's only
            # loss protection, which makes "I could not check this one" the
            # single most important thing this sweep can find and the one
            # thing it was silent about.
            #
            # Recorded as its own condition and the sweep carries on to the
            # next symbol: a read failure on one name says nothing about any
            # other, and aborting would hide the rest of the book behind it.
            try:
                ok, specs = self.broker.snapshot_protective_stops(
                    symbol,
                    side=("buy" if is_short else "sell"),
                )
            except Exception as exc:  # noqa: BLE001
                record_protection_fault(self, "coverage.snapshot", exc, symbol=symbol)
                unreadable.append(
                    {
                        "symbol": symbol,
                        "held_qty": qty,
                        "covered_qty": None,
                        "coverage": "unreadable",
                        "repaired": False,
                        "is_short": is_short,
                        "read_error": f"snapshot_protective_stops raised: {exc}",
                    }
                )
                continue
            # `ok=False` is the COMMON read failure and the reason this
            # whole item exists: the broker's order listing swallows its own
            # exception, so a snapshot that raises is the rare case and a
            # snapshot that comes back False-with-nothing is the usual one.
            # Before it was honoured here, an outage read as 'none' — a
            # confirmed naked position — and was repaired against.
            if not ok:
                unreadable.append(
                    {
                        "symbol": symbol,
                        "held_qty": qty,
                        "covered_qty": None,
                        "coverage": "unreadable",
                        "repaired": False,
                        "is_short": is_short,
                        "read_error": (
                            "the broker's open-order listing failed, so whether "
                            "a protective stop exists could not be established"
                        ),
                    }
                )
                continue
            if specs is not None and not isinstance(specs, list):
                # Not iterable in the loop below, and `for s in (specs or [])`
                # would raise straight out of this method and take the whole
                # sweep — every other position included — with it.
                unreadable.append(
                    {
                        "symbol": symbol,
                        "held_qty": qty,
                        "covered_qty": None,
                        "coverage": "unreadable",
                        "repaired": False,
                        "is_short": is_short,
                        "read_error": (f"protective-stop snapshot in an unusable shape: {type(specs).__name__}"),
                    }
                )
                continue
            # A stop order whose quantity cannot be parsed is a stop nobody
            # can size, and neither possible guess is safe: counting it as
            # zero invents a gap, skipping it invents coverage. Left
            # unparsed, the old `sum(...)` raised straight out of this whole
            # method and took the entire sweep — every other position
            # included — with it.
            covered = 0.0
            unparsable = ""
            for s in specs or []:
                try:
                    covered += float(s.get("qty", 0) or 0)
                except (TypeError, ValueError, AttributeError) as exc:
                    unparsable = f"protective stop in an unreadable shape: {exc}"
                    break
            if unparsable:
                unreadable.append(
                    {
                        "symbol": symbol,
                        "held_qty": qty,
                        "covered_qty": None,
                        "coverage": "unreadable",
                        "repaired": False,
                        "is_short": is_short,
                        "read_error": unparsable,
                    }
                )
                continue
            # ---- ELECTED BUT UNFILLED -------------------------------------
            # Runs for EVERY held position, including the ones this sweep is
            # about to call perfectly covered — which is the entire point.
            # The desk's protective stops now rest as stop-MARKET orders
            # (owner ratified 2026-09-25), which FILL when elected — so this
            # state should no longer arise for them. It CAN still arise for
            # a stop-LIMIT still resting from before the stop-limit fallback
            # was deleted (2026-10-13): on a gap past that limit the stop is ELECTED and does not fill, the
            # order stays `status=OPEN`, so `specs` above still counts its
            # shares as covered and this sweep — correctly by its own logic —
            # does nothing about it, indefinitely. The shares are not
            # protected: an unfilled order is not an exit. Kept as the
            # backstop for exactly that residual case.
            #
            # Detected only while the market is OPEN, reusing the one
            # `market_open` read this pass already took: with the tape shut
            # there is no live "price through the stop", only yesterday's
            # close, and nobody could act on it anyway.
            if market_open:
                row = self._elected_unfilled_stop_row(
                    p,
                    specs,
                    is_short=is_short,
                )
                if row is not None:
                    elected_unfilled.append(row)
            # Owner ruling 2026-10-15, whole shares only: a long's sub-share
            # remainder has no overnight stop, so it is sold, not re-covered.
            if not is_short and market_open and _sell_fractional_remainder(self, p, symbol, held):
                continue
            if covered + 1e-6 < held:
                # Spec §11.1 guard 3. NO STOP AT ALL and STOP PRESENT BUT
                # MIS-SIZED were previously one condition with one message.
                # They are not the same thing and must never read as if they
                # were: a position stopped at the wrong size still has a
                # broker order standing watch over most of it, while a
                # position with zero coverage has nothing between it and the
                # tape. The second is the state that ends a desk, and it was
                # being reported in the same sentence as the first.
                coverage, frac_uncovered = _classify_coverage_gap(
                    held=held,
                    covered=covered,
                )
                gap = {
                    "symbol": symbol,
                    "held_qty": qty,
                    "covered_qty": covered,
                    "coverage": coverage,
                }
                # ---- Spec §11.1 hybrid fractional stops: case (a) ----
                # The durable whole-share GTC leg is intact and the only
                # thing missing is the sub-share remainder, whose DAY stop
                # the broker expires at 16:00 ET BY DESIGN. Outside session
                # hours that is not a fault, it is the mechanism working, and
                # it happens to EVERY fractional position EVERY night. It is
                # reported as measured overnight exposure — a number the
                # owner can look at — and it does not touch either red
                # banner or the owner escalation. Nor is a repair attempted:
                # a DAY order submitted into a shut market is a rejection at
                # best and a surprise queued order at worst.
                if coverage == "fractional" and not market_open:
                    # WHERE THE QUIET STATE ENDS. The ratified overnight
                    # lapse is a sub-share DAY stop that WAS placed and
                    # expired at 16:00 by design. A remainder that spent the
                    # whole session waiting for its name's first print and
                    # never got a stop at all is not that, and filing it as
                    # that would turn the bell-adjacent silence into a
                    # suppression: nothing would ever have told the owner.
                    # This is the pass that can honestly say the session's
                    # repair path is exhausted, so this is the pass that
                    # pages.
                    try:
                        from src.coverage_watchdog import (
                            session_awaiting_print_symbols,
                        )

                        never_covered = symbol.strip().upper() in session_awaiting_print_symbols()
                    except Exception as exc:  # noqa: BLE001
                        record_protection_fault(self, "coverage.awaiting_print_read", exc, symbol=symbol)
                        logger.warning(
                            "coverage sweep: could not read today's "
                            "awaiting-print names (%s) — treating %s as the "
                            "ordinary overnight lapse.",
                            exc,
                            symbol,
                        )
                        never_covered = False
                    if never_covered:
                        gap["coverage"] = "partial" if covered > 1e-6 else "none"
                        gap["uncovered_qty"] = frac_uncovered
                        gap["unprotected_value"] = _position_notional(
                            p,
                            frac_uncovered,
                        )
                        gap["repaired"] = False
                        gap["is_short"] = is_short
                        gap["session_repair_failed"] = True
                        gap["never_printed_today"] = True
                        logger.error(
                            "FRACTIONAL STOP NEVER RE-PLACED THIS SESSION: "
                            "%s held=%.4f, %.4f covered — the name produced "
                            "no confirmed trade print all session, so the "
                            "repair could never price a stop, and the "
                            "sub-share remainder has now been uncovered "
                            "since the previous close. This is NOT the "
                            "expected overnight lapse and it alerts.",
                            symbol,
                            qty,
                            covered,
                        )
                        gaps.append(gap)
                        continue
                    gap["coverage"] = "fractional_overnight"
                    gap["uncovered_qty"] = frac_uncovered
                    gap["unprotected_value"] = _position_notional(
                        p,
                        frac_uncovered,
                    )
                    gap["repaired"] = False
                    logger.info(
                        "FRACTIONAL DAY STOP LAPSED (expected): %s held=%.4f, "
                        "%.4f whole share(s) still covered by the durable GTC "
                        "stop, %s sub-share remainder unprotected until the "
                        "next session re-places its DAY stop.",
                        symbol,
                        qty,
                        covered,
                        frac_uncovered,
                    )
                    gaps.append(gap)
                    continue
                if coverage == "fractional":
                    # ---- case (b), first half: session hours ----
                    # The remainder should be covered RIGHT NOW. Repair it,
                    # and only if the repair fails does it carry a real
                    # condition name into the alerting below.
                    logger.warning(
                        "FRACTIONAL STOP MISSING DURING SESSION HOURS: %s "
                        "held=%.4f, %.4f covered — the sub-share DAY stop is "
                        "absent while the market is OPEN, which is a placement "
                        "failure, not the expected overnight lapse. Repairing.",
                        symbol,
                        qty,
                        covered,
                    )
                    repaired = self._repair_stop_coverage(
                        symbol,
                        held - covered,
                        is_short=is_short,
                        outcome=gap,
                        resting_stops=list(specs or []),
                    )
                    gap["repaired"] = repaired
                    if repaired:
                        # Re-placed inside the same pass. This is the ordinary
                        # start-of-session path for every fractional position
                        # the desk holds, so it must NOT read as a red banner
                        # — it is the design's daily heartbeat.
                        gap["coverage"] = "fractional_replaced"
                        gap["uncovered_qty"] = 0.0
                        repaired_symbols.append(symbol)
                        logger.info(
                            "FRACTIONAL DAY STOP RE-PLACED: %s — the sub-share "
                            "remainder is covered again for this session.",
                            symbol,
                        )
                    else:
                        # Could not re-place during session hours. Falls back
                        # onto guard 3's existing ladder unchanged: zero
                        # coverage escalates, some coverage banners.
                        gap["coverage"] = "none" if covered <= 1e-6 else "partial"
                        gap["uncovered_qty"] = held - covered
                        gap["unprotected_value"] = _position_notional(
                            p,
                            held - covered,
                        )
                        # The marker that makes the log line below TRUE.
                        # It used to be a claim only: a partial fallback
                        # ('partial' whenever any whole share is still
                        # covered, which is every fractional position) never
                        # reached `_alert_owner_no_stop`, and the only code
                        # that could page lived in the standalone coverage
                        # watchdog — a separate process that need not be
                        # running, and was not on 2026-09-18, when NET and
                        # RSG sat uncovered during the session and the owner
                        # was never told. The session that OBSERVED it now
                        # sends it.
                        gap["is_short"] = is_short
                        # WHICH SWEEP IS ENTITLED TO PAGE. Not this one, if
                        # the refusal is the tape's rather than the desk's.
                        # Measured across the whole retained production log,
                        # every `no_trade_print_today` refusal fired inside
                        # 45 seconds of the opening bell and every one was
                        # resolved in the same session, five of them by the
                        # very next sweep — so the first attempt paged before
                        # the mechanism that fixes it had had its turn, and
                        # the owner was sent to place a stop by hand that the
                        # desk placed itself fifteen minutes later. The
                        # classifier and its derivation live in one place
                        # (`src.coverage_watchdog.page_now_for_refusal`),
                        # shared with the standalone sweep, and a position
                        # with NO coverage left is never deferred by it.
                        try:
                            from src.coverage_watchdog import (
                                awaiting_first_print,
                                note_awaiting_first_print,
                            )

                            waiting = awaiting_first_print(
                                refusal_code=str(gap.get("repair_refusal_code") or ""),
                                still_covered=covered > 1e-6,
                                market_open=True,
                            )
                            if waiting:
                                note_awaiting_first_print(symbol)
                        except Exception as exc:  # noqa: BLE001
                            record_protection_fault(self, "coverage.refusal_classify", exc, symbol=symbol)
                            # An unreadable marker file errs towards telling
                            # the owner, the same way the claim does.
                            logger.warning(
                                "coverage sweep: could not classify the stop-repair refusal for %s (%s) — paging.",
                                symbol,
                                exc,
                            )
                            waiting = False
                        page_now = not waiting
                        gap["session_repair_failed"] = page_now
                        if page_now:
                            logger.error(
                                "FRACTIONAL STOP RE-PLACEMENT FAILED for %s "
                                "during session hours (held=%.4f, "
                                "covered=%.4f) — this is case (b) and it "
                                "alerts.",
                                symbol,
                                qty,
                                covered,
                            )
                        else:
                            # NOT a new `coverage` word. The gap stays
                            # 'partial' and keeps its ⚠️ STOP MIS-SIZED line
                            # in the session feed and the evening banner:
                            # the position really is under-protected and the
                            # owner should still SEE it. The only thing this
                            # state changes is whether it INTERRUPTS him,
                            # which is the defect. Reclassifying it would
                            # have meant registering a fifth coverage word
                            # in `src/notifier.py` and `src/trader_feed.py`
                            # and would have hidden a real shortfall to fix
                            # an alerting bug.
                            gap["awaiting_first_print"] = True
                            logger.warning(
                                "FRACTIONAL STOP AWAITING FIRST PRINT: %s "
                                "(held=%.4f, covered=%.4f): %s. The repair "
                                "was attempted and will be attempted again "
                                "on every pass; the whole-share leg is still "
                                "standing watch. Not an owner page while the "
                                "session can still resolve it — the pass "
                                "that finds the market shut with this still "
                                "true is the one that pages.",
                                symbol,
                                qty,
                                covered,
                                gap.get("repair_refusal") or "no reason given",
                            )
                    gaps.append(gap)
                    continue
                # ---- case (c) and every pre-existing condition ----
                # The whole-share GTC leg is missing or short. Never
                # suppressed, never reclassified, market hours irrelevant:
                # that leg is the durable protection and its absence is the
                # state that ends a desk.
                if coverage == "none":
                    logger.critical(
                        "NO STOP AT ALL: %s held=%.4f with ZERO open "
                        "protective %s-stops — the position is COMPLETELY "
                        "unprotected and has no WAL recovery row.",
                        symbol,
                        qty,
                        "buy" if is_short else "sell",
                    )
                else:
                    logger.warning(
                        "STOP MIS-SIZED: %s held=%.4f but only %.4f covered by "
                        "open protective %s-stops — partially unprotected with "
                        "no WAL recovery row.",
                        symbol,
                        qty,
                        covered,
                        "buy" if is_short else "sell",
                    )
                gap["repaired"] = self._repair_stop_coverage(
                    symbol,
                    held - covered,
                    is_short=is_short,
                    outcome=gap,
                    resting_stops=list(specs or []),
                )
                if gap["repaired"]:
                    repaired_symbols.append(symbol)
                gaps.append(gap)
        if (longs_checked or shorts_checked) and not gaps and not unreadable:
            logger.info(
                "Stop-coverage reconcile: all %d long / %d short position(s) adequately stop-covered",
                longs_checked,
                shorts_checked,
            )
        elif unreadable and not gaps:
            # Board item 172. The clean line above says every position is
            # covered. A pass that could not read one is not entitled to
            # say that about the book, only about the part it could read.
            logger.error(
                "Stop-coverage reconcile: %d of %d position(s) UNREADABLE "
                "(%s) — every position that COULD be read is adequately "
                "stop-covered; the rest is unknown.",
                len(unreadable),
                longs_checked + shorts_checked,
                ", ".join(str(g.get("symbol")) for g in unreadable),
            )
        # Spec §11.1 hybrid fractional stops, observability half. Total the
        # deliberate overnight exposure into ONE line the owner can read at a
        # glance. The individual gap dicts carry it too (the notifier renders
        # them), but a running total is what turns "a bounded remainder" from
        # a promise into a measurement.
        overnight = [g for g in gaps if g.get("coverage") == "fractional_overnight"]
        if overnight:
            total_value = sum(float(g.get("unprotected_value") or 0) for g in overnight)
            logger.warning(
                "OVERNIGHT FRACTIONAL EXPOSURE: %d position(s) carrying a "
                "sub-share remainder with no live stop until the next session "
                "— $%.2f total at risk. Expected and bounded by design; the "
                "whole-share part of each is still covered by its GTC stop.",
                len(overnight),
                total_value,
            )
        # Spec §11.1 guard 3, escalation half. A gap the auto-repair CLOSED
        # needs no interruption — the belt did its job. A position still
        # carrying NO stop at all after the repair attempt is a live naked
        # position, and the sweep runs on a 30-minute cadence whose
        # `intra_check` message is silent unless it liquidates: without this,
        # the worst state this reconciler can find would be reported only in
        # a log file. Mis-sized gaps stay in the session banner rather than
        # interrupting the owner — they are real but bounded, and alerting on
        # both is how a channel gets tuned out.
        #
        # Spec §11.1 hybrid fractional stops: the `coverage == "none"` test is
        # exactly the right filter and needs no exception added to it. An
        # expected overnight lapse is stamped 'fractional_overnight' and a
        # re-placed one 'fractional_replaced', so neither can reach this list
        # — while a sub-share position that could NOT be re-covered during
        # SESSION hours falls back to 'none' above and escalates here, which
        # is precisely case (b). The suppression lives in one classifier, not
        # in a growing list of special cases at the escalation site.
        naked = [g for g in gaps if g.get("coverage") == "none" and not g.get("repaired")]
        if elected_unfilled:
            self._alert_owner_elected_unfilled(elected_unfilled)
        if naked:
            self._alert_owner_no_stop(naked)
        # A session-hours re-placement that did not land is its own
        # escalation, separate from the naked list above: the whole-share
        # GTC leg is usually still standing watch, so the gap classifies as
        # 'partial' and would otherwise be a banner line the owner reads
        # hours later, if at all. Suppression is shared with the standalone
        # watchdog so whichever process sees it first is the one that tells
        # him, and neither repeats the other.
        session_failures = [
            g
            for g in gaps
            if g.get("session_repair_failed")
            and not g.get("repaired")
            # A sub-share failure with zero coverage left already went out
            # as NO STOP AT ALL above; one condition, one message.
            and g not in naked
        ]
        if session_failures:
            self._alert_owner_session_repair_failed(session_failures)
        # Sent AFTER the escalations above, and last for a reason: a symbol
        # this pass both repaired and then found short again must end on the
        # alarm, not on the retraction.
        if repaired_symbols:
            try:
                from src.coverage_watchdog import clear_awaiting_first_print

                # The gap is closed, so the name is no longer waiting on a
                # print and must not be reported after the close as though
                # it had waited all session.
                clear_awaiting_first_print(repaired_symbols)
            except Exception as exc:  # noqa: BLE001
                record_protection_fault(self, "coverage.clear_marker", exc)
                logger.warning(
                    "coverage sweep: could not clear the awaiting-print marker for %s: %s",
                    ", ".join(repaired_symbols),
                    exc,
                )
            self._alert_owner_repair_resolved(repaired_symbols)
        # Board item 172. Appended AFTER every filter above has been built
        # from `gaps`, so an unreadable row cannot reach the naked list, the
        # session-failure list, the overnight dollar total or a repair — all
        # of which require a measured shortfall this row does not have.
        if unreadable:
            self._alert_owner_unreadable_stop(unreadable)
            gaps.extend(unreadable)
        try:
            from src.execution.stop_records import (
                reconcile_recorded_stop_levels,
                report_stop_level_mismatches,
                write_back_live_protective_stops,
            )

            mismatches = reconcile_recorded_stop_levels(
                broker=self.broker,
                last_buy=lambda sym, action="BUY": self.db.get_symbol_last_buy(
                    sym,
                    include_in_flight=True,
                    action=action,
                ),
                positions=positions,
                sweep_symbol=sweep_symbol,
                skip_symbols=pending_syms,
                db=self.db,
            )
            mismatches = write_back_live_protective_stops(self.db, mismatches)
            report_stop_level_mismatches(
                record_reconciliation(db=self.db, kind="recorded_stop_levels", result=mismatches)
            )
        except Exception as exc:  # noqa: BLE001
            record_protection_fault(self, "coverage.stop_level_reconcile", exc)
            logger.error("stop-level reconcile failed: %s", exc)
        return record_reconciliation(db=self.db, kind="stop_coverage", result=gaps)

    def _elected_unfilled_stop_row(self, *args, **kwargs):
        """Thin shim -> CoverageElection (src/protection/coverage_election.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.coverage_election import CoverageElection

        return CoverageElection._elected_unfilled_stop_row(_build_coverage_election(self), *args, **kwargs)

    @staticmethod
    def _alert_owner_elected_unfilled(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_elected_unfilled (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_elected_unfilled(*args, **kwargs)

    def _still_uncovered(self, *args, **kwargs):
        """Thin shim -> OwnerAlerts (src/protection/owner_alerts.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._still_uncovered(_build_owner_alerts(self), *args, **kwargs)

    def _alert_owner_session_repair_failed(self, *args, **kwargs):
        """Thin shim -> OwnerAlerts (src/protection/owner_alerts.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_session_repair_failed(_build_owner_alerts(self), *args, **kwargs)

    @staticmethod
    def _alert_owner_repair_resolved(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_repair_resolved (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_repair_resolved(*args, **kwargs)

    @staticmethod
    def _alert_owner_no_stop(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_no_stop (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_no_stop(*args, **kwargs)

    @staticmethod
    def _alert_owner_stop_pending_acceptance(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_stop_pending_acceptance (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_stop_pending_acceptance(*args, **kwargs)

    @staticmethod
    def _alert_owner_unreadable_stop(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_unreadable_stop (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_unreadable_stop(*args, **kwargs)

    @staticmethod
    def _alert_owner_exit_declined(*args, **kwargs):
        """Thin shim -> OwnerAlerts._alert_owner_exit_declined (static; src/protection/owner_alerts.py)."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_exit_declined(*args, **kwargs)

    def _wire_protective_stop_block_recorder(self, *args, **kwargs):
        """Thin shim -> CoverageRepair (this module); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return CoverageRepair._wire_protective_stop_block_recorder(_build_coverage_repair(self), *args, **kwargs)

    @staticmethod
    @staticmethod
    def _alert_owner_kill_switch_blocked(*args, **kwargs):
        """Thin shim -> CoverageRepair._alert_owner_kill_switch_blocked (static; this module)."""
        return CoverageRepair._alert_owner_kill_switch_blocked(*args, **kwargs)

    def _repair_stop_coverage(self, *args, **kwargs):
        """Thin shim -> CoverageRepair (this module); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return CoverageRepair._repair_stop_coverage(_build_coverage_repair(self), *args, **kwargs)

    def _submit_protected_sell(self, *args, **kwargs):
        """Thin shim -> ProtectedSell (src/protection/protected_sell.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ProtectedSell._submit_protected_sell(_build_protected_sell(self), *args, **kwargs)

    #: Exit orders this process submitted and waited on that did NOT reach a
    #: terminal broker state: {order_id: {symbol, submitted_qty}}.
    #: `wait_for_order_terminal` has a 15s ceiling and returns the LAST KNOWN
    #: status, so a marketable limit that does not fill in time leaves the
    #: order working while the code moves on and re-reads the book from the
    #: broker. That refreshed book still carries exposure already on its way
    #: out, and anything that then re-measures gross would shed it twice.
    #: Registered centrally in `_finalize_pending_protections` so EVERY exit
    #: path is covered by construction — the cash-deficit safety net, the
    #: gross-ceiling de-lever, the position reviewer's sells, the execution
    #: stage and the cash sweep — rather than by remembering to flag each one.
    _unsettled_exit_orders: dict[str, dict]

    def _register_exit_settlement(self, *args, **kwargs):
        """Thin shim -> ExitRelief (this module); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ExitRelief._register_exit_settlement(_build_exit_relief(self), *args, **kwargs)

    def _open_exit_relief(self, *args, **kwargs):
        """Thin shim -> ExitRelief (this module); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ExitRelief._open_exit_relief(_build_exit_relief(self), *args, **kwargs)

    def _finalize_pending_protections(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._finalize_pending_protections(_build_sell_finalization(self), *args, **kwargs)

    def _finalize_protection_after_sell(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._finalize_protection_after_sell(_build_sell_finalization(self), *args, **kwargs)

    def _finalize_protection_after_sell_core(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._finalize_protection_after_sell_core(_build_sell_finalization(self), *args, **kwargs)

    def _cancel_stray_stops_on_flat(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._cancel_stray_stops_on_flat(_build_sell_finalization(self), *args, **kwargs)

    def _write_ahead_protection_restore(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._write_ahead_protection_restore(_build_sell_finalization(self), *args, **kwargs)

    def _cancel_stops_with_write_ahead(self, *args, **kwargs):
        """Thin shim -> ProtectedSell (src/protection/protected_sell.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ProtectedSell._cancel_stops_with_write_ahead(_build_protected_sell(self), *args, **kwargs)

    def _restore_after_unconfirmed_sell(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._restore_after_unconfirmed_sell(_build_sell_finalization(self), *args, **kwargs)

    def _persist_orphaned_protection_restore(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._persist_orphaned_protection_restore(_build_sell_finalization(self), *args, **kwargs)

    def _derive_close_side_for_drain(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._derive_close_side_for_drain(_build_sell_finalization(self), *args, **kwargs)

    def _resolve_wal_row_side(self, *args, **kwargs):
        """Thin shim -> SellFinalization (src/protection/sell_finalization.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.sell_finalization import SellFinalization

        return SellFinalization._resolve_wal_row_side(_build_sell_finalization(self), *args, **kwargs)

    def _drain_pending_repegs(self, *args, **kwargs):
        """Thin shim -> RepegDrain (src/protection/repeg_drain.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.repeg_drain import RepegDrain

        return RepegDrain._drain_pending_repegs(_build_repeg_drain(self), *args, **kwargs)

    def _delete_repeg_row(self, *args, **kwargs):
        """Thin shim -> RepegDrain (src/protection/repeg_drain.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.repeg_drain import RepegDrain

        return RepegDrain._delete_repeg_row(_build_repeg_drain(self), *args, **kwargs)

    def _drain_pending_protection_restores(self, *args, **kwargs):
        """Thin shim -> RestoreDrain (this module); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return RestoreDrain._drain_pending_protection_restores(_build_restore_drain(self), *args, **kwargs)

    def _reprotect_residual_after_partial_sell(self, *args, **kwargs):
        """Thin shim -> ReprotectResidual (this module; body over the 400-line ceiling for a new file); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ReprotectResidual._reprotect_residual_after_partial_sell(
            _build_reprotect_residual(self), *args, **kwargs
        )

    def _record_reprotect_identity_gap(self, *args, **kwargs):
        """Thin shim -> ReprotectRecords (src/protection/reprotect_records.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ReprotectRecords._record_reprotect_identity_gap(_build_reprotect_records(self), *args, **kwargs)

    def _alert_owner_reprotect_left_naked(self, *args, **kwargs):
        """Thin shim -> OwnerAlerts (src/protection/owner_alerts.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.owner_alerts import OwnerAlerts

        return OwnerAlerts._alert_owner_reprotect_left_naked(_build_owner_alerts(self), *args, **kwargs)

    @staticmethod
    def _order_accepted(*args, **kwargs):
        """Thin shim -> FillReconciler._order_accepted (static; src/protection/fill_reconciler.py)."""
        from src.protection.fill_reconciler import FillReconciler

        return FillReconciler._order_accepted(*args, **kwargs)

    def _reconcile_fills(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler

        return FillReconciler._reconcile_fills(_build_fill_reconciler(self), *args, **kwargs)
        # Any other non-terminal status (new, accepted, pending_new, ...)
        # has nothing filled yet: stay 'submitted' for the next pass.

    def _reconcile_orphan_pending_submits(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler

        return FillReconciler._reconcile_orphan_pending_submits(_build_fill_reconciler(self), *args, **kwargs)

    @staticmethod
    def _parse_broker_fill_timestamp(*args, **kwargs):
        """Thin shim -> FillReconciler._parse_broker_fill_timestamp (static; src/protection/fill_reconciler.py)."""
        from src.protection.fill_reconciler import FillReconciler

        return FillReconciler._parse_broker_fill_timestamp(*args, **kwargs)

    def _flag_stop_out_anomaly(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler

        return FillReconciler._flag_stop_out_anomaly(_build_fill_reconciler(self), *args, **kwargs)

    def _reconcile_stop_out_fills(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler

        return FillReconciler._reconcile_stop_out_fills(_build_fill_reconciler(self), *args, **kwargs)

    def _surface_reconcile_outcomes(self, *args, **kwargs):
        """Thin shim -> FillReconciler (src/protection/fill_reconciler.py); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        from src.protection.fill_reconciler import FillReconciler

        return FillReconciler._surface_reconcile_outcomes(_build_fill_reconciler(self), *args, **kwargs)

    def _handle_ex_dividends(self, *args, **kwargs):
        """Thin shim -> ExDividends (this module); calls the class method so the collaborator of the same name on the built object is never re-entered."""
        return ExDividends._handle_ex_dividends(_build_ex_dividends(self), *args, **kwargs)
