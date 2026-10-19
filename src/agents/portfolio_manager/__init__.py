import json
import sys as _sys
import types as _types
import logging
import re
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent
from src.agents.prompt_limits import LiveLimitPrompt
from src.models import (
    AnalystVerdict,
    CandidateRejection,
    EarningsAnalysis,
    MacroAnalysis,
    NewsIntelligenceReport,
    PortfolioDecision,
    Position,
    TargetPosition,
    TechAnalysisResult,
    SmartMoneyFinding,
    news_verdict_for_symbol,
    normalize_sector_stance,
    open_target_missing_falsifier,
    parse_telemetry,
)
from src.data.news_store import ACTIVE_STATE_CHANGE_WINDOW_DAYS
from src.quantities import collapse_stances
from src.risk.constants import (
    STARTER_POSITION_RISK_PCT,
    reward_risk_floor_applies,
)
from src.risk.budget import allocate_risk_budget
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import (
    _gross_multiplier,
    book_exposure as _book_exposure,
    own_bar_block_reason,
    own_bar_opposition_reason,
    count_aligned_sources,
    count_opposing_sources,
    position_weight_pct,
    entry_floor_score,
    stance_is_aligned,
    weight_pct_of,
)
from src.rotation import (
    RotationOpportunity,
    RotationPrecheck,
    evaluate_rotation,
    funding_view_measured,
    holdings_below_entry_bar,
    rotation_binding_constraints,
)
from src.trading_calendar import et_today
from src.verdicts import RankedCandidate, rank_verdicts

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "config" / "prompts" / "portfolio_manager.md"
SETTINGS_PATH = Path(__file__).parent.parent.parent.parent / "config" / "settings.yaml"

from src.agents.portfolio_manager.grounding import (  # noqa: F401 — re-exported
    CONFLICT_UNADJUDICATED_STATUS,
    SUBFLOOR_CATALYST_UNVERIFIED_STATUS,
    _ISO_DATE_RE,
    _STATE_CHANGE_ROW_RE,
    _SYMBOL_DIRECTION_RE,
)
from src.agents.portfolio_manager.grounding import hold_decision_grounding
from src.agents.portfolio_manager.prompt_evidence import hold_prompt_evidence
from src.agents.portfolio_manager.ranking import hold_candidate_ranking
from src.agents.portfolio_manager.rotation_section import hold_rotation_section

from src.agents.portfolio_manager.registry_notes import (
    broadcast_registry_note,
    omitted_rows_line,
    stale_registry_note,
)


