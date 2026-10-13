"""Idle-cash sweep: park excess cash in a T-bill ETF, release it on demand.

Motivation (2026-07-16 forensics): the account sat at ~84% idle cash for
weeks while short-dated T-bills yielded 4%+. On a ~$100k book that is
~$300/month of risk-free carry left on the table — and unlike everything
else in this system, capturing it requires no forecast at all.

Design contract (mirrors CLAUDE.md 金额/仓位语义):

1. The sweep vehicle (default SGOV) is CASH-EQUIVALENT, never a position:
   - excluded from every LLM-facing view (PM / position_reviewer / evening
     builders) — the LLM never reasons about it, never sells it, never
     counts it toward exposure;
   - its market value counts as CASH in `_filter_hard_risk_decisions`
     (cash_only) and is excluded from net-exposure math, so parked cash can
     never block a legitimate BUY;
   - exempt from `_reconcile_stop_coverage` (it deliberately carries no
     protective stop — a T-bill ladder gapping 5% is not a scenario stops
     defend against);
   - `_force_delever` liquidates it FIRST (before any real long) when the
     account drifts into margin.

2. Deterministic and zero-LLM. Both bookend operations — the pre-BUY
   funding sale and the end-of-session parking purchase — were REMOVED in
   docs/WORK.md item 190. What remains is `release_retired_vehicle`, the
   one-way exit for a vehicle still held after the sweep was switched off.

3. SELL discipline: funding sells go through
   `pipeline._submit_protected_sell` + `_finalize_pending_protections`,
   exactly like FORCE_DELEVER — the vehicle has no stops so the
   cancel/restore halves are no-ops, but the WAL bookkeeping stays uniform
   with every other SELL path (a future stop on the vehicle would be
   handled instead of orphaned).

4. Ledger isolation: trades are recorded as SWEEP_BUY / SWEEP_SELL. Those
   action names are deliberately ABSENT from every action-tuple consumer
   (evening grading, calibration, recent-sells builders), so parking churn
   never pollutes the learning loops.

Failure posture: every operation is best-effort and conservative. Any
uncertainty (broker query failed, non-finite numbers, open-order notional
unknowable) resolves to "do nothing this session" — an unswept dollar
costs basis points; an over-swept dollar can reject a real trade.
"""

import logging
import math
import time
from src.sentinel.guarded import record_guarded_pass

from src.storage.event_journal import DatabaseEventJournal

logger = logging.getLogger(__name__)

# Funding-sell confirmation budgets. The funding sale is the one order the
# session's entire purpose depends on: if its proceeds are not confirmed
# before the BUY loop, every risk-approved BUY is skipped as unfunded.
# Production fills of the vehicle have been observed at 2s, 5s and 51s —
# the 51s one (2026-08-19 13:35) outlived the default 15s terminal wait,
# the account was read pre-fill, all three approved BUYs died, and the
# proceeds landed 36 seconds into an already-dead session. Terminal wait
# gets 3.5x the observed worst case; the cash-settle poll covers any lag
# between the status flip and the cash credit. Both stay far inside the
# session wrapper's 20-minute kill budget.
_FUND_TERMINAL_TIMEOUT_S = 180.0
_FUND_CASH_SETTLE_TIMEOUT_S = 30.0
_FUND_CASH_SETTLE_POLL_S = 2.0

# Limit-price padding on the release sell. The vehicle trades at ~1bp
# spreads; -0.1% crosses the book immediately while still capping a
# pathological fill. The BUY-side pad died with the parking buy (item 190).
_SELL_LIMIT_PAD = 0.999


