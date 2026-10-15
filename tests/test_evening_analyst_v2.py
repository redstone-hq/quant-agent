"""Evening analyst v2 — schema + memory layer + calibration math invariants.

Scope:
  - Required 6-step reasoning_chain + min_length=1 on core fields
  - SellGrade / BuyGrade structured lists
  - _build_recent_buys_for_grading math
  - _build_recent_outlook_calibration math (hit-rate, bias-specific rates,
    conviction-stratified rates, insufficient-data handling)
  - Prompt renders memory layers
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.models import (
    BuyGrade,
    EveningReasoningChain,
    EveningReport,
    SellGrade,
)
from src.pipeline import TradingPipeline
from tests.pipeline_factory import build_pipeline


def _valid_rc() -> EveningReasoningChain:
    return EveningReasoningChain(
        performance_attribution="x",
        outlook_retrospection="x",
        thesis_health_review="x",
        decision_quality_review="x",
        calibration_meta="x",
        market_regime_read="x",
        tomorrow_preparation="x",
    )


# ---------------------------------------------------------------------------
# Schema: reasoning_chain required; grades parse + validate
# ---------------------------------------------------------------------------


def test_evening_report_requires_reasoning_chain():
    with pytest.raises(ValidationError):
        EveningReport(
            daily_summary="x",
            lessons="x",
            tomorrow_outlook="x",
            risk_rating="low",
        )


def test_evening_reasoning_chain_rejects_empty_steps():
    with pytest.raises(ValidationError):
        EveningReasoningChain(
            performance_attribution="",  # empty
            outlook_retrospection="x",
            decision_quality_review="x",
            calibration_meta="x",
            market_regime_read="x",
            tomorrow_preparation="x",
        )


def test_evening_report_requires_non_empty_core_prose_fields():
    """daily_summary / lessons / tomorrow_outlook must be non-empty."""
    with pytest.raises(ValidationError):
        EveningReport(
            reasoning_chain=_valid_rc(),
            daily_summary="",  # empty
            lessons="ok",
            tomorrow_outlook="ok",
            risk_rating="low",
        )


def test_sell_grade_validates_enum_and_reason():
    # valid
    g = SellGrade(
        symbol="NVDA",
        sell_date="2026-04-17",
        sell_price=200.0,
        current_price=210.0,
        pct_move_since_sell=5.0,
        grade="premature",
        reason="Uptrend intact, sold on noise",
    )
    assert g.grade == "premature"
    assert g.symbol == "NVDA"
    # invalid grade
    with pytest.raises(ValidationError):
        SellGrade(
            symbol="NVDA",
            sell_date="2026-04-17",
            sell_price=200.0,
            current_price=210.0,
            pct_move_since_sell=5.0,
            grade="not-a-grade",
            reason="x",
        )
    # empty reason fails
    with pytest.raises(ValidationError):
        SellGrade(
            symbol="NVDA",
            sell_date="2026-04-17",
            sell_price=200.0,
            current_price=210.0,
            pct_move_since_sell=5.0,
            grade="correct",
            reason="",
        )


def test_buy_grade_roundtrip():
    g = BuyGrade(
        symbol="AAPL",
        buy_date="2026-04-15",
        buy_price=180.0,
        current_price=186.0,
        pct_move_since_buy=3.3,
        grade="correct",
        reason="Thesis on track",
    )
    assert g.pct_move_since_buy == 3.3


def test_full_evening_report_with_grades_roundtrip():
    report = EveningReport(
        reasoning_chain=_valid_rc(),
        daily_summary="Book up 0.8%.",
        lessons="keep disciplined",
        tomorrow_outlook="retail sales 08:30 ET",
        risk_rating="moderate",
        tomorrow_bias="bullish",
        tomorrow_conviction="medium",
        tomorrow_key_risks=["Retail sales 08:30", "NVDA $220 target"],
        sell_grades=[
            SellGrade(
                symbol="XOM",
                sell_date="2026-04-17",
                sell_price=108.0,
                current_price=106.0,
                pct_move_since_sell=-1.8,
                grade="correct",
                reason="Ceasefire held",
            )
        ],
        buy_grades=[
            BuyGrade(
                symbol="NVDA",
                buy_date="2026-04-17",
                buy_price=196.0,
                current_price=210.0,
                pct_move_since_buy=7.1,
                grade="correct",
                reason="AI capex confirmed",
            )
        ],
    )
    d = report.model_dump()
    assert d["reasoning_chain"]["performance_attribution"] == "x"
    assert len(d["sell_grades"]) == 1
    assert d["sell_grades"][0]["grade"] == "correct"
    assert d["buy_grades"][0]["symbol"] == "NVDA"


# ---------------------------------------------------------------------------
# _build_recent_buys_for_grading math
# ---------------------------------------------------------------------------


def _pipeline_with_broker_price(price: float) -> TradingPipeline:
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    pipeline.broker.get_latest_price.return_value = price
    return pipeline


def test_recent_buys_computes_pct_move():
    pipeline = _pipeline_with_broker_price(220.0)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    pipeline.db.get_trades.return_value = [
        {
            "action": "BUY",
            "symbol": "NVDA",
            "price": 200.0,
            "fill_price": 200.0,
            "timestamp": f"{yesterday} 14:00:00",
            "reasoning": "AI breakout",
            "fill_status": "filled",
            "fill_qty": 10,
        },
    ]
    buys = pipeline._build_recent_buys_for_grading(lookback_days=5)
    assert len(buys) == 1
    assert buys[0]["symbol"] == "NVDA"
    assert buys[0]["buy_price"] == 200.0
    assert buys[0]["current_price"] == 220.0
    assert buys[0]["pct_move_since_buy"] == 10.0


def test_recent_buys_filters_stale_and_non_buy_rows():
    """Lookback window excludes older BUYs; non-BUY action rows are skipped."""
    pipeline = _pipeline_with_broker_price(100.0)
    old_date = (date.today() - timedelta(days=10)).isoformat()
    new_date = (date.today() - timedelta(days=1)).isoformat()
    pipeline.db.get_trades.return_value = [
        # BUY from 10 days ago — outside lookback
        {
            "action": "BUY",
            "symbol": "OLD",
            "price": 100.0,
            "fill_price": 100.0,
            "timestamp": f"{old_date} 14:00:00",
            "fill_status": "filled",
            "fill_qty": 1,
        },
        # SELL from yesterday — wrong action
        {
            "action": "SELL",
            "symbol": "SOLD",
            "price": 50.0,
            "fill_price": 50.0,
            "timestamp": f"{new_date} 14:00:00",
            "fill_status": "filled",
            "fill_qty": 1,
        },
        # Valid BUY from yesterday
        {
            "action": "BUY",
            "symbol": "NEW",
            "price": 80.0,
            "fill_price": 80.0,
            "timestamp": f"{new_date} 14:00:00",
            "fill_status": "filled",
            "fill_qty": 1,
        },
    ]
    buys = pipeline._build_recent_buys_for_grading(lookback_days=5)
    syms = [b["symbol"] for b in buys]
    assert syms == ["NEW"]


def test_recent_buys_dedupes_multiple_buys_on_same_symbol():
    """Per-symbol dedup — only the newest BUY is surfaced."""
    pipeline = _pipeline_with_broker_price(220.0)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    two_days = (date.today() - timedelta(days=2)).isoformat()
    # Sorted newest-first (matches get_trades default order)
    pipeline.db.get_trades.return_value = [
        {
            "action": "BUY",
            "symbol": "NVDA",
            "price": 215.0,
            "fill_price": 215.0,
            "timestamp": f"{yesterday} 14:00:00",
            "fill_status": "filled",
            "fill_qty": 5,
        },
        {
            "action": "BUY",
            "symbol": "NVDA",
            "price": 200.0,
            "fill_price": 200.0,
            "timestamp": f"{two_days} 14:00:00",
            "fill_status": "filled",
            "fill_qty": 10,
        },
    ]
    buys = pipeline._build_recent_buys_for_grading(lookback_days=5)
    assert len(buys) == 1
    assert buys[0]["buy_price"] == 215.0  # the newer of the two


# ---------------------------------------------------------------------------
# _build_recent_outlook_calibration math
# ---------------------------------------------------------------------------


def test_outlook_calibration_matches_bullish_with_positive_day():
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights.return_value = [
        {"date": "2026-04-14", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
    ]
    pipeline.db.get_daily_pnl.return_value = [
        {"date": "2026-04-15", "daily_return_pct": 1.2},
    ]
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    assert calib["n"] == 1
    assert calib["samples"][0]["matched"] is True
    assert calib["overall_hit_rate_pct"] == 100.0


def test_outlook_calibration_marks_wrong_bias_as_miss():
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights.return_value = [
        {"date": "2026-04-14", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
        {"date": "2026-04-15", "tomorrow_bias": "bearish", "tomorrow_conviction": "medium"},
    ]
    pipeline.db.get_daily_pnl.return_value = [
        {"date": "2026-04-15", "daily_return_pct": -2.0},  # bullish called, down -2% → miss
        {"date": "2026-04-16", "daily_return_pct": +1.5},  # bearish called, up +1.5% → miss
    ]
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    assert calib["n"] == 2
    assert all(not s["matched"] for s in calib["samples"])
    assert calib["overall_hit_rate_pct"] == 0.0
    assert calib["bullish_hit_rate_pct"] == 0.0
    assert calib["bearish_hit_rate_pct"] == 0.0


def test_outlook_calibration_neutral_band():
    """neutral bias matches when actual is within ±0.3%."""
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights.return_value = [
        {"date": "2026-04-14", "tomorrow_bias": "neutral", "tomorrow_conviction": "low"},
        {"date": "2026-04-15", "tomorrow_bias": "neutral", "tomorrow_conviction": "low"},
    ]
    pipeline.db.get_daily_pnl.return_value = [
        {"date": "2026-04-15", "daily_return_pct": 0.15},  # within band → hit
        {"date": "2026-04-16", "daily_return_pct": 0.8},  # outside band → miss
    ]
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    # 1 of 2 neutral calls matched
    assert calib["neutral_hit_rate_pct"] == 50.0


def test_outlook_calibration_empty_when_no_history():
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights.return_value = []
    pipeline.db.get_daily_pnl.return_value = []
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    assert calib["n"] == 0
    assert calib["samples"] == []


def test_outlook_calibration_bullish_below_neutral_band_is_a_miss():
    """Edge case: bullish bias on a day that returned +0.1% (positive but
    inside the ±0.3% neutral band) must NOT count as matched — bullish
    means clearly up, not "barely positive". Without this guard, the
    bullish hit-rate would be inflated by flat-day flukes."""
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights.return_value = [
        {"date": "2026-04-14", "tomorrow_bias": "bullish", "tomorrow_conviction": "medium"},
    ]
    pipeline.db.get_daily_pnl.return_value = [
        {"date": "2026-04-15", "daily_return_pct": 0.1},  # inside ±0.3 band
    ]
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    assert calib["n"] == 1
    assert calib["samples"][0]["matched"] is False
    assert calib["bullish_hit_rate_pct"] == 0.0


def test_outlook_calibration_pairs_friday_prediction_with_monday_actual():
    """Friday evening's tomorrow_bias predicts Monday's session (weekend
    intervenes). Pairing logic must walk forward up to +4 days to find
    the next daily_pnl row, not silently drop the sample."""
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights.return_value = [
        # Friday evening
        {"date": "2026-04-17", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
    ]
    pipeline.db.get_daily_pnl.return_value = [
        # No Saturday / Sunday rows — next trading day is Monday.
        {"date": "2026-04-20", "daily_return_pct": 0.9},
    ]
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    assert calib["n"] == 1, (
        "Friday prediction must pair with Monday actual via the +1..+4d forward walk; got no samples"
    )
    assert calib["samples"][0]["matched"] is True


def test_outlook_calibration_respects_lookback_limit():
    """With 12 insights and lookback=5, only 5 samples land in the output —
    the rolling window must NOT silently grow when more insights exist."""
    pipeline = build_pipeline(db=MagicMock())
    insights = [
        {"date": f"2026-04-{day:02d}", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"}
        for day in range(1, 13)
    ]
    pnls = [{"date": f"2026-04-{day + 1:02d}", "daily_return_pct": 1.0} for day in range(1, 13)]
    pipeline.db.get_recent_insights.return_value = insights
    pipeline.db.get_daily_pnl.return_value = pnls
    calib = pipeline._build_recent_outlook_calibration(lookback=5)
    assert calib["n"] == 5
    assert len(calib["samples"]) == 5


def test_outlook_calibration_stratifies_by_conviction():
    pipeline = build_pipeline(db=MagicMock())
    pipeline.db.get_recent_insights.return_value = [
        {"date": "2026-04-10", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
        {"date": "2026-04-11", "tomorrow_bias": "bullish", "tomorrow_conviction": "high"},
        {"date": "2026-04-12", "tomorrow_bias": "bullish", "tomorrow_conviction": "low"},
        {"date": "2026-04-13", "tomorrow_bias": "bullish", "tomorrow_conviction": "low"},
    ]
    pipeline.db.get_daily_pnl.return_value = [
        # high-conviction bullish: one hit, one miss → 50%
        {"date": "2026-04-11", "daily_return_pct": 1.0},  # hit
        {"date": "2026-04-12", "daily_return_pct": -1.0},  # miss
        # low-conviction bullish: both hit → 100%
        {"date": "2026-04-13", "daily_return_pct": 1.0},  # hit
        {"date": "2026-04-14", "daily_return_pct": 1.0},  # hit
    ]
    calib = pipeline._build_recent_outlook_calibration(lookback=10)
    assert calib["high_conviction_hit_rate_pct"] == 50.0
    assert calib["low_conviction_hit_rate_pct"] == 100.0


# ---------------------------------------------------------------------------
# Prompt renders memory layers
# ---------------------------------------------------------------------------


def test_prompt_embeds_calibration_numbers():
    from src.agents.evening_analyst import EveningAnalystAgent

    with patch("anthropic.Anthropic"):
        agent = EveningAnalystAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 18}},
            total_value=100_000.0,
            daily_pnl=800.0,
            daily_return_pct=0.8,
            outlook_calibration={
                "n": 5,
                "samples": [
                    {
                        "date": "2026-04-14",
                        "predicted_bias": "bullish",
                        "predicted_conviction": "high",
                        "actual_return_pct": 1.2,
                        "matched": True,
                    },
                ],
                "overall_hit_rate_pct": 60.0,
                "bullish_hit_rate_pct": 57.0,
                "bearish_hit_rate_pct": 75.0,
                "neutral_hit_rate_pct": 50.0,
                "high_conviction_hit_rate_pct": 40.0,
                "low_conviction_hit_rate_pct": 70.0,
                "overall_trend_hit_rate_pct": 80.0,
                "bullish_trend_hit_rate_pct": 78.0,
                "bearish_trend_hit_rate_pct": 50.0,
            },
        )

    assert "NEXT-DAY hit rate" in msg
    assert "60%" in msg
    assert "high: 40%" in msg
    assert "low: 70%" in msg
    assert "2026-04-14" in msg
    # the new multi-day trend metric must be rendered (the directional scorecard)
    assert "TREND hit rate" in msg
    assert "78%" in msg


def test_prompt_says_insufficient_when_no_calibration_history():
    from src.agents.evening_analyst import EveningAnalystAgent

    with patch("anthropic.Anthropic"):
        agent = EveningAnalystAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 18}},
            total_value=100_000.0,
            daily_pnl=0.0,
            daily_return_pct=0.0,
            outlook_calibration={"n": 0, "samples": []},
        )

    assert "insufficient history" in msg


def test_prompt_contains_recent_buys_section():
    from src.agents.evening_analyst import EveningAnalystAgent

    with patch("anthropic.Anthropic"):
        agent = EveningAnalystAgent(api_key="test", model="claude-sonnet-4-6")
        msg = agent.build_user_message(
            positions=[],
            macro_summary={"vix": {"current": 18}},
            total_value=100_000.0,
            daily_pnl=0.0,
            daily_return_pct=0.0,
            recent_buys=[
                {
                    "symbol": "NVDA",
                    "buy_date": "2026-04-17",
                    "buy_price": 200.0,
                    "current_price": 210.0,
                    "pct_move_since_buy": 5.0,
                    "reasoning": "AI capex thesis",
                }
            ],
        )

    assert "Recent BUY decisions to grade" in msg
    assert "NVDA" in msg
    assert "$200.00" in msg
    assert "+5.00%" in msg


def test_prompt_contains_money_making_principles():
    """The system prompt codifies the 4 money-making principles + 'good stocks'."""
    from src.agents.evening_analyst import EveningAnalystAgent

    with patch("anthropic.Anthropic"):
        agent = EveningAnalystAgent(api_key="test", model="claude-sonnet-4-6")
        sp = agent.system_prompt

    assert "Calibration > looking smart" in sp
    assert "Good stocks are meant to be held" in sp
    assert "Intraday noise" in sp


def test_old_evening_json_without_reasoning_chain_fails_gracefully():
    """Backward-compat documentation test.

    Pre-v2 agent_logs rows stored EveningReport JSON without a
    reasoning_chain field. If any future code re-parses that JSON through
    `EveningReport(**data)`, it will ValidationError — which is the
    correct behavior (we WANT v2 to demand the 6-step chain). This test
    pins that failure mode so no one silently adds a backward-compat
    shim that weakens the schema guarantee.

    In practice nothing re-parses old agent_logs into Pydantic — callers
    parse JSON and use `dict.get(...)`. This test is only here as a
    structural guardrail.
    """
    old_json = {
        "daily_summary": "legacy row from before v2",
        "lessons": "no reasoning_chain field existed",
        "tomorrow_outlook": "watch FOMC",
        "risk_rating": "moderate",
        "tomorrow_bias": "neutral",
        "tomorrow_conviction": "medium",
        # NO reasoning_chain key
    }
    with pytest.raises(ValidationError) as exc_info:
        EveningReport(**old_json)
    # Error must mention reasoning_chain by name so operators can diagnose
    assert "reasoning_chain" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Per-entry isolation for missed_opportunities (2026-05-01 incident)
# ---------------------------------------------------------------------------


def _valid_evening_json() -> dict:
    """Minimum-viable EveningReport JSON shaped like what the LLM returns.

    Matches the on-the-wire dict shape (not Pydantic model instances), so we
    can exercise the same construction path the real analyze() uses.
    """
    return {
        "reasoning_chain": {
            "performance_attribution": "Book up 0.1% on AVGO/DXPE adds.",
            "outlook_retrospection": "Yesterday's bullish bias was correct.",
            "thesis_health_review": "All 7 holdings intact-to-strengthening.",
            "decision_quality_review": "Trims were premature on GOOGL.",
            "calibration_meta": "Bullish hit rate 6/9 last 10 sessions.",
            "market_regime_read": "Risk-on continues; HY OAS contained.",
            "tomorrow_preparation": "Watch jobs print 08:30 ET.",
        },
        "daily_summary": "Quiet day with two adds.",
        "lessons": "Mechanical auto-TP overruled a strengthening thesis.",
        "tomorrow_outlook": "Bullish bias contingent on jobs print.",
        "risk_rating": "moderate",
        "tomorrow_bias": "bullish",
        "tomorrow_conviction": "medium",
        "tomorrow_key_risks": ["jobs print 08:30 ET"],
        "sell_decisions_assessment": "",
        "sell_grades": [],
        "buy_grades": [],
    }


def _valid_missed_opportunity_dict(symbol: str = "ORCL") -> dict:
    return {
        "symbol": symbol,
        "source": "universe",
        "move_pct": -8.4,
        "miss_category": "value_entry_missed",
        "theme_if_any": "AI-capex",
        "theme_durability": "multi_year_secular",
        "lesson": "Down >=8% with intact fundamentals; deserved a value-watch entry.",
        "universe_addition_recommendation": "no",
        "universe_addition_reason": "Already in universe.",
    }


def _the_2026_05_01_bad_entry() -> dict:
    """Exact shape of the entry that crashed the 2026-05-01 evening report.

    LLM marked CMCSA as a 'value_entry_missed' but left theme_if_any empty,
    which USED to be rejected; a theme-less miss is now valid (see
    test_evening_missed_no_theme.py). The fixture is kept as a genuinely malformed
    entry (empty lesson) so the drop path stays covered.
    """
    return {
        "symbol": "CMCSA",
        "source": "universe",
        "move_pct": -3.2,
        "miss_category": "value_entry_missed",
        "theme_if_any": "",
        "theme_durability": "unknown",
        "lesson": "",
        "universe_addition_recommendation": "no",
        "universe_addition_reason": "Already in universe.",
    }


def test_drop_invalid_missed_opportunities_strips_2026_05_01_shape():
    """The exact 2026-05-01 bad entry must be dropped, leaving the rest.

    Regression-pin for the incident where one CMCSA entry with
    miss_category='value_entry_missed' + empty theme_if_any failed pydantic
    validation, taking the entire EveningReport (7 thesis_health_review
    narratives + sell_grades + tomorrow_outlook) down with it.
    """
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = _valid_evening_json()
    parsed["missed_opportunities"] = [
        _valid_missed_opportunity_dict("ORCL"),
        _the_2026_05_01_bad_entry(),
        _valid_missed_opportunity_dict("META"),
    ]
    out = EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    syms = [m["symbol"] for m in out["missed_opportunities"]]
    assert syms == ["ORCL", "META"], f"CMCSA must be dropped, ORCL+META kept; got {syms}"


def test_evening_report_constructs_after_dropping_bad_missed_opportunity():
    """End-to-end: with the bad entry stripped, EveningReport(**parsed)
    must succeed. This is the property that mattered on 2026-05-01 — the
    core report payload (reasoning_chain, thesis review, outlook) was
    clean and should have been persisted."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = _valid_evening_json()
    parsed["missed_opportunities"] = [
        _valid_missed_opportunity_dict("ORCL"),
        _the_2026_05_01_bad_entry(),
    ]
    cleaned = EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    # Must not raise — that's the whole point of the fix.
    report = EveningReport(**cleaned)
    assert report.tomorrow_bias == "bullish"
    assert len(report.missed_opportunities) == 1
    assert report.missed_opportunities[0].symbol == "ORCL"


