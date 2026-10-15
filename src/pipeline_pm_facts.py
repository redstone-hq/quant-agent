"""`PMFacts` — the structured quantitative snapshot surfaced to PM.

Moved verbatim out of `src/pipeline_context.py`, which was at its size
ceiling. Re-exported from there, so every existing import still works.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PMFacts:
    """Quantitative snapshot surfaced to PM as structured fields, not prose.

    Codex: 'Memory is LLM-summarizing-LLM — events, interpretations, and
    facts get mashed together in prose.' PMFacts carries pure numbers so
    PM can reference, compare, and reason against them directly instead
    of re-parsing prose that may have drifted.

    All values are POST-trade (i.e., current book state) unless tagged
    _pre_ (e.g., cash before executing). The snapshot is captured once
    at the top of DecisionStage and passed down — not recomputed.
    """

    # Calibration (realized outcomes)
    closed_trades_30d: int = 0
    win_rate_30d_pct: float | None = None
    avg_return_30d_pct: float | None = None
    avg_hold_days_30d: float | None = None

    # RM discipline (how often RM overrode PM lately)
    rm_verdicts_seen: int = 0
    rm_scale_downs_last5: int = 0  # count with scale_all_buys < 1.0
    rm_mods_last5: int = 0  # count with any modifications

    # Current book state
    invested_pct: float = 0.0
    #: Signed, leverage-aware net direction of the book, as a % of equity.
    #: NEGATIVE means net short. Deliberately separate from `invested_pct`:
    #: "is the money at work" and "which way does the book lean" are two
    #: questions and one number cannot answer both. Both come from the same
    #: `src.risk.rules.book_exposure` call, so they can never disagree about
    #: which positions they measured.
    net_exposure_pct: float = 0.0
    cash_pct: float = 100.0
    position_count: int = 0
    # Spec §12.2 (owner-ratified 2026-09-01) — SEPARATE long and short sector
    # budgets, each `{sector: % of equity}` as an UNSIGNED gross magnitude.
    #
    # This reverses the earlier, deliberate netting (a long 15% and a short
    # -5% in Technology used to render as one line, 10%). Owner's reasoning:
    # *"A long and a short in the same sector is not a hedge... We are
    # trading opportunities."* The PM must see the two sides separately or it
    # will reason about concentration differently from the engine that
    # enforces it — the same PM-sees-one-thing / gate-enforces-another defect
    # class as Phase 10.
    sector_weights_long: dict[str, float] = field(default_factory=dict)
    sector_weights_short: dict[str, float] = field(default_factory=dict)
    positions_under_5d: int = 0
    positions_5_to_15d: int = 0
    positions_over_15d: int = 0

    # Signal freshness (from TA output)
    tech_signals_count: int = 0
    tech_signals_median_age_days: int | None = None
    tech_signals_stale_count: int = 0  # age >= 8

    # System performance (existing; surfaced here as facts)
    rolling_5d_pct: float | None = None
    rolling_20d_pct: float | None = None

    # RC3 (2026-07-16): deployment vs the invested target. Macro demanded
    # 72-75% invested for three months while realized invested% averaged
    # 39% and NOTHING forced the gap into PM's face — every layer shaved
    # sizes independently and no one reconciled the compound. Since the
    # owner mandate of 2026-09-17 the target is the fixed fully-invested
    # `DESK_INVESTED_TARGET_PCT` (100%), no longer a macro output. None only
    # when there was no book to measure.
    invested_target_pct: float | None = None
    deployment_gap_pp: float | None = None  # invested - target (negative = under)
    # Tolerance band for the ⚠️ section below: the owner-set advisory
    # band (`deployment_gap.band_pct`), not an invented number — see
    # `src.risk.rules.deployment_gap_band_pct`. None only when the pipeline
    # never set it (e.g. a bare `PMFacts()` in a test); render() then falls
    # back to `DeploymentGapConfig`'s own declared default rather than a number
    # invented here.
    deployment_gap_band_pct: float | None = None

    # Phase 2 / audit §1.3-§1.4: the book's actual risk, computed in Python.
    # `heat` carries per-position at-risk dollars, open risk, the release flag
    # and R-multiples; `risk_ceiling_pct` is the owner-ratified total at-risk
    # ceiling the headroom is measured against. None when the heat build failed
    # — the prompt then says so rather than rendering a confident zero.
    heat: "PortfolioHeat | None" = None
    risk_ceiling_pct: float = 25.0

    # Who the tickers actually are. Every layer above this one reasons about
    # a symbol as a price series with a sector tag, and "Utilities" covers
    # both a regulated water utility and a merchant power trader with
    # commodity exposure — a label alone lets the PM reach for the wrong
    # prior with complete confidence. `CompanyProfile` objects for the
    # symbols in scope for THIS decision (held + candidates), never the whole
    # universe. Empty when the lookup failed or the cache was cold and the
    # fetch degraded — the section then renders as nothing at all, which is
    # the correct failure direction: a missing profile must never cost a
    # session, and a heading over "no profile available" lines is worse than
    # silence.
    company_profiles: list = field(default_factory=list)

    def render(self) -> str:
        """Format as a compact markdown block for PM's prompt."""

        def _pct(v: float | None) -> str:
            return f"{v:+.2f}%" if v is not None else "n/a"

        def _num(v: float | int | None) -> str:
            return f"{v}" if v is not None else "n/a"

        # Spec §12.2 — the two sides are rendered as two lists, never summed.
        # Netting them here would show the PM a smaller number than the gate
        # enforces against, which is precisely the defect being removed.
        def _sector_lines(weights: dict[str, float]) -> str:
            return (
                "\n".join(f"  - {s}: {w:.1f}%" for s, w in sorted(weights.items(), key=lambda kv: -kv[1])[:8])
                or "  (none)"
            )

        long_sector_lines = _sector_lines(self.sector_weights_long)
        short_sector_lines = _sector_lines(self.sector_weights_short)

        # audit round 2 #35: the denominator is rm_verdicts_seen (the query
        # is limit=5 but can return 0-5 rows), not a hardcoded 5 — a fresh
        # deployment with 2 verdicts, both overrides, used to render "2/5"
        # (40%) when the true override rate was 2/2 (100%). PM must cite
        # these numbers verbatim, so the block itself has to be honest.
        if self.rm_verdicts_seen > 0:
            rm_block = (
                f"### RM Discipline (last {self.rm_verdicts_seen} verdicts)\n"
                f"- scale_all_buys<1.0 count: {self.rm_scale_downs_last5}/{self.rm_verdicts_seen}"
                f" · mods emitted: {self.rm_mods_last5}/{self.rm_verdicts_seen}"
            )
        else:
            rm_block = "### RM Discipline\n- (no RM verdicts on record — cite as [UNSOURCED:no_rm_history])"

        return f"""### Calibration (last 30d closed trades)
- n={self.closed_trades_30d} · win_rate={_pct(self.win_rate_30d_pct)} · avg_return={_pct(self.avg_return_30d_pct)} \
· avg_hold={_num(self.avg_hold_days_30d)}d

{rm_block}

### Book State (current)
- invested={self.invested_pct:.1f}% (capital at work, unsigned) \
· net direction={self.net_exposure_pct:+.1f}% (leverage-aware; negative = net short) \
· cash={self.cash_pct:.1f}% · positions={self.position_count}
- age buckets: <5d={self.positions_under_5d} · 5-15d={self.positions_5_to_15d} · >15d={self.positions_over_15d}
- sector weights — LONG side (top 8, gross % of equity):
{long_sector_lines}
- sector weights — SHORT side (top 8, gross % of equity):
{short_sector_lines}
- (§12.2) the two sides carry SEPARATE budgets against the same sector
  limit and are NOT netted. A long and a short in the same sector is not a
  hedge — it is two opportunities that share a label.

### Signal Freshness (TA output this session)
- signals={self.tech_signals_count} · median_age={_num(self.tech_signals_median_age_days)}d \
· stale(≥8d)={self.tech_signals_stale_count}

### System Performance
- rolling 5d={_pct(self.rolling_5d_pct)} · 20d={_pct(self.rolling_20d_pct)}

{self._render_risk()}
{self._render_deployment_gap()}{self._render_companies()}"""

    def _render_risk(self) -> str:
        from src.risk.metrics import format_heat_block

        if self.heat is None:
            return (
                "### Portfolio Risk\n"
                "- not computed this run (stop data unavailable) — treat total "
                "at-risk as UNKNOWN, cite as [UNSOURCED:no_risk_data], and do "
                "not assume headroom exists."
            )
        return format_heat_block(self.heat, self.risk_ceiling_pct).rstrip("\n")

    def _render_companies(self) -> str:
        """Business identities for the symbols in scope, or nothing at all.

        Profiles that came back with no identifying field (fetch failed, cold
        cache with `allow_fetch=False`, a symbol yfinance does not know) are
        dropped before rendering rather than printed as "no company profile
        available" — a heading followed by a list of shrugs teaches the PM
        that the section is noise. If nothing survives the filter the whole
        section disappears.
        """
        try:
            from src.data.company import format_profiles_block

            known = [
                p
                for p in self.company_profiles
                if p is not None
                and any(
                    (
                        getattr(p, "name", None),
                        getattr(p, "summary", None),
                        getattr(p, "industry", None),
                    )
                )
            ]
            if not known:
                return ""
            block = format_profiles_block(
                known,
                title="Who These Companies Are",
            ).rstrip("\n")
        except Exception as e:  # noqa: BLE001 — never fail a render on prose
            logger.warning("pm_facts: company profile render failed: %s", e)
            return ""
        return f"\n\n{block}" if block else ""

    def _render_deployment_gap(self) -> str:
        if self.invested_target_pct is None or self.deployment_gap_pp is None:
            return ""
        # No OVER branch: there is no macro target left to be above, and a
        # book past 100% (margin) is governed by the enforced gross ceiling,
        # not by a prompt nudge to trim.
        band = self.deployment_gap_band_pct
        if band is None:
            from src.config import DeploymentGapConfig

            band = DeploymentGapConfig.model_fields["band_pct"].get_default()
        # 2026-09-18 fix: the not-under branch used to read
        # "invested=109.4% vs mandate=100% (gap +9pp)". On 2026-09-17 the PM
        # cited that line, alongside the (separately fixed) "no margin" cash
        # label, as a reason it could not add without selling protected
        # holdings — it read a deliberately levered book as over a limit.
        # The 100% is a FLOOR on cash deployment (the owner's mandate is
        # about cash drag: cash below inflation is a loss); it says nothing
        # about gross exposure, which has its own enforced ceiling. Neither
        # `DESK_INVESTED_TARGET_PCT` nor `max_gross_exposure_x` changes here
        # — only what the seat is told the 100 means.
        if self.deployment_gap_pp > 0:
            return (
                f"\n\n### Deployment vs Fully-Invested Mandate"
                f"\n- invested={self.invested_pct:.1f}%; the mandate is a FLOOR of "
                f"{self.invested_target_pct:.0f}%, not a ceiling."
                f" You are {self.deployment_gap_pp:+.0f}pp above the floor, which is"
                f" margin at work — that is NOT a breach and NOT a limit you are"
                f" over, so it is not by itself a reason to reduce anything."
                f"\n- The only ceiling on gross exposure is the enforced one in the"
                f" risk engine (the standing gross-exposure cap, tightened by the"
                f" de-levering ladder), and Python applies it after you submit."
                f" Judge adds on their own evidence, not against this line."
            )
        if self.deployment_gap_pp >= -band:
            return (
                f"\n\n### Deployment vs Fully-Invested Mandate"
                f"\n- invested={self.invested_pct:.1f}% against a floor of "
                f"{self.invested_target_pct:.0f}% (gap {self.deployment_gap_pp:+.0f}pp)."
                f" Any cash left undeployed is a cost — name why in `cash_target`."
            )
        return (
            f"\n\n### ⚠️ DEPLOYMENT GAP (address in cash_target step)"
            f"\n- invested={self.invested_pct:.1f}% vs fully-invested mandate="
            f"{self.invested_target_pct:.0f}% — you are {-self.deployment_gap_pp:.0f}pp UNDER the target"
            f"\n- This gap has been the single largest P&L drag (idle cash in a"
            f" rising market). In `cash_target`, either (a) close it with"
            f" qualified candidates THIS session, or (b) name the concrete"
            f" blocker per unfilled slot (no-qualified-setups after filters /"
            f' regime gate / earnings-queue). "Staying cautious" without a'
            f" named blocker is not an answer, and a bearish read is expressed"
            f" with shorts or inverse ETFs, not with cash."
        )
