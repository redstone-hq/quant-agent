"""Protective-stop snapshot and cancel, lifted verbatim from AlpacaBroker.

`snapshot_protective_stops`, `cancel_snapshotted_stops`,
`cancel_protective_stops` and `cancel_stray_protective_stops` as module
functions taking the broker first. `AlpacaBroker` keeps a same-named one-line
shim for each, and every other broker member (including the composed calls
between these four) is reached through `broker.<name>`, so a class-level patch
on `AlpacaBroker` still bites.
"""

from __future__ import annotations

import logging

from src.stop_cancel_outcome import StopCancelOutcome, StopCoverageLost, settle_cancel
from src.sentinel.guarded import record_guarded_pass

# The moved bodies log under the broker's logger name, and the move must not
# change what callers and tests see.
logger = logging.getLogger("src.execution.broker")


def snapshot_protective_stops(
    broker,
    symbol: str,
    *,
    side: str = "sell",
) -> tuple[bool, list[dict]]:
    """List + snapshot open protective stop orders WITHOUT cancelling them.

    audit F1 (review #1): the write-ahead recovery row must be
    persisted BEFORE any broker mutation. Splitting the read
    (snapshot) from the write (cancel) lets the pipeline do
    snapshot → persist WAL → cancel, so a process kill anywhere
    from the cancel onward is recoverable. Previously the WAL insert
    ran AFTER cancel_protective_stops had already cancelled the
    stops at the broker — a kill in that window left a naked
    position with no recovery intent.

    `side` is the STOP order's own side: "sell" (default) finds the
    stops protecting a long; "buy" finds the stops protecting a short.
    Every existing caller cancels/restores/re-protects a long being
    SOLD, so the default is unchanged; the coverage reconciler is the
    one caller that passes `side="buy"` to check a short.

    Returns ``(ok, specs)``. ``ok`` is FALSE when the broker's own
    order listing failed — board item 172, and this used to be the
    single most dangerous lie on the read path.

    It was documented as "always True", because a listing API error was
    swallowed by `_list_open_protective_stop_orders` and surfaced as an
    empty list. An empty list means "this position has no protective
    stop", so a broker outage was reported to the desk as a CONFIRMED
    NAKED POSITION — and the coverage reconciler then repaired against
    it, placing a full-size stop on top of a live stop it could not
    see. Both the strongest possible false statement about loss
    protection and a duplicate-protection write, from one swallowed
    exception.

    Callers that ignore `ok` are no worse off than before: `specs` is
    still empty in that case. Callers that read it can tell "there is
    no stop" from "I could not ask", which is the whole distinction
    item 172 exists for.

    NOT true of every caller, and the first version of this docstring
    said it was. `TradingPipeline._cancel_stops_with_write_ahead` reads
    `ok` and skips the SELL on False, so making this return False where
    it previously always returned True changed the EXIT path as well as
    the read path — five exit call sites, none of them reviewed when
    that change was made. The test pinning that skip
    (`test_cancel_stops_with_write_ahead_skips_on_snapshot_failure`,
    added 2026-05-16) pinned unreachable code for four months, because
    until board item 172 `ok` could not be False [measured from
    `git log -S`, 2026-09-23]. Nobody chose that behaviour; it was
    inherited. What it does now is decided at that call site and
    documented there.

    THE READ IS NOT RETRIED BY THE DESK (2026-10-14). Its retry loop
    borrowed the stop-placement attempt count and backoff, which were
    arbitrary and are deleted; it had no number of its own. alpaca-py's
    REST client already retries 429 and 504 itself (`RESTClient`
    `DEFAULT_RETRY_ATTEMPTS` 3, 3 s wait). Any other listing failure
    reports UNKNOWN at once, exactly as the last failed attempt did.
    """
    errors: list = []
    stops = broker._list_open_protective_stop_orders(
        symbol,
        side=side,
        errors=errors,
    )
    if errors:
        logger.error(
            "snapshot_protective_stops: could not READ %s's protective stops (%s) — reporting UNKNOWN, not 'no stop'.",
            symbol,
            "; ".join(errors),
        )
        return False, []
    if not stops:
        return True, []
    specs: list[dict] = []
    for order in stops:
        spec = broker._snapshot_stop_order(order)
        if spec:
            specs.append(spec)
    return True, specs


#: `StopCancelOutcome.detail` prefix: a cancel failed and the fresh position
#: read shows nothing held, so the stop filled and nothing was rolled back.
POSITION_GONE = "position_gone"


def _held_now(broker, symbol: str) -> float | None:
    """Signed qty held in `symbol` from a fresh broker read; None if unreadable."""
    want = symbol.strip().upper().replace("/", "")
    try:
        positions = list(broker.get_positions())
        held = sum(float(p.qty) for p in positions if str(p.symbol).strip().upper().replace("/", "") == want)
        record_guarded_pass(broker, "cancel_snapshotted_stops.position_read", context={"symbol": symbol})
    except Exception as exc:  # noqa: BLE001 - unreadable keeps the existing rollback
        record_guarded_pass(
            broker,
            "cancel_snapshotted_stops.position_read",
            exc,
            log=logger,
            context={"symbol": symbol, "effect": "position unknown; the rollback runs as before"},
        )
        return None
    return 0 if abs(held) < 1e-9 else held