class CashSweeper:
    """Pipeline-owned helper; all broker/DB access goes through `pipeline`."""

    def __init__(self, *, pipeline):
        self._pipeline = pipeline

    # ---------- config / views ----------

    @property
    def _cfg(self):
        return getattr(getattr(self._pipeline, "config", None), "cash_sweep", None)

    def enabled(self) -> bool:
        # `is True` (not truthiness): tests stub pipeline.config with
        # MagicMock, whose auto-created attributes are truthy — a sweeping
        # MagicMock must read as DISABLED, never as configured-on.
        cfg = self._cfg
        return cfg is not None and getattr(cfg, "enabled", False) is True

    @property
    def symbol(self) -> str | None:
        cfg = self._cfg
        return getattr(cfg, "symbol", None) if cfg is not None else None

    def split_positions(self, positions):
        """(investable_positions, parked_position_or_None).

        The investable list is what every LLM view and the risk engine
        should see; `parked` is the sweep-vehicle position when held.
        Disabled sweeper → passthrough (positions, None).
        """
        if not self.enabled() or not positions:
            return positions, None
        sym = self.symbol
        investable = [p for p in positions if getattr(p, "symbol", None) != sym]
        parked = next((p for p in positions if getattr(p, "symbol", None) == sym), None)
        return investable, parked

    def parked_value(self, positions) -> float:
        """Market value of the parked vehicle (0.0 when none / non-finite)."""
        _, parked = self.split_positions(positions)
        if parked is None:
            return 0.0
        mv = getattr(parked, "market_value", 0.0)
        try:
            mv = float(mv)
        except (TypeError, ValueError):
            return 0.0
        return mv if math.isfinite(mv) and mv > 0 else 0.0

    # ---------- funding (un-park before BUYs) ----------

    # ---------- retirement (sweep disabled, vehicle still held) ----------

    def release_retired_vehicle(self, run_id: str | None = None) -> dict | None:
        """Sell the WHOLE held vehicle back into cash when the sweep is OFF.

        Owner mandate 2026-09-17: fully invested, nothing in T-bills. With
        `cash_sweep.enabled: false` every other sweep hook goes inert —
        the bookend sweep operations are gone entirely (item 190) and
        `split_positions` stops hiding it. A vehicle bought
        before the switch would otherwise sit as a stopless, thesis-less
        position that nothing is designed to sell. This is the one path that
        does: a deterministic, zero-LLM full exit, recorded as SWEEP_SELL so
        the ledger isolation in the module docstring still holds.

        No-op when the sweep is enabled (the vehicle is then still managed),
        when no symbol is configured, or when nothing is held. Best-effort:
        any broker failure logs and returns None, and the next session tries
        again. Returns the accepted order dict, or None.
        """
        if self.enabled():
            return None
        sym = self.symbol
        if not isinstance(sym, str) or not sym.strip():
            return None
        pipeline = self._pipeline
        try:
            positions = pipeline.broker.get_positions()
        except Exception as e:  # noqa: BLE001
            # Recorded, not just logged: a swallowed read failure is
            # otherwise indistinguishable from "nothing held". The
            # vehicle stays held and stopless until the next session.
            DatabaseEventJournal(pipeline.db).record_pipeline_event(
                run_id=str(run_id or ""),
                decision_id=None,
                symbol=sym,
                stage="cash_sweep_release",
                outcome="skipped",
                reason="position read failed",
                error=str(e),
            )
            logger.warning("cash sweep retired: position read failed — not releasing %s this session: %s", sym, e)
            return None
        held = next(
            (p for p in (positions or []) if getattr(p, "symbol", None) == sym),
            None,
        )
        if held is None:
            return None
        try:
            qty = float(getattr(held, "qty", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(qty) or qty <= 0:
            return None
        price = getattr(held, "current_price", None)
        if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
            logger.warning("cash sweep retired: no usable price for %s — not releasing this session", sym)
            return None
        sell_qty = pipeline._full_sell_qty(qty)
        if sell_qty is None:
            return None
        sale = pipeline._submit_protected_sell(
            symbol=sym,
            qty=sell_qty,
            limit_price=round(price * _SELL_LIMIT_PAD, 2),
            reference_price=price,
            position_qty_before_sell=qty,
            label="SWEEP_SELL",
        )
        if sale is None:
            return None
        order, prot = sale
        try:
            pipeline.db.insert_trade(
                symbol=sym,
                action="SWEEP_SELL",
                qty=sell_qty,
                price=price,
                reasoning=(
                    "cash sweep retired (owner mandate 2026-09-17: fully "
                    f"invested, no T-bills): releasing all held {sym} into cash"
                ),
                run_id=run_id,
                broker_order_id=order.get("id"),
                fill_status="submitted",
            )
            record_guarded_pass((pipeline, pipeline.broker), "cash_sweep.sweep_sell_insert", context={"symbol": sym})
        except Exception as e:  # noqa: BLE001 — ledger failure must not strand finalize
            record_guarded_pass((pipeline, pipeline.broker), "cash_sweep.sweep_sell_insert", e, context={"symbol": sym})
            logger.warning("cash sweep retired: insert_trade failed for SWEEP_SELL: %s", e)
        pipeline._finalize_pending_protections([prot], context="CASH SWEEP RETIRED")
        logger.info(
            "cash sweep retired: submitted full release of %s (%s sh @ ~$%.2f)",
            sym,
            pipeline._format_qty(sell_qty),
            price,
        )
        return order


def sweeper_or_none(cash_sweeper):
    """The cash sweeper, or None when absent/disabled.

    Former `TradingPipeline._sweeper` (conversion step 9). getattr-guarded
    at the call site because tests build TradingPipeline via __new__()
    without __init__ (covered by tests/test_cash_sweep.py) — for them (and
    for enabled=False configs) every sweep hook must be a structural no-op.
    """
    sweeper = cash_sweeper
    if not isinstance(sweeper, CashSweeper):
        return None
    try:
        return sweeper if sweeper.enabled() else None
    except Exception as e:  # noqa: BLE001 — a broken config must not take down a session
        # Recorded, not just logged: a config that cannot even be read is
        # otherwise indistinguishable from "sweep disabled".
        DatabaseEventJournal(getattr(sweeper._pipeline, "db", None)).record_pipeline_event(
            run_id="",
            decision_id=None,
            symbol=None,
            stage="cash_sweep_config",
            outcome="disabled",
            reason="config read failed",
            error=str(e),
        )
        return None
