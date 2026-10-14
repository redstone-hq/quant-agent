"""src.agents.portfolio_manager.evidence_prompting -- the standalone prompt-evidence piece.

Bodies moved verbatim from src/agents/portfolio_manager/prompt_evidence.py (PromptEvidenceMixin);
the mixin keeps same-named thin shims. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no agent behind it.
"""

from datetime import date
from src.models import (
    EarningsAnalysis,
    NewsIntelligenceReport,
    Position,
    SmartMoneyFinding,
    TechAnalysisResult,
    normalize_sector_stance,
)
from src.quantities import collapse_stances
from src.risk.rules import EARNINGS_STANCE_MAX_AGE_DAYS
from src.trading_calendar import et_today


def earnings_filing_is_stale(filing_date, asof: date | None = None) -> bool:
    """§9.4 freshness: an earnings stance whose filing is older than
    `EARNINGS_STANCE_MAX_AGE_DAYS`, or whose filing date is absent or
    unparseable, is stale (an unknowable age is not evidence of freshness).
    The ONE rule: the tally (`stale_evidence_sources`) and the shared seat
    view builder (buys and judgement sales alike) both call it."""
    try:
        return ((asof or et_today()) - date.fromisoformat(filing_date)).days > EARNINGS_STANCE_MAX_AGE_DAYS
    except (TypeError, ValueError):
        return True