def test_drop_invalid_missed_opportunities_keeps_all_when_all_valid():
    """No-op path: a clean list passes through untouched."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = _valid_evening_json()
    parsed["missed_opportunities"] = [
        _valid_missed_opportunity_dict("ORCL"),
        _valid_missed_opportunity_dict("META"),
    ]
    out = EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    assert len(out["missed_opportunities"]) == 2


def test_drop_invalid_missed_opportunities_handles_non_list_shape():
    """Defensive: if the LLM emits None or a bare string for the list,
    normalize to empty list rather than letting it propagate into pydantic
    as a confusing 'expected list, got str' error in the middle of an
    otherwise-clean report."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = _valid_evening_json()
    parsed["missed_opportunities"] = "oops not a list"
    out = EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    assert out["missed_opportunities"] == []


def test_drop_invalid_missed_opportunities_drops_non_dict_items():
    """If a list slot is the wrong type (string, None, number) skip it
    individually rather than raising AttributeError when we try to
    instantiate MissedOpportunity(**item)."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = _valid_evening_json()
    parsed["missed_opportunities"] = [
        _valid_missed_opportunity_dict("ORCL"),
        "stray string the LLM hallucinated",
        None,
        _valid_missed_opportunity_dict("META"),
    ]
    out = EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    syms = [m["symbol"] for m in out["missed_opportunities"]]
    assert syms == ["ORCL", "META"]


def test_drop_invalid_missed_opportunities_logs_bad_entries(caplog):
    """The drop must be observable in logs — silent stripping is worse
    than the original crash because operators wouldn't see the LLM is
    repeatedly tripping the same validator."""
    import logging
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = _valid_evening_json()
    parsed["missed_opportunities"] = [_the_2026_05_01_bad_entry()]
    with caplog.at_level(logging.WARNING, logger="src.agents.evening_analyst"):
        EveningAnalystAgent._drop_invalid_missed_opportunities(parsed)
    # Must mention the symbol so operators can correlate with the trade log.
    assert any("CMCSA" in rec.message for rec in caplog.records), (
        f"warning must mention CMCSA; got {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Scope guard for buy_grades / sell_grades (2026-09-02 incident)
#
# The evening prompt gives the LLM a short "Recent BUY decisions to grade"
# list (the actual recent_buys candidates) AND a much larger "Thesis Health
# Review" section covering every held position, with similar "entry $X ->
# $Y" framing. The model can confuse the two and emit buy_grades entries
# for symbols pulled from the Thesis Health Review — using wrong field
# shapes (sell_date/sell_price instead of buy_date/buy_price/
# pct_move_since_buy) that fail BuyGrade validation. On a real production
# night (2026-09-02) this cost the ONE legitimate BUY candidate (RSG) too,
# because it was in a corrupted slot of a list whose failures all got
# logged identically as "malformed" — indistinguishable from genuine
# schema mistakes. This guard drops out-of-scope entries BEFORE schema
# validation, with a distinct log message, so a legitimate candidate never
# gets confused for one. It does NOT fix the prompt ambiguity that causes
# the confusion in the first place — that needs prompt iteration verified
# by a paid LLM benchmark, out of scope for this guard.
# ---------------------------------------------------------------------------


def _rsg_shaped_buy_grade() -> dict:
    """A well-formed BuyGrade entry for the one legitimate candidate."""
    return {
        "symbol": "RSG",
        "buy_date": "2026-08-28",
        "buy_price": 210.50,
        "current_price": 213.10,
        "pct_move_since_buy": 1.24,
        "grade": "correct",
        "reason": "Entered on pullback to support; thesis intact.",
        "thesis_trajectory": "intact",
    }


def _thesis_health_confused_entry(symbol: str = "GOOGL") -> dict:
    """Sell-shaped entry the LLM pulled from Thesis Health Review instead
    of the short BUY-grading list — the exact confusion class from the
    2026-09-02 incident. Uses sell_date/sell_price fields, which fail
    BuyGrade validation (missing buy_date/buy_price/pct_move_since_buy)."""
    return {
        "symbol": symbol,
        "sell_date": "2026-08-15",
        "sell_price": 195.00,
        "current_price": 201.30,
        "grade": "correct",
        "reason": "Thesis strengthening on ad revenue beat.",
    }


def test_out_of_scope_buy_grade_dropped_legitimate_one_kept():
    """RSG-shaped repro: recent_buys has exactly one legitimate candidate
    (RSG). The parsed buy_grades list has a well-formed entry for RSG AND
    an extra, sell-shaped entry for GOOGL (not in recent_buys, and not
    schema-valid as a BuyGrade either). The extra must be dropped via the
    new scope guard with the distinct out-of-scope log message, and RSG
    must survive untouched."""
    import logging
    from src.agents.evening_analyst import EveningAnalystAgent

    recent_buys = [
        {
            "symbol": "RSG",
            "buy_date": "2026-08-28",
            "buy_price": 210.50,
            "current_price": 213.10,
            "pct_move_since_buy": 1.24,
        }
    ]
    allowed = {b["symbol"] for b in recent_buys}

    parsed = {
        "buy_grades": [
            _rsg_shaped_buy_grade(),
            _thesis_health_confused_entry("GOOGL"),
        ],
    }

    caplog_records = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            caplog_records.append(record.getMessage())

    logger = logging.getLogger("src.agents.evening_analyst")
    handler = _CaptureHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        out = EveningAnalystAgent._drop_invalid_entries(
            parsed,
            "buy_grades",
            BuyGrade,
            allowed_symbols=allowed,
        )
    finally:
        logger.removeHandler(handler)

    syms = [g["symbol"] for g in out["buy_grades"]]
    assert syms == ["RSG"], f"expected only RSG to survive; got {syms}"

    out_of_scope_msgs = [m for m in caplog_records if "out of scope" in m]
    assert out_of_scope_msgs, f"expected a distinct out-of-scope warning; got {caplog_records}"
    assert "GOOGL" in out_of_scope_msgs[0]
    # Must be distinguishable from a plain schema-validation failure message.
    assert "Thesis Health Review" in out_of_scope_msgs[0]
    # Must NOT be logged as an ordinary "dropping malformed" schema failure —
    # that's the confusion this guard exists to prevent.
    assert not any("dropping malformed buy_grades entry for GOOGL" in m for m in caplog_records)

    # The surviving RSG entry must still validate cleanly end-to-end.
    validated = BuyGrade(**out["buy_grades"][0])
    assert validated.symbol == "RSG"
    assert validated.grade == "correct"


def test_scope_guard_does_not_drop_legitimate_grade_when_no_allowed_symbols_given():
    """Backward compatibility / no-regression check: when allowed_symbols is
    not passed (None), the scope guard must be a complete no-op — every
    existing call site that doesn't pass it (or any future one) must behave
    exactly as before. This pins that the guard can never fire unless a
    caller explicitly opts in with a candidate set."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = {"buy_grades": [_rsg_shaped_buy_grade()]}
    out = EveningAnalystAgent._drop_invalid_entries(parsed, "buy_grades", BuyGrade)
    assert len(out["buy_grades"]) == 1
    assert out["buy_grades"][0]["symbol"] == "RSG"


