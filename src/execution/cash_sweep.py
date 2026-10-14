"""Cash-sweep view: the retired T-bill vehicle (default SGOV) read as cash, not a position.

The sweep no longer trades. The pre-BUY funding sale and the end-of-session
parking purchase were removed in docs/WORK.md item 190; the release of a
leftover vehicle followed on 2026-10-14, once no code path could buy the
vehicle any more. What remains is the read-only view:

1. The vehicle is CASH-EQUIVALENT, never a position:
   - excluded from every LLM-facing view (PM / position_reviewer / evening
     builders) - the LLM never reasons about it, never sells it, never
     counts it toward exposure;
   - its market value counts as CASH in `_filter_hard_risk_decisions`
     (cash_only) and is excluded from net-exposure math, so parked cash can
     never block a legitimate BUY;
   - exempt from `_reconcile_stop_coverage` (it deliberately carries no
     protective stop);
   - `_force_delever` liquidates it FIRST (before any real long) when the
     account drifts into margin.

2. Ledger isolation: historical SWEEP_BUY rows and `_force_delever`'s
   SWEEP_SELL exit are deliberately ABSENT from every action-tuple consumer
   (evening grading, calibration, recent-sells builders), so vehicle churn
   never pollutes the learning loops.
"""

import math

from src.storage.event_journal import DatabaseEventJournal


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
