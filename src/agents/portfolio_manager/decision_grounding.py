"""src.agents.portfolio_manager.decision_grounding -- the standalone decision-grounding piece.

Bodies moved verbatim from src/agents/portfolio_manager/grounding.py (DecisionGroundingMixin);
the mixin keeps same-named thin shims. Every collaborator is an explicit keyword-only
constructor argument, so this builds and runs with no agent behind it.
"""

import logging
import re
from datetime import date
from pydantic import ValidationError
from src.data.news_store import ACTIVE_STATE_CHANGE_WINDOW_DAYS
from src.models import (
    CandidateRejection,
    NewsIntelligenceReport,
    PortfolioDecision,
    Position,
    SmartMoneyFinding,
    TargetPosition,
    TechAnalysisResult,
    parse_telemetry,
)
from src.risk.constants import reward_risk_floor_applies
from src.risk.rules import stance_is_aligned, weight_pct_of
from src.trading_calendar import et_today

# Same logger object the monolith used, so log capture by name is unchanged.
logger = logging.getLogger("src.agents.portfolio_manager")


# §9.3 — greppable status key for a target dropped over an unadjudicated
# seat conflict, matching the naming convention of Phase 3.3's
# `exit_blocked_no_named_trigger` (src/pipeline.py). Logs and tests key on
# this exact string.
CONFLICT_UNADJUDICATED_STATUS = "pm_conflict_unadjudicated"

# 2026-09-01 (measured 2026-09-02) — greppable status key for the sub-floor
# catalyst gate, same naming convention as the one above. See
# `_apply_subfloor_catalyst_rule` for what it means. Logs and tests key on
# this exact string.
SUBFLOOR_CATALYST_UNVERIFIED_STATUS = "pm_subfloor_catalyst_unverified"

#: One rendered `active_state_changes` row, as
#: `TradingPipeline._build_active_state_changes` emits it:
#:     - [2026-08-31] Anthropic signs a cloud deal with Lambda → NVDA(bullish)
#: The date and the affected-symbol+direction list are the fields the
#: catalyst gate resolves a citation against; the event prose is
#: deliberately NOT matched (see `_catalyst_cites_state_change`).
_STATE_CHANGE_ROW_RE = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(?P<rest>.+)$")

#: One `SYMBOL(direction)` pair inside a state-change row's symbol list.
#: A symbol rendered without a parenthesized direction (legacy format, or
#: a stray comma) does not match and is treated as having no recorded
#: direction — see `_state_change_symbols_by_date`.
_SYMBOL_DIRECTION_RE = re.compile(r"^([A-Z0-9.\-]+)\((\w+)\)$")

#: Any ISO date appearing anywhere in a `catalyst` string. The PM cites a row
#: by its date; the symbol half of the pair is the target's own symbol, which
#: it cannot misstate without the target being about a different name.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _order_is_buy_side(intent: str, pos: Position | None) -> bool:
    """True when the ORDER a target implies is buy-side, i.e. needs bullish evidence.

    `intent` says whether exposure grows ("buy"/"short") or shrinks ("sell");
    it does not say which side of the book the order hits. Shrinking a held
    SHORT is a cover — a buy-side order — so bearish evidence argues AGAINST
    it, exactly as it argues against a long entry. 2026-10-09: five FLNC
    cover decisions labelled bearish evidence "conflicts" (correct) and were
    refused because polarity was read from `intent == "buy"` alone.
    """
    if intent == "buy":
        return True
    return intent == "sell" and pos is not None and (pos.qty or 0) < 0


