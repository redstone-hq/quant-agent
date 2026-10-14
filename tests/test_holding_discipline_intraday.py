"""The midday / close exit surfaces fact-check their hard-trigger claims.

THE DEFECT (2026-09-11). To SELL / REDUCE / COVER a structurally protected
position the reviewer must NAME a hard trigger. Until this landed, "name" was
the whole of it on the intraday surfaces: `pipeline._reason_cites_hard_trigger`
is a case-insensitive SUBSTRING match over free LLM prose, and nothing
downstream of it ever asked whether the named trigger had actually happened.

The deterministic answer already existed — `exit_guard.holding_discipline_claim_check`,
which cross-checks a claimed regime flip against the day's real macro read and
a claimed HIGH-conviction bearish state change against real Active News State
Change rows. It was imported from exactly ONE place in the codebase, the
morning Portfolio-Manager path in `pipeline_stages.RiskStage`. Midday and
close — the desk's two busiest exit surfaces — were never wired to it, so
"regime shift to risk-off" sold a protected position on the strength of the
words alone. (The same sentence used to carry "correlation breach across the
book"; that phrase was removed from the accepted trigger vocabulary entirely
on 2026-09-13 — WORK.md item 44 — because nothing could ever check it.)

WHAT MUST NOT REGRESS. The checker deliberately separates PROVABLY FALSE from
UNVERIFIABLE, and only the first blocks. An unverifiable claim passing through
is the whole point: refusing an exit on a claim we merely CANNOT CHECK would
strand the desk in a losing position, which is a worse failure than the one
these tests pin. Every "passes through" test below is guarding that, not
incidental coverage.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.models import (
    Position,
    PositionAction,
    PositionReasoningChain,
    PositionReview,
)
from src.pipeline import TradingPipeline
from src.risk.exit_guard import HoldingDisciplineClaimCheck
from src.trading_calendar import et_today
from tests.pipeline_factory import build_pipeline


# ---------------------------------------------------------------------------
# Fixtures — a pipeline double with only what this gate touches
# ---------------------------------------------------------------------------


def _position(symbol="AAA", qty=10, avg_entry=100.0, current_price=101.0):
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        current_price=current_price,
        market_value=qty * current_price,
        unrealized_pnl=qty * (current_price - avg_entry),
        sector="Technology",
    )


def _protection(protected: bool):
    """Stand-in for `_structural_protection_for_holding`'s return value.

    The structural read has its own suite (`test_holding_discipline_block.py`,
    `test_structural_protection.py`); what these tests pin is the WIRING of
    the claim check into the intraday executor, so the protection verdict is
    supplied rather than re-derived from bars.
    """
    stub = MagicMock()
    stub.protected = protected
    stub.basis = "stop_backed_by_level"
    stub.detail = "test fixture"
    stub.raw_broken = False
    stub.confirmed_chart_break = not protected
    return stub


def _pipeline(*, macro_state=None, protected=True, state_changes=None):
    p = build_pipeline(
        db=MagicMock(),
        broker=MagicMock(),
        market=MagicMock(),
        tech_store=MagicMock(),
        macro_store=MagicMock(),
        news_store=MagicMock(),
    )
    p.db.get_symbol_last_buy.return_value = {
        "price": 100.0,
        "stop_loss": 94.0,
        "thesis_invalid_if": "loses the 94 shelf on a close",
        "timestamp": f"{et_today().isoformat()} 14:00:00",
    }
    p.broker.get_current_stop_price.return_value = None
    p.tech_store.get_history.return_value = []
    p.macro_store.load_last_state.return_value = macro_state
    p.news_store.recent_state_changes.return_value = state_changes or []
    p._atr_for_symbol = MagicMock(return_value=2.0)
    p._structural_protection_for_holding = MagicMock(return_value=_protection(protected))
    return p


def _review(action="SELL", symbol="AAA", reason="regime shift to risk-off"):
    return PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="flipped",
            thesis_progress_check="on track",
            thesis_integrity_check="intact",
            winners_discipline_check="n/a",
            session_disposition_check="intraday",
            execution_rationale="exit",
        ),
        actions=[PositionAction(action=action, symbol=symbol, reason=reason)],
        overall_assessment="de-risking",
        risk_level="moderate",
    )


def _statuses(pipeline):
    return [call.kwargs.get("status") for call in pipeline.db.record_intraday_evaluation.call_args_list]


def _execute(pipeline, review, run_id):
    return pipeline._midday_execute_llm_actions(
        positions=[_position("AAA")],
        review=review,
        run_id=run_id,
    )


#: Today's macro read, risk-ON. A SELL claiming a flip TO risk-off today is
#: provably contradicted by it. `_carry_forward_macro` only accepts a state
#: dated today, which is what makes this a TRUSTED read rather than a stale one.
def _macro_today(
    regime,
):  # read at RUN time, never at import: a collection-time stamp compared to a run-time et_today() reds this file whenever the suite crosses ET midnight between collecting and running it (reproduced 2026-10-02)
    return {"date": str(et_today()), "regime": regime}


#: Nothing stored, or stored from a previous day — no read to check against.
_MACRO_STALE = {"date": "2020-01-02", "regime": "risk-on"}


# ---------------------------------------------------------------------------
# 1. A PROVABLY FALSE claim is now caught — on BOTH intraday surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", ["midday-2026-09-11", "close-2026-09-11"])
def test_provably_false_regime_claim_blocks_the_intraday_exit(run_id):
    """The defect, reproduced and then closed. "regime shift to risk-off"
    clears the substring gate (`_HARD_TRIGGER_KEYWORDS` lists "regime shift"),
    and before this fix that was the last word: the order went out. Today's
    real macro read says risk-ON, so the claim is provably false and the exit
    is dropped with an audit row naming why.

    Parametrised over a midday and a close run_id because
    `run_position_review` — the single entry `run_midday` and `run_close` both
    delegate to — dispatches both sessions through this one executor.
    """
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)

    orders = _execute(pipeline, _review(), run_id)

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert "exit_blocked_holding_discipline_claim_false" in _statuses(pipeline)


@pytest.mark.parametrize("run_id", ["midday-2026-09-11", "close-2026-09-11"])
def test_provably_false_bearish_state_change_claim_blocks_the_intraday_exit(run_id):
    """The other checkable claim, on the same surfaces. The reason asserts a
    HIGH-conviction bearish state change; today's real Active News State
    Change row names AAA with direction BULLISH. Contradicted, so blocked."""
    pipeline = _pipeline(
        macro_state=None,
        protected=True,
        state_changes=[
            {
                "first_seen_date": str(et_today()),
                "event": "upgrade cycle confirmed",
                "affected_symbols": ["AAA"],
                "symbol_direction": {"AAA": "bullish"},
            }
        ],
    )

    orders = _execute(
        pipeline,
        _review(reason="high-conviction bearish state change on the name"),
        run_id,
    )

    assert orders == []
    pipeline.broker.submit_order.assert_not_called()
    assert "exit_blocked_holding_discipline_claim_false" in _statuses(pipeline)


def test_the_morning_path_is_not_the_only_importer_any_more():
    """Pins the defect's shape directly: the fact-checker used to be reachable
    from `pipeline_stages` alone. The midday/close executor — moved to
    `src/pipeline_exits.py` by step 4 of docs/PIPELINE_SPLIT_PLAN.md — must
    now reach it too. The executor's fact-check phase was lifted verbatim into
    `src/exits_parts/midday_holding_discipline.py` (2026-10-09), so the scan
    reads that module and pins that the executor still calls it."""
    import inspect

    import src.exits_parts.midday_holding_discipline as phase_module
    from src.pipeline_exits import ExitEngineMixin

    source = inspect.getsource(phase_module)
    assert "holding_discipline_claim_check" in source
    assert "self._holding_discipline_check_for_exit(" in source
    assert "midday_holding_discipline(" in inspect.getsource(ExitEngineMixin._midday_execute_llm_actions)


def test_midday_and_close_share_the_one_executor_this_gate_lives_in():
    """Why the parametrised tests above are honest coverage of BOTH surfaces
    rather than one surface twice: `run_midday` and `run_close` are both thin
    delegations to `run_position_review`, which dispatches every LLM exit
    through `_midday_execute_llm_actions`. One gate, both sessions.

    `run_position_review` itself became a thin persistence wrapper around
    `_run_position_review_body` on 2026-09-18 (see `Database.
    save_session_report` — the same shape `run_evening`/`_run_evening_body`
    already used), so the executor call now lives in the body."""
    import inspect

    assert "run_position_review" in inspect.getsource(TradingPipeline.run_midday)
    assert "run_position_review" in inspect.getsource(TradingPipeline.run_close)
    assert "_run_position_review_body" in inspect.getsource(
        __import__("src.pipeline_parts.review", fromlist=["review"]).run_position_review
    )
    review_src = inspect.getsource(
        __import__("src.sessions.position_review_session", fromlist=["PositionReviewSession"]).PositionReviewSession.run
    )
    assert review_src.count("_midday_execute_llm_actions(") == 1


# ---------------------------------------------------------------------------
# 2. THE REGRESSION THAT WOULD HURT MOST — unverifiable still passes through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", ["midday-2026-09-11", "close-2026-09-11"])
def test_unverifiable_regime_claim_still_passes_through(run_id):
    """No macro read is stored for today (no macro analyst runs at midday or
    close, and nothing was carried forward), so the regime claim can be
    neither confirmed nor denied. It must be RECORDED and ALLOWED — blocking
    an exit on a claim we cannot check would trap the desk in the position."""
    pipeline = _pipeline(macro_state=None, protected=True)

    _execute(pipeline, _review(reason="regime shift to risk-off; thesis invalidated"), run_id)

    statuses = _statuses(pipeline)
    assert "holding_discipline_claim_unverified" in statuses
    assert "exit_blocked_holding_discipline_claim_false" not in statuses


@pytest.mark.parametrize("run_id", ["midday-2026-09-11", "close-2026-09-11"])
def test_a_stale_macro_read_is_unverifiable_not_false(run_id):
    """A regime stored on a PREVIOUS day is not evidence about today. It must
    never be used to call today's claim false — `_carry_forward_macro` refuses
    it, and the claim lands as unverifiable rather than contradicted."""
    pipeline = _pipeline(macro_state=_MACRO_STALE, protected=True)

    _execute(pipeline, _review(reason="regime shift to risk-off; thesis invalidated"), run_id)

    statuses = _statuses(pipeline)
    assert "holding_discipline_claim_unverified" in statuses
    assert "exit_blocked_holding_discipline_claim_false" not in statuses


@pytest.mark.parametrize("run_id", ["midday-2026-09-11", "close-2026-09-11"])
def test_unverifiable_bearish_state_change_still_passes_through(run_id):
    """No same-day state-change row names the symbol either way. The news
    pipeline can simply not have logged a real catalyst as a formal row yet,
    so "not found" is not "false"."""
    pipeline = _pipeline(macro_state=None, protected=True, state_changes=[])

    _execute(
        pipeline,
        _review(reason="high-conviction bearish state change on the name"),
        run_id,
    )

    statuses = _statuses(pipeline)
    assert "holding_discipline_claim_unverified" in statuses
    assert "exit_blocked_holding_discipline_claim_false" not in statuses


def test_an_infrastructure_failure_inside_the_check_fails_open():
    """Same reasoning as the unverifiable case, one layer down: if the check
    itself cannot run, the exit proceeds unverified rather than being blocked
    on an error."""
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)
    pipeline._structural_protection_for_holding = MagicMock(side_effect=RuntimeError("bars store down"))

    _execute(pipeline, _review(), "midday-2026-09-11")

    assert "exit_blocked_holding_discipline_claim_false" not in _statuses(pipeline)


# ---------------------------------------------------------------------------
# 3. A legitimate, verifiable exit still executes normally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", ["midday-2026-09-11", "close-2026-09-11"])
def test_a_confirmed_regime_flip_executes_normally(run_id):
    """Today's macro read agrees: the regime really is risk-off. The claim is
    CONFIRMED, the checker says "ok", and neither audit row is written."""
    pipeline = _pipeline(macro_state=_macro_today("risk-off"), protected=True)

    _execute(pipeline, _review(reason="regime shift to risk-off; thesis invalidated"), run_id)

    statuses = _statuses(pipeline)
    assert "exit_blocked_holding_discipline_claim_false" not in statuses
    assert "holding_discipline_claim_unverified" not in statuses


@pytest.mark.parametrize("run_id", ["midday-2026-09-11", "close-2026-09-11"])
def test_a_confirmed_regime_flip_on_market_mood_alone_is_refused(run_id):
    """Owner mandate 2026-10-09: even a confirmed risk-off read cannot decide
    the exit of a name whose own level is intact."""
    pipeline = _pipeline(macro_state=_macro_today("risk-off"), protected=True)

    _execute(pipeline, _review(), run_id)

    assert "exit_blocked_holding_discipline_claim_false" in _statuses(pipeline)


def test_a_confirmed_bearish_state_change_executes_normally():
    pipeline = _pipeline(
        macro_state=None,
        protected=True,
        state_changes=[
            {
                "first_seen_date": str(et_today()),
                "event": "guidance withdrawn",
                "affected_symbols": ["AAA"],
                "symbol_direction": {"AAA": "bearish"},
            }
        ],
    )

    _execute(
        pipeline,
        _review(reason="high-conviction bearish state change on the name"),
        "midday-2026-09-11",
    )

    statuses = _statuses(pipeline)
    assert "exit_blocked_holding_discipline_claim_false" not in statuses
    assert "holding_discipline_claim_unverified" not in statuses


def test_an_unprotected_position_is_not_this_gates_business():
    """`protected=False` means the thesis-backing level has broken and been
    confirmed. A plain SELL there needs no special justification, so the check
    returns "ok" whatever the macro read says — unchanged from the morning
    path's semantics."""
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=False)

    _execute(pipeline, _review(), "midday-2026-09-11")

    statuses = _statuses(pipeline)
    assert "exit_blocked_holding_discipline_claim_false" not in statuses
    assert "holding_discipline_claim_unverified" not in statuses


