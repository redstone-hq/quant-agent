import logging
from pathlib import Path

from pydantic import ValidationError

from src.agents import risk_review_mode
from src.agents.base import BaseAgent
from src.agents.prompt_limits import LiveLimitPrompt
from src.agents.risk_verdict_canon import (
    canonical_modifications,
    canonical_rejections,
    drop_invalid_modifications,
)
from src.models import (
    ExitRiskVerdict,
    NewsIntelligenceReport,
    PortfolioDecision,
    Position,
    RiskVerdict,
    TechAnalysisResult,
)
from src.risk.constants import reward_risk_floor_applies
from src.risk.rules import HARD_BLOCK_RULES, RiskViolation
from src.sentinel.guarded import record_guarded_pass

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent


#: The empty case. It used to read "No hard rule violations detected." — a
#: FALSE ALL-CLEAR, and the reason board item 162 exists. `TradingPipeline.
#: _filter_hard_risk_decisions` drops an order whose hard rule fired and
#: `continue`s past the list this block renders, forwarding only the
#: `sector_unresolved_*` advisory. So a hard-limit entry cannot reach here,
#: and an empty block says nothing whatsoever about hard limits. The seat's
#: own prompt used to ask it to "confirm the Hard Risk Rule Check block shows
#: no position-cap or exposure violation slipped through", which was a check
#: against evidence filtered out upstream.
_NO_FINDINGS = (
    "Nothing raised. Read this as: the engine raised no ADVISORY this run. "
    "It is NOT an all-clear on the hard limits — an order that breaches a "
    "hard limit is dropped by the engine BEFORE this block is built, so a "
    "hard breach is invisible here by construction. Orders refused that way "
    "are simply absent from the proposed trades above."
)


def _format_engine_findings(rule_violations: list[RiskViolation]) -> str:
    """Render the engine's findings so a HARD LIMIT and an ADVISORY cannot
    be mistaken for one another.

    Board item 162, and the owner ruling of 2026-09-19 behind it: hard limits
    are enforced by CODE, and above a mere guideline the risk seat may RESIZE
    but never VETO. Every entry used to render with one prefix, `VIOLATION`,
    which told the seat it was looking at a breach of an enforced limit when
    it was looking at a guideline — and 3 of 17 stored verdicts then vetoed a
    whole plan citing an advisory as "the hard risk rule".

    The class is read from `HARD_BLOCK_RULES` membership, NEVER from the rule
    name.

    Hard entries are listed FIRST and unconditionally, so the more serious
    class can never rank below the less serious one however many advisories
    a run raises.
    """
    hard = [v for v in rule_violations if v.rule in HARD_BLOCK_RULES]
    advisory = [v for v in rule_violations if v.rule not in HARD_BLOCK_RULES]
    if not hard and not advisory:
        return _NO_FINDINGS
    lines: list[str] = []
    if hard:
        lines.append(
            "HARD LIMITS — enforced by code. The engine has ALREADY REFUSED "
            "the order; nothing here is yours to approve or wave through."
        )
        lines.extend(
            f"- HARD LIMIT BREACHED [{v.rule}]: {v.message} (value: {v.value}, limit: {v.limit})" for v in hard
        )
    if advisory:
        if hard:
            lines.append("")
        lines.append(
            "ADVISORIES — guidelines, not limits. NO order was blocked by "
            "any line below. You may resize (`modifications` / "
            "`scale_all_buys`) or refuse a named trade (`rejected_symbols`); "
            "you may NOT veto the plan (`approved: false`) on one of these "
            "alone. Each must still be answered in the matching "
            "`reasoning_chain` field."
        )
        lines.extend(
            f"- ADVISORY (nothing blocked) [{v.rule}]: {v.message} (value: {v.value}, limit: {v.limit})"
            for v in advisory
        )
    return "\n".join(lines)


PROMPT_PATH = PROJECT_ROOT / "config" / "prompts" / "risk_manager.md"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def _fmt_or_na(value, suffix: str = "") -> str:
    """Render a macro metric, falling back to 'N/A' when the provider
    returned None (FRED outage). The macro provider always ships every
    key with None values on failure, so `.get(key, 'N/A')` defaults never
    fire — the prompt was literally rendering 'VIX: None' / 'inverted:
    None' on outage days (audit round 2 #34)."""
    return "N/A" if value is None else f"{value}{suffix}"


