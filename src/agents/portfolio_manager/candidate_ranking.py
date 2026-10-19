"""src.agents.portfolio_manager.candidate_ranking -- the standalone candidate-ranking piece.

Bodies moved verbatim from src/agents/portfolio_manager/ranking.py (CandidateRankingMixin);
the mixin keeps same-named thin shims. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no agent behind it.
"""

import logging
from datetime import date
from src.models import (
    AnalystVerdict,
    EarningsAnalysis,
    MacroAnalysis,
    Position,
    SmartMoneyFinding,
    TechAnalysisResult,
    news_verdict_for_symbol,
)
from src.risk.rules import own_bar_block_reason, own_bar_opposition_reason, seat_objections
from src.verdicts import RankedCandidate, rank_verdicts

# Same logger object the monolith used, so log capture by name is unchanged.
logger = logging.getLogger("src.agents.portfolio_manager")

#: R2 for a neutral rating on a name the desk HOLDS (owner Option A, 14 Oct
#: 2026). It still refuses the name as a NEW buy, but R3-R6 are then read in
#: the holding's own direction, so the same-rules gate judges a sale on the
#: same weighed view as a buy (`src/exits/same_rules_gate.py`).
HELD_NEUTRAL_R2 = "R2 neutral rating — held, so R3-R6 are read in the holding's direction"


class _HostState:
    """Live view of the host attributes the ranking bodies read and ASSIGN.

    `_macro_parse_failures` is read with a None default and assigned by
    `_collect_seat_verdicts`; `_macro_sectors` is called. Each goes through the
    getter/setter/callable the shim passes, never a construction-time copy.
    """

    def __init__(self, *, get_macro_parse_failures, set_macro_parse_failures, macro_sectors) -> None:
        self._get_macro_parse_failures = get_macro_parse_failures
        self._set_macro_parse_failures = set_macro_parse_failures
        self._macro_sectors = macro_sectors

    @property
    def _macro_parse_failures(self):
        return self._get_macro_parse_failures()

    @_macro_parse_failures.setter
    def _macro_parse_failures(self, value) -> None:
        self._set_macro_parse_failures(value)