# ---------------------------------------------------------------------------
# 4. The check buys nothing it does not need
# ---------------------------------------------------------------------------


def test_a_thesis_invalidation_exit_now_consults_the_structural_check():
    """THE 2026-09-14 DEFECT (WORK.md item 60), reproduced by inversion.

    A thesis-invalidation exit makes neither claim
    `holding_discipline_claim_check` can adjudicate, and the assembler used
    to return before the structural read on that basis. But thesis
    invalidation is precisely the exit class where "did the level backing
    this stop actually break?" IS the question — and the only class for
    which the ATR noise band is not already redundant (21 of the 26
    hard-trigger keywords bypass the band via
    `cites_external_information`). The desk computes the answer and threw
    it away here.

    The read now happens, READ-ONLY, and lands in the evidence ledger.
    """
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)

    _execute(
        pipeline,
        _review(reason="thesis_invalid triggered — lost the level on the close"),
        "midday-2026-09-11",
    )

    pipeline._structural_protection_for_holding.assert_called_once()
    # Read-only: filing today's break from this NEW call site would let a
    # break confirm a session early, which lifts `protected` a session
    # early, which can release an exit that is blocked today.
    assert pipeline._structural_protection_for_holding.call_args.kwargs["persist"] is False
    kinds = [call.kwargs.get("kind") for call in pipeline.db.insert_specialist_evidence.call_args_list]
    assert "thesis_invalidation_structural_check" in kinds


