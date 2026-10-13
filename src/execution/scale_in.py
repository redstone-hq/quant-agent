"""Long scale-in path B: cancel the resting protective sell, confirm, buy, rearm.

Owner ruling 2026-09-15: a BUY add on a name that already has a resting
protective sell (stop / stop_limit) is allowed ONLY through this sequence,
never by silently refusing the add.

    1. Snapshot the protective sell and persist a WAL recovery row.
    2. Cancel it.
    3. Confirm the cancel via the broker's own order status — do not assume.
       (Was `trade_updates` first; that socket is off since 2026-09-17, so
       this is the bounded REST wait. Same call, same window.)
    4. Submit the BUY add.
    5. On any fill (including partial): place ONE protective sell covering
       the broker's FULL position quantity at the existing protection level
       (most-protective of the cancelled stop and the add's own stop).
    6. If rearm fails: fail-closed owner alert. Do not leave the position
       naked quietly.

WHY THIS EXISTS (Alpaca wash-trade / opposite-side block)
---------------------------------------------------------
A resting SELL stop and a new BUY cannot both be working on the same
symbol. The old whole-book daily breaker cancelled every protective stop,
failed to sell on a gap, and restored — leaving an unprotected window on
the exact day it existed for (`docs/INCIDENT_HISTORY.md`, 2026-09-14).
This path is the same cancel-to-free-shares shape, but:

  * it is one symbol, one add, not the whole book;
  * cancel is confirmed before the BUY is sent;
  * a crash mid-sequence is recovered from the WAL row using the broker's
    CURRENT quantity, never the cancelled-stop size or the add's fill size;
  * trail / in-session coverage repair / the coverage watchdog skip a
    symbol that has a live scale-in WAL row so they cannot re-place the
    stop we just cancelled (which would re-create the wash-trade block)
    and cannot resize it while the add is in flight.

SHORT ADDS
----------
Built (owner-approved), as the mirror of the long path: `prepare_short_add`
cancels a resting *buy*-stop, submits the SELL add, and rearms a buy-stop
covering the FULL enlarged short. Most-protective for a short is the LOWEST
trigger (covers soonest); the protective order is a stop-MARKET, so it
carries no limit. It adds two guards
the long path does not need: a wash-trade guard (the SELL add cannot rest
against a foreign working BUY) and an abort when a cancelled buy-stop FILLED
(the short was covered — do not restore a stop onto a flat name, do not open
a new short). A SHORT that opens a flat name is unchanged. Missing short
stops are repaired separately (item 73, closed).

No new magic percentages. The stop trigger is the live protective level
already at the broker (or the add's reviewed stop if that is tighter).
The re-armed protective stop is a stop-MARKET (guaranteed exit, owner
ratified 2026-09-25) with no limit and no stop-limit fallback.
`execution.repeg_enabled` is not consulted and is not changed.
"""

from __future__ import annotations

import json
from src.stop_cancel_outcome import handle_add_cancel
from src.alert_claims import claim_typed_alert
from src.execution.scale_in_loud import record_scale_in, record_scale_in_fault
from src.execution.broker_parts.cancel_confirm import (  # noqa: F401 (re-export; patch targets)
    _CANCEL_CONFIRMED,
    _STOP_FILLED,
    _confirm_cancels_status,
    cancelled_stop_specs,
)
from src.execution.held_qty import (  # noqa: F401 (re-export; patch targets)
    _SESSION_LOCK_DIR,
    broker_position_qty,
    cover_qty_for_rearm,
    held_signed_qty,
    list_open_entry_ids,
    trading_session_lock_held,
)
from src.execution.scale_in_readers import (  # noqa: F401 (re-export)
    WAL_SCALE_IN_SENTINEL,
    pending_scale_in_rows_from_path,
    pending_scale_in_symbols_from_path,
)
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LongAddPrep:
    """Outcome of preparing a long BUY that may be an add to a held name."""

    is_scale_in: bool = False
    cancelled: bool = False
    wal_row_id: int | None = None
    specs: list[dict] = field(default_factory=list)
    intended_stop: float = 0.0
    held_qty_before: float = 0.0
    skip_reason: str | None = None
    skip_detail: str = ""
    #: Side of the PROTECTIVE stop this prep cancels/restores: "sell" for a
    #: long add (sell-stop below), "buy" for a short add (buy-stop above).
    #: `restore_after_failed_add` / `restore_cancelled_stops` re-place on
    #: this side so a short's stop is never restored as a sell.
    side: str = "sell"
    #: `time.monotonic()` at the instant the BROKER acknowledged the cancel
    #: as terminal (board item 193). The unprotected window is measured from
    #: here to the broker's acknowledgement of the rearm, so the figure bounds
    #: real exposure rather than database write times. None when nothing was
    #: cancelled, which is exactly the case with no window to measure.
    cancel_confirmed_at: float | None = None

    @classmethod
    def not_scale_in(cls) -> "LongAddPrep":
        return cls()


