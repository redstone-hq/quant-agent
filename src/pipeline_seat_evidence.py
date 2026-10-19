"""Seat-evidence helpers (split out of src/pipeline_stages.py).

Moved VERBATIM out of `src/pipeline_stages.py`, following the pattern of
`src/pipeline_candidate_records.py`: the nomination-to-decision join, the
per-seat stance rows, the raw seat-nomination gather, the dual-shape macro
read and its parse-failure stash, the risk seat's per-symbol `risk` event
and edit snapshot, the advisory-only `scale_all_buys` record, and the SEC
sale-census probe. Every function takes what it needs as plain arguments, so
each can be exercised with stand-ins and no `TradingPipeline` is ever built
here (`tests/test_boundary_pipeline_seat_evidence.py` is the clause-5
witness). Every moved name is re-exported from `src.pipeline_stages` through
its one lazy re-export table, so each original import path and each test
patch target is unchanged; the write-through mirror on that module keeps a
patched name the same object on both sides.

The shared names below are imported FROM `src.pipeline_stages` as the stage
modules do; `src.pipeline_stages` imports this module only lazily, by name,
so the graph stays acyclic. Not in `src.number_sources.SCOPED_PATHS` on
purpose: the block carries no ledgered number site, so `number_sources.py`
is not grown for nothing. This module must not import `src.pipeline`.
"""

from __future__ import annotations

from src.pipeline_stages import (  # noqa: F401  shared helpers and module-level names
    Nomination,
    logger,
)


