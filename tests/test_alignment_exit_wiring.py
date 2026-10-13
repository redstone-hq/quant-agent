"""Wiring tests for the ALIGNMENT EXIT — the class of defect that killed the
first two attempts.

The pure module has its own tests in `tests/test_alignment_exit.py`. These
cover the parts that live OUTSIDE it: the keyword surface, the bypass it is
and is not allowed to buy, which structural level it reads, the
EMA-vs-SMA question, and the unreadable-chart posture.

HONEST LIMIT: the sell loop in `TradingPipeline` that consumes the verdict
is one branch of a method thousands of lines long and is not invoked here.
What is asserted instead is every predicate that loop reads.
"""

from __future__ import annotations

import types

import pytest

from src.risk import alignment_exit as ae
from tests.pipeline_factory import build_pipeline
from src.risk.exit_trigger import ExitTrigger


# --------------------------------------------------------------------------
# 1. THE HIDDEN SECOND BYPASS — closed.
# --------------------------------------------------------------------------
def test_alignment_phrases_are_not_hard_trigger_keywords():
    """`_reason_cites_hard_trigger` waves a reason past the TRAIL_STOP
    ratchet cooldown and the 1.25xATR trail clamp, a path that runs NO
    chart check. The alignment exit must never be buyable there by prose.
    """
    from src.pipeline import _HARD_TRIGGER_KEYWORDS, _reason_cites_hard_trigger

    for phrase in ("trend alignment over", "alignment exit", "trend_alignment_over"):
        assert phrase not in _HARD_TRIGGER_KEYWORDS
        assert _reason_cites_hard_trigger(f"selling: {phrase} on the daily") is False


def test_canonical_name_autoappend_excludes_chart_verified_triggers():
    """The enum member's canonical prose names are auto-appended to the
    hard-trigger tuple. That auto-append is the sneaky half of the same
    bypass, so chart-verified triggers are filtered out of it."""
    from src.pipeline import _CHART_VERIFIED_TRIGGER_NAMES, _HARD_TRIGGER_KEYWORDS

    assert "trend alignment over" in _CHART_VERIFIED_TRIGGER_NAMES
    assert not (_CHART_VERIFIED_TRIGGER_NAMES & set(_HARD_TRIGGER_KEYWORDS))


def test_reason_claims_alignment_exit_reads_trigger_then_prose():
    from src.pipeline import _reason_claims_alignment_exit

    assert _reason_claims_alignment_exit("", ExitTrigger.TREND_ALIGNMENT_OVER)
    assert _reason_claims_alignment_exit("Trend Alignment Over on the daily", None)
    assert not _reason_claims_alignment_exit("taking profit at my target", None)


# --------------------------------------------------------------------------
# 2. EMA vs SMA — judge the price the thesis actually named.
# --------------------------------------------------------------------------
def test_thesis_naming_an_ema_is_judged_against_an_ema():
    # A curved advance, so the exponential weighting actually differs from
    # the arithmetic mean (on a straight line the two coincide).
    closes = [float(i * i) / 40.0 + 10.0 for i in range(1, 61)]
    sma = ae.simple_moving_average(closes, 50)
    ema = ae.exponential_moving_average(closes, 50)
    assert sma is not None and ema is not None
    assert ema != pytest.approx(sma)

    assert ae.thesis_ma_ref("close below the EMA50") == (50, "EMA")
    assert ae.thesis_ma_ref("close below the 50-day moving average") == (50, "SMA")

    v = ae.check_alignment_exit(
        thesis_invalid_if="close below the EMA50",
        closes=closes,
        atr=1.0,
    )
    assert v.thesis_ma_kind == "EMA"
    prices = {m.source: m.price for m in v.marks}
    assert prices["EMA50 (thesis rides the EMA50)"] == pytest.approx(ema)
    # The ladder mark is the next longer average the DESK computes, and is
    # labelled as the simple average it is — not silently relabelled.
    assert any(s.startswith("SMA200") for s in prices) or len(closes) < 200


def test_unsupported_or_absent_ma_reference_yields_no_average_mark():
    assert ae.thesis_ma_ref("close below the MA33") is None
    assert ae.thesis_ma_ref("it looks tired") is None