def most_protective_long_stop(prices: list[float]) -> float:
    """Tightest long stop: highest trigger below price. 0 if none usable."""
    usable = [float(p) for p in prices if isinstance(p, (int, float)) and p > 0]
    return max(usable) if usable else 0.0


def most_protective_short_stop(prices: list[float]) -> float:
    """Tightest SHORT stop: the LOWEST trigger above entry — it covers the
    short soonest (a buy-stop fires as price rises). Mirror of
    `most_protective_long_stop` with min instead of max. 0.0 if none usable.
    """
    usable = [float(p) for p in prices if isinstance(p, (int, float)) and p > 0]
    return min(usable) if usable else 0.0


def intended_specs(cancelled: list[dict], intended_stop: float) -> list[dict]:
    """Cancelled snapshots plus the add's own stop, for drain most-protective."""
    specs = [dict(s) for s in cancelled]
    if intended_stop > 0:
        specs.append(
            {
                "id": None,
                "qty": 0.0,
                "stop_price": float(intended_stop),
                "limit_price": None,
                "role": "intended",
            }
        )
    return specs


def confirm_protective_cancels(broker: Any, specs: list[dict]) -> tuple[bool, str]:
    """Wait until each cancelled stop is terminal, via `wait_for_order_terminal`.

    Thin wrapper over `_confirm_cancels_status`: ``(True, "")`` only when
    every spec with an id reached a cancelled-like terminal state. Both a
    fill and an unconfirmed status collapse to ``(False, detail)`` here —
    the long path treats them the same (skip the add, restore). The short
    path calls `_confirm_cancels_status` directly to tell a fill (short
    covered — do not restore) from a merely-unconfirmed cancel.
    """
    status, detail = _confirm_cancels_status(broker, specs)
    return status == "confirmed", detail


def rearm_full_position_stop(
    broker: Any,
    *,
    symbol: str,
    qty: float,
    stop_price: float,
    db: Any = None,
) -> dict | None:
    """Place ONE protective SELL covering `qty` at `stop_price`.

    Routes through `_submit_protective_stop_retrying` so hybrid GTC+DAY
    fractional legs and the §11.1 retry burst are the same as every other
    protective-stop path. No take-profit, no bracket, no invented buffer.
    On accept, writes `stop_price` back onto the opening row when `db` is
    given — the rearmed level can be tighter than the add's own stop.
    """
    # docs/WORK.md item 88: a non-finite trigger passed the old `<= 0` test
    # (NaN/Inf compare False) and reached the broker, where quantization
    # turned it into None. Returning None here is the caller's failure
    # signal — the WAL row stays and the gap keeps escalating.
    from src.execution.stop_records import STOP_USABLE, classify_stop_price

    if qty <= 0 or classify_stop_price(stop_price)[0] != STOP_USABLE:
        logger.error(
            "scale-in rearm REFUSED for %s: qty=%r stop=%r is not a "
            "placeable protective stop — nothing placed, the gap stays open",
            symbol,
            qty,
            stop_price,
        )
        return None
    result = broker._submit_protective_stop_retrying(
        symbol=symbol,
        qty=qty,
        stop_price=stop_price,
        side="sell",
    )
    from src.execution.stop_records import accepted_stop_order, write_back_stop_loss

    if accepted_stop_order(result) and db is not None:
        write_back_stop_loss(db, symbol, stop_price, is_short=False)
    return result


