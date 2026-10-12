"""Phase 14b — automatic opportunity-cost rotation, behind
`execution.rotation_enabled` (default OFF).

What these pin, in the order the owner asked for them:

  * flag OFF is a byte-for-byte no-op — no target appended, no event, and
    the structural-protection check is never even called;
  * a categorically-ineligible, already-unprotected holding IS rotated out
    (an ordinary zero-size target is appended) when the PM asked to buy the
    better candidate and there is no room;
  * the ranked-margin tier (both still eligible) is never executed;
  * a holding whose structural protection is intact is surfaced, never sold;
  * the sale's reason is built from measured facts, makes no regime /
  * bearish-state-change claim the holding-discipline checker could ever
    find false, and does not word itself past the midday keyword gate;
  * the appended target becomes an ordinary full SELL through the real
    `PortfolioConstructor._build_sell` — the same object RiskStage and
    ExecutionStage gate;
  * in-flight orders, a same-day BUY, a pending protection-restore WAL row
    or a pending re-peg on the held name all refuse the rotation, and an
    unanswerable in-flight question refuses it too (fail closed);
  * the owner alert carries the sold name, the replacement, the failed
    rules, the protection basis and the broker order id;
  * the BUY leg is dropped when the close was refused or did not fill (the
    freed room is not real), and every other BUY is untouched;
  * both legs' outcome is recorded durably, and a sold-but-not-bought
    rotation is paged;
  * the risk ceiling still binds the BUY: a close frees only its own risk.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import notifier
from src.models import (
    PortfolioDecision,
    Position,
    ReasoningChain,
    TargetPosition,
    TradeDecision,
)
from src.pipeline import TradingPipeline, _reason_cites_hard_trigger
from src.pipeline_context import RunContext
from src.pipeline_stages import (
    _alert_rotation_executed,
    _apply_rotation_execution,
    _drop_rotation_buy_if_room_not_freed,
    _record_rotation_buy_leg_outcome,
    _rotation_execution_enabled,
)
from src.portfolio_constructor import PortfolioConstructor
from src.risk.budget import RiskRequest, allocate_risk_budget
from src.risk.exit_guard import (
    StructuralProtectionCheck,
    claims_bearish_state_change,
    claims_regime_flip,
)
from src.exits.pm_claim_check import holding_discipline_claim_check
from src.rotation import RotationOpportunity, RotationPrecheck, rotation_sell_reason
from src.storage.db import Database

HELD_REASONS = ("R5 net evidence -1 if long — no rung",)
BROKEN_DETAIL = (
    "structural level 100.0 backing the stop has closed beyond it on two "
    "consecutive trading days: close 95.0 vs level 100.0 (break margin 0.05)"
)
from tests.pipeline_factory import build_pipeline


def _opportunity(tier: str = "ineligible_hold") -> RotationOpportunity:
    return RotationOpportunity(
        new_symbol="NEW",
        new_score=1.8,
        held_symbol="OLD",
        held_score=None if tier == "ineligible_hold" else 0.9,
        tier=tier,
        reasons=HELD_REASONS if tier == "ineligible_hold" else (),
    )


def _precheck(opportunity=None) -> RotationPrecheck:
    return RotationPrecheck(
        opportunity=opportunity,
        headroom_pct=0.2,
        ceiling_pct=25.0,
        floor_pct=0.5,
    )


def _rc() -> ReasoningChain:
    return ReasoningChain(
        macro_filter="m",
        news_check="n",
        earnings_check="e",
        signal_conflicts="s",
        sizing_logic="z",
        portfolio_balance="p",
        cash_target="c",
    )


def _decision(*targets: TargetPosition) -> PortfolioDecision:
    return PortfolioDecision(
        reasoning_chain=_rc(),
        portfolio_view="v",
        targets=list(targets),
    )


def _buy_new() -> TargetPosition:
    return TargetPosition(symbol="NEW", risk_allocation_pct=2.0, thesis="best idea")


def _pos(symbol="OLD", qty=10.0, price=95.0) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=100.0,
        current_price=price,
        market_value=qty * price,
        unrealized_pnl=(price - 100.0) * qty,
        sector="Technology",
    )


class _ProtectionProbe:
    """Stands in for `TradingPipeline._structural_protection_for_holding`
    and counts calls, so a test can prove the check was never made."""

    def __init__(self, protected: bool, basis: str = "structural_level_broken", detail: str = BROKEN_DETAIL):
        self.protected = protected
        self.basis = basis
        self.detail = detail
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> StructuralProtectionCheck:
        self.calls.append(kwargs)
        return StructuralProtectionCheck(
            protected=self.protected,
            basis=self.basis,
            detail=self.detail,
        )


def _pipeline(tmp_path, *, enabled=True, precheck=None, protected=False):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = build_pipeline(
        db=db,
        portfolio_manager=SimpleNamespace(
            last_rotation_precheck=precheck if precheck is not None else _precheck(_opportunity()),
        ),
    )
    pipeline.config = SimpleNamespace(
        execution=SimpleNamespace(rotation_enabled=enabled),
    )
    probe = _ProtectionProbe(protected=protected)
    pipeline._structural_protection_for_holding = probe
    return pipeline, db, probe


def _ctx(run_id="run-1") -> RunContext:
    return RunContext(run_id=run_id, session="morning")


def _events(db, run_id="run-1") -> list[tuple[str | None, dict]]:
    import json

    rows = db.conn.execute(
        "SELECT symbol, evidence_json FROM specialist_evidence "
        "WHERE kind = 'pipeline_event' AND run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [(r["symbol"], json.loads(r["evidence_json"])) for r in rows]


def _rotation_events(db, run_id="run-1") -> list[tuple[str | None, dict]]:
    return [(s, e) for s, e in _events(db, run_id) if e.get("stage") == "rotation"]


HISTORY = {"OLD": {"thesis_invalid_if": "closes below 100", "entry_price": 100.0, "stop_loss": 92.0}}


# ---------------------------------------------------------------------------
# Flag OFF — byte-for-byte the surfacing-only behaviour
# ---------------------------------------------------------------------------


def test_flag_off_is_a_no_op(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path, enabled=False)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert ctx.rotation is None
    assert _events(db) == []
    assert probe.calls == [], "protection check must not even run with the flag off"


def test_flag_default_is_off_and_a_mock_config_never_reads_as_on():
    from src.config import ExecutionConfig

    assert ExecutionConfig().rotation_enabled is False
    # `_repeg_settings` convention: a MagicMock's truthy auto-attribute must
    # never read as "yes, close a real position on your own".
    assert _rotation_execution_enabled(SimpleNamespace(config=MagicMock())) is False
    assert _rotation_execution_enabled(SimpleNamespace(config=None)) is False
    assert (
        _rotation_execution_enabled(
            SimpleNamespace(config=SimpleNamespace(execution=SimpleNamespace(rotation_enabled=1)))
        )
        is False
    )


# ---------------------------------------------------------------------------
# The categorical tier IS acted on, when the desk's own data says it is safe
# ---------------------------------------------------------------------------


def test_categorically_ineligible_unprotected_holding_is_rotated_out(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)

    assert [t.symbol for t in decision.targets] == ["NEW", "OLD"]
    close = decision.targets[1]
    assert close.is_close and close.risk_allocation_pct == 0.0
    assert close.direction == "long"
    assert close.thesis.startswith("ROTATION (deterministic, src/rotation.py): OLD")
    assert close.thesis_invalid_if == "closes below 100"
    # The PM's own BUY is untouched.
    assert decision.targets[0].risk_allocation_pct == 2.0

    # Protection was checked with the position's OWN entry context, as a
    # long, under this run's id — the same call RiskStage makes.
    assert len(probe.calls) == 1
    assert probe.calls[0]["symbol"] == "OLD"
    assert probe.calls[0]["thesis_invalid_if"] == "closes below 100"
    assert probe.calls[0]["is_short"] is False
    assert probe.calls[0]["run_id"] == "run-1"

    assert ctx.rotation["held_symbol"] == "OLD"
    assert ctx.rotation["new_symbol"] == "NEW"
    assert ctx.rotation["protection_basis"] == "structural_level_broken"
    assert "sell_order_id" not in ctx.rotation  # nothing submitted yet

    # Durable audit record, not a log line.
    events = _rotation_events(db)
    assert len(events) == 1
    symbol, payload = events[0]
    assert symbol == "OLD"
    assert payload["outcome"] == "proposed"
    assert payload["new_symbol"] == "NEW"
    assert payload["held_reasons"] == list(HELD_REASONS)
    assert payload["protection_basis"] == "structural_level_broken"
    assert payload["reason"] == close.thesis


def test_ranked_margin_tier_is_surfaced_only_while_its_flag_is_off(tmp_path):
    """Board item 39 default posture: `rotation_ranked_margin_enabled` is
    False, so the tier is information-only and byte-for-byte what it was —
    no target appended, no rotation on the context, no protection check even
    attempted."""
    pipeline, db, probe = _pipeline(
        tmp_path,
        precheck=_precheck(_opportunity("ranked_margin")),
    )
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert ctx.rotation is None
    assert probe.calls == []
    ((_, payload),) = _rotation_events(db)
    assert payload["outcome"] == "skipped"
    assert payload["reason"] == "ranked_margin_tier_not_enabled"


def test_nothing_surfaced_means_nothing_happens(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path, precheck=_precheck(None))
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert _events(db) == []


def test_structurally_protected_below_bar_holding_is_still_sold(tmp_path):
    """OWNER RULING 2026-10-01. Item 25 protects a still-ELIGIBLE,
    thesis-intact position; a name in `blocked` is not still-eligible, and
    failing the fresh-entry bar is now itself the real trigger. Intact
    protection is measured and reported, and no longer vetoes."""
    pipeline, db, probe = _pipeline(tmp_path, protected=True)
    probe.basis, probe.detail = "structural_level_intact", "close 101 vs level 100"
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW", "OLD"]
    assert ctx.rotation is not None
    assert ctx.rotation["protection_basis"] == "structural_level_intact"
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"].startswith("ROTATION (deterministic")
    assert payload["protection_basis"] == "structural_level_intact"


def test_protection_check_failure_fails_closed(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)

    def _boom(**kwargs):
        raise RuntimeError("bars feed down")

    pipeline._structural_protection_for_holding = _boom
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"] == "protection_check_failed"


# ---------------------------------------------------------------------------
# Board item 39 — the cull walk does not give up on the first protected name
# ---------------------------------------------------------------------------

WORST_REASONS = ("R2 rating below bar", "R5 net evidence -1 if long — no rung")
NEXT_REASONS = ("R5 net evidence -1 if long — no rung",)
MULTI_HISTORY = {
    "WORST": {"thesis_invalid_if": "closes below 50", "entry_price": 50.0, "stop_loss": 46.0},
    "NEXT": {"thesis_invalid_if": "closes below 100", "entry_price": 100.0, "stop_loss": 92.0},
}


class _PerSymbolProbe:
    """Structural-protection stand-in that answers per symbol, so a test can
    make the WORST below-bar name protected and the NEXT-worst not."""

    def __init__(self, protected_by_symbol: dict[str, bool]):
        self.protected_by_symbol = protected_by_symbol
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> StructuralProtectionCheck:
        self.calls.append(kwargs)
        sym = str(kwargs.get("symbol") or "").upper()
        protected = self.protected_by_symbol[sym]
        return StructuralProtectionCheck(
            protected=protected,
            basis="structural_level_intact" if protected else "structural_level_broken",
            detail=BROKEN_DETAIL,
        )


def _multi_opportunity() -> RotationOpportunity:
    """Two held names below the entry bar this session, worst-first."""
    return RotationOpportunity(
        new_symbol="NEW",
        new_score=1.8,
        held_symbol="WORST",
        held_score=None,
        tier="ineligible_hold",
        reasons=WORST_REASONS,
        ineligible_candidates=(("WORST", WORST_REASONS), ("NEXT", NEXT_REASONS)),
    )


def test_protected_worst_advances_to_the_next_worst_below_bar_holding(tmp_path):
    """>=2 holdings below the bar: the WORST is culled. Since 2026-10-01
    intact protection no longer skips it, so the walk stops at the first
    name — the advance-to-next-worst machinery remains for the guards that
    DO still skip (in flight, bought today, PM already targeting)."""
    pipeline, db, _ = _pipeline(tmp_path, precheck=_precheck(_multi_opportunity()))
    probe = _PerSymbolProbe({"WORST": True, "NEXT": False})
    pipeline._structural_protection_for_holding = probe
    ctx = _ctx()
    decision = _decision(_buy_new())
    positions = [_pos(symbol="WORST", price=45.0), _pos(symbol="NEXT", price=95.0)]

    _apply_rotation_execution(pipeline, ctx, decision, positions, MULTI_HISTORY)

    assert [t.symbol for t in decision.targets] == ["NEW", "WORST"]
    assert ctx.rotation is not None
    assert ctx.rotation["held_symbol"] == "WORST"
    assert ctx.rotation["new_symbol"] == "NEW"
    close = decision.targets[1]
    assert close.is_close and close.risk_allocation_pct == 0.0
    assert close.thesis.startswith("ROTATION (deterministic, src/rotation.py): WORST")

    assert [c["symbol"] for c in probe.calls] == ["WORST"]

    events = _rotation_events(db)
    assert not any(e["reason"] == "held_symbol_structurally_protected" for _, e in events)
    proposed = [(sym, e) for sym, e in events if e["outcome"] == "proposed"]
    assert len(proposed) == 1 and proposed[0][0] == "WORST"


def test_rotation_abandoned_only_when_every_below_bar_holding_is_protected(tmp_path):
    """Every below-bar holding structurally protected. Before 2026-10-01
    nothing was sold; under the ruling the worst is sold anyway, because
    intact protection is no longer a precondition on this tier."""
    pipeline, db, _ = _pipeline(tmp_path, precheck=_precheck(_multi_opportunity()))
    probe = _PerSymbolProbe({"WORST": True, "NEXT": True})
    pipeline._structural_protection_for_holding = probe
    ctx = _ctx()
    decision = _decision(_buy_new())
    positions = [_pos(symbol="WORST", price=45.0), _pos(symbol="NEXT", price=95.0)]

    _apply_rotation_execution(pipeline, ctx, decision, positions, MULTI_HISTORY)

    assert [t.symbol for t in decision.targets] == ["NEW", "WORST"]
    assert ctx.rotation is not None
    events = _rotation_events(db)
    assert not any(e["reason"] == "held_symbol_structurally_protected" for _, e in events)
    assert [sym for sym, e in events if e["outcome"] == "proposed"] == ["WORST"]


# ---------------------------------------------------------------------------
# The desk never invents the buy leg and never overrides the PM on the held name
# ---------------------------------------------------------------------------


def test_sold_even_when_the_pm_targeted_no_new_candidate(tmp_path):
    """MEASURED: this tier fired 8 times (24-25 Sep) and died 8 of 8 at this
    precondition, circularly — the prompt told the model there was no room
    to buy, so it never wrote the buy, so the sell was never proposed. The
    sale no longer depends on the buy."""
    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    other = TargetPosition(symbol="OTHER", risk_allocation_pct=1.0, thesis="x")
    decision = _decision(other)
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["OTHER", "OLD"]
    assert [c["symbol"] for c in probe.calls] == ["OLD"]
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"].startswith("ROTATION (deterministic")


def test_a_zero_size_pm_target_for_the_new_name_no_longer_blocks_the_cull(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(TargetPosition(symbol="NEW", risk_allocation_pct=0.0, thesis="x"))
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"].startswith("ROTATION (deterministic")


def test_not_rotated_when_pm_already_targets_the_held_symbol(tmp_path):
    """Whether the PM closes OLD itself or adds to it, the model has spoken —
    no duplicate close, no override."""
    for pm_size in (0.0, 1.5):
        sub = tmp_path / f"s{pm_size}"
        sub.mkdir()
        pipeline, db, probe = _pipeline(sub)
        ctx = _ctx()
        decision = _decision(
            _buy_new(),
            TargetPosition(symbol="OLD", risk_allocation_pct=pm_size, thesis="pm call"),
        )
        _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
        assert [t.symbol for t in decision.targets] == ["NEW", "OLD"]
        assert decision.targets[1].thesis == "pm call"
        assert ctx.rotation is None
        assert probe.calls == []
        ((_, payload),) = _rotation_events(db)
        assert payload["reason"] == "pm_already_targets_held_symbol"


def test_a_short_is_never_rotated(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos(qty=-10.0)], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"] == "held_symbol_is_not_a_long_position"


# ---------------------------------------------------------------------------
# In-flight orders, partial fills, same-day entries — never rotated
# ---------------------------------------------------------------------------


def test_in_flight_order_on_held_symbol_blocks_rotation(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    db.insert_trade(
        symbol="OLD",
        action="PARTIAL_SELL(50%)",
        qty=5.0,
        price=95.0,
        reasoning="midday trim",
        run_id="run-0",
        broker_order_id="ord-1",
        fill_status="submitted",
    )
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"] == "order_in_flight_on_held_symbol"
    assert "ord-1" in payload["detail"]


def test_held_symbol_bought_today_is_never_rotated(tmp_path):
    """A day-zero exit is what the exit noise band exists to prevent (OKLO
    2026-08-26: bought and sold at 0.67 ATR, never given one session's
    normal range). A same-day BUY — filled or not — refuses the rotation."""
    pipeline, db, probe = _pipeline(tmp_path)
    db.insert_trade(
        symbol="OLD",
        action="BUY",
        qty=10.0,
        price=100.0,
        reasoning="entry",
        run_id="run-0",
        broker_order_id="ord-b",
        fill_status="filled",
        stop_loss=90.0,
    )
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"] == "held_symbol_bought_today"


def test_pending_protection_restore_wal_row_blocks_rotation(tmp_path):
    """A pending_protection_restores row means a SELL on this name is
    already mid-flight (stops cancelled write-ahead, sale not yet
    finalized). Rotating on top of it would race the drain."""
    pipeline, db, probe = _pipeline(tmp_path)
    db.get_pending_protection_restores = lambda: [
        {"symbol": "OLD", "sell_order_id": "wal-9"},
    ]
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"] == "sell_already_in_flight_wal_row"
    assert payload["detail"] == "wal-9"


def test_pending_repeg_row_blocks_rotation(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)
    db.get_pending_repegs = lambda: [{"symbol": "OLD", "old_order_id": "rp-1"}]
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"] == "entry_repeg_in_flight"


def test_unanswerable_in_flight_question_fails_closed(tmp_path):
    pipeline, db, probe = _pipeline(tmp_path)

    def _boom():
        raise RuntimeError("db locked")

    db.get_pending_repegs = _boom
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    assert [t.symbol for t in decision.targets] == ["NEW"]
    assert probe.calls == []
    ((_, payload),) = _rotation_events(db)
    assert payload["reason"] == "in_flight_check_failed"


# ---------------------------------------------------------------------------
# The reason is real and checkable, and the sale goes through the normal gates
# ---------------------------------------------------------------------------


def _reason() -> str:
    return rotation_sell_reason(
        _opportunity(),
        protection_basis="structural_level_broken",
        protection_detail=BROKEN_DETAIL,
        headroom_pct=0.2,
        ceiling_pct=25.0,
        floor_pct=0.5,
    )


def test_reason_names_every_measured_fact():
    reason = _reason()
    assert "OLD" in reason and "NEW" in reason
    assert "R5 net evidence -1 if long" in reason
    assert "structural_level_broken" in reason
    assert "structural level 100.0" in reason
    assert "stopped earning its place" in reason
    assert "2026-10-01" in reason
    assert "score 1.80" in reason
    # `_build_sell` appends " (thesis_invalid_if: ...)" and then truncates
    # the whole reasoning at 500 — the measured clauses must all survive.
    assert len(reason) <= 420, len(reason)


def test_reason_makes_no_claim_the_holding_discipline_checker_could_find_false():
    """The rotation reason states only what was measured. It never asserts a
    regime flip or a bearish state change, so `holding_discipline_claim_check`
    — the deterministic gate RiskStage runs on every SELL — can never find
    it PROVABLY FALSE, and it never rides through on a claim it did not
    check. Verdict is 'ok' even against a protected position, which is
    exactly why `_apply_rotation_execution` refuses protected positions
    itself instead of relying on this check to."""
    reason = _reason()
    assert not claims_regime_flip(reason)
    assert not claims_bearish_state_change(reason)
    check = holding_discipline_claim_check(
        action="SELL",
        reason=reason,
        symbol="OLD",
        protected=True,
        macro_regime_today="risk-on",
        macro_status="ok",
        active_state_changes="",
    )
    assert check.verdict == "ok" and not check.blocks


def test_reason_does_not_word_itself_past_the_midday_keyword_gate():
    """`_reason_cites_hard_trigger` is the midday/close reviewer's phrase
    gate. A rotation reason built on a broken structural level or the noise
    band contains none of its keywords — this feature does not smuggle a
    trigger word in to pass a gate it does not go through (it takes the
    morning PM path, whose gates are RiskStage's). The one basis that DOES
    contain a keyword is `thesis_invalid_if_triggered`, and that is a real
    trigger by the desk's own definition, confirmed on two closes."""
    assert not _reason_cites_hard_trigger(_reason())
    noise = rotation_sell_reason(
        _opportunity(),
        protection_basis="noise_band_broken",
        protection_detail="adverse move 3.1 ATR from entry, outside the band",
        headroom_pct=0.2,
        ceiling_pct=25.0,
        floor_pct=0.5,
    )
    assert not _reason_cites_hard_trigger(noise)


def test_reason_builder_refuses_the_ranked_margin_tier():
    with pytest.raises(ValueError):
        rotation_sell_reason(
            _opportunity("ranked_margin"),
            protection_basis="noise_band_broken",
            protection_detail="x",
            headroom_pct=0.2,
            ceiling_pct=25.0,
            floor_pct=0.5,
        )


def test_injected_close_becomes_an_ordinary_full_sell_through_the_real_builder(tmp_path):
    """The appended target is built by `PortfolioConstructor._build_sell`
    like any PM close: a SELL for 100% carrying the rotation reason as its
    reasoning. That `TradeDecision` is what RiskStage (hard rules, Risk
    Manager refusal, holding-discipline check) and ExecutionStage
    (`_submit_protected_sell`) then gate — no separate path exists."""
    pipeline, _, _ = _pipeline(tmp_path)
    ctx = _ctx()
    decision = _decision(_buy_new())
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)
    close = decision.targets[1]
    trade = PortfolioConstructor._build_sell(close, _pos(), 10.0, 0.0)
    assert isinstance(trade, TradeDecision)
    assert trade.action == "SELL" and trade.allocation_pct == 100.0
    assert trade.reasoning.startswith("ROTATION (deterministic")
    assert "stopped earning its place" in trade.reasoning, "measured clauses survive truncation"
    assert trade.thesis_invalid_if == "closes below 100"


