"""THE one place a missing protective stop is re-placed.

Lifted out of `TradingPipeline._repair_stop_coverage` unchanged so that the
two callers that need it can share it rather than grow a second copy:

  * the in-session coverage sweep (`TradingPipeline._reconcile_stop_coverage`),
    which is where this behaviour has always lived, and
  * `src/coverage_watchdog.py`, which runs from the alert-heartbeat unit and
    is the only thing still running while the trading timers are stopped
    (docs/INCIDENT_HISTORY.md, item 53).

The pipeline method is now a thin delegate. A second order-placement path
was the alternative and is exactly how one behaviour ends up with two homes
that drift; the fractional tif rule alone (`_derive_stop_tif`) is a rule the
duplicate would have had to re-learn.

The DEPENDENCIES are passed in rather than reached for: `broker` supplies the
price and the retrying submit, `last_buy` supplies the recorded stop level.
The watchdog has no `TradingPipeline` and must never build one — that would
pull the agents, the LLM clients and the whole session machinery into a
06:15 heartbeat.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Callable

from src.execution.pending_stop_drain import drain_safely as drain_owed_stop_levels  # the sweep's first coverage repair
from src.execution.stop_repair_parts import _refuse, derive_protective_level
from src.execution.stop_records import (
    STOP_ABSENT,
    STOP_USABLE,
    classify_stop_price,
)

logger = logging.getLogger(__name__)


def repair_stop_coverage(
    *,
    broker: Any,
    last_buy: Callable[..., dict | None],
    symbol: str,
    uncovered_qty: float,
    is_short: bool,
    db: Any = None,
    outcome: dict | None = None,
    resting_stops: list | None = None,
    caller: str = "",
    market: Any = None,
    close_breached: Callable[..., str | None] | None = None,
) -> bool:
    """Best-effort: re-place protective stop coverage on an uncovered
    position using the stop level recorded on its last opening row (BUY for
    a long, SHORT for a short). Returns True when the gap was actually
    closed.

    Spec §11.1 hybrid fractional stops: this is also THE re-placement path
    for a sub-share DAY stop that lapsed at yesterday's close. It needs no
    fractional special-case of its own — it routes through
    `_submit_protective_stop_retrying`, which splits a fractional
    `uncovered_qty` into its GTC and DAY legs, so re-placing 0.3456 share(s)
    at the open and protecting a whole fresh 12.3456-share entry are the same
    one code path. THE CALLER owns the decision of WHETHER to call this at
    the current hour; this function does not consult the clock.

    Why this is safe to auto-repair (it deliberately wasn't before): the old
    objection was "the original protective level is unknown for a position
    with no live stop, so picking one is a policy decision". It isn't
    unknown — the opening row carries the `stop_loss` the PM/RM agreed and
    the constructor sized against. Repairing to THAT level restores the
    reviewed intent rather than inventing a new one. The same is true of a
    SHORT row: `insert_trade` writes `stop_loss` on that action the same way
    it does on BUY.

    This is the belt for the 2026-07-16 CRITICAL (BUY-attached OTO stops
    inherited a DAY tif and expired at the close, leaving positions naked
    overnight) — both for any position that bug left uncovered, and for a
    crash between an entry fill and `place_entry_protection`.

    Guards: never place a stop that would instantly fire (a long's sell-stop
    at/above the live price, a short's buy-stop at/below it — that would
    turn a repair into a market-order exit, a decision for the reviewer, not
    for a janitor), and never invent a level when the opening row has none.
    After a trail, `stop_loss` on that row is the LIVE recorded level, not
    the frozen entry stop. `would_fire` therefore uses the trailed price.
    Restoring the entry stop instead would be a widen; this janitor does
    not widen. A stop cancelled after a trail, with the tape already through
    the trailed level, stays flagged for the reviewer.
    The repair places the same protective stop the entry path does: a
    stop-MARKET (guaranteed exit, owner ratified 2026-09-25), with no
    limit and no stop-limit fallback. No new constant.

    It ADDS an order and nothing else, with ONE exception (k40, 2026-10-13):
    when a RECORDED, current stop is already crossed by today's
    regular-session trade print, re-placing it is impossible and leaving it
    off strands the shares, so the repair executes that stop — a market
    order for the uncovered quantity, capped at a fresh position read, through
    the desk's protected sell path (see `_breach_blocker` / `_execute_breached_stop`). It
    never resizes, closes the covered shares, or re-pegs an entry, and the
    level comes from a recorded row or the broker's resting stop, never from a target.

    When the broker accepts an order id it writes that same recorded level
    back onto the opening row (`db.update_open_stop_loss` when `db` is
    supplied), including a partial cover whose id landed. For a first-time
    restore this is a no-op on the number; after a trail it is what keeps
    a later repair from re-placing a stale entry stop. A kill-switch or
    missing-id payload is not written back — the broker does not hold it.
    Partial cover still returns False so the pass stays loud until the
    gap is gone.
    """
    if uncovered_qty <= 0:
        return False
    # What the durable refusal row carries (see `_refuse`). `held_qty` /
    # `covered_qty` come off the caller's gap dict when it has them.
    gap = outcome if isinstance(outcome, dict) else {}
    rec = {
        "db": db,
        "symbol": symbol,
        "uncovered_qty": uncovered_qty,
        "is_short": is_short,
        "caller": caller,
        "held_qty": gap.get("held_qty"),
        "covered_qty": gap.get("covered_qty"),
        "resting_stops": resting_stops,
    }
    opening = "SHORT" if is_short else "BUY"
    protective_side = "buy" if is_short else "sell"
    try:
        entry = last_buy(symbol, action=opening) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "coverage repair: last-%s lookup failed for %s: %s",
            opening,
            symbol,
            exc,
        )
        return _refuse(
            outcome,
            f"the archive could not be read for the recorded {opening} stop",
            code="archive_unreadable",
            record=rec,
        )
    # docs/WORK.md item 88 — the two cases this used to collapse into one
    # sentence. "No stop was ever recorded" and "a stop was recorded and its
    # value is garbage" are different facts: the first is a row this janitor
    # has no reviewed level for, the second is a corrupt archive on a live
    # position, and an owner reading "has no recorded stop_loss" about a row
    # that DOES carry one would go looking in the wrong place. Neither is
    # repaired — inventing a level was never this function's job — but they
    # are no longer reported as the same thing. The classification also
    # catches a recorded ±Inf, which the old `<= 0` test passed straight
    # through to the broker (NaN cannot survive SQLite, which stores it as
    # NULL; Inf round-trips as a real number).
    recorded = entry.get("stop_loss")
    stop_state, stop_price = classify_stop_price(recorded)
    # A derived level is this janitor's own fallback, not a stop the desk
    # decided on: it may be placed, never executed (`_breach_blocker`).
    level_was_derived = stop_state == STOP_ABSENT
    if stop_state == STOP_ABSENT:
        # Owner ruling 2026-10-02: the desk acts rather than flagging a
        # naked position for a review nobody performs. The archive has been
        # asked; ask the broker and derive off the name's own ATR instead.
        derived, derived_basis = derive_protective_level(
            broker=broker,
            market=market,
            symbol=symbol,
            is_short=is_short,
        )
        if derived is None:
            return _refuse(
                outcome,
                f"no stop level was ever recorded on the {opening} row and "
                f"none could be derived from the broker's own average entry "
                f"price and the name's ATR",
                code="no_recorded_stop",
                record=rec,
            )
        logger.warning(
            "coverage repair: %s has no recorded %s stop_loss — placing a protective stop at $%.2f, %s",
            symbol,
            opening,
            derived,
            derived_basis,
        )
        stop_state, stop_price = STOP_USABLE, derived
        if isinstance(outcome, dict):
            outcome["derived_stop_basis"] = derived_basis
    if stop_state != STOP_USABLE:
        logger.error(
            "coverage repair REFUSED for %s: the recorded %s stop_loss is "
            "%r, which cannot be a stop price. NOT treated as 'no stop "
            "needed' — the gap stays flagged for manual review and the "
            "archive row needs a look.",
            symbol,
            opening,
            recorded,
        )
        return _refuse(
            outcome,
            f"the recorded {opening} stop level is {recorded!r}, which "
            f"cannot be a stop price — the archive row is corrupt",
            code="recorded_stop_unusable",
            record=rec,
        )
    # The wrong-side test below decides whether putting this stop back would
    # fire it instantly — i.e. sell the position at market. That test is only
    # as good as the price it runs against, and a price with no trade time on
    # it can be yesterday's last print on a thin name, or a quote the tape
    # never confirmed. Against a stale number the test can read "safe" while
    # today's real price is already through the stop, and the repair becomes
    # an unintended market exit. So a repair requires a trade print stamped
    # today; anything else leaves the gap flagged for the next sweep, which
    # is the same outcome this function already produces for every other
    # unverifiable input.
    # `LivePrice` comes from the module that DEFINES it, not from the broker
    # facade that re-exports it: the facade would close an import cycle.
    from src.execution.broker_parts.market_data import LivePrice
    from src.execution.stop_repair_price import read_repair_price

    stamped, price, price_error = read_repair_price(
        broker,
        symbol,
        stop_price=stop_price,
        uncovered_qty=uncovered_qty,
        is_short=is_short,
        caller=caller,
        db=db,
        outcome=outcome,
        resting_stops=resting_stops,
        rec=rec,
        live_price_cls=LivePrice,
    )
    blind = price is None and price_error is not None
    if not blind and not (isinstance(price, (int, float)) and price > 0 and math.isfinite(price)):
        return _refuse(
            outcome,
            "the broker returned no usable live price, so the recorded stop could not be checked against the tape",
            code="price_unusable",
            record=rec,
        )
    if not blind and stamped is not None and not stamped.is_today_print:
        # `get_latest_price_stamped` only ever stamps `is_today_print` for a
        # `last_trade` — never for today's still-live 1-minute or session
        # bar, even though `src.data.live_price.resolve_live_price` treats
        # either of those as a legitimate today print. A name with a stale
        # last_trade (thin overnight name, feed gap) but a live minute bar
        # was refusing here even though the tape has, in fact, printed
        # today — an avoidable no-print refusal and a false owner alarm.
        # Before giving up, check the same intraday snapshot the research
        # path already trusts for "is this today's". This can ONLY resolve
        # to a real print (last trade, minute bar, session bar) — never a
        # quote mid, which `resolve_live_price` structurally cannot return —
        # so the "never repair off a quote mid" rule above is untouched.
        resolved_price = None
        resolved_source = None
        try:
            snapshots_getter = getattr(broker, "get_intraday_snapshots", None)
            if callable(snapshots_getter):
                snapshots = snapshots_getter([symbol])
                snapshot = snapshots.get(symbol) if isinstance(snapshots, dict) else None
                if snapshot:
                    from src.data.live_price import resolve_live_price

                    resolved = resolve_live_price(snapshot)
                    if resolved.price is not None:
                        resolved_price = resolved.price
                        resolved_source = resolved.source
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "coverage repair: intraday snapshot lookup failed for %s: %s",
                symbol,
                exc,
            )
        if resolved_price is not None:
            logger.info(
                "coverage repair: %s last read ($%.2f from %s) was not a "
                "today print, but today's %s at $%.2f satisfies the "
                "freshness requirement — proceeding with that price.",
                symbol,
                price,
                stamped.source,
                resolved_source,
                resolved_price,
            )
            price = resolved_price
        else:
            logger.warning(
                "coverage repair: %s has no trade print from today (price $%.2f "
                "came from %s, and no today minute/session bar was available "
                "either) — a stop placed off an unconfirmed price could fire "
                "immediately. Leaving the gap flagged for the next sweep.",
                symbol,
                price,
                stamped.source,
            )
            return _refuse(
                outcome,
                "there is no trade print from today to check the recorded stop against",
                code="no_trade_print_today",
                record=rec,
            )
    # Long sell-stop must sit strictly below the tape; short buy-stop must
    # sit strictly above it. The wrong-side test is the one that would turn
    # this janitor into an immediate marketable exit.
    would_fire = False if blind else (stop_price <= price if is_short else stop_price >= price)
    if would_fire:
        breach, exec_level, level_source = _breach_blocker(
            db=db,
            symbol=symbol,
            stamped=stamped,
            price=price,
            level_was_derived=level_was_derived,
            live_price_cls=LivePrice,
            recorded_level=stop_price,
            resting_stops=resting_stops,
            is_short=is_short,
            closer_supplied=close_breached is not None,
        )
        if breach is None:
            return _execute_breached_stop(
                broker=broker,
                db=db,
                symbol=symbol,
                uncovered_qty=uncovered_qty,
                is_short=is_short,
                stop_price=exec_level,
                level_source=level_source,
                close_breached=close_breached,
                stamped=stamped,
                outcome=outcome,
                rec=rec,
            )
        logger.warning(
            "coverage repair: %s recorded %s-stop $%.2f is on the live-price "
            "side of $%.2f — a repair would fire immediately. Leaving the "
            "gap flagged; the reviewer owns this exit decision.",
            symbol,
            protective_side,
            stop_price,
            price,
        )
        return _refuse(
            outcome,
            f"the recorded stop ${stop_price:,.2f} is already on the live-"
            f"price side of ${price:,.2f}, so restoring it would exit the "
            f"position at market — that is the reviewer's decision; it was "
            f"not executed as a stop because {breach}",
            code="would_fire_immediately",
            record=rec,
            stop_price=stop_price,
        )
    # Spec §11.1 guard 1 belongs here too. This was a single bare
    # `_submit_stop_limit_order` call with NO retry burst at all — a
    # transient failure (429, dropped connection) cost the position a full
    # 30-minute cycle instead of clearing in ~2 seconds the way the entry
    # path does, and for a FRACTIONAL `uncovered_qty` (this repair is
    # reached with one whenever the gapped position is itself fractional)
    # there was also no whole-share fallback — the exact gap guard 1 exists
    # to close on the entry side, left open on the belt that is supposed to
    # be its backstop. Route through the same retrying+fallback machinery
    # instead of a second, weaker copy of it.
    result = broker._submit_protective_stop_retrying(
        symbol=symbol,
        qty=uncovered_qty,
        stop_price=stop_price,
        side=protective_side,
    )
    from src.execution.stop_records import accepted_stop_order, write_back_stop_loss
    from src.execution.exit_path_records import (
        is_kill_switch_block,
        kill_switch_blocked_text,
    )

    if is_kill_switch_block(result):
        # The desk's own kill switch refused the order before it reached the
        # broker. This used to fall into the branch below and tell the owner
        # "the broker did not accept a protective stop after every retry" —
        # the wrong cause, and the wrong place to look. Same outcome (not
        # repaired, gap stays flagged); only the words and the record differ.
        logger.error(
            "coverage repair BLOCKED BY KILL SWITCH for %s (%.4f uncovered, "
            "stop $%.2f) — nothing was sent to the broker",
            symbol,
            uncovered_qty,
            stop_price,
        )
        return _refuse(
            outcome,
            kill_switch_blocked_text(symbol),
            code="kill_switch",
            record=rec,
            stop_price=stop_price,
        )
    if result is None or not accepted_stop_order(result):
        logger.error(
            "coverage repair FAILED for %s (%.4f uncovered, stop $%.2f) — "
            "retries exhausted or broker did not accept an order id",
            symbol,
            uncovered_qty,
            stop_price,
        )
        return _refuse(
            outcome,
            f"the broker did not accept a protective stop at ${stop_price:,.2f} after every retry",
            code="broker_did_not_accept",
            record=rec,
            stop_price=stop_price,
        )
    residual = 0.0
    try:
        residual = float(result.get("uncovered_qty") or 0)
    except (TypeError, ValueError):
        residual = 0.0
    if db is not None:
        write_back_stop_loss(db, symbol, stop_price, is_short=is_short)
    if residual > 0:
        # A whole-share floor stop landed but a sub-share sliver is still
        # gapped. Reported as NOT repaired — not because nothing happened,
        # but because the gap is real and smaller, not gone. The broker
        # snapshot the NEXT sweep takes reflects the partial cover on its own
        # and reclassifies this symbol "partial" rather than "none"; this
        # pass keeps escalating instead of going quiet on a still-real gap.
        logger.warning(
            "coverage repair PARTIAL for %s: covered %.4f of %.4f "
            "uncovered share(s) at stop $%.2f — %.4f share(s) still "
            "gapped; next sweep will re-check",
            symbol,
            uncovered_qty - residual,
            uncovered_qty,
            stop_price,
            residual,
        )
        return _refuse(
            outcome,
            f"only {uncovered_qty - residual:.4f} of {uncovered_qty:.4f} uncovered share(s) could be covered",
            code="partial_cover",
            record=rec,
            stop_price=stop_price,
            placed={
                k: result.get(k)
                for k in (
                    "id",
                    "covered_qty",
                    "uncovered_qty",
                    "gtc_qty",
                    "day_qty",
                    "gtc_stop_id",
                    "day_stop_id",
                )
            },
        )
    logger.warning(
        "COVERAGE REPAIRED: %s — placed protective %s stop-MARKET coverage "
        "for %.4f uncovered share(s) at the recorded %s stop $%.2f (GTC over "
        "the whole shares, DAY over any sub-share remainder)",
        symbol,
        protective_side,
        uncovered_qty,
        opening,
        stop_price,
    )
    return True


# ---------------------------------------------------------------------------
# A recorded stop the tape has already crossed (k40, measured 2026-10-12).
# ---------------------------------------------------------------------------
# Sub-share remainders carry DAY stops, re-placed by this repair at the open.
# When the price gapped through the recorded level overnight, re-placing it
# was refused as "would fire immediately" and handed to a reviewer that is
# suspended, so the shares sat with NO stop and NO exit. The stop decision
# was made when the level was recorded; the tape crossing it is that
# decision triggering. So the repair executes it: a market order on the
# protective side for exactly the uncovered quantity that stop protected,
# through the desk's protected sell path (write-ahead, fresh re-size, kill
# switch, quantity floor, owner oversell gate). Every precondition fails CLOSED to the old
# refusal, and every outcome writes the same durable row a refusal does.


def _breach_blocker(
    *,
    recorded_level: float,
    resting_stops: list | None,
    is_short: bool,
    closer_supplied: bool = False,
    **checks: Any,
) -> tuple[str | None, float, str]:
    """`(blocker, level, level_source)`: blocker None when the breach may be
    executed against `level`.

    The CURRENT trailed stop is the one the broker holds: when a protective
    stop rests on the symbol its level is authoritative over the recorded
    row (a trail that landed at the broker but not on the row, or the
    reverse, must not trigger an exit off the stale number). Resting legs
    that disagree with each other, or a resting level the print has NOT
    crossed, block execution.
    """
    blocker = _breach_precondition(**checks)
    if blocker is None and not closer_supplied:
        # The execution layer may not import the protection layer; the exit
        # path is injected by a caller above both. None injected: fail closed.
        blocker = "this caller supplied no exit path for a breached stop"
    levels = set()
    for spec in resting_stops or []:
        level = spec.get("stop_price") if isinstance(spec, dict) else None
        if isinstance(level, (int, float)) and math.isfinite(level) and level > 0:
            levels.add(round(float(level), 4))
    if not levels:
        return blocker, recorded_level, "recorded_row"
    if blocker is not None:
        return blocker, recorded_level, "recorded_row"
    if len(levels) > 1:
        return "the broker holds resting stops at different levels, so the current stop is ambiguous", 0.0, ""
    level = levels.pop()
    print_price = float(checks["stamped"].price)
    if not (print_price >= level if is_short else print_price <= level):
        return (
            f"the broker's resting stop ${level:,.2f} is the current level and today's print has not crossed it, "
            f"so the recorded level is stale",
            level,
            "broker_resting_stop",
        )
    return None, level, "broker_resting_stop"


def _breach_precondition(
    *,
    db: Any,
    symbol: str,
    stamped: Any,
    price: float | None,
    level_was_derived: bool,
    live_price_cls: type,
) -> str | None:
    """None when the breach may be executed, else the plain reason it may not.

    Executes only against (1) a RECORDED level, never one this repair derived;
    (2) a real trade print, stamped today, printed inside the regular session,
    and the very price the wrong-side test used — a snapshot substitute, a
    quote, a pre-market or after-hours print, or yesterday's close never
    qualifies; (3) a session open now, so the order meets today's tape rather
    than queuing; (4) the CURRENT level — an owed out-of-hours amend still
    pending for the symbol means the recorded level is superseded, and an
    unreadable owed-amend table is treated the same way.
    """
    from src.trading_calendar import in_regular_session

    if level_was_derived:
        return "no stop was ever recorded for it — the level was derived by this repair, not decided by the desk"
    print_ok = (
        isinstance(stamped, live_price_cls)
        and stamped.source == "last_trade"
        and stamped.is_today_print
        and isinstance(stamped.trade_at, datetime)
        and stamped.trade_at.tzinfo is not None
        and in_regular_session(stamped.trade_at)
        and price == stamped.price
    )
    if not print_ok:
        return "no regular-session trade print from today could be read to confirm the breach"
    if not in_regular_session():
        return "the regular session is not open, so a market order could not meet today's tape"
    if db is None:
        return "the owed-stop record could not be checked, so the recorded level may be superseded"
    try:
        from src.storage.trades import pending_stop_amends_store

        owed = pending_stop_amends_store.get_all(db._trades())
    except Exception as exc:  # noqa: BLE001
        logger.warning("breached-stop check: owed-stop read failed for %s: %s", symbol, exc)
        return "the owed-stop record could not be read, so the recorded level may be superseded"
    if any(_same_symbol(row.get("symbol"), symbol) for row in owed or [] if isinstance(row, dict)):
        return "a newer owed stop level is still pending for it, so the recorded level is superseded"
    return None


def _same_symbol(a: Any, b: Any) -> bool:
    """Symbols equal ignoring case and the broker's class-share separator."""

    def norm(x: Any) -> str:
        return "".join(ch for ch in str(x or "").upper() if ch.isalnum())

    return bool(norm(a)) and norm(a) == norm(b)


def _working_exit_order(broker: Any, symbol: str, side: str) -> tuple[bool, str | None]:
    """`(readable, order_id)` of a working NON-stop order on the exit side.

    A resting protective stop on the whole shares is not an exit in flight; a
    market or limit order on the same side is, and a second one would
    double-sell. An unreadable order book is `readable=False`: never guess.
    """
    try:
        readable, orders = broker.list_open_orders_checked()
    except Exception as exc:  # noqa: BLE001
        logger.warning("breached-stop check: open-order read failed for %s: %s", symbol, exc)
        return False, None
    if not readable:
        return False, None
    for order in orders or []:
        o_side = getattr(order, "side", None)
        o_side = str(getattr(o_side, "value", o_side) or "").lower()
        o_type = getattr(order, "order_type", None) or getattr(order, "type", None)
        o_type = str(getattr(o_type, "value", o_type) or "").lower()
        if _same_symbol(getattr(order, "symbol", None), symbol) and o_side == side and "stop" not in o_type:
            return True, str(getattr(order, "id", "") or "unknown")
    return True, None


def _held_now(broker: Any, symbol: str, is_short: bool) -> float | None:
    """What a closing order may close right now, from a FRESH position read:
    the long for a sell, the short for a cover. None when the read fails."""
    try:
        signed = sum(
            float(p.qty) for p in list(broker.get_positions()) if _same_symbol(getattr(p, "symbol", None), symbol)
        )
    except Exception as exc:  # noqa: BLE001
        from src.sentinel.guarded import record_guarded_pass

        record_guarded_pass((broker, None), "stop_repair.breach_held_reread", exc, context={"symbol": symbol})
        logger.warning("breached-stop exit: position re-read failed for %s: %s", symbol, exc)
        return None
    closable = -signed if is_short else signed
    return closable if closable > 0 else 0.0


def _execute_breached_stop(
    *,
    broker: Any,
    db: Any,
    symbol: str,
    uncovered_qty: float,
    is_short: bool,
    stop_price: float,
    level_source: str,
    close_breached: Callable[..., str | None],
    stamped: Any,
    outcome: dict | None,
    rec: dict,
) -> bool:
    """Execute the crossed stop through the protected sell path; one durable
    row either way. Always False: no stop was placed, so the gap is not
    'repaired' — it is being exited.

    Quantity: the uncovered quantity that stop protected, capped at a FRESH
    position read (a whole-share stop that filled at the open leaves only the
    remainder; nothing left means nothing sent), and re-capped by the
    protected path's own read immediately before submit. A long remainder is
    a PARTIAL_SELL (the trim path keeps the whole-share stop resting); the
    whole position is a SELL or COVER. A short is never covered in part — the
    desk keeps a short whole or closes it whole — so a short still partly
    held by a resting stop is left to that stop, and a cover can never
    exceed the short held, so it cannot flip the position long.
    """
    from src.execution.order_gates import _FRACTIONAL_QTY_EPSILON

    side = "buy" if is_short else "sell"
    print_price = float(stamped.price)
    through = f"today's print ${print_price:,.2f} is through the current stop ${stop_price:,.2f} ({level_source})"
    evidence = {
        "stop_price": stop_price,
        "level_source": level_source,
        "print_price": print_price,
        "print_at": stamped.trade_at.isoformat(),
        "side": side,
        "qty": uncovered_qty,
    }

    def record(tail: str, code: str, placed: dict) -> bool:
        if isinstance(outcome, dict):
            outcome["breached_stop_exit"] = placed
        return _refuse(outcome, through + tail, code=code, record=rec, stop_price=stop_price, placed=placed)

    readable, working_id = _working_exit_order(broker, symbol, side)
    if not readable:
        tail = ", but the open orders could not be read to rule out an exit already working, so nothing was sent"
        return record(tail, "breached_stop_orders_unreadable", evidence)
    if working_id is not None:
        tail = f" and an exit order ({working_id}) is already working, so no second order was sent"
        return record(tail, "breached_stop_exit_already_working", {**evidence, "order_id": working_id})
    held = _held_now(broker, symbol, is_short)
    if held is None:
        return record(
            ", but the position could not be re-read, so nothing was sent", "breached_stop_held_unreadable", evidence
        )
    evidence["held_now"] = held
    if held <= _FRACTIONAL_QTY_EPSILON:
        tail = ", but nothing is held any more (a resting stop already closed it), so nothing was sent"
        return record(tail, "breached_stop_nothing_held", evidence)
    qty = min(abs(float(uncovered_qty)), held)
    whole_exit = held - qty <= _FRACTIONAL_QTY_EPSILON
    if is_short and not whole_exit:
        tail = (
            f"; {held:.4f} share(s) are still short and the desk never covers part of a short, so the resting "
            f"stop owns this exit and nothing was sent"
        )
        return record(tail, "breached_stop_partial_cover_refused", evidence)
    label = ("COVER" if is_short else "SELL") if whole_exit else "PARTIAL_SELL"
    evidence.update(qty=qty, label=label, print_price=print_price)
    try:
        order_id = close_breached(symbol, qty, is_short, label, evidence)
    except Exception as exc:  # noqa: BLE001
        logger.error("breached-stop exit for %s raised: %s", symbol, exc)
        evidence["refusal"] = f"the exit path raised: {exc}"
        order_id = None
    if not order_id:
        why = evidence.get("refusal") or "the order was not accepted"
        logger.error("BREACHED STOP NOT EXECUTED for %s: the protected sell path declined it (%s)", symbol, why)
        return record(f"; the protected sell path declined the {label} ({why})", "breached_stop_exit_refused", evidence)
    placed = {**evidence, "order_id": order_id}
    try:
        db.insert_trade(
            symbol=symbol,
            action=label,
            qty=qty,
            price=print_price,
            reasoning=f"stop executed at the open: {through}",
            run_id=str(rec.get("caller") or "stop_repair"),
            broker_order_id=order_id,
            fill_status="submitted",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("breached-stop exit for %s: ledger row failed (order %s is live): %s", symbol, order_id, exc)
        placed["ledger_error"] = str(exc) or type(exc).__name__
    logger.warning(
        "BREACHED STOP EXECUTED: %s print $%.2f is through the current stop $%.2f (%s) — %s %.4f share(s), order %s",
        symbol,
        print_price,
        stop_price,
        level_source,
        label,
        qty,
        order_id,
    )
    tail = f", so the stop was executed: market {label} of {qty:.4f} share(s), order {order_id}"
    return record(tail, "breached_stop_executed", placed)