def rearm_full_position_short_stop(
    broker: Any,
    *,
    symbol: str,
    qty: float,
    stop_price: float,
    db: Any = None,
) -> dict | None:
    """Place ONE protective BUY-stop covering `qty` at `stop_price` for a short.

    Mirror of `rearm_full_position_stop` for the short side. A short is
    protected by a BUY stop ABOVE the entry; the order is a stop-MARKET and
    carries no limit. Same `classify_stop_price` / `qty <= 0`
    refusal (docs/WORK.md item 88 — a non-finite trigger must not reach the
    broker). Write-back records the short row (`is_short=True`) so R-multiple
    measures the short that was actually made (board item 73, closed).
    """
    from src.execution.stop_records import STOP_USABLE, classify_stop_price

    if qty <= 0 or classify_stop_price(stop_price)[0] != STOP_USABLE:
        logger.error(
            "short scale-in rearm REFUSED for %s: qty=%r stop=%r is not a "
            "placeable protective stop — nothing placed, the gap stays open",
            symbol,
            qty,
            stop_price,
        )
        return None
    result = broker._submit_protective_stop_retrying(
        symbol=symbol,
        qty=qty,
        stop_price=stop_price,
        side="buy",
    )
    from src.execution.stop_records import accepted_stop_order, write_back_stop_loss

    if accepted_stop_order(result) and db is not None:
        write_back_stop_loss(db, symbol, stop_price, is_short=True)
    return result


def restore_cancelled_stops(
    broker: Any,
    symbol: str,
    specs: list[dict],
    *,
    side: str = "sell",
) -> bool:
    """Put the snapshotted protective stops back. True when none remain failed.

    `side` is the stops' own side: "sell" (default) restores a long's
    sell-stops; "buy" restores a short's buy-stops. A short add that then
    fails must not have its buy-stop restored as a sell.
    """
    to_restore = cancelled_stop_specs(specs)
    if not to_restore:
        return True
    try:
        _restored, failed = broker._restore_stop_orders(
            symbol,
            to_restore,
            check_idempotency=True,
            side=side,
        )
    except Exception as exc:  # noqa: BLE001
        record_scale_in(broker, "restore_cancelled_stops", exc, symbol=symbol)
        return False
    record_scale_in(broker, "restore_cancelled_stops")
    return not failed


def discharge_scale_in_wal(db: Any, wal_row_id: int | None) -> None:
    if wal_row_id is None:
        return
    try:
        db.delete_pending_protection_restore(wal_row_id)
    except Exception as exc:  # noqa: BLE001
        record_scale_in(db, "discharge_wal", exc, wal_row=wal_row_id)  # the drain will retry
    else:
        record_scale_in(db, "discharge_wal")


def prepare_long_add(
    *,
    broker: Any,
    db: Any,
    symbol: str,
    positions: list | None,
    intended_stop: float,
) -> LongAddPrep:
    """Cancel-confirm a resting protective sell so a long add can submit.

    No-op (``is_scale_in=False``) when the name is not already held long.
    Scale-in with no live stop: ``is_scale_in=True`` and nothing cancelled —
    the fill path still rearms to broker full qty.
    """
    held = held_signed_qty(positions, symbol)
    if held <= 0:
        return LongAddPrep.not_scale_in()

    intended = most_protective_long_stop([intended_stop])
    prep = LongAddPrep(
        is_scale_in=True,
        held_qty_before=held,
        intended_stop=intended,
    )
    try:
        ok, specs = broker.snapshot_protective_stops(symbol, side="sell")
    except Exception as exc:  # noqa: BLE001
        record_scale_in(db, "long_add.snapshot", exc, symbol=symbol)
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = f"could not read the resting protective sell ({exc})"
        return prep
    else:
        record_scale_in(db, "long_add.snapshot")
    if not ok:
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = "could not read the resting protective sell"
        return prep
    live = cancelled_stop_specs(specs or [])
    if not live:
        # Naked add: nothing to cancel. Fill path still covers full qty.
        return prep

    live_stops = [float(s.get("stop_price") or 0) for s in live]
    prep.intended_stop = most_protective_long_stop(live_stops + [intended])
    wal_specs = intended_specs(live, prep.intended_stop)
    prep.specs = wal_specs
    try:
        wal_row_id = db.insert_pending_protection_restore(
            symbol=symbol,
            sell_order_id=WAL_SCALE_IN_SENTINEL,
            position_qty_before_sell=held,
            specs_json=json.dumps(wal_specs),
            side="sell",
        )
    except Exception as exc:  # noqa: BLE001
        record_scale_in(db, "long_add.wal_write", exc, symbol=symbol)  # the add is refused
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = "could not persist the protection-restore intent before cancel"
        return prep
    else:
        record_scale_in(db, "long_add.wal_write")
    prep.wal_row_id = wal_row_id if isinstance(wal_row_id, int) else wal_row_id

    cancel = broker.cancel_snapshotted_stops(symbol, live)
    if not handle_add_cancel(db, prep, cancel, logger, discharge_scale_in_wal):
        return prep

    confirmed, detail = confirm_protective_cancels(broker, live)
    if not confirmed:
        restored = restore_cancelled_stops(broker, symbol, live)
        if restored:
            discharge_scale_in_wal(db, prep.wal_row_id)
            prep.wal_row_id = None
        prep.skip_reason = "scale_in_cancel_unconfirmed"
        prep.skip_detail = detail
        logger.error("scale-in: %s — add skipped for %s", detail, symbol)
        return prep

    prep.cancelled = True
    prep.cancel_confirmed_at = time.monotonic()
    logger.info(
        "scale-in: cancelled and confirmed %d protective sell(s) for %s "
        "so a BUY add can submit; WAL row %s covers the unprotected window",
        len(live),
        symbol,
        prep.wal_row_id,
    )
    return prep


