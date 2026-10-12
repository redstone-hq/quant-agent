"""The Spec §11.2 gross-exposure ceiling and the de-levering ladder -- shims only.

Bodies live in src/delever/ (five constructed parts: ladder, conviction, enforce,
forced, trims); this mixin keeps same-named thin shims, built per call so a
collaborator swapped after construction is what the body sees. The two owner
alerts (`_alert_owner_force_delever_incomplete`, `_alert_owner_delever_incomplete`)
still carry their bodies here and are handed into the parts as collaborators.

The module-level names below are re-exported unchanged for importers and patchers
of this module (the ONE mirror block for this module): the risk-number helpers
`_optional_risk_number` / `_risk_number` now live in src/delever/risk_number.py and
are still imported from here by `src.pipeline_config_build` and `src.pipeline_risk_gate`.

Nothing here may import `src.pipeline`: this module is one of its bases.
"""

import logging

from src.sentinel.guarded import record_guarded_pass
import math  # noqa: F401 -- re-exported

from src.delever.conviction import DeleverConviction
from src.delever.enforce import DeleverEnforce
from src.delever.forced import DeleverForced
from src.delever.ladder import DeleverLadder
from src.delever.risk_number import _optional_risk_number, _risk_number  # noqa: F401 -- re-exported
from src.delever.trims import DeleverTrims
from src.pipeline_context import RunContext  # noqa: F401 -- re-exported
from src.risk.rules import (  # noqa: F401 -- re-exported
    GROSS_LADDER,
    GROSS_LADDER_ALERT_PCT,
    GrossCeiling,
    SECTOR_SIDE_LONG,
    apply_gross_ceiling,
    count_aligned_sources,
    count_opposing_sources,
    distance_to_forced_liquidation_pct,
    gross_exposure,
    peak_to_trough_pct,
    position_side,
    resolve_gross_ceiling,
)
from src.verdicts import rank_verdicts  # noqa: F401 -- re-exported

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class DeleverMixin:
    """The gross-exposure ceiling and the de-levering ladder (Spec §11.2); bodies on src/delever/ parts.

    Each `_delever_*` builder reads the host's collaborators at call time. A part that calls a
    sibling body is handed the host's shim for it (or whatever a test swapped in), never a body
    it owns, so no recursion guard is needed."""

    def _delever_ladder(self) -> DeleverLadder:
        return DeleverLadder(
            sweep_symbol=getattr(self, "_sweep_symbol", None),
            broker=getattr(self, "broker", None),
            config=getattr(self, "config", None),
            db=getattr(self, "db", None),
        )

    def _delever_conviction(self) -> DeleverConviction:
        return DeleverConviction()

    def _delever_enforce(self) -> DeleverEnforce:
        return DeleverEnforce(
            is_margin_floor_breach=getattr(self, "_is_margin_floor_breach", None),
            open_exit_relief=getattr(self, "_open_exit_relief", None),
            resolve_gross_ceiling=self._resolve_gross_ceiling,  # a class method: never absent on a real pipeline
            submit_gross_ceiling_trims=getattr(self, "_submit_gross_ceiling_trims", None),
            sweep_symbol=getattr(self, "_sweep_symbol", None),
            config=getattr(self, "config", None),
            db=getattr(self, "db", None),
        )

    def _delever_forced(self) -> DeleverForced:
        return DeleverForced(
            alert_owner_force_delever_incomplete=getattr(self, "_alert_owner_force_delever_incomplete", None),
            compute_deployable_cash=getattr(self, "_compute_deployable_cash", None),
            finalize_pending_protections=getattr(self, "_finalize_pending_protections", None),
            format_qty=getattr(self, "_format_qty", None),
            full_sell_qty=getattr(self, "_full_sell_qty", None),
            live_delever_price=getattr(self, "_live_delever_price", None),
            submit_protected_sell=getattr(self, "_submit_protected_sell", None),
            sweeper=getattr(self, "_sweeper", None),
            broker=getattr(self, "broker", None),
            config=getattr(self, "config", None),
            db=getattr(self, "db", None),
        )

    def _delever_trims(self) -> DeleverTrims:
        return DeleverTrims(
            alert_owner_delever_incomplete=getattr(self, "_alert_owner_delever_incomplete", None),
            compute_deployable_cash=getattr(self, "_compute_deployable_cash", None),
            conviction_cut_order=getattr(self, "_conviction_cut_order", None),
            enforce_gross_ceiling=getattr(self, "_enforce_gross_ceiling", None),
            finalize_pending_protections=getattr(self, "_finalize_pending_protections", None),
            format_qty=getattr(self, "_format_qty", None),
            full_sell_qty=getattr(self, "_full_sell_qty", None),
            live_delever_price=getattr(self, "_live_delever_price", None),
            record_delever_shortfall=getattr(self, "_record_delever_shortfall", None),
            resolve_gross_ceiling=self._resolve_gross_ceiling,  # a class method: never absent on a real pipeline
            submit_protected_sell=getattr(self, "_submit_protected_sell", None),
            broker=getattr(self, "broker", None),
            config=getattr(self, "config", None),
            db=getattr(self, "db", None),
        )

    def _live_delever_price(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/ladder.py."""
        return self._delever_ladder()._live_delever_price(*args, **kwargs)

    def _resolve_gross_ceiling(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/ladder.py."""
        return self._delever_ladder()._resolve_gross_ceiling(*args, **kwargs)

    def _is_margin_floor_breach(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/ladder.py."""
        return self._delever_ladder()._is_margin_floor_breach(*args, **kwargs)

    def _conviction_cut_order(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/conviction.py."""
        return self._delever_conviction()._conviction_cut_order(*args, **kwargs)

    def _enforce_gross_ceiling(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/enforce.py."""
        return self._delever_enforce()._enforce_gross_ceiling(*args, **kwargs)

    def _record_delever_shortfall(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/enforce.py."""
        return self._delever_enforce()._record_delever_shortfall(*args, **kwargs)

    def _force_delever(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/forced.py."""
        return self._delever_forced()._force_delever(*args, **kwargs)

    def _submit_gross_ceiling_trims(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/trims.py."""
        return self._delever_trims()._submit_gross_ceiling_trims(*args, **kwargs)

    def _enforce_gross_ceiling_by_conviction(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/trims.py."""
        return self._delever_trims()._enforce_gross_ceiling_by_conviction(*args, **kwargs)

    def _discharge_deferred_gross_ceiling(self, *args, **kwargs):
        """Thin shim: body moved to src/delever/trims.py."""
        return self._delever_trims()._discharge_deferred_gross_ceiling(*args, **kwargs)

    def _alert_owner_force_delever_incomplete(
        self,
        *,
        deficit: float,
        projected_proceeds: float,
        failed_symbols: list[str],
    ) -> None:
        """Page the owner when the cash-only forced de-lever could NOT clear
        the margin deficit — the sibling of `_alert_owner_delever_incomplete`
        for the `allow_margin=False` sweep path. Never raises.

        Without this a genuinely unfillable name here (a market order the broker
        rejected, a stop-clear that failed, or simply not enough sellable value)
        would leave the account on margin with only a log line nobody reads. It
        is reporting-only: it changes no order, no sizing and no sequencing.
        """
        try:
            from src import notifier as _notifier

            shortfall = max(0.0, deficit - projected_proceeds)
            names = ", ".join(sorted(failed_symbols)) if failed_symbols else "—"
            msg = (
                f"FORCE DE-LEVER INCOMPLETE: the cash-only sweep could not "
                f"clear the ${deficit:,.2f} margin deficit (still ~"
                f"${shortfall:,.2f} short). Names that could not be sold even "
                f"at a MARKET order: {names}. The account remains on margin "
                f"until this is resolved."
            )
            logger.error(msg)
            _notifier.send_owner_alert(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("force de-lever incomplete owner alert failed: %s", exc)

    def _alert_owner_delever_incomplete(self, ctx: RunContext) -> None:
        """Flag AND page the owner when the gross-exposure de-lever did not work.

        `_enforce_gross_ceiling` already logs a warning the moment it
        *decides* to trim. What nothing checked before this is the OUTCOME:
        a trim can be submitted and still leave the book over the ceiling —
        an order that failed to place, a partial fill, integer-share
        rounding down, or the market moving between the plan and the fill
        can all produce this. That gap is unreportable, not silent: it was
        always in the log, just never in anything the owner actually reads
        (a Telegram message) or in the session result a test can assert on.
        This is a reporting-only check — it runs after every broker call in
        the de-lever is already done and changes no order, no sizing, and no
        sequencing.

        Sets `ctx.leverage["delever_incomplete"] = True` (which every
        session-result dict already threads through, since they all copy
        `ctx.leverage` verbatim) whenever we have a real, freshly-measured
        gross exposure that is still above the ceiling. Never guesses: both
        numbers come from the same post-refresh `_resolve_gross_ceiling`
        call used for the ordinary leverage line, and the check is skipped
        (not defaulted to False) when either is unmeasurable.

        Item 112: promoted from a one-line session bullet to a STANDALONE
        `send_owner_alert`, mirroring the sibling
        `_alert_owner_force_delever_incomplete` — a book left over its
        ceiling after the ladder ran is the same class of unattended
        margin risk. The page is guarded by a STATE CHANGE: it fires only on
        the transition INTO still-over (prior recorded state was cleared or
        absent), so a book that sits over the ceiling for days does not page
        every session. The flag, the log line and the shortfall evidence
        record are unchanged — they still happen every session it is over;
        only the owner PAGE is edge-triggered. No "keep selling" behaviour is
        added here (owner-escalated, out of scope), and no global throttle is
        added to `send_owner_alert`.
        """
        leverage = ctx.leverage or {}
        gross_x = leverage.get("gross_x")
        ceiling_x = leverage.get("ceiling_x")
        if not isinstance(gross_x, (int, float)) or not isinstance(ceiling_x, (int, float)):
            # Unmeasurable this session — record no state, so it neither pages
            # nor resets the transition edge for the next real measurement.
            return
        still_over = gross_x > ceiling_x

        # Read the prior session's recorded state BEFORE writing this one, so
        # the read sees only earlier sessions (transition detection). Both the
        # read and the write are best-effort: a persistence hiccup must not
        # break the de-lever path, and on a failed read we fall back to paging
        # (fail loud, never silence a real over-ceiling book).
        run_id = getattr(ctx, "run_id", None)
        was_over: bool | None
        try:
            was_over = self.db.get_last_delever_over_ceiling(exclude_run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delever ceiling-state read failed: %s", exc)
            record_guarded_pass(self.db, "delever.ceiling_state_read", exc, log=logger)
            was_over = None
        try:
            self.db.save_delever_ceiling_state(
                run_id=run_id or "",
                over_ceiling=still_over,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("delever ceiling-state write failed: %s", exc)
            record_guarded_pass(self.db, "delever.ceiling_state_write", exc, log=logger)

        if not still_over:
            return

        leverage["delever_incomplete"] = True
        logger.warning(
            "GROSS-EXPOSURE DE-LEVER: still over the ceiling after de-levering "
            "— gross exposure %.2fx equity vs a %.2fx ceiling.",
            gross_x,
            ceiling_x,
        )

        if was_over:
            # Already paged when the book first crossed into still-over; do not
            # repeat every session while it stays there.
            return

        try:
            from src import notifier as _notifier

            msg = (
                f"GROSS-EXPOSURE DE-LEVER INCOMPLETE: after de-levering, gross "
                f"exposure is still {gross_x:.2f}x equity against a "
                f"{ceiling_x:.2f}x ceiling. The book remains over its ceiling "
                f"until this is resolved."
            )
            _notifier.send_owner_alert(msg)
        except Exception as exc:  # noqa: BLE001
            logger.error("de-lever incomplete owner alert failed: %s", exc)
