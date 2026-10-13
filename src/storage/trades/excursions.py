"""TradeLedger excursions bodies, lifted VERBATIM from ledger.py.

`ledger` is the TradeLedger (`self` before the move).
"""

from __future__ import annotations

import logging
import json

logger = logging.getLogger(__name__)


def _accumulate_excursions(ledger, position) -> None:
    """Widen the recorded worst- and best-against-entry excursions.

    STOP-FLOOR EVIDENCE, RECORDING ONLY. These are the running half of
    the facts the desk needs before it can ever check its ratified
    minimum stop width against its own trades (the pinned half,
    `entry_atr`, `initial_stop_loss` and `stop_basis`, is written at
    entry by `insert_trade`; the resolved half, `realized_pnl` and
    `exit_reason_category`, lands when the position closes). Read the
    `max_adverse_excursion` migration note in `_migrate` for the hard
    limit on its use: it may show whether the floor was ever VIOLATED in
    practice, and it may NOT be optimised against to produce a new
    multiplier. Doctrine bars fitting a number to this desk's history.

    Monotonic: the stored figure only ever widens while the position is
    open, so a recovery cannot erase the excursion that preceded it. It
    is written onto the OPENING rows of the position (the rows carrying
    `entry_atr`), which is where a later reader joins entry ATR, stop
    basis, excursion and realised outcome together.

    Side-agnostic: "against" is below entry for a long and above entry
    for a short, decided from the sign of `qty` rather than from a
    stored action, because that is the only side fact a broker position
    snapshot carries. A zero or missing entry price is skipped rather
    than guessed.

    Assumes the caller holds `self._lock` and an open transaction —
    `sync_positions` is the only caller and does both.
    """
    try:
        entry = float(getattr(position, "avg_entry", 0) or 0)
        last = float(getattr(position, "current_price", 0) or 0)
        qty = float(getattr(position, "qty", 0) or 0)
    except (TypeError, ValueError):
        return
    if entry <= 0 or last <= 0 or qty == 0:
        return
    # Excursion AGAINST the position, in price units. Never negative:
    # a position in profit contributes nothing. The favourable leg is
    # its exact mirror, and at most one of the two is positive at any
    # snapshot, so each column widens only on the snapshots that
    # actually evidence it.
    adverse = (entry - last) if qty > 0 else (last - entry)
    favourable = -adverse
    for column, excursion in (
        ("max_adverse_excursion", adverse),
        ("max_favourable_excursion", favourable),
    ):
        if excursion <= 0:
            continue
        ledger.conn.execute(
            f"UPDATE trades SET {column} = ? "  # noqa: S608 - literal, not input
            "WHERE symbol = ? AND action IN ('BUY', 'SHORT') "
            "AND entry_atr IS NOT NULL "
            f"AND ({column} IS NULL OR {column} < ?) "
            "AND position_id IN ("
            "  SELECT position_id FROM trades WHERE symbol = ? "
            "  AND position_id IS NOT NULL AND action IN ('BUY', 'SHORT') "
            "  ORDER BY COALESCE(filled_at, timestamp) DESC, id DESC LIMIT 1)",
            (excursion, position.symbol, excursion, position.symbol),
        )


