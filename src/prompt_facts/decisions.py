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

    def _build_pm_recent_decisions(self, limit: int = 3) -> str:
        """PM's own last N decision sets — used to spot flip-flopping against itself."""
        try:
            rows = self._cut_candidates(
                agent_name="portfolio_manager",
                limit=limit,
                before_date=session_date_key(),
                where="prompt_facts.pm_recent_decisions.cut",
            )
        except Exception as e:
            record_guarded_pass(self.db, "prompt_facts.pm_recent_decisions", e, log=logger)
            return ""
        if not rows:
            return ""
        lines = []
        for row in reversed(rows):  # oldest→newest
            ts = (row.get("timestamp") or "")[:10]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                # PM's L6 layer reads its own recent decision history to
                # spot flip-flops. A silent skip on JSON corruption hides
                # the gap; same fix as L5 / L3d / L3f builders.
                logger.warning(
                    "pm_recent_decisions: JSON parse failed for row %s: %s",
                    ts or "?",
                    "no decision object found",
                )
                continue
            # Phase 2: new schema emits `targets` (target weights + thesis);
            # older logs in the DB carry `decisions` (legacy TradeDecision).
            # Parse whichever is present so PM reads a unified history.
            targets = data.get("targets") or []
            decisions = data.get("decisions") or []
            summary_parts: list[str] = []
            if targets:
                for t in targets[:8]:
                    if not isinstance(t, dict):
                        continue
                    sym = t.get("symbol", "?")
                    # The live schema sizes a target by `risk_allocation_pct`;
                    # `target_weight_pct` is the legacy notional field older
                    # logs carry. Reading only the legacy one rendered every
                    # recent size as "?", and a flip-flop check that cannot
                    # see the size is not a check. Tag the unit — 1% of risk
                    # and 1% of notional are not the same number.
                    risk = t.get("risk_allocation_pct")
                    weight = t.get("target_weight_pct")
                    if risk is not None:
                        w = f"{risk}%r"
                    elif weight is not None:
                        w = f"{weight}%w"
                    else:
                        w = "?"
                    conv = (t.get("conviction") or "?")[0]
                    summary_parts.append(f"{sym}→{w}({conv})")
            elif decisions:
                for d in decisions[:8]:
                    if not isinstance(d, dict):
                        continue
                    act = d.get("action", "?")
                    sym = d.get("symbol", "?")
                    alloc = d.get("allocation_pct", "?")
                    summary_parts.append(f"{act} {sym} {alloc}%")
            if not summary_parts:
                lines.append(f"- {ts}: (no trades that day)")
                continue
            rc = data.get("reasoning_chain") or {}
            sizing = (rc.get("sizing_logic") or "")[:160].strip().replace("\n", " ")
            continuity = (rc.get("continuity_check") or "")[:160].strip().replace("\n", " ")
            line = f"- {ts}: {'; '.join(summary_parts)}"
            if sizing:
                line += f"\n    sizing: {sizing}"
            if continuity:
                line += f"\n    continuity: {continuity}"
            lines.append(line)
        return "\n".join(lines)

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

    def _build_own_recent_decisions(self, limit: int = 3) -> str:
        """Pull last N position_reviewer sessions from agent_logs.

        Anti-flip-flop memory: shows the reviewer its own previous 3 sessions'
        actions per symbol so it can't silently reverse itself within hours
        without a named trigger. Complement to PM's `_build_pm_recent_decisions`.
        """
        try:
            # No before_date cutoff (audit round 2): the 15:30 close session
            # must see the 13:00 midday row — this anti-flip-flop memory says
            # "don't reverse yourself WITHIN HOURS", and the ET-midnight
            # cutoff excluded exactly those rows. The current session's own
            # row is inserted AFTER this builder runs, so no self-read.
            rows = self._cut_candidates(
                agent_name="position_reviewer",
                limit=limit,
                before_date=None,
                where="prompt_facts.own_recent_decisions.cut",
            )
        except Exception as e:
            record_guarded_pass(self.db, "prompt_facts.own_recent_decisions", e, log=logger)
            return ""
        if not rows:
            return ""
        lines: list[str] = []
        for row in reversed(rows):  # oldest → newest
            ts = (row.get("timestamp") or "")[:16]
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                continue
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                continue
            action_bits = []
            for a in actions:
                if not isinstance(a, dict):
                    continue
                sym = a.get("symbol", "?")
                act = a.get("action", "?")
                if act == "HOLD":
                    continue  # only surface actionable past decisions
                action_bits.append(f"{sym}:{act}")
            if action_bits:
                lines.append(f"- {ts}: {', '.join(action_bits[:8])}")
        return "\n".join(lines)