class CandidateRanking:
    """Candidate eligibility, seat-verdict collection, ranking, conviction bar and its rendering.

    Standalone: every collaborator is an explicit keyword-only constructor argument.
    Bodies moved verbatim from CandidateRankingMixin (src/agents/portfolio_manager/ranking.py);
    the only mechanical changes are `cls` -> `self` (decorators dropped) and `self` inserted as the first
    parameter of each former staticmethod,
    plus `PortfolioManagerAgent` -> `self._host` (the live host-state view).
    """

    def __init__(
        self,
        *,
        get_macro_parse_failures,
        set_macro_parse_failures,
        macro_sectors,
        collect_seat_verdicts=None,
        candidate_eligibility=None,
    ) -> None:
        self._host = _HostState(
            get_macro_parse_failures=get_macro_parse_failures,
            set_macro_parse_failures=set_macro_parse_failures,
            macro_sectors=macro_sectors,
        )
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if collect_seat_verdicts is not None:
            self._collect_seat_verdicts = collect_seat_verdicts
        if candidate_eligibility is not None:
            self.candidate_eligibility = candidate_eligibility

    def candidate_eligibility(
        self,
        *,
        analyses: list[TechAnalysisResult],
        evidence_registry: dict[str, dict[str, str]],
        stale_sources: dict[str, set[str]] | None = None,
        non_corroborating_sources: dict[str, set[str]] | None = None,
        allowed_buy_symbols: set[str] | None,
        active_state_changes: str,
        asof: date | None = None,
        real_reward_risk_by_symbol: dict[str, float | None] | None = None,
        constructor_refusals_by_symbol: dict[str, dict[str, str]] | None = None,
        held_directions: dict[str, str] | None = None,
    ) -> dict[str, list[str]]:
        """Which analysed names the desk's own rules ADMIT, before the PM
        decides — `{SYMBOL: [reasons it is blocked]}`, empty list = eligible.

        These are the pre-decision halves of the gates this class and the
        constructor already enforce after submission, restated so the prompt
        can order the survivors (item 18b, `ops/model_policy/
        deterministic_selection.py::evaluate`, now in production code):

          R2  rating actionable (neutral is not a candidate). For a name
              in `held_directions` a neutral rating reads `HELD_NEUTRAL_R2`
              — still blocked as a buy — and R3-R6 are read in the
              holding's direction (owner Option A, 14 Oct 2026)
          R3  a long must be in the BUY-eligible set — the same set
              `validate_grounding` refuses increases outside of
          R4  **retired 2026-09-17.** Used to read "R/R at or above a floor",
              then "R/R must be measurable or a catalyst row names the
              symbol". Neither is a block. A range ratio, including None, is
              ranking information. A breakout is not measured on
              reward:risk. The floor argument is deleted. Catalyst is not an
              admission door.
          R5  **retired 2026-10-19** (desk archetype). Was "net source
              score >= 1". Without opposing name-level seats and macro it
              is implied by R7 for an entry and is a catalyst test a
              holding must not face. The constructor keeps the narrowed
              floor (`entry_floor_score`) as an entry backstop.
          R6  the constructor's own preview REFUSED this name by code
              (`constructor_refusals_by_symbol`, a snapshot of
              `PortfolioConstructor.last_refusals` taken after
              `real_reward_risk_preview` ran over every analysis) — today
              `no_structural_stop_and_no_volatility_reading` (docs/WORK.md
              item 180 dropped the young-listing bar-count refusal, and
              item 56 deleted the stop-WIDTH refusal
              `stop_wider_than_instrument_reach` on 2026-09-26 — a wide
              stop is answered by a smaller position, never a refusal).
              The enforcing check is one stage later, in the ONE funnel
              construction shares with the preview; this only stops the PM
              being shown a name that funnel has already refused. Absent
              structure is NOT a reason — the earlier "no floor, no trade"
              R6 was replaced the day it shipped, on sourced research.

        R1 (current technical coverage) is implied: only symbols with an
        analysis in `analyses` are considered at all. Nothing here removes or
        weakens a gate — a name this admits can still be dropped after
        submission by the stricter post-decision checks.

        **R4's number, 2026-09-04 fix.** `real_reward_risk_by_symbol` — when
        supplied, keyed by upper-case symbol — is
        `PortfolioConstructor.real_reward_risk_preview`'s output: the SAME
        derived-target, noise-floor-widened reward:risk `construct_orders`
        gates on, not `TechAnalysisResult.risk_reward` (the analyst's own
        guessed target, real arithmetic but never checked against
        structure). A 2026-09-04 audit found this gate and the
        constructor's real one passing DISJOINT sets on a real day — zero
        overlap — because this gate ran first and screened candidates on a
        different number than the one that would decide them one stage
        later. A symbol absent from the map is treated as `None` (fail
        closed, same as an unmeasurable ratio always has been here).
        Omitted entirely (`None`), this falls back to `analysis.risk_reward`
        for callers that have not wired a constructor preview through
        (rare — pre-existing tests, and any harness with no
        `PortfolioConstructor` instance to hand); production always
        supplies it.
        """
        _ = (active_state_changes, asof, real_reward_risk_by_symbol)
        allowed = {str(s).strip().upper() for s in (allowed_buy_symbols or set()) if str(s).strip()}
        # The registry, stale and broadcast maps fed R5, retired 2026-10-19;
        # kept in the signature so every caller keeps its call shape.
        _ = (evidence_registry, stale_sources, non_corroborating_sources)

        verdicts: dict[str, list[str]] = {}
        for analysis in analyses:
            symbol = analysis.symbol.upper()
            blocked: list[str] = []
            held_direction = (held_directions or {}).get(symbol)
            if analysis.rating == "neutral" and held_direction is None:
                blocked.append("R2 neutral rating")
                verdicts[symbol] = blocked
                continue
            if analysis.rating == "neutral":
                blocked.append(HELD_NEUTRAL_R2)
                direction = held_direction
            else:
                direction = "short" if analysis.rating in ("sell", "strong_sell") else "long"
            if direction == "long" and symbol not in allowed:
                blocked.append("R3 not BUY-eligible")
            # R4 is retired. Reward:risk — computed, thin, or unmeasurable —
            # does not admit or refuse. Ranking still consumes the number
            # when it exists. Catalyst rows are not a door.
            # R5 retired 2026-10-19 (desk archetype). With opposing
            # name-level seats and macro out of the net, it reduced to "the
            # chart or a name-level seat backs it": always true for a rated
            # entry (the chart rating sets the direction) and, on a holding,
            # a catalyst requirement the keep rule does not have. Admission
            # (R7) is the entry rule; the constructor keeps the floor as a
            # backstop (`entry_floor_score`).
            # R6 — the constructor's preview refused this name by code
            # (item 54). Read from the snapshot, never recomputed here: the
            # width gate and the history gate live in the one funnel the
            # preview and construction share, and a second copy could
            # drift. What R6 adds over R4 is the case R4 cannot see — a
            # breakout (no reward:risk number at all) whose stop is wider
            # than the instrument's own noise band, or a listing too young
            # to measure.
            refusal = (constructor_refusals_by_symbol or {}).get(symbol)
            if refusal and refusal.get("refusal"):
                blocked.append(
                    f"R6 constructor refused [{refusal['refusal']}] — {refusal.get('detail') or 'no detail recorded'}"
                )
            verdicts[symbol] = blocked
        return verdicts

    def _collect_seat_verdicts(
        self,
        *,
        analyses: list[TechAnalysisResult],
        news_intel: "NewsIntelligenceReport | None",
        macro_analysis: dict | None,
        earnings_analyses: list[dict],
        smart_money_findings: list[SmartMoneyFinding] | None,
        symbol_sectors: dict[str, str] | None = None,
        positions: list[Position] | None = None,
        asof: date | None = None,
    ) -> list[AnalystVerdict]:
        """Every seat's Phase 13 verdict, best-effort, one bad entry never
        drops another's or the run's.

        2026-09-03: Technical was the only seat on this shape when ranking
        first shipped. This extends it to the other four — news, macro,
        earnings, smart_money — each via its own `to_verdict()` (or, for
        news, the module-level `news_verdict_for_symbol`, since News files
        several items per symbol with no single object to call it on).

        Two of the four arrive in a DIFFERENT shape here than their own
        agent modules use: `macro_analysis` and each `earnings_analyses`
        entry's `"analysis"` key are plain dicts (already `model_dump()`'d
        for the pipeline), not live `MacroAnalysis`/`EarningsAnalysis`
        objects — re-parsed via `model_validate` before `to_verdict()` can
        be called. `smart_money_findings` and `news_intel` are already the
        real objects.

        Every per-seat, per-symbol step is wrapped: a single malformed dict,
        an `EarningsAnalysis` whose disclosed bull/bear case is the literal
        "not disclosed" placeholder (which `to_verdict()` deliberately
        refuses to construct from — see that method), or any other
        unexpected shape must drop ONLY that one seat's contribution for
        that one symbol, never the whole ranking. Mirrors the
        never-block-the-run posture already established by
        `_record_seat_stances` and `_check_levels_coverage`.

        **2026-09-13, retired item 31 — macro is now sector-adjusted.**
        It used to be applied via its plain `equity_outlook`, the same for
        every symbol, while `build_evidence_registry` — the same macro read,
        rendered into the same prompt — already resolved a per-symbol stance
        from `sector_guidance`. The two could and did disagree for any symbol
        whose sector view differed from the broad market view: one belief,
        two answers, in one prompt.

        `symbol_sectors` (the same mapping `build_evidence_registry` takes,
        from the same run-scoped `ctx.symbol_sectors` map) is now passed
        per symbol into `MacroAnalysis.to_verdict`, which resolves the sector
        stance through the identical `collapse_stances` reduction the
        registry uses and falls back to `equity_outlook` when the read stated
        nothing for that sector. See that method for why the old objection
        ("nothing sector-specific to attach") only held for conviction, and
        what is done about it.

        Sectors arrive keyed however the caller had them; matching is
        case-insensitive on the symbol, so a lower-case key still resolves.
        A symbol absent from the mapping simply gets the broad read, exactly
        as before — this can only ever make a verdict agree with the
        registry, never introduce a stance neither of them held.
        """
        verdicts: list[AnalystVerdict] = []

        for analysis in analyses:
            try:
                verdicts.append(analysis.to_verdict())
            except Exception:
                logger.warning(
                    "Phase 13: technical verdict failed for %s",
                    analysis.symbol,
                    exc_info=True,
                )

        if news_intel is not None:
            for symbol, items in (news_intel.stock_news or {}).items():
                if not items:
                    continue
                try:
                    verdict = news_verdict_for_symbol(symbol, items)
                    if verdict is not None:
                        verdicts.append(verdict)
                except Exception:
                    logger.warning(
                        "Phase 13: news verdict failed for %s",
                        symbol,
                        exc_info=True,
                    )

        if macro_analysis:
            try:
                payload = macro_analysis if isinstance(macro_analysis, dict) else macro_analysis.model_dump()
                from src.seat_heal import coerce_macro_shape, describe_macro_parse_failure

                payload, _fixes = coerce_macro_shape(payload)
                macro = MacroAnalysis.model_validate(payload)
            except Exception as exc:
                macro = None
                from src.seat_heal import describe_macro_parse_failure

                reason = describe_macro_parse_failure(
                    macro_analysis if isinstance(macro_analysis, dict) else {},
                    exc,
                )
                logger.error("Phase 13: macro_analysis failed to parse: %s", reason, exc_info=True)
                failures = getattr(self._host, "_macro_parse_failures", None)
                if not isinstance(failures, list):
                    self._host._macro_parse_failures = []
                    failures = self._host._macro_parse_failures
                failures.append(reason)
            if macro is not None:
                # ONE sector input, shared with `build_evidence_registry` and
                # `broadcast_macro_sources` (board item 109, 2026-09-26).
                # This used to build its own map from `symbol_sectors` alone
                # while the registry side ALSO back-filled `Position.sector`,
                # so for a held name with no entry in the cache the two sides
                # resolved different sectors — measured divergence on one
                # macro read and one symbol: the registry said bullish and
                # sector-specific while the verdict said bearish and
                # broadcast, opposite direction AND opposite classification,
                # with the tally and the conviction bar each acting on its
                # own answer. `_macro_sectors` is now the only resolution.
                sectors = self._host._macro_sectors(
                    list(positions or []),
                    symbol_sectors,
                )
                # Analysed names PLUS every holding (owner 2026-10-13, same
                # rules): a held name the scan did not read still gets this
                # run's macro view, from the same sector resolution.
                symbols = {a.symbol.upper() for a in analyses} | {
                    str(getattr(p, "symbol", "") or "").strip().upper()
                    for p in positions or []
                    if str(getattr(p, "symbol", "") or "").strip()
                }
                for symbol in symbols:
                    try:
                        verdicts.append(
                            macro.to_verdict(symbol, sector=sectors.get(symbol)),
                        )
                    except Exception:
                        logger.warning(
                            "Phase 13: macro verdict failed for %s",
                            symbol,
                            exc_info=True,
                        )

        for item in earnings_analyses or []:
            raw = item.get("analysis") if isinstance(item, dict) else None
            if not isinstance(raw, dict):
                continue
            wrapper_symbol = str(item.get("symbol") or "").strip().upper()
            if not wrapper_symbol:
                # No ground truth to check against at all — dropped rather
                # than trusting the LLM's own `EarningsAnalysis.symbol`
                # unverified. Matches `_earnings_stance_rows` above, which
                # drops on the identical missing-wrapper-symbol case rather
                # than falling back to the embedded one. Found in adversarial
                # review 2026-09-03: the mismatch check below only fires
                # when BOTH sides are present, which silently let an
                # unverified symbol through when the wrapper's was blank.
                continue
            # §9.4 currency, the same rule the tally uses: a stance whose
            # filing is no longer the latest periodic report (or whose
            # currency is unknown) is dropped HERE, before any rule reads
            # it — buys and judgement sales alike.
            from src.agents.portfolio_manager.evidence_prompting import earnings_stance_is_stale

            if earnings_stance_is_stale(
                str(item.get("current_until") or "").strip(),
                asof,  # the caller's decided day; the PM's own call passes none, so today (ET)
            ):
                logger.info("Phase 13: earnings stance for %s is stale — not a view", wrapper_symbol)
                continue
            try:
                earnings = EarningsAnalysis.model_validate(raw)
                # The wrapper's symbol is pipeline-set ground truth (the
                # filing this analysis was actually run against);
                # `EarningsAnalysis.symbol` is part of the LLM's own JSON
                # response and could in principle disagree (a hallucinated
                # or misread ticker) — `_earnings_stance_rows` above already
                # trusts the wrapper's symbol for exactly this reason. A
                # mismatch here is treated as malformed input, not silently
                # resolved either way, matching this codebase's fail-closed
                # posture on divergent ground-truth sources.
                if earnings.symbol.upper() != wrapper_symbol:
                    logger.warning(
                        "Phase 13: earnings symbol mismatch, wrapper=%s analysis=%s — dropped",
                        wrapper_symbol,
                        earnings.symbol,
                    )
                    continue
                verdicts.append(earnings.to_verdict())
            except Exception:
                logger.warning(
                    "Phase 13: earnings verdict failed for %s",
                    wrapper_symbol,
                    exc_info=True,
                )

        for finding in smart_money_findings or []:
            try:
                verdicts.append(finding.to_verdict())
            except Exception:
                logger.warning(
                    "Phase 13: smart_money verdict failed for %s",
                    getattr(finding, "symbol", "?"),
                    exc_info=True,
                )

        return verdicts

    def rank_candidates(
        self,
        *,
        analyses: list[TechAnalysisResult],
        evidence_registry: dict[str, dict[str, str]],
        stale_sources: dict[str, set[str]] | None = None,
        non_corroborating_sources: dict[str, set[str]] | None = None,
        allowed_buy_symbols: set[str] | None,
        active_state_changes: str,
        asof: date | None = None,
        news_intel: "NewsIntelligenceReport | None" = None,
        macro_analysis: dict | None = None,
        earnings_analyses: list[dict] | None = None,
        smart_money_findings: list[SmartMoneyFinding] | None = None,
        real_reward_risk_by_symbol: dict[str, float | None] | None = None,
        constructor_refusals_by_symbol: dict[str, dict[str, str]] | None = None,
        symbol_sectors: dict[str, str] | None = None,
        positions: list[Position] | None = None,
    ) -> tuple[list[RankedCandidate], dict[str, list[str]]]:
        """The eligible names in ranked order, plus the blocked names with
        their reasons. Ordering is `src/verdicts.py::rank_verdicts` over
        every seat's `AnalystVerdict` (2026-09-03: extended from Technical
        alone to all five — see `_collect_seat_verdicts`), every seat counted
        equally (per-seat weights deleted 2026-10-15, n06); since 2026-10-19
        the score is the count of agreeing seats, conviction unread and macro
        uncounted (`src/verdicts.py::UNCOUNTED_SEATS`).
        Blocked names are never ordered: they are reported, not ranked.

        Eligibility itself is still decided from Technical's own analyses
        only (`candidate_eligibility` — the R/R floor, structural-level, and
        catalyst gates all key off Technical's numbers) — the other seats
        only ever ADD a second, third, fourth, or fifth vote onto a symbol
        Technical already cleared. A symbol only news/macro/earnings/
        smart_money covered, with no Technical read, can never appear here;
        it was never eligible in the first place.

        `real_reward_risk_by_symbol` is passed straight through to
        `candidate_eligibility` — see that method's docstring — AND, as of
        2026-09-11 (docs/WORK.md item 1(d)), into `rank_verdicts` as the
        ordering key, alongside each candidate's setup type. That is the
        substantive half of item 1(d): the real per-trade reward:risk stopped
        being a pass/fail cutoff and became a real input to the already-
        ratified weighted ranking, and a breakout carries no such number at
        all rather than a zero.
        """
        eligibility = self.candidate_eligibility(
            analyses=analyses,
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            non_corroborating_sources=non_corroborating_sources,
            allowed_buy_symbols=allowed_buy_symbols,
            active_state_changes=active_state_changes,
            real_reward_risk_by_symbol=real_reward_risk_by_symbol,
            constructor_refusals_by_symbol=constructor_refusals_by_symbol,
            asof=asof,
            held_directions={
                str(p.symbol).strip().upper(): ("short" if p.qty < 0 else "long")
                for p in positions or []
                if str(getattr(p, "symbol", "") or "").strip() and getattr(p, "qty", 0)
            },
        )
        all_verdicts = self._collect_seat_verdicts(
            analyses=analyses,
            news_intel=news_intel,
            macro_analysis=macro_analysis,
            earnings_analyses=earnings_analyses or [],
            smart_money_findings=smart_money_findings,
            symbol_sectors=symbol_sectors,
            positions=positions,
            asof=asof,
        )
        eligible_verdicts = [v for v in all_verdicts if not eligibility.get(v.symbol.upper(), ["no eligibility row"])]
        # The desk's own real, structure-derived ratio and each candidate's
        # setup type both reach the ranking (2026-09-11, item 1(d)) so the
        # ratio is RECORDED on each candidate (a breakout carries none).
        # Since 2026-10-10 it no longer ORDERS candidates (owner ruling
        # 9 Oct: the target it uses is not a sell rule and is unscored).
        ranked = rank_verdicts(
            eligible_verdicts,
            real_reward_risk=real_reward_risk_by_symbol,
            setup_types={a.symbol.upper(): a.setup_type for a in analyses},
        )
        blocked = {s: why for s, why in eligibility.items() if why}
        return ranked, blocked

    def _apply_conviction_bar(
        self,
        *,
        ranked: list[RankedCandidate],
        blocked: dict[str, list[str]],
        held_symbols: set[str],
        all_verdicts: list[AnalystVerdict],
    ) -> tuple[list[RankedCandidate], dict[str, list[str]]]:
        """The conviction bar (R7), applied to the ranking — desk archetype,
        owner 2026-10-19 (`src/risk/conviction_bar.py`).

        A governance overlay on top of `candidate_eligibility`'s R2-R6, kept
        OUTSIDE it so the audit shadow (`ops/model_policy/
        deterministic_selection.py`) keeps mirroring those gates.

          * ENTRY (not in `held_symbols`): admitted only when the chart
            confirms AND a name-level seat (news, earnings, smart money)
            supports the direction (`own_bar_block_reason`). A failure moves
            the name OUT of `ranked` and INTO `blocked`.
          * STAY (in `held_symbols`): culled into `blocked` (rotation's
            `ineligible_hold` tier) only when the CHART is against the held
            direction (`own_bar_opposition_reason`). A soft miss drops it
            from the ranked survivors with no cull reason.
          * OBJECTIONS: an opposing name-level seat blocks neither. Each
            surviving or held name's objections (`seat_objections`: seat,
            direction, reason) are kept on `self.last_objections` and shown
            beside the row; a buy over one is refused as
            `objection_unanswered` unless the planning AI overrules it in
            writing (`_drop_unanswered_objections`).

        SAFETY INVARIANT (not enforced by the type system): every ranked
        symbol carries a technical verdict, because ranking is derived from
        technical reads. A ranked name with none is logged loudly and handled
        by the absent-technical branch (entry refused, holding not culled).
        """
        by_symbol: dict[str, list[AnalystVerdict]] = {}
        for v in all_verdicts:
            by_symbol.setdefault(v.symbol.upper(), []).append(v)
        held = {str(s).strip().upper() for s in held_symbols if str(s).strip()}
        blocked = {s: list(why) for s, why in blocked.items()}
        survivors: list[RankedCandidate] = []
        objections: dict[str, list[dict]] = {}
        for c in ranked:
            sym = c.symbol.upper()
            sym_verdicts = by_symbol.get(sym, [])
            if not any(v.seat == "technical" for v in sym_verdicts):
                logger.warning(
                    "R7 conviction bar: ranked candidate %s carries no "
                    "technical verdict in by_symbol — the ranked-implies-"
                    "technical invariant broke upstream; handling %s per the "
                    "absent-technical branch (not a silent cull, not a crash)",
                    sym,
                    sym,
                )
            reason = own_bar_block_reason(
                sym_verdicts,
                direction=c.direction,
            )
            if reason is None or sym in held:
                found = seat_objections(sym_verdicts, direction=c.direction)
                if found:
                    objections[sym] = found
            if reason is None:
                survivors.append(c)
            elif sym in held:
                # STAY: the chart against the holding is the only cull. A
                # soft miss drops the name from the entry budget order but
                # never adds a cull reason.
                opposition = own_bar_opposition_reason(
                    sym_verdicts,
                    direction=c.direction,
                )
                if opposition is not None:
                    blocked.setdefault(sym, []).append(opposition)
            else:
                # ENTRY: full-strict.
                blocked.setdefault(sym, []).append(reason)
        self.last_objections = objections
        return survivors, blocked

    def _render_candidate_ranking(
        self,
        ranked: list[RankedCandidate],
        blocked: dict[str, list[str]],
    ) -> str:
        """The prompt section. Order first, arithmetic beside each row, then
        the refused names with the gate that refused them."""
        lines = ["## Candidate Ranking (deterministic, Phase 13)"]
        if not ranked and not blocked:
            lines.append("(no Technical reads this session — nothing to rank)")
            return "\n".join(lines)
        lines += [
            "The names below passed every rule that can be checked before you "
            "decide (actionable rating; longs BUY-eligible; "
            "not refused by the order "
            "constructor's own preview). They are ORDERED by how many seats agree "
            "on the name's direction — each agreeing seat adds 1, "
            "whatever conviction it stated (a seat's own confidence is not "
            "checked, so it does not rank). Macro is not counted: its read "
            "is market-wide and reaches every name alike, so it tilts size, "
            "not order. An agreeing seat can never pull a name down. "
            "The order stays a plain count, every seat equal, until each seat's "
            "measured weight is available; the chart weighs most for a "
            "days-to-weeks hold and a fresh catalyst is the why-now. Weighing the "
            "views — which seat matters more for this name — is your job "
            "as head trader, not the ranking's. "
            "Only technical "
            "states a rating rung (strong over plain); it is NOT added to "
            "the score and only breaks a tie between equal scores. The "
            "other four seats have no rating rung at all — absent, not "
            "zero. The score is not comparable across sessions with "
            "different coverage. "
            "This is the tiebreak among equally eligible names: to take a "
            "lower-ranked name over a higher one, say what the ranking does "
            "not see. It is not a size, and it does not waive any rule "
            "below.",
        ]
        if ranked:
            for i, c in enumerate(ranked, 1):
                seats = ", ".join(c.seats)
                convictions = "/".join(v.conviction for v in c.verdicts)
                invalidation = "; ".join(f"{v.seat}: {v.invalidation}" for v in c.verdicts if v.invalidation)
                # A seat that looked and came back with no lean is stated,
                # not omitted (2026-09-13). Otherwise "macro's own sector
                # rows contradicted each other on this name" is invisible
                # here and reads exactly like "macro never covered it".
                no_lean = f" | no lean from: {', '.join(c.neutral_seats)}" if c.neutral_seats else ""
                # n06, 2026-10-16: the rating rung is NOT part of the score any
                # more; it only breaks ties, and the prompt says so. Reported
                # as ABSENT when no seat on this name states a rung.
                if c.strength_seats:
                    strength = f"rating rung {c.rung_tiebreak:.2f} from {'/'.join(c.strength_seats)} breaks ties only"
                else:
                    strength = "no rating rung stated by any seat on this name"
                no_strength = f" | no strength scale: {', '.join(c.no_strength_seats)}" if c.no_strength_seats else ""
                # Desk archetype (19 Oct 2026): an opposing name-level seat
                # is an objection, shown as data beside the row it is about.
                found = (getattr(self, "last_objections", None) or {}).get(c.symbol.upper()) or []
                objection_note = (
                    " | objections: " + "; ".join(f"{o['seat']} {o['direction']} — {o['reason']}" for o in found)
                    if found
                    else ""
                )
                lines.append(
                    f"{i}. {c.symbol} — {c.direction} | score {c.score:.2f} "
                    f"({c.components['agreeing_seats']:.0f} agreeing seat(s) "
                    f"counted of {len(c.verdicts)} leaning; {strength}) | seats: {seats} "
                    f"(stated conviction, not scored: {convictions}){no_strength}{no_lean} | invalid if "
                    f"— {invalidation}{objection_note}"
                )
        else:
            lines.append("(no name passes every pre-decision rule today)")
        if blocked:
            lines.append("")
            lines.append("Not ranked — refused by a rule, with the rule:")
            for symbol in sorted(blocked):
                lines.append(f"- {symbol}: {'; '.join(blocked[symbol])}")
        return "\n".join(lines)