class RiskManagerAgent(LiveLimitPrompt, BaseAgent):
    # The reviewer is SHOWN the desk's limits, not TOLD them. Every numeric
    # ceiling in `config/prompts/risk_manager.md` is a `{{risk.*}}`
    # placeholder rendered from the live config at construction time — see
    # `src/agents/prompt_limits.py` for the mechanism, and for the
    # 2026-09-11 defect (a limit that was wrong at birth, replacing a
    # relational phrasing that could not go wrong at all) that motivated it.
    # `result_model` is set on the INSTANCE in review(), not as a class
    # attribute: this seat's verdict schema is RiskVerdict or ExitRiskVerdict
    # depending on review_mode, decided per-call.
    result_model = None
    _prompt_path = PROMPT_PATH
    _settings_path = SETTINGS_PATH
    _fallback_prompt = "You are a risk manager. Respond with JSON."

    @property
    def name(self) -> str:
        return "risk_manager"

    def build_user_message(self, **kwargs) -> str:
        portfolio_decision: PortfolioDecision = kwargs["portfolio_decision"]
        positions: list[Position] = kwargs["positions"]
        macro_summary: dict = kwargs["macro_summary"]
        rule_violations: list[RiskViolation] = kwargs["rule_violations"]
        tech_analyses: list[TechAnalysisResult] = kwargs.get("tech_analyses", []) or []
        news_intel: NewsIntelligenceReport | None = kwargs.get("news_intel")
        earnings_analyses: list[dict] = kwargs.get("earnings_analyses", []) or []
        total_value: float | None = kwargs.get("total_value")
        cash: float | None = kwargs.get("cash")
        # Cash-equivalent sweep reserve (SGOV), reported separately from
        # `cash` — 2026-08-19 SGOV/deployable-liquidity forensic. `cash` is
        # already the truthful immediately-deployable figure; never re-add
        # this to it (that is the exact bug that let the hard gate approve
        # BUYs execution couldn't fund).
        reserve_balance: float = kwargs.get("reserve_balance", 0.0) or 0.0
        # 2026-08-13 agent audit — "risk evidence completeness". RM's prompt
        # claims it enforces PM's holding-discipline rules, but the input did
        # not reach it: Position carries no entry date. Both are optional so
        # every existing call site keeps working; absent, the sections render
        # as "not provided" rather than silently reading as "no position is
        # young".
        position_history: dict = kwargs.get("position_history") or {}
        recent_performance: dict = kwargs.get("recent_performance") or {}
        # Audit §1.3 — total capital at risk if every open stop were hit. RM's
        # `sizing_sanity` step has always been asked whether any bet is
        # outsized while being shown only notional weights, which answer a
        # different question: a 15% position stopped 3% away risks less than a
        # 5% position stopped 20% away. None when the heat build failed.
        heat = kwargs.get("heat")
        # The total at-risk ceiling shown in the Portfolio Risk block. The
        # fallback used to be a hand-typed 25.0 — a second copy of
        # `risk.max_portfolio_risk_pct` that nothing kept in step with the
        # setting. It now reads the live value, so a caller that passes
        # nothing shows the reviewer the ceiling the engine enforces.
        risk_ceiling_pct: float = float(kwargs.get("risk_ceiling_pct") or self.risk_config.max_portfolio_risk_pct)
        # `reasoning_chain.event_risk` is a REQUIRED field. Its fetched input
        # is earnings proximity (information only); scheduled economic
        # releases are not shown (owner ruling 2026-10-18). When the caller
        # passes nothing, the block still renders and says NOT FETCHED.
        # WHICH review this is. One seat, two callers: the morning plan
        # (`pipeline_stages.RiskStage`) and the exit review
        # (`pipeline._risk_review_exits`). The exit path reused this renderer
        # unchanged and was therefore told, on every run, that two mandatory
        # audit steps had been skipped — steps that do not exist on that path
        # at all. See `src/agents/risk_review_mode.py` for the full account;
        # the rule is that an absence which is nobody's fault must never be
        # rendered as an analyst's omission. Defaults to the morning path, so
        # every existing call site is byte-identical.
        review_mode: str = risk_review_mode.normalize(kwargs.get("review_mode"))
        event_risk_block: str = str(kwargs.get("event_risk_block") or "").strip()
        if not event_risk_block:
            from src.data.event_calendar import format_event_risk_block

            event_risk_block = format_event_risk_block(earnings=None).strip()

        # audit round 2 #5: RM's rr_audit / sizing_sanity / concentration
        # checks were running blind — no equity, no cash, no per-position
        # weights. When the caller doesn't pass total_value, approximate the
        # denominator with the sum of listed position values (understates
        # true equity by the cash balance — flagged in the header).
        #
        # Computed HERE, above `_fmt_decision`, rather than at its original
        # site below the decisions block: the BUY-that-adds line needs the
        # same denominator to state a resulting weight, and a second copy of
        # this fallback is exactly the kind of drift that produced the defect
        # that line exists to fix.
        approx_book = sum(p.market_value for p in positions) if positions else 0.0
        denom = total_value if (total_value or 0) > 0 else approx_book

        # Raw (unlevered) dollars already held per symbol, for the
        # entry-that-adds line below. Raw because `weight_pct_of` applies the
        # gross multiplier itself — pre-multiplying here would square it.
        # SIGNED: a short position carries a negative `market_value`, and the
        # SHORT branch below needs that sign to tell "adding to a short I
        # already hold" from "shorting a name I am long" (board item 135).
        held_raw_by_symbol: dict[str, float] = {}
        for _p in positions or []:
            _sym = str(getattr(_p, "symbol", "") or "").strip().upper()
            if _sym:
                held_raw_by_symbol[_sym] = held_raw_by_symbol.get(_sym, 0.0) + (_p.market_value or 0.0)

        # audit round 2 #6: allocation_pct has TWO meanings — %-of-portfolio
        # for BUY vs %-of-current-position for SELL (100 = full close,
        # 0 = skip). Rendering both with the same "% allocation" template
        # made the RM misread SELL fractions as portfolio weights and emit
        # allocation_pct mods that silently downgraded PM-sized exits.
        def _fmt_decision(d) -> str:  # d: TradeDecision
            # Stage 3: COVER carries the same %-OF-CURRENT-POSITION semantics
            # as SELL (100 = full cover, 0 = skip) — it is not a portfolio
            # weight either. Rendering it with the BUY/SHORT template would
            # make the RM misread a cover fraction as a portfolio-sized bet.
            if d.action in ("SELL", "COVER"):
                verb = "sell" if d.action == "SELL" else "cover"
                alloc = (
                    f"{verb} {d.allocation_pct}% OF CURRENT POSITION "
                    f"(100 = full close; NOT a portfolio weight — never set to 0, 0 = skip)"
                )
            else:
                alloc = f"{d.allocation_pct}% of portfolio"
                # 2026-09-18: a THIRD meaning of this field, never noticed by
                # the audit that wrote the comment above. For a BUY that OPENS
                # a position "% of portfolio" is true. For a BUY that ADDS to
                # one already held, the constructor sizes an INCREMENT —
                # `name_headroom_pct = (max_position_pct - current_pct) /
                # gross_mul` (`portfolio_constructor.py`) — so the same label
                # understates the resulting position by the whole existing
                # holding. Live consequence: this seat was shown "44.23% of
                # portfolio" for an add to a name already at 20.77%, objected
                # that 44.23 was over-concentrated, wrote 30 — and the
                # pipeline applied 30 as an increment, landing the position at
                # 50.8%, LARGER than the number it had just rejected.
                #
                # The resulting weight is computed with `weight_pct_of`, the
                # ONE definition of a gross-leverage weight in this codebase
                # and the same function `max_position_pct` is measured with.
                # NOT the raw `market_value / denom` used for the position
                # lines further down: for a 3x name those two differ by 3x,
                # and stating a non-gross "resulting weight" next to a
                # gross-measured ceiling would just relocate the lie.
                #
                # Deliberately NOT stated as "write 9.23 to land on 30" — this
                # seat produced two contradictory hand-computed R/R figures in
                # one response on 2026-08-31, which is why R/R is pre-computed
                # for it twelve lines below. Arithmetic on the live sizing path
                # is bounded in Python instead: `_apply_risk_modifications`
                # refuses an allocation_pct edit on a BUY that INCREASES size.
                #
                # 2026-09-18, board items 135 and 137 — two gaps left behind
                # by the change above, both of which let the RAW number on
                # this row differ from the GROSS number the ceiling measures:
                #
                #   135 — the branch was `d.action == "BUY"`. A SHORT that
                #         ADDS to a short already held is sized by the SAME
                #         cumulative arithmetic in the constructor
                #         (`name_headroom_pct = (max_position_pct -
                #         current_short_gross_pct) / gross_mul`, the mirror of
                #         the long clamp), so the label understated a short
                #         add by the whole existing short. Nothing in the
                #         renderer or the guard covered it.
                #
                #   137 — even on a FRESH open with no holding at all,
                #         `allocation_pct` is RAW NOTIONAL. For a 3x fund
                #         (`ETF_LEVERAGE` in src/quantities.py — SQQQ -3.0,
                #         SDS -2.0, both in the configured universe) 21.67%
                #         "of portfolio" is 65% of gross exposure, i.e. the
                #         whole single-name ceiling. The constructor already
                #         divides its headroom by `gross_mul`, so it sizes
                #         correctly; only the number SHOWN to this seat was
                #         un-levered, and the seat is asked to judge it
                #         against a gross-measured ceiling.
                #
                # Both are stated from the SAME `weight_pct_of` the clamp
                # uses. No constant is introduced: the multiple comes from
                # `ETF_LEVERAGE`, the ceiling from `RiskConfig`, the weights
                # from the equity denominator already rendered below.
                held_raw = held_raw_by_symbol.get(str(d.symbol or "").strip().upper(), 0.0)
                if d.action in ("BUY", "SHORT") and denom > 0:
                    from src.risk.rules import _gross_multiplier, weight_pct_of

                    # Same-side only. A SHORT proposed against a name the desk
                    # is LONG is a reduction of net exposure, not an add to a
                    # short, and the constructor does not route it through the
                    # short builder's cumulative clamp — so claiming it adds
                    # to an existing position would be a new false statement.
                    same_side_raw = (
                        held_raw
                        if (d.action == "BUY" and held_raw > 0) or (d.action == "SHORT" and held_raw < 0)
                        else 0.0
                    )
                    # Unsigned throughout: `max_position_pct` is measured on
                    # gross magnitude, which is why the short clamp takes
                    # `abs(current_pct)`. weight_pct_of is signed.
                    held_pct = abs(weight_pct_of(same_side_raw, d.symbol, denom))
                    increment_pct = abs(
                        weight_pct_of(
                            denom * (d.allocation_pct / 100.0),
                            d.symbol,
                            denom,
                        )
                    )
                    resulting_pct = held_pct + increment_pct
                    gross_mul = _gross_multiplier(str(d.symbol or "").strip().upper())
                    side_word = "position" if d.action == "BUY" else "short"
                    if held_pct > 0:
                        alloc = (
                            f"ADD of {d.allocation_pct}% of portfolio — this is an "
                            f"INCREMENT, not the resulting weight. {d.symbol} is "
                            f"already {held_pct:.2f}% of the book, so filling this "
                            f"order leaves the {side_word} at {resulting_pct:.2f}% "
                            f"(gross-leverage weight — the same measure "
                            f"`max_position_pct` is checked against). If you edit "
                            f"this `allocation_pct` it is applied as an INCREMENT "
                            f"too, on top of the {held_pct:.2f}% already held — it "
                            f"is not the position size you are setting. An edit "
                            f"that raises it is refused by the engine; only a "
                            f"reduction is applied"
                        )
                    elif gross_mul != 1.0:
                        alloc = (
                            f"{d.allocation_pct}% of portfolio in RAW NOTIONAL — "
                            f"{d.symbol} is a {gross_mul:g}x fund, so this order "
                            f"opens {increment_pct:.2f}% of GROSS exposure, which "
                            f"is the measure `max_position_pct` is checked "
                            f"against. Judge the size on {increment_pct:.2f}%, not "
                            f"on {d.allocation_pct}%. An edit to this "
                            f"`allocation_pct` is also read as raw notional and is "
                            f"multiplied by {gross_mul:g} the same way; an edit "
                            f"that raises it is refused by the engine, only a "
                            f"reduction is applied"
                        )
            # R/R is rendered from `TradeDecision.reward_risk`, a Python
            # computed field — NOT left for the model to divide out of the
            # prices below. On 2026-08-31 this seat was given bare prices,
            # did the arithmetic itself, produced 1.65 in `rr_audit` and 1.31
            # in `reasoning` IN THE SAME RESPONSE, and rejected a compliant
            # trade on the wrong one. The ratio the floor is judged against
            # must come from the same deterministic code that built the order.
            # None only for SELL/COVER/HOLD, which have no entry geometry.
            #
            # 2026-09-11 (docs/WORK.md item 1(d)): a Type B / breakout order
            # gets no ratio shown at all, and is told why. The arithmetic
            # would still divide — `take_profit` carries a measured-move
            # reference — but printing "R/R 0.9:1" to a seat whose prompt
            # tells it R/R discipline is non-negotiable invited exactly the
            # refusal this change removed, on a number no gate in the
            # pipeline consults for this setup type any more. Silence would
            # be worse than a bad number here: the RM would simply assume
            # the field was missing.
            # item 82: pass the MEASURED half too (`TradeDecision.
            # structural_ceiling`, populated at construction from the same
            # `derivation.level_used` the constructor's own reward:risk gate
            # used), not just the raw analyst label — otherwise a measured
            # breakout the analyst still labelled "range" is shown a real
            # R/R ratio here and this seat can refuse/resize a trade
            # construction's own exemption forbids cutting.
            if not reward_risk_floor_applies(
                getattr(d, "setup_type", None),
                structural_ceiling=getattr(d, "structural_ceiling", None),
            ):
                rr = (
                    " | R/R n/a — BREAKOUT setup: no overhead level to "
                    "measure a reward against, managed by trailing stop with "
                    "no fixed target. Judge it on the RISK side (stop, "
                    "conviction, evidence); do NOT refuse or resize it on a "
                    "reward:risk figure"
                )
            else:
                rr = f" | R/R {d.reward_risk}:1" if d.reward_risk is not None else ""
            # No take-profit (owner rule 2026-10-09) reads ABSENT, never "$None".
            tgt = f"${d.take_profit}" if d.take_profit is not None else "absent (no structural level; trend trade)"
            return (
                f"- {d.action} {d.symbol}: {alloc} | Entry: ${d.entry_price} | "
                f"Stop: ${d.stop_loss} | Target: {tgt}{rr}\n  Reasoning: {d.reasoning}"
            )

        decisions_text = "\n".join(_fmt_decision(d) for d in portfolio_decision.decisions)

        if (total_value or 0) > 0:
            cash_bit = ""
            if cash is not None:
                cash_pct = (cash / total_value * 100) if total_value else 0.0
                cash_bit = f" | Cash (deployable this session): ${cash:,.0f} ({cash_pct:.1f}%)"
            if reserve_balance > 0:
                cash_bit += (
                    f" (a further ${reserve_balance:,.0f} is held in the "
                    f"cash-equivalent sweep vehicle; it is NOT sold to fund "
                    f"a BUY and is NOT part of the cash above)"
                )
            account_section = f"## Account\n- Total equity: ${total_value:,.0f}{cash_bit}\n"
        elif approx_book > 0:
            account_section = (
                f"## Account\n- Total book (approx = sum of listed positions; "
                f"broker equity not provided, so weights below slightly "
                f"overstate true %-of-equity): ${approx_book:,.0f}\n"
            )
        else:
            # No equity and no positions — the drawdown line below still needs
            # a header to hang off, so open the section anyway.
            account_section = "## Account\n"

        # Recent system performance, rendered inside the Account section
        # because it is a property of the account, not of any one name.
        # INFORMATION ONLY since 2026-09-20: the rolling-return brake that
        # used to halve every BUY on these numbers was removed with the rest
        # of the account-level loss alarms (retired item 32).
        if recent_performance:
            r5 = recent_performance.get("rolling_5d_pct")
            r20 = recent_performance.get("rolling_20d_pct")
            trailing = recent_performance.get("trailing_days")
            sample_bit = f", {trailing} trailing sessions" if trailing is not None else ""
            account_section += (
                f"- System performance: 5d {_fmt_or_na(r5, '%')} | 20d {_fmt_or_na(r20, '%')}{sample_bit}\n"
            )
        else:
            account_section += "- System performance: not provided (drawdown state unknown this run)\n"

        # Audit §1.3 — the book's actual risk, in dollars and in % of equity,
        # with each position's R-multiple. `sizing_sanity` is asked whether any
        # bet is outsized; this is the number that answers it.
        if heat is not None:
            from src.risk.metrics import format_heat_block

            risk_section = format_heat_block(
                heat,
                risk_ceiling_pct,
                title="Portfolio Risk (deterministic, computed in Python)",
            )
        else:
            risk_section = (
                "## Portfolio Risk\n"
                "- not computed this run (stop data unavailable). Total at-risk "
                "is UNKNOWN; say so rather than assuming the book has "
                "headroom.\n"
            )

        def _fmt_position(p: Position) -> str:
            weight_bit = ""
            if denom > 0:
                weight_bit = f" | Value: ${p.market_value:,.0f} ({p.market_value / denom * 100:.1f}% of book)"
            # days_held is informational only (spec item 25, 2026-09-03/04):
            # holding-discipline protection is NO LONGER a function of age.
            # It replaced a flat <5d/5-15d/>15d day-count tier (no backtest
            # behind it, owner-rejected as arbitrary) with a deterministic,
            # data-driven check (`check_structural_protection` /
            # `holding_discipline_false_claim`) run in Python against each
            # SELL/REDUCE/COVER PM actually proposes — a young position with
            # a broken, confirmed thesis is NOT protected, and an old one
            # with an intact thesis IS. RM cannot see that verdict before
            # it speaks (the deterministic check runs on PM's decisions
            # after RM's own review), so `held: Nd` here is just context,
            # not a claim about whether an exit needs a named trigger —
            # see config/prompts/risk_manager.md item 8 for what to actually
            # apply.
            hist = position_history.get(p.symbol) or {}
            days = hist.get("days_held")
            age_bit = " | held: unknown" if days is None else f" | held: {days}d"
            return (
                f"- {p.symbol}: {p.qty} shares @ ${p.avg_entry:.2f} | "
                f"Current: ${p.current_price:.2f} | P&L: ${p.unrealized_pnl:.2f}"
                f"{weight_bit}{age_bit} | Sector: {p.sector}"
            )

        positions_text = "\n".join(_fmt_position(p) for p in positions) if positions else "No current positions."

        violations_text = _format_engine_findings(rule_violations)

        vix = macro_summary.get("vix", {}) or {}
        treasury = macro_summary.get("treasury", {}) or {}
        fed_funds_obj = macro_summary.get("fed_funds_rate", {}) or {}
        # Backward-compat: fed_funds_rate was previously a float; now a dict.
        if isinstance(fed_funds_obj, (int, float)):
            fed_funds = fed_funds_obj
        else:
            fed_funds = fed_funds_obj.get("current")

        # PM reasoning chain (if available).
        #
        # 2026-08-13 agent audit — two findings land here.
        #
        # "risk evidence completeness": risk_manager.md tells RM it reads a
        # NINE-field chain and names `continuity_check` / `premortem_check`
        # explicitly. This renderer emitted seven. The two it dropped are the
        # two the schema defaults to "" (ReasoningChain, src/models.py), i.e.
        # exactly the two that can go missing without any parse error — so the
        # only reviewer positioned to notice was the one not being shown them.
        #
        # "premortem/observability": an absent field is rendered as an explicit
        # [MISSING] line rather than omitted. A silently absent section reads
        # to RM as "PM had nothing to say"; a [MISSING] marker reads as "the
        # mandatory step did not happen", which is a finding RM can act on.
        rc = portfolio_decision.reasoning_chain
        if rc:

            def _field(label: str, value: str, *, mandatory_prompt_only: bool = False) -> str:
                text = (value or "").strip()
                if text:
                    return f"- {label}: {text}"
                if mandatory_prompt_only:
                    return (
                        f"- {label}: [MISSING — this field is MANDATORY in PM's "
                        f"prompt but optional in the schema, so PM returning it "
                        f"empty raises no parse error. Treat the audit step as "
                        f"NOT PERFORMED and say so in `reasoning_chain.overall`.]"
                    )
                return f"- {label}: [EMPTY]"

            # The two PM-only audit steps are rendered ONLY where they exist.
            # On the exit path the chain is the position reviewer's, whose
            # schema has no such fields — banner-ing them as NOT PERFORMED
            # there told the seat a falsehood on every single run.
            chain_title, chain_preamble = risk_review_mode.reasoning_chain_heading(review_mode)
            chain_rows = [
                _field(label, getattr(rc, attr, ""), mandatory_prompt_only=mandatory)
                for label, attr, mandatory in risk_review_mode.chain_rows(review_mode)
            ]
            reasoning_section = "\n".join([chain_title, "", chain_preamble, ""] + chain_rows + [""])
        else:
            reasoning_section = (
                "## PM Reasoning Chain\n"
                "(not provided — PM emitted no audit trail at all. Its plan is "
                "unaudited by construction; weigh that in your verdict.)\n"
            )

        # Tech Analyst Signals — lets RM audit PM's fidelity AND enforce R/R discipline.
        #
        # The R/R on THIS line is the analyst's, measured at the analyst's
        # own snapshot entry against the analyst's own guessed target. The
        # R/R on the decision line above is the ORDER's, measured at the
        # live entry the constructor priced against the target it derived
        # from the bars. They are different numbers because they are
        # different questions, and both are arithmetically correct.
        #
        # Saying so is not decoration. On 2026-09-01 this seat rejected an
        # entire plan over XLE with "PM's reasoning assumes R/R 1.67 but the
        # executed order has R/R 1.18 ... no justification is given", and on
        # 2026-08-31 it halved an XLE allocation citing "entry price
        # degradation from TechAnalyst's $62.29 to $63.76". Nothing had gone
        # wrong either time and no stop had moved — the entry drifted between
        # analysis and construction, as it always will. The seat was left to
        # infer a defect from an unexplained gap, so it inferred one.
        if tech_analyses:
            tech_lines = []
            for a in tech_analyses:
                rr = getattr(a, "risk_reward", None)
                rr_str = f"R/R {rr:.2f}:1" if rr is not None else "R/R n/a"
                price_str = f"entry ${a.entry_price}, stop ${a.stop_loss}" if a.entry_price else "no prices"
                tech_lines.append(
                    f"- {a.symbol}: {a.rating} ({a.conviction}) | {rr_str} | {price_str} — {a.reasoning[:120]}"
                )
            tech_section = (
                "## Tech Analyst Signals (cross-check PM's decisions)\n"
                "The R/R here is the ANALYST's, priced at its own snapshot entry "
                "against its own target. The R/R on each order above is the "
                "ORDER's, priced at the live entry and the structurally derived "
                "target — that is the real one. A gap between the two is normal "
                "price drift between analysis and construction, not a defect and "
                "not PM inconsistency; both are computed by the same Python "
                "function. **There is no reward:risk floor.** Nothing has been "
                "refused for failing one, a breakout has no ratio at all, and a "
                "thin ratio on a range trade is NOT resized by anything: the "
                "constructor computes the ratio for ranking and logging only, "
                "so no size has been adjusted for it before you see it. It is "
                "information; the sizing judgement is yours. A low number is "
                "not, on its own, grounds to refuse anything.\n" + "\n".join(tech_lines)
            )
        else:
            tech_section = risk_review_mode.absent_block("tech", review_mode)

        # News intelligence — RM needs it to catch silent contradictions between
        # PM's proposals and today's news (e.g., BUY energy on a ceasefire day).
        if news_intel:
            conv_order = {"high": 0, "medium": 1, "low": 2}
            state_lines = [
                f"- [{c.conviction.upper()}] {c.event}: {c.previous_state} → {c.new_state} "
                f"(impact: {c.market_impact}; affects: {', '.join(c.affected_symbols[:5]) or 'broad'})"
                for c in (news_intel.state_changes or [])[:5]
            ]
            state_text = "\n".join(state_lines) or "No HIGH/MED state changes today."
            # Alerts on symbols PM is trading
            trade_syms = {d.symbol for d in portfolio_decision.decisions}
            alert_lines = []
            for sym, alerts in (news_intel.stock_news or {}).items():
                if sym not in trade_syms:
                    continue
                for a in sorted(alerts, key=lambda x: conv_order.get(x.conviction, 9))[:2]:
                    alert_lines.append(f"- {sym}: [{a.conviction.upper()}] {a.sentiment} — {a.impact_summary}")
            alerts_text = "\n".join(alert_lines) or "No alerts on traded symbols."
            lost_text = news_intel.format_dropped_symbols_block()
            news_section = f"""## News Intelligence (use to verify PM hasn't contradicted today's events)
PM Briefing: {news_intel.pm_briefing[:300]}

State changes today:
{state_text}

Alerts on PM's traded symbols:
{alerts_text}{lost_text}

Overall sentiment: {news_intel.format_market_sentiment()} ({news_intel.confidence})
"""
        else:
            news_section = risk_review_mode.absent_block("news", review_mode)

        # Earnings — placeholders for queued filings flag event risk on those names.
        if earnings_analyses:
            earn_lines = []
            for ea in earnings_analyses:
                sym = ea.get("symbol", "?")
                if ea.get("queued"):
                    earn_lines.append(
                        f"- {sym}: [JUST FILED {ea.get('form_type', '?')} {ea.get('filing_date', '?')} — "
                        f"ANALYSIS PENDING; cap BUY ≤ 5%]"
                    )
                else:
                    analysis = ea.get("analysis") or {}
                    impl = analysis.get("investment_implications") or {}
                    earn_lines.append(
                        f"- {sym}: {impl.get('sentiment', '?')} ({impl.get('conviction', '?')}) — "
                        f"{impl.get('key_thesis', '')[:120]}"
                    )
            earnings_section = "## Earnings (verify PM respected queued-filing cap)\n" + "\n".join(earn_lines) + "\n"
        else:
            earnings_section = ""

        # 2026-08-13 agent audit — "PM/Risk independence". PM's reasoning chain
        # used to be the FIRST thing in this message, so RM read PM's case for
        # the plan before it saw a single primary number and then graded the
        # story rather than the book. The blocks are now ordered
        #
        #   what PM proposes -> the account/market facts -> PM's claims about
        #   them -> the deterministic engine's findings -> verdict
        #
        # so RM forms its own read from primary data first, and the last input
        # before the verdict is the one input PM did not author. Nothing was
        # added to or removed from what RM may DO about a disagreement — no
        # threshold moved; only the order in which it learns things.
        # Deterministic removals, stated plainly. PM's reasoning_chain below
        # was written BEFORE the constructor ran, so it may argue for symbols
        # that are not in the order list above. Without this block that reads
        # as PM contradicting itself and invites a full-plan veto — which is
        # exactly what happened on 2026-08-31, costing two trades this seat
        # had just called valid.
        dropped = getattr(portfolio_decision, "constructor_dropped", None) or []
        dropped_text = ""
        if dropped:
            dropped_text = (
                "\n## Removed Before You Saw This\n"
                f"Removed by deterministic code before this review: "
                f"{', '.join(dropped)}.\n"
                "Each was struck by a rule in Python — the constructor could "
                "not measure or build it, or a later gate (unsupported "
                "symbol, an earnings date inside the window, a hard risk "
                "limit) removed it. Which rule it was is recorded per symbol "
                "in the evidence trail; it was NOT a judgement call and is "
                "not yours to review. PM's reasoning below was written BEFORE "
                "happened, so it may still argue for them. That is EXPECTED "
                "and is NOT evidence of an incoherent plan — do not veto the "
                "surviving trades over it. Judge only the orders listed "
                "above.\n"
            )

        return f"""{risk_review_mode.mode_header(review_mode)}## Proposed Trades
{decisions_text}
{dropped_text}
Portfolio View: {portfolio_decision.portfolio_view}

{account_section}
{risk_section}
## Current Positions
{positions_text}

{tech_section}

{news_section}
{earnings_section}{event_risk_block}

## Macro Context
- VIX: {_fmt_or_na(vix.get("current"))} (5d avg: {_fmt_or_na(vix.get("mean_5d"))}, trend: {_fmt_or_na(vix.get("trend"))})
- 2Y Treasury: {_fmt_or_na(treasury.get("us2y"), "%")}
- 10Y Treasury: {_fmt_or_na(treasury.get("us10y"), "%")}
- 2Y-10Y Spread: {_fmt_or_na(treasury.get("spread_2_10"), "%")} (inverted: {_fmt_or_na(treasury.get("inverted"))})
- Fed Funds Rate: {_fmt_or_na(fed_funds, "%")}

{reasoning_section}
## Engine Risk Check Results
{violations_text}

Review these proposed trades and provide your verdict as JSON."""

    def review(
        self,
        portfolio_decision: PortfolioDecision,
        positions: list[Position],
        macro_summary: dict,
        rule_violations: list[RiskViolation],
        tech_analyses: list[TechAnalysisResult] | None = None,
        news_intel: NewsIntelligenceReport | None = None,
        earnings_analyses: list[dict] | None = None,
        total_value: float | None = None,
        cash: float | None = None,
        reserve_balance: float = 0.0,
        position_history: dict | None = None,
        recent_performance: dict | None = None,
        heat=None,
        risk_ceiling_pct: float | None = None,
        event_risk_block: str | None = None,
        review_mode: str = risk_review_mode.MORNING_PLAN,
    ) -> tuple["RiskVerdict | ExitRiskVerdict | None", "AgentResult"]:
        # WHICH verdict shape this path returns, computed BEFORE the call so
        # `result_model` can be set for it (see BaseAgent.result_model /
        # _openai_wire_call's response_format). The exit review's schema is
        # `RiskVerdict` minus `modifications` and `scale_all_buys`: nothing on
        # that path applies either (`_apply_risk_modifications` is called only
        # from the morning `RiskStage`), so they are not asked for and not
        # stored. See `src/models.ExitRiskVerdict`.
        exit_mode = risk_review_mode.is_exit_review(review_mode)
        verdict_model = ExitRiskVerdict if exit_mode else RiskVerdict
        schema_name = verdict_model.__name__
        self.result_model = verdict_model
        # audit round 2 #5: total_value / cash are optional so existing call
        # sites keep working; when omitted, build_user_message approximates
        # the book denominator from the sum of position market values.
        # 2026-08-13 audit: position_history / recent_performance are optional
        # for the same reason — they carry the `days_held` evidence RM needs
        # to audit PM's holding-discipline rules, and their absence is
        # rendered explicitly rather than assumed benign.
        result = self.run(
            portfolio_decision=portfolio_decision,
            positions=positions,
            macro_summary=macro_summary,
            rule_violations=rule_violations,
            tech_analyses=tech_analyses or [],
            news_intel=news_intel,
            earnings_analyses=earnings_analyses or [],
            total_value=total_value,
            cash=cash,
            reserve_balance=reserve_balance,
            position_history=position_history or {},
            recent_performance=recent_performance or {},
            heat=heat,
            risk_ceiling_pct=risk_ceiling_pct,
            # Optional for the same reason every other evidence kwarg here is:
            # existing call sites keep working. Absent, build_user_message
            # renders the explicit NOT FETCHED form rather than nothing —
            # `event_risk` is a MANDATORY output field, so the one thing this
            # input must never do is disappear silently.
            event_risk_block=event_risk_block,
            # Defaults to the morning plan, so every pre-existing call site
            # renders byte-identically. See src/agents/risk_review_mode.py.
            review_mode=review_mode,
        )
        # `modifications` and `scale_all_buys` are decision-bearing ONLY where
        # they decide something. On the exit path they are not fields at all,
        # so a repair that "changed" one changed nothing that can reach the
        # broker — treating that as an unauthorized re-decision would fail a
        # sound verdict closed, and on THIS path failing closed blocks a SALE.
        decision_fields = self._EXIT_DECISION_FIELDS if exit_mode else self._DECISION_FIELDS
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Risk manager returned non-JSON response")
            result.gate_reason = "risk_non_json"  # item 188, recording only
            return None, result
        # Per-entry isolation for modifications: a single malformed
        # RiskModification (e.g. non-numeric original_value, wrong field
        # name) must not drop the whole RiskVerdict. The verdict carries
        # `approved`, `reasoning_chain`, `rejected_symbols`, `scale_all_buys`,
        # `reason_category`, plus the OTHER modifications — losing all of that because one
        # mod row is bad means execution stage has no RM guidance and
        # PM's calibration history loses a row. Mirrors PR #74 pattern.
        #
        # `rejected_symbols` deliberately gets the OPPOSITE treatment and has
        # no drop-invalid pass. Dropping a malformed modification loses a
        # tuning instruction; dropping a malformed refusal would let a symbol
        # the risk manager explicitly refused go on and trade. So the model
        # normalizes every shape that still names a symbol
        # (`SymbolRejection._coerce_shorthand`) and lets anything that does
        # not fail validation — which, `rejected_symbols` being decision-
        # bearing, fails the whole verdict closed.
        if isinstance(parsed, dict) and not exit_mode:
            parsed = self._drop_invalid_modifications(parsed)
        try:
            verdict = verdict_model(**parsed)
            record_guarded_pass(self, "risk_manager.verdict_parse", log=logger)
            return verdict, result
        except ValidationError as e:
            # 2026-08-18 incident: an APPROVING verdict with three sound
            # modifications died because two reasoning_chain prose fields
            # were omitted — recorded as "REJECTED: parse error", trading
            # day over. One bounded repair reprompt names the exact
            # validation errors; a second failure keeps the fail-closed
            # None → reject path exactly as before.
            #
            # External review (post-implementation): a schema repair must
            # never become a re-decision. If the validation failure is
            # itself rooted in a DECISION-bearing field, a repair call
            # can't fix it without the model re-deciding — skip repair
            # and fail closed immediately. Otherwise, after repair,
            # decision-bearing fields must be byte-identical to the
            # pre-repair parse; any drift is treated as an unauthorized
            # re-decision and also fails closed.
            if self.validation_error_touches(e, decision_fields):
                logger.error(
                    "Risk verdict validation failure is rooted in a "
                    "decision-bearing field (%s) — not schema-repairable; "
                    "failing closed: %s",
                    ", ".join(decision_fields),
                    e,
                )
                result.gate_reason = "risk_decision_field_validation_failure"
                return None, result
            repaired = self.repair_reprompt(result, e, schema_name)
            reparsed = repaired.parse_json()
            if isinstance(reparsed, dict):
                if not exit_mode:
                    reparsed = self._drop_invalid_modifications(reparsed)
                if not self._decision_fields_unchanged(
                    parsed,
                    reparsed,
                    fields=decision_fields,
                ):
                    logger.error(
                        "Risk verdict repair changed decision-bearing "
                        "content (%s) instead of only completing the schema "
                        "— treating as an unauthorized re-decision and "
                        "failing closed.",
                        "/".join(decision_fields),
                    )
                    repaired.gate_reason = "risk_repair_changed_decision"
                    return None, repaired
                try:
                    verdict = verdict_model(**reparsed)
                    logger.info(
                        "%s repair succeeded (approved=%s, %d mods, %d per-symbol refusals)",
                        schema_name,
                        verdict.approved,
                        len(getattr(verdict, "modifications", ())),
                        len(verdict.rejected_symbols),
                    )
                    record_guarded_pass(self, "risk_manager.verdict_repair_parse", log=logger)
                    return verdict, repaired
                except Exception as e2:  # noqa: BLE001
                    record_guarded_pass(self, "risk_manager.verdict_repair_parse", e2, log=logger)
                    logger.error(
                        "Failed to parse risk verdict after repair: %s",
                        e2,
                    )
                    repaired.gate_reason = "risk_repair_schema_error"
                    return None, repaired
            logger.error(
                "Risk verdict repair returned %s, not an object",
                type(reparsed).__name__,
            )
            repaired.gate_reason = "risk_repair_not_object"
            return None, repaired
        except Exception as e:
            record_guarded_pass(self, "risk_manager.verdict_parse", e, log=logger)
            logger.error("Failed to parse risk verdict: %s", e)
            result.gate_reason = "risk_parse_exception"
            return None, result

    # `rejected_symbols` (Phase 10.1) belongs here for exactly the reason the
    # other four do: it is a REFUSAL, not prose. A repair call that adds,
    # drops or rewrites one has re-decided which trades die, and a validation
    # failure rooted in it cannot be schema-repaired without the model
    # re-deciding — both fail closed, refusing the whole plan, which is the
    # conservative direction.
    _DECISION_FIELDS = (
        "approved",
        "modifications",
        "rejected_symbols",
        "scale_all_buys",
        "reason_category",
    )

    #: The same list on the EXIT-REVIEW path, minus the two levers that are
    #: not fields of `ExitRiskVerdict` at all. Nothing applies a modification
    #: or a scale factor to an exit, so neither can re-decide anything there;
    #: keeping them would fail a repairable verdict CLOSED, and a fail-closed
    #: exit leaves a broken-thesis position on the book (the exact asymmetry
    #: `_risk_review_exits` fails OPEN for, owner-ratified 2026-08-27).
    _EXIT_DECISION_FIELDS = (
        "approved",
        "rejected_symbols",
        "reason_category",
    )

    _canonical_rejections = staticmethod(canonical_rejections)
    _canonical_modifications = staticmethod(canonical_modifications)
    _drop_invalid_modifications = staticmethod(drop_invalid_modifications)

    @classmethod
    def _decision_fields_unchanged(
        cls,
        original: dict,
        repaired: dict,
        *,
        fields: tuple[str, ...] | None = None,
    ) -> bool:
        """True iff every decision-bearing field survived a schema
        repair unchanged. `original` and `repaired` are both already
        post-`_drop_invalid_modifications` for a fair comparison.

        Strict and type-safe by construction — no `bool()` coercion (a
        repair emitting the JSON STRING `"false"` for `approved` must
        fail closed, not compare equal to `True` because `bool("false")`
        is truthy) and no `or 1.0` fallback on `scale_all_buys` (0.0 is
        a real, meaningful value — RM's explicit "kill all BUYs" veto —
        not an absent one; collapsing it to 1.0 would silently accept a
        repair that reinstated every BUY the original verdict killed).

        `fields` names which of them decide anything on the calling path;
        it defaults to `_DECISION_FIELDS` (the morning plan). The exit path
        passes `_EXIT_DECISION_FIELDS`, because `modifications` and
        `scale_all_buys` are not fields of `ExitRiskVerdict` and nothing
        there applies them — comparing them would fail a sound verdict
        closed over content that cannot reach the broker.
        """
        fields = fields or cls._DECISION_FIELDS
        orig_approved = original.get("approved")
        rep_approved = repaired.get("approved")
        if type(orig_approved) is not bool or type(rep_approved) is not bool:
            return False
        if orig_approved != rep_approved:
            return False

        if "modifications" in fields:
            orig_mods = cls._canonical_modifications(original.get("modifications"))
            rep_mods = cls._canonical_modifications(repaired.get("modifications"))
            if orig_mods is None or rep_mods is None or orig_mods != rep_mods:
                return False

        # Phase 10.1: a repair that quietly reinstates a refused symbol, or
        # newly refuses one, has changed which trades die.
        orig_rej = cls._canonical_rejections(original.get("rejected_symbols"))
        rep_rej = cls._canonical_rejections(repaired.get("rejected_symbols"))
        if orig_rej is None or rep_rej is None or orig_rej != rep_rej:
            return False

        if "scale_all_buys" in fields:
            orig_scale = original.get("scale_all_buys", 1.0)
            rep_scale = repaired.get("scale_all_buys", 1.0)
            if isinstance(orig_scale, bool) or isinstance(rep_scale, bool):
                return False
            if not isinstance(orig_scale, (int, float)) or not isinstance(rep_scale, (int, float)):
                return False
            if round(float(orig_scale), 6) != round(float(rep_scale), 6):
                return False

        return original.get("reason_category") == repaired.get("reason_category")