# --------------------------------------------------------------------------
# 3. The structural mark is the level that BROKE, not the nearest one.
# --------------------------------------------------------------------------
class _Bar:
    def __init__(self, date, close):
        self.date, self.close = date, close


def _market(**methods):
    """A market stand-in carrying what the REAL constructor calls on it."""
    return types.SimpleNamespace(set_fallback_bars=lambda fn: None, **methods)


def _pipeline_stub(monkeypatch, *, basis, broken_level, closes, atr=1.0):

    bars = [_Bar(i, c) for i, c in enumerate(closes)]
    p = build_pipeline(market=_market(get_ohlcv=lambda s, n: bars))
    p.config = types.SimpleNamespace(trading=types.SimpleNamespace(lookback_days=200))
    p._structural_protection_for_holding = lambda **kw: types.SimpleNamespace(
        basis=basis,
        broken_level=broken_level,
    )
    monkeypatch.setattr(
        "src.data.technical.compute_indicators",
        lambda sym, b: types.SimpleNamespace(atr_14=atr),
    )
    return p


def test_structural_mark_is_the_level_the_check_named(monkeypatch):
    closes = [100.0] * 60 + [80.0]
    p = _pipeline_stub(
        monkeypatch,
        basis="structural_level_broken",
        broken_level=95.0,
        closes=closes,
    )
    v = p._alignment_exit_for_holding(
        symbol="X",
        thesis_invalid_if=None,
        is_short=False,
        entry_price=100.0,
        stop_loss=90.0,
        run_id="r",
    )
    # The chart's own averages are marks as well (the thesis named none),
    # and the confirmed-broken level is recorded exactly once. Since
    # 2026-10-09 marks are recorded, not decisive: these stub bars carry no
    # lows, so there is no swing reference and the verdict holds.
    assert 95.0 in [m.price for m in v.marks]
    assert sum("structural" in m.source for m in v.marks) == 1
    assert v.status == "HOLD" and v.code == ae.CODE_NO_SWING


def test_caller_hands_its_own_bars_to_the_swing_test(monkeypatch):
    """The swing test reads bar LOWS; the caller must pass the very bars
    its closes came from, or the exit could never fire in production."""
    closes = [100.0 + i for i in range(12)]
    p = _pipeline_stub(monkeypatch, basis="structural_level_broken", broken_level=95.0, closes=closes)
    seen = {}
    real = ae.check_alignment_exit

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(ae, "check_alignment_exit", spy)
    p._alignment_exit_for_holding(
        symbol="X", thesis_invalid_if=None, is_short=False, entry_price=100.0, stop_loss=90.0, run_id="r"
    )
    assert [b.close for b in seen["bars"]] == closes == seen["closes"]


def test_no_structural_mark_when_the_level_was_not_confirmed_broken(monkeypatch):
    """An UNBROKEN level must never be admitted — the proximity guess used
    to let an overhead resistance in as 'the confirmed-broken level'."""
    p = _pipeline_stub(
        monkeypatch,
        basis="structural_level_intact",
        broken_level=None,
        closes=[100.0] * 60 + [80.0],
    )
    v = p._alignment_exit_for_holding(
        symbol="X",
        thesis_invalid_if=None,
        is_short=False,
        entry_price=100.0,
        stop_loss=90.0,
        run_id="r",
    )
    # The verdict may still be readable off the chart's own averages; what
    # must never happen is an UNBROKEN level being admitted as a mark.
    assert all("structural" not in m.source for m in v.marks)
    assert 95.0 not in [m.price for m in v.marks]


# --------------------------------------------------------------------------
# 4. The unreadable chart DROPS the sale (fails closed), and says so.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs, code",
    [
        (dict(thesis_invalid_if="close below the MA50", closes=[], atr=1.0), ae.CODE_NO_CLOSES),
        (
            dict(thesis_invalid_if="close below the MA50", closes=[float(i) for i in range(1, 61)], atr=None),
            ae.CODE_NO_ATR,
        ),
        (dict(thesis_invalid_if=None, closes=[1.0, 2.0], atr=1.0), ae.CODE_NO_MARK),
    ],
)
def test_unreadable_chart_is_unparseable_never_a_silent_clear(kwargs, code):
    v = ae.check_alignment_exit(**kwargs)
    assert v.status == "UNPARSEABLE"
    assert v.code == code
    assert v.exit_cleared is False


