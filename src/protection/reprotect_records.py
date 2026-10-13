"""src.protection.reprotect_records -- the reprotect identity-gap record.

Bodies moved verbatim from src/pipeline_protection.py (`ProtectionMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body are the HOST's shim, handed in, never a body this part owns.
"""

import logging

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class ReprotectRecords:
    """The reprotect identity-gap record; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        record_exit_refusal=None,
    ) -> None:
        self._record_exit_refusal = record_exit_refusal

    def _record_reprotect_identity_gap(self, symbol: str, detail: str) -> None:
        """Durable, append-only, per-symbol record of a reprotect ambiguity.

        Adversary round 2, defect 4: the reason this path could not prove
        what rests at the broker was a log WARNING and nothing else, so it
        existed only in a rotated file nobody reads per symbol. It is now
        written through `src/risk/exit_refusal.py`, the desk's existing
        append-only per-symbol refusal record, so the next reader of the
        symbol sees why a duplicate may rest or why an intent survived.

        The run id is synthesised from the symbol and the UTC instant
        because this function runs inside broker restore/drain, which
        carries no run context to thread one from; the row is forensic and
        keyed by symbol, not joined to a pipeline run. Never raises: the
        SELL already succeeded and a forensic write must not unwind it.
        """
        try:
            from datetime import datetime, timezone

            from src.trading_calendar import utc_now

            stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
            self._record_exit_refusal(
                symbol=symbol,
                run_id=f"reprotect-{str(symbol).strip().upper()}-{stamp}",
                action="REPROTECT",
                code="reprotect_broker_state_unprovable",
                dropped=False,
                detail=detail,
                layer="execution",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "reprotect ambiguity record failed for %s: %s",
                symbol,
                exc,
            )