def prepare_short_add(
    *,
    broker: Any,
    db: Any,
    symbol: str,
    positions: list | None,
    intended_stop: float,
) -> LongAddPrep:
    """Cancel-confirm a resting protective BUY-stop so a short ADD can submit.

    The short-side mirror of `prepare_long_add` (see the module docstring's
    SHORT ADDS section). Differences from the long path, all forced by the
    side flip:

      * only a name held SHORT (``held_signed_qty < 0``) is a scale-in; a
        flat/long name returns ``is_scale_in=False`` so the caller opens a
        new short unchanged (a short on a flat name is not this path);
      * the protective stop is a BUY-stop, so snapshot / WAL / restore all
        use ``side="buy"`` and most-protective is the LOWEST trigger;
      * a WASH-TRADE GUARD runs BEFORE any cancel (the add is a SELL — a
        resting non-stop BUY would collide with it), and
      * a protective stop that FILLED during the cancel means the short was
        COVERED: the name may be flat, so the add is aborted and NO buy-stop
        is restored and the caller must NOT fall through to open a new short.

    KNOWN LIMITATIONS, shared with the long path (do not re-solve here):
      * the cancel→rearm window is unprotected and exposes the WHOLE
        position, not just the dollars being added; a short's upside is
        unbounded there, but the WAL row + cancel-confirm cover a crash.
        MEASURED 2026-09-30 over the complete production record of this
        feature (14 cancel/rearm pairs, board item 193): median 1s, worst
        4s, ZERO unpaired cancels, median $1,209 of position exposed and a
        median expected adverse move of $0.24. This line used to say "~15s",
        which no measurement ever supported and which overstated the window
        by roughly four times. The figures bound the window rather than
        measure it: they are database write times at whole-second
        resolution, not broker acknowledgements, and they price an ordinary
        few-second drift, NOT the gap or halt a protective stop exists for;
      * the borrow gate is a lifetime-cached per-asset boolean, not a locate
        count, so borrow is not re-verified at the add size;
      * borrow-fee / dividend / Reg-SHO are not modeled in paper.
    No new conviction/loss gate — the PM+risk decision IS the conviction
    gate, exactly as for a long add.
    """
    held = held_signed_qty(positions, symbol)
    if held >= 0:
        # Flat or long: not a short scale-in. The caller opens a new short.
        return LongAddPrep.not_scale_in()

    intended = most_protective_short_stop([intended_stop])
    prep = LongAddPrep(
        is_scale_in=True,
        # NEGATIVE for a short. `cover_qty_for_rearm` abs()es it — the
        # fallback bug fix above — so the rearm covers the full enlarged
        # short, not just the add.
        held_qty_before=held,
        intended_stop=intended,
        side="buy",
    )

    # WASH-TRADE GUARD (adversary). The add is a SELL (sell_short). A resting
    # non-stop BUY on this symbol — a cover-limit or take-profit — would
    # collide with it under Alpaca's opposite-side block, the same way a
    # resting SELL would with a long BUY add. Protective buy-stops are STOP
    # orders and are excluded here, so any working BUY the listing returns is
    # FOREIGN and must block the add before we cancel the protection.
    #
    # H4: FAIL CLOSED. `list_open_entry_orders_checked` returns (ok, ids) —
    # ok=False when the order listing itself FAILED, which is NOT the same as
    # a confirmed-empty (True, []). We are about to cancel protection and
    # submit a SELL; we may not do that on the unverified assumption that
    # Alpaca will bounce a self-cross (paper may not). If we cannot VERIFY
    # there is no colliding BUY, refuse the add and leave the protection up.
    try:
        listing_ok, foreign_buys = broker.list_open_entry_orders_checked(
            symbol,
            side="buy",
        )
    except (TypeError, AttributeError):
        # A broker build without the checked accessor cannot answer the guard
        # cleanly — refuse rather than ship the add without verification.
        listing_ok, foreign_buys = False, []
    if not listing_ok:
        prep.skip_reason = "short_add_wash_guard_unverified"
        prep.skip_detail = (
            "could not verify there is no colliding working BUY (the order "
            "listing failed) — refusing the short add rather than cancelling "
            "protection on the assumption Alpaca will bounce a self-cross"
        )
        logger.warning(
            "short scale-in: %s — order listing failed, cannot clear the "
            "wash-trade guard; refusing the add (fail-closed)",
            symbol,
        )
        return prep
    if foreign_buys:
        prep.skip_reason = "short_add_foreign_buy"
        prep.skip_detail = (
            "a working BUY order already rests on this short; cancelling the "
            "protective buy-stop and selling more would collide with it "
            "(Alpaca wash-trade block) — refusing the add"
        )
        logger.warning(
            "short scale-in: %s has %d foreign working BUY order(s) — refusing the add rather than colliding",
            symbol,
            len(foreign_buys),
        )
        return prep

    try:
        ok, specs = broker.snapshot_protective_stops(symbol, side="buy")
    except Exception as exc:  # noqa: BLE001
        record_scale_in(db, "short_add.snapshot", exc, symbol=symbol)
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = f"could not read the resting protective buy-stop ({exc})"
        return prep
    else:
        record_scale_in(db, "short_add.snapshot")
    if not ok:
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = "could not read the resting protective buy-stop"
        return prep
    live = cancelled_stop_specs(specs or [])
    if not live:
        # Naked short add: nothing to cancel. Fill path still covers full qty.
        return prep

    live_stops = [float(s.get("stop_price") or 0) for s in live]
    prep.intended_stop = most_protective_short_stop(live_stops + [intended])
    wal_specs = intended_specs(live, prep.intended_stop)
    prep.specs = wal_specs
    try:
        wal_row_id = db.insert_pending_protection_restore(
            symbol=symbol,
            sell_order_id=WAL_SCALE_IN_SENTINEL,
            position_qty_before_sell=held,
            specs_json=json.dumps(wal_specs),
            side="buy",
        )
    except Exception as exc:  # noqa: BLE001
        record_scale_in(db, "short_add.wal_write", exc, symbol=symbol)  # the add is refused
        prep.skip_reason = "scale_in_stop_clear_failed"
        prep.skip_detail = "could not persist the protection-restore intent before cancel"
        return prep
    else:
        record_scale_in(db, "short_add.wal_write")
    prep.wal_row_id = wal_row_id if isinstance(wal_row_id, int) else wal_row_id

    cancel = broker.cancel_snapshotted_stops(symbol, live)
    if not handle_add_cancel(db, prep, cancel, logger, discharge_scale_in_wal):
        return prep

    status, detail = _confirm_cancels_status(broker, live)
    if status == "filled":
        # A BUY-stop FIRED during the cancel — it covered SOME of the short.
        # `_confirm_cancels_status` returns on the FIRST filled spec, so with
        # a multi-lot short one lot may be covered while a SIBLING lot's
        # buy-stop was successfully cancelled and is now GONE. Discharging the
        # WAL and restoring nothing here (the old behaviour, and the H2 hole)
        # would leave that sibling lot NAKED with no recovery row.
        #
        # Source of truth is a broker position RE-READ, never the per-spec
        # order status:
        #   * confirmed FLAT  -> the whole short is covered; discharge the WAL
        #     and abort (do NOT restore a stop onto a flat name, do NOT open a
        #     new short);
        #   * still SHORT / unreadable -> rearm ONE buy-stop over the broker's
        #     CURRENT remaining short immediately (exactly the remaining qty,
        #     so no double-cover), and only discharge the WAL if that rearm is
        #     accepted; otherwise KEEP the WAL so drain / coverage-reconcile
        #     finishes the job. Never discharge-and-restore-nothing unless the
        #     re-read confirms flat.
        current = broker_position_qty(broker, symbol)
        if current is not None and abs(float(current)) < 1e-9:
            discharge_scale_in_wal(db, prep.wal_row_id)
            prep.wal_row_id = None
            prep.skip_reason = "scale_in_stop_filled"
            prep.skip_detail = f"{detail} — position confirmed flat"
            logger.warning(
                "short scale-in: %s — %s confirmed flat, add aborted",
                detail,
                symbol,
            )
            return prep
        remaining = abs(float(current)) if current is not None else 0.0
        rearmed = None
        if remaining > 0:
            rearmed = rearm_full_position_short_stop(
                broker,
                symbol=symbol,
                qty=remaining,
                stop_price=prep.intended_stop,
                db=db,
            )
        if rearmed is not None:
            discharge_scale_in_wal(db, prep.wal_row_id)
            prep.wal_row_id = None
            prep.skip_reason = "scale_in_stop_filled_partial"
            prep.skip_detail = (
                f"{detail} — short only partly covered; rearmed a buy-stop over "
                f"the remaining {remaining:g} share(s), add aborted"
            )
            logger.critical(
                "short scale-in: %s fired but %s is still short %.4f after the "
                "cancel — rearmed a buy-stop over the remaining short and "
                "aborted the add",
                detail,
                symbol,
                remaining,
            )
            return prep
        # Could not confirm flat AND could not rearm in-session: KEEP the WAL
        # so drain / coverage-reconcile rearms the remaining short. Add
        # aborted; the recovery row is the crash-window guarantee.
        prep.skip_reason = "scale_in_stop_filled_partial"
        prep.skip_detail = (
            f"{detail} — short NOT confirmed flat and could not rearm "
            "in-session; WAL row kept so the remaining short is rearmed"
        )
        logger.critical(
            "short scale-in: %s fired for %s but the short is not confirmed "
            "flat and no in-session rearm landed — WAL row %s KEPT so the "
            "remaining short is not left naked without recovery",
            detail,
            symbol,
            prep.wal_row_id,
        )
        return prep
    if status != "confirmed":
        restored = restore_cancelled_stops(broker, symbol, live, side="buy")
        if restored:
            discharge_scale_in_wal(db, prep.wal_row_id)
            prep.wal_row_id = None
        prep.skip_reason = "scale_in_cancel_unconfirmed"
        prep.skip_detail = detail
        logger.error("short scale-in: %s — add skipped for %s", detail, symbol)
        return prep

    prep.cancelled = True
    prep.cancel_confirmed_at = time.monotonic()
    logger.info(
        "short scale-in: cancelled and confirmed %d protective buy-stop(s) "
        "for %s so a SELL add can submit; WAL row %s covers the unprotected "
        "window",
        len(live),
        symbol,
        prep.wal_row_id,
    )
    return prep