def test_scope_guard_is_case_insensitive_on_symbol():
    """A legitimate grade must not be dropped merely because the LLM cased
    the symbol differently than the candidate list (defensive — tickers are
    normalized uppercase elsewhere, but the guard itself must not add a new
    false-drop risk on casing)."""
    from src.agents.evening_analyst import EveningAnalystAgent

    parsed = {"buy_grades": [dict(_rsg_shaped_buy_grade(), symbol="rsg")]}
    out = EveningAnalystAgent._drop_invalid_entries(
        parsed,
        "buy_grades",
        BuyGrade,
        allowed_symbols={"RSG"},
    )
    assert len(out["buy_grades"]) == 1


def test_analyze_end_to_end_scope_guard_keeps_rsg_drops_googl_confusion(monkeypatch):
    """Full analyze() path: mock the LLM call to return the exact
    RSG-shaped confusion, and confirm the persisted EveningReport keeps the
    RSG grade while the GOOGL confusion entry never reaches EveningReport
    construction (so it can't corrupt the whole report the way the
    2026-05-01 missed_opportunities incident did)."""
    from src.agents.evening_analyst import EveningAnalystAgent

    agent = EveningAnalystAgent.__new__(EveningAnalystAgent)

    recent_buys = [
        {
            "symbol": "RSG",
            "buy_date": "2026-08-28",
            "buy_price": 210.50,
            "current_price": 213.10,
            "pct_move_since_buy": 1.24,
        }
    ]

    llm_response = _valid_evening_json()
    llm_response["buy_grades"] = [
        _rsg_shaped_buy_grade(),
        _thesis_health_confused_entry("GOOGL"),
    ]

    class _FakeResult:
        def parse_json(self):
            return llm_response

    monkeypatch.setattr(agent, "run", lambda **kwargs: _FakeResult())

    report, _result = agent.analyze(
        positions=[],
        macro_summary={},
        total_value=100_000.0,
        daily_pnl=0.0,
        daily_return_pct=0.0,
        recent_buys=recent_buys,
        recent_sells=[],
    )
    assert report is not None
    syms = [g.symbol for g in report.buy_grades]
    assert syms == ["RSG"], f"expected only RSG in final report; got {syms}"