def record_overnight_gap(
    ledger,
    symbol: str,
    prev_close: float,
    open_price: float,
    session_date: str,
) -> bool:
    """Record one session's ADVERSE overnight gap against an open SHORT.

    SHORT-SIDE GAP EVIDENCE, RECORDING ONLY. Read the
    `max_adverse_overnight_gap` migration note in `_migrate` for why
    this exists (item 186 — the sizing haircut cannot be read off the
    instrument because the evidence was never kept) and for the hard
    limit on its use: nothing may read it back into a sizing, stop or
    exit decision, and it may not be swept for an optimal multiple.

    The stored figure is the WORST (largest) adverse gap seen on any
    session the short was held, `open - prev_close` in price units,
    positive when the name gapped UP against the short. It is stored
    SIGNED and unfiltered: a short every one of whose gaps ran in its
    favour records a negative worst, which is a real and different fact
    from "never observed". `overnight_gap_sessions` counts the sessions
    observed so the two stay distinguishable.

    Written onto the OPENING rows of the position (the `SHORT` rows
    carrying `entry_atr`), which is where a later reader joins the gap
    to the volatility read and the stop distance pinned at entry —
    `entry_atr` and `initial_stop_loss` on the same row — and, once the
    position closes, to its realised outcome.

    Idempotent per session: `last_overnight_gap_date` gates the write,
    so a second position sync on the same date cannot count one gap
    twice. Returns True when a row was updated.
    """
    try:
        prev_close = float(prev_close)
        open_price = float(open_price)
    except (TypeError, ValueError):
        return False
    if prev_close <= 0 or open_price <= 0 or not session_date:
        return False
    gap = open_price - prev_close
    with ledger._lock:
        cur = ledger.conn.execute(
            "UPDATE trades SET "
            "  max_adverse_overnight_gap = CASE "
            "    WHEN max_adverse_overnight_gap IS NULL "
            "      OR max_adverse_overnight_gap < ? THEN ? "
            "    ELSE max_adverse_overnight_gap END, "
            "  overnight_gap_sessions = COALESCE(overnight_gap_sessions, 0) + 1, "
            "  last_overnight_gap_date = ? "
            "WHERE symbol = ? AND action = 'SHORT' "
            "AND entry_atr IS NOT NULL "
            "AND (last_overnight_gap_date IS NULL OR last_overnight_gap_date < ?) "
            "AND position_id IN ("
            "  SELECT position_id FROM trades WHERE symbol = ? "
            "  AND position_id IS NOT NULL AND action IN ('BUY', 'SHORT') "
            "  ORDER BY COALESCE(filled_at, timestamp) DESC, id DESC LIMIT 1)",
            (gap, gap, session_date, symbol, session_date, symbol),
        )
        ledger.conn.commit()
        return cur.rowcount > 0


def _accumulate_level_distances(ledger, position) -> None:
    """Widen what the market has done to the stop's structural level.

    ITEM 55 RECORDING, FALSIFICATION ONLY, and it decides nothing. The
    pinned half (`stop_level_basis`) says what the stop stood on; this
    is the running half that says what price then did to it, so that
    "is this a real level" becomes answerable from the desk's own
    record instead of from argument. Read the `stop_level_basis`
    migration note for the hard limit on its use: it may show the
    current definition of a level is WRONG, and it may NEVER be swept
    for a better pivot window or zone width.

    TWO RAW DISTANCES, NO VERDICT. `level_max_penetration` is how far
    beyond the zone's FAR edge price has travelled (monotonic upward,
    never negative); `level_closest_approach` is the smallest gap ever
    seen to the zone's NEAR edge (monotonic downward, signed, negative
    once price is inside). Nothing here calls an outcome "respected",
    "pierced" or "broken", because each of those needs a cutoff nobody
    can source; a later reader states its own cutoff and applies it to
    these numbers, which were never rounded to one.

    Side-agnostic in the same way as `_accumulate_excursions`, and for
    the same reason: the side is read off the sign of `qty`, the only
    side fact a broker position snapshot carries. A row with no
    `stop_level_basis`, or one whose record had no level behind the
    stop, is skipped and stays NULL rather than being given a
    substitute.

    Assumes the caller holds `self._lock` and an open transaction —
    `sync_positions` is the only caller and does both.
    """
    try:
        last = float(getattr(position, "current_price", 0) or 0)
        qty = float(getattr(position, "qty", 0) or 0)
    except (TypeError, ValueError):
        return
    if last <= 0 or qty == 0:
        return
    row = ledger.conn.execute(
        "SELECT id, stop_level_basis FROM trades "
        "WHERE symbol = ? AND action IN ('BUY', 'SHORT') "
        "AND stop_level_basis IS NOT NULL "
        "AND position_id IN ("
        "  SELECT position_id FROM trades WHERE symbol = ? "
        "  AND position_id IS NOT NULL AND action IN ('BUY', 'SHORT') "
        "  ORDER BY COALESCE(filled_at, timestamp) DESC, id DESC LIMIT 1) "
        "ORDER BY id DESC LIMIT 1",
        (position.symbol, position.symbol),
    ).fetchone()
    if row is None:
        return
    try:
        basis = json.loads(row[1])
    except (TypeError, ValueError):
        return
    if not isinstance(basis, dict) or not basis.get("level_backed"):
        return
    low, high = basis.get("zone_low"), basis.get("zone_high")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return
    if qty > 0:
        # Long: the level is support below, so the far edge is the
        # bottom of the zone and the near edge is the top of it.
        penetration = float(low) - last
        approach = last - float(high)
    else:
        penetration = last - float(high)
        approach = float(low) - last
    if penetration > 0:
        ledger.conn.execute(
            "UPDATE trades SET level_max_penetration = ? WHERE id = ? "
            "AND (level_max_penetration IS NULL OR level_max_penetration < ?)",
            (penetration, row[0], penetration),
        )
    ledger.conn.execute(
        "UPDATE trades SET level_closest_approach = ? WHERE id = ? "
        "AND (level_closest_approach IS NULL OR level_closest_approach > ?)",
        (approach, row[0], approach),
    )


