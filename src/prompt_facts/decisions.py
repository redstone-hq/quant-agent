"""src.prompt_facts.decisions -- recent risk verdicts, recent PM decisions, review metric deltas and own recent decisions.

Bodies moved verbatim from src/pipeline_prompt_facts.py (`PromptFactsMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it.
"""

import json
import logging

from src.sentinel.counted import record_swallowed_here
from src.sentinel.reconciliation import ReconciliationLog
from src.sentinel.guarded import record_guarded_pass
from src.storage.analytics.agent_log_reads import count_recent_agent_outputs
from src.trading_calendar import session_date_key

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")


class PromptDecisions:
    """Recent risk verdicts, recent PM decisions, review metric deltas and own recent decisions; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        db=None,
        parse_logged_agent_response=None,
    ) -> None:
        self.db = db
        self._parse_logged_agent_response = parse_logged_agent_response  # the host's parser, handed in

    def _cut_candidates(self, *, agent_name: str, limit: int, before_date: str | None, where: str) -> list[dict]:
        """The limited read, plus one row recording what the LIMIT hid.

        The cut used to happen inside the query, so the number of candidates
        it chose from never existed anywhere a caller could see it. The read
        below is unchanged -- same SQL, same ORDER BY, same surviving rows --
        and the count is a separate query over the same predicate, so nothing
        here can move which rows survive.

        The row carries the candidates offered, how many survived, and the
        oldest survivor's date. It does NOT carry the seat's verdict: that is
        formed later in the session and is not observable at this site.
        """
        rows = self.db.get_recent_agent_outputs(
            agent_name=agent_name,
            limit=limit,
            before_date=before_date,
        )
        try:
            before = count_recent_agent_outputs(
                conn=self.db.conn,
                lock=self.db._lock,
                agent_name=agent_name,
                before_date=before_date,
            )
        except Exception:  # noqa: BLE001 - an observer never breaks the prompt
            record_swallowed_here(where, log=logger)
            return rows
        ReconciliationLog(conn=self.db.conn).record(
            kind=where,
            agreed=True,
            detail=json.dumps(
                {
                    "candidates_before_cut": before,
                    "survived": len(rows),
                    "cut_limit": limit,
                    "oldest_surviving": min((r.get("timestamp") or "") for r in rows)[:10] if rows else None,
                }
            ),
        )
        return rows

    def _build_review_metric_deltas(self, position_facts: dict, *, run_id: str) -> dict:
        """`{symbol: MetricDeltas}` versus this seat's previous review.

        Phase 3.2 / audit §1.5. Degrades to empty deltas (never to a wrong
        comparison) when the prior snapshot is missing or unparseable — a
        first look at a position legitimately has nothing to compare against,
        and the guard downstream treats "no prior" as "do not veto".
        """
        import json as _json
        from src.risk.exit_guard import compute_deltas

        symbols = list(position_facts or {})
        if not symbols:
            return {}
        try:
            prior_rows = self.db.get_prior_position_review_metrics(
                symbols,
                exclude_run_id=run_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "review memory: prior-metric read failed (%s) — this review runs without memory of its own last look",
                e,
            )
            prior_rows = {}
        deltas: dict = {}
        for symbol, current in (position_facts or {}).items():
            row = prior_rows.get(symbol.upper())
            prior = None
            if row:
                try:
                    prior = _json.loads(row.get("evidence_json") or "{}")
                except (TypeError, ValueError) as e:
                    logger.warning(
                        "review memory: %s prior snapshot is unparseable (%s) — treating as no prior",
                        symbol,
                        e,
                    )
                    prior = None
            deltas[symbol.upper()] = compute_deltas(
                symbol,
                prior,
                current,
                prior_timestamp=(row or {}).get("timestamp"),
            )
        return deltas
