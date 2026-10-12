"""src.delever.enforce -- ceiling enforcement and the shortfall record.

Bodies moved verbatim from src/pipeline_delever.py (`DeleverMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it. Collaborators
named after a sibling body (e.g. `_live_delever_price`) are the HOST's shim, handed in,
never a body this part owns, so no recursion guard is needed.
"""

import logging


from src.pipeline_context import RunContext

from src.risk.rules import apply_gross_ceiling

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class DeleverEnforce:
    """Ceiling enforcement and the shortfall record; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        is_margin_floor_breach=None,
        open_exit_relief=None,
        resolve_gross_ceiling=None,
        submit_gross_ceiling_trims=None,
        sweep_symbol=None,
        config=None,
        db=None,
    ) -> None:
        self._is_margin_floor_breach = is_margin_floor_breach
        self._open_exit_relief = open_exit_relief
        self._resolve_gross_ceiling = resolve_gross_ceiling
        self._submit_gross_ceiling_trims = submit_gross_ceiling_trims
        self._sweep_symbol = sweep_symbol
        self.config = config
        self.db = db

    def _enforce_gross_ceiling(
        self,
        ctx: RunContext,
        *,
        floor_only: bool = False,
        conviction_rank: dict[str, tuple[int, int]] | None = None,
        planned_exits: list | None = None,
    ) -> list[dict]:
        """De-lever the HELD book when it is over the §11.2 gross ceiling.

        Runs in the session preamble, beside `_force_delever`, and therefore
        BEFORE any agent is called. That placement is the requirement, not a
        convenience: if any part of the ladder depended on the Portfolio
        Manager returning a usable book, a truncated model response would
        mean the desk stays levered exactly when it should be shedding
        exposure. Nothing here reads a PM decision.

        The ordering rule still holds and is enforced inside
        `apply_gross_ceiling`: the only decisions this call passes are
        already-working EXITS (`_open_exit_relief`), never an entry, so there
        is no new exposure to block and trims are emitted only because the
        held book alone exceeds the ceiling. New exposure proposed later in the
        same session is blocked by the sizing gate (the constructor) and the
        execution gate (`max_gross_exposure`), never by selling something the
        desk already owns to make room.

        `floor_only` (item 112) scopes this to a live-price MARGIN FLOOR: it
        fires only when the book has eroded the margin buffer the ratified
        BASE leverage cap is engineered to preserve — i.e. it is levered
        beyond the base cap, dangerously close to a broker forced
        liquidation. The morning lane passes `floor_only=True` because its
        ordinary §11.2 ceiling breach is de-levered LATER, after the PM has
        run, by `_enforce_gross_ceiling_by_conviction` — which cuts the
        WEAKEST-by-conviction names first using this session's fresh per-seat
        read, instead of the stale-stance biggest-loser cut. The margin floor
        DEFERRAL IS NOT A WAIVER. `floor_only` only says "not yet": it sets
        `ctx.gross_ceiling_deferred`, and the morning body's `finally`
        discharges that debt with a FULL ordinary-ceiling pass on every lane
        the conviction pass did not reach — the nine PM-less early returns,
        the resume lane and an exception exit included. Midday, close and
        intraday keep `floor_only=False` (default): they have no fresh read,
        so they de-lever the full §11.2 ceiling in the preamble with the
        unchanged biggest-loser ordering.

        `conviction_rank` (item 112) is the weakest-conviction-first cut
        order built by `_conviction_cut_order`. This is the ONE place that
        calls `apply_gross_ceiling` with trims enabled — the single-owner
        invariant `test_trimming_the_held_book_has_exactly_one_owner` pins —
        so the conviction pass delegates here rather than building a second
        de-lever.

        Returns the submitted orders (empty when the book is under its
        ceiling, which is the ordinary case). `ctx` is refreshed from the
        broker after fills so downstream stages see truth.
        """
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        if risk_cfg is None:
            # Tests that bypass __init__ via TradingPipeline.__new__.
            return []
        ceiling = self._resolve_gross_ceiling(ctx)
        # ASYNC-FILL RACE. Any exit this process submitted that has not
        # reached a terminal broker state is still shedding exposure the
        # refreshed book has not caught up with. Net those open quantities
        # out of the measurement (STEP 1 of `apply_gross_ceiling` subtracts
        # planned exits before it judges anything) so this pass cuts the TRUE
        # residual rather than the same exposure twice.
        open_exits, unmeasurable_exits = self._open_exit_relief(ctx.positions)
        if unmeasurable_exits:
            # An in-flight exit whose state cannot be read is the one case
            # where netting nothing would double the shed, and there is no
            # honest number to net. Leave the debt OWED — never silently
            # paid — so the next pass or the next session re-measures it.
            logger.warning(
                "Gross-exposure ceiling: an exit order from this run cannot "
                "be read — refusing to re-cut a book that may already be "
                "shedding, and leaving the ceiling recorded as still owed",
            )
            ctx.gross_ceiling_deferred = True
            return []
        if floor_only and not self._is_margin_floor_breach(ctx):
            # No genuine liquidation-proximity breach: leave an ordinary
            # §11.2 ceiling breach to the post-decision conviction pass — or,
            # if the run never reaches it, to the `finally` discharge.
            ctx.gross_ceiling_deferred = True
            return []
        if floor_only:
            # The floor trims to the ordinary ceiling, but a shortfall (an
            # unfillable name, a min-order clamp) can leave the book over it.
            # Keep the debt open; the discharge re-measures and no-ops when
            # the book did come under.
            ctx.gross_ceiling_deferred = True
        # PLANNED EXITS ALREADY DECIDED THIS SESSION. The preamble pass runs
        # before any agent, so there are none. The post-decision conviction
        # pass runs AFTER the risk stage, when the desk has already approved
        # SELL/COVER decisions the execution stage is about to submit — and
        # measuring the held book as though those names were staying counts
        # exposure that is leaving, overstates the breach, and reaches one
        # extra name. Under the weakest-first cut that extra name is by
        # construction among the STRONGEST convictions still standing, so
        # the overstatement sells exactly what the ordering exists to
        # protect. STEP 1 of `apply_gross_ceiling` subtracts these before it
        # judges anything, and its per-symbol `max()` means an exit that is
        # both decided and already working is counted once, not twice.
        # Exits ONLY: passing the session's BUYs here would let STEP 2
        # re-cut allocations the risk stage has already ruled on.
        decisions = list(open_exits) + [
            d for d in (planned_exits or []) if getattr(d, "action", None) in ("SELL", "COVER")
        ]
        outcome = apply_gross_ceiling(
            decisions,
            ctx.positions,
            ctx.total_value,
            ceiling,
            cash_park_symbol=self._sweep_symbol(),
            conviction_rank=conviction_rank,
        )
        orders = self._submit_gross_ceiling_trims(ctx, ceiling, outcome)
        if not floor_only:
            # The FULL ordinary ceiling has now been measured and acted on.
            # This is the only thing that clears the debt — a pass that
            # returned early above never marks it paid.
            ctx.gross_ceiling_deferred = False
        return orders

    def _record_delever_shortfall(
        self,
        ctx: RunContext,
        *,
        held_gross_before: float,
        ceiling_usd_before: float | None,
        equity_before: float | None,
        protections: list[dict],
    ) -> None:
        """Durable record of a gross-exposure de-lever that finished with the
        book STILL over its ceiling (docs/WORK.md item 112).

        `_alert_owner_delever_incomplete` already sets the flag the session
        message reads; what nothing kept was the evidence — the book before
        and after, the ceiling, and what each order actually did — so a
        failed de-lever could not be reviewed after the log rotated. This
        writes ONE row in the desk's existing lifecycle-event stream
        (`specialist_evidence`, `agent_name='pipeline'`,
        `kind='pipeline_event'`, `scope='run'` — the same shape
        `_record_pipeline_event` and the stop-out reconciler use), NOT in
        `agent_logs`: that table is the paid-model ledger, and the cost
        circuit refuses a same-day `agent_logs` row whose run has no budget
        session, which a pre-agent preamble write could produce.

        Observability only: no order, alert, sizing or sequencing depends on
        it, it writes nothing when the book cleared its ceiling, and it never
        raises.
        """
        leverage = ctx.leverage or {}
        if not leverage.get("delever_incomplete"):
            return
        try:
            import json

            gross_before_x = (
                held_gross_before / equity_before
                if isinstance(equity_before, (int, float)) and equity_before > 0
                else None
            )
            order_rows = [
                {
                    "symbol": p.get("symbol"),
                    "action": p.get("trim_action"),
                    "qty_submitted": p.get("trim_qty"),
                    "broker_order_id": p.get("order_id"),
                    "terminal_status": p.get("terminal_status"),
                    "stop_coverage_confirmed": p.get("coverage_confirmed"),
                }
                for p in protections
            ]
            payload = {
                "stage": "gross_delever",
                "outcome": "still_over_ceiling",
                "reason": leverage.get("reason") or "",
                "rung": leverage.get("rung"),
                "gross_usd_before": held_gross_before,
                "gross_x_before": gross_before_x,
                "equity_before": equity_before,
                "ceiling_usd_before": ceiling_usd_before,
                "gross_usd_after": leverage.get("gross_usd"),
                "gross_x_after": leverage.get("gross_x"),
                "ceiling_x": leverage.get("ceiling_x"),
                "orders": order_rows,
            }
            self.db.insert_specialist_evidence(
                run_id=ctx.run_id,
                agent_name="pipeline",
                kind="pipeline_event",
                scope="run",
                symbol=None,
                decision_id=getattr(ctx, "decision_id", None),
                evidence_json=json.dumps(payload, sort_keys=True, default=str),
            )
        except Exception as exc:  # noqa: BLE001 — evidence is never trading authority
            logger.warning(
                "GROSS-EXPOSURE DE-LEVER: could not persist the shortfall record for run %s: %s",
                ctx.run_id,
                exc,
            )
