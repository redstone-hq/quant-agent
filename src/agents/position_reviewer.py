"""Position reviewer — the sell-only agent that runs at midday (13:00 ET) and
close (15:30 ET).

Formerly MiddayReviewerAgent. Renamed because it runs twice per day and both
sessions share the same logic + memory, differing only in `session_type`
disposition (midday is patient, close is act-on-trigger because there's no
overnight control).

v3 upgrades vs the original MiddayReviewerAgent:
  - Schema: mandatory 6-step `PositionReasoningChain` (parallel depth to
    morning PM's 7-step chain). Empty strings fail Pydantic validation.
  - Memory layers: macro regime + 7-day macro trajectory + 7-day portfolio
    narrative + 14-day active HIGH state changes + 45-day trade calibration
    + PM's recent decisions + yesterday's evening insights + recent system
    performance + full earnings analyses (not just queued list).
  - Deterministic pre-compute: per-held-position `thesis_progress_pct` /
    `pace` / `distance_to_stop_pct` / `distance_to_target_pct` + one
    winner flag (parabolic). Math done in Python
    and surfaced as numbers, not asked of the LLM.
  - Session-type awareness: 'midday' and 'close' prompts differ in bias.
  - Anti-flip-flop: "Own Recent Decisions" memory shown so the agent
    doesn't reverse itself within hours without a named thesis break.
"""

import logging
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent
from src.models import (
    NewsIntelligenceReport,
    Position,
    PositionAction,
    PositionReview,
)
from src.agents.prompt_limits import render_prompt_limits
from src.risk.metrics import unrealized_pnl_pct

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "position_reviewer.md"


def _fmt_or_na(value, suffix: str = "") -> str:
    """Render a macro metric, falling back to 'N/A' when the provider
    returned None (FRED outage). The macro provider always ships every
    key with None values on failure, so `.get(key, 'N/A')` defaults never
    fire — prompts were literally rendering 'VIX: None' / 'Nonebps'
    (audit round 2 #34)."""
    return "N/A" if value is None else f"{value}{suffix}"


_SESSION_LABEL = {
    "midday": "Midday (13:00 ET) — afternoon still open",
    "close": "Close (15:30 ET) — ~25 min to close, 17.5h until next intraday control",
}

_SESSION_DISPOSITION = {
    "midday": (
        "You have 2.5+ hours of trading left. Default disposition is PATIENT. "
        "Prefer TRAIL_STOP over SELL when the situation is 'drifting, not breaking'. "
        "Let afternoon resolve ambiguous signals — it usually will. "
        "Only SELL on a specific thesis trigger that won't improve by waiting."
    ),
    "close": (
        "You won't have hands on the wheel for 17.5 hours after close. That does NOT "
        "mean sell more — 'it's close time' is never a trigger. It means: if a thesis "
        "trigger is CLEARLY firing (thesis_invalid_if condition satisfied, HIGH "
        "conviction state_change reversing the thesis, momentum demonstrably dead), "
        "act NOW rather than hoping tomorrow morning catches it. If no trigger is "
        "firing, HOLD — good stocks are meant to be held over weekends and "
        "overnights alike."
    ),
}