def sync_positions(ledger, positions) -> None:
    """Replace positions table with a fresh broker snapshot.

    Upserts rows for currently-held symbols and deletes rows for any symbol
    no longer present. Prevents stale closed positions from lingering in the DB.

    Wraps DELETE + INSERT loop in an explicit BEGIN/COMMIT transaction so
    a crash between the DELETE and the first INSERT cannot leave the table
    in a half-state (would otherwise leave the next session's reviewer
    reading an empty positions snapshot while the broker still holds them).
    Mirrors the atomic-write discipline used in `save_evening_snapshot`.
    """
    current_symbols = {p.symbol for p in positions}
    with ledger._lock:
        try:
            ledger.conn.execute("BEGIN")
            if current_symbols:
                placeholders = ",".join("?" for _ in current_symbols)
                ledger.conn.execute(
                    f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})",
                    tuple(current_symbols),
                )
            else:
                ledger.conn.execute("DELETE FROM positions")
            for p in positions:
                ledger.conn.execute(
                    """INSERT INTO positions (symbol, qty, avg_entry, current_price, market_value,
                       unrealized_pnl, sector, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                           ON CONFLICT(symbol) DO UPDATE SET
                             qty=excluded.qty, avg_entry=excluded.avg_entry,
                             current_price=excluded.current_price, market_value=excluded.market_value,
                             unrealized_pnl=excluded.unrealized_pnl, sector=excluded.sector,
                             updated_at=datetime('now')""",
                    (p.symbol, p.qty, p.avg_entry, p.current_price, p.market_value, p.unrealized_pnl, p.sector),
                )
                # Stop-floor evidence, RECORDING ONLY — see the
                # `max_adverse_excursion` migration note for what this
                # data may and may NOT be used for. Nothing reads it back
                # into a trading decision; it cannot change sizing, stop
                # placement or an exit. Inside the same transaction as
                # the snapshot it is derived from, so the two can never
                # disagree, and swallowed on error so a recording problem
                # can never fail a position sync.
                try:
                    ledger._accumulate_excursions(p)
                    ledger._accumulate_level_distances(p)
                except Exception:
                    logger.debug(
                        "excursion recording skipped for %s",
                        getattr(p, "symbol", "?"),
                        exc_info=True,
                    )
            ledger.conn.commit()
        except Exception:
            ledger.conn.rollback()
            raise