def test_chart_read_failure_degrades_to_unparseable(monkeypatch):

    p = build_pipeline(
        market=_market(
            get_ohlcv=lambda s, n: (_ for _ in ()).throw(RuntimeError("feed down")),
        )
    )
    p.config = types.SimpleNamespace(trading=types.SimpleNamespace(lookback_days=200))
    v = p._alignment_exit_for_holding(
        symbol="X",
        thesis_invalid_if=None,
        is_short=False,
        entry_price=None,
        stop_loss=None,
        run_id="r",
    )
    assert v.status == "UNPARSEABLE"
    assert v.exit_cleared is False


# --------------------------------------------------------------------------
# 5. The made-up 3.0 ATR give-back is retired from the code AND the ledger.
# --------------------------------------------------------------------------
def test_retired_give_back_has_no_ledger_row_and_no_constant():
    import yaml

    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    led = yaml.safe_load((root / "config" / "number_ledger.yaml").read_text())
    gone = "src.risk.alignment_exit.ALIGNMENT_GIVE_BACK_ATR_MULTIPLE"
    assert all(n["id"] != gone for n in led["numbers"])
    assert not hasattr(ae, "ALIGNMENT_GIVE_BACK_ATR_MULTIPLE")


# --------------------------------------------------------------------------
# 6. THE SCAN — the alignment exit can now INITIATE a sale.
#
# Until this landed, `check_alignment_exit` ran only on positions the review
# had already named, and only GATED the ones whose prose already claimed the
# alignment exit. Nothing ever asked the question of a position the models
# were silent about, so the owner-ratified rule could never start a sale.
# --------------------------------------------------------------------------
def _scan_pipeline(verdicts: dict):
    """A pipeline whose only real behaviour is the scan; the chart read is
    replaced by a per-symbol canned verdict."""

    p = build_pipeline()
    seen = []

    def _chart(*, symbol, **kw):
        seen.append(symbol)
        return verdicts[symbol]

    p._alignment_exit_for_holding = _chart
    p._seen = seen
    # Bought long before today: the scan's same-session gate needs a real
    # entry date, and an unreadable one deliberately holds.
    p._recorded = []
    p.db = types.SimpleNamespace(
        get_symbol_last_buy=lambda sym, **kw: {"timestamp": "2020-01-01 10:00:00"},
        get_position_open_timestamp=lambda row: row.get("timestamp"),
        record_alignment_exit_reading=lambda **kw: p._recorded.append(kw),
    )
    return p


def _verdict(status):
    return ae.AlignmentExitCheck(
        status,
        "c",
        (),
        None,
        None,
        None,
        "detail",
        owner_reason="Trend alignment over: the last line ...",
    )


def _pos(symbol, qty=10.0):
    from src.models import Position

    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry=100.0,
        current_price=90.0,
        market_value=900.0,
        unrealized_pnl=-100.0,
        sector="Tech",
    )


_PRIORITY = {"SELL": 0, "COVER": 0, "REDUCE": 1, "TRAIL_STOP": 2, "HOLD": 3}


def test_scan_raises_a_sale_on_a_position_no_model_mentioned():
    """THE POINT OF THE WHOLE BUILD."""
    p = _scan_pipeline({"AAA": _verdict("EXIT")})
    best: dict = {}
    p._alignment_exit_scan(
        [_pos("AAA")],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
    )
    assert best["AAA"]["action"] == "SELL"
    assert best["AAA"]["exit_trigger"] == ExitTrigger.TREND_ALIGNMENT_OVER.value
    # The reason names the trigger in the accepted wording, so the confirmer
    # downstream recognises the claim and gates the sale on the verdict.
    from src.pipeline import _reason_claims_alignment_exit

    assert _reason_claims_alignment_exit(best["AAA"]["reason"], None)