def test_the_thesis_invalidation_read_never_blocks_or_releases_an_exit():
    """Strictly additive. Whatever the structural check says, the exit is
    handled exactly as it was before this branch existed: the assembler
    still returns None for an (a)-only reason, so no verdict reaches the
    caller and no audit row claims one."""
    for protected in (True, False):
        pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=protected)

        verdict = pipeline._holding_discipline_check_for_exit(
            symbol="AAA",
            action="SELL",
            reason="thesis_invalid_if triggered on the close",
            positions=[_position("AAA")],
            run_id="midday-2026-09-11",
            position_history={"AAA": {}},
        )

        assert verdict is None
        statuses = _statuses(pipeline)
        assert "exit_blocked_holding_discipline_claim_false" not in statuses
        assert "holding_discipline_claim_unverified" not in statuses


def test_a_reason_making_no_recognised_claim_at_all_still_short_circuits():
    """The cost-saving short-circuit survives for reasons that name neither
    (a), (b) nor (c) — there is still nothing for the structural read to
    inform."""
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)

    pipeline._holding_discipline_check_for_exit(
        symbol="AAA",
        action="SELL",
        reason="stopped out at the broker",
        positions=[_position("AAA")],
        run_id="midday-2026-09-11",
        position_history={"AAA": {}},
    )

    pipeline._structural_protection_for_holding.assert_not_called()