class DecisionGrounding:
    """Decision grounding: validation of the model reply, conflict/catalyst/rejection/target drops, canonical targets.

    Standalone: every collaborator is an explicit keyword-only constructor argument.
    Bodies moved verbatim from DecisionGroundingMixin (src/agents/portfolio_manager/grounding.py);
    the only mechanical change is `cls` -> `self` (decorators dropped).
    """

    def __init__(
        self,
        *,
        build_evidence_registry,
        conflict_source_aliases,
        target_intent=None,
        canonical_targets=None,
        conflict_is_named=None,
    ) -> None:
        self.build_evidence_registry = build_evidence_registry
        self._CONFLICT_SOURCE_ALIASES = conflict_source_aliases
        # The bodies below are themselves moved; a caller passes one only when it was
        # swapped on the host (see the shim), otherwise this object's own run.
        if target_intent is not None:
            self._target_intent = target_intent
        if canonical_targets is not None:
            self._canonical_targets = canonical_targets
        if conflict_is_named is not None:
            self._conflict_is_named = conflict_is_named

    def _target_intent(
        self,
        target: TargetPosition,
        held: dict[str, Position],
        total_value: float,
        existing_risk_pct: dict[str, float] | None = None,
    ) -> str:
        """ "buy" / "short" (opens or increases exposure) vs "sell" (exits or
        reduces it).

        The single definition both `validate_grounding` (does this claim's
        polarity support the action?) and §9.3's
        `_drop_unadjudicated_conflicts` (is this target even in scope for
        conflict adjudication?) classify a target by — factored out so the
        two can never disagree about what counts as an increase.

        Risk-based targets (spec §2.1) state risk, not weight, so they are
        compared against the holding's CURRENT risk: `existing_risk_pct`,
        the per-symbol stop-based budget risk (% of equity) from the heat
        roll-up (`src/risk/metrics.py`) — the same "equity at-risk" figure
        the PM is shown per holding and the constructor rations against
        (`src/pipeline_stages.py::_book_risk_inputs`). A request strictly
        BELOW that figure, on a name held on the SAME side, is a trim
        ("sell"). Anything else — not held, held on the other side, or a
        request at or above current risk — is an increase.

        Fail safe: when current risk cannot be determined for this holding
        (no map, or no entry for the symbol), the target is classified as an
        INCREASE, the stricter treatment in both callers. 2026-09-17
        incident (intra_check-44594a05): before current risk was consulted,
        EVERY non-zero risk target was an increase, so an intraday funding
        trim of a held name the scan had not analysed failed "increase lacks
        a current-run Technical analysis" and rejected the whole plan,
        including a valid new entry. `is_close` (zero risk, or a legacy zero
        weight) is always a full exit.
        """
        symbol = target.symbol.upper()
        pos = held.get(symbol)
        current_weight = 0.0
        if pos is not None and total_value > 0:
            current_weight = weight_pct_of(pos.market_value, symbol, total_value)
        if target.risk_allocation_pct is not None:
            if target.is_close:
                return "sell"
            increase = "short" if target.direction == "short" else "buy"
            if pos is None or existing_risk_pct is None or not pos.qty:
                return increase
            held_side = "short" if pos.qty < 0 else "long"
            if held_side != target.direction:
                return increase
            current_risk = {str(k).upper(): v for k, v in existing_risk_pct.items()}.get(symbol)
            if current_risk is None:
                return increase
            return "sell" if target.risk_allocation_pct < current_risk else increase
        return "buy" if (target.target_weight_pct or 0.0) > current_weight + 0.01 else "sell"

    def validate_grounding(
        self,
        decision: PortfolioDecision,
        *,
        analyses: list[TechAnalysisResult],
        positions: list[Position],
        news_intel: NewsIntelligenceReport | None,
        earnings_analyses: list[dict],
        macro_analysis: dict | None,
        total_value: float,
        symbol_sectors: dict[str, str] | None = None,
        smart_money_findings: list[SmartMoneyFinding] | None = None,
        allowed_buy_symbols: set[str] | None = None,
        existing_risk_pct: dict[str, float] | None = None,
    ) -> list[str]:
        """Validate only machine-readable claims against the prompt registry.

        Prompt and validator now consume the exact same canonical records.
        This removes the former impossible contract (decorated display text
        versus undecorated validation values) and brittle regex interpretation
        of free-form narrative while retaining phantom-exit, source-existence,
        exact-stance, uniqueness, relationship, and alignment checks.
        """

        errors: list[str] = []
        if decision.decisions:
            errors.append(
                "portfolio manager supplied concrete decisions; only grounded targets may cross the PM boundary"
            )
        held = {p.symbol.upper(): p for p in positions}
        registry = self.build_evidence_registry(
            analyses=analyses,
            positions=positions,
            news_intel=news_intel,
            earnings_analyses=earnings_analyses,
            macro_analysis=macro_analysis,
            smart_money_findings=smart_money_findings or [],
            symbol_sectors=symbol_sectors or {},
        )
        # 2026-09-11 redesign: `support_eligible` is now purely STRUCTURAL
        # (real, single-direction, legally-disclosed evidence) — it no
        # longer expires on a calendar. Whether an aged smart-money finding
        # may actually support a target is instead decided per-target,
        # below, by whether it CORRELATES with at least one other CURRENT
        # source's stance on the same symbol — one piece of information
        # among several, weighted by agreement, never an island judged on
        # its own age. See `SmartMoneyFinding.deterministic_eligibility`.
        smart_money_structurally_eligible: dict[str, bool] = {}
        for finding in smart_money_findings or []:
            symbol = finding.symbol.upper()
            smart_money_structurally_eligible[symbol] = (
                smart_money_structurally_eligible.get(symbol, False) or finding.support_eligible
            )
        reasoning_text = "\n".join(str(value) for value in decision.reasoning_chain.model_dump().values())

        for target in decision.targets:
            symbol = target.symbol.upper()
            pos = held.get(symbol)
            if target.is_close and pos is None:
                errors.append(f"{symbol}: close/exit target is not an actual holding")
            if not target.provenance:
                errors.append(f"{symbol}: target has no structured specialist provenance")
                continue

            # Risk-based targets (spec §2.1) state risk, not weight.
            # `_target_intent` compares them against the holding's current
            # stop-based risk (`existing_risk_pct`): below it on the same
            # side is a trim; everything else — and any holding whose current
            # risk is unknown — is an INCREASE, a BUY for a long and a SHORT
            # for a short (Stage 3). The increase branch below applies the
            # STRICTER checks (universe membership, an actual technical
            # analysis backing the name) to both sides alike. §9.3's conflict
            # adjudication (`_drop_unadjudicated_conflicts`) reuses this same
            # classification for its own "opens or increases" scope, so the
            # two never disagree about what counts as an increase.
            intent = self._target_intent(
                target,
                held,
                total_value,
                existing_risk_pct=existing_risk_pct,
            )
            if intent in ("buy", "short"):
                if allowed_buy_symbols is not None and symbol not in {
                    str(item).strip().upper() for item in allowed_buy_symbols
                }:
                    errors.append(
                        f"{symbol}: increase is outside the configured universe and "
                        "the deterministic temporary-admission allowlist"
                    )
                # Current-run Technical is a hard gate on increases. Missing
                # Tech is a producing-step defect (the intraday scan must
                # include held names in the paid batch), not a reason to
                # drop the name here and keep the rest of the book.
                if symbol not in {analysis.symbol.upper() for analysis in analyses}:
                    errors.append(f"{symbol}: increase lacks a current-run Technical analysis")
            expected_sources = registry.get(symbol, {})
            # Correlation, not calendar age: smart_money may support THIS
            # target only if it is structurally real evidence AND at least
            # one OTHER current source (technical/news/earnings/macro)
            # already in the registry for this symbol independently points
            # the same direction. An insider trade with nothing else
            # backing it right now is still shown as context — it simply
            # doesn't get to count as support on its own, regardless of
            # whether it happened yesterday or three months ago.
            smart_money_correlates = smart_money_structurally_eligible.get(
                symbol,
                False,
            ) and any(
                stance_is_aligned(
                    other_source,
                    symbol,
                    other_stance,
                    wants_bullish=_order_is_buy_side(intent, pos),
                )
                for other_source, other_stance in expected_sources.items()
                if other_source != "smart_money"
            )
            seen_sources: set[str] = set()
            supporting_sources: set[str] = set()
            for claim in target.provenance:
                source = claim.source
                stance = claim.observed_stance.strip().lower().replace(" ", "_")
                expected = expected_sources.get(source)
                if expected is None:
                    errors.append(f"{symbol}: claims {source} coverage that does not exist")
                    continue
                if stance != expected:
                    errors.append(f"{symbol}: claims {source} stance {stance!r}; canonical stance is {expected!r}")
                    continue
                if source in seen_sources:
                    errors.append(f"{symbol}: duplicate {source} provenance claim")
                    continue
                seen_sources.add(source)

                # Stage 3: "short" (opening/adding a short, direction=="short")
                # needs the same bearish-polarity evidence a "sell" (trimming
                # a long) does — both are bearish-direction actions on the
                # symbol. "buy" (opening/adding a long) and a "sell" that
                # COVERS a held short are buy-side orders and need bullish
                # evidence (`_order_is_buy_side`). `stance_is_aligned` (src/risk/rules.py) is the
                # SAME polarity rule §9.4's agreement-count ceiling uses —
                # one definition, not a second one that could quietly drift
                # from this one.
                polarity_supports = stance_is_aligned(
                    source,
                    symbol,
                    stance,
                    wants_bullish=_order_is_buy_side(intent, pos),
                )
                # Owner ruling 2026-10-09: whole exits only. Every sell/cover
                # is judged as a full close by order side; there is no
                # partial-trim evidence exception.
                if claim.relationship == "supports":
                    if source == "smart_money" and not smart_money_correlates:
                        errors.append(
                            f"{symbol}: smart-money evidence with nothing else "
                            "currently corroborating it cannot support a target "
                            "on its own; use context"
                        )
                        continue
                    if not polarity_supports:
                        errors.append(
                            f"{symbol}: {source} stance {stance!r} does not support "
                            f"the proposed {intent}; record a conflict or context"
                        )
                    else:
                        supporting_sources.add(source)
                elif claim.relationship == "conflicts" and polarity_supports:
                    errors.append(
                        f"{symbol}: {source} stance {stance!r} supports the proposed "
                        f"{intent}; it cannot be labelled conflicts"
                    )
                elif (
                    claim.relationship == "context"
                    and stance not in {"neutral", "mixed"}
                    and source != "macro"
                    and not (source == "smart_money" and not smart_money_correlates)
                ):
                    errors.append(
                        f"{symbol}: directional {source} stance {stance!r} must be "
                        "marked supports or conflicts, not context"
                    )

            # Dynamic N/M alignment covers the core evidence sources actually
            # available for this symbol. Optional smart-money context remains
            # explicit provenance but does not dilute the established
            # technical/news/earnings/macro denominator.
            texts = [target.thesis]
            texts.extend(
                m.group(0)
                for m in re.finditer(
                    rf"\b{re.escape(symbol)}\b[^.\n]{{0,240}}\b\d+/\d+\b",
                    reasoning_text,
                    flags=re.IGNORECASE,
                )
            )
            for text in texts:
                for match in re.finditer(r"\b(\d+)/(\d+)\b", text):
                    stated_support, stated_available = map(int, match.groups())
                    available_sources = set(expected_sources) - {"smart_money"}
                    seen_alignment_sources = seen_sources & available_sources
                    supporting_alignment_sources = supporting_sources & available_sources
                    if stated_available != len(available_sources):
                        errors.append(
                            f"{symbol}: claims denominator {stated_available}, but "
                            f"{len(available_sources)} current source(s) are available"
                        )
                    elif seen_alignment_sources != available_sources:
                        errors.append(
                            f"{symbol}: alignment shorthand requires provenance for all "
                            f"available sources {sorted(available_sources)!r}"
                        )
                    elif stated_support != len(supporting_alignment_sources):
                        errors.append(
                            f"{symbol}: claims {stated_support}/{stated_available} aligned "
                            f"but provenance proves "
                            f"{len(supporting_alignment_sources)}/{stated_available}"
                        )
        return errors

    def _conflict_is_named(self, signal_conflicts: str, symbol: str, source: str) -> bool:
        """Whether `signal_conflicts` names BOTH `symbol` and `source`.

        SPECIFICITY OF REFERENCE ONLY — this is what `_drop_unadjudicated_
        conflicts` below checks for, and it proves the PM's text names the
        symbol and the source, NOT that its reasoning about the conflict is
        any good. A bland-but-specific sentence ("NVDA: macro is bearish
        but we are buying on the earnings beat") satisfies it. That is a
        strictly lower bar than "the desk resolved the disagreement," and
        it must never be described as more than that — this is still
        stronger than today, where a recorded conflict can go entirely
        unmentioned and the trade proceeds unchanged.

        Word-boundary, case-insensitive match on the symbol so a substring
        can't accidentally satisfy it (e.g. "V" inside "INVALID", or "DE"
        inside "TRADE"). `source` matches case-insensitively by substring
        against its alias list.
        """
        text = signal_conflicts or ""
        if not re.search(rf"\b{re.escape(symbol)}\b", text, flags=re.IGNORECASE):
            return False
        text_lower = text.lower()
        aliases = self._CONFLICT_SOURCE_ALIASES.get(source, (source,))
        return any(alias in text_lower for alias in aliases)

    def _drop_unadjudicated_conflicts(
        self,
        decision: PortfolioDecision,
        *,
        positions: list[Position],
        total_value: float,
        existing_risk_pct: dict[str, float] | None = None,
        dropped: list[dict] | None = None,
    ) -> PortfolioDecision:
        """An unresolved seat conflict on a target that OPENS or INCREASES
        exposure drops THAT ONE TARGET; it never fails the whole session.

        This is deliberately NOT implemented by appending to
        `validate_grounding`'s error list: `decide()` treats ANY non-empty
        error list as total session failure via `_semantic_failure`,
        discarding every target and the whole book. That is the right
        penalty for a decision that fabricates evidence, but the wrong one
        for a single candidate carrying one unaddressed disagreement — the
        punishment has to fit the offence. This mirrors two existing
        precedents instead: per-target isolation
        (`_drop_invalid_targets`/PR #73-#74) and Phase 3.3's exit gate,
        which drops one exit and logs `exit_blocked_no_named_trigger`
        rather than failing the run (see `src/pipeline.py`,
        `_reason_cites_hard_trigger`).

        HONESTY NOTE — read `_conflict_is_named`'s docstring before
        touching this. It enforces SPECIFICITY OF REFERENCE, not QUALITY
        OF REASONING. Do not describe this method's effect as "the desk
        resolves its disagreements" anywhere it is discussed.

        Scope, deliberately asymmetric (mirrors §3.4's exit-side
        asymmetry): only targets classified `_target_intent in ("buy",
        "short")` — opening or increasing — are subject to this. Exits
        and reductions are exempt; this desk must never find it harder to
        cut risk than to add it.
        """
        held = {p.symbol.upper(): p for p in positions}
        signal_conflicts = decision.reasoning_chain.signal_conflicts
        kept: list[TargetPosition] = []
        for target in decision.targets:
            intent = self._target_intent(
                target,
                held,
                total_value,
                existing_risk_pct=existing_risk_pct,
            )
            if intent not in ("buy", "short"):
                kept.append(target)  # exits/reductions are exempt on purpose
                continue
            conflicting_sources = sorted(
                {claim.source for claim in target.provenance if claim.relationship == "conflicts"}
            )
            unaddressed = [
                source
                for source in conflicting_sources
                if not self._conflict_is_named(signal_conflicts, target.symbol, source)
            ]
            if unaddressed:
                logger.warning(
                    "%s: dropping %s (%s) — signal_conflicts does not name "
                    "both the symbol and %s. A recorded conflict on a name "
                    "being opened/increased must be individually addressed "
                    "in signal_conflicts (symbol + source) or the target is "
                    "dropped, not traded; the rest of this session's "
                    "decision is unaffected. signal_conflicts was: %r",
                    CONFLICT_UNADJUDICATED_STATUS,
                    target.symbol,
                    intent,
                    unaddressed,
                    signal_conflicts[:300],
                )
                # Board item 164: `dropped` is the optional per-symbol sink
                # `decide()` hands in so the drop is persisted, not only
                # logged. Recording only — the drop above is unchanged.
                if dropped is not None:
                    dropped.append(
                        {
                            "symbol": target.symbol,
                            "gate": CONFLICT_UNADJUDICATED_STATUS,
                            "intent": intent,
                            "unaddressed_sources": list(unaddressed),
                            "reason": (
                                f"{target.symbol} target ({intent}) DROPPED: the "
                                f"target records a conflict from "
                                f"{', '.join(unaddressed)} that signal_conflicts "
                                f"does not address by naming both the symbol and "
                                f"the source; a name being opened or increased "
                                f"must address every recorded conflict."
                            ),
                        }
                    )
                continue
            kept.append(target)
        decision.targets = kept
        return decision

    # --- The sub-floor catalyst gate (2026-09-02) -------------------------
    #
    # WHAT WENT WRONG. The prompt sets a reward:risk floor and permits a
    # below-floor pick that names a catalyst. Benchmarked 2026-09-01 on the
    # real opportunity set of the zero-trade day (`run-64290730`), both
    # candidate models picked NVDA at R/R 1.03 in 9 of 9 runs and passed over
    # GEV, which cleared the floor. THE MODELS DID NOT DISOBEY: every
    # sub-floor pick named a catalyst, cut size, and said in plain text that
    # the ratio was below floor. The rule-compliance grader passed them and
    # the risk manager agreed.
    #
    # The hole is in the RULE. For any mega-cap the news feed always carries
    # a concrete catalyst, so an assertable exception is a null constraint on
    # exactly the names it most needs to bind. Worse, the desk's own
    # `active_state_changes` block fed the PM two HIGH-conviction bullish
    # NVDA items that morning, which became the catalyst justifying the
    # exception — the accountability machinery supplying the key to its own
    # lock. The live run's recorded NVDA catalyst was a $3B SB Energy
    # investment that appears in NO state-change row at all.
    #
    # So this is a code problem, not a model problem, and a tenth firmly
    # worded sentence in a 52KB prompt whose ninth was obeyed is not a
    # design. Two deterministic changes, both AFTER the PM submits:
    #   1. the catalyst must RESOLVE to a specific `active_state_changes`
    #      row (this table already carries dates and symbols), or the
    #      exception does not apply and the target is dropped;
    #   2. a sub-floor pick that does resolve is capped at the smallest
    #      starter size the desk can hold.
    # Costs nothing when the catalyst is real; costs the slot when it is
    # decorative.

    def _state_change_symbols_by_date(
        self,
        active_state_changes: str,
        asof: date | None = None,
    ) -> dict[str, dict[str, set[str]]]:
        """Parse the rendered `active_state_changes` block into
        `{iso_date: {SYMBOL: {direction, ...}, ...}}`.

        The block the PM is shown is built by
        `TradingPipeline._build_active_state_changes`, which is the only
        producer of this format, so parsing its own output back is a
        round-trip over a format this repo owns end to end — not an attempt
        to read arbitrary prose. A line that does not match is skipped
        rather than raising: an unparseable row must narrow what can be
        cited, never fail the session.

        DIRECTION. Each symbol is rendered as `SYMBOL(direction)` (Phase 13
        catalyst-gate fix — see `StateChange.symbol_direction` in
        src/models.py). A symbol rendered without a recognized
        `(bullish|bearish|neutral)` suffix — including the `(unknown)` the
        producer writes for a symbol with no recorded direction — is still
        recorded (so the "row exists and names this symbol" fact is not
        lost) but with an empty direction set, which cannot satisfy
        `_catalyst_cites_state_change`'s directional check. Fail closed:
        an undirected mention proves the row exists, not that it agrees
        with the trade.

        Rows sharing a date are UNIONED per symbol (a symbol's direction
        set can pick up entries from more than one same-day row). A
        citation therefore proves "a HIGH-conviction state change affecting
        this symbol in THIS direction was recorded on this date", which is
        the checkable claim; it does not distinguish two same-day rows
        about the same name, and it does not need to.

        RECENCY. A row older than `ACTIVE_STATE_CHANGE_WINDOW_DAYS`, or dated
        in the future, is dropped. This is redundant TODAY — the producer
        scans exactly that window, so it cannot render an older row — and it
        is here so it stays true: the age bound is currently a property of
        one function in `pipeline.py`, and if that ever drifts, the thing
        that silently widens is what counts as a catalyst. It reuses the
        producer's own constant rather than choosing a second number.
        `asof` defaults to the trading calendar's today; if that cannot be
        read the error propagates, so the session fails loudly instead of
        quietly switching off the news-reason checks.
        """
        if asof is None:
            asof = et_today()
        by_date: dict[str, dict[str, set[str]]] = {}
        for line in (active_state_changes or "").splitlines():
            match = _STATE_CHANGE_ROW_RE.match(line)
            if match is None:
                continue
            try:
                row_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            age = (asof - row_date).days
            if age < 0 or age > ACTIVE_STATE_CHANGE_WINDOW_DAYS:
                # Stale, or dated ahead of the session. Either way it cannot
                # be what a trade taken today is reacting to.
                continue
            # Split on the LAST arrow: the event prose can contain one, the
            # symbol list cannot.
            rest = match.group("rest")
            if "→" not in rest:
                continue
            _event, _, symbol_text = rest.rpartition("→")
            row_symbols: dict[str, str | None] = {}
            for part in symbol_text.split(","):
                part = part.strip()
                if not part or part == "—":
                    continue
                m = _SYMBOL_DIRECTION_RE.match(part)
                if m:
                    row_symbols[m.group(1).upper()] = m.group(2).lower()
                else:
                    # Legacy row with no `(direction)` suffix at all — the
                    # symbol is still recorded (existence), just with no
                    # direction to offer the gate.
                    row_symbols[part.upper()] = None
            if not row_symbols:
                # `_build_active_state_changes` writes an em dash when the
                # news analyst attached no affected symbols. A market-wide
                # row names nobody, so it can back nobody.
                continue
            date_bucket = by_date.setdefault(match.group(1), {})
            for symbol, direction in row_symbols.items():
                symbol_directions = date_bucket.setdefault(symbol, set())
                if direction in ("bullish", "bearish", "neutral"):
                    symbol_directions.add(direction)
        return by_date

    def _catalyst_cites_state_change(
        self,
        catalyst: str,
        symbol: str,
        required_direction: str,
        by_date: dict[str, dict[str, set[str]]],
    ) -> bool:
        """Does `catalyst` resolve to a state-change row that names `symbol`
        with a direction that actually supports this trade?

        The citation is a DATE + SYMBOL pair, because that is what the table
        already carries — there is no row id to cite (`news_store.
        recent_state_changes` dedupes on the event string and has never
        emitted one). The symbol half is the target's own symbol, which a
        target cannot misstate without being about a different name, so the
        model only has to supply the date.

        `required_direction` is `"bullish"` for a long and `"bearish"` for a
        short (see the call site in `_apply_subfloor_catalyst_rule`, which
        derives it from `_target_intent`). A `"neutral"` direction, or a
        symbol with no recorded direction at all, does NOT satisfy either
        requirement — fail closed, same posture as everything else in this
        gate.

        HONESTY NOTE, and read it before describing this anywhere (Phase 13
        catalyst-gate fix, 2026-09-03 — this replaced an EXISTENCE-only
        check): this now proves the cited row EXISTS, COVERS THIS NAME, and
        is recorded in the DIRECTION this trade needs. It still does not
        prove the PM's prose about the row is any good, or that the news
        analyst's direction call was correct — same posture as
        `_conflict_is_named`: specificity and substance of the checkable
        claim, not quality of reasoning or ground truth. What it removes is
        the free-text assertion that no reader could ever check, and — as
        of this fix — the ability for a stock moving on genuinely bad news
        to walk through the door meant for good news (or vice versa for a
        short).
        """
        text = (catalyst or "").strip()
        if not text:
            return False
        symbol = symbol.strip().upper()
        return any(
            required_direction in by_date.get(cited, {}).get(symbol, set()) for cited in _ISO_DATE_RE.findall(text)
        )

    def _apply_subfloor_catalyst_rule(
        self,
        decision: PortfolioDecision,
        *,
        analyses: list[TechAnalysisResult],
        positions: list[Position],
        total_value: float,
        active_state_changes: str,
        starter_risk_pct: float,
        asof: date | None = None,
        real_reward_risk_by_symbol: dict[str, float | None] | None = None,
        existing_risk_pct: dict[str, float] | None = None,
    ) -> PortfolioDecision:
        """Keep every target at the size the PM asked for.

        **Rewritten 2026-09-17 — owner: residual invented R/R is a defect.
        Overnight bind: unmeasurable payoff honesty is a recorded fact /
        ranking hint with zero refuse, zero size floor, zero sub-floor
        branch. Catalyst-exception theater around a dead floor is gone.**

          Type B / breakout          -> untouched. No drop, no cap.
          Type A, ratio measurable   -> untouched. Any computed ratio is
                                        ranking information.
          Type A, ratio unmeasurable -> kept at the asked size. Python
                                        logs the missing ratio; it does
                                        not drop, shrink, or require a
                                        catalyst.

        Starter size is not assigned
        here. This is not an entry in `validate_grounding`'s error list.
        """
        _ = (starter_risk_pct, asof, active_state_changes)
        if real_reward_risk_by_symbol is not None:
            rr_by_symbol = {a.symbol.upper(): real_reward_risk_by_symbol.get(a.symbol.upper()) for a in analyses}
        else:
            rr_by_symbol = {a.symbol.upper(): a.risk_reward for a in analyses}
        setup_by_symbol = {a.symbol.upper(): a.setup_type for a in analyses}
        held = {p.symbol.upper(): p for p in positions}
        for target in decision.targets:
            intent = self._target_intent(
                target,
                held,
                total_value,
                existing_risk_pct=existing_risk_pct,
            )
            if intent not in ("buy", "short"):
                continue
            symbol = target.symbol.upper()
            if not reward_risk_floor_applies(setup_by_symbol.get(symbol)):
                continue
            if rr_by_symbol.get(symbol) is None:
                logger.info(
                    "%s: %s (%s) payoff geometry cannot be computed — "
                    "recorded as unknown ranking hint, not dropped and "
                    "not size-capped. Invented reward:risk floors are "
                    "retired.",
                    SUBFLOOR_CATALYST_UNVERIFIED_STATUS,
                    target.symbol,
                    intent,
                )
        return decision

    def _drop_invalid_rejections(self, parsed: dict) -> dict:
        """Same per-entry isolation as `_drop_invalid_targets`, for the
        candidate-accounting list (board item 133, 2026-09-18).

        `rejections` is BOOKKEEPING. A malformed entry in it must never
        destroy `reasoning_chain`, `portfolio_view` and every target — the
        seat explaining itself badly is not a reason to lose a whole
        session's decision. A dropped entry is not lost either: the symbol
        it named is then simply unaccounted for, and the accounting step in
        `DecisionStage` re-asks and records it exactly as it does for a name
        the seat never mentioned. So the failure mode is one paid re-ask,
        never a silent hole and never a dead session.
        """
        raw = parsed.get("rejections")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Portfolio manager: rejections is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["rejections"] = []
            return parsed
        valid: list = []
        for i, item in enumerate(raw):
            try:
                CandidateRejection.model_validate(item)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Portfolio manager: dropping unusable rejections entry at "
                    "index %d (%s) — the symbol it named will be re-asked "
                    "for, not silently omitted: %r",
                    i,
                    e,
                    item,
                )
                continue
            valid.append(item)
        parsed["rejections"] = valid
        return parsed

    def _drop_invalid_targets(self, parsed: dict, dropped: list[dict] | None = None) -> dict:
        """Pre-validate each TargetPosition; drop malformed entries with a
        warning naming the symbol (or list index when missing).

        `dropped` (board item 164) is an optional sink: each discarded entry
        is appended with its symbol, the gate and the validation error, so
        the caller can persist it per symbol instead of the only trace being
        a log line and an in-memory counter. Recording only.

        Mutates parsed in place for `targets`. Non-list shapes normalize to
        []. The TargetPosition validators stay strict (target_weight_pct
        must be in [0, 25], symbol normalised) — we just stop letting one
        bad row weaponize that strictness against the rest of the book.
        """
        raw = parsed.get("targets")
        if raw is None:
            return parsed
        if not isinstance(raw, list):
            logger.warning(
                "Portfolio manager: targets is %s, not list — replacing with []",
                type(raw).__name__,
            )
            parsed["targets"] = []
            return parsed
        valid: list[dict] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                logger.warning(
                    "Portfolio manager: dropping non-dict targets entry at index %d: %r",
                    i,
                    item,
                )
                if dropped is not None:
                    dropped.append(
                        {
                            "symbol": None,
                            "index": i,
                            "gate": "pm_target_malformed",
                            "reason": (
                                f"targets entry at index {i} DROPPED: it is a "
                                f"{type(item).__name__}, not an object, so it "
                                f"names no symbol: {item!r}"
                            ),
                        }
                    )
                continue
            try:
                # Dry run: the surviving dicts are validated again by
                # PortfolioDecision, so tallying here would double-count.
                with parse_telemetry.suspended():
                    TargetPosition(**item)
            except ValidationError as e:
                sym = item.get("symbol") or f"<idx {i}>"
                # A target the PM proposed and the desk then discarded is a
                # position that will not be opened. Counted for the same
                # reason the tech-side drop is: an idea lost at parse looks
                # identical to an idea nobody had.
                parse_telemetry.record_dropped_item("TargetPosition", str(sym))
                logger.warning(
                    "Portfolio manager: dropping malformed target for %s: %s",
                    sym,
                    e,
                )
                if dropped is not None:
                    errors = "; ".join(
                        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}" for err in e.errors()
                    )
                    raw_symbol = item.get("symbol")
                    dropped.append(
                        {
                            "symbol": (
                                str(raw_symbol).strip().upper()
                                if isinstance(raw_symbol, str) and raw_symbol.strip()
                                else None
                            ),
                            "index": i,
                            "gate": "pm_target_malformed",
                            "reason": (
                                f"{sym} target DROPPED at parse: it fails the "
                                f"target schema ({errors}), so no position is "
                                f"built from it; the rest of the decision stands."
                            ),
                        }
                    )
                continue
            valid.append(item)
        parsed["targets"] = valid
        return parsed

    def _canonical_targets(self, targets) -> list[tuple] | None:
        """Full TargetPosition decision payload (symbol, target_weight_pct,
        risk_allocation_pct, direction, conviction, thesis,
        thesis_invalid_if, suggested_stop_price, catalyst),
        order-insensitive. Built by re-validating each entry
        through the `TargetPosition` model itself — its own field
        normalization (symbol case, conviction case, numeric coercion)
        is the single source of truth for what "the same value" means,
        rather than a second, ad-hoc coercion path that can drift out of
        sync with the schema (or hide a real change behind a bug, as the
        prior `round(float(...))`-only / symbol+weight-only comparison
        did). Returns None — never `==` to anything, including itself —
        when the shape doesn't validate, so a malformed side fails closed
        instead of comparing (incorrectly) equal.
        """
        if targets is None:
            targets = []
        if not isinstance(targets, list):
            return None
        models: list[TargetPosition] = []
        for t in targets:
            if not isinstance(t, dict):
                return None
            try:
                with parse_telemetry.suspended():
                    models.append(TargetPosition(**t))
            except ValidationError as exc:
                # Fails closed, and says WHY: the real validation reason is
                # recorded against this symbol. Anything else is a bug and raises.
                sym = str(t.get("symbol") or "<unknown>")
                reason = "; ".join(
                    f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}" for err in exc.errors()
                )
                parse_telemetry.record_dropped_item("TargetPosition", sym, reason=f"malformed target: {reason}")
                return None
        return sorted(
            (
                (
                    m.symbol,
                    m.target_weight_pct,
                    m.risk_allocation_pct,
                    m.direction,
                    m.conviction,
                    m.thesis,
                    m.thesis_invalid_if,
                    m.suggested_stop_price,
                    m.catalyst,
                )
                for m in models
            ),
            key=lambda row: row[0],
        )

    def _decision_fields_unchanged(self, original: dict, repaired: dict) -> bool:
        """True iff the ENTIRE target set — every field of every
        TargetPosition, not just symbol/weight — survived a schema repair
        unchanged. `original` and `repaired` are both already post-
        `_drop_invalid_targets` for a fair comparison."""
        orig = self._canonical_targets(original.get("targets"))
        rep = self._canonical_targets(repaired.get("targets"))
        if orig is None or rep is None:
            return False
        return orig == rep