class PortfolioManagerAgent(
    LiveLimitPrompt,
    BaseAgent,
):
    # This seat SIZES under the desk's limits, so it is shown them rather
    # than told them: `{{risk.*}}` placeholders in
    # `config/prompts/portfolio_manager.md`, rendered from the live config at
    # construction time. Same mechanism as the Risk Manager — see
    # `src/agents/prompt_limits.py`. PM's sheet was CORRECT when the
    # reviewer's went wrong on 2026-09-11, and the reason is NOT that anyone
    # was more careful: `tests/test_prompts_anchors.py` pinned the literal
    # "capped at 65% single-name" here and had no equivalent anchor on the
    # reviewer's sheet. The mechanical check is what held. It held by keeping
    # a THIRD hand-maintained copy of the value, which is what this change
    # removes — the anchor is retargeted to the placeholder.
    _prompt_path = PROMPT_PATH
    _settings_path = SETTINGS_PATH
    _fallback_prompt = "You are a portfolio manager. Respond with JSON."
    # See decide(): parsed JSON is validated as PortfolioDecision(**parsed).
    result_model = PortfolioDecision

    #: Phase 14b. The rotation comparison this agent's LAST prompt was
    #: rendered from (`rotation_precheck`), reset at the top of every
    #: `build_user_message`. `DecisionStage._apply_rotation_execution`
    #: reads it so the desk acts on exactly what the model was shown.
    last_rotation_precheck: RotationPrecheck | None = None

    #: retired board item 49, owner decision 2026-09-12 (`docs/INCIDENT_HISTORY.md`, 2026-09-14) ("best-ranked first").
    #: The candidate ranking this agent's LAST prompt was rendered from, in
    #: `rank_verdicts` order, best first. Reset at the top of every
    #: `build_user_message` exactly like `last_rotation_precheck` above, and
    #: for the same reason: `pipeline_stages.DecisionStage` reads it to tell
    #: the constructor which order to spend the risk budget in, and a stale
    #: ranking from a previous session must never leak into this one. None
    #: means "no ranking view this session" — the allocator then falls back
    #: to its pre-decision ordering rather than having one invented for it.
    last_candidate_ranking: list[RankedCandidate] | None = None

    #: Every name refused this session (symbol -> reason), the whole `blocked`
    #: map after the conviction bar. Reset with `last_candidate_ranking`;
    #: `DecisionStage` journals one `candidate_refused` event per name.
    last_blocked: dict[str, str] | None = None

    #: Desk archetype (owner 2026-10-19): {SYMBOL: [objection]} — every
    #: opposing name-level seat on a ranked or held name (`seat_objections`),
    #: and the seat verdicts they were read from, so a target bought over an
    #: objection the AI did not overrule is dropped as `objection_unanswered`.
    last_objections: dict[str, list[dict]] | None = None
    last_seat_verdicts: list | None = None

    @property
    def name(self) -> str:
        return "portfolio_manager"

    def build_user_message(self, **kwargs) -> str:
        analyses: list[TechAnalysisResult] = kwargs["analyses"]
        positions: list[Position] = kwargs["positions"]
        macro_analysis: dict | None = kwargs.get("macro_analysis")
        cash_balance: float = kwargs["cash_balance"]
        # Short-term reserve (SGOV/cash-equivalent sweep parking), reported
        # separately from cash_balance — 2026-08-19 SGOV/deployable-
        # liquidity forensic. Never fold this into cash_balance: it is not
        # reliably spendable same-day (Alpaca T+1 equity settlement), so
        # sizing against it produces BUYs execution can't actually fund.
        reserve_balance: float = kwargs.get("reserve_balance", 0.0) or 0.0
        total_value: float = kwargs["total_value"]
        news_intel: NewsIntelligenceReport | None = kwargs.get("news_intel")
        earnings_analyses: list[dict] = kwargs.get("earnings_analyses", [])
        smart_money_findings: list[SmartMoneyFinding] = kwargs.get("smart_money_findings", [])
        evidence_registry = self.build_evidence_registry(
            analyses=analyses,
            positions=positions,
            news_intel=news_intel,
            earnings_analyses=earnings_analyses,
            macro_analysis=macro_analysis,
            smart_money_findings=smart_money_findings,
            symbol_sectors=kwargs.get("symbol_sectors") or {},
        )
        # §9.4 freshness — which registry entries are real coverage but too
        # old to EARN size. Computed from the same earnings list the registry
        # was built from, so the prompt and the constructor gate the same
        # stances (`pipeline_stages` recomputes both from identical inputs).
        # `asof` is the decision day: today (ET) in production, the recorded
        # day in a replay, so a recording judges currency as of when it ran.
        stale_sources = self.stale_evidence_sources(
            earnings_analyses=earnings_analyses,
            asof=kwargs.get("asof"),
        )
        # Item 109 — a SEPARATE mapping with a separate consequence, never
        # merged into `stale_sources`: a macro stance broadcast onto a name
        # whose sector this read never mentioned may not CORROBORATE the
        # name, while its dissent still counts. Merging the two would both
        # loosen the gate and record "stale" as the durable reason for a
        # stance that is not stale.
        non_corroborating_sources = self.broadcast_macro_sources(
            registry=evidence_registry,
            positions=positions,
            macro_analysis=macro_analysis,
            symbol_sectors=kwargs.get("symbol_sectors") or {},
        )
        evidence_registry_text = json.dumps(
            evidence_registry,
            sort_keys=True,
            indent=2,
        )
        if stale_sources:
            evidence_registry_text += stale_registry_note(
                stale_sources,
                evidence_registry,
            )
        if non_corroborating_sources:
            evidence_registry_text += broadcast_registry_note(
                non_corroborating_sources,
                evidence_registry,
            )
        # §9.4 "agreement earns size" — tell the PM the count BEFORE it
        # sizes, not after. Rendered for both directions since the PM has
        # not chosen one yet when it reads this: a name it takes long
        # counts bullish-aligned sources, one it shorts counts bearish.
        # This is the exact registry the deterministic ceiling in
        # `PortfolioConstructor` re-derives the count from — not a preview
        # of a different number. See 2026-08-20/Phase 2b's incident class:
        # a silent clamp the PM's own stated reasoning disagreed with.
        #
        # The entry backstop score (`src/risk/seat_agreement.py::
        # entry_floor_score`, the constructor's refusal) is shown with both
        # counts. Both halves are shown
        # anyway: "3 aligned, 1 opposed" and "net +2" are different facts, and
        # a PM that only saw the net could not tell a thin unanimous idea from
        # a broad contested one. Showing the net is not optional — a ceiling
        # the PM cannot predict is the 2026-08-20 incident class, where the
        # constructor silently sized against the PM's own stated reasoning.
        # Why all-zero rows are omitted: see registry_notes.omitted_rows_line.
        omitted_broadcast: list[str] = []
        omitted_stale: list[str] = []
        omitted_plain: list[str] = []

        def _agreement_line(symbol: str, sources: dict[str, str]) -> str | None:
            ignored = stale_sources.get(symbol)
            broadcast = non_corroborating_sources.get(symbol)
            # Each caveat states its OWN reason. A merged "A or B" line let a
            # reader attribute the wrong one, and the two do different things.
            notes = []
            stale_here = sorted(s for s in (ignored or ()) if s in sources)
            if stale_here:
                notes.append(
                    f"{', '.join(stale_here)} stance NOT counted either way "
                    "— filing superseded or past the next report's deadline"
                )
            broadcast_here = sorted(s for s in (broadcast or ()) if s in sources)
            if broadcast_here:
                notes.append(
                    # Item 18, 2026-09-30: the reason this note exists is
                    # IDENTICAL on every line that carries it, so it is
                    # stated ONCE under the section instead of ~110 times.
                    # What stays per line is the only per-line fact: WHICH
                    # source was broadcast. Prior wording repeated 130-odd
                    # characters of explanation per symbol, measured at 14.9%
                    # of the whole briefing.
                    f"{', '.join(broadcast_here)} stance broadcast — one-sided, see note below"
                )
            stale_note = f"; {'; '.join(notes)}" if notes else ""
            # The ALIGNED side drops both; the OPPOSED side drops only the
            # stale stance. That asymmetry is the whole point of item 109's
            # one-sided gate. The backstop score below is computed by
            # `entry_floor_score`, exactly as the constructor computes it.
            for_ignored = frozenset(ignored or ()) | frozenset(broadcast or ())
            long_for = count_aligned_sources(symbol, sources, "long", ignored_sources=for_ignored)
            long_against = count_opposing_sources(symbol, sources, "long", ignored_sources=ignored)
            short_for = count_aligned_sources(symbol, sources, "short", ignored_sources=for_ignored)
            short_against = count_opposing_sources(symbol, sources, "short", ignored_sources=ignored)
            # The SAME function and arguments the constructor refuses on
            # (`PortfolioConstructor._plan_risk_targets`), so the score the
            # AI reads is the score that decides. Macro and opposing
            # name-level seats are not in it (`entry_floor_score`).
            long_net = entry_floor_score(symbol, sources, "long", ignored_sources=ignored)
            short_net = entry_floor_score(symbol, sources, "short", ignored_sources=ignored)
            if not any((long_for, long_against, short_for, short_against)):
                if broadcast_here:
                    omitted_broadcast.append(symbol)
                elif stale_here:
                    omitted_stale.append(symbol)
                else:
                    omitted_plain.append(symbol)
                return None
            return (
                f"- {symbol}: if long {long_for} seat(s) for / {long_against} against, "
                f"entry backstop {long_net:+d}; "
                f"if short {short_for} seat(s) for / {short_against} against, "
                f"entry backstop {short_net:+d} "
                f"(of {len(sources)} source(s) with current coverage{stale_note})"
            )

        rendered_agreement = [_agreement_line(symbol, sources) for symbol, sources in sorted(evidence_registry.items())]
        agreement_lines = [line for line in rendered_agreement if line is not None]
        omitted_agreement_rows = sum(1 for line in rendered_agreement if line is None)
        if omitted_agreement_rows:
            agreement_lines.append(
                omitted_rows_line(
                    omitted_agreement_rows,
                    len(omitted_broadcast),
                    len(omitted_stale),
                )
            )
        agreement_text = "\n".join(agreement_lines) if agreement_lines else "No symbols with current coverage."
        allowed_buy_symbols = sorted(
            {str(symbol).strip().upper() for symbol in (kwargs.get("allowed_buy_symbols") or []) if str(symbol).strip()}
        )
        transient_admitted_symbols = sorted(
            {
                str(symbol).strip().upper()
                for symbol in (kwargs.get("transient_admitted_symbols") or [])
                if str(symbol).strip()
            }
        )
        permanent_symbols = [symbol for symbol in allowed_buy_symbols if symbol not in set(transient_admitted_symbols)]
        eligibility_section = (
            "## Deterministic BUY Eligibility\n"
            f"- Permanent configured universe: {', '.join(permanent_symbols) or 'none'}\n"
            "- Temporary SEC Form 4 admissions for THIS RUN only: "
            f"{', '.join(transient_admitted_symbols) or 'none'}\n"
            "Temporary admission permits evaluation; it is not a recommendation, "
            "does not waive Technical/Risk requirements, and does not permanently "
            "change the universe. Do not target any other new symbol."
        )

        def _fmt_tech(a):
            rr = a.risk_reward
            rr_str = f"R/R {rr:.2f}:1" if rr is not None else "R/R n/a"
            invalid = a.thesis_invalid_if or "(not specified)"
            age = getattr(a, "signal_age_days", None)
            age_str = f", age {age}d" if age is not None and age > 0 else ""
            # PM TEST GATE item 7 — a `neutral` technical read has, by
            # construction, no entry, no stop, no reference target and
            # therefore no reward/risk: `TechAnalysisResult.risk_reward` is
            # None for one. Measured on the frozen run_64290730 fixture: 21
            # of the 59 reads were neutral, and each spent a full four-field
            # geometry line printing the word "None" three times plus an
            # "Invalid if: (not specified)" line — 21 symbols' worth of
            # fields that exist only to say the field is absent. The
            # analyst's own one-sentence conclusion is the whole content of
            # such a read, so that is what is rendered, verbatim and
            # untruncated. This is a shape change, not a cap: no report is
            # dropped, no text is shortened, and a read that HAS geometry
            # still renders every field it has.
            if a.rating == "neutral" and rr is None:
                return (
                    f"- {a.symbol}: neutral ({a.conviction}{age_str}) | "
                    f"no entry/stop/target, not sizeable this session\n"
                    f"  Reasoning: {a.reasoning}"
                )
            return (
                f"- {a.symbol}: {a.rating} ({a.conviction}{age_str}) | {rr_str} | "
                f"Entry: {a.entry_price} | Stop: {a.stop_loss} | Target: {a.target_text}\n"
                f"  Invalid if: {invalid}\n"
                + (f"  {a.missing_levels_text}\n" if getattr(a, "missing_levels_text", None) else "")
                + f"  Reasoning: {a.reasoning}"
            )

        analyses_text = "\n".join(_fmt_tech(a) for a in analyses)

        # Phase 13 — the missing ordering step. Every gate above admits or
        # refuses; none of them ranks. The verdicts of the seats that have
        # been moved onto the shared shape (Technical only, so far) are
        # scored at the ratified equal weight and the eligible names are
        # shown IN ORDER, so "which of the twelve" is a stated rule rather
        # than whatever the model defaults toward. Names a gate refuses
        # are listed with the gate that refused them and are NOT ordered.
        # Item 49: reset FIRST, so a raise inside `rank_candidates` leaves no
        # previous session's ranking behind for the constructor to spend this
        # session's budget against.
        self.last_candidate_ranking = None
        self.last_blocked = None
        self.last_objections = None
        self.last_seat_verdicts = None
        ranked, blocked = self.rank_candidates(
            analyses=analyses,
            evidence_registry=evidence_registry,
            stale_sources=stale_sources,
            non_corroborating_sources=non_corroborating_sources,
            positions=positions,
            allowed_buy_symbols=set(allowed_buy_symbols),
            active_state_changes=kwargs.get("active_state_changes") or "",
            news_intel=news_intel,
            macro_analysis=macro_analysis,
            earnings_analyses=earnings_analyses,
            smart_money_findings=smart_money_findings,
            real_reward_risk_by_symbol=kwargs.get("real_reward_risk_by_symbol"),
            constructor_refusals_by_symbol=kwargs.get("constructor_refusals_by_symbol"),
            # The same mapping `build_evidence_registry` is given a few lines
            # above, so macro's ranked verdict and macro's registry stance
            # resolve one symbol's sector identically (item 31, 2026-09-13).
            symbol_sectors=kwargs.get("symbol_sectors") or {},
            asof=kwargs.get("asof"),
        )

        # R7 — the 2026-09-25 owner conviction bar, applied ONCE here to the
        # candidate_eligibility result and consumed by BOTH sides below: the
        # PM prompt (ENTRY) and, through the same `blocked` set,
        # `holdings_below_entry_bar` / rotation's `ineligible_hold` tier
        # (STAYING). One definition, both sides. It rides on TOP of the R2-R6
        # pre-decision gates rather than inside `candidate_eligibility`, so the
        # audit shadow (`ops/model_policy/deterministic_selection.py`) still
        # mirrors those gates exactly; R7 is a NEW governance overlay on seat
        # AGREEMENT, separate from the §9.4 net-evidence floor and from the
        # continuous ranking score. `_apply_conviction_bar` moves any ENTRY
        # candidate that fails the bar out of `ranked` and into `blocked`, so the
        # constructor's budget ordering (`last_candidate_ranking`) can never
        # fund a name the bar refused. For a currently-HELD name (desk archetype,
        # owner 2026-10-19) it is culled into `blocked` only when the CHART is
        # against it; an opposing name-level seat is an objection
        # (`last_objections`) for the AI to answer, never a cull, and a held
        # name that fails the ENTRY bar on soft grounds is dropped from the
        # ranked survivors with no cull reason.
        _held_now = {(p.symbol or "").strip().upper() for p in positions if (p.symbol or "").strip()}
        self.last_seat_verdicts = self._collect_seat_verdicts(
            analyses=analyses,
            news_intel=news_intel,
            macro_analysis=macro_analysis,
            earnings_analyses=earnings_analyses or [],
            smart_money_findings=smart_money_findings,
            symbol_sectors=kwargs.get("symbol_sectors") or {},
            positions=positions,
            asof=kwargs.get("asof"),
        )
        ranked, blocked = self._apply_conviction_bar(
            ranked=ranked,
            blocked=blocked,
            held_symbols=_held_now,
            all_verdicts=self.last_seat_verdicts,
        )
        # The bar's objections live on the held ranking part (it renders
        # them); copied here so this session's record is on the agent.
        self.last_objections = dict(getattr(getattr(self, "_candidate_ranking", None), "last_objections", None) or {})
        self.last_candidate_ranking = list(ranked)
        self.last_blocked = {str(k): str(v) for k, v in dict(blocked).items()}

        ranking_section = self._render_candidate_ranking(ranked, blocked)

        # Phase 14 — opportunity-cost rotation. The ranking above orders
        # eligible names; it never asks whether capital tied up in a weak
        # holding is the actual reason a stronger new idea has no room.
        # Silent unless the book's EXISTING risk (before anything this
        # session proposes) already leaves less headroom than the desk's
        # own minimum tradeable size — see `src/rotation.py` for the
        # citations behind the margin and why the check is silent
        # otherwise. `existing_risk_pct` is None when the book's risk is
        # not visible this session (facts unavailable) — same fail-open
        # posture as every other consumer of it, never a fabricated view.
        existing_risk_pct: dict[str, float] | None = kwargs.get("existing_risk_pct")
        max_portfolio_risk_pct = float(kwargs.get("max_portfolio_risk_pct", 25.0) or 25.0)
        held_symbols = {p.symbol.upper() for p in positions if getattr(p, "qty", 0)}
        # Phase 14b: the precheck is computed ONCE and kept on the agent so
        # `DecisionStage._apply_rotation_execution` acts on exactly the
        # numbers the model was shown — never a second evaluation against
        # inputs that may have moved in between. Reset first so a stale
        # value from a previous session can never leak into this one.
        self.last_rotation_precheck = None
        # 2026-09-23. The funding half of the rotation precondition, from
        # the SAME `_entry_deployment_budget` figure the Margin Capacity
        # section above is rendered from and execution sizes entries
        # against — never a second computation of "is the book full".
        # `margin_ladder_backed=False` means that computation could not
        # resolve this session, and `None` then switches the funding test
        # off rather than letting an unreadable input read as "there is
        # room". See `src/rotation.py` for why this constraint, and not the
        # risk budget alone, is what has bound this desk.
        _entry_budget_usd = kwargs.get("margin_headroom_usd")
        if (
            not bool(kwargs.get("margin_ladder_backed", False))
            or not isinstance(
                _entry_budget_usd,
                (int, float),
            )
            or isinstance(_entry_budget_usd, bool)
        ):
            _entry_budget_usd = None
        from src.risk.min_risk import MinRiskFloor

        _min_risk_floor = kwargs.get("min_risk_floor")
        if not isinstance(_min_risk_floor, MinRiskFloor):
            _min_risk_floor = None
        # Owner decision 2026-10-15 (q07): the swap brake is the desk's own
        # measured score noise; anything not a real number is unmeasured.
        _score_noise_brake = kwargs.get("score_noise_brake")
        if isinstance(_score_noise_brake, bool) or not isinstance(_score_noise_brake, (int, float)):
            _score_noise_brake = None
        rotation_precheck = self.rotation_precheck(
            ranked=ranked,
            blocked=blocked,
            held_symbols=held_symbols,
            existing_risk_pct=existing_risk_pct,
            ceiling_pct=max_portfolio_risk_pct,
            entry_budget_usd=(None if _entry_budget_usd is None else float(_entry_budget_usd)),
            min_risk_floor=_min_risk_floor,
            score_noise_brake=_score_noise_brake,
        )
        self.last_rotation_precheck = rotation_precheck
        rotation_section = self._render_rotation_section(
            ranked=ranked,
            blocked=blocked,
            held_symbols=held_symbols,
            existing_risk_pct=existing_risk_pct,
            ceiling_pct=max_portfolio_risk_pct,
            precheck=rotation_precheck,
            execute_enabled=bool(kwargs.get("rotation_execute_enabled", False)),
            ranked_margin_enabled=bool(
                kwargs.get("rotation_ranked_margin_enabled", False),
            ),
        )

        # L2 memory: each position line also gets entry context + Tech rating trajectory
        # so PM can anchor "when bought / for what reason / how signal has evolved".
        position_history: dict = kwargs.get("position_history") or {}

        def _fmt_position(p: Position) -> str:
            # audit round 2 #22: show the GROSS weight — the same basis
            # PortfolioConstructor uses when comparing target_weight_pct to
            # current weights (leveraged/inverse ETF market value × |mult|).
            # Rendering the raw weight made PM restate e.g. a 3x SQQQ's 6%
            # as its target, which the constructor read as "cut from 18% to
            # 6%" and emitted a 67% SELL the PM never intended.
            gross_mul = _gross_multiplier(p.symbol)
            weight_pct = position_weight_pct(p, total_value)
            lev_note = f" (gross, {gross_mul:g}x leveraged)" if gross_mul != 1.0 else ""
            # Flag drift candidates directly in the line so PM can't miss them.
            # P&L% tells PM whether the weight came from price appreciation (drift)
            # or a large entry.
            # `unrealized_pnl_pct` is the single definition (see
            # src/risk/metrics.py). The `cost_basis > 0` guard this replaces
            # printed a literal +0.0% for every short — a winning short
            # rendered `P&L: $1000.00 (+0.0%)`, self-contradicting on one
            # line. None means genuinely unknowable, and must not drift-flag.
            pnl_pct = unrealized_pnl_pct(p)
            pnl_pct_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "n/a"
            core = (
                f"- {p.symbol}: {p.qty} shares @ ${p.avg_entry:.2f} | "
                f"Current: ${p.current_price:.2f} | P&L: ${p.unrealized_pnl:.2f} ({pnl_pct_str}) | "
                f"Weight: {weight_pct:.1f}%{lev_note} | Sector: {p.sector}"
            )
            hist = position_history.get(p.symbol) or {}
            lines = [core]
            entry_date = hist.get("entry_date")
            days_held = hist.get("days_held")
            if entry_date or days_held is not None:
                label = f"entry {entry_date or 'unknown'}"
                if days_held is not None:
                    label += f", held {days_held}d"
                reasoning = (hist.get("entry_reasoning") or "").strip()
                if reasoning:
                    label += f' — "{reasoning}"'
                lines.append(f"  Bought: {label}")
            tech_hist = hist.get("tech_history") or []
            if tech_hist:
                trail = " → ".join(f"{h.get('rating', '?')}({h.get('conviction', '?')[0]})" for h in tech_hist)
                lines.append(f"  Tech history (last {len(tech_hist)}d): {trail}")
            return "\n".join(lines)

        positions_text = "\n".join(_fmt_position(p) for p in positions) if positions else "No current positions."

        # Format macro analysis section
        if macro_analysis:
            observations_text = (
                "\n".join(
                    f"- {o['indicator']}: {o['reading']} — {o['interpretation']}"
                    for o in macro_analysis.get("key_observations", [])
                )
                if macro_analysis.get("key_observations")
                else "No observations."
            )

            # Rendered through the same normalizer the evidence registry uses:
            # the model is told to copy the validated stance exactly, so a
            # Macro section speaking a different vocabulary than the registry
            # is an invitation to cite a stance the validator will reject.
            # `reason` survives only in the live shape — MacroStore drops it.
            guidance_rows = self._sector_guidance_rows(macro_analysis.get("sector_guidance"))
            reasons = {
                str(row.get("sector")): str(row.get("reason") or "")
                for row in (macro_analysis.get("sector_guidance") or [])
                if isinstance(row, dict)
            }

            def _fmt_guidance(row: dict) -> str:
                reason = reasons.get(row["sector"], "")
                return f"- {row['sector']}: {row['stance']}" + (f" — {reason}" if reason else "")

            sector_guidance_text = (
                "\n".join(_fmt_guidance(row) for row in guidance_rows) if guidance_rows else "No sector guidance."
            )

            risk_factors_text = (
                "\n".join(f"- {r}" for r in macro_analysis.get("risk_factors", []))
                if macro_analysis.get("risk_factors")
                else "None identified."
            )

            pos_guidance = macro_analysis.get("position_guidance", {}) or {}
            rc = macro_analysis.get("reasoning_chain", {}) or {}

            shift_line = ""
            if macro_analysis.get("regime_shift"):
                shift_line = f"\n- **REGIME SHIFT TODAY**: {macro_analysis.get('shift_reason', 'reason unspecified')}"

            alignment = macro_analysis.get("alignment_with_news", "")
            alignment_line = f"\n- News alignment: {alignment}" if alignment else ""

            reasoning_section = ""
            if rc:
                reasoning_section = f"""

### Macro Reasoning Chain (audit these for logic errors — report in `reasoning_chain.macro_audit`)
- Volatility: {rc.get("volatility_analysis", "N/A")}
- Yield curve: {rc.get("yield_curve_analysis", "N/A")}
- Monetary policy: {rc.get("monetary_policy_analysis", "N/A")}
- Inflation/labor/credit: {rc.get("inflation_labor_credit", "N/A")}
- Cross-signal synthesis: {rc.get("cross_signal_synthesis", "N/A")}
- Sector implications: {rc.get("sector_implications", "N/A")}"""

            bull_triggers = macro_analysis.get("bull_triggers", []) or []
            bear_triggers = macro_analysis.get("bear_triggers", []) or []
            triggers_section = ""
            if bull_triggers or bear_triggers:
                bull_text = "\n".join(f"  + {t}" for t in bull_triggers) or "  (none)"
                bear_text = "\n".join(f"  - {t}" for t in bear_triggers) or "  (none)"
                triggers_section = f"""

### View-Change Triggers
Bull triggers (would turn more constructive):
{bull_text}
Bear triggers (would turn defensive):
{bear_text}"""

            # No invested / cash numbers: owner mandate 2026-09-17, the book
            # is fully invested and macro informs direction only. Older
            # snapshots still carry `target_invested_pct` /
            # `cash_recommendation_pct`; they are deliberately not rendered.
            # Board item 119. A regime call formed on an incomplete FRED set
            # is not a complete read and must never be rendered as one. The
            # stamp is the deterministic fetch record
            # (`src/data/macro.py::MacroCoverage.verdict_stamp`), not the
            # economist's self-assessment, and it survives the macro_store
            # round trip so a CARRIED partial read still says so here. An
            # unstamped verdict ("unknown") prints nothing: it makes no claim
            # in either direction and inventing one would be the same defect
            # pointed the other way.
            coverage_state = str(macro_analysis.get("coverage_state") or "unknown")
            coverage_line = ""
            if coverage_state in ("partial", "failed"):
                note = str(macro_analysis.get("coverage_note") or "").strip()
                coverage_line = (
                    "\n- **PARTIAL READ — this view was formed on an INCOMPLETE "
                    "macro set"
                    + (f" ({note})" if note else "")
                    + ".** The missing series are gaps, not calm readings. Do not "
                    "treat this regime call as a complete read of the macro "
                    "picture, and do not cite an indicator that is not listed "
                    "below as confirming anything."
                )
            macro_section = f"""## Macro Analysis{coverage_line}
- Regime: {macro_analysis.get("regime", "N/A")} | Outlook: {macro_analysis.get("equity_outlook", "N/A")} | Confidence: {macro_analysis.get("confidence", "N/A")}{shift_line}{alignment_line}
- Summary: {macro_analysis.get("summary", "N/A")}{reasoning_section}

### Key Observations
{observations_text}

### Sector Guidance
{sector_guidance_text}

### Risk Factors
{risk_factors_text}{triggers_section}

### Directional Lean (the book stays fully invested — this is which way, not how much)
- Reasoning: {pos_guidance.get("reasoning", "N/A")}"""
        else:
            macro_section = "## Macro Analysis\nNo macro data available."

        # Format news intelligence section (3-layer)
        if news_intel:
            # Layer 1: Macro narrative
            mn = news_intel.macro_narrative
            era_text = "; ".join(mn.era_themes) if mn.era_themes else "N/A"
            state_items = (
                "\n".join(f"  - {k}: {v}" for k, v in mn.key_state_tracker.items())
                if mn.key_state_tracker
                else "  No tracked states."
            )

            # Layer 2: State changes
            if news_intel.state_changes:
                changes_text = "\n".join(
                    f"- [{c.conviction.upper()}] {c.event}\n  Was: {c.previous_state} → Now: {c.new_state}\n  Impact: {c.market_impact}"
                    for c in news_intel.state_changes
                )
            else:
                changes_text = "No significant state changes today."

            # Layer 3: Stock-specific (sorted by conviction, top 3 per symbol)
            _conv_order = {"high": 0, "medium": 1, "low": 2}
            stock_items = []
            for sym, alerts in news_intel.stock_news.items():
                sorted_alerts = sorted(alerts, key=lambda a: _conv_order.get(a.conviction, 9))
                for a in sorted_alerts[:3]:
                    stock_items.append(f"- {sym}: [{a.conviction.upper()}] {a.sentiment} — {a.impact_summary}")
            stock_text = "\n".join(stock_items) if stock_items else "No stock-specific news."
            lost_text = news_intel.format_dropped_symbols_block()

            news_section = f"""## News Intelligence
### PM Briefing
{news_intel.pm_briefing}

### Macro Narrative (Grand Backdrop)
- Regime: {mn.current_regime}
- Era themes: {era_text}
- State tracker:
{state_items}

### State Changes (What Changed Today)
{changes_text}

### Stock-Specific News
{stock_text}{lost_text}

Overall sentiment: {news_intel.format_market_sentiment()} (confidence: {news_intel.confidence})"""
        else:
            news_section = "## News Intelligence\nNo news data available."

        if smart_money_findings:
            smart_money_section = "## Smart Money Evidence\n" + "\n".join(
                f"- {f.symbol}: stance={f.stance}; role={f.economic_role}; {f.summary} Why now: {f.why_now}"
                for f in smart_money_findings
            )
        else:
            smart_money_section = (
                "## Smart Money Evidence\nNo material source-backed finding available. Do not claim coverage."
            )

        # Format earnings analysis section
        if earnings_analyses:
            earnings_items = []
            # PM TEST GATE item 7 — filings the seat read without reaching a
            # direction are collected here and rendered as one line each at
            # the end of the section, instead of a four-line verdict block
            # whose direction, thesis and falsifier are all absent. See
            # `_render_earnings_no_call_rollup` for the measurement and for
            # what is guaranteed to survive.
            earnings_no_call: list[dict] = []
            for ea in earnings_analyses:
                sym = ea.get("symbol", "?")
                # Queued placeholder — new filing dropped today, LLM still analyzing.
                if ea.get("queued") and not ea.get("analysis"):
                    earnings_items.append(
                        f"### {sym} — {ea.get('form_type', '?')} ({ea.get('filing_date', '?')}) "
                        f"[JUST FILED — analysis in progress, not yet ready for this run]\n"
                        f"- Discount any prior-quarter cached data for {sym} accordingly. "
                        f"New filing's numbers and guidance will be available next session."
                    )
                    continue
                analysis = ea.get("analysis")
                if not analysis:
                    continue
                filing_label = f"{ea.get('form_type', '?')} ({ea.get('filing_date', '?')})"
                source_note = " [from cache]" if not ea.get("is_new") else " [new filing]"
                # §9.4 freshness: `[from cache]` and a filing date were
                # already here, so the model COULD see the age — but the same
                # stance was simultaneously being counted as a live
                # corroborating source in the agreement block below. Say
                # plainly which way it is, in the section the PM actually
                # reads the view from.
                if "earnings" in stale_sources.get(str(sym).strip().upper(), frozenset()):
                    source_note += (
                        " [STALE: superseded or past next report's deadline — context only; "
                        "does NOT count toward the agreement score]"
                    )

                impl = (analysis or {}).get("investment_implications") or {}
                # Roll up ONLY a non-directional read. "mixed" is a
                # disagreement, not an absence, and stays a full block: a
                # summary that hides a split between seats is worse for the
                # decision seat than the prose it replaces.
                if self._collapse_stances([impl.get("sentiment")]) in (None, "neutral"):
                    earnings_no_call.append(
                        {
                            "symbol": sym,
                            "filing_label": filing_label,
                            "conviction": impl.get("conviction", "N/A"),
                            "source_note": source_note,
                        }
                    )
                    continue

                earnings_items.append(
                    self._render_earnings_verdict(
                        sym=sym,
                        analysis=analysis,
                        filing_label=filing_label,
                        source_note=source_note,
                        analysis_path=ea.get("analysis_path"),
                    )
                )
            rollup = self._render_earnings_no_call_rollup(earnings_no_call)
            if rollup:
                earnings_items.append(rollup)
            earnings_section = "## Earnings Analysis (from SEC Filings)\n\n" + "\n\n".join(earnings_items)
        else:
            earnings_section = "## Earnings Analysis\nNo recent earnings filings available."

        # Account Status "Invested" reads the SAME `book_exposure` the
        # PMFacts Book State block and the pre-trade `deployment_gap`
        # advisory read. It used to be `total_value - cash_balance`, a third
        # definition of the same quantity inside this one prompt.
        #
        # That subtraction is not merely a different basis, it is wrong in a
        # specific direction: equity is `cash + sum(market_value)` and a held
        # short's `market_value` is NEGATIVE, so every short made the book
        # look LESS invested to the PM — which then deployed more. Deployment
        # is unsigned: shorting is capital put to work.
        book = _book_exposure(positions, total_value)
        invested = book.deployed_usd
        invested_pct = book.deployed_pct
        net_exposure_pct = book.net_pct

        # Margin policy — when allow_margin is False and cash is already
        # negative, de-lever SELLs are mandatory this session. The risk
        # engine will hard-block any new BUY that doesn't fit in cash, so
        # surfacing the mandate here gives the LLM the chance to pick
        # which positions to trim rather than having every BUY rejected
        # without context.
        allow_margin: bool = bool(kwargs.get("allow_margin", True))
        from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD

        if not allow_margin and cash_balance < -MARGIN_DEFICIT_FLOOR_USD:
            deficit = -cash_balance
            margin_section = (
                "## ⚠️ DE-LEVER MANDATE (margin disabled, cash is negative)\n"
                f"- Current cash: ${cash_balance:,.2f} (deficit ${deficit:,.2f})\n"
                f"- Policy: this account runs cash-only — new BUYs cannot draw margin.\n"
                f"- **You MUST emit SELL targets summing to at least ${deficit:,.2f} of "
                f"market value this session.** Pick the weakest-conviction / most-extended "
                f"positions per your usual rules.\n"
                "- Any BUY you propose will be hard-blocked until cash is ≥ 0 after the "
                "session's SELLs clear."
            )
        elif not allow_margin:
            margin_section = (
                "## Margin Policy\n"
                "- Cash-only account: BUYs are capped at available cash after prior "
                "BUYs this session. Margin is disabled."
            )
        else:
            # Margin is enabled. Do NOT tell the model cash is the limit —
            # the account may run to the §11.2 de-levering ladder's ceiling,
            # not just to settled cash (2026-09-17 CRM incident: the prompt
            # said "no margin is deployable" while $11.4k of real ladder
            # headroom existed and execution had already spent margin three
            # hours earlier that same session).
            #
            # `margin_headroom_usd` / `margin_ladder_multiple` /
            # `margin_ladder_rung` are threaded in from the SAME §11.2
            # computation execution's submit loop uses
            # (`_entry_deployment_budget` / `_session_gross_ceiling` in
            # `src/pipeline_stages.py`) — this section never derives its own
            # number. `margin_ladder_backed=False` means that computation
            # could not resolve this session (e.g. equity/ceiling unreadable
            # at prompt-build time), and the section says so rather than
            # guessing a figure.
            headroom_usd = kwargs.get("margin_headroom_usd")
            ladder_multiple = kwargs.get("margin_ladder_multiple")
            ladder_rung = kwargs.get("margin_ladder_rung")
            ladder_backed = bool(kwargs.get("margin_ladder_backed", False))
            if ladder_backed and isinstance(headroom_usd, (int, float)) and isinstance(ladder_multiple, (int, float)):
                margin_section = (
                    "## Margin Capacity (margin is ENABLED)\n"
                    f"- This account may run gross exposure up to "
                    f"{ladder_multiple:.2f}x equity (the §11.2 de-levering "
                    f"ladder's current ceiling, rung {ladder_rung}) — NOT just "
                    f"up to available cash.\n"
                    f"- Ladder headroom remaining this session: "
                    f"${headroom_usd:,.2f}. A BUY or SHORT may still draw on "
                    f"this even when Cash Balance above is negative.\n"
                    f"- This is the same figure execution sizes new entries "
                    f"against — not a separate estimate."
                )
            else:
                margin_section = (
                    "## Margin Policy\n"
                    "- Margin is ENABLED for this account, but this session's "
                    "ladder headroom could not be resolved for this prompt. "
                    "Do NOT read the raw Cash Balance above as your spending "
                    "limit — a BUY may still be able to draw margin. Treat "
                    "the ladder as unknown, not as zero."
                )
            # Board item 95. Capacity without its price is half the picture:
            # this block has named the spending limit since 2026-09-17 and
            # never once named the cost, while the PM's own sheet says "You
            # may borrow". The cost lines go on BOTH branches — the
            # unresolved-ladder branch is exactly where a seat is most
            # likely to reach for margin on a guess — and are silent when
            # there is no debit and no headroom to price. Rate read here
            # rather than threaded through kwargs so an older caller that
            # predates this cannot silently drop the price; a config read
            # that fails must never break the prompt, so it degrades to
            # saying nothing extra rather than to a fabricated figure.
            # The rate is THREADED IN from the caller's already-loaded
            # `config.risk.margin_interest_rate_pct`, never re-read here:
            # loading `AppConfig` inside a prompt renderer validates API
            # keys and fails in every context that has none, which would
            # make the price silently vanish exactly where it is hardest
            # to notice. Absent rate -> no cost lines, never a guessed one.
            try:
                from src.margin_interest import format_borrowing_cost_lines

                _rate_pct = kwargs.get("margin_interest_rate_pct")
                _cost_lines = format_borrowing_cost_lines(
                    cash_balance,
                    _rate_pct if isinstance(_rate_pct, (int, float)) and not isinstance(_rate_pct, bool) else None,
                    headroom_usd
                    if isinstance(headroom_usd, (int, float)) and not isinstance(headroom_usd, bool)
                    else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not render the borrowing-cost lines for the PM "
                    "prompt (%s); the Margin Capacity block is being sent "
                    "WITHOUT its cost of carry.",
                    exc,
                )
                _cost_lines = []
            if _cost_lines:
                margin_section += "\n\n### What borrowing costs\n" + "\n".join(_cost_lines)

        # Recent system performance, REPORTING ONLY. The `in_drawdown`  # retired-ok
        # flag and its two thresholds used to live here and halved every new
        # BUY; that brake was removed 2026-09-20 on the owner's instruction
        # (retired item 32, docs/INCIDENT_HISTORY.md). These numbers now
        # inform the seat and gate nothing.
        recent_perf = kwargs.get("recent_performance") or {}
        if recent_perf:
            r5 = recent_perf.get("rolling_5d_pct")
            r20 = recent_perf.get("rolling_20d_pct")
            trailing = recent_perf.get("trailing_days") or 0

            # A window with too little equity history to evaluate used to
            # render as "None%", which reads to a model as a number near
            # zero. The rolling windows need 6 and 21 recorded sessions
            # respectively (`_compute_recent_performance` reads rows[5] /
            # rows[20]), and rows are only written by an evening run, so a
            # paused desk does not accrue them. Say so instead of printing
            # a null.
            def _window(value, needed: int) -> str:
                if value is not None:
                    return f"{value}%"
                return (
                    f"NOT YET MEASURABLE — needs {needed} recorded sessions, "
                    f"{trailing} on record. Do not read it as zero or as an "
                    f"all-clear."
                )

            perf_section = (
                f"## Recent System Performance\n"
                f"- Trailing 5-day return: {_window(r5, 6)}\n"
                f"- Trailing 20-day return: {_window(r20, 21)}\n"
                f"- History length: {trailing} days recorded\n"
            )
        else:
            perf_section = "## Recent System Performance\nNo history yet."

        # Yesterday's insights section
        yesterday_insights: dict | None = kwargs.get("yesterday_insights")
        if yesterday_insights and yesterday_insights.get("tomorrow_outlook"):
            actions = yesterday_insights.get("suggested_actions", "")
            if isinstance(actions, str):
                try:
                    actions = json.loads(actions)
                except (json.JSONDecodeError, TypeError):
                    pass
            actions_text = "\n".join(f"  - {a}" for a in actions) if isinstance(actions, list) else f"  - {actions}"
            key_risks = yesterday_insights.get("tomorrow_key_risks", "[]")
            if isinstance(key_risks, str):
                try:
                    key_risks = json.loads(key_risks)
                except (json.JSONDecodeError, TypeError):
                    key_risks = []
            risks_text = (
                "\n".join(f"  - {r}" for r in key_risks)
                if isinstance(key_risks, list) and key_risks
                else "  (none named)"
            )
            insights_date = yesterday_insights.get("date", "unknown")
            insights_ts = yesterday_insights.get("timestamp", "")
            freshness = f" (from {insights_date}"
            if insights_ts:
                freshness += f", written {insights_ts}"
            freshness += ")"
            bias = yesterday_insights.get("tomorrow_bias") or "neutral"
            conviction = yesterday_insights.get("tomorrow_conviction") or "medium"
            sell_grade = (yesterday_insights.get("sell_decisions_assessment") or "").strip()
            sell_line = f"- **SELL discipline grade** (previous run): {sell_grade[:400]}" if sell_grade else ""

            # Defect (d) fix: evening's structured "lesson categories" —
            # thesis_updates / selection_rules / discipline_notes — were
            # produced by the LLM every night and asked for in the evening
            # prompt, but never made it past `save_evening_snapshot` into
            # the DB, so Step 6 ("Yesterday's lessons: apply any relevant
            # learnings") had nothing to read. Wired here the same way the
            # rest of this section already is — date-labeled by `freshness`
            # above, with a labelled absence (not silence, not a fabricated
            # note) when evening didn't fill a category that day.
            def _parse_str_list(raw) -> list[str]:
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        return []
                else:
                    parsed = raw
                return [str(x) for x in parsed] if isinstance(parsed, list) else []

            def _lesson_bullets(items: list[str], empty_label: str) -> str:
                if not items:
                    return f"  ({empty_label})"
                # Defensive per-item cap — evening's prompt already asks for
                # 0-5/0-3 short items, this just bounds a runaway one.
                return "\n".join(f"  - {item[:220]}" for item in items)

            thesis_updates = _parse_str_list(yesterday_insights.get("thesis_updates_json", "[]"))
            selection_rules = _parse_str_list(yesterday_insights.get("selection_rules_json", "[]"))
            discipline_notes = _parse_str_list(yesterday_insights.get("discipline_notes_json", "[]"))
            thesis_text = _lesson_bullets(thesis_updates, "no thesis updates carried from last night")
            selection_text = _lesson_bullets(selection_rules, "no new selection rules carried from last night")
            discipline_text = _lesson_bullets(discipline_notes, "no discipline notes carried from last night")

            insights_section = f"""## Prior Evening Insights{freshness}
- **Tilt for today**: bias={bias}, conviction={conviction}
- Outlook (prose): {yesterday_insights.get("tomorrow_outlook", "N/A")}
- Key risks to watch today:
{risks_text}
- Lessons: {yesterday_insights.get("lessons", "N/A")}
- Risk Rating: {yesterday_insights.get("risk_rating", "N/A")}
- Suggested Actions:
{actions_text}
{sell_line}
- Thesis updates on held positions (apply at Step 6):
{thesis_text}
- New selection rules (apply when sizing new BUYs):
{selection_text}
- Discipline notes (apply at Step 6 holding discipline):
{discipline_text}"""
        else:
            insights_section = (
                "## Yesterday's Evening Insights\n"
                "No prior session insights available "
                "(no outlook, lessons, or thesis/selection/discipline notes from last night)."
            )

        # L3 memory layers — past environment trajectory
        weekly_narrative: str = kwargs.get("weekly_narrative") or ""
        macro_trajectory: str = kwargs.get("macro_trajectory") or ""
        active_state_changes: str = kwargs.get("active_state_changes") or ""
        # Phase-1 evening-upgrade feedback:
        # L3d — themes evening flagged as missed ≥ 2 times in last 14 days.
        # L3f — loss root-causes evening classified on wrong BUYs repeatedly.
        # Both empty strings when no recurring pattern; section shows defaults.
        recent_missed_lessons: str = kwargs.get("recent_missed_lessons") or ""
        recent_loss_pits: str = kwargs.get("recent_loss_pits") or ""

        narrative_section = (
            f"## Portfolio Narrative (last 7 trading days)\n{weekly_narrative}"
            if weekly_narrative
            else "## Portfolio Narrative\nNo prior narrative yet (fresh table)."
        )
        trajectory_section = (
            f"## Macro Regime Trajectory (last 7 days)\n{macro_trajectory}"
            if macro_trajectory
            else "## Macro Regime Trajectory\nNo prior snapshots yet."
        )
        # The `[date]` prefix on each row is not decoration: it is the
        # citation key the sub-floor catalyst gate resolves against
        # (`_apply_subfloor_catalyst_rule`). Saying so HERE, next to the rows
        # themselves, is what makes the requirement actionable — the rule
        # itself is enforced in Python after submission either way.
        active_changes_section = (
            "## Active News State Changes (HIGH conviction, last 14d)\n"
            "Cite a row by its `[date]` in a target's `catalyst` field. That is "
            "the ONLY way to claim the sub-floor R/R exception, and the row must "
            "name the symbol WITH a direction that supports the trade — "
            "`SYMBOL(bullish)` for a long, `SYMBOL(bearish)` for a short. "
            "`(neutral)` or `(unknown)` does not qualify either direction.\n"
            f"{active_state_changes}"
            if active_state_changes
            else "## Active News State Changes\n(none surfaced in the rolling 14-day "
            "window — with no rows to cite, the sub-floor R/R exception is "
            "unavailable today)"
        )
        missed_lessons_section = (
            f"## Recurring Missed Themes (last 14d — themes evening repeatedly "
            f"flagged as misses)\n{recent_missed_lessons}\n\n"
            "If a theme has appeared 2+ times here, it's a coverage or "
            "timing blind-spot, not random noise. Take a fresh look at it "
            "today before it runs further away."
            if recent_missed_lessons
            else "## Recurring Missed Themes\n(no recurring missed themes in the last 14 days)"
        )
        loss_pits_section = (
            f"## Recent Loss Pits (last 14d — repeat failure modes on losing "
            f"BUYs)\n{recent_loss_pits}\n\n"
            "If a root-cause has 2+ occurrences, it's a discipline gap, not "
            "bad luck. Lean against it today — tighten entries / respect "
            "warnings / cut concentration before you do the same thing again."
            if recent_loss_pits
            else "## Recent Loss Pits\n(no repeat failure modes in the last 14 days)"
        )

        # What you asked for and never got. Diagnostic only — nothing here
        # blocks a name; it tells you which of your asks the machinery keeps
        # refusing, and with what stored reason.
        blocked_proposals: str = kwargs.get("blocked_proposals") or ""
        blocked_section = (
            f"## Proposal Conversion (last 21d — what you asked for vs what "
            f"you got)\n{blocked_proposals}\n\n"
            "A block is cleaner evidence than a loss: it comes with its cause "
            "attached. If a name is listed here, re-proposing it unchanged "
            "will fail the same way again — either fix what the reason names "
            "(geometry, sizing, cash) or drop the name. This is information, "
            "not a prohibition: none of these symbols is barred."
            if blocked_proposals
            else "## Proposal Conversion\n(no proposals on record in the last 21 days)"
        )

        projected_portfolio: str = kwargs.get("projected_portfolio") or ""
        calibration_note: str = kwargs.get("calibration_note") or ""
        macro_tech_alignment: str = kwargs.get("macro_tech_alignment") or ""
        facts = kwargs.get("facts")  # PMFacts | None

        projected_section = (
            f"## Projected Book Preview (if you rubber-stamp TA's BUYs at 5% each)\n{projected_portfolio}"
            if projected_portfolio
            else "## Projected Book Preview\n(no projection available — empty book or no BUY candidates)"
        )
        calibration_section = (
            f"## Trade Calibration (your actual realized outcomes)\n{calibration_note}"
            if calibration_note
            else "## Trade Calibration\n(not enough closed trades yet for calibration — <3 in window)"
        )
        alignment_section = f"## Macro-Tech Alignment Advisory\n{macro_tech_alignment}" if macro_tech_alignment else ""
        # Phase 4 #4: structured facts block — numbers, not prose. PM should
        # prefer these over the derived narrative sections below for quantitative
        # questions (win rate, sector weight, age distribution).
        facts_section = (
            f"## Quantitative Facts (read these first for numbers)\n{facts.render()}" if facts is not None else ""
        )

        reserve_line = (
            f"\n  (a further ${reserve_balance:,.2f} is parked in the "
            f"cash-equivalent sweep vehicle; the desk does NOT sell it to "
            f"fund a BUY, and it is NOT part of the Cash Balance above — "
            f"do not size against it)"
            if reserve_balance > 0
            else ""
        )
        # 2026-09-17 fix: this used to hardcode "no margin" regardless of
        # `allow_margin`. When margin is enabled, cash is still raw cash —
        # real, and can go negative — but it is NOT the spending limit, so
        # the label must not claim the account has none. See the Margin
        # Capacity / Margin Policy section below for what may still be spent.
        cash_status = (
            "deployable this session, no margin"
            if not allow_margin
            else "raw cash — see Margin Capacity below for what may still be spent"
        )
        # Accounting re-ask (board item 133, 2026-09-18). Non-empty ONLY on
        # the one bookkeeping re-ask `DecisionStage` may make in a session,
        # and it names the candidates this seat dropped without saying why.
        # Empty string on every first call, so the prompt this seat normally
        # sees is byte-for-byte unchanged. It asks for the missing
        # `rejections` entries and nothing else — see
        # `src/pm_accounting.REASK_DIRECTIVE` for why it must not re-open
        # the decision.
        accounting_challenge: str = kwargs.get("accounting_challenge") or ""
        accounting_section = f"### ⚠️ {accounting_challenge}\n\n" if accounting_challenge else ""
        return f"""{accounting_section}## Account Status
- Total Value: ${total_value:,.2f}
- Cash Balance: ${cash_balance:,.2f} ({cash_status}){reserve_line}
- Invested: ${invested:,.2f} ({invested_pct:.1f}% of equity — capital at work, unsigned and un-leveraged; a short counts its notional, not a credit)
- Net direction: {net_exposure_pct:+.1f}% of equity (leverage-aware and signed; negative = net short). This is NOT the number macro's target is set against — `Invested` is.

## Current Positions (with entry context + signal trajectory)
{positions_text}

{margin_section}

{facts_section}

{projected_section}

{perf_section}

{calibration_section}

{alignment_section}



{narrative_section}

{trajectory_section}

{active_changes_section}

{missed_lessons_section}

{loss_pits_section}

{blocked_section}

{insights_section}

{macro_section}

{news_section}

{earnings_section}

{smart_money_section}

{eligibility_section}

## Technical Analysis Reports
{analyses_text}

{ranking_section}

{rotation_section}

## Canonical Current Evidence Registry (authoritative for provenance)
{evidence_registry_text}

For every target, cite only source/stance pairs present for that exact symbol
in this registry and copy the stance string exactly. Omit unavailable sources.
Memory and narrative sections are context, never current specialist coverage.

## Independent Source Agreement (deterministic entry backstop — Step 5)
{agreement_text}
Each row shows how many seats point FOR and AGAINST each direction, and the
entry backstop score the constructor computes from this registry (not from
what you write in provenance). It is not a vote count and not a size dial:
weighing the views is your job as head trader. A source whose stance is
marked stale is in neither count: an old filing is still worth reading, but
it has not confirmed anything about today, and it has not contradicted
anything either.

The backstop score is the chart for, plus name-level seats (news, earnings,
smart money) for, minus the chart against. Macro is not counted: a
`broadcast` stance is the market-wide outlook, the backdrop that tilts size
and direction, not a read on this name, so it never counts FOR a trade and is
not subtracted. An opposing name-level seat is not subtracted either: it is
an objection you answer in writing in `objection_answers` (overrule or
accept).
**A backstop score of zero or below produces NO ORDER AT ALL** — not a small
position, no position. Anything already held is left alone; refusing to open
is not a decision to sell.

**A backstop score of +1 or more imposes no size restriction of its own** (the
graduated ceiling was retired 2026-09-14: it scaled size by the square root of
the seat count, which is the statistics of INDEPENDENT estimates, and these
seats read overlapping evidence). Size the idea on its own merits and the risk
side, within the ratified per-trade envelope. Do NOT shrink an idea because it
has fewer agreeing seats. If you believe a dissenting seat is wrong, answer it
in writing in `objection_answers`; the constructor computes this score from
the registry and cannot read your argument.

Based on all the above (memory of past decisions + environment trajectory + today's signals), what trades should we execute? Respond as JSON."""

    @staticmethod
    def _semantic_failure(result, status: str, error: object):
        result.semantic_status = status
        # Board item 188 (recording only): the gate's own word, so the
        # agent_logs row says WHICH way the answer was unusable rather than
        # only that the seat produced no decision.
        result.gate_reason = status
        result.semantic_error = str(error)
        return None, result

    def decide(
        self,
        analyses: list[TechAnalysisResult],
        positions: list[Position],
        macro_analysis: dict | None = None,
        cash_balance: float = 0,
        reserve_balance: float = 0.0,
        total_value: float = 0,
        news_intel: NewsIntelligenceReport | None = None,
        earnings_analyses: list[dict] | None = None,
        smart_money_findings: list[SmartMoneyFinding] | None = None,
        yesterday_insights: dict | None = None,
        recent_performance: dict | None = None,
        position_history: dict | None = None,
        weekly_narrative: str = "",
        macro_trajectory: str = "",
        active_state_changes: str = "",
        projected_portfolio: str = "",
        calibration_note: str = "",
        macro_tech_alignment: str = "",
        recent_missed_lessons: str = "",
        recent_loss_pits: str = "",
        blocked_proposals: str = "",
        facts=None,
        allow_margin: bool = True,
        # §11.2 ladder headroom, threaded from the SAME computation
        # execution's submit loop uses (`_entry_deployment_budget` /
        # `_session_gross_ceiling` in `src/pipeline_stages.py`) so the
        # Margin Capacity section never derives its own number.
        # `margin_ladder_backed=False` means that computation could
        # not resolve this session — the section says so rather than
        # showing a stale or invented figure.
        margin_headroom_usd: float | None = None,
        margin_ladder_backed: bool = False,
        margin_ladder_multiple: float | None = None,
        margin_ladder_rung: str | None = None,
        # Board item 95: the annual margin-interest rate the account
        # is actually charged on its OVERNIGHT debit, threaded from
        # the caller's already-loaded `config.risk`. `None` means the
        # cost of carry is simply not stated — never guessed.
        margin_interest_rate_pct: float | None = None,
        # The owner's minimum risk per position (owner rule 2026-08-27)
        # plus each candidate's own entry and stop
        # (`src.risk.min_risk.MinRiskFloor`), so the rotation pre-check
        # judges "short of cash" at the candidate's real stop. Replaced the
        # deleted $500 `cash_sweep.min_order_usd` floor. `None` switches
        # the funding half of the rotation precondition off.
        min_risk_floor=None,
        # Owner decision 2026-10-15 (q07): the measured score-noise brake
        # for the ranked-margin swap; `None` = unmeasured, no swap.
        score_noise_brake: float | None = None,
        symbol_sectors: dict[str, str] | None = None,
        asof: date | None = None,
        session_type: str = "morning",
        allowed_buy_symbols: set[str] | None = None,
        transient_admitted_symbols: set[str] | None = None,
        # The sub-floor catalyst gate's threshold. Its default is
        # the shared constants `RiskConfig` itself defaults to, so a
        # caller that does not thread config (the model-policy
        # harness, most tests) gates on exactly the production
        # numbers rather than on a second opinion about them.
        starter_risk_pct: float = STARTER_POSITION_RISK_PCT,
        # Phase 14 (opportunity-cost rotation): the EXISTING book's
        # per-symbol risk (before anything this session proposes) and
        # the total-risk ceiling it is rationed against — the same
        # inputs `PortfolioConstructor` rations orders against
        # (`src/pipeline_stages.py::_book_risk_inputs`). `None` for
        # `existing_risk_pct` disables the rotation check for this
        # session rather than running it against a fabricated
        # "book is empty" view — see `_render_rotation_section`.
        existing_risk_pct: dict[str, float] | None = None,
        max_portfolio_risk_pct: float = 25.0,
        # Phase 14b: whether `execution.rotation_enabled` is on.
        # Wording only — tells the model the desk may itself close
        # a categorically-ineligible holding this session; the act
        # is decided in `DecisionStage`, never in this prompt.
        rotation_execute_enabled: bool = False,
        rotation_ranked_margin_enabled: bool = False,
        # 2026-09-04 fix: the SAME real derived reward:risk
        # `PortfolioConstructor.construct_orders` gates on,
        # keyed by upper-case symbol — see `candidate_eligibility`
        # and `_apply_subfloor_catalyst_rule` for why this replaces
        # `TechAnalysisResult.risk_reward` at both eligibility gates.
        # `None` (the default) falls back to that field, for the rare
        # caller with no `PortfolioConstructor` to preview from.
        real_reward_risk_by_symbol: dict[str, float | None] | None = None,
        # Item 54 (2026-09-12): the constructor's structured refusals
        # from the same preview pass, so eligibility rule R6 can name
        # a candidate the one shared funnel has already refused.
        constructor_refusals_by_symbol: dict[str, dict[str, str]] | None = None,
        # Board item 133 (2026-09-18): the ONE bookkeeping re-ask
        # `DecisionStage` may make when this seat dropped a candidate
        # without naming a ground. Empty on every ordinary call.
        accounting_challenge: str = "",
    ) -> tuple[PortfolioDecision | None, "AgentResult"]:
        # One fill retry per decide() call — the agent is long-lived across
        # morning/midday/close. A morning miss must not spend the close's
        # shot, and a spent flag must not skip a later session.
        self._soft_exit_retry_used = False
        # Board item 164 (2026-09-19): every target this call removes after
        # the model answered — malformed, or carrying an unadjudicated seat
        # conflict — with the gate and the reason, for `DecisionStage` to
        # persist per symbol. Reset per call; recording only.
        self.last_dropped_targets = []
        # Board item 78 (2026-09-26): what the soft-exit heal ACTUALLY did
        # to each open/increase name that arrived without a falsifier —
        # filled, blocked by the spend cap, never attempted for want of a
        # replayable message, errored, or answered without one. Reset per
        # call; recording only. `DecisionStage` drains it and files one
        # durable row per symbol, and the blank-falsifier refusal quotes it
        # instead of asserting a retry that may never have run.
        self.last_soft_exit_heals: dict[str, dict[str, str]] = {}
        result = self.run(
            analyses=analyses,
            positions=positions,
            macro_analysis=macro_analysis,
            cash_balance=cash_balance,
            reserve_balance=reserve_balance,
            total_value=total_value,
            news_intel=news_intel,
            earnings_analyses=earnings_analyses or [],
            smart_money_findings=smart_money_findings or [],
            yesterday_insights=yesterday_insights,
            recent_performance=recent_performance or {},
            position_history=position_history or {},
            weekly_narrative=weekly_narrative,
            macro_trajectory=macro_trajectory,
            active_state_changes=active_state_changes,
            projected_portfolio=projected_portfolio,
            calibration_note=calibration_note,
            macro_tech_alignment=macro_tech_alignment,
            recent_missed_lessons=recent_missed_lessons,
            recent_loss_pits=recent_loss_pits,
            blocked_proposals=blocked_proposals,
            facts=facts,
            allow_margin=allow_margin,
            margin_headroom_usd=margin_headroom_usd,
            margin_ladder_backed=margin_ladder_backed,
            margin_ladder_multiple=margin_ladder_multiple,
            margin_ladder_rung=margin_ladder_rung,
            margin_interest_rate_pct=margin_interest_rate_pct,
            min_risk_floor=min_risk_floor,
            score_noise_brake=score_noise_brake,
            symbol_sectors=symbol_sectors or {},
            asof=asof,
            session_type=session_type,
            allowed_buy_symbols=allowed_buy_symbols or set(),
            transient_admitted_symbols=transient_admitted_symbols or set(),
            # Phase 13: the candidate ranking shown in the prompt gates on
            # the same floor `_apply_subfloor_catalyst_rule` enforces after
            # submission, so the PM is ranked on the rule it is held to.
            # Phase 14: opportunity-cost rotation pre-check inputs.
            existing_risk_pct=existing_risk_pct,
            max_portfolio_risk_pct=max_portfolio_risk_pct,
            rotation_execute_enabled=rotation_execute_enabled,
            rotation_ranked_margin_enabled=rotation_ranked_margin_enabled,
            real_reward_risk_by_symbol=real_reward_risk_by_symbol,
            constructor_refusals_by_symbol=constructor_refusals_by_symbol,
            accounting_challenge=accounting_challenge,
        )
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Portfolio manager returned non-JSON response")
            return self._semantic_failure(
                result,
                "pm_parse_error",
                "response did not contain a valid decision JSON object",
            )
        if not isinstance(parsed, dict):
            # A PortfolioDecision is an OBJECT. A bare list here means the
            # candidate scan surfaced a fragment (historically: the plan's own
            # `targets` array) instead of the decision — treat as a parse
            # failure so the session retries, never as a deliberate hold.
            # `PortfolioDecision(**list)` below would raise anyway; this makes
            # the failure mode explicit and greppable.
            logger.error(
                "Portfolio manager parse produced %s, not a decision object — "
                "treating as parse failure (fragment selected over full plan?)",
                type(parsed).__name__,
            )
            return self._semantic_failure(
                result,
                "pm_parse_error",
                f"parsed {type(parsed).__name__}, expected object",
            )
        # Per-entry isolation for targets: a single malformed TargetPosition
        # (e.g. target_weight_pct=30 violating the 0-25 range, or empty
        # thesis on a Field with no min_length but PortfolioConstructor's
        # contract assumes non-empty) must not drop the WHOLE PortfolioDecision.
        # Highest blast radius of any per-entry isolation gap: losing the
        # decision means losing reasoning_chain + portfolio_view + every
        # OTHER target → entire morning session is silenced. The
        # PortfolioConstructor downstream still has remaining valid targets
        # to translate into orders; better to fire 4 of 5 trades than 0 of 5.
        # Mirrors PR #73/#74 pattern.
        parsed_target_count = (
            len(parsed.get("targets", []))
            if isinstance(parsed, dict) and isinstance(parsed.get("targets", []), list)
            else 0
        )
        if isinstance(parsed, dict):
            parsed = self._drop_invalid_targets(
                parsed,
                dropped=self.last_dropped_targets,
            )
            parsed = self._drop_invalid_rejections(parsed)
        try:
            decision = PortfolioDecision(**parsed)
            if parsed_target_count > 0 and not decision.targets:
                logger.error(
                    "Portfolio manager emitted %d target(s), but all were invalid; "
                    "treating as agent failure, not a no-action decision",
                    parsed_target_count,
                )
                return self._semantic_failure(
                    result,
                    "pm_schema_error",
                    f"all {parsed_target_count} emitted targets were invalid",
                )
            decision, result = self._fill_missing_open_falsifiers(
                decision,
                result,
                positions=positions,
                total_value=total_value,
                existing_risk_pct=existing_risk_pct,
            )
            # §9.3 — drop any target that OPENS/INCREASES exposure while
            # carrying an unadjudicated seat conflict, before grounding is
            # even checked. This is a per-target prune, not an error: it
            # must never join `validate_grounding`'s list (see that
            # method's non-empty-error contract — it fails the ENTIRE
            # session, not one target).
            decision = self._drop_unadjudicated_conflicts(
                decision,
                positions=positions,
                total_value=total_value,
                existing_risk_pct=existing_risk_pct,
                dropped=self.last_dropped_targets,
            )
            # Desk archetype (19 Oct 2026): a target opening or adding over
            # an objection the AI did not overrule in writing is dropped.
            decision = self._drop_unanswered_objections(
                decision,
                positions=positions,
                total_value=total_value,
                existing_risk_pct=existing_risk_pct,
                dropped=self.last_dropped_targets,
                seat_verdicts=self.last_seat_verdicts,
            )
            # The sub-floor catalyst gate. Same per-target-prune contract as
            # the conflict drop above and applied in the same place, before
            # grounding: a target this rule removes must not be able to fail
            # the whole session on its way out.
            decision = self._apply_subfloor_catalyst_rule(
                decision,
                analyses=analyses,
                positions=positions,
                total_value=total_value,
                active_state_changes=active_state_changes,
                starter_risk_pct=starter_risk_pct,
                real_reward_risk_by_symbol=real_reward_risk_by_symbol,
                existing_risk_pct=existing_risk_pct,
            )
            errors = self.validate_grounding(
                decision,
                analyses=analyses,
                positions=positions,
                news_intel=news_intel,
                earnings_analyses=earnings_analyses or [],
                macro_analysis=macro_analysis,
                total_value=total_value,
                smart_money_findings=smart_money_findings or [],
                symbol_sectors=symbol_sectors or {},
                allowed_buy_symbols=allowed_buy_symbols,
                existing_risk_pct=existing_risk_pct,
            )
            if errors:
                logger.error(
                    "Portfolio decision failed deterministic grounding: %s",
                    "; ".join(errors),
                )
                return self._semantic_failure(
                    result,
                    "pm_grounding_error",
                    "; ".join(errors),
                )
            return decision, result
        except ValidationError as e:
            # Mirror of the RiskManager repair path (2026-08-18 incident
            # class): a decision that parsed as JSON but failed schema
            # validation (typically an omitted mandatory reasoning_chain
            # field) costs a FULL research re-run 30 minutes later via
            # analysis_error. One immediate ~$0.006 repair call naming the
            # validation errors is strictly cheaper; a second failure keeps
            # today's fail-closed None → analysis_error path.
            #
            # External review (post-implementation): a schema repair must
            # never become a re-decision. `targets` is the decision — if
            # the validation failure is rooted there, repair can't fix it
            # without the model re-deciding, so skip repair and fail
            # closed. Otherwise, after repair, the target set (symbol +
            # weight) must be byte-identical to the pre-repair parse; any
            # drift fails closed too.
            if self.validation_error_touches(e, self._DECISION_FIELDS):
                logger.error(
                    "Portfolio decision validation failure is rooted in a "
                    "decision-bearing field (%s) — not schema-repairable; "
                    "failing closed: %s",
                    ", ".join(self._DECISION_FIELDS),
                    e,
                )
                return self._semantic_failure(result, "pm_schema_error", e)
            repaired = self.repair_reprompt(result, e, "PortfolioDecision")
            reparsed = repaired.parse_json()
            if isinstance(reparsed, dict):
                repaired_target_count = (
                    len(reparsed.get("targets", [])) if isinstance(reparsed.get("targets", []), list) else 0
                )
                # The repaired answer replaces the first one, so its drops
                # replace the first attempt's in the record too.
                self.last_dropped_targets = []
                reparsed = self._drop_invalid_targets(
                    reparsed,
                    dropped=self.last_dropped_targets,
                )
                reparsed = self._drop_invalid_rejections(reparsed)
                if not self._decision_fields_unchanged(parsed, reparsed):
                    logger.error(
                        "Portfolio decision repair changed target symbols/"
                        "weights instead of only completing the schema — "
                        "treating as an unauthorized re-decision and "
                        "failing closed.",
                    )
                    return self._semantic_failure(
                        repaired,
                        "pm_repair_changed_decision",
                        "schema repair changed target symbols or weights",
                    )
                try:
                    decision = PortfolioDecision(**reparsed)
                    if repaired_target_count > 0 and not decision.targets:
                        logger.error(
                            "Portfolio repair emitted %d target(s), but all were invalid; failing closed",
                            repaired_target_count,
                        )
                        return self._semantic_failure(
                            repaired,
                            "pm_schema_error",
                            f"all {repaired_target_count} repaired targets were invalid",
                        )
                    decision, repaired = self._fill_missing_open_falsifiers(
                        decision,
                        repaired,
                        positions=positions,
                        total_value=total_value,
                        existing_risk_pct=existing_risk_pct,
                    )
                    # §9.3 — same per-target conflict prune as the
                    # first-attempt path, applied before grounding here too.
                    decision = self._drop_unadjudicated_conflicts(
                        decision,
                        positions=positions,
                        total_value=total_value,
                        existing_risk_pct=existing_risk_pct,
                        dropped=self.last_dropped_targets,
                    )
                    # Desk archetype (19 Oct 2026): a target opening or adding over
                    # an objection the AI did not overrule in writing is dropped.
                    decision = self._drop_unanswered_objections(
                        decision,
                        positions=positions,
                        total_value=total_value,
                        existing_risk_pct=existing_risk_pct,
                        dropped=self.last_dropped_targets,
                        seat_verdicts=self.last_seat_verdicts,
                    )
                    # Same sub-floor catalyst gate as the first-attempt path.
                    # A schema repair must not be a way around it.
                    decision = self._apply_subfloor_catalyst_rule(
                        decision,
                        analyses=analyses,
                        positions=positions,
                        total_value=total_value,
                        active_state_changes=active_state_changes,
                        starter_risk_pct=starter_risk_pct,
                        real_reward_risk_by_symbol=real_reward_risk_by_symbol,
                        existing_risk_pct=existing_risk_pct,
                    )
                    errors = self.validate_grounding(
                        decision,
                        analyses=analyses,
                        positions=positions,
                        news_intel=news_intel,
                        earnings_analyses=earnings_analyses or [],
                        macro_analysis=macro_analysis,
                        total_value=total_value,
                        smart_money_findings=smart_money_findings or [],
                        symbol_sectors=symbol_sectors or {},
                        allowed_buy_symbols=allowed_buy_symbols,
                        existing_risk_pct=existing_risk_pct,
                    )
                    if errors:
                        logger.error(
                            "Repaired portfolio decision failed deterministic grounding: %s",
                            "; ".join(errors),
                        )
                        return self._semantic_failure(
                            repaired,
                            "pm_grounding_error",
                            "; ".join(errors),
                        )
                    logger.info(
                        "Portfolio decision repair succeeded (%d targets)",
                        len(decision.targets),
                    )
                    return decision, repaired
                except Exception as e2:  # noqa: BLE001
                    logger.error(
                        "Failed to parse portfolio decision after repair: %s",
                        e2,
                    )
                    return self._semantic_failure(repaired, "pm_schema_error", e2)
            logger.error(
                "Portfolio decision repair returned %s, not an object",
                type(reparsed).__name__,
            )
            return self._semantic_failure(
                repaired,
                "pm_parse_error",
                f"repair parsed {type(reparsed).__name__}, expected object",
            )
        except Exception as e:
            logger.error("Failed to parse portfolio decision: %s", e)
            return self._semantic_failure(result, "pm_schema_error", e)

    def drain_soft_exit_heals(self) -> dict[str, dict[str, str]]:
        """Per-symbol soft-exit heal outcomes from the last `decide()`, cleared.

        Same hand-over contract as `PortfolioConstructor.drain_refusals`: a
        fresh dict the caller can hold, and the agent forgets it so a later
        session cannot re-file a stale outcome. Board item 78.
        """
        heals = dict(getattr(self, "last_soft_exit_heals", None) or {})
        self.last_soft_exit_heals = {}
        return heals

    def _record_soft_exit_heal(self, symbols, outcome: str, detail: str) -> None:
        """File one heal outcome per named symbol. Recording only; never raises."""
        try:
            store = getattr(self, "last_soft_exit_heals", None)
            if store is None:
                store = {}
                self.last_soft_exit_heals = store
            for symbol in symbols or []:
                key = str(symbol).strip().upper()
                if key:
                    store[key] = {"outcome": outcome, "detail": detail}
        except Exception:  # noqa: BLE001 - bookkeeping must not break a decision
            pass

    def _fill_missing_open_falsifiers(
        self,
        decision,
        result,
        *,
        positions=None,
        total_value: float = 0.0,
        existing_risk_pct=None,
    ):
        """One paid retry to fill a missing thesis_invalid_if. Never invents.

        Mechanical heal already restored a stated string the null-wipe
        dropped. This asks the seat to actually write the falsifier on
        open/increase names that still have empty/`unknown`. Reductions
        and closes are not asked — a blank field must not spend a retry
        on a size drop. Catalyst is not filled here. If the retry still
        leaves an open name blank, the book-entry refuse records
        `soft-exit missing after retry`.
        """
        from src.cost_circuit import PaidAnalysisSuspended
        from src.seat_heal import (
            HEAL_CAP_BLOCKED,
            HEAL_FAILED,
            HEAL_NOT_ATTEMPTED,
            HEAL_PAID_RETRY,
            merge_retry_falsifiers,
        )
        from src.soft_exit_never_blank import (
            apply_mechanical_heal,
            soft_exit_fill_coda,
            soft_exit_retry_targets,
        )

        if decision is None:
            return decision, result
        retry_already_used = bool(getattr(self, "_soft_exit_retry_used", False))
        held = {str(getattr(p, "symbol", "")).upper(): p for p in list(positions or []) if getattr(p, "symbol", None)}
        missing = [
            t.symbol
            for t in list(getattr(decision, "targets", None) or [])
            if open_target_missing_falsifier(
                t,
                intent=self._target_intent(
                    t,
                    held,
                    total_value,
                    existing_risk_pct=existing_risk_pct,
                ),
            )
        ]
        if not missing:
            return decision, result

        # Mechanical heal of last resort, before any spend (board item 78).
        missing = apply_mechanical_heal(
            decision,
            result,
            missing,
            self._record_soft_exit_heal,
            logger,
        )
        if not missing:
            return decision, result
        if retry_already_used:
            self._record_soft_exit_heal(
                missing,
                HEAL_NOT_ATTEMPTED,
                "the seat's one soft-exit fill retry was already spent on an "
                "earlier attempt in this decide() call; no second retry was "
                "bought and no falsifier was invented",
            )
            return decision, result
        user_message = getattr(result, "user_message", None) or ""
        if not str(user_message).strip():
            logger.warning(
                "Open target(s) missing thesis_invalid_if (%s) — no user message to replay for a fill retry",
                missing,
            )
            self._record_soft_exit_heal(
                missing,
                HEAL_NOT_ATTEMPTED,
                "no replayable user message survived, so the paid soft-exit "
                "fill retry was NEVER ATTEMPTED for this name",
            )
            return decision, result
        self._soft_exit_retry_used = True
        coda = soft_exit_fill_coda(missing)
        try:
            retried = self._execute(
                str(user_message) + coda,
                retry_kind="soft_exit_fill",
                optional_retry=True,
            )
        except PaidAnalysisSuspended as exc:
            logger.warning(
                "Soft-exit fill retry blocked by spend cap for %s: %s",
                missing,
                exc,
            )
            self._record_soft_exit_heal(
                missing,
                HEAL_CAP_BLOCKED,
                f"the paid soft-exit fill retry was blocked by the spend cap "
                f"({exc}); the seat was never re-asked for this name",
            )
            return decision, result
        except Exception as exc:
            logger.warning(
                "Soft-exit fill retry failed for %s: %s",
                missing,
                exc,
            )
            self._record_soft_exit_heal(
                missing,
                HEAL_FAILED,
                f"the paid soft-exit fill retry was attempted and errored ({type(exc).__name__}: {exc})",
            )
            return decision, result
        retry_targets = soft_exit_retry_targets(retried)
        merged, filled = merge_retry_falsifiers(
            list(decision.targets),
            retry_targets,
        )
        if filled:
            decision.targets = merged
            logger.info(
                "Soft-exit fill retry stated thesis_invalid_if for %s — not invented",
                filled,
            )
        else:
            logger.warning(
                "Soft-exit fill retry did not produce a stated falsifier for %s",
                missing,
            )
        filled_keys = {str(s).strip().upper() for s in (filled or [])}
        self._record_soft_exit_heal(
            filled,
            HEAL_PAID_RETRY,
            "the seat stated a real thesis_invalid_if on the one paid "
            "soft-exit fill retry; the string is the seat's, not invented",
        )
        self._record_soft_exit_heal(
            [s for s in missing if str(s).strip().upper() not in filled_keys],
            HEAL_FAILED,
            "the one paid soft-exit fill retry WAS attempted and the seat "
            "still did not state a falsifier for this name",
        )
        return decision, retried