def test_a_denied_thesis_invalidation_is_not_a_claim():
    """Same negation handling the other two claim predicates get: "no thesis
    invalidation has occurred" asserts nothing."""
    from src.risk.exit_guard import claims_thesis_invalidation

    assert claims_thesis_invalidation("thesis_invalid_if triggered") is True
    assert claims_thesis_invalidation("thesis invalidated on the close") is True
    assert claims_thesis_invalidation("broken thesis") is True
    assert claims_thesis_invalidation("no thesis invalidation has occurred") is False
    assert claims_thesis_invalidation("trimming into strength") is False


def test_a_hold_only_review_buys_no_entry_context_reads():
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)

    orders = _execute(pipeline, _review(action="HOLD"), "midday-2026-09-11")

    assert orders == []
    pipeline.db.get_symbol_last_buy.assert_not_called()
    pipeline._structural_protection_for_holding.assert_not_called()


# ---------------------------------------------------------------------------
# 5. The morning path is untouched
# ---------------------------------------------------------------------------


def test_the_checker_itself_is_unchanged_on_the_split_that_matters():
    """Guards the one semantic this change must not have moved: `.blocks` is
    true for a proven-false verdict and for nothing else."""
    assert HoldingDisciplineClaimCheck("false").blocks is True
    assert HoldingDisciplineClaimCheck("unverifiable", "noted").blocks is False
    assert HoldingDisciplineClaimCheck("ok").blocks is False


