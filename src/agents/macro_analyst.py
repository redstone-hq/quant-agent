import json
import logging
from pathlib import Path

from pydantic import ValidationError

from src.agents.base import BaseAgent, AgentResult
from src.models import MacroAnalysis, MacroObservation

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "macro_analyst.md"


class MacroAnalystAgent(BaseAgent):
    # See analyze(): parsed JSON is validated as MacroAnalysis(**parsed).
    result_model = MacroAnalysis

    @property
    def name(self) -> str:
        return "macro_analyst"

    @property
    def system_prompt(self) -> str:
        if PROMPT_PATH.exists():
            return PROMPT_PATH.read_text()
        return "You are a macro analyst. Respond with JSON."

    def build_user_message(self, **kwargs) -> str:
        macro_summary: dict = kwargs["macro_summary"]
        universe: list[str] = kwargs.get("universe", [])
        last_state: dict | None = kwargs.get("last_state")
        news_narrative: dict | None = kwargs.get("news_narrative")
        macro_coverage = kwargs.get("macro_coverage")

        vix = macro_summary.get("vix", {}) or {}
        treasury = macro_summary.get("treasury", {}) or {}
        fed = macro_summary.get("fed_funds_rate", {}) or {}
        infl = macro_summary.get("inflation", {}) or {}
        une = macro_summary.get("unemployment", {}) or {}
        hy = macro_summary.get("credit_spread", {}) or {}
        # Phase 4.2 additions (docs/AGENT_ROLE_AUDIT.md §2.3) — see the
        # "Guardrails" note below on how these are scoped relative to the
        # six PRIMARY confidence-calibration indicators above.
        real_rates = macro_summary.get("real_rates", {}) or {}
        dollar = macro_summary.get("dollar_index", {}) or {}
        ig = macro_summary.get("ig_credit_spread", {}) or {}
        claims = macro_summary.get("jobless_claims", {}) or {}

        def _stale(d: dict, monthly: bool = False, weekly: bool = False) -> str:
            """Freshness label for one indicator's heading.

            NOT a day count any more (2026-09-11). The previous version
            labelled an indicator stale past a per-cadence calendar
            threshold (>3 business days daily, >55 monthly, >10 weekly).
            Both the threshold and the idea were wrong: the reading FRED
            publishes IS the current reading whatever its age, so age
            measures the publication calendar, not data quality. The
            provider now states, per series, whether the held value is the
            latest published reading and whether a newer print is overdue
            (`src/data/macro.py::SeriesFreshness`); this renders that.

            Age is still shown, because "the current CPI print is 36 days
            old" is real, useful context for the seat's reasoning — it is
            just presented as the publication cadence it is, never as a
            defect. The `monthly` / `weekly` flags survive only to word
            that sentence; they no longer gate anything.
            """
            freshness = d.get("freshness")
            s = d.get("staleness_days")
            age = f", {s}d old" if isinstance(s, int) else ""
            cadence = "monthly" if monthly else "weekly" if weekly else "daily"
            if freshness == "overdue":
                detail = d.get("freshness_detail") or ""
                return (
                    f" (OVERDUE — a newer print is past due and has not "
                    f"arrived{'; ' + detail if detail else ''}. Treat the "
                    f"value below as a reading that should already have been "
                    f"superseded, not as current.)"
                )
            if freshness == "empty":
                return " (NO DATA returned this run — not a reading of any kind)"
            if freshness == "current":
                return f" (latest published reading{age}; normal {cadence} release cadence)"
            # None/unknown: freshness could not be established this run.
            return f" (latest reading we hold{age}; freshness UNVERIFIED — FRED release metadata unavailable this run)"

        universe_text = ", ".join(universe) if universe else "N/A"

        prior_state_section = "## Yesterday's Macro State\nNo prior state on file (first run)."
        if last_state:
            prior_state_section = f"""## Yesterday's Macro State (for shift detection)
- Date: {last_state.get("date", "N/A")}
- Regime: {last_state.get("regime", "N/A")}
- Confidence: {last_state.get("confidence", "N/A")}
- Equity outlook: {last_state.get("equity_outlook", "N/A")}
- Prior summary: {last_state.get("summary", "N/A")}"""

        news_section = "## Yesterday's News Narrative\nNot available."
        if news_narrative:
            tracker = news_narrative.get("key_state_tracker", {}) or {}
            tracker_text = "\n".join(f"  - {k}: {v}" for k, v in tracker.items()) or "  (empty)"
            news_section = f"""## Yesterday's News Narrative (cross-reference)
- Regime: {news_narrative.get("current_regime", "N/A")}
- Era themes: {"; ".join(news_narrative.get("era_themes", []) or []) or "N/A"}
- State tracker:
{tracker_text}"""

        coverage_section = "## Macro Data Coverage\nUNKNOWN (caller did not report FRED coverage). Treat with the same caution as a reported gap."
        if macro_coverage is not None:
            coverage_section = f"## Macro Data Coverage\n{macro_coverage.describe()}"

        return f"""{coverage_section}

## Current Macro Indicators

### VIX (CBOE Volatility Index){_stale(vix)}
- Current: {vix.get("current", "N/A")}
- 5-day Average: {vix.get("mean_5d", "N/A")}
- Trend: {vix.get("trend", "N/A")}

### Treasury Yields{_stale(treasury)}
- 3-Month: {treasury.get("us3mo", "N/A")}%
- 2-Year: {treasury.get("us2y", "N/A")}%
- 10-Year: {treasury.get("us10y", "N/A")}%
- 2Y-10Y Spread: {treasury.get("spread_2_10", "N/A")}%
- Inverted (2Y/10Y): {treasury.get("inverted", "N/A")}
- 3M-10Y Spread: {treasury.get("spread_3m_10y", "N/A")}%
- Inverted (3M/10Y): {treasury.get("inverted_3m_10y", "N/A")}

### Fed Funds Rate (DFF, daily){_stale(fed)}
- Current: {fed.get("current", "N/A")}%
- 30-day change: {fed.get("change_30d", "N/A")}

### Inflation{_stale(infl, monthly=True)}
- Headline CPI YoY: {infl.get("headline_cpi_yoy", "N/A")}% (MoM: {infl.get("headline_cpi_mom", "N/A")}%)
- Core CPI YoY: {infl.get("core_cpi_yoy", "N/A")}% (MoM: {infl.get("core_cpi_mom", "N/A")}%)
- PCE YoY: {infl.get("pce_yoy", "N/A")}%

### Real 10Y Yield & Breakeven Inflation (DFII10, T10YIE){_stale(real_rates)}
- Real 10Y Yield: {real_rates.get("real_10y", "N/A")}%
- 10Y Breakeven Inflation: {real_rates.get("breakeven_10y", "N/A")}%

### Unemployment (UNRATE){_stale(une, monthly=True)}
- Current: {une.get("current", "N/A")}%
- Change 3m: {une.get("change_3m", "N/A")}pp
- Change 12m: {une.get("change_12m", "N/A")}pp

### Initial Jobless Claims (ICSA, weekly){_stale(claims, weekly=True)}
- Current: {claims.get("current", "N/A")}
- 4-week change: {claims.get("change_4w", "N/A")}
- Trend: {claims.get("trend", "N/A")}

### HY Credit Spread (BAMLH0A0HYM2){_stale(hy)}
- Current: {hy.get("current_bps", "N/A")}bps
- 30-day change: {hy.get("change_30d_bps", "N/A")}bps

### IG Credit Spread (BAMLC0A0CM){_stale(ig)}
- Current: {ig.get("current_bps", "N/A")}bps
- 30-day change: {ig.get("change_30d_bps", "N/A")}bps

### Dollar Index (DTWEXBGS, Fed Broad Nominal){_stale(dollar, weekly=True)}
- Current: {dollar.get("current", "N/A")}
- 30-day change: {dollar.get("change_30d", "N/A")}

{prior_state_section}

{news_section}

## Trading Universe
{universe_text}

Walk through the 6-step reasoning chain, then emit the full JSON schema (including reasoning_chain, regime_shift, triggers, alignment_with_news)."""

    def analyze(
        self,
        macro_summary: dict,
        universe: list[str] | None = None,
        last_state: dict | None = None,
        news_narrative: dict | None = None,
        macro_coverage=None,
    ) -> tuple[MacroAnalysis | None, AgentResult]:
        """Run LLM, validate via Pydantic, return the typed object.

        Phase 4 #7: returns MacroAnalysis instead of dict. Consumers that
        need dict form (PM's rendering, macro_store serialization) call
        .model_dump() at their boundary.

        `macro_coverage` (src.data.macro.MacroCoverage, optional) is
        Phase 4.2: how many of the configured FRED series actually
        returned data this run. Threaded into the prompt's "Macro Data
        Coverage" section — mirrors news_analyst's `news_coverage` kwarg
        exactly. Optional/untyped here (rather than importing
        MacroCoverage) to avoid a src.agents -> src.data import for a
        value only ever used for its .describe() string.

        Scheduled economic releases and FOMC meetings are deliberately not
        an input (owner ruling 2026-10-18: they must not shape buy or risk
        decisions; stops cover event risk).
        """
        result = self.run(
            macro_summary=macro_summary,
            universe=universe or [],
            last_state=last_state,
            news_narrative=news_narrative,
            macro_coverage=macro_coverage,
        )
        parsed = result.parse_json()
        if parsed is None:
            logger.error("Macro analyst returned non-JSON response")
            return None, result
        if not isinstance(parsed, dict):
            logger.error("Macro analyst expected object, got %s", type(parsed).__name__)
            return None, result
        # Per-entry isolation for key_observations: a single malformed
        # MacroObservation (e.g. missing `interpretation` field) must not
        # drop the whole MacroAnalysis. The core fields PM relies on
        # (regime / position_guidance / sector_guidance / equity_outlook)
        # are typically clean even when one observation row is mangled.
        # Mirrors EveningAnalyst._drop_invalid_missed_opportunities (PR #73)
        # and the news_analyst / position_reviewer / meta_reflector pattern
        # (PR #74). sector_guidance is already protected by the existing
        # _sanitize_sector_guidance @model_validator on MacroAnalysis.
        parsed = self._drop_invalid_key_observations(parsed)
        from src.seat_heal import coerce_macro_shape

        parsed, _fixes = coerce_macro_shape(parsed)
        try:
            analysis = MacroAnalysis(**parsed)
        except ValidationError as e:
            if getattr(self, "_heal_retry_used", False):
                logger.error("Macro analysis failed validation after one heal retry: %s", e)
                return None, result
            # Mechanical coerce already ran. One paid retry, then durable
            # failure — never a loop, never invented reasoning_chain text.
            self._heal_retry_used = True
            logger.warning(
                "Macro analysis failed validation (%s); one paid heal retry",
                e,
            )
            try:
                return self.analyze(
                    macro_summary,
                    universe=universe,
                    last_state=last_state,
                    news_narrative=news_narrative,
                    macro_coverage=macro_coverage,
                )
            except Exception:
                logger.error("Macro heal retry raised; not resetting the one-retry flag")
                return None, result
        analysis = self._apply_sanity_checks(analysis, macro_summary)
        return analysis, result

    @staticmethod
    def _apply_sanity_checks(
        analysis: MacroAnalysis,
        macro_summary: dict,
    ) -> MacroAnalysis:
        """Soft Python-side floor for two `macro_analyst.md` discipline
        rules the LLM occasionally violates by self-inflating.

        Both rules used to be CALENDAR-DAY tests and both were wrong for
        macro data. Rule 2 required `staleness_days <= 1` on two of the six
        primary indicators; FRED's real publication lag on its daily series
        is about 2 business days, so the bar demanded a print that does not
        exist and the rule fired on 14 of 27 retained production runs (52%,
        2026-08-17..09-02) — the macro seat's regime call was being thrown
        away about half the time for being punctual. Rule 1 had the same
        shape at a looser number (>3 daily, >55 monthly). Full reasoning,
        the measurement, and what replaced the day count:
        `src/data/macro.py::SeriesFreshness`.

        The test now is the one that matches how macro data actually
        publishes: an indicator counts when it is the LATEST PUBLISHED
        reading for its series and no newer print is overdue. A CPI figure
        five weeks old is the current figure; a daily yield two days behind
        is the current yield. What does NOT count is an indicator that is
        missing, or one whose next print is past due by that series' own
        measured cadence and publication lag (a publication failure, a
        fetch failure, a shutdown) — that is real staleness and it still
        blocks, which is the half of the old gate worth keeping.

        1. `confidence == "high"` requires all six primary indicators to
           be present with a usable reading and none overdue. ("high" is
           the LLM's most-impactful confidence call; PM's Step 1
           evening-tilt scales sizing by it, so a self-inflated "high" on
           absent data leaks into position size.)

        2. `regime_shift == True` requires >= 2 primary indicators
           present and not overdue, per the prompt's "Regime-Shift
           Detection" rule. Below that, clear `regime_shift` and
           `shift_reason` — calling a flip when the data is missing is
           guessing, and PM treats `regime_shift=true` as a "size
           appropriately and name the flip" trigger.

        Logs a warning on each override so the operator can see WHICH
        side of the gate misbehaved (LLM ignored prompt rule vs the
        sanity check fired correctly).
        """
        primary_keys = (
            "vix",
            "treasury",
            "fed_funds_rate",
            "inflation",
            "unemployment",
            "credit_spread",
        )
        _USABLE = {"current", "unknown"}
        _BLOCKING = {"overdue", "empty"}

        # Per-indicator freshness state, as reported by the provider.
        #
        # A caller that predates the `freshness` field (an older test
        # double, a replayed checkpoint) is not assumed fresh and is not
        # assumed broken: `staleness_days is None` means "no data at all"
        # by that field's own long-standing contract (see
        # `MacroDataProvider._staleness_days`), so it maps to "empty"; any
        # real age maps to "unknown", which is usable but unverified. That
        # is the honest reading of a payload that cannot answer the
        # question, and it is what keeps this gate reachable instead of
        # recreating the defect in a new form.
        state: dict[str, str] = {}
        for key in primary_keys:
            d = macro_summary.get(key)
            if not isinstance(d, dict) or not d:
                state[key] = "empty"
                continue
            reported = d.get("freshness")
            if reported in _USABLE or reported in _BLOCKING:
                state[key] = str(reported)
                continue
            state[key] = "empty" if d.get("staleness_days") is None else "unknown"

        blocked = [k for k, v in state.items() if v in _BLOCKING]

        # Rule 1: high confidence requires every indicator present, and no
        # indicator whose next print is overdue.
        if analysis.confidence == "high" and blocked:
            logger.warning(
                "Macro sanity-check: LLM emitted confidence='high' but "
                "indicator(s) %s are missing or their next print is OVERDUE "
                "— downgrading to 'medium' per macro_analyst.md Confidence "
                "Calibration rule.",
                ", ".join(f"{k} ({state[k]})" for k in blocked),
            )
            analysis.confidence = "medium"

        # Rule 2: regime_shift requires >= 2 usable indicators.
        if analysis.regime_shift:
            usable = [k for k, v in state.items() if v in _USABLE]
            if len(usable) < 2:
                logger.warning(
                    "Macro sanity-check: LLM set regime_shift=True but only "
                    "%d indicator(s) carry a usable latest reading%s — "
                    "clearing regime_shift per macro_analyst.md Regime-Shift "
                    "Detection rule ('shift requires >= 2 indicators whose "
                    "latest published reading is in hand and not overdue'). "
                    "Blocked: %s.",
                    len(usable),
                    f" ({', '.join(usable)})" if usable else "",
                    ", ".join(f"{k}={state[k]}" for k in blocked) or "none",
                )
                analysis.regime_shift = False
                analysis.shift_reason = ""

        return analysis

    @staticmethod
    def _drop_invalid_key_observations(parsed: dict) -> dict:
        """Pre-validate each MacroObservation; drop malformed entries with a
        warning naming the indicator (or list index when missing).

        Mutates parsed in place for `key_observations`. Non-list shapes
        normalize to []. The schema's required-field discipline stays —
        we just stop letting one bad row weaponize that strictness against
        the rest of the analysis.
        """
        raw = parsed.get("key_observations")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Macro analyst: key_observations is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["key_observations"] = []
            return parsed
        valid: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "Macro analyst: dropping non-dict key_observations entry at index %d: %r",
                    i,
                    item,
                )
                continue
            try:
                MacroObservation(**item)
            except ValidationError as e:
                indicator = item.get("indicator") or f"<idx {i}>"
                logger.warning(
                    "Macro analyst: dropping malformed key_observation %r: %s",
                    indicator,
                    e,
                )
                continue
            valid.append(item)
        parsed["key_observations"] = valid
        return parsed