# --- Patch mirroring. Tests patch names on `src.agents.portfolio_manager`
# (e.g. `et_today`) that the moved code now reads from its own submodule.
# Same design as src/trader_feed/__init__.py: a write to this package's
# namespace is mirrored into every submodule that already binds that name,
# and the delete that `patch` performs on exit restores the pristine value.
# This is the ONE mirror block for this package — a second one would cancel it.
_SUBMODULES = (
    "src.agents.portfolio_manager.prompt_evidence",
    "src.agents.portfolio_manager.evidence_prompting",
    "src.agents.portfolio_manager.ranking",
    "src.agents.portfolio_manager.candidate_ranking",
    "src.agents.portfolio_manager.rotation_section",
    "src.agents.portfolio_manager.rotation_rendering",
    "src.agents.portfolio_manager.grounding",
    "src.agents.portfolio_manager.decision_grounding",
)
_PRISTINE: dict[str, object] = {}


class _PortfolioManagerMirroringModule(_types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        if name in vars(self):
            _PRISTINE.setdefault(name, vars(self)[name])
        super().__setattr__(name, value)
        for module_path in _SUBMODULES:
            sub = _sys.modules.get(module_path)
            if sub is not None and name in vars(sub):
                setattr(sub, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        pristine = _PRISTINE.pop(name, None)
        for module_path in _SUBMODULES:
            sub = _sys.modules.get(module_path)
            if sub is not None and name in vars(sub):
                if pristine is None:
                    delattr(sub, name)
                else:
                    setattr(sub, name, pristine)


# The seat HOLDS its four parts (one instance each, collaborators handed in live)
# and delegates the old names to them; it inherits none of them.
hold_prompt_evidence(PortfolioManagerAgent)
hold_candidate_ranking(PortfolioManagerAgent)
hold_rotation_section(PortfolioManagerAgent)
hold_decision_grounding(PortfolioManagerAgent)
_sys.modules[__name__].__class__ = _PortfolioManagerMirroringModule