def unprotected_window_seconds(cancel_confirmed_at: float | None) -> float | None:
    """Seconds since the broker acknowledged the cancel, or None.

    Board item 193. Both ends are `time.monotonic()` readings taken in the
    session that did the cancelling, so the figure is immune to clock changes
    and does not depend on when any row was written.
    """
    if cancel_confirmed_at is None:
        return None
    return round(max(0.0, time.monotonic() - cancel_confirmed_at), 3)


def restore_after_failed_add(
    broker: Any,
    db: Any,
    prep: LongAddPrep,
    symbol: str,
) -> None:
    """BUY never landed — put the original protective sell back if we cancelled it."""
    if not prep.cancelled:
        discharge_scale_in_wal(db, prep.wal_row_id)
        return
    if restore_cancelled_stops(broker, symbol, prep.specs, side=prep.side):
        discharge_scale_in_wal(db, prep.wal_row_id)
        return
    logger.critical(
        "scale-in: BUY add for %s failed AND the original protective sell "
        "could not be restored — WAL row %s remains so drain/watchdog can "
        "rearm; OWNER must be alerted",
        symbol,
        prep.wal_row_id,
    )
    alert_rearm_failed(
        symbol=symbol,
        qty=abs(float(prep.held_qty_before or 0)),
        stop_price=(most_protective_short_stop if prep.side == "buy" else most_protective_long_stop)(
            [float(spec.get("stop_price") or 0) for spec in (prep.specs or [])]
        ),
        order_id=None,
        detail=(
            "The add itself never landed, so the position is the size it "
            "already was — but the protective sell that was cancelled to "
            "make room for it could not be put back."
        ),
        db=db,
    )