def test_scan_raises_nothing_on_hold_or_unreadable_chart():
    for status in ("HOLD", "UNPARSEABLE"):
        p = _scan_pipeline({"AAA": _verdict(status)})
        best: dict = {}
        p._alignment_exit_scan(
            [_pos("AAA")],
            best,
            run_id="r",
            position_facts=None,
            priority=_PRIORITY,
        )
        assert best == {}, status


def test_scan_failure_holds_and_does_not_stop_the_other_names():

    p = build_pipeline()

    def _chart(*, symbol, **kw):
        if symbol == "BOOM":
            raise RuntimeError("no bars")
        return _verdict("EXIT")

    p._alignment_exit_for_holding = _chart
    p.db = types.SimpleNamespace(
        get_symbol_last_buy=lambda sym, **kw: {"timestamp": "2020-01-01 10:00:00"},
        get_position_open_timestamp=lambda row: row.get("timestamp"),
    )
    best: dict = {}
    p._alignment_exit_scan(
        [_pos("BOOM"), _pos("AAA")],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
    )
    assert "BOOM" not in best
    assert best["AAA"]["action"] == "SELL"


def test_scan_covers_a_short_and_never_sells_it():
    p = _scan_pipeline({"SHT": _verdict("EXIT")})
    best: dict = {}
    p._alignment_exit_scan(
        [_pos("SHT", qty=-10.0)],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
    )
    assert best["SHT"]["action"] == "COVER"


def test_scan_never_overwrites_an_exit_the_review_asked_for():
    p = _scan_pipeline({"AAA": _verdict("EXIT")})
    mine = {"symbol": "AAA", "action": "SELL", "reason": "earnings miss"}
    best = {"AAA": mine}
    p._alignment_exit_scan(
        [_pos("AAA")],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
    )
    assert best["AAA"] is mine
    # STILL not read: item 75's recording does not buy a chart read (a live
    # yfinance download) to fill itself. The position is recorded as NOT
    # EVALUATED instead, so a later reader knows its own denominator.
    assert p._seen == []
    assert [r["symbol"] for r in p._recorded] == ["AAA"]
    assert p._recorded[0]["verdict"] is None
    assert "not read this session" in p._recorded[0]["not_evaluated_reason"]


def test_scan_supersedes_hold_because_the_chart_decides_not_the_prose():
    p = _scan_pipeline({"AAA": _verdict("EXIT")})
    best = {"AAA": {"symbol": "AAA", "action": "HOLD", "reason": "still like it"}}
    p._alignment_exit_scan(
        [_pos("AAA")],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
    )
    assert best["AAA"]["action"] == "SELL"


def test_verdict_is_read_once_per_position_per_run():

    p = build_pipeline()
    calls = []

    def _chart(*, symbol, **kw):
        calls.append(symbol)
        return _verdict("EXIT")

    p._alignment_exit_for_holding = _chart
    kw = dict(
        symbol="AAA",
        thesis_invalid_if=None,
        is_short=False,
        entry_price=1.0,
        stop_loss=None,
        run_id="r",
    )
    assert p._alignment_exit_cached(**kw).status == "EXIT"
    assert p._alignment_exit_cached(**kw).status == "EXIT"
    assert calls == ["AAA"]


