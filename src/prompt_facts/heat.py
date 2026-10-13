"""src.prompt_facts.heat -- the portfolio heat block.

Bodies moved verbatim from src/pipeline_prompt_facts.py (`PromptFactsMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging
from src.sentinel.counted import record_swallowed

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptHeat:
    """The portfolio heat block; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        sweeper=None,
        build_stop_map=None,
    ) -> None:
        self._sweeper = sweeper  # the host's callable, read per use (never snapshotted)
        self._build_stop_map = build_stop_map  # owned by PromptExposure; the host's shim is handed in

    def _build_portfolio_heat(self, positions, total_value: float):
        """Audit §1.3 — total capital at risk, which nothing computed before.

        The cash-equivalent sweep vehicle is excluded rather than counted as
        unprotected: it is deliberately stopless everywhere in this codebase
        and is not a risk position. Returns None on failure so the prompt can
        say "unknown" instead of rendering a confident zero.
        """
        from src.risk.heat_unreadable import portfolio_heat_with_unreadable as portfolio_heat

        try:
            sweeper = self._sweeper()
            excluded = set()
            if sweeper is not None and sweeper.symbol:
                excluded.add(str(sweeper.symbol).upper())
            live_stops, entry_risks, unreadable = self._build_stop_map(positions)
            return portfolio_heat(
                positions=positions,
                equity=total_value,
                stops=live_stops,
                entry_risks=entry_risks,
                exclude_symbols=excluded,
                unreadable_stops=unreadable,
            )
        except Exception as e:  # noqa: BLE001
            record_swallowed("prompt_facts.heat._build_portfolio_heat", e, log=logger)
            return None
