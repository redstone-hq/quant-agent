"""src.prompt_facts.review.blocked -- the blocked-proposal record.

Bodies moved verbatim from src/pipeline_prompt_facts_review.py (`PromptFactsReviewMixin`),
which keeps same-named thin shims built per call. Every collaborator is an explicit
keyword-only constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging

from src.trading_calendar import et_today

logger = logging.getLogger(__name__)


#: `cut_bite` site name for this builder's two count cuts.
CUT_SITE = "prompt_facts.review.blocked._build_blocked_proposals"


class ReviewBlocked:
    """The blocked-proposal record; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        db=None,
    ) -> None:
        self.db = db

    def _record_cut_bite(self, *, run_id, unfilled, repeats) -> None:
        """Record what the count cut here removed, for THIS session.

        `min_proposals` cuts the never-filled symbols down to the repeat
        offenders; every offender is printed. The edge is counted here, where
        the cut happens. The seat's verdict
        is NOT guessed here -- it is formed later in this same session and is
        joined to this row by `run_id` when the observation is read back.
        """
        from datetime import date

        from src.storage.analytics.cut_bite import record_cut_bite

        shown = repeats
        oldest_age: float | None = None
        stamps = [ts for _, rows in shown for ts, _ in rows if ts]
        if stamps:
            try:
                oldest = date.fromisoformat(min(stamps)[:10])
                oldest_age = float((et_today() - oldest).days)
            except (TypeError, ValueError):
                oldest_age = None
        record_cut_bite(
            db=self.db,
            run_id=run_id,
            site=CUT_SITE,
            cuts={
                "min_proposals": {"before": len(unfilled), "survived": len(repeats)},
            },
            oldest_surviving_age_days=oldest_age,
        )

    def _build_blocked_proposals(
        self,
        lookback_days: int = 21,
        min_proposals: int = 3,
        run_id: str | None = None,
    ) -> str:
        """PM memory: names it keeps asking for and never gets, and why.

        Every other per-symbol memory PM reads (loss pits, missed lessons,
        position history, R-multiples) is keyed on a POSITION, so a symbol
        that never became a position is invisible to all of them — however
        many times PM proposed it. This is the only section that can see a
        block, and a block is the cleanest feedback the desk produces: it
        arrives with its cause attached, where a filled trade's loss is
        confounded by whatever the market did next.

        Computed at prompt-build time from existing tables — no schema
        change. `specialist_evidence` marks each stage of a proposal's life
        and `decision_id` joins it to `trades`:

            target → proposed_order → verdict → execution_skip | trades.fill

        A `target` is one proposal. Targets sized to zero are EXIT
        instructions, not requests to get in, so they are excluded — a
        blocked exit is a different defect and counting it here would
        overstate the entry-side block rate.

        Every blocking reason is copied VERBATIM out of stored data —
        `execution_skip.reason` (`qty_zero`, `geometry_unmeasurable`,
        historical `geometry_rr`, `insufficient_cash`), `verdict.reason_category` (`rr_fail`, …),
        `trades.fill_status` (`canceled`, …) — so this section and the
        RM-verdict section name the same failure the same way. Exactly three
        tokens are ours: `rm_zeroed`, `order_not_placed` and
        `no_order_built`. Each describes an ABSENCE, which no table records:
        nothing was written, so nothing can be quoted. They are kept
        distinct because "the order was never built" and "the order was
        built and never placed" are different halves of the machinery.

        Conversion is judged on any `filled` trade sharing the proposal's
        `decision_id`. Today only entry orders carry a `decision_id`, so
        that is exact; if exits ever carry one, this biases toward calling a
        proposal converted, which makes the section quieter rather than
        making it cry wolf.

        Diagnostic only. Nothing here gates, filters or caps anything.
        That is final, not interim: a count-based re-proposal gate was
        ANSWERED NO on 2026-09-14 (docs/WORK.md item 10(b)) because the
        conversion rate measures this desk's own gates and plumbing, not
        the instrument. Do not add one.

        Returns "" when the window holds no proposals at all — PM's section
        then shows its own "no proposals on record" default. When there are
        proposals but no repeat offender, the aggregate line still renders
        with an explicit "none" so the desk can never mistake a quiet
        section for a missing one.
        """
        import json as _json
        from datetime import timedelta

        try:
            since = (et_today() - timedelta(days=lookback_days)).isoformat()
            raw = self.db.get_proposal_funnel_rows(since)
        except Exception as e:
            logger.warning("blocked_proposals: DB fetch failed: %s", e)
            return ""

        proposals: list[tuple[str, str, str]] = []  # (ts, decision_id, symbol)
        ordered: set[tuple[str, str]] = set()  # (decision_id, symbol)
        skips: dict[tuple[str, str], str] = {}  # → verbatim reason
        verdicts: dict[str, dict] = {}  # decision_id → verdict
        constructor_drops: dict[tuple[str, str], str] = {}  # → constructor's own reason
        data_faults: dict[tuple[str, str], str] = {}  # → FAULT_* code (unmeasurable)
        constructor_refusals: dict[tuple[str, str], str] = {}  # → "constructor_refused:<code>"
        for row in raw.get("evidence") or []:
            kind = row.get("kind")
            did = row.get("decision_id")
            if not did:
                continue
            try:
                data = _json.loads(row.get("evidence_json") or "{}")
            except (TypeError, ValueError) as e:
                # One unparseable row must not blank the whole section, but a
                # silent drop hides a proposal PM did make. Same discipline as
                # the L3d/L3f builders above.
                logger.warning(
                    "blocked_proposals: JSON parse failed for %s row %s: %s",
                    kind,
                    (row.get("timestamp") or "?"),
                    e,
                )
                continue
            if not isinstance(data, dict):
                continue
            if kind == "verdict":
                verdicts[did] = data
                continue
            sym = (row.get("symbol") or data.get("symbol") or "").strip().upper()
            if not sym:
                continue
            if kind == "target":
                # `risk_allocation_pct` is the live field; `target_weight_pct`
                # is the legacy one older rows carry. Either can size a
                # target (see TargetPosition), so read whichever is present.
                size = data.get("risk_allocation_pct")
                if size is None:
                    size = data.get("target_weight_pct")
                try:
                    if size is None or float(size) <= 0.0:
                        continue  # an exit instruction, not a proposal
                except (TypeError, ValueError):
                    continue
                proposals.append((row.get("timestamp") or "", did, sym))
            elif kind == "proposed_order":
                ordered.add((did, sym))
            elif kind == "execution_skip":
                reason = (data.get("reason") or "").strip()
                if reason:
                    skips[(did, sym)] = reason
            elif kind == "pipeline_event":
                # The deterministic constructor's own reason for dropping a
                # target before it ever became a `proposed_order` row (see
                # `pipeline_stages.DecisionStage`, which persists this via
                # `PortfolioConstructor.last_drop_reasons`). Mirrors
                # `scripts/blocked_proposals_census.py::_load_recorded_reasons`
                # (renamed 2026-09-11 when that script started reading three
                # more durable reason kinds the same way — symbol_guard,
                # hard_risk, risk_manager_unparseable_output — this PM-facing
                # helper does NOT read those three yet, only
                # `constructor_dropped`)
                # — without it, a constructor drop falls through to the
                # generic `no_order_built` bucket below with no explanation,
                # even though the real reason was captured at drop time.
                if (
                    data.get("stage") == "deterministic_gate"
                    and data.get("outcome") == "blocked"
                    and data.get("reason") == "constructor_dropped"
                ):
                    constructor_drops[(did, sym)] = data.get("detail") or "constructor_dropped"
                elif (
                    data.get("stage") == "deterministic_gate"
                    and data.get("outcome") == "unmeasurable"
                    and data.get("reason") == "data_fault"
                ):
                    # 2026-09-12: a symbol the constructor could not
                    # MEASURE (no price / ATR / usable bars / analysis).
                    # Its own bucket — not `constructor_dropped`, which is
                    # for trades the desk judged. Mirrors
                    # `scripts/blocked_proposals_census.py`.
                    data_faults[(did, sym)] = str(data.get("fault") or "unknown")
                # 2026-09-12: a refusal the constructor recorded AS DATA
                # (`PortfolioConstructor.last_refusals`, filed by
                # `DecisionStage` under `constructor_refused` with the code
                # beside it — today `no_structural_stop_and_no_
                # volatility_reading`; the young-listing bar-count refusal
                # was dropped, item 180, and the stop-WIDTH refusal
                # `stop_wider_than_instrument_reach` was deleted 2026-09-26,
                # item 56, after refusing nothing in 648 sized stops).
                # Kept apart from
                # the regex-recovered `constructor_dropped` so the digest
                # names the rule, not a sentence.
                elif (
                    data.get("stage") == "deterministic_gate"
                    and data.get("outcome") == "blocked"
                    and data.get("reason") == "constructor_refused"
                ):
                    constructor_refusals[(did, sym)] = f"constructor_refused:{data.get('refusal') or 'unknown'}"

        if not proposals:
            return ""

        fills: dict[tuple[str, str], str] = {}
        for row in raw.get("trades") or []:
            did = row.get("decision_id")
            sym = (row.get("symbol") or "").strip().upper()
            if not did or not sym:
                continue
            status = (row.get("fill_status") or "").strip().lower()
            if not status:
                continue
            # A decision can emit more than one order for a symbol (a retry, a
            # repeg). One fill converts the proposal, so a filled row wins
            # over any other status regardless of arrival order.
            if fills.get((did, sym)) == "filled":
                continue
            fills[(did, sym)] = status

        def _outcome(did: str, sym: str) -> str | None:
            """None == converted. Otherwise the verbatim blocking reason."""
            key = (did, sym)
            status = fills.get(key)
            if status == "filled":
                return None
            if status:
                return f"order_{status}"
            if key in skips:
                return skips[key]
            if key in data_faults:
                # Not a trade judgement: the desk could not measure the
                # symbol. Named by fault so a feed outage and a missing
                # analysis stay distinguishable in the digest.
                return f"data_fault:{data_faults[key]}"
            if key in constructor_refusals:
                # Same precedence as a constructor drop (the constructor
                # runs before the Risk Manager), but the CODE is the
                # category, so "no floor" aggregates under its own line.
                return constructor_refusals[key]
            if key in constructor_drops:
                # Checked before the verdict/`ordered` logic below, so a
                # symbol the deterministic constructor dropped before the
                # Risk Manager ever saw the plan is attributed to the
                # constructor, never to the RM's veto of whatever plan
                # survived. A fixed category (not the per-symbol detail
                # text) so this still aggregates in `top` below; the real
                # sentence lives in `constructor_drops[key]` for anyone
                # who wants it. Mirrors
                # `scripts/blocked_proposals_census.py::classify`.
                return "constructor_dropped"
            # A verdict rejection/zeroing is only attributed to a symbol
            # confirmed to have reached the constructor's own order list
            # (`ordered`). Without this guard every ORIGINALLY-proposed
            # symbol gets blamed for an AI Risk Manager veto — including
            # ones the deterministic constructor had already dropped
            # before the Risk Manager ever saw the plan. Mirrors
            # `scripts/blocked_proposals_census.py::classify`.
            verdict = verdicts.get(did)
            if isinstance(verdict, dict) and key in ordered:
                if verdict.get("approved") is False:
                    cat = (verdict.get("reason_category") or "").strip()
                    return f"rm_rejected:{cat}" if cat else "rm_rejected"
                for mod in verdict.get("modifications") or []:
                    if not isinstance(mod, dict):
                        continue
                    if (mod.get("symbol") or "").strip().upper() != sym:
                        continue
                    try:
                        if float(mod.get("new_value")) == 0.0:
                            return "rm_zeroed"
                    except (TypeError, ValueError):
                        continue
            if key in ordered:
                return "order_not_placed"
            return "no_order_built"

        by_symbol: dict[str, list[tuple[str, str | None]]] = {}
        block_counts: dict[str, int] = {}
        converted = 0
        for ts, did, sym in proposals:
            reason = _outcome(did, sym)
            by_symbol.setdefault(sym, []).append((ts, reason))
            if reason is None:
                converted += 1
            else:
                block_counts[reason] = block_counts.get(reason, 0) + 1

        total = len(proposals)
        pct = (100.0 * converted / total) if total else 0.0
        top = sorted(block_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        lines = [
            f"Conversion: {converted} of {total} proposals reached a fill "
            f"({pct:.0f}%) in the last {lookback_days} days.",
        ]
        if top:
            lines.append("Top blocks: " + ", ".join(f"{reason} × {n}" for reason, n in top) + ".")

        # The pool the `min_proposals` count-cut chooses FROM: every symbol
        # this window saw proposed and never filled. A symbol with a fill was
        # never a candidate for the repeat list at any count, so including it
        # would overstate what the cut removed.
        unfilled = [(sym, rows) for sym, rows in by_symbol.items() if all(reason is not None for _, reason in rows)]
        repeats = [(sym, rows) for sym, rows in unfilled if len(rows) >= min_proposals]
        # Sorted before the record so the rows the record calls "surviving"
        # are exactly the rows the prompt goes on to render.
        repeats.sort(key=lambda item: (-len(item[1]), item[0]))
        self._record_cut_bite(
            run_id=run_id,
            unfilled=unfilled,
            repeats=repeats,
        )
        if not repeats:
            lines.append(
                f"Repeat blocked names: none — no symbol was proposed "
                f"{min_proposals}+ times without a fill in this window."
            )
            return "\n".join(lines)

        lines.append(f"Repeat blocked names ({min_proposals}+ proposals, 0 fills):")
        for sym, rows in repeats:
            rows = sorted(rows, key=lambda r: r[0], reverse=True)  # newest first
            sessions = len({ts[:10] for ts, _ in rows if ts})
            recent = ", ".join(str(reason) for _, reason in rows[:3])
            lines.append(
                f"- {sym}: proposed {len(rows)}× across {sessions} sessions, filled 0 — most recent first: {recent}"
            )
        return "\n".join(lines)