class PromptEvidence:
    """Prompt evidence rows: stance collapsing, sector/macro/earnings rows, the evidence registry.

    Standalone: every collaborator is an explicit keyword-only constructor argument.
    Bodies moved verbatim from PromptEvidenceMixin (src/agents/portfolio_manager/prompt_evidence.py);
    the only mechanical changes are `cls` -> `self` (decorators dropped) and `self` inserted as the first
    parameter of each former staticmethod.
    """

    def __init__(
        self,
        *,
        collapse_stances=None,
        earnings_stance_rows=None,
        macro_sectors=None,
        macro_stance_rows=None,
        sector_guidance_rows=None,
    ) -> None:
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if collapse_stances is not None:
            self._collapse_stances = collapse_stances
        if earnings_stance_rows is not None:
            self._earnings_stance_rows = earnings_stance_rows
        if macro_sectors is not None:
            self._macro_sectors = macro_sectors
        if macro_stance_rows is not None:
            self._macro_stance_rows = macro_stance_rows
        if sector_guidance_rows is not None:
            self._sector_guidance_rows = sector_guidance_rows

    def _collapse_stances(self, values) -> str | None:
        """Thin wrapper — the reduction itself lives in `src.quantities.
        collapse_stances` now, so `src/models.py::news_verdict_for_symbol`
        (Phase 13) can share the exact same rule without this module
        importing that one (this module already imports `src.models`, so
        the reverse would be circular). See that function's docstring for
        the full rule; this wrapper exists only so every existing call site
        here (`build_evidence_registry`, `_earnings_stance_rows`, ...) keeps
        working unchanged.
        """
        return collapse_stances(values)

    def _sector_guidance_rows(self, raw) -> list[dict]:
        """`sector_guidance`, in either shape, as [{sector, stance, reason}].

        Two shapes reach the PM. The live macro agent emits
        [{sector, stance, reason}, ...] with stance ∈ overweight|neutral|
        underweight; `MacroStore` persists the normalized {sector: direction}
        form (see `macro_store._normalize_sector_guidance`, which drops the
        bulky reasons). Both arrive in normal operation now that an intraday
        tick carries the morning's STORED regime forward, so every reader of
        this field has to handle both — iterating the dict shape as though it
        were a list yields bare strings, and indexing those took the whole PM
        call down.

        Stances come out in ONE vocabulary, the bullish/neutral/bearish
        directions the rest of the system persists and grades against (see
        `SECTOR_STANCE_TO_DIRECTION`). Without that the same macro view
        reached the evidence registry as "overweight" in the morning and
        "bullish" at 14:00, purely by which session read it. Unrecognized
        stances are dropped rather than passed through: a stance no polarity
        set knows can only produce a grounding error the model cannot fix.
        """
        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list):
            pairs = [(row.get("sector"), row.get("stance")) for row in raw if isinstance(row, dict)]
        else:
            return []
        rows: list[dict] = []
        for sector, stance in pairs:
            direction = normalize_sector_stance(stance)
            if sector and direction:
                rows.append({"sector": str(sector), "stance": direction})
        return rows

    def _macro_sectors(
        self,
        positions: list[Position],
        symbol_sectors: dict[str, str] | None,
    ) -> dict[str, str]:
        """`{SYMBOL: sector}` from the caller's map, back-filled from the
        held positions' own `sector` field. Extracted so the registry and
        the uncounted-source gate below resolve a symbol's sector from
        exactly the same inputs in exactly the same order.
        """
        sectors = {str(k).upper(): str(v) for k, v in (symbol_sectors or {}).items()}
        for position in positions:
            if position.sector:
                sectors.setdefault(position.symbol.upper(), position.sector)
        return sectors

    def _macro_stance_rows(
        self,
        *,
        macro_analysis: dict | None,
        symbols: set[str],
        sectors: dict[str, str],
    ) -> list[tuple[str, str, bool]]:
        """`(SYMBOL, stance, is_broadcast)` for every symbol macro covers.

        ONE definition, two readers — `build_evidence_registry`, which puts
        the stance IN the registry, and `broadcast_macro_sources`, which
        decides whether that stance may CORROBORATE a trade in the §9.4
        agreement tally. A second walk over `sector_guidance` in the second reader is
        precisely how the two would drift about which symbols got the broad
        fallback, and the whole point of the gate is that they cannot.

        `is_broadcast` is True when this read stated NO stance for the
        symbol's own sector and the market-wide `equity_outlook` was
        back-filled instead. It is the registry-side twin of the same
        distinction `src/risk/rules.py::_is_broadcast_macro_verdict` draws
        on the Phase 13 verdict, where it is carried by the
        `sector_stance:<sector>` evidence label.
        """
        if not macro_analysis:
            return []
        guidance: dict[str, list[str]] = {}
        for row in self._sector_guidance_rows(macro_analysis.get("sector_guidance")):
            sector = str(row.get("sector") or "").strip().lower()
            stance = row.get("stance")
            if sector and stance:
                guidance.setdefault(sector, []).append(str(stance))
        broad = self._collapse_stances([macro_analysis.get("equity_outlook") or macro_analysis.get("regime")])
        rows: list[tuple[str, str, bool]] = []
        for symbol in symbols:
            sector = sectors.get(symbol, "").strip().lower()
            stance = self._collapse_stances(guidance.get(sector, [])) if sector else None
            resolved = stance or broad
            if resolved:
                rows.append((symbol, resolved, stance is None))
        return rows

    def _earnings_stance_rows(
        self,
        earnings_analyses: list[dict],
    ) -> list[tuple[str, str, str]]:
        """`(SYMBOL, stance, filing_date)` for every earnings entry that
        produces a registry stance, in input order.

        The filter is exactly the one `build_evidence_registry`'s `put`
        applies — a dict `analysis`, a non-empty collapsed sentiment, a
        non-empty symbol — extracted so the freshness gate below and the
        registry itself cannot drift apart about WHICH entry a symbol's
        earnings stance came from. Order is preserved because the registry
        is last-wins per symbol.

        `filing_date` is read from the pipeline wrapper first (the shape
        `run_earnings_preprocess` / `EarningsAnalystAgent.analyze_reports`
        emit) and from the validated analysis second. Empty string when
        neither carries one — an unknowable age, which the gate treats as
        stale.
        """
        rows: list[tuple[str, str, str]] = []
        for item in earnings_analyses:
            analysis = item.get("analysis")
            if not isinstance(analysis, dict):
                continue
            sentiment = (analysis.get("investment_implications") or {}).get("sentiment")
            stance = self._collapse_stances([sentiment])
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol or not stance:
                continue
            filing_date = str(item.get("filing_date") or analysis.get("filing_date") or "").strip()
            rows.append((symbol, stance, filing_date))
        return rows

    def _render_earnings_verdict(
        self,
        *,
        sym: str,
        analysis: dict,
        filing_label: str,
        source_note: str,
        analysis_path: str | None,
    ) -> str:
        """Item 18 (2026-09-04): earnings used to courier its whole eight-
        field extraction form into this prompt (~1,400 chars/filing, 70% of
        a 200k-char PM prompt across dozens of filings/day) for one line of
        actual judgement. This renders the SHORT verdict instead — call,
        conviction, a 2-3 sentence thesis, and a pointer to the full record.

        Reuses `EarningsAnalysis.to_verdict()` (Phase 13, `src/verdicts.py`
        ranking shape) rather than inventing a second short-form
        representation — see that method for why direction/conviction are
        verbatim and why the falsifier comes from bear_case/bull_case.

        `to_verdict()` (via `AnalystVerdict`'s own validator) REFUSES to
        construct a directional call with no stated invalidation — a real
        gap surfaced during item 18: PM's evidence needs are lower-stakes
        than the ranking machinery's, so a directional read with no
        disclosed falsifier still belongs in the PM prompt (marked as
        such) rather than being dropped from PM's picture entirely, which
        is what happened to a *ranking* candidate in this situation. Fall
        back to the raw `investment_implications` fields when that happens.
        """
        impl = (analysis or {}).get("investment_implications") or {}
        sentiment = impl.get("sentiment", "neutral")
        conviction = impl.get("conviction", "N/A")
        thesis = (impl.get("key_thesis") or "").strip() or "not disclosed"

        pointer = f"data/earnings/{sym}/... ({filing_label})" if not analysis_path else analysis_path

        verdict = None
        try:
            verdict = EarningsAnalysis.model_validate(analysis).to_verdict()
        except Exception:  # noqa: BLE001 — see docstring: fall back to the
            # raw fields rather than dropping this filing from PM's prompt.
            verdict = None

        if verdict is not None:
            falsifier_line = (
                f"- Invalidated if: {verdict.invalidation}"
                if verdict.invalidation
                else "- Invalidated if: not disclosed by the analyst"
            )
        else:
            falsifier = impl.get("bear_case") if sentiment == "bullish" else impl.get("bull_case")
            falsifier = (falsifier or "").strip()
            falsifier_line = (
                f"- Invalidated if: {falsifier}"
                if falsifier and falsifier.lower() != "not disclosed"
                else "- Invalidated if: not disclosed by the analyst"
            )

        return (
            f"### {sym} — {filing_label}{source_note}\n"
            f"- Call: {sentiment} ({conviction})\n"
            f"- Thesis: {thesis}\n"
            f"{falsifier_line}\n"
            f"- Full 8-field extraction (metrics, guidance, strategy, risks, "
            f"data quality): {pointer}"
        )

    def _render_earnings_no_call_rollup(self, rows: list[dict]) -> str:
        """One line per filing the earnings seat read WITHOUT reaching a call.

        PM TEST GATE item 7 (2026-09-13). Measured on the frozen
        `run_64290730` fixture rendered through this very method's caller:
        38 of the 65 analysed filings came back `sentiment: neutral`. A
        neutral earnings sentiment is not a quiet bearish read — it is the
        seat declining to conclude, and `EarningsAnalysis.to_verdict()`
        renders no invalidation for one, so the four-line verdict block for
        such a filing carries a direction the PM cannot trade, a thesis the
        seat did not write, and the literal words "not disclosed by the
        analyst". Those 38 blocks were 16,885 of the prompt's 100,968
        characters — 16.7% of everything the decision seat reads, saying
        nothing it can act on.

        WHAT THE CUT IS, AND WHY IT IS NOT A TRUNCATION LIMIT. There is no
        length threshold here and no cap on how many filings survive. The
        partition is structural and read from the data: a filing is rolled
        up if and only if its collapsed sentiment is non-directional. If
        every filing on a given day carries a call, this roll-up is empty
        and nothing is shortened.

        WHAT SURVIVES. Every rolled-up symbol is still named, on its own
        line, with its form, its filing date, its conviction, its cache
        provenance and its staleness marker — so coverage is never
        silently absent and a reader can always tell "read, no call" from
        "never read". The stance itself continues to reach the PM through
        the Canonical Evidence Registry and the Independent Source
        Agreement block unchanged: a neutral earnings seat still counts as
        a non-aligned source and still subtracts from the net score that
        ceilings size. Nothing about dissent or low conviction moves.
        """
        if not rows:
            return ""
        lines = [
            "### Read, no call — {n} filing(s)".format(n=len(rows)),
            "The earnings seat read each of these and did not reach a "
            "direction, so there is no thesis and no falsifier to show. They "
            'are listed rather than omitted so that "covered, concluded '
            'nothing" is never mistaken for "not covered". Each still '
            "carries a `neutral` earnings stance in the registry below and "
            "is counted there against any direction you propose.",
        ]
        for row in rows:
            lines.append(
                f"- {row['symbol']} | {row['filing_label']} | conviction {row['conviction']}{row['source_note']}"
            )
        return "\n".join(lines)

    def stale_evidence_sources(
        self,
        *,
        earnings_analyses: list[dict],
        asof: date | None = None,
    ) -> dict[str, frozenset[str]]:
        """`{SYMBOL: {"earnings"}}` for stances too old to earn size.

        §9.4 pays for agreement, and before this gate it paid the same for a
        view formed yesterday and one formed six months ago: the registry
        recorded only `investment_implications.sentiment` and dropped
        `filing_date` and `is_new` on the floor, so a cached bullish earnings
        stance was a full live corroborating source forever. Nothing in
        `src/risk/rules.py` or `src/portfolio_constructor.py` looked at age.
        That was reachable, not theoretical: when a symbol has no filing
        inside the provider's 45-day SEC scan window,
        `EarningsProvider._check_symbol` fell back to
        `_get_existing_analysis`, which re-served whatever was on disk with
        no age bound of its own (the store prunes at 1000 days).
        `_get_existing_analysis` now carries this same bound (2026-09-02,
        same constant, `src/data/earnings.py`), so that specific route to a
        stale stance is closed at the source — an over-age analysis is no
        longer handed to a session at all. This gate stays regardless: it is
        what actually governs the TALLY for a stance from ANY source, so a
        stale view that reaches the registry some other way is still caught
        here rather than relying on every producer to self-police age.

        Threshold: `EARNINGS_STANCE_MAX_AGE_DAYS` (90) — the number the
        earnings seat's own prompt and the missed-opportunity scan already
        use. See that constant for why it is reused rather than invented.

        A stale stance is REMOVED FROM THE TALLY ONLY. It stays in the
        canonical registry, so `validate_grounding` still recognises the
        coverage and a PM that cites it does not fail the session — this is
        a size reduction, not a new hard block. The prompt marks it so the
        PM cannot read it as corroborating.

        An absent or unparseable `filing_date` is treated as stale: an
        unknowable age is not evidence of freshness, and the same call is
        already made in `TradingPipeline._missed_ops_earnings_signal`.
        """
        today = asof or et_today()
        stale: dict[str, frozenset[str]] = {}
        for symbol, _stance, filing_date in self._earnings_stance_rows(earnings_analyses):
            is_stale = earnings_filing_is_stale(filing_date, today)
            # Last-wins, exactly as the registry resolves the stance itself:
            # a later entry for the same symbol replaces the verdict rather
            # than merging with it.
            if is_stale:
                stale[symbol] = frozenset({"earnings"})
            else:
                stale.pop(symbol, None)
        return stale

    def broadcast_macro_sources(
        self,
        *,
        registry: dict[str, dict[str, str]],
        positions: list[Position],
        macro_analysis: dict | None,
        symbol_sectors: dict[str, str] | None = None,
    ) -> dict[str, frozenset[str]]:
        """`{SYMBOL: {"macro"}}` for every symbol whose macro stance is the
        market-wide BROADCAST rather than a stance for its own sector.

        Fed to `signed_source_score(non_corroborating_sources=...)`, which is
        a ONE-SIDED removal: a broadcast stance may not CORROBORATE a trade,
        and its dissent still counts against one. NOT `ignored_sources`, the
        two-sided freshness removal — see "why one-sided" below, which is the
        load-bearing part of this docstring.

        Board item 109, owner ruling 2026-09-25. Macro is the only seat that
        back-fills a stance onto every name: when this read stated nothing
        about a symbol's sector, `build_evidence_registry` writes the broad
        `equity_outlook` into that symbol's registry slot. The §9.4 tally
        then reads it as one more INDEPENDENT per-name seat, so a single
        market-wide opinion is counted once per name — the same one data
        point, re-used as though several analysts had each looked at each
        name. That is the double count this removes.

        Macro is NOT removed and NOT muted. A SECTOR-SPECIFIC macro stance
        counts in the tally exactly as before, and a broadcast stance still
        reaches the candidate RANKING through the seat's own stated
        `confidence` (`MacroAnalysis.to_verdict` ->
        `src/verdicts.py::score_verdict`, at `SEAT_WEIGHT["macro"]`). Only
        its claim to be per-NAME agreement goes.

        **WHY ONE-SIDED, and why the first attempt at this was wrong.** The
        first version of this gate took the broadcast stance off BOTH counts,
        reasoning that a symmetric removal was what "sign-symmetric" meant.
        It is not, and it LOOSENED the entry gate — the exact opposite of
        what a double-count fix must do. Worked example, executed: technical
        bullish, earnings bullish, news bearish, macro broadcast bearish is
        2 aligned / 2 opposed, net 0, which `agreement_refuses_trade`
        REFUSES; drop the macro dissent and it is 2 aligned / 1 opposed,
        net +1, and the desk buys it. Nobody sanctioned that, and three
        comments in the same change still claimed gating a stance "can only
        ever shrink" the net while it no longer could.

        The owner's sign-symmetry ruling does not decide this, and saying it
        does is how the error got in. "It can be measured and weighted
        depending on if it's positive or negative. Would help on a long or a
        short" is symmetry between the two SIDES OF A TRADE — a long and a
        short must be treated alike. A one-sided removal satisfies that
        exactly: flip the reading's sign and flip the trade's direction and
        the answer is unchanged (broadcast-bullish on a short and
        broadcast-bearish on a long both score -1; broadcast-bullish on a
        long and broadcast-bearish on a short both score 0). What differs is
        SUPPORT versus OPPOSITION, which is a different axis entirely and one
        he did not rule on.

        So the axis he DID rule on is the one that decides it: a market-wide
        opinion must not manufacture agreement for a name nobody examined. It
        was never an argument for silencing a warning. Declining a trade on a
        broad bearish read costs an opportunity; taking one on a broad
        bullish read costs money, and only one of those is reversible.

        **This is deliberately the OPPOSITE carve-out from
        `src/risk/rules.py::_is_broadcast_macro_verdict`, which drops a
        broadcast stance from OPPOSITION at the conviction bar.** The two
        are not inconsistent, and the difference is the blast radius, not the
        seat: there, opposition CULLS, and one `equity_outlook` flip acting
        on every held name at once is a portfolio-wide forced liquidation
        dressed up as per-name judgement (that is the documented reason the
        carve-out exists). Here, opposition merely refuses ONE new entry.
        Both gates therefore resolve a broadcast stance the same way in
        substance: it is never the reason the desk takes on risk, and never
        the reason it dumps the book, but it may be the reason it declines a
        single new name.
        """
        if not macro_analysis:
            return {}
        sectors = self._macro_sectors(positions, symbol_sectors)
        symbols = set(registry) | {p.symbol.upper() for p in positions}
        return {
            symbol: frozenset({"macro"})
            for symbol, _stance, is_broadcast in self._macro_stance_rows(
                macro_analysis=macro_analysis,
                symbols=symbols,
                sectors=sectors,
            )
            if is_broadcast
        }

    def build_evidence_registry(
        self,
        *,
        analyses: list[TechAnalysisResult],
        positions: list[Position],
        news_intel: NewsIntelligenceReport | None,
        earnings_analyses: list[dict],
        macro_analysis: dict | None,
        smart_money_findings: list[SmartMoneyFinding] | None = None,
        symbol_sectors: dict[str, str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Canonical source/stance records shared by prompt and validator.

        Display decorations such as conviction and signal age never enter the
        stance. Historical narrative/memory is intentionally excluded.

        The intraday path used to return TECH ONLY, on the reasoning that it
        "cannot cite yesterday's macro/news/earnings as if they ran this
        tick". The grounding concern is real; the remedy was too broad. What
        must never happen is stale evidence being cited AS FRESH — not the PM
        reasoning about a 14:00 move with no idea what regime it is happening
        in. Today's macro and news are now carried forward explicitly (see
        `TradingPipeline._carry_forward_macro` / `_carry_forward_news`, which
        refuse anything not from today) and marked `carried_from_morning` in
        `data_status`, so the staleness travels with the evidence instead of
        being handled by deleting it. Earnings stay excluded: an intraday
        filing genuinely has not been read this tick.
        """

        registry: dict[str, dict[str, str]] = {}

        def put(symbol: str, source: str, stance: str | None) -> None:
            if symbol and stance:
                registry.setdefault(symbol.strip().upper(), {})[source] = stance

        for analysis in analyses:
            put(analysis.symbol, "technical", self._collapse_stances([analysis.rating]))

        if news_intel is not None:
            for symbol, items in news_intel.stock_news.items():
                put(symbol, "news", self._collapse_stances(i.sentiment for i in items))

        # One rule, two readers: `_earnings_stance_rows` decides which
        # earnings entries produce a stance at all, and `put`'s last-wins
        # ordering is preserved exactly. `stale_evidence_sources` walks the
        # SAME rows so the freshness verdict can never attach to a different
        # filing than the one whose stance actually landed in the registry.
        for symbol, stance, _filing_date in self._earnings_stance_rows(earnings_analyses):
            put(symbol, "earnings", stance)

        smart_money_stances: dict[str, list[str]] = {}
        for finding in smart_money_findings or []:
            smart_money_stances.setdefault(finding.symbol.upper(), []).append(finding.stance)
        for symbol, stances in smart_money_stances.items():
            put(symbol, "smart_money", self._collapse_stances(stances))

        if macro_analysis:
            # The stance still lands in the registry for EVERY covered
            # symbol, broadcast or not — it is real coverage, the PM may
            # still cite it, and `validate_grounding` must still recognise
            # it. What a BROADCAST stance no longer does is CORROBORATE a
            # trade in the agreement tally — its dissent still counts.
            # See `broadcast_macro_sources`.
            for symbol, stance, _is_broadcast in self._macro_stance_rows(
                macro_analysis=macro_analysis,
                symbols=set(registry) | {p.symbol.upper() for p in positions},
                sectors=self._macro_sectors(positions, symbol_sectors),
            ):
                put(symbol, "macro", stance)

        return {symbol: sources for symbol, sources in registry.items() if sources}