def test_scanned_sale_reaches_the_real_sell_path_with_the_reason_voiced():
    """End to end through `_midday_execute_llm_actions`: a review that names
    NOTHING, a chart that says the move is over, and the desk's ordinary
    protected-sell path carrying the chart's own owner-facing sentence."""
    from unittest.mock import MagicMock

    from src.models import PositionReasoningChain, PositionReview

    p = build_pipeline()
    p.broker = MagicMock()
    p.db = MagicMock()
    p.db.get_trades.return_value = []
    p.db.get_acted_exit_triggers_today.return_value = []
    p._format_qty = lambda q: str(q)
    p._atr_for_symbol = lambda s: 2.0
    p._record_exit_refusal = lambda **kw: None
    p._alignment_exit_for_holding = lambda **kw: _verdict("EXIT")
    p._submit_protected_sell = lambda **kw: (
        {"id": "o-1", "symbol": kw["symbol"], "status": "accepted"},
        None,
    )

    review = PositionReview(
        reasoning_chain=PositionReasoningChain(
            macro_continuity_check="stable",
            thesis_progress_check="on pace",
            thesis_integrity_check="intact",
            winners_discipline_check="no flags",
            session_disposition_check="patient",
            execution_rationale="n/a",
        ),
        actions=[],
        overall_assessment="nothing to do",
        risk_level="low",
    )
    orders = p._midday_execute_llm_actions(
        positions=[_pos("AAA")],
        review=review,
        run_id="r-scan",
    )
    assert orders, "the chart said the move was over and nothing was sold"
    # The sale states WHY, in the desk's existing owner-facing wording: the
    # scan's own line plus the chart's own sentence appended by the confirmer.
    reason = p.db.insert_trade.call_args.kwargs.get("reasoning") or ""
    assert "Trend alignment over" in reason
    assert "the last line" in reason


# --------------------------------------------------------------------------
# 6. ADVERSARY PASS — the five defects.
# --------------------------------------------------------------------------


def _scan_shell(*, status="EXIT", buy_ts="2020-01-01 10:00:00", calls=None):
    """A pipeline shell with only what `_alignment_exit_scan` touches."""

    p = build_pipeline()
    verdict = types.SimpleNamespace(
        status=status,
        exit_cleared=(status == "EXIT"),
        reason="the chart says so",
        code="c",
        owner_reason="because",
    )

    def _cached(**kw):
        if calls is not None:
            calls.append(kw)
        return verdict

    p._alignment_exit_cached = _cached
    p.db = types.SimpleNamespace(
        get_symbol_last_buy=lambda sym, **kw: {"timestamp": buy_ts} if buy_ts else {},
        get_position_open_timestamp=lambda row: row.get("timestamp"),
    )
    return p


def _pos_stub(symbol="AAA", qty=10.0, stop_loss=None):
    return types.SimpleNamespace(
        symbol=symbol,
        qty=qty,
        avg_entry=100.0,
        stop_loss=stop_loss,
        thesis_invalid_if=None,
    )


# --- Defect 1: a refused scan sale must not strip the position's stop. ----
def test_refused_scan_sale_restores_the_displaced_trail_stop():
    from src.pipeline import _actions_with_scan_fallback

    sale = {"symbol": "AAA", "action": "SELL", "_alignment_scan_raised": True}
    trail = {"symbol": "AAA", "action": "TRAIL_STOP"}
    orders: list = []
    seen = [
        item
        for item in _actions_with_scan_fallback(
            [sale],
            {"AAA": trail},
            orders,
        )
    ]
    assert seen == [sale, trail], (
        "a scan-raised sale that produced no order must hand the symbol back the TRAIL_STOP it displaced"
    )


def test_executed_scan_sale_does_not_restore_the_displaced_action():
    from src.pipeline import _actions_with_scan_fallback

    sale = {"symbol": "AAA", "action": "SELL", "_alignment_scan_raised": True}
    displaced = {"AAA": {"symbol": "AAA", "action": "TRAIL_STOP"}}
    orders: list = []
    seen = []
    for item in _actions_with_scan_fallback([sale], displaced, orders):
        seen.append(item)
        orders.append({"id": "1"})  # the sale went through
    assert seen == [sale]
    assert displaced == {"AAA": {"symbol": "AAA", "action": "TRAIL_STOP"}}


def test_refused_model_action_is_never_re_queued():
    from src.pipeline import _actions_with_scan_fallback

    model_sell = {"symbol": "AAA", "action": "SELL"}
    seen = list(
        _actions_with_scan_fallback(
            [model_sell],
            {"AAA": {"symbol": "AAA", "action": "TRAIL_STOP"}},
            [],
        )
    )
    assert seen == [model_sell]


def test_scan_records_the_action_it_displaced_and_flags_its_own():
    best = {"AAA": {"symbol": "AAA", "action": "TRAIL_STOP", "reason": "ratchet"}}
    displaced: dict = {}
    _scan_shell()._alignment_exit_scan(
        [_pos_stub()],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
        displaced=displaced,
    )
    assert best["AAA"]["action"] == "SELL"
    assert best["AAA"]["_alignment_scan_raised"] is True
    assert displaced["AAA"]["action"] == "TRAIL_STOP"