def pending_scale_in_symbols(db: Any) -> set[str]:
    try:
        rows = db.get_pending_protection_restores()
    except Exception:  # noqa: BLE001
        record_scale_in_fault(db, "pending_scale_in_symbols")
        return set()
    out: set[str] = set()
    for row in rows or []:
        if row.get("sell_order_id") == WAL_SCALE_IN_SENTINEL and row.get("symbol"):
            out.add(str(row["symbol"]))
    return out


def pending_protection_symbols(db: Any) -> set[str]:
    """Every WAL-owned symbol — scale-in and sell — so trail/repair skip them."""
    try:
        rows = db.get_pending_protection_restores()
    except Exception:  # noqa: BLE001
        record_scale_in_fault(db, "pending_protection_symbols")
        return set()
    return {str(r["symbol"]) for r in (rows or []) if r.get("symbol")}


def drain_scale_in_row(broker: Any, db: Any, row: dict) -> bool:
    """Crash recovery for a scale-in WAL row. True when coverage is known-good.

    Cancels any leftover DAY entry (the add may still be working after a
    kill), confirms that, then places ONE protective sell covering the
    broker's current quantity at the most-protective stored stop. A working
    entry that will not confirm leaves the row in place.
    """
    symbol = row.get("symbol") or ""
    # H3: derive long/short from the SIGNED position quantity, NEVER from the
    # `side` column. That column carries the protective-stop / close-order
    # side, whose documented meaning has been ambiguous (and was documented
    # backwards); a scale-in row must not be read with the generic
    # position/close-side convention. The sign of position_qty_before_sell is
    # unambiguous: it is `held_signed_qty` captured at prep time — NEGATIVE
    # for a short, POSITIVE for a long. Everything below flips on it:
    # most-protective is the lowest trigger for a short, and the snapshot /
    # rearm run on the buy-stop side.
    try:
        _held_before = float(row.get("position_qty_before_sell") or 0)
    except (TypeError, ValueError):
        _held_before = 0.0
    is_short = _held_before < 0
    try:
        specs = json.loads(row.get("specs_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.error(
            "scale-in drain: row %s has unparseable specs (%s)",
            row.get("id"),
            exc,
        )
        return False
    _most_protective = most_protective_short_stop if is_short else most_protective_long_stop
    stop_side = "buy" if is_short else "sell"
    stop_price = _most_protective(
        [float(s.get("stop_price") or 0) for s in specs],
    )
    entry_ids = list_open_entry_ids(broker, symbol)
    for order_id in entry_ids:
        try:
            broker.cancel_entry_order(order_id)
        except Exception as exc:  # noqa: BLE001
            record_scale_in(db, "drain.cancel_leftover_entry", exc, symbol=symbol, order=order_id)
        else:
            record_scale_in(db, "drain.cancel_leftover_entry")
        try:
            status = str(broker.wait_for_order_terminal(order_id) or "").lower()
        except Exception as exc:  # noqa: BLE001
            record_scale_in(db, "drain.confirm_leftover_entry", exc, symbol=symbol, order=order_id)
            return False
        else:
            record_scale_in(db, "drain.confirm_leftover_entry")
        if status and status not in (_CANCEL_CONFIRMED | _STOP_FILLED | frozenset({"done_for_day"})):
            logger.warning(
                "scale-in drain: leftover entry %s for %s still %s — leaving WAL",
                order_id,
                symbol,
                status,
            )
            return False

    qty = broker_position_qty(broker, symbol)
    if qty is None:
        return False
    held = abs(float(qty))
    if held <= 0:
        return True
    # item 88: `<= 0` alone let a recorded ±Inf through to the broker.
    from src.execution.stop_records import STOP_USABLE, classify_stop_price

    if classify_stop_price(stop_price)[0] != STOP_USABLE:
        logger.error(
            "scale-in drain: %s held %.4f but the WAL row's stop price is "
            "%r, which cannot be a stop — leaving the row so the owner alert "
            "/ next repair can see it",
            symbol,
            held,
            stop_price,
        )
        return False

    try:
        existing_ok, existing = broker.snapshot_protective_stops(symbol, side=stop_side)
    except Exception:
        record_scale_in_fault(db, "drain.snapshot_existing_stops", symbol=symbol)
        existing_ok, existing = False, []
    else:
        record_scale_in(db, "drain.snapshot_existing_stops")
    covered = 0.0
    if existing_ok:
        for spec in existing or []:
            try:
                covered += float(spec.get("qty") or 0)
            except (TypeError, ValueError):
                continue
    if covered + 1e-6 >= held:
        return True

    _rearm = rearm_full_position_short_stop if is_short else rearm_full_position_stop
    placed = _rearm(
        broker,
        symbol=symbol,
        qty=held,
        stop_price=stop_price,
        db=db,
    )
    if placed is None:
        logger.error(
            "scale-in drain: rearm FAILED for %s qty=%.4f stop=$%.2f",
            symbol,
            held,
            stop_price,
        )
        alert_rearm_failed(
            symbol=symbol,
            qty=held,
            stop_price=stop_price,
            order_id=(entry_ids[0] if entry_ids else None),
            detail=(
                "Crash-recovery drain could not place the protective "
                f"{stop_side} over the held {held:.4f}. The WAL row is kept "
                "so the next drain and the coverage sweep retry."
            ),
            db=db,
        )
        return False
    uncovered = 0.0
    if isinstance(placed, dict):
        try:
            uncovered = float(placed.get("uncovered_qty") or 0)
        except (TypeError, ValueError):
            uncovered = 0.0
    return uncovered <= 0


#: Agent name on the durable `specialist_evidence` row this alert writes.
#: The API/dashboard evidence surface already reads that table, so the
#: record survives Telegram being muted (it is, as of 2026-09-30).
REARM_FAILURE_AGENT_NAME = "scale_in_rearm_failure"

#: Claim key for the once-per-symbol-per-trading-day page. One naked
#: position must not page 44 times, which is what muted the channel.
REARM_FAILURE_ALERT_KIND = "scale_in_rearm_failed"


def record_rearm_failure(
    db: Any,
    *,
    symbol: str,
    qty: float,
    stop_price: float,
    order_id: str | None,
    detail: str = "",
    run_id: str | None = None,
) -> bool:
    """Durable record that a post-add protective stop did not go back on.

    Written on EVERY occurrence (a record is not a page), into the existing
    `specialist_evidence` table the health/journal surface already reads, so
    the fault is visible with the alert channel muted. Never raises: a
    record is never trading authority.
    """
    if db is None:
        return False
    try:
        db.insert_specialist_evidence(
            run_id=str(run_id or ""),
            agent_name=REARM_FAILURE_AGENT_NAME,
            kind="pipeline_event",
            scope="symbol",
            symbol=str(symbol).upper() or None,
            evidence_json=json.dumps(
                {
                    "event": "scale_in_rearm_failed",
                    "symbol": str(symbol).upper(),
                    "qty": qty,
                    "stop_price": stop_price,
                    "entry_order_id": order_id,
                    "detail": detail,
                    "position_protected": False,
                },
                sort_keys=True,
                default=str,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        record_scale_in(db, "record_rearm_failure", exc, symbol=symbol)
        return False
    else:
        record_scale_in(db, "record_rearm_failure")
        return True


def alert_rearm_failed(
    *,
    symbol: str,
    qty: float,
    stop_price: float,
    order_id: str | None,
    detail: str = "",
    db: Any = None,
    run_id: str | None = None,
) -> None:
    """Fail-closed owner page when the post-add protective sell did not land.

    Two channels, deliberately different cadences: the durable record goes
    down every time, the Telegram page at most once per symbol per trading
    day.
    """
    record_rearm_failure(
        db,
        symbol=symbol,
        qty=qty,
        stop_price=stop_price,
        order_id=order_id,
        detail=detail,
        run_id=run_id,
    )
    try:
        if not claim_typed_alert(REARM_FAILURE_ALERT_KIND, [symbol]):
            return
    except Exception as exc:  # noqa: BLE001 — never swallow the page on a
        # state-file fault; err towards telling the owner twice.
        record_scale_in(db, "alert_rearm.claim", exc, symbol=symbol)  # sending anyway
    body = (
        "STOP NOT REARMED AFTER A SCALE-IN\n"
        f"{symbol}: the desk cancelled the resting protective sell so it "
        f"could add, the add filled, and putting ONE protective sell back "
        f"over the full position ({qty:g} share(s) at {stop_price}) failed. "
        "The position is open at the broker with nothing standing watch.\n"
        f"Entry order: {order_id or 'unknown'}\n"
        f"{detail or 'Place a stop manually or flatten. The coverage sweep will also try to repair it.'}"
    )
    try:
        from src import notifier as _notifier

        _notifier.send_owner_alert(body, symbols=[str(symbol)])
    except Exception as exc:  # noqa: BLE001
        record_scale_in(db, "alert_rearm.send", exc, symbol=symbol)
    else:
        record_scale_in(db, "alert_rearm.send")