def test_analyze_omitting_recent_buys_kwarg_keeps_guard_inactive(monkeypatch):
    """Regression (caught in CI on the first version of this fix): a caller
    of analyze() that doesn't pass recent_buys/recent_sells at all (they
    default to None) must NOT have every legitimate grade wiped out. The
    original fix built `allowed_symbols` via `recent_buys or []`, which
    collapses "no scope info given" and "explicitly zero candidates" into
    the same empty set — turning the guard on with nothing in it, so it
    rejected every grade including well-formed ones. `None` must mean the
    guard stays off; only an explicit `[]` should make it actively reject
    everything."""
    from src.agents.evening_analyst import EveningAnalystAgent

    agent = EveningAnalystAgent.__new__(EveningAnalystAgent)
    llm_response = _valid_evening_json()
    llm_response["buy_grades"] = [_rsg_shaped_buy_grade()]

    class _FakeResult:
        def parse_json(self):
            return llm_response

    monkeypatch.setattr(agent, "run", lambda **kwargs: _FakeResult())

    report, _result = agent.analyze(
        positions=[],
        macro_summary={},
        total_value=100_000.0,
        daily_pnl=0.0,
        daily_return_pct=0.0,
    )
    assert report is not None
    assert len(report.buy_grades) == 1
    assert report.buy_grades[0].symbol == "RSG"