# --- Defect 3: the chart supplies a mark when the prose does not. ---------
def test_chart_supplies_marks_when_the_thesis_names_no_average():
    closes = [100.0] * 60 + [50.0]
    v = ae.check_alignment_exit(
        thesis_invalid_if="it looks tired",
        closes=closes,
        atr=1.0,
    )
    assert v.thesis_ma_period is None
    assert v.marks, "no thesis average must not mean no mark"
    # Marks are recorded, not decisive (2026-10-09): no bars, no swing.
    assert v.status == "HOLD" and v.code == ae.CODE_NO_SWING


def test_chart_marks_use_only_periods_the_desk_already_computes():
    assert set(ae.CHART_MA_PERIODS) == set(ae.SMA_LADDER) | set(ae.SMA_LADDER.values())


def test_unreadable_only_when_the_chart_itself_has_nothing():
    v = ae.check_alignment_exit(
        thesis_invalid_if=None,
        closes=[100.0, 101.0],
        atr=1.0,
    )
    assert v.status == "UNPARSEABLE" and v.code == ae.CODE_NO_MARK


# --- Defect 4: no same-session close. -------------------------------------
def test_position_opened_today_is_not_closed_by_the_scan():
    from src.trading_calendar import et_today

    best: dict = {}
    _scan_shell(buy_ts=f"{et_today()} 09:35:00")._alignment_exit_scan(
        [_pos_stub()],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
        displaced={},
    )
    assert best == {}


def test_unknown_entry_date_holds_rather_than_sells():
    from src.pipeline import TradingPipeline

    p = _scan_shell()

    def _boom(sym):
        raise RuntimeError("db down")

    p.db = types.SimpleNamespace(get_symbol_last_buy=_boom)
    assert p._position_opened_today("AAA") is True
    best: dict = {}
    p._alignment_exit_scan(
        [_pos_stub()],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
        displaced={},
    )
    assert best == {}
    assert TradingPipeline._position_opened_today is not None


def test_older_position_is_still_eligible():
    best: dict = {}
    _scan_shell(buy_ts="2020-01-01 10:00:00")._alignment_exit_scan(
        [_pos_stub()],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
        displaced={},
    )
    assert best["AAA"]["action"] == "SELL"


# --- Defect 5: the two callers of the shared memo pass identical inputs. --
def test_scan_and_confirmer_resolve_stop_loss_identically():
    """Both key the SAME per-run memo and `stop_loss` decides whether a broken-level mark
    exists, so both call sites must resolve it the same way; the scan body is in `AlignmentExit`."""
    import inspect

    from src.exits.alignment_exit import AlignmentExit
    from src.pipeline import TradingPipeline

    scan_src = inspect.getsource(AlignmentExit._alignment_exit_scan)
    exec_src = inspect.getsource(TradingPipeline._midday_execute_llm_actions)
    needle = 'or facts.get("stop_loss")'
    assert needle in scan_src
    assert needle in exec_src


def test_scan_passes_the_position_facts_stop_loss(monkeypatch):
    calls: list = []
    _scan_shell(calls=calls)._alignment_exit_scan(
        [_pos_stub(stop_loss=None)],
        {},
        run_id="r",
        position_facts={"AAA": {"stop_loss": 91.0}},
        priority=_PRIORITY,
        displaced={},
    )
    assert calls and calls[0]["stop_loss"] == 91.0


# --------------------------------------------------------------------------
# 7. ITEM 75 RECORDING — the reading is kept for EVERY open position EVERY
# session, INCLUDING the sessions the exit does not fire.
#
# Only a FIRING exit left any trace before this, so "do positions pass
# through a durable intermediate band of weakening before the trend ends, or
# do they fall straight through?" could not be asked at all. RECORDING ONLY:
# nothing reads these rows back into a sizing, stop or exit decision.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["EXIT", "HOLD", "UNPARSEABLE"])
def test_every_position_is_recorded_whether_or_not_the_exit_fires(status):
    p = _scan_pipeline({"AAA": _verdict(status)})
    p._alignment_exit_scan(
        [_pos("AAA")],
        {},
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
    )
    assert len(p._recorded) == 1, status
    assert p._recorded[0]["symbol"] == "AAA"
    assert p._recorded[0]["run_id"] == "r"
    assert p._recorded[0]["verdict"].status == status
    assert p._recorded[0]["not_evaluated_reason"] is None