class PositionReviewerAgent(BaseAgent):
    """Sell-only agent: reviews open positions, outputs HOLD / TRAIL_STOP /
    SELL / COVER (whole exits only). Never BUYs. Used at midday and close sessions."""

    # See review(): parsed JSON is validated as PositionReview(**parsed).
    result_model = PositionReview

    @staticmethod
    def _trade_executed(trade: dict) -> bool:
        """Belt-and-braces guard for BUY rows surfaced to the LLM prompt.

        In practice the caller has already filtered via db.get_trades(
        executed_only=True), so by the time a row reaches here it's either
        filled, legacy NULL-status, or canceled-with-partial-fill. This
        predicate re-applies the same shape check so a future caller that
        forgets executed_only can't accidentally inject a submitted-only or
        zero-fill canceled BUY into the reviewer's trade-context map.
        """
        status = trade.get("fill_status")
        if status is None:
            return True
        if str(status).lower() == "filled":
            return True
        try:
            return float(trade.get("fill_qty") or 0) > 0
        except (TypeError, ValueError):
            return False

    @property
    def name(self) -> str:
        return "position_reviewer"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            # Item 107: the sheet's drift thresholds are placeholders, not
            # typed digits, so they cannot disagree with the pair the flag is
            # actually computed from. `risk_config=None` is correct here —
            # this sheet is registered for the `flags` namespace only, and a
            # `{{risk.*}}` placeholder added to it raises rather than
            # rendering blank.
            return render_prompt_limits(PROMPT_PATH.read_text(), None)
        return "You are a position reviewer. Respond with JSON."

    def build_user_message(self, **kwargs) -> str:
        positions: list[Position] = kwargs["positions"]
        macro_summary: dict = kwargs["macro_summary"]
        cash_balance: float = kwargs["cash_balance"]
        # Cash-equivalent sweep reserve (SGOV), reported separately —
        # 2026-08-19 SGOV/deployable-liquidity forensic. Never re-add this
        # to cash_balance; it is not reliably spendable same-day.
        reserve_balance: float = kwargs.get("reserve_balance", 0.0) or 0.0
        total_value: float = kwargs["total_value"]
        session_type: str = kwargs.get("session_type", "midday")
        position_facts: dict = kwargs.get("position_facts") or {}  # symbol → deterministic-metrics dict
        # Phase 3.2 / audit §1.5 — {symbol: MetricDeltas} versus this seat's
        # own previous review. Without it the reviewer rebuilds its view from
        # scratch twice a day and can call a position stalled while every
        # number it recorded six hours earlier improved.
        metric_deltas: dict = kwargs.get("metric_deltas") or {}
        morning_trades: list[dict] = kwargs.get("morning_trades", [])
        # Board item 89 defect 3 / item 104's eighth prompt defect: the
        # date-unrestricted entry row per held symbol. `morning_trades`
        # stays `today_only` — the system-actions block below depends on
        # that — so this is the only thing that can answer "why is this
        # held" for a position opened before today.
        entry_context: dict[str, dict] = kwargs.get("entry_context") or {}
        news_intel: NewsIntelligenceReport | None = kwargs.get("news_intel")
        earnings_analyses: list[dict] = kwargs.get("earnings_analyses") or []
        macro_analysis: dict | None = kwargs.get("macro_analysis")
        weekly_narrative: str = kwargs.get("weekly_narrative") or ""
        macro_trajectory: str = kwargs.get("macro_trajectory") or ""
        active_state_changes: str = kwargs.get("active_state_changes") or ""
        calibration_note: str = kwargs.get("calibration_note") or ""
        own_recent_decisions: str = kwargs.get("own_recent_decisions") or ""
        trade_grade_summary: dict = kwargs.get("trade_grade_summary") or {}
        yesterday_insights: dict | None = kwargs.get("yesterday_insights")
        recent_performance: dict = kwargs.get("recent_performance") or {}
        already_trimmed_today: set[str] = set(kwargs.get("already_trimmed_today") or set())
        # Board item 74 — pre-formatted by `src.risk.spent_trigger`, which is
        # the same module the executor enforces with. Empty when nothing is
        # spent or the record could not be read.
        spent_triggers_block: str = str(kwargs.get("spent_triggers_block") or "")

        # Build morning trade context map (entry thesis, stop_loss, reference target).
        trade_context: dict[str, dict] = {}
        for t in morning_trades:
            sym = t.get("symbol", "")
            if t.get("action") == "BUY" and sym and sym not in trade_context and self._trade_executed(t):
                trade_context[sym] = t

        # Non-LLM system actions taken earlier in this session: a
        # deterministic de-lever (`FORCE_DELEVER` — the gross-exposure
        # ladder, or the cash-only safety net when margin drifted negative),
        # or a broker-resident protective stop firing and getting written
        # back by the stop-out reconciler (2026-08-28 ONDS/CCJ). The
        # `EMERGENCY_SELL` / `EMERGENCY_COVER` tags are still matched here
        # because historical rows carry them; NOTHING EMITS THEM ANY MORE.
        # The account-level loss breaker was removed entirely on 2026-09-20
        # (retired item 32). These bypass the
        # reviewer — the closed positions are already gone from ctx.positions
        # — but surfacing them prevents the reviewer from reasoning in a
        # vacuum about why the book shrank. A circuit-breaker cover
        # shrinks/flattens the book exactly as materially as a forced sell
        # does, so it has to be in this list too, not just the sell side.
        # STOP_OUT rows are backdated to their ACTUAL fill time (see
        # TradingPipeline._parse_broker_fill_timestamp), so this only fires
        # for a stop that fired and was detected within THIS session — an
        # older one simply isn't in `morning_trades` (today_only), which is
        # correct: it isn't today's news.
        system_action_lines: list[str] = []
        for t in morning_trades:
            act = t.get("action")
            if act not in ("FORCE_DELEVER", "EMERGENCY_SELL", "EMERGENCY_COVER", "STOP_OUT"):
                continue
            sym = t.get("symbol") or "?"
            qty = t.get("qty") or 0
            reason = (t.get("reasoning") or "").strip()[:160]
            suffix = f" — {reason}" if reason else ""
            system_action_lines.append(f"- {act} {sym} (qty={qty}){suffix}")

        # Positions block — surfaces deterministic metrics alongside the raw numbers.
        def _pnl_pct(p: Position) -> str:
            # `unrealized_pnl_pct` is the ONE definition of this quantity
            # (src/risk/metrics.py) — the reviewer, the PM's position lines,
            # `_build_position_facts`, the PM facts drift check and evening's
            # thesis-health context all now render the same number. The
            # denominator is |avg_entry x qty|, because a held short's `qty`
            # is negative (Alpaca's convention) and a signed cost basis flips
            # every short's P&L% into reading as its own opposite.
            pct = unrealized_pnl_pct(p)
            return f"{pct:.1f}%" if pct is not None else "N/A"

        positions_lines = []
        for p in positions:
            pnl_pct = _pnl_pct(p)
            # Stage 3 (shorts): state the side explicitly rather than
            # leaving the reviewer to infer it from a negative `qty` —
            # a reviewer that misses the sign reads a winning short as
            # a loser (and vice versa) on every metric that follows.
            side_tag = "SHORT" if p.qty < 0 else "LONG"
            header = (
                f"- **{p.symbol}** ({p.sector}) [{side_tag}]: {p.qty} sh @ "
                f"${p.avg_entry:.2f} | Now ${p.current_price:.2f} | "
                f"P&L ${p.unrealized_pnl:.2f} ({pnl_pct})"
            )
            lines = [header]

            # Deterministic per-position metrics (computed by pipeline).
            pf = position_facts.get(p.symbol) or {}

            ctx = trade_context.get(p.symbol) or {}
            # Board item 89 defect 3 — scoped DELIBERATELY to the thesis
            # text and nothing else.
            #
            # The entry row also carries `stop_loss` and `take_profit`, and
            # reading those from it would be a change to what the reviewer
            # DECIDES on, not to what the owner is told: the back-compute
            # below prefers `position_facts`, which tracks the LIVE broker
            # stop and the live take-profit (revisable and provenanced as
            # of 2026-09-18), whereas an old entry row holds the number
            # PINNED at entry. Letting a stale pinned level win over a live
            # one would degrade the `new_stop >= old_stop * 1.02` judgement.
            # So `entry_context` answers only "why is this held", which is
            # the defect, and the level lines are untouched.
            entry_row = entry_context.get(p.symbol) or {}
            sl = ctx.get("stop_loss") or 0
            tp = ctx.get("take_profit") or 0
            # audit round 2 #4: trade_context only covers TODAY's BUY rows
            # (pipeline fetches morning trades with today_only=True), so for
            # any position opened on a prior day the stop/target/thesis lines
            # silently vanished — while the Metrics line still rendered
            # to_stop/to_target derived from the very same data. Back-compute
            # the levels from the position_facts distance metrics (which
            # prefer the LIVE broker stop over the stale BUY row) so the
            # reviewer always sees the actual protection levels it is asked
            # to reason about (new_stop >= old_stop*1.02 rule).
            if not sl and pf.get("distance_to_stop_pct") is not None and p.current_price > 0:
                # Inverse of `TradingPipeline._build_position_facts`'s
                # `dist_stop_pct` formula, which is side-mirrored since
                # 2026-09-18: a LONG's stop sits BELOW price
                # (`sl = cur * (1 - dist/100)`), a SHORT's sits ABOVE
                # (`sl = cur * (1 + dist/100)`). Using the long-only inverse
                # for a short back-computed a stop on the wrong side of
                # price entirely.
                _dist = pf["distance_to_stop_pct"] / 100
                sl = p.current_price * ((1 + _dist) if p.qty < 0 else (1 - _dist))
            if not tp and pf.get("distance_to_target_pct") is not None and p.current_price > 0:
                tp = p.current_price * (1 + pf["distance_to_target_pct"] / 100)
            if sl:
                lines.append(f"  Hard stop (broker): ${sl:.2f}")
            if tp:
                lines.append(
                    f"  Reference target: ${tp:.2f} (a recorded expectation — nothing exits, caps or trails on it)"
                )
            else:
                # No take-profit on record (owner rule 2026-10-09: a chart
                # with no level trades with none). Said as ABSENT so the
                # reviewer never reads a missing number as $0.00.
                lines.append(
                    "  Reference target: absent (no structural level was found — "
                    "a trend trade managed by its stop; no number is invented)"
                )
            entry_reasoning = (ctx.get("reasoning") or "").strip() or (entry_row.get("reasoning") or "").strip()
            if entry_reasoning:
                lines.append(f"  Entry thesis: {entry_reasoning[:220]}")
            else:
                # audit round 2 #4: make the absence explicit — a missing
                # thesis line must read as "data unavailable", not "this
                # position has no thesis / no thesis_invalid_if condition".
                #
                # Board item 89 defect 3: this branch used to fire for EVERY
                # position held overnight, because the only lookup feeding
                # it was date-bounded to today. With `entry_context` above
                # it now fires only when the entry row genuinely carries no
                # reasoning, so the old "opened before today" explanation
                # would be a false one and is gone.
                lines.append(
                    "  Entry thesis: (not recorded on this position's entry "
                    "row; judge integrity via Metrics, tech trail, and "
                    "memory sections, do not invent one)"
                )
            metric_bits: list[str] = []
            if pf.get("days_held") is not None:
                metric_bits.append(f"days_held={pf['days_held']} (calendar)")
            if pf.get("sessions_held") is not None:
                metric_bits.append(f"sessions_held={pf['sessions_held']} (trading)")
            if pf.get("thesis_progress_pct") is not None:
                metric_bits.append(f"thesis_progress={pf['thesis_progress_pct']:.0f}%")
            # R-multiple (audit §1.4) — profit in units of the risk originally
            # taken, against the stop the position was OPENED with. Answers
            # "is this working?" in the unit a trader actually judges it in;
            # thesis_progress answers a different question and does not
            # normalise for how much was risked. Omitted when the entry stop
            # was not below entry, in which case no R was ever defined.
            if pf.get("r_multiple") is not None:
                metric_bits.append(f"R={pf['r_multiple']:+.2f}")
            if pf.get("risk_released"):
                metric_bits.append("risk_released (stop at/above entry)")
            # Phase 3.1 — pace is measured against the horizon the analyst
            # pinned AT ENTRY, and is deliberately absent more often than it
            # used to be. When it is absent, say WHY: a missing number that
            # reads as "nothing to see" is how a healthy day-2 position got
            # called stalled.
            if pf.get("pace") is not None:
                metric_bits.append(f"pace={pf['pace']:.2f}×")
            else:
                _status = pf.get("pace_status")
                if _status == "n/a_breakout":
                    metric_bits.append(
                        "pace=n/a (breakout setup — progress/pace disabled by "
                        "design; manage by trailing, not by schedule)"
                    )
                elif _status == "unavailable_no_pinned_horizon":
                    metric_bits.append(
                        "pace=unavailable (no horizon pinned at entry — do not "
                        "infer one; judge this position on thesis and structure)"
                    )
            if pf.get("expected_horizon_sessions") is not None:
                metric_bits.append(f"horizon={pf['expected_horizon_sessions']}sessions (pinned at entry)")
            if pf.get("setup_type"):
                metric_bits.append(f"setup={pf['setup_type']}")
            if pf.get("distance_to_stop_pct") is not None:
                metric_bits.append(f"to_stop={pf['distance_to_stop_pct']:.1f}%")
            if pf.get("distance_to_target_pct") is not None:
                metric_bits.append(f"to_target={pf['distance_to_target_pct']:.1f}%")
            # Vol units. The prompt tells the reviewer to reason in ATRs ("a
            # stop closer than NOISE_BAND_ATR_MULTIPLE ATRs, rendered into the
            # sheet, is inside daily noise") and the pipeline
            # pays for an ATR fetch per position to compute these — but they
            # were never rendered, so the instruction referred to data the
            # model could not see (2026-07-16 audit; drift introduced with the
            # exit-quality work). None => omit: unknown must not read as zero.
            if pf.get("atr_pct") is not None:
                metric_bits.append(f"atr={pf['atr_pct']:.2f}%")
            if pf.get("stop_distance_atrs") is not None:
                metric_bits.append(f"stop_distance={pf['stop_distance_atrs']:.2f}×ATR")
            if pf.get("weight_pct") is not None:
                metric_bits.append(f"weight={pf['weight_pct']:.1f}%")
            if metric_bits:
                lines.append(f"  Metrics: {' | '.join(metric_bits)}")

            flag_bits: list[str] = []
            if pf.get("parabolic_flag"):
                flag_bits.append("⚠️ PARABOLIC (+15% in <3d, momentum confirmation needed)")
            if flag_bits:
                lines.append(f"  Flags: {' '.join(flag_bits)}")

            positions_lines.append("\n".join(lines))
        positions_text = "\n".join(positions_lines) if positions_lines else "No open positions."

        # Macro block — full regime, not just raw numbers.
        vix = macro_summary.get("vix", {}) or {}
        hy = macro_summary.get("credit_spread", {}) or {}
        infl = macro_summary.get("inflation", {}) or {}
        if macro_analysis:
            regime = macro_analysis.get("regime", "N/A")
            outlook = macro_analysis.get("equity_outlook", "N/A")
            confidence = macro_analysis.get("confidence", "N/A")
            # No macro invested target: owner mandate 2026-09-17, the desk
            # stays fully invested and macro informs direction only.
            macro_regime_line = f"Regime: **{regime}** | Outlook: **{outlook}** ({confidence})"
        else:
            macro_regime_line = "Regime: (no macro analysis this session)"

        # News block.
        if news_intel:
            conv_order = {"high": 0, "medium": 1, "low": 2}
            state_lines = []
            for c in news_intel.state_changes[:5]:
                state_lines.append(
                    f"- [{c.conviction.upper()}] {c.event}: {c.previous_state} → {c.new_state} "
                    f"(impact: {c.market_impact}; affects: {', '.join(c.affected_symbols[:5]) or 'broad'})"
                )
            state_text = "\n".join(state_lines) or "No significant session state changes."
            held_syms = {p.symbol for p in positions}
            stock_lines = []
            for sym, alerts in (news_intel.stock_news or {}).items():
                if sym not in held_syms:
                    continue
                for a in sorted(alerts, key=lambda x: conv_order.get(x.conviction, 9))[:2]:
                    stock_lines.append(f"- {sym}: [{a.conviction.upper()}] {a.sentiment} — {a.impact_summary}")
            stock_text = "\n".join(stock_lines) or "No per-position news alerts."
            lost_text = news_intel.format_dropped_symbols_block()
            news_section = (
                f"### Session News Intelligence\n"
                f"PM Briefing: {news_intel.pm_briefing[:300]}\n\n"
                f"State changes this session:\n{state_text}\n\n"
                f"Held-position alerts:\n{stock_text}{lost_text}\n\n"
                f"Overall sentiment: {news_intel.format_market_sentiment()} ({news_intel.confidence})\n"
            )
        else:
            news_section = "### Session News\n(no news report available)\n"

        # Earnings — full content, not just queued list.
        held_syms = {p.symbol for p in positions}
        earnings_lines: list[str] = []
        queued: list[str] = []
        for ea in earnings_analyses:
            sym = ea.get("symbol")
            if not sym or sym not in held_syms:
                continue
            if ea.get("queued"):
                queued.append(sym)
                continue
            analysis = ea.get("analysis") or {}
            impl = analysis.get("investment_implications") or {}
            sentiment = impl.get("sentiment", "?")
            conv = impl.get("conviction", "?")
            key_thesis = (impl.get("key_thesis") or "").strip()[:180]
            earnings_lines.append(f"- {sym} [{sentiment} / {conv}]: {key_thesis}")
        earnings_parts: list[str] = []
        if earnings_lines:
            earnings_parts.append("### Earnings Analyses (held positions)\n" + "\n".join(earnings_lines))
        if queued:
            earnings_parts.append(
                f"### Just-filed (analysis queued — treat as elevated event risk): {', '.join(queued)}"
            )
        earnings_section = "\n\n".join(earnings_parts) if earnings_parts else ""

        # Memory layers (reuse PM-style prose).
        def _opt_section(title: str, body: str) -> str:
            body = (body or "").strip()
            return f"### {title}\n{body}\n" if body else ""

        narrative_section = _opt_section("Portfolio Narrative (last 7 evenings)", weekly_narrative)
        trajectory_section = _opt_section("Macro Regime Trajectory (7 days)", macro_trajectory)
        active_changes_section = _opt_section("Active HIGH-conviction State Changes (14 days)", active_state_changes)
        calibration_section = _opt_section("Trade Calibration (45-day realized)", calibration_note)
        decisions_section = _opt_section(
            "Your Recent Decisions (don't flip-flop without a named trigger)",
            own_recent_decisions,
        )

        if yesterday_insights:
            yi_outlook = (yesterday_insights.get("tomorrow_outlook") or "").strip()[:300]
            yi_bias = yesterday_insights.get("tomorrow_bias", "neutral")
            yi_conviction = yesterday_insights.get("tomorrow_conviction", "medium")
            yi_risk = yesterday_insights.get("risk_rating", "moderate")
            # v2: surface lessons + sell_decisions_assessment (prose) so the
            # reviewer sees what evening learned last night, not just the
            # forward-looking bias. PM already reads these; position reviewer
            # was reading a narrower slice of yesterday_insights.
            yi_lessons = (yesterday_insights.get("lessons") or "").strip()[:280]
            yi_sell_prose = (yesterday_insights.get("sell_decisions_assessment") or "").strip()[:280]
            lines = [
                "### Yesterday Evening's Outlook for Today",
                f"- Bias: {yi_bias} ({yi_conviction}) | Risk: {yi_risk}",
                f"- Outlook: {yi_outlook}",
            ]
            if yi_lessons:
                lines.append(f"- Lessons: {yi_lessons}")
            if yi_sell_prose:
                lines.append(f"- SELL discipline note: {yi_sell_prose}")
            yesterday_section = "\n".join(lines) + "\n"
        else:
            yesterday_section = ""

        # v2: recent SELL/BUY calibration counts from evening's structured
        # grades over the last 14 days. When premature + wrong >= correct,
        # tilt toward patience — you've been cutting winners too early.
        grade_section = ""
        # audit round 2 #36: gate on sells OR buys — a quiet fortnight with
        # entries but no exits (n_sells=0, n_buys>0) used to suppress the
        # whole section, hiding BUY-grade calibration ("wrong" = thesis
        # broken) that is material to hold-vs-sell judgment.
        if trade_grade_summary and (
            trade_grade_summary.get("n_sells", 0) > 0 or trade_grade_summary.get("n_buys", 0) > 0
        ):
            sc = trade_grade_summary.get("sell_counts") or {}
            bc = trade_grade_summary.get("buy_counts") or {}
            n_sells = trade_grade_summary.get("n_sells", 0)
            n_buys = trade_grade_summary.get("n_buys", 0)
            premature = sc.get("premature", 0)
            wrong = sc.get("wrong", 0)
            correct = sc.get("correct", 0)
            miss_rate = ((premature + wrong) / n_sells * 100) if n_sells else 0
            tilt_note = ""
            if miss_rate >= 50:
                tilt_note = (
                    "⚠️ SELL miss rate ≥ 50% — you've been cutting winners too "
                    "early. Lean PATIENT today; require a clearly-named thesis "
                    "trigger before any SELL."
                )
            elif miss_rate >= 33:
                tilt_note = (
                    "SELL miss rate elevated — raise the bar on today's SELL triggers; no price-action-only exits."
                )
            repeat_premature = trade_grade_summary.get("repeat_premature_symbols") or []
            repeat_wrong = trade_grade_summary.get("repeat_wrong_symbols") or []
            symbol_warn = ""
            if repeat_premature:
                symbol_warn += (
                    f"\n- Symbols you've marked premature ≥ 2× (be extra patient on these): "
                    f"{', '.join(repeat_premature)}"
                )
            if repeat_wrong:
                symbol_warn += (
                    f"\n- Symbols marked wrong ≥ 2× (thesis repeatedly defended in hindsight): "
                    f"{', '.join(repeat_wrong)}"
                )
            grade_section = (
                f"### Recent Trade Calibration from Evening (last 14 days)\n"
                f"SELLs graded: {n_sells} (correct {correct} / premature {premature} "
                f"/ wrong {wrong})\n"
                f"BUYs graded: {n_buys} "
                f"(correct {bc.get('correct', 0)} / premature {bc.get('premature', 0)} "
                f"/ wrong {bc.get('wrong', 0)})"
                f"{symbol_warn}\n"
                f"{tilt_note}\n"
            ).rstrip() + "\n"

        # Deterministic post-exit reality (computed from trades × live
        # prices, NO self-assessment): what the tape did after our recent
        # exits. Rendered even when nightly grades are absent — the
        # 2026-07-16 audit found grades said 97% "correct" while 53% of
        # exits ran ≥5% higher within 20 days.
        reality = trade_grade_summary.get("post_exit_reality") if trade_grade_summary else None
        if reality and reality.get("n"):
            worst_lines = "; ".join(
                f"{w['symbol']} exited {w['date']} → {w['move_pct']:+.1f}% since" for w in (reality.get("worst") or [])
            )
            frac = reality["n_higher_5pct"] / reality["n"]
            reality_note = ""
            if frac >= 0.5:
                reality_note = (
                    "⚠️ Over HALF of recent exits ran ≥5% after you sold — the "
                    "tape says your exits fire too early. Any SELL/tighten today "
                    "must name a hard trigger, not price action."
                )
            grade_section += (
                f"### Post-exit reality check (deterministic, from price data)\n"
                f"Exits in window: {reality['n']}; ran ≥5% higher after exit: "
                f"{reality['n_higher_5pct']}; avg move since exit: "
                f"{reality['avg_move_pct']:+.1f}%\n"
                f"Biggest post-exit runs: {worst_lines}\n"
                f"{reality_note}\n"
            ).rstrip() + "\n"

        if recent_performance:
            # A missing rolling return renders as "not provided", never as
            # "None%" and never as 0. The PM's equivalent block already warns
            # its seat not to read a missing value as zero; this one rendered
            # the literal string "None%", which reads as a number to a model.
            def _pct(value) -> str:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return f"{float(value):+.2f}%"
                return "not provided"

            r5 = _pct(recent_performance.get("rolling_5d_pct"))
            r20 = _pct(recent_performance.get("rolling_20d_pct"))
            perf_section = f"### Recent System Performance\n- 5d: {r5} | 20d: {r20}\n"
        else:
            perf_section = ""

        # Account + cash.
        cash_pct = f"{cash_balance / total_value * 100:.1f}%" if total_value else "N/A"
        reserve_line = (
            f"\n  (a further ${reserve_balance:,.2f} is sweep-parked; it "
            f"is NOT sold to fund a BUY and is NOT part of the Cash above)"
            if reserve_balance > 0
            else ""
        )

        # Substantiation re-ask (2026-09-18). Non-empty only on the ONE
        # re-ask `TradingPipeline._substantiate_exit_triggers` may make in
        # a session, and it names the symbols whose exit trigger came back
        # unsubstantiated. Empty string on every first call, so the prompt
        # this seat sees normally is byte-for-byte unchanged.
        substantiation_challenge: str = kwargs.get("substantiation_challenge") or ""
        substantiation_section = f"### ⚠️ {substantiation_challenge}\n" if substantiation_challenge else ""

        # Margin mandate (carried over from v2 — sub-dollar threshold).
        allow_margin: bool = bool(kwargs.get("allow_margin", True))
        from src.risk.constants import MARGIN_DEFICIT_FLOOR_USD

        if not allow_margin and cash_balance < -MARGIN_DEFICIT_FLOOR_USD:
            deficit = -cash_balance
            margin_section = (
                f"### ⚠️ Cash-only policy — de-lever required\n"
                f"Cash is ${cash_balance:,.2f} (deficit ${deficit:,.2f}). "
                f"This account runs cash-only; prefer SELL on the "
                f"weakest-conviction position(s) to restore cash ≥ 0. Do NOT "
                f"TRAIL_STOP when the real problem is over-leverage.\n"
            )
        elif not allow_margin:
            margin_section = ""
        else:
            # Margin is enabled. Mirrors `PortfolioManagerAgent.build_user_
            # message` (same 2026-09-17 CRM-incident fix): never let this
            # seat assume cash is the spending limit either — it decides
            # whether to hold or cut existing positions twice a day on
            # exactly the same false "no margin" premise the PM prompt had.
            # `margin_headroom_usd` / `margin_ladder_multiple` /
            # `margin_ladder_rung` are threaded in from the SAME §11.2
            # computation execution's submit loop uses
            # (`_entry_deployment_budget` / `_session_gross_ceiling` in
            # `src/pipeline_stages.py`) — never a second formula.
            # `margin_ladder_backed=False` means that computation could not
            # resolve this session, and the section says so rather than
            # guessing a figure.
            headroom_usd = kwargs.get("margin_headroom_usd")
            ladder_multiple = kwargs.get("margin_ladder_multiple")
            ladder_rung = kwargs.get("margin_ladder_rung")
            ladder_backed = bool(kwargs.get("margin_ladder_backed", False))
            if ladder_backed and isinstance(headroom_usd, (int, float)) and isinstance(ladder_multiple, (int, float)):
                margin_section = (
                    "### Margin Capacity (margin is ENABLED)\n"
                    f"This account may run gross exposure up to "
                    f"{ladder_multiple:.2f}x equity (the §11.2 de-levering "
                    f"ladder's current ceiling, rung {ladder_rung}) — NOT "
                    f"just up to available cash.\n"
                    f"Ladder headroom remaining this session: "
                    f"${headroom_usd:,.2f}. This is the same figure "
                    f"execution sizes new entries against — not a separate "
                    f"estimate.\n"
                )
            else:
                margin_section = (
                    "### Margin Policy\n"
                    "Margin is ENABLED for this account, but this session's "
                    "ladder headroom could not be resolved for this prompt. "
                    "Do NOT read the Cash figure below as the spending "
                    "limit — treat the ladder as unknown, not as zero.\n"
                )

        if system_action_lines:
            system_actions_section = (
                "### Non-LLM System Actions Earlier Today\n"
                "These sells were triggered by a deterministic safety net, or by the "
                "broker's own protective stop firing (STOP_OUT — the market closed the "
                "position, not a decision anyone made), and bypassed LLM review — the "
                "listed symbols are already closed out of the book. Context-only; do "
                "not try to re-open, re-stop, or second-guess:\n" + "\n".join(system_action_lines) + "\n"
            )
        else:
            system_actions_section = ""

        # Same-day trim discipline section. Renders only when at least one
        # symbol was already sold earlier today (a midday exit, or a
        # deterministic de-lever). The Python executor
        # enforces this rule independently — this section is the prompt-side
        # belt so the LLM isn't fighting an invisible filter.
        if already_trimmed_today or spent_triggers_block:
            trimmed_line = (
                f"Symbols sold earlier today: {', '.join(sorted(already_trimmed_today))}\n"
                if already_trimmed_today
                else ""
            )
            already_trimmed_section = (
                "### ⚠️ Already Trimmed Today — DO NOT SELL again\n"
                + trimmed_line
                + "These positions ALREADY received a sell-side action this session day "
                "(a midday REDUCE, or a deterministic de-lever).\n"
                "**HOLD them at this session unless a HARD trigger fires that the "
                "desk has NOT already acted on today:**\n"
                "  - Named `thesis_invalid_if` condition is satisfied (price closed below "
                "cited level, fundamental signal flipped, etc.)\n"
                "  - HIGH-conviction bearish stock-specific state_change reversal landed today\n"
                "  - Bearish earnings filing analysis posted today for this symbol\n"
                "\n" + spent_triggers_block + "\n"
                "`TARGET_BREACH`, slowing pace, geopolitical noise, valuation stretch, "
                "concentration drift — these are NOT hard triggers. The earlier action "
                "already harvested them. Trimming a second time on the same flag is the "
                "mechanical loop that produced the 2026-05-04 AMZN incident "
                "(41 → 21 → 11 shares in one day on a strengthening thesis).\n"
                "TRAIL_STOP is exempt from THIS gate (it adjusts protection, "
                "doesn't sell shares) — but its own ratchet-cooldown / "
                "ATR-noise-band clamps still apply without a hard trigger.\n"
            )
        else:
            already_trimmed_section = ""

        session_label = _SESSION_LABEL.get(session_type, session_type)
        session_bias = _SESSION_DISPOSITION.get(session_type, "")

        # Phase 3.2 — what THIS SEAT measured last time, and which way each
        # number moved since. Rendered right above the reasoning instructions
        # because it is the direct antidote to the failure it exists for:
        # calling a position stalled when its own numbers improved.
        if metric_deltas:
            delta_lines = [deltas.render() for _, deltas in sorted(metric_deltas.items())]
            contradicted = sorted(symbol for symbol, deltas in metric_deltas.items() if deltas.net_improved)
            warn = ""
            if contradicted:
                warn = (
                    "\n- ⚠️ IMPROVED SINCE YOUR LAST REVIEW: "
                    + ", ".join(contradicted)
                    + ". A SELL or REDUCE on one of these citing stalling, "
                    "lack of progress, fading momentum or deterioration will "
                    "be VETOED by deterministic code — you would be "
                    "contradicting your own recorded numbers. Exit on NEW "
                    "INFORMATION (news, earnings, regime shift, correlation "
                    "breach, triggered thesis_invalid_if) is unaffected and "
                    "remains entirely your call."
                )
            deltas_section = (
                "### Your Own Previous Review (metric deltas)\n"
                "These are the numbers YOU recorded last session and how they "
                "have moved. Read them before judging whether anything is "
                "actually deteriorating.\n" + "\n".join(delta_lines) + warn + "\n"
            )
        else:
            deltas_section = (
                "### Your Own Previous Review\n"
                "- No prior snapshot on record for these positions (first look, "
                "or memory unavailable this run). Judge on thesis and structure; "
                "do not assume a trajectory you have not measured.\n"
            )

        # 2026-09-17 fix (mirrors PortfolioManagerAgent): this used to
        # hardcode "no margin" regardless of `allow_margin`. Cash is still
        # raw cash — real, can go negative — but it is not the spending
        # limit when margin is enabled, so the label must not claim the
        # account has none. See Margin Capacity / Margin Policy above.
        cash_status = (
            "deployable this session, no margin"
            if not allow_margin
            else "raw cash — see Margin Capacity above for what may still be spent"
        )
        return f"""## Position Review — {session_label}

{substantiation_section}
{margin_section}
{system_actions_section}
{already_trimmed_section}
### Session Disposition
{session_bias}

### Account
- Total Value: ${total_value:,.2f}
- Cash: ${cash_balance:,.2f} ({cash_pct}, {cash_status}){reserve_line}

### Open Positions
{positions_text}

### Macro
{macro_regime_line}
- VIX: {_fmt_or_na(vix.get("current"))} (trend: {_fmt_or_na(vix.get("trend"))})
- HY OAS: {_fmt_or_na(hy.get("current_bps"), "bps")} (30d Δ: {_fmt_or_na(hy.get("change_30d_bps"), "bps")})
- Core CPI YoY: {_fmt_or_na(infl.get("core_cpi_yoy"), "%")}

{trajectory_section}
{active_changes_section}
{news_section}
{earnings_section}

{narrative_section}
{yesterday_section}
{grade_section}
{calibration_section}
{decisions_section}
{deltas_section}
{perf_section}

Review each position. Fill the 6-step `reasoning_chain` before emitting
any action. Remember: intraday price is noise; thesis is signal; good
stocks are meant to be held. Respond as JSON matching the PositionReview
schema."""

    def review(
        self,
        positions: list[Position],
        macro_summary: dict,
        cash_balance: float,
        total_value: float,
        reserve_balance: float = 0.0,
        session_type: str = "midday",
        position_facts: dict | None = None,
        metric_deltas: dict | None = None,
        morning_trades: list[dict] | None = None,
        # Board item 89 defect 3 — {symbol: entry trade row} for
        # every held position, from the date-unrestricted lookup.
        # See `TradingPipeline.run_position_review`.
        entry_context: dict[str, dict] | None = None,
        news_intel: NewsIntelligenceReport | None = None,
        earnings_analyses: list[dict] | None = None,
        macro_analysis: dict | None = None,
        weekly_narrative: str = "",
        macro_trajectory: str = "",
        active_state_changes: str = "",
        calibration_note: str = "",
        own_recent_decisions: str = "",
        trade_grade_summary: dict | None = None,
        yesterday_insights: dict | None = None,
        recent_performance: dict | None = None,
        already_trimmed_today: set[str] | None = None,
        # Board item 74 — the triggers this seat has already spent
        # today, rendered by `src.risk.spent_trigger` so the prompt
        # and the executor's refusal read the same record.
        spent_triggers_block: str = "",
        allow_margin: bool = True,
        substantiation_challenge: str = "",
        # §11.2 ladder headroom, threaded from the SAME computation
        # execution's submit loop uses (`_entry_deployment_budget` /
        # `_session_gross_ceiling` in `src/pipeline_stages.py`) so
        # the Margin Capacity section never derives its own number.
        margin_headroom_usd: float | None = None,
        margin_ladder_backed: bool = False,
        margin_ladder_multiple: float | None = None,
        margin_ladder_rung: str | None = None,
    ) -> tuple[PositionReview | None, "AgentResult"]:
        result = self.run(
            positions=positions,
            macro_summary=macro_summary,
            cash_balance=cash_balance,
            reserve_balance=reserve_balance,
            total_value=total_value,
            session_type=session_type,
            position_facts=position_facts or {},
            metric_deltas=metric_deltas or {},
            morning_trades=morning_trades or [],
            entry_context=entry_context or {},
            news_intel=news_intel,
            earnings_analyses=earnings_analyses or [],
            macro_analysis=macro_analysis,
            weekly_narrative=weekly_narrative,
            macro_trajectory=macro_trajectory,
            active_state_changes=active_state_changes,
            calibration_note=calibration_note,
            own_recent_decisions=own_recent_decisions,
            trade_grade_summary=trade_grade_summary or {},
            yesterday_insights=yesterday_insights,
            recent_performance=recent_performance or {},
            already_trimmed_today=already_trimmed_today or set(),
            spent_triggers_block=spent_triggers_block,
            allow_margin=allow_margin,
            substantiation_challenge=substantiation_challenge,
            margin_headroom_usd=margin_headroom_usd,
            margin_ladder_backed=margin_ladder_backed,
            margin_ladder_multiple=margin_ladder_multiple,
            margin_ladder_rung=margin_ladder_rung,
        )
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Position reviewer returned non-JSON response")
            result.gate_reason = "review_non_json"  # item 188, recording only
            return None, result
        if not isinstance(parsed, dict):
            logger.error("Position reviewer expected object, got %s", type(parsed).__name__)
            result.gate_reason = "review_not_object"
            return None, result
        # Per-entry isolation: a single malformed PositionAction (e.g. a
        # TRAIL_STOP without new_stop_price) must not drop the WHOLE
        # review. The review carries reasoning_chain, overall_assessment,
        # risk_level, plus the OTHER actions — losing all of that because
        # one TRAIL_STOP forgot a price would silence midday/close
        # entirely on that session. Mirrors EveningAnalyst.
        # _drop_invalid_missed_opportunities (PR #73).
        parsed = self._drop_invalid_actions(parsed)
        try:
            review = PositionReview(**parsed)
        except ValidationError as e:
            logger.error("Position review failed schema validation: %s", e)
            result.gate_reason = "review_schema_validation_failure"
            return None, result
        return review, result

    @staticmethod
    def _drop_invalid_actions(parsed: dict) -> dict:
        """Pre-validate each PositionAction; drop malformed entries with a
        warning naming the symbol so operators can correlate against the
        position book.

        Mutates parsed in place for `actions`; non-list shapes normalize
        to []. The schema's `_trail_stop_requires_new_price` validator
        stays strict (that rule is critical — placing a TRAIL_STOP with
        no price would later crash broker.replace_stop_loss). What
        changes is *where* the strictness applies: per-action, not as
        an atomic whole-review check.
        """
        raw = parsed.get("actions")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Position reviewer: actions is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["actions"] = []
            return parsed
        valid: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "Position reviewer: dropping non-dict actions entry at index %d: %r",
                    i,
                    item,
                )
                continue
            try:
                PositionAction(**item)
            except ValidationError as e:
                sym = item.get("symbol") or f"<idx {i}>"
                logger.warning(
                    "Position reviewer: dropping malformed action for %s: %s",
                    sym,
                    e,
                )
                continue
            valid.append(item)
        parsed["actions"] = valid
        return parsed