# ---------------------------------------------------------------------------
# Owner alert, buy-leg discipline, durable outcome
# ---------------------------------------------------------------------------


def _capture_alerts(monkeypatch) -> list[tuple[str, list[str] | None]]:
    sent: list[tuple[str, list[str] | None]] = []

    def _fake(text, *, symbols=None):
        sent.append((text, symbols))
        return True

    monkeypatch.setattr(notifier, "send_owner_alert", _fake)
    return sent


def _rotation_dict(**extra) -> dict:
    base = {
        "held_symbol": "OLD",
        "new_symbol": "NEW",
        "new_score": 1.8,
        "held_reasons": list(HELD_REASONS),
        "protection_basis": "structural_level_broken",
        "protection_detail": BROKEN_DETAIL,
        "headroom_pct": 0.2,
        "ceiling_pct": 25.0,
        "reason": _reason(),
    }
    base.update(extra)
    return base


def test_owner_alert_fires_with_the_sale_and_the_measured_reason(monkeypatch):
    sent = _capture_alerts(monkeypatch)
    _alert_rotation_executed(
        rotation=_rotation_dict(),
        qty=10.0,
        limit_price=94.53,
        order_id="brk-42",
        terminal_status="filled",
        filled_qty=10.0,
        avg_price=94.6,
    )
    ((text, symbols),) = sent
    assert text.startswith("POSITION CLOSED AUTOMATICALLY")
    assert "OLD" in text and "NEW" in text
    assert "10 share(s)" in text and "$94.53" in text and "brk-42" in text
    assert "R5 net evidence -1 if long" in text
    assert "structural_level_broken" in text
    assert "score 1.80" in text
    assert symbols == ["OLD", "NEW"]


