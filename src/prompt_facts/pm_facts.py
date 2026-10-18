"""src.prompt_facts.pm_facts -- the PM facts block and the conviction-outcome operator log.

Bodies moved verbatim from src/pipeline_prompt_facts.py (`PromptFactsMixin`), which keeps
same-named thin shims built per call. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no pipeline behind it.
"""

import logging

from src.pipeline_context import PMFacts
from src.risk.metrics import unrealized_pnl_pct
from src.risk.rules import position_weight_pct
from src.trading_calendar import session_date_key

#: The moved code logged under `src.pipeline` before the move and still does;
#: binding the name rather than `__name__` keeps log records byte-identical.
logger = logging.getLogger("src.pipeline")

#: Ceiling on how many symbols get a company-profile lookup for PM's facts
#: block. Profiles are 30-day-cached, so this only bites on a cold cache —
#: but on a cold cache it is one network round trip per symbol, and a
#: pathological candidate list must not be able to turn a nice-to-have
#: identity block into the longest step of the morning session.
_PM_PROFILE_SYMBOL_CAP = 40


class PromptPMFacts:
    """The PM facts block and the conviction-outcome operator log; standalone, built from explicit collaborators."""

    def __init__(
        self,
        *,
        db=None,
        config=None,
        parse_logged_agent_response=None,
        build_portfolio_heat=None,
        build_position_history=None,
        atr_for_symbol=None,
    ) -> None:
        self.db = db
        self.config = config
        self._parse_logged_agent_response = parse_logged_agent_response  # the host's parser, handed in
        self._build_portfolio_heat = build_portfolio_heat  # owned by PromptHeat; the host's shim is handed in
        self._build_position_history = build_position_history  # owned by PromptHistory; the host's shim is handed in
        self._atr_for_symbol = atr_for_symbol  # the host's ATR(14) reader, handed in

    def _build_pm_facts(
        self,
        *,
        positions: list,
        analyses: list,
        total_value: float,
        cash: float,
        recent_performance: dict,
        macro_analysis=None,
    ) -> PMFacts:
        """Quantitative snapshot surfaced to PM as structured fields.

        Phase 4 #4: reduces PM's reliance on LLM-summarized prose for the
        things that are actually numbers (win rate, sector weights, age
        buckets). Prose layers (weekly_narrative)
        stay for qualitative continuity.
        """
        import statistics
        from src.sector_reference import _get_sector as _sector_of

        f = PMFacts()

        # Calibration
        try:
            calib = self.db.compute_trade_calibration(lookback_days=30)
        except Exception as e:
            logger.warning("pm_facts: calibration failed: %s", e)
            calib = {}
        if calib:
            f.closed_trades_30d = int(calib.get("n") or 0)
            f.win_rate_30d_pct = calib.get("win_rate_pct")
            f.avg_return_30d_pct = calib.get("avg_return_pct")
            f.avg_hold_days_30d = calib.get("avg_hold_days")

        # RM discipline
        try:
            rm_rows = self.db.get_recent_agent_outputs(
                agent_name="risk_manager",
                limit=5,
                before_date=session_date_key(),
            )
        except Exception as e:
            logger.warning("pm_facts: rm outputs failed: %s", e)
            rm_rows = []
        f.rm_verdicts_seen = len(rm_rows)
        for row in rm_rows:
            data = self._parse_logged_agent_response(row)
            if not isinstance(data, dict):
                continue
            scale = data.get("scale_all_buys", 1.0)
            try:
                if float(scale) < 1.0:
                    f.rm_scale_downs_last5 += 1
            except (TypeError, ValueError):
                pass
            if data.get("modifications"):
                f.rm_mods_last5 += 1

        # Book state.
        #
        # `invested_pct` comes from `book_exposure` — the SAME function the
        # pre-trade gate's `deployment_gap` advisory reads, so PM
        # and RM can no longer be told opposite things about one book (they
        # were: 70% "10pp OVER" to PM and 10% "50pp UNDER" to RM on the same
        # $50k-long/$20k-SQQQ book). `positions` here is already sweep-split
        # by DecisionStage, and `cash` is `deployable_cash` (raw cash + the
        # parked vehicle), so the parked T-bills count as cash on both legs.
        #
        # `net_exposure_pct` is reported ALONGSIDE rather than substituted:
        # deployment answers "is the money at work", direction answers "which
        # way does the book lean", and one number cannot be both.
        from src.risk.rules import book_exposure

        if total_value > 0:
            exposure = book_exposure(positions, total_value)
            f.invested_pct = round(exposure.deployed_pct, 1)
            f.net_exposure_pct = round(exposure.net_pct, 1)
            f.cash_pct = round((cash or 0) / total_value * 100, 1)
        f.position_count = len(positions)

        # Sector weights — SEPARATE long and short budgets (spec §12.2).
        #
        # This REVERSES the netting that shipped with the shorts work: a held
        # short used to add a NEGATIVE weight, so a long 15% and a short 5% in
        # Technology rendered as a single 10% line. Owner's ratified reasoning:
        # *"A long and a short in the same sector is not a hedge... We are
        # trading opportunities."* Netting also showed the PM a smaller number
        # than `RiskRuleEngine.check` enforces against — the PM would reason
        # about concentration from one book while the gate refused on another.
        #
        # `sector_side_weights` is the shared definition the gate and the
        # constructor use, so all three cannot drift apart again. The only
        # thing local here is sector RESOLUTION: PM facts fall back to
        # `_sector_of` when the broker left `Position.sector` blank, and
        # "Unknown" is rendered rather than dropped so the PM can see that a
        # slice of the book is unclassified.
        from src.risk.rules import (
            SECTOR_SIDE_SHORT,
            sector_side_weights,
        )

        for (sector, side), weight in sector_side_weights(
            positions,
            total_value,
            resolve_sector=lambda p: p.sector or _sector_of(p.symbol) or "Unknown",
            include_unknown=True,
        ).items():
            bucket = f.sector_weights_short if side == SECTOR_SIDE_SHORT else f.sector_weights_long
            bucket[sector] = round(bucket.get(sector, 0.0) + weight, 1)

        # Age buckets + drift flag
        try:
            position_history = self._build_position_history(positions)
        except Exception:
            position_history = {}
        for p in positions:
            hist = position_history.get(p.symbol) or {}
            days = hist.get("days_held")
            if days is None:
                continue
            if days < 5:
                f.positions_under_5d += 1
            elif days <= 15:
                f.positions_5_to_15d += 1
            else:
                f.positions_over_15d += 1

        # Signal freshness
        ages = [a.signal_age_days for a in analyses if a.signal_age_days is not None]
        f.tech_signals_count = len(analyses)
        if ages:
            f.tech_signals_median_age_days = int(statistics.median(ages))
            f.tech_signals_stale_count = sum(1 for a in ages if a >= 8)

        # System perf
        f.rolling_5d_pct = recent_performance.get("rolling_5d_pct")
        f.rolling_20d_pct = recent_performance.get("rolling_20d_pct")

        # RC3: deployment gap vs the invested target as a hard fact in PM's
        # face. The target is the owner's fixed fully-invested mandate
        # (2026-09-17), not a macro output — macro no longer sets or lowers
        # it. Only rendered when there is a book to measure. The warning
        # threshold is the book's own one-session drift (ATR-weighted), not a
        # typed band; None means "drift unmeasured" and the warning is withheld.
        from src.risk.rules import DESK_INVESTED_TARGET_PCT, book_one_session_drift_pp

        if total_value > 0:
            f.invested_target_pct = DESK_INVESTED_TARGET_PCT
            f.deployment_gap_pp = round(
                f.invested_pct - DESK_INVESTED_TARGET_PCT,
                1,
            )
            f.deployment_drift_pp = book_one_session_drift_pp(
                positions, total_value, getattr(self, "_atr_for_symbol", None)
            )

        # Audit §1.3/§1.4 — the book's real risk, and each position's
        # R-multiple. None on failure; PMFacts.render() then says "unknown"
        # rather than implying the book is risk-free.
        f.heat = self._build_portfolio_heat(positions, total_value)
        # getattr-guarded for the tests that build TradingPipeline via
        # __new__() without __init__ (see tests/test_bounded_repeg.py) — same
        # convention as `_sweeper`.
        risk_cfg = getattr(getattr(self, "config", None), "risk", None)
        ceiling = getattr(risk_cfg, "max_portfolio_risk_pct", None)
        if isinstance(ceiling, (int, float)) and ceiling > 0:
            f.risk_ceiling_pct = float(ceiling)
        # Who these tickers actually are. PM has been reasoning about `CCJ`
        # and `PATH` as price series with a sector tag; "Energy" covers both
        # an integrated major and a pre-revenue nuclear startup, and a sector
        # label alone lets it reach for the wrong prior confidently. Scoped to
        # the symbols already in play for THIS decision (held + candidates) —
        # never the configured universe — and capped, because a cold cache
        # pays one network round trip per symbol and the morning session's
        # budget is not the place to discover a slow yfinance. Every failure
        # mode inside the store degrades to an identity-less profile, which
        # PMFacts.render() drops; this except is the belt to that suspenders.
        try:
            from src.data.company import CompanyProfileStore

            profile_symbols = sorted({p.symbol for p in positions if p.qty > 0} | {a.symbol for a in analyses})[
                :_PM_PROFILE_SYMBOL_CAP
            ]
            f.company_profiles = list(CompanyProfileStore().get_many(profile_symbols, allow_fetch=True).values())
        except Exception as e:  # noqa: BLE001 — identity is nice-to-have
            logger.warning("pm_facts: company profiles failed: %s", e)
            f.company_profiles = []

        return f

    @staticmethod
    def _log_conviction_outcome_for_operator(stats: dict) -> None:
        """Log the FULL by_conviction / by_allocated_risk breakdown for a
        human operator reading logs — including every bucket below
        `_CONVICTION_OUTCOME_MIN_N`, which `_build_calibration_note` never
        puts in front of an agent (see the "MOST IMPORTANT CONSTRAINT" note
        at its call site). This is the ONLY place that count is surfaced at
        all: recorded, not silently dropped, per spec §7.2 — "that must be
        discovered from data, not assumed" cuts both ways: assumed-absent
        is as wrong as assumed-present.
        """
        try:
            parts = []
            for grouping_key in ("by_conviction", "by_allocated_risk"):
                grouping = stats.get(grouping_key) or {}
                bucket_strs = []
                for label, s in grouping.items():
                    if not s:
                        continue
                    if s.get("insufficient_data"):
                        bucket_strs.append(f"{label}: n={s.get('n', 0)} (below floor)")
                    else:
                        bucket_strs.append(
                            f"{label}: n={s.get('n')} win={s.get('win_rate_pct')}% avg={s.get('avg_return_pct')}%"
                        )
                if bucket_strs:
                    parts.append(f"{grouping_key}=[{'; '.join(bucket_strs)}]")
            if not parts:
                return
            logger.info(
                "Conviction/risk-outcome calibration (OPERATOR-ONLY — never "
                "sent to any agent prompt below the sample floor): %s | "
                "conviction_unknown_n=%s allocated_risk_unknown_n=%s",
                " ".join(parts),
                stats.get("conviction_unknown_n"),
                stats.get("allocated_risk_unknown_n"),
            )
        except Exception as e:  # noqa: BLE001 — logging must never break calibration
            logger.warning("conviction_outcome operator log failed: %s", e)