def _stop_side(spec: dict) -> str:
    """The side a restored leg is placed on: the spec's own side, else 'sell'
    (what `_restore_stop_orders` places when no side is passed)."""
    return "buy" if str(spec.get("side") or "sell").lower().endswith("buy") else "sell"


def _restore_at_held(broker, held: float):
    """A rollback that restores the cancelled legs only onto the side HELD now,
    and never for more shares than are held.

    A failed cancel can be a stop that filled in part: restoring the old full
    size would rest more stop than shares. A SELL stop protects a long and a
    BUY stop a short, so a leg whose side does not match the signed position
    (e.g. the book flipped short) is NOT restored -- it would add to the
    position instead of protecting it. Legs are kept in order and the last one
    kept is cut to fit; a dropped leg is not reported as lost coverage (no held
    share needs it). Failures map back to the original specs.
    """

    def restore(symbol, cancelled):
        room = {"sell": max(held, 0.0), "buy": max(-held, 0.0)}
        sized, origin = [], []
        for spec in cancelled:
            side = _stop_side(spec)
            q = abs(float(spec.get("qty", 0) or 0))
            take = min(q, room[side])
            if take <= 1e-9:
                continue
            sized.append(spec if take == q else {**spec, "qty": take})
            origin.append(spec)
            room[side] -= take
        if len(sized) < len(cancelled) or any(x is not y for x, y in zip(sized, origin, strict=True)):
            logger.warning(
                "cancel_snapshotted_stops: %s rollback sized to the %s share(s) held now (signed), not the snapshot",
                symbol,
                held,
            )
        restored, lost = 0, set()
        for side in ("sell", "buy"):
            group = [x for x in sized if _stop_side(x) == side]
            if not group:
                continue
            n, failed = broker._restore_stop_orders(symbol, group, **({} if side == "sell" else {"side": side}))
            restored += n
            lost |= {id(x) for x in failed}
        return restored, [o for x, o in zip(sized, origin, strict=True) if id(x) in lost]

    return restore


def cancel_snapshotted_stops(
    broker,
    symbol: str,
    specs: list[dict],
) -> StopCancelOutcome:
    """Cancel pre-snapshotted protective stops by id.

    Returns a :class:`StopCancelOutcome`, never a bool. Three states the
    caller MUST distinguish: ``cleared`` (SELL may proceed), not cleared
    with ``coverage_shrank`` False (nothing moved on net, every share is
    still covered), and ``coverage_shrank`` True (a cancel failed, the
    rollback also failed, and ``outcome.unprotected`` names the shares
    that are naked at the broker right now — skip the SELL AND keep the
    recovery row for them). That third state is what the old bare
    ``False`` hid: two callers read it as "nothing moved" and deleted
    the only durable intent that could re-attach the missing stops.
    """
    if not specs:
        return StopCancelOutcome.nothing_to_do(symbol)
    cancelled: list[dict] = []
    untouched: list[dict] = []
    cancel_failed: list[dict] = []
    for spec in specs:
        sid = spec.get("id")
        if not sid:
            # Never sent to the broker: still alive, still covering.
            untouched.append(spec)
            continue
        try:
            from src.execution.broker_parts.cancel_confirm import now_iso

            spec["cancel_sent_at"] = now_iso()
            broker.client.cancel_order_by_id(sid)
            cancelled.append(spec)
            record_guarded_pass(broker, "cancel_snapshotted_stops.cancel", context={"symbol": symbol, "order": sid})
        except Exception as exc:
            record_guarded_pass(
                broker,
                "cancel_snapshotted_stops.cancel",
                exc,
                log=logger,
                context={"symbol": symbol, "order": sid, "effect": "stop left resting; rollback decides coverage"},
            )
            cancel_failed.append(spec)
    held = _held_now(broker, symbol) if cancel_failed else None
    if cancel_failed and held == 0:
        # A cancel failed AND the fresh read shows nothing held: the stop
        # FILLED (a filled order cannot be cancelled). Rolling the cancelled
        # legs back would rest closing stops on a position that no longer
        # exists — on the opposite side of an empty book that is an opening
        # order. Nothing is restored; the outcome says why, by name.
        logger.error(
            "cancel_snapshotted_stops: %s cancel failed and the position is gone (stop filled) — "
            "nothing is rolled back onto an empty position",
            symbol,
        )
        return StopCancelOutcome(
            symbol=symbol,
            requested=tuple(specs),
            still_resting=tuple(cancel_failed) + tuple(untouched),
            detail=f"{POSITION_GONE}: {len(cancel_failed)}/{len(specs)} cancel(s) failed and nothing is held",
        )
    return settle_cancel(
        symbol,
        specs,
        cancelled,
        untouched,
        cancel_failed,
        broker._restore_stop_orders if held is None else _restore_at_held(broker, held),
        logger,
    )