def test_owner_alert_never_raises(monkeypatch):
    def _boom(text, *, symbols=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notifier, "send_owner_alert", _boom)
    _alert_rotation_executed(
        rotation=_rotation_dict(),
        qty=1.0,
        limit_price=1.0,
        order_id=None,
    )  # no exception


def _buy(symbol: str) -> TradeDecision:
    return TradeDecision(
        action="BUY",
        symbol=symbol,
        allocation_pct=5.0,
        entry_price=10.0,
        stop_loss=9.0,
        take_profit=12.0,
        reasoning="r",
    )


def test_buy_leg_dropped_when_the_rotation_close_did_not_fill(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    buys = [_buy("NEW"), _buy("OTHER")]
    kept = _drop_rotation_buy_if_room_not_freed(
        pipeline,
        ctx,
        buys,
        {"s1": "canceled"},
    )
    assert [d.symbol for d in kept] == ["OTHER"], "only the rotation's own BUY is dropped"
    (skip,) = ctx.execution_skips
    assert skip["symbol"] == "NEW" and skip["reason"] == "rotation_room_not_freed"
    assert "canceled" in skip["detail"]


def test_buy_leg_dropped_when_the_rotation_close_was_refused_upstream(tmp_path):
    """No `sell_order_id` on the rotation record means the close never
    reached the broker — Risk Manager refusal, hard-rule drop, or a
    protected-sell skip. The freed room is not real; the BUY must not
    proceed on it."""
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict()
    kept = _drop_rotation_buy_if_room_not_freed(pipeline, ctx, [_buy("NEW")], {})
    assert kept == []
    (skip,) = ctx.execution_skips
    assert skip["reason"] == "rotation_room_not_freed"
    assert "not submitted" in skip["detail"]


def test_buy_leg_kept_when_the_rotation_close_filled(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    buys = [_buy("NEW"), _buy("OTHER")]
    assert _drop_rotation_buy_if_room_not_freed(pipeline, ctx, buys, {"s1": "filled"}) is buys
    assert ctx.execution_skips == []


def test_no_rotation_means_buy_list_untouched(tmp_path):
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    buys = [_buy("NEW")]
    assert _drop_rotation_buy_if_room_not_freed(pipeline, ctx, buys, {}) is buys


def test_sold_but_not_bought_is_recorded_and_paged(tmp_path, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    ctx.execution_skips.append(
        {
            "symbol": "NEW",
            "reason": "insufficient_cash",
            "detail": "needs $500, has $20",
        }
    )
    _record_rotation_buy_leg_outcome(pipeline, ctx, orders=[])
    ((symbol, payload),) = _rotation_events(db)
    assert symbol == "NEW" and payload["outcome"] == "buy_not_submitted"
    assert payload["reason"] == "insufficient_cash: needs $500, has $20"
    ((text, symbols),) = sent
    assert text.startswith("ROTATION INCOMPLETE")
    assert "insufficient_cash" in text and symbols == ["OLD", "NEW"]


def test_sold_and_bought_is_recorded_and_not_paged(tmp_path, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict(sell_order_id="s1")
    _record_rotation_buy_leg_outcome(
        pipeline,
        ctx,
        orders=[{"id": "b1", "symbol": "NEW", "action": "BUY"}],
    )
    ((symbol, payload),) = _rotation_events(db)
    assert symbol == "NEW" and payload["outcome"] == "buy_submitted"
    assert sent == []


def test_outcome_is_not_recorded_when_no_rotation_sell_reached_the_broker(tmp_path, monkeypatch):
    sent = _capture_alerts(monkeypatch)
    pipeline, db, _ = _pipeline(tmp_path)
    ctx = _ctx()
    ctx.rotation = _rotation_dict()  # proposed, but refused upstream
    _record_rotation_buy_leg_outcome(pipeline, ctx, orders=[])
    assert _rotation_events(db) == [] and sent == []


# ---------------------------------------------------------------------------
# Every existing risk ceiling still binds
# ---------------------------------------------------------------------------


def test_a_close_frees_only_its_own_risk_and_the_ceiling_still_binds():
    """Closing OLD (4% of a book at 24.8%) leaves the other 20.8% committed:
    NEW asking 6% is capped at the 4.2% the ceiling actually leaves. The
    rotation is not a side door around `max_portfolio_risk_pct`."""
    allocation = allocate_risk_budget(
        [RiskRequest("OLD", 0.0), RiskRequest("NEW", 6.0)],
        existing_pct={"OLD": 4.0, "A": 10.4, "B": 10.4},
        ceiling_pct=25.0,
        floor_pct=0.5,
    )
    grant = allocation.grants["NEW"]
    assert grant.granted_pct == pytest.approx(4.2)
    assert grant.limited_by == "total_ceiling"


def test_same_session_close_now_reaches_the_allocator_as_a_zero_request():
    """The constructor fix this feature depends on: a full close is handed
    to `allocate_risk_budget` as the zero request it already documents, so
    the closed name's existing risk stops counting against the same
    session's BUY. Measured before the fix: NEW granted 0.00% here."""
    from tests.test_portfolio_constructor import _analysis

    constructor = PortfolioConstructor()
    targets = [
        TargetPosition(symbol="OLD", risk_allocation_pct=0.0, thesis="close"),
        TargetPosition(symbol="NEW", risk_allocation_pct=2.0, thesis="open"),
    ]
    analysis = _analysis("NEW", entry=100.0, stop=95.0, target=115.0)
    plans = constructor._plan_risk_targets(
        targets,
        analyses_by_sym={"NEW": analysis},
        price_map={"NEW": 100.0},
        current_weights={"OLD": 10.0},
        existing_risk_pct={"OLD": 24.8},
        clusters=None,
    )
    assert plans["OLD"].risk_pct == 0.0
    assert "NEW" in plans, "the BUY must not be denied for room the close frees"
    assert plans["NEW"].risk_pct == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# PM prompt wording
# ---------------------------------------------------------------------------


def test_pm_section_says_so_only_when_execution_is_enabled():
    from src.agents.portfolio_manager import PortfolioManagerAgent
    from src.verdicts import RankedCandidate

    kwargs = dict(
        ranked=[RankedCandidate(symbol="NEW", direction="bullish", score=1.8)],
        blocked={"OLD": list(HELD_REASONS)},
        held_symbols={"OLD"},
        existing_risk_pct={"OLD": 24.8},
        ceiling_pct=25.0,
    )
    off = PortfolioManagerAgent._render_rotation_section(**kwargs)
    on = PortfolioManagerAgent._render_rotation_section(**kwargs, execute_enabled=True)
    assert "AUTOMATIC ROTATION IS ENABLED" not in off
    assert "AUTOMATIC ROTATION IS ENABLED" in on
    # Board item 39 deliberately does NOT reword the categorical tier's
    # live prompt: "doing nothing is another [reasonable call]" is wrong
    # once the desk can close the name itself, but fixing it is a
    # behaviour change on a path that is trading today and belongs in its
    # own change. So this invariant still holds for this tier.
    assert on.startswith(off), "enabling only APPENDS a note; the comparison text is unchanged"
    assert "doing nothing is another" in off and "doing nothing is another" in on
    # And the precheck the pipeline acts on is the one the prompt was built from.
    precheck = PortfolioManagerAgent.rotation_precheck(**kwargs)
    assert precheck.opportunity is not None
    assert precheck.opportunity.tier == "ineligible_hold"
    assert precheck.opportunity.held_symbol == "OLD"
    assert precheck.headroom_pct == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# The pre-check is RECORDED, whatever it concluded
# ---------------------------------------------------------------------------
#
# The gap: `_apply_rotation_execution` returns silently when nothing was
# surfaced, and that is the desk's commonest rotation outcome. No log line,
# no durable row, nothing in the owner's report — a session that made the
# comparison looked identical to one that never ran it.


def _precheck_events(db, run_id="run-1"):
    return [e for _s, e in _rotation_events(db, run_id) if e.get("outcome") == "precheck"]


def test_a_surfaced_nothing_still_leaves_a_durable_precheck_row(tmp_path):
    from src.pipeline_stages import _record_rotation_precheck

    pipeline, db, _probe = _pipeline(tmp_path, precheck=_precheck(None))
    _record_rotation_precheck(pipeline, _ctx())
    rows = _precheck_events(db)
    assert len(rows) == 1
    assert rows[0]["reason"] == "full_nothing_outranked_a_holding"
    assert rows[0]["headroom_pct"] == pytest.approx(0.2)
    assert rows[0]["ceiling_pct"] == pytest.approx(25.0)


def test_the_precheck_row_is_written_even_with_rotation_execution_off(tmp_path):
    """The comparison happens in the PM's own prompt whether or not the desk
    may act on it, so the owner is owed the result either way — and the row
    records which of the two it was."""
    from src.pipeline_stages import _record_rotation_precheck

    pipeline, db, _probe = _pipeline(
        tmp_path,
        enabled=False,
        precheck=_precheck(None),
    )
    _record_rotation_precheck(pipeline, _ctx())
    rows = _precheck_events(db)
    assert len(rows) == 1
    assert rows[0]["execute_enabled"] is False


def test_the_precheck_row_names_the_two_symbols_when_one_was_surfaced(tmp_path):
    from src.pipeline_stages import _record_rotation_precheck

    pipeline, db, _probe = _pipeline(tmp_path)
    _record_rotation_precheck(pipeline, _ctx())
    rows = _precheck_events(db)
    assert len(rows) == 1
    assert rows[0]["reason"] == "full_candidate_outranked_a_holding"
    assert rows[0]["held_symbol"] == "OLD"
    assert rows[0]["new_symbol"] == "NEW"


def test_recording_the_precheck_never_raises(tmp_path):
    """Bookkeeping must not be able to end a live session."""
    from src.pipeline_stages import _record_rotation_precheck

    pipeline, db, _probe = _pipeline(tmp_path)
    pipeline.db = None  # any write failure at all
    _record_rotation_precheck(pipeline, _ctx())
    assert _precheck_events(db) == []


def test_precheck_outcome_covers_the_same_four_cases_the_prompt_branches_on(
    tmp_path,
):
    from src.rotation import (
        ROTATION_FULL_NOTHING_BETTER,
        ROTATION_FULL_OPPORTUNITY,
        ROTATION_ROOM_AVAILABLE,
        ROTATION_TELEMETRY_UNAVAILABLE,
        RotationPrecheck,
        precheck_outcome,
    )

    blind = RotationPrecheck(
        opportunity=None,
        headroom_pct=0.0,
        ceiling_pct=25.0,
        floor_pct=0.5,
        telemetry_available=False,
    )
    roomy = RotationPrecheck(
        opportunity=None,
        headroom_pct=4.0,
        ceiling_pct=25.0,
        floor_pct=0.5,
    )
    assert precheck_outcome(blind) == ROTATION_TELEMETRY_UNAVAILABLE
    assert precheck_outcome(roomy) == ROTATION_ROOM_AVAILABLE
    assert precheck_outcome(_precheck(None)) == ROTATION_FULL_NOTHING_BETTER
    assert precheck_outcome(_precheck(_opportunity())) == ROTATION_FULL_OPPORTUNITY
    # All four say something to the owner — none renders empty.
    from src.rotation import owner_precheck_lines, precheck_record

    for pre in (blind, roomy, _precheck(None), _precheck(_opportunity())):
        record = precheck_record(
            pre,
            execute_enabled=True,
            ranked_margin_enabled=False,
        )
        assert owner_precheck_lines(record), record["outcome"]


# ---------------------------------------------------------------------------
# 2026-09-23 — the near-miss DETAIL rides in that same row.
#
# The pre-check row above records THAT a comparison happened and what it
# concluded. It did not record WHICH holding was weighed against which
# candidate, on what seats, at what ratio, or which of the seven refusal
# points was hit — and board item 39(a), the open question behind the
# unmeasured 25% margin, cannot be answered from anything else. Folded into
# the one row rather than written as a second: one session's one comparison
# is one fact.
# ---------------------------------------------------------------------------


def _refusal_precheck(point="book_not_constrained", **over):
    from src.rotation import RotationPrecheck, RotationRefusal

    fields = dict(
        point=point,
        detail="real room exists on every constraint",
        held_symbol="OLD",
        new_symbol="NEW",
        held_score=0.9,
        new_score=1.8,
        shared_seats=("earnings", "technical"),
        held_shared_score=0.9,
        new_shared_score=1.8,
        ratio=2.0,
        binding=("funding",),
    )
    fields.update(over)
    return RotationPrecheck(
        opportunity=None,
        headroom_pct=14.5,
        ceiling_pct=25.0,
        floor_pct=0.5,
        refusal=RotationRefusal(**fields),
        entry_budget_usd=92.20,
        min_entry_usd=500.0,
        binding=fields["binding"],
    )


def _refusal_pipeline(tmp_path, precheck, *, enabled=False):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    pipeline = build_pipeline(db=db, portfolio_manager=SimpleNamespace(last_rotation_precheck=precheck))
    pipeline.config = SimpleNamespace(
        execution=SimpleNamespace(rotation_enabled=enabled),
    )
    return pipeline, db


def test_the_near_miss_row_carries_every_named_field(tmp_path):
    from src.pipeline_stages import _record_rotation_precheck

    pipeline, db = _refusal_pipeline(tmp_path, _refusal_precheck())
    _record_rotation_precheck(pipeline, _ctx())
    rows = _rotation_events(db)
    assert len(rows) == 1, "one session, one comparison, one row"
    symbol, event = rows[0]
    assert symbol is None, (
        "RUN-scoped, with the symbols in the payload. A symbol-scoped row "
        "here would be read by src/refusal_signature.py as a candidate the "
        "session considered as a new idea — a weakest HOLDING is not one, "
        "and this row fires every session, so it would break the "
        "monomorphic-refusal streak on essentially every run and disarm the "
        "jam alarm. Board item 164 and exit_path_records.py ruled the same "
        "way twice before."
    )
    assert event["reason"] == "full_nothing_outranked_a_holding"
    assert event["refusal_point"] == "book_not_constrained"
    assert event["held_symbol"] == "OLD"
    assert event["new_symbol"] == "NEW"
    assert event["held_score"] == 0.9 and event["new_score"] == 1.8
    assert event["shared_seats"] == "earnings,technical"
    assert event["held_shared_score"] == 0.9
    assert event["new_shared_score"] == 1.8
    assert event["ratio"] == 2.0
    assert event["margin_pct"] == 0.25
    assert event["binding"] == "funding"
    assert event["entry_budget_usd"] == 92.20
    assert event["min_entry_usd"] == 500.0
    assert event["detail"]


def test_every_refusal_point_lands_under_its_own_code(tmp_path):
    """One code per point, grouped on the CODE rather than on prose — the
    same `refusal`/`note` split `_record_accounted_candidate` uses."""
    from src.pipeline_stages import _record_rotation_precheck
    from src.rotation import ROTATION_REFUSAL_POINTS

    seen = []
    for i, point in enumerate(ROTATION_REFUSAL_POINTS):
        (tmp_path / f"p{i}").mkdir(parents=True, exist_ok=True)
        pipeline, db = _refusal_pipeline(
            tmp_path / f"p{i}",
            _refusal_precheck(point=point),
        )
        _record_rotation_precheck(pipeline, _ctx())
        rows = _rotation_events(db)
        assert len(rows) == 1, point
        seen.append(rows[0][1]["refusal_point"])
    assert seen == list(ROTATION_REFUSAL_POINTS)


def test_the_near_miss_detail_is_written_with_the_execution_flag_OFF(tmp_path):
    """Recording is not acting. Gating the dataset on the switch it exists
    to inform would mean it only starts existing once the decision it
    informs has already been taken."""
    from src.pipeline_stages import _record_rotation_precheck

    pipeline, db = _refusal_pipeline(tmp_path, _refusal_precheck(), enabled=False)
    _record_rotation_precheck(pipeline, _ctx())
    assert _rotation_events(db)[0][1]["refusal_point"] == "book_not_constrained"


def test_a_surfaced_opportunity_carries_no_refusal_point(tmp_path):
    from src.pipeline_stages import _record_rotation_precheck

    pipeline, db = _refusal_pipeline(tmp_path, _precheck(_opportunity()))
    _record_rotation_precheck(pipeline, _ctx())
    event = _rotation_events(db)[0][1]
    assert event["reason"] == "full_candidate_outranked_a_holding"
    assert "refusal_point" not in event


def test_the_owner_line_names_the_limit_that_is_actually_binding():
    """2026-09-17 CRM, from the reporting side. Quoting risk headroom while
    the real cause is $92 against a $500 minimum is a true-sounding
    sentence about the wrong number."""
    from src.rotation import owner_precheck_lines, precheck_record

    record = precheck_record(
        _refusal_precheck(),
        execute_enabled=False,
        ranked_margin_enabled=False,
    )
    text = " ".join(owner_precheck_lines(record))
    assert "the book is FULL" in text
    assert "$92 of cash and borrowing room" in text
    assert "below the $500 that the owner's 0.5% minimum risk per position needs" in text
    assert "14.50% of risk headroom" not in text


def test_a_book_full_only_on_funding_is_not_reported_as_having_room():
    """THE REGRESSION, at the reporting layer. `precheck_outcome` tested
    risk headroom alone and so called 14.50% "room available" on the day
    the book could not fund a $500 order."""
    from src.rotation import ROTATION_FULL_NOTHING_BETTER, precheck_outcome

    assert precheck_outcome(_refusal_precheck()) == ROTATION_FULL_NOTHING_BETTER


def test_an_unread_funding_view_is_not_reported_as_having_room():
    """Adversary review 2026-09-23. `binding == ()` means "nothing bound",
    and the owner line rendered that as "enough cash and borrowing room to
    open a new position" — asserted from a figure that came back
    unreadable. The direction is adverse: the ladder is unreadable exactly
    when execution has fallen back to raw settled cash, which on a 2x book
    is near zero."""
    from src.rotation import RotationPrecheck, owner_precheck_lines, precheck_record

    unread = RotationPrecheck(
        opportunity=None,
        headroom_pct=14.5,
        ceiling_pct=25.0,
        floor_pct=0.5,
        entry_budget_usd=None,
        min_entry_usd=500.0,
        binding=(),
    )
    text = " ".join(
        owner_precheck_lines(
            precheck_record(
                unread,
                execute_enabled=False,
                ranked_margin_enabled=False,
            )
        )
    )
    assert "could NOT read how much cash" in text
    assert "enough cash and borrowing room" not in text


def test_the_sale_reason_names_the_limit_that_actually_bound():
    """Adversary review 2026-09-23, and the finding that would not have been
    merged past: the string written onto the broker order and handed to the
    Risk Manager said "Headroom 14.50% ... under the 0.50% minimum" on a
    funding-bound rotation. That is a false arithmetic claim on the audit
    record of a live sale, produced only by this change."""
    from src.rotation import rotation_sell_reason

    reason = rotation_sell_reason(
        _opportunity(),
        protection_basis="structural_level_broken",
        protection_detail=BROKEN_DETAIL,
        headroom_pct=14.50,
        ceiling_pct=25.0,
        floor_pct=0.5,
        binding=("funding",),
        entry_budget_usd=92.20,
        min_entry_usd=500.0,
    )
    # Since the 2026-10-01 ruling the categorical sale does not rest on the
    # book being constrained at all, so it states no capital claim — which
    # is the strongest possible form of "no false arithmetic claim".
    assert "under the 0.50% minimum" not in reason
    assert "deployable" not in reason
    assert "stopped earning its place" in reason


# ---------------------------------------------------------------------------
# OWNER RULING 2026-10-01 — a below-bar holding with NO replacement candidate.
# `evaluate_rotation` now reaches the categorical tier BEFORE the "nothing
# better was found" refusal, so `new_symbol` is None on exactly this case.
# Every reader on the execution path has to tolerate it; the first one used
# to crash with AttributeError before the sale was even proposed.
# ---------------------------------------------------------------------------


def _opportunity_no_replacement() -> RotationOpportunity:
    return RotationOpportunity(
        new_symbol=None,
        new_score=None,
        held_symbol="OLD",
        held_score=None,
        tier="ineligible_hold",
        reasons=HELD_REASONS,
    )


def test_below_bar_holding_with_no_replacement_candidate_is_sold(tmp_path):
    pipeline, db, probe = _pipeline(
        tmp_path,
        precheck=_precheck(_opportunity_no_replacement()),
    )
    ctx = _ctx()
    decision = _decision()
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)

    assert [t.symbol for t in decision.targets] == ["OLD"]
    close = decision.targets[0]
    assert close.is_close and close.risk_allocation_pct == 0.0
    assert ctx.rotation["held_symbol"] == "OLD"
    assert ctx.rotation["new_symbol"] is None
    assert ctx.rotation["new_score"] is None

    ((symbol, payload),) = _rotation_events(db)
    assert symbol == "OLD"
    assert payload["outcome"] == "proposed"
    assert payload["new_symbol"] is None


def test_the_owner_alert_for_a_replacementless_cull_is_true_and_renders(
    tmp_path,
    monkeypatch,
):
    """The sale must reach the owner WITH its real reason, and must not
    claim it freed room for a replacement that does not exist."""
    from src import notifier as _notifier
    from src import pipeline_stages as _ps

    pipeline, db, probe = _pipeline(
        tmp_path,
        precheck=_precheck(_opportunity_no_replacement()),
    )
    ctx = _ctx()
    decision = _decision()
    _apply_rotation_execution(pipeline, ctx, decision, [_pos()], HISTORY)

    sent: list = []
    monkeypatch.setattr(
        _notifier,
        "send_owner_alert",
        lambda text, symbols=None: sent.append((text, symbols)) or True,
    )
    _ps._alert_rotation_executed(
        rotation=ctx.rotation,
        qty=10.0,
        limit_price=95.0,
        order_id="o-1",
    )
    ((text, symbols),) = sent
    assert symbols == ["OLD"]
    assert "None" not in text
    assert "free room for" not in text
    assert "NO replacement" in text
    assert HELD_REASONS[0] in text


def test_no_replacement_leg_is_recorded_and_never_paged_as_a_missing_buy(
    tmp_path,
    monkeypatch,
):
    from src import pipeline_stages as _ps

    pipeline, db, probe = _pipeline(
        tmp_path,
        precheck=_precheck(_opportunity_no_replacement()),
    )
    ctx = _ctx()
    _apply_rotation_execution(pipeline, ctx, _decision(), [_pos()], HISTORY)
    ctx.rotation["sell_order_id"] = "o-1"

    paged: list = []
    monkeypatch.setattr(
        _ps,
        "_alert_rotation_buy_leg_missing",
        lambda **kw: paged.append(kw),
    )
    _ps._record_rotation_buy_leg_outcome(pipeline, ctx, [])
    assert paged == []
    outcomes = [p["outcome"] for _, p in _rotation_events(db)]
    assert "no_replacement_leg" in outcomes
    assert "buy_not_submitted" not in outcomes


# ---------------------------------------------------------------------------
# BUY-side anti-churn: the mirror of `held_symbol_bought_today`.
# ---------------------------------------------------------------------------


def test_a_name_sold_today_below_the_bar_is_not_bought_back_the_same_day(
    tmp_path,
):
    from src import pipeline_stages as _ps

    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    _ps._record_pipeline_event(
        pipeline,
        ctx,
        "OLD",
        "rotation",
        "sell_submitted",
        "because",
    )
    buys = [
        TargetPosition(symbol="OLD", risk_allocation_pct=2.0, thesis="back in"),
        TargetPosition(symbol="NEW", risk_allocation_pct=2.0, thesis="fine"),
    ]
    kept = _ps._drop_buys_sold_today_below_bar(pipeline, ctx, buys)
    assert [d.symbol for d in kept] == ["NEW"]
    assert [s["reason"] for s in ctx.execution_skips] == [
        "sold_today_below_entry_bar",
    ]


def test_the_rebuy_guard_fails_open_when_it_cannot_read_its_own_record(
    tmp_path,
):
    from src import pipeline_stages as _ps

    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()

    def _boom():
        raise RuntimeError("db gone")

    pipeline.db = SimpleNamespace(get_rotation_sell_symbols_today=_boom)
    buys = [TargetPosition(symbol="OLD", risk_allocation_pct=2.0, thesis="t")]
    assert _ps._drop_buys_sold_today_below_bar(pipeline, ctx, buys) == buys


def test_a_rotation_sale_from_another_day_does_not_block_the_buy(tmp_path):
    from src import pipeline_stages as _ps

    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    _ps._record_pipeline_event(
        pipeline,
        ctx,
        "OLD",
        "rotation",
        "sell_submitted",
        "because",
    )
    db.conn.execute(
        "UPDATE specialist_evidence SET timestamp = '2020-01-02T15:00:00+00:00'",
    )
    db.conn.commit()
    buys = [TargetPosition(symbol="OLD", risk_allocation_pct=2.0, thesis="t")]
    assert [d.symbol for d in _ps._drop_buys_sold_today_below_bar(pipeline, ctx, buys)] == ["OLD"]


# ---------------------------------------------------------------------------
# The PROMPT side of the replacementless case. Three tests above exercise the
# replacementless execution path and none of them rendered the prompt, which
# is why `_render_rotation_section` kept crashing on `new_score=None` inside
# `build_user_message`, BEFORE the decision stage was ever reached.
# ---------------------------------------------------------------------------


def _render_replacementless(*, execute_enabled: bool) -> str:
    from src.agents.portfolio_manager import PortfolioManagerAgent

    return PortfolioManagerAgent._render_rotation_section(
        ranked=[],
        blocked={},
        held_symbols={"OLD"},
        existing_risk_pct={"OLD": 2.0},
        ceiling_pct=25.0,
        precheck=_precheck(_opportunity_no_replacement()),
        execute_enabled=execute_enabled,
    )


@pytest.mark.parametrize("execute_enabled", [False, True])
def test_the_prompt_renders_for_a_below_bar_holding_with_no_replacement(
    execute_enabled,
):
    """Crashed with `TypeError: unsupported format string passed to
    NoneType.__format__` before the fix, on both switch settings."""
    text = _render_replacementless(execute_enabled=execute_enabled)
    assert "OLD" in text
    assert "None" not in text
    assert HELD_REASONS[0] in text


def test_the_categorical_prompt_states_what_the_code_actually_does():
    """OWNER RULING 2026-10-01 removed BOTH preconditions this paragraph
    used to assert. Prompt text is code that can rot."""
    text = _render_replacementless(execute_enabled=True)
    assert "AUTOMATIC ROTATION IS ENABLED" in text
    assert "None" not in text
    # The two REMOVED conditions must not be asserted any more.
    assert "protection has already broken" not in text
    assert "if you include a BUY target" not in text.lower()
    # The two that remain in the code must still be stated.
    assert "not bought today" in text
    assert "in flight" in text


# ---------------------------------------------------------------------------
# A fail-open anti-churn guard must leave a trace somebody can count.
# ---------------------------------------------------------------------------


def _failed_open(db) -> list:
    return [(sym, payload) for sym, payload in _rotation_events(db) if payload["outcome"] == "rebuy_guard_failed_open"]


def test_an_unreadable_anti_churn_record_leaves_a_durable_reason(
    tmp_path,
    monkeypatch,
):
    from src import pipeline_stages as _ps

    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()

    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(
        pipeline.db,
        "get_rotation_sell_symbols_today",
        _boom,
    )
    real_db = pipeline.db
    buys = [TargetPosition(symbol="OLD", risk_allocation_pct=2.0, thesis="t")]
    # Fail-open behaviour is UNCHANGED: the buy still proceeds.
    assert _ps._drop_buys_sold_today_below_bar(pipeline, ctx, buys) == buys

    ((symbol, payload),) = _failed_open(real_db)
    assert symbol == "OLD"
    assert payload["failure"] == "record_unreadable"
    assert "could not read" in payload["reason"]


def test_a_lost_anti_churn_write_leaves_a_durable_reason(tmp_path):
    """`_persist_evidence` swallows write failures, so the `sell_submitted`
    row can be LOST as well as unreadable. Same silent hole, other half.

    2026-10-01: the session's own rotation result proves the name was sold
    today, so the guard now CLOSES on that fact instead of only recording
    itself open. The durable row stays, and still says what happened.
    """
    from src import pipeline_stages as _ps

    pipeline, db, probe = _pipeline(tmp_path)
    ctx = _ctx()
    # A rotation sale WAS submitted this session, but no durable record of
    # it can be read back — the write was lost.
    ctx.rotation = {"held_symbol": "OLD", "sell_order_id": "o-1"}
    buys = [
        TargetPosition(symbol="OLD", risk_allocation_pct=2.0, thesis="t"),
        TargetPosition(symbol="FRESH", risk_allocation_pct=2.0, thesis="t"),
    ]
    kept = _ps._drop_buys_sold_today_below_bar(pipeline, ctx, buys)
    # The same-day re-buy is STOPPED; every other buy is untouched.
    assert [d.symbol for d in kept] == ["FRESH"]

    ((symbol, payload),) = [
        (sym, p) for sym, p in _rotation_events(db) if p["outcome"] == "rebuy_guard_closed_from_session_fact"
    ]
    assert symbol == "OLD"
    assert payload["failure"] == "record_unwritten"
    assert "still stopped" in payload["reason"]
