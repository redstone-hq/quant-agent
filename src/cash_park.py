"""src.cash_park -- the deployable-cash figure and the news-eligible held set as a constructed part.

Bodies moved verbatim from src/pipeline.py (TradingPipeline). The one collaborator is an
explicit keyword-only constructor argument; nothing here imports src.pipeline or the
broker seam. The host's `_sweeper` callable is handed IN (not lifted) so a test that swaps
it on the host is what these bodies see. Read-only: places no orders, cancels nothing.
The retired-vehicle shim (`_retired_cash_park_symbol`) stays
on the host because it imports `src.execution.cash_sweep` inline and the broker-seam
importer set must not widen (tests/test_import_layering.py); see docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import logging

from src.quantities import deployable_cash

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class CashPark:
    """Deployable cash and the news-eligible held set, read through the host's sweeper callable."""

    def __init__(self, *, sweeper) -> None:
        self._sweeper = sweeper

    def _compute_deployable_cash(self, cash: float, positions) -> float:
        """Cash QAMC can deploy into equities WITHOUT borrowing.

        Verified Alpaca account-field semantics (2026-08-19, official docs):

        - `cash` is credited as soon as a SELL **fills** — Alpaca:
          "The cash is updated post the SELL trade is filled, but the
          cash_withdrawable and cash_transferable are updated post T+1."
          So proceeds of a filled SGOV sale ARE usable for an equity BUY
          the same session; there is no settlement wait for trading.
        - `non_marginable_buying_power` is the settled/non-margin (crypto)
          figure and LAGS a same-day equity sale by one business day. Using
          it to size equity BUYs is wrong in the conservative direction —
          it makes legitimately-available money invisible. An earlier pass
          in this tranche did exactly that; this is the correction.
        - `buying_power` / `regt_buying_power` are MARGIN figures. Every
          Alpaca account is a margin account and this one's equity puts it
          at multiplier 2, so those fields are ~2x equity. QAMC must never
          size against them — that is borrowed money by definition.

        Deployable is therefore raw `cash` plus the market value of the
        cash-equivalent sweep vehicle. Both components are assets QAMC
        already owns, so the sum can never exceed equity and never creates
        leverage. NOTE (item 190): nothing sells the vehicle before the BUY
        phase any more, so the parked component is owned but not
        automatically converted; see docs/WORK.md item 190.

        This is a PLANNING figure for PM / RM / the pre-trade gate. It is
        not authoritative for execution, and — stale since the 2026-09-02
        margin flip — it is no longer true that execution "skips any BUY
        that cash does not actually cover": ExecutionStage still re-reads
        raw broker `cash` after the funding sale, but with `allow_margin`
        true a BUY may draw beyond that raw cash, bounded by the §11.2
        gross-exposure ladder's headroom, not by this figure (see
        `_entry_deployment_budget` in `src/pipeline_stages.py`). With
        `allow_margin` false the old description still holds: cash is the
        hard ceiling. Either way, this function itself never reads
        `buying_power` / `regt_buying_power` — see above — that boundary is
        unrelated to and unmoved by the ladder.

        The arithmetic itself lives in `src.quantities.deployable_cash` —
        one definition, shared with Mission Control's "Deployable" tile,
        which used to show `max(cash - sweep_reserve, 0)` instead and read
        1.58x lower than the figure the engine actually sized against.
        """
        sweeper = self._sweeper()
        if sweeper is None:
            return deployable_cash(cash, 0.0)
        try:
            parked = sweeper.parked_value(positions)
        except Exception as e:  # noqa: BLE001 — unknowable sweep state must not inflate
            logger.warning("deployable cash: parked-value read failed (%s) — treating sweep reserve as unavailable", e)
            parked = 0.0
        return deployable_cash(cash, parked)

    def _news_held_symbols(self, positions) -> list[str]:
        """Held symbols eligible for the capped per-symbol company-news fetch.

        Applies the same cash-sweep exclusion as every other LLM-facing
        position view (`CashSweeper.split_positions`) before extracting
        symbols: the parked T-bill vehicle is cash-equivalent, has no
        thesis to follow, and must never take a per-symbol news request
        (2026-08-31 forensic — it was doing exactly that in the midday/close
        path, when those requests were capped).

        Shared by every same-day session that fetches held-symbol news
        (`run_position_review`, which itself backs both midday and close,
        and `run_evening`) so the exclusion cannot drift apart between
        them again.
        """
        sweeper = self._sweeper()
        investable = positions
        if sweeper is not None:
            investable, _parked = sweeper.split_positions(positions)
        return [
            s for s in (str(getattr(p, "symbol", "")).strip().upper() for p in investable if getattr(p, "qty", 0)) if s
        ]