def _link_nominations_to_decision(pipeline, ctx) -> None:
    """Spec §9.5 — close the nomination→decision join. NEVER raises.

    Nominations are recorded during MorningResearchStage, where
    `ctx.decision_id` is still None: the id is not minted until DecisionStage
    mints it from a successful PM call. Every nomination row therefore landed
    with decision_id NULL, and nothing connected a nomination to the trade it
    became.

    This back-fills the id onto those rows the moment it exists. It is an
    UPDATE on the forensic evidence table and nothing more — no pipeline
    input, no ordering change, no new state read by any later stage. The
    alternative (deferring the nomination write until DecisionStage) would
    move a forensic write into the decision path and reorder it relative to
    the responder pass that acts on the same nominations; this does not.

    Best-effort by the same rule every other evidence write here follows: a
    persistence failure is a display gap, never a reason to alter or
    interrupt a decision.
    """
    if not getattr(ctx, "decision_id", None):
        return
    try:
        linked = pipeline.db.link_nominations_to_decision(
            run_id=ctx.run_id,
            decision_id=ctx.decision_id,
        )
        if linked:
            logger.info(
                "Conviction ledger: joined %d nomination row(s) to decision %s",
                linked,
                ctx.decision_id,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Conviction ledger: nomination join failed: %s", e)


def _record_seat_stances(
    pipeline,
    ctx,
    evidence_registry,
    symbols,
    *,
    non_corroborating_sources=None,
) -> None:
    """Spec §9.5 — record who ARGUED AGAINST, not only who proposed. NEVER raises.

    `non_corroborating_sources` (board item 109) marks the stances the §9.4
    tally would not let corroborate the trade — today only a macro stance
    broadcast onto a name whose sector the macro read never mentioned. The
    ledger is read later to score who was right; a stance recorded with no
    trace that the desk declined to count it reads as a seat that backed the
    idea, which is not what happened. It is recorded in `observation`
    because that field already exists and already travels with the row; no
    schema change is taken for this.

    §9.4 already computes each seat's stance per symbol into the canonical
    evidence registry, and counts only the ALIGNED ones to earn size. The
    opposing stances were computed and then discarded: nothing persisted
    "macro was underweight this name and the desk bought it anyway" in a form
    that could later be scored.

    So one `seat_stance` row per (idea, seat) is written from that same
    registry — support and dissent alike, no re-derivation, no second notion
    of what a stance is. Conviction comes from what the seat actually
    DECLARED: its nomination conviction where it nominated the symbol
    (`ctx.nomination_convictions`), Technical's own `conviction` field for the
    technical seat, and the neutral default where the schema offers none.

    `symbols` is the PM's target set — the ideas the desk actually decided on
    — not the whole registry, which would record a stance on every symbol
    merely covered this run.

    Purely additive: writes evidence rows, reads nothing back, returns
    nothing. No caller consumes its effect within the run.
    """
    if not getattr(ctx, "decision_id", None) or not evidence_registry:
        return
    try:
        from src.conviction_ledger import DEFAULT_CONVICTION, SeatStance, normalize_seat

        wanted = {str(s).strip().upper() for s in (symbols or []) if str(s).strip()}
        nominations = getattr(ctx, "nomination_convictions", None) or {}
        tech_conviction = {
            str(getattr(a, "symbol", "")).strip().upper(): str(getattr(a, "conviction", "") or DEFAULT_CONVICTION)
            for a in (ctx.analyses or [])
        }
        non_corroborating = non_corroborating_sources or {}
        stances: list[SeatStance] = []
        for symbol in sorted(wanted):
            gated = non_corroborating.get(symbol) or frozenset()
            for source, stance in sorted((evidence_registry.get(symbol) or {}).items()):
                seat = normalize_seat(source)
                declared = (nominations.get(symbol) or {}).get(seat) or {}
                conviction = declared.get("conviction")
                if not conviction and seat == "technical":
                    conviction = tech_conviction.get(symbol)
                observation = str(declared.get("observation") or "")
                if source in gated:
                    note = (
                        "market-wide stance, not a read on this name's "
                        "sector — macro never counts for or against a "
                        "trade; it tilts size only"
                    )
                    observation = f"{observation} [{note}]".strip()
                stances.append(
                    SeatStance(
                        seat=seat,
                        symbol=symbol,
                        stance=stance,
                        conviction=conviction or DEFAULT_CONVICTION,
                        nominated=bool(declared),
                        observation=observation,
                    )
                )
        if not stances:
            return
        pipeline.db.record_seat_stances(
            run_id=ctx.run_id,
            decision_id=ctx.decision_id,
            stances=stances,
        )
        logger.info(
            "Conviction ledger: recorded %d seat stance(s) across %d idea(s) for decision %s",
            len(stances),
            len(wanted),
            ctx.decision_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Conviction ledger: seat-stance recording failed: %s", e)


def _collect_seat_nominations(
    news_intel,
    macro_analysis,
    earnings_results,
) -> dict[str, list[Nomination]]:
    """Gather each seat's raw (not yet capped/deduped) nominations this run.

    Phase 9 §9.1. News and Macro nominations come straight off the live
    Pydantic report each seat produces once per morning session. Earnings
    is different: `EarningsAnalystAgent` runs one LLM call PER NEW FILING
    (`analyze_reports` / `_analyze_one`), so a session that reads several
    filings makes several `EarningsAnalysis` objects, not one. Its
    nominations are therefore the union across every filing analyzed this
    run, re-validated from the stored dict shape
    (`earnings_results[i]["analysis"]`, already `validated_model.model_dump()`
    — see `EarningsAnalystAgent._analyze_new`/`_load_analysis`) via
    `Nomination.model_validate` rather than trusted as already-typed.

    Always returns all three seat keys, even when a seat produced nothing
    this run, so `select_nominations` never has to special-case a missing
    seat.
    """
    seats: dict[str, list[Nomination]] = {
        "news_analyst": [],
        "macro_analyst": [],
        "earnings_analyst": [],
    }
    if news_intel is not None:
        seats["news_analyst"] = list(getattr(news_intel, "nominations", None) or [])
    # macro_analysis can be a plain carried-forward dict in other stages
    # (see _macro_analysis_as_dict), but never inside MorningResearchStage
    # — it is always either a fresh MacroAnalysis or None here. Guard
    # anyway so a future caller passing the carried-forward shape degrades
    # to "no macro nominations" instead of an AttributeError.
    if macro_analysis is not None and not isinstance(macro_analysis, dict):
        seats["macro_analyst"] = list(getattr(macro_analysis, "nominations", None) or [])
    for item in earnings_results or []:
        analysis = item.get("analysis") if isinstance(item, dict) else None
        if not analysis:
            continue
        for raw in analysis.get("nominations") or []:
            try:
                seats["earnings_analyst"].append(Nomination.model_validate(raw))
            except Exception as e:
                logger.warning("Dropping malformed earnings nomination: %s", e)
    return seats


def _macro_analysis_as_dict(macro_analysis) -> dict | None:
    """Dual-shape read: a live MacroAnalysis or a MacroStore snapshot.

    MacroStore now persists `reasoning_chain` and `sector_guidance_rows`
    so a same-day snapshot can re-validate. Coerce then validate; a trim
    that still cannot parse is None plus a durable fail reason — never a
    broken dict smuggled into PM.
    """
    if macro_analysis is None:
        return None
    from src.models import MacroAnalysis
    from src.seat_heal import coerce_macro_shape, describe_macro_parse_failure

    if isinstance(macro_analysis, MacroAnalysis):
        return macro_analysis.model_dump()
    if isinstance(macro_analysis, dict):
        payload, _fixes = coerce_macro_shape(macro_analysis)
    else:
        dump = getattr(macro_analysis, "model_dump", None)
        payload = dump() if callable(dump) else None
        if not isinstance(payload, dict):
            return None
        payload, _fixes = coerce_macro_shape(payload)
    try:
        return MacroAnalysis.model_validate(payload).model_dump()
    except Exception as exc:
        reason = describe_macro_parse_failure(payload, exc)
        logger.error(
            "macro_analysis failed to parse after coerce: %s",
            reason,
            exc_info=True,
        )
        _stash_macro_parse_failure(reason)
        return None


def _stash_macro_parse_failure(reason: str) -> None:
    """One durable reason, de-duplicated, drained by DecisionStage."""
    from src.agents.portfolio_manager import PortfolioManagerAgent

    failures = getattr(PortfolioManagerAgent, "_macro_parse_failures", None)
    if not isinstance(failures, list):
        PortfolioManagerAgent._macro_parse_failures = []
        failures = PortfolioManagerAgent._macro_parse_failures
    if reason not in failures:
        failures.append(reason)


#: The four fields a risk-seat edit or `scale_all_buys` can change — the
#: same set `_apply_risk_modifications` accepts (`modifiable_fields` there).
_RISK_EDITABLE_FIELDS = ("allocation_pct", "entry_price", "stop_loss", "take_profit")


def _risk_edit_snapshot(decisions) -> dict:
    """`{(SYMBOL, action): {field: value}}` for the editable fields."""
    out: dict = {}
    for d in decisions or []:
        if d is None:
            continue
        out[(d.symbol.strip().upper(), d.action)] = {f: getattr(d, f, None) for f in _RISK_EDITABLE_FIELDS}
    return out


def _risk_event_for(
    decision,
    pre_rm_fields: dict,
    verdict,
    scale: float,
    field_aliases: dict | None = None,
):
    """The per-symbol `risk` event for a leg that SURVIVED the risk seat.

    Board item 164 (2026-09-19). This event used to carry the constant
    reason `risk_manager_verdict` on every symbol, and read `modified` for
    any symbol the seat merely NAMED in a modification — including an edit
    that was rejected, reverted or never matched — and for every leg,
    exits included, whenever `scale_all_buys` was below 1. Now:

    - `outcome` is `modified` only when a field of this decision actually
      differs from what it was before the seat's edits and scaling;
    - `reason` is the seat's OWN reason for this symbol — the stated reason
      on each modification that took effect — and only where the seat gave
      none does it say so in words;
    - `changes` carries every field that moved, as `[before, after]`.

    Pure: reads the decision, the snapshot and the verdict; changes nothing.
    """
    key = (decision.symbol.strip().upper(), decision.action)
    before = pre_rm_fields.get(key) or {}
    changes = {
        f: [before[f], getattr(decision, f, None)]
        for f in _RISK_EDITABLE_FIELDS
        if f in before and before[f] != getattr(decision, f, None)
    }
    category = getattr(verdict, "reason_category", None)
    details: dict = {"gate": "risk_manager", "reason_category": category}
    if not changes:
        reason = (
            f"risk manager approved {decision.symbol} unchanged; the seat "
            f"gave no reason specific to this symbol (verdict category "
            f"{category!r}; its run-level reasoning is on this run's verdict "
            f"row)"
        )
        return "approved", reason, details
    aliases = field_aliases if isinstance(field_aliases, dict) else {}
    seat_reasons = []
    seat_edited_fields: set[str] = set()
    for m in getattr(verdict, "modifications", None) or []:
        field = aliases.get(m.field, m.field)
        if m.symbol.strip().upper() == key[0] and field in changes:
            seat_edited_fields.add(field)
            if (m.reason or "").strip():
                seat_reasons.append(f"{field}: {m.reason}")
    # Board item 134. When a stop_loss/entry_price edit widened risk-per-share,
    # `_apply_risk_modifications` reduces `allocation_pct` to keep dollar risk
    # within the granted budget. That drop is NOT a field the seat named, so it
    # would otherwise sit in `changes` with no reason of its own — reading as an
    # unexplained move or bucketed under the stop edit. Attribute it explicitly
    # (only when the seat did not itself edit allocation_pct, the size fell, and
    # a stop/entry edit is what moved).
    alloc_change = changes.get("allocation_pct")
    if (
        alloc_change is not None
        and "allocation_pct" not in seat_edited_fields
        and decision.action in ("BUY", "SHORT")
        and isinstance(alloc_change[0], (int, float))
        and isinstance(alloc_change[1], (int, float))
        and alloc_change[1] < alloc_change[0]
        and seat_edited_fields & {"stop_loss", "entry_price"}
    ):
        widened = ", ".join(sorted(seat_edited_fields & {"stop_loss", "entry_price"}))
        seat_reasons.append(
            f"allocation_pct: reduced to keep dollar-risk within the granted "
            f"budget after the risk seat edited {widened} (wider stop / edited "
            f"entry -> smaller position, never larger dollar risk)"
        )
    # Board items 134 + 162 (owner ruling 2026-09-25): `scale_all_buys` is
    # ADVISORY on entries and no longer changes any allocation_pct, so it can
    # no longer be the cause of an allocation move here — any allocation change
    # in `changes` now comes only from the seat's per-symbol `modifications`.
    # The scale concern is recorded separately as a `scale_advisory` event in
    # `RiskStage.run`; it must not be attributed to a modification here.
    details["changes"] = changes
    reason = "; ".join(seat_reasons) or (
        f"risk manager changed {', '.join(sorted(changes))} on "
        f"{decision.symbol} without stating a reason for this symbol"
    )
    return "modified", reason, details


def _record_scale_advisory(decisions, verdict) -> tuple[list, float, list]:
    """RECORD — but do NOT APPLY — RiskVerdict.scale_all_buys on entries.

    Owner ruling 2026-09-25 (reaffirming his 2026-09-19 ruling), board items
    134 + 162: a model-picked, unverifiable portfolio-wide multiplier may not
    size real trades. On ENTRIES the risk seat is now ADVISORY — its
    `scale_all_buys` concern and reason are captured and recorded durably
    (owner-facing evidence / feed), but the multiplier is NOT applied to any
    `allocation_pct` and drops NO trade. Every entry proceeds at the size the
    constructor / allocator set, subject to the HARD aggregate limits enforced
    downstream (gross-exposure ceiling, per-trade risk %, correlation /
    at-risk budget, per-name `max_position_pct`), which are unchanged and
    remain the real constraint.

    Before this ruling the same value multiplied every BUY/SHORT allocation
    and DROPPED any entry it zeroed (board item 136). That sizing effect is
    removed. The recording it fed is kept, re-cast as an advisory record: the
    caller files one pipeline event per flagged entry so the concern + reason
    still reach the desk. `scale_all_buys` still feeds logging / metrics / the
    trader feed elsewhere (unchanged) — the ONLY behaviour removed here is its
    effect on entry sizing.

    Treats None/missing as 1.0 (no concern). Returns
    ``(decisions_unchanged, scale, advised)`` where `advised` is the list of
    ``(symbol, allocation_pct)`` BUY/SHORT entries the seat flagged, populated
    only when ``0.0 <= scale < 1.0``. SELL, COVER and HOLD were never scaled
    and are never flagged. The decisions list is returned unchanged.
    """
    scale_raw = getattr(verdict, "scale_all_buys", 1.0)
    scale = 1.0 if scale_raw is None else float(scale_raw)
    advised: list[tuple[str, float]] = []
    if not (0.0 <= scale < 1.0):
        return list(decisions), scale, advised

    for d in decisions:
        if d is not None and d.action in ("BUY", "SHORT"):
            advised.append((d.symbol, d.allocation_pct))
            logger.info(
                "scale_all_buys=%.2f is ADVISORY on entries (board item 134): "
                "recording %s's exposure concern; allocation_pct %.2f%% is "
                "UNCHANGED and the trade is NOT dropped — the hard aggregate "
                "limits remain the constraint",
                scale,
                d.symbol,
                d.allocation_pct,
            )
    return list(decisions), scale, advised


def _probe_sale_census(provider: object) -> dict | None:
    """Board item 63: find the SEC provider's last sale census, however the
    provider happens to be wrapped. Duck-typed on purpose -- the combined
    provider delegates by attribute, exactly as `form4_coverage` is probed
    a few lines below. Returns None when nothing recorded one."""
    candidates: list[object] = [provider]
    nested = getattr(provider, "providers", None)
    if isinstance(nested, (list, tuple)):
        candidates.extend(nested)
    if hasattr(provider, "__dict__"):
        candidates.extend(vars(provider).values())
    for candidate in candidates:
        census = getattr(candidate, "last_sale_census", None)
        if isinstance(census, dict) and census.get("sale_rows"):
            return census
    return None