def test_the_intraday_assembler_passes_no_macro_status_when_none_is_stored():
    """No fabricated input: with nothing carried forward, `macro_status` must
    reach the checker as None (-> unverifiable), never defaulted to a trusted
    value that would let a stale or absent read call a claim false."""
    pipeline = _pipeline(macro_state=None, protected=True)

    with patch("src.risk.exit_guard.holding_discipline_claim_check") as spy:
        spy.return_value = HoldingDisciplineClaimCheck("ok")
        pipeline._holding_discipline_check_for_exit(
            symbol="AAA",
            action="SELL",
            reason="regime shift to risk-off",
            positions=[_position("AAA")],
            run_id="midday-2026-09-11",
            position_history={"AAA": {}},
        )

    assert spy.call_args.kwargs["macro_status"] is None
    assert spy.call_args.kwargs["macro_regime_today"] is None


def test_the_intraday_assembler_labels_a_same_day_carry_forward_honestly():
    """And when there IS a read dated today, it is labelled with the status
    this repo already uses for it — `carried_from_morning`, which
    `TRUSTED_MACRO_STATUSES` accepts — rather than a second invented one."""
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)

    with patch("src.risk.exit_guard.holding_discipline_claim_check") as spy:
        spy.return_value = HoldingDisciplineClaimCheck("ok")
        pipeline._holding_discipline_check_for_exit(
            symbol="AAA",
            action="SELL",
            reason="regime shift to risk-off",
            positions=[_position("AAA")],
            run_id="midday-2026-09-11",
            position_history={"AAA": {}},
        )

    assert spy.call_args.kwargs["macro_status"] == "carried_from_morning"
    assert spy.call_args.kwargs["macro_regime_today"] == "risk-on"


def test_regime_only_snapshot_is_trusted_for_exits_not_passed_to_pm():
    """A same-day {date, regime} trim must still prove a false flip.
    The same payload must not be smuggled into PM as a MacroAnalysis."""
    from src.pipeline_stages import _macro_analysis_as_dict

    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)
    carried = pipeline._carry_forward_macro()
    assert carried.status == "carried_from_morning"
    assert carried.payload["regime"] == "risk-on"
    assert _macro_analysis_as_dict(carried.payload) is None


def test_a_sale_the_same_rules_gate_decided_keeps_a_false_claim_as_evidence_only():
    """One rule (owner ruling 2026-10-13, coordinator 14 Oct defect 4): the
    checks above drop a sale the same-rules gate did NOT decide (every test
    above passes no `same_rules_allowed`); a gate-decided sale keeps the
    proven-false finding as evidence only, never a drop."""
    pipeline = _pipeline(macro_state=_macro_today("risk-on"), protected=True)

    pipeline._midday_execute_llm_actions(
        positions=[_position("AAA")],
        review=_review(),
        run_id="midday-2026-10-14",
        same_rules_allowed={"AAA"},
    )

    statuses = _statuses(pipeline)
    assert "exit_blocked_holding_discipline_claim_false" not in statuses
    assert "holding_discipline_claim_false_evidence_only" in statuses