def cancel_protective_stops(broker, symbol: str) -> tuple[bool, list[dict]]:
    """Cancel all open SELL stop orders for one symbol so a fresh exit
    order has free shares to work with.

    Returns ``(success, cancelled_specs)``:
      - ``success`` is True iff every stop was cancelled cleanly (or
        none existed). Caller should skip the SELL on False.
      - ``cancelled_specs`` is the list of stop snapshots (qty,
        stop_price, limit_price) that were successfully cancelled.
        Caller uses this to:
          1. ``_restore_stop_orders`` if the SELL is rejected by
             the broker (rollback the cancellation so coverage is
             preserved).
          2. ``_submit_stop_limit_order`` on the residual qty after
             a *partial* exit (TAKE_PROFIT / REDUCE / PARTIAL_SELL)
             — without this, the residual position rides naked
             until the next session re-attaches an OTO stop.

    Why this exists: Alpaca rejects new SELL orders when shares are
    held_for_orders by an existing protective stop — the OTO stop-loss
    leg attached to a morning BUY, or a TRAIL_STOP placed by midday.
    Without clearing those holds first, REDUCE / SELL / EMERGENCY_SELL
    / TAKE_PROFIT all surface as 'insufficient qty available' rejects
    (2026-04-25 AMZN incident, related_orders=[<TRAIL_STOP id>]).

    On partial cancel failure (some succeed, then one raises) the
    already-cancelled stops are restored before returning False —
    same rollback discipline as ``replace_stop_loss``. The caller
    won't proceed with the SELL anyway, so leaving partial-cancelled
    state at the broker would just shrink coverage for no gain.

    Now composed from snapshot_protective_stops +
    cancel_snapshotted_stops (audit F1 review #1). The external
    contract is unchanged: no stops -> (True, []); all cancelled ->
    (True, specs); partial failure -> rolled back, (False, []).
    Direct callers/tests are unaffected; SELL paths use the
    pipeline's write-ahead orchestrator instead so the recovery row
    lands before the cancel.
    """
    ok, specs = broker.snapshot_protective_stops(symbol)
    if not ok:
        return False, []
    if not specs:
        return True, []
    outcome = broker.cancel_snapshotted_stops(symbol, specs)
    if not outcome.cleared:
        if outcome.coverage_shrank:
            # This composite has no channel for a partial loss, so it
            # must not quietly answer "nothing happened". Direct callers
            # get the specs they now have to re-protect.
            raise StopCoverageLost(outcome)
        return False, []
    return True, specs


def cancel_stray_protective_stops(
    broker,
    symbol: str,
    *,
    side: str = "sell",
) -> int:
    """Cancel every protective stop still resting on a symbol that is
    now FLAT. Returns the count cancelled.

    Board item 127(b), owner ruling 2026-09-25: a forced/emergency exit
    fires IMMEDIATELY and never waits on stop-work, so a concurrent
    stop-repair can re-add a protective stop inside the cancel-then-sell
    window. Once the exit takes the position to zero shares that stop is
    a stray — it protects nothing, the reprotect path never sees it (it
    was placed AFTER the pre-sell snapshot, so it is not in the sell's
    ``cancelled_specs``), and ``_reconcile_stop_coverage`` skips flat
    symbols outright — so nothing else would ever clear it, and a stop
    left resting on zero shares can later elect into an unintended
    short. This is the cheap cleanup the ruling assumes in place of the
    rejected lock-wait.

    Unlike ``cancel_snapshotted_stops`` there is NO rollback: the
    position is flat, so there is nothing to protect and a "restore"
    would only re-place the very stray order being removed. Best-effort
    and side-correct (``side="buy"`` finds the buy-stops that had
    protected a short); a cancel that raises is logged and never blocks
    the others, and the whole thing degrades to a no-op — the exit has
    already succeeded and must not be undone by a housekeeping error.
    """
    try:
        ok, specs = broker.snapshot_protective_stops(symbol, side=side)
        record_guarded_pass(broker, "cancel_stray_protective_stops.list", context={"symbol": symbol, "side": side})
    except Exception as exc:  # noqa: BLE001
        record_guarded_pass(
            broker,
            "cancel_stray_protective_stops.list",
            exc,
            log=logger,
            context={
                "symbol": symbol,
                "side": side,
                "effect": "a stray stop may still rest; the operator should confirm it is gone",
            },
        )
        return 0
    if not ok or not specs:
        return 0
    cancelled = 0
    for spec in specs:
        sid = spec.get("id")
        if not sid:
            continue
        try:
            broker.client.cancel_order_by_id(sid)
            cancelled += 1
            record_guarded_pass(
                broker, "cancel_stray_protective_stops.cancel", context={"symbol": symbol, "order": sid, "side": side}
            )
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                broker,
                "cancel_stray_protective_stops.cancel",
                exc,
                log=logger,
                context={
                    "symbol": symbol,
                    "order": sid,
                    "side": side,
                    "effect": "a stop may still rest on a flat position; clear it by hand",
                },
            )
    if cancelled:
        logger.info(
            "Cancelled %d stray protective %s-stop(s) on now-flat %s "
            "(item 127(b): a repair re-added protection inside the "
            "cancel-then-sell window)",
            cancelled,
            side,
            symbol,
        )
    return cancelled