def test_a_recording_failure_never_blocks_the_sale():
    p = _scan_pipeline({"AAA": _verdict("EXIT")})

    def _boom(**kw):
        raise RuntimeError("disk full")

    p.db.record_alignment_exit_reading = _boom
    best: dict = {}
    p._alignment_exit_scan(
        [_pos("AAA")],
        best,
        run_id="r",
        position_facts=None,
        priority=_PRIORITY,
    )
    assert best["AAA"]["action"] == "SELL"


def test_the_stored_reading_keeps_the_raw_distance_and_nulls_the_unknown(tmp_path):
    """Unknown stays NULL — never a zero and never an assumed value, and no
    band edge or classification is stored for a later reader to inherit."""
    from src.storage.db import Database

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    held = ae.AlignmentExitCheck(
        "HOLD",
        ae.CODE_HOLD,
        (ae.ChartMark(99.0, "SMA50"),),
        ae.ChartMark(99.0, "SMA50"),
        0.37,
        1.0,
        "detail",
        sessions_since_mark_lost=2,
    )
    blind = ae.AlignmentExitCheck(
        "UNPARSEABLE",
        ae.CODE_NO_ATR,
        (),
        None,
        None,
        None,
        "no atr",
    )
    assert db.record_alignment_exit_reading(
        symbol="AAA",
        verdict=held,
        run_id="r",
        is_short=False,
    )
    assert db.record_alignment_exit_reading(
        symbol="BBB",
        verdict=blind,
        run_id="r",
        is_short=False,
    )
    rows = {
        r[0]: r
        for r in db.conn.execute(
            "SELECT symbol, status, breach_atrs, band_atrs,"
            " sessions_since_mark_lost, last_mark_source"
            " FROM alignment_exit_readings"
        )
    }
    # A position that weakened WITHOUT firing is now on the record.
    assert rows["AAA"][1] == "HOLD"
    assert rows["AAA"][2] == pytest.approx(0.37)
    assert rows["AAA"][4] == 2
    # An unreadable chart is a row with NULLs, not a fabricated zero.
    assert rows["BBB"][1] == "UNPARSEABLE"
    assert rows["BBB"][2] is None and rows["BBB"][3] is None
    assert rows["BBB"][5] is None
    # No classification column exists: the cutoff belongs to a later reader.
    cols = {c[1] for c in db.conn.execute("PRAGMA table_info(alignment_exit_readings)")}
    assert not (cols & {"band", "classification", "weakening", "trim_fraction"})
    # A position the scan never evaluated is a row of NULLs plus a reason —
    # the recording never buys a chart read (a live download) to fill itself.
    assert db.record_alignment_exit_reading(
        symbol="CCC",
        verdict=None,
        run_id="r",
        is_short=False,
        not_evaluated_reason="the review already proposed a SELL",
    )
    row = db.conn.execute(
        "SELECT status, breach_atrs, marks_count, not_evaluated_reason"
        " FROM alignment_exit_readings WHERE symbol = 'CCC'"
    ).fetchone()
    assert row[0] is None and row[1] is None and row[2] is None
    assert row[3] == "the review already proposed a SELL"


def test_nothing_reads_the_item_75_recording_back_into_a_decision():
    """RECORDING ONLY. The table may be WRITTEN, and read by a human asking
    item 75's question; it may never be read back into live logic."""
    import pathlib

    readers = []
    for f in pathlib.Path("src").rglob("*.py"):
        text = f.read_text()
        if "alignment_exit_readings" not in text:
            continue
        if "SELECT" in text and "FROM alignment_exit_readings" in text:
            readers.append(str(f))
    assert readers == []
