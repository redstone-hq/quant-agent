import json
import pathlib
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.agents.tech_analyst import TechAnalystAgent, _CHUNK_SIZE
from src.cost_circuit import LLMCostCircuitBreaker
from src.data.levels import find_structural_levels
from src.models import OHLCV, TechnicalIndicators
from src.storage.db import Database


@pytest.fixture
def sample_indicators():
    return TechnicalIndicators(
        symbol="SPY",
        ma_20=505.0,
        ma_50=498.0,
        ma_200=480.0,
        rsi_14=58.0,
        macd=1.5,
        macd_signal=1.2,
        macd_hist=0.3,
        bb_upper=520.0,
        bb_middle=505.0,
        bb_lower=490.0,
        atr_14=8.5,
        volume_change_pct=15.0,
    )


@pytest.fixture
def sample_bars():
    return [
        OHLCV(date=date(2026, 4, 7), open=503.0, high=510.0, low=500.0, close=507.0, volume=1_000_000),
    ]


def _sym_data(symbol: str, bars, indicators):
    # The batch API expects this shape
    return [{"symbol": symbol, "bars": bars, "indicators": indicators}]


def _valid_response_for(symbol: str) -> str:
    """JSON response covering the full v2 schema (reasoning_chain + conviction + reference_target)."""
    return json.dumps(
        [
            {
                "symbol": symbol,
                "rating": "buy",
                "conviction": "high",
                "entry_price": 507.0,
                "reference_target": 530.0,
                "stop_loss": 494.0,
                "support_levels": [494.0],
                "resistance_levels": [530.0],
                "setup_type": "range",
                "expected_horizon_sessions": 10,
                "thesis_invalid_if": "Price closes below MA50 on above-average volume",
                "reasoning_chain": {
                    "trend": "Above MA20/50/200 stacked bullish.",
                    "momentum": "RSI 58 neutral-bullish, MACD hist positive.",
                    "volatility": "Mid-band, ATR steady.",
                    "volume": "+15% confirms uptrend.",
                    "support_resistance": "Support MA50 498, resistance upper band 520.",
                },
                "reasoning": "Clean bullish alignment.",
            }
        ]
    )


@patch("anthropic.Anthropic")
def test_tech_analyst_batch_parses_full_schema(mock_cls, sample_indicators, sample_bars):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=_valid_response_for("SPY"))]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    assert "SPY" in results
    spy = results["SPY"]
    assert spy.rating == "buy"
    assert spy.conviction == "high"
    assert spy.reference_target == 530.0
    assert spy.stop_loss == 494.0
    assert spy.reasoning_chain is not None
    assert "bullish" in spy.reasoning_chain.trend.lower()


@patch("anthropic.Anthropic")
def test_tech_analyst_bad_response(mock_cls, sample_indicators, sample_bars):
    """2026-08-19 Tech batch-response symbol-loss fix: a non-JSON response
    must NOT silently return an empty dict — the submitted symbol still
    comes back as an explicit key, mapped to None (a visible failure),
    even after the bounded retry (the mock keeps returning the same bad
    text, so retry can't rescue it either)."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="I think it's bullish but I'm not sure")]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))
    assert results == {"SPY": None}
    # Retried exactly once (bounded) — two total LLM calls, not an
    # unbounded retry loop.
    assert mock_client.messages.create.call_count == 2


def test_build_user_message_includes_indicators_and_current_close(sample_indicators, sample_bars):
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(symbols_data=_sym_data("SPY", sample_bars, sample_indicators))
        assert "SPY" in msg
        # Prices are rendered through _px, which drops float64 conversion
        # noise AND a bare trailing ".0" — 505.0 and 505 are the same number,
        # and asserting the spelling rather than the value is what made this
        # test fail on a change that lost no information (2026-08-31).
        assert "MA20=505" in msg and "MA20=505.0" not in msg
        assert "RSI=58" in msg  # rsi_14
        assert "ATR=8.5" in msg  # ATR is surfaced for ATR-based stops
        # Renamed from "Current close" (2026-08-19): with intraday context
        # now possible, calling the last completed daily close "current"
        # was the exact ambiguity that let a stale price read as live.
        assert "Last completed close: 507" in msg
        # With no prior_ratings passed, no prior line should appear
        assert "Prior rating" not in msg


def test_build_user_message_surfaces_prior_rating_with_age(sample_indicators, sample_bars):
    """When prior_ratings is supplied, the LLM sees a 'Prior rating' context line."""
    from datetime import timedelta
    from src.util.time import et_today

    prior = {
        "SPY": {
            "rating": "buy",
            "conviction": "high",
            "first_seen_date": (et_today() - timedelta(days=4)).isoformat(),
            "last_rating_date": et_today().isoformat(),
            "entry_price": 500.0,
            "stop_loss": 490.0,
            "reference_target": 525.0,
        }
    }
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("SPY", sample_bars, sample_indicators),
            prior_ratings=prior,
        )
        assert "Prior rating (context): buy" in msg
        assert "4d ago" in msg
        assert "entry 500.0" in msg  # prior price surfaced


def test_build_user_message_surfaces_valuation_when_provided(sample_indicators, sample_bars):
    valuations = {"SPY": {"trailing_pe": 28.5, "forward_pe": 26.1, "ps_ratio": 4.2}}
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("SPY", sample_bars, sample_indicators),
            valuations=valuations,
        )
        assert "Valuation: trailing PE 28.5 | forward PE 26.1 | P/S 4.2" in msg


def test_build_user_message_hides_valuation_when_all_none(sample_indicators, sample_bars):
    """ETFs typically return all-None valuations — should not render the line at all."""
    valuations = {"SPY": {"trailing_pe": None, "forward_pe": None, "ps_ratio": None}}
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("SPY", sample_bars, sample_indicators),
            valuations=valuations,
        )
        assert "Valuation:" not in msg


def test_build_user_message_omits_prior_for_new_symbol(sample_indicators, sample_bars):
    """A symbol with no prior entry should not have a Prior rating line."""
    with patch("anthropic.Anthropic"):
        agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
        msg = agent.build_user_message(
            symbols_data=_sym_data("NEWSYMBOL", sample_bars, sample_indicators),
            prior_ratings={"OTHER_SYMBOL": {"rating": "buy"}},
        )
        assert "Prior rating" not in msg


@pytest.fixture
def fixed_chunks(monkeypatch):
    """Pin the batch split to the classic fixed 25 symbols per request.

    These tests are about the SHARED missing-symbol recovery budget across
    chunks, and their arithmetic depends on how many chunks there are. Since
    2026-08-31 the real split is packed to a token budget, so on tiny
    fixtures it produces a different (correct) number of chunks and the
    retry arithmetic no longer lines up. Pinning it here keeps each test
    about one thing: chunk SIZING is covered by
    test_tech_analyst_auto_chunks_large_batch and
    tests/test_token_budget.py, retry SEMANTICS are covered here.
    """
    from src.agents.tech_analyst import TechAnalystAgent, _CHUNK_SIZE

    def _fixed(self, symbols_data, *args, **kwargs):
        return [symbols_data[i : i + _CHUNK_SIZE] for i in range(0, len(symbols_data), _CHUNK_SIZE)]

    monkeypatch.setattr(TechAnalystAgent, "_split_to_budget", _fixed)


def _assert_clean_split(asked_per_call, syms):
    """Every symbol asked exactly once, in more than one request, none over
    the per-call cap. The sizes themselves are a budget outcome, not a
    constant — asserting them pinned the arithmetic instead of the property.
    """
    from src.agents.tech_analyst import _MAX_SYMBOLS_PER_CALL

    assert len(asked_per_call) > 1, "a large batch must still be split"
    flat = [s for asked in asked_per_call for s in asked]
    assert sorted(flat) == sorted(syms), "every symbol asked exactly once"
    assert all(len(a) <= _MAX_SYMBOLS_PER_CALL for a in asked_per_call)


@patch("anthropic.Anthropic")
def test_tech_analyst_auto_chunks_large_batch(mock_cls, sample_indicators, sample_bars):
    """Batches > 30 symbols are split into chunks of 25 to avoid context overflow."""
    # Build 50 symbols.
    syms = [f"SYM{i:02d}" for i in range(50)]
    data = [
        {
            "symbol": s,
            "bars": sample_bars,
            "indicators": TechnicalIndicators(**{**sample_indicators.model_dump(), "symbol": s}),
        }
        for s in syms
    ]

    # Answer whatever was actually asked. Hardcoding syms[:25]/syms[25:] tied
    # this mock to one particular split, so it silently returned the wrong
    # symbols the moment chunking became a token budget rather than a count —
    # and the resulting "missing symbol" recovery pass looked like a chunking
    # bug instead of a stale fixture.
    calls = []

    def _chunk_response(**kw):
        content = kw.get("messages", [{}])[0].get("content", "")
        asked = [sym for sym in syms if f"### {sym}" in content]
        calls.append(asked)
        arr = [json.loads(_valid_response_for(s))[0] for s in asked]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 1000
        resp.usage.output_tokens = 500
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _chunk_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, merged = agent.analyze_batch(data)

    # All 50 symbols present, split across more than one request, each asked
    # exactly once. The number of requests is a budget outcome now.
    assert len(results) == 50
    _assert_clean_split(calls, syms)
    n_calls = mock_client.messages.create.call_count
    assert n_calls == len(calls)
    # Token accounting aggregates across chunks.
    assert merged.tokens_used == 1500 * n_calls
    # Cost accounting also aggregates across chunks — was buggy until
    # 2026-05-13 (the merged AgentResult only summed tokens_used and
    # left input_tokens / output_tokens / cost_usd at their defaults,
    # which made every real morning's tech_analyst row land in DB
    # with cost_usd=NULL → Telegram push showed "$?.??").
    assert merged.input_tokens == 1000 * 2
    assert merged.output_tokens == 500 * 2
    # Note: model used here is 'claude-sonnet-4-6-20250514' which is
    # NOT in cost_table.PRICING — so cost_usd should be None (any
    # unknown-model chunk flags the whole merged value as unknown).
    assert merged.cost_usd is None


@patch("anthropic.Anthropic")
def test_tech_analyst_chunked_merged_cost_sums_when_model_priced(
    mock_cls,
    fixed_chunks,
    sample_indicators,
    sample_bars,
    monkeypatch,
):
    """Pin the happy path: when the configured model IS in cost_table.PRICING
    (e.g. claude-opus-4-7), the merged AgentResult.cost_usd is the sum
    of per-chunk costs — not None and not the cost of just the first chunk.

    Uses a test-fixture PRICING with known rates (input=$10/M, output=$50/M)
    instead of reading the live PRICING dict for both sides of the
    comparison. Tautological pattern: if PRICING were zeroed out,
    expected=$0 and actual=$0 — test would pass while production silently
    misreports cost. Fixed fixture rates make a real math regression visible.
    """
    # Pin pricing for this test; monkeypatch auto-reverts at test exit.
    from src import cost_table

    monkeypatch.setitem(
        cost_table.PRICING,
        "claude-opus-4-7",
        {"input": 10.0, "output": 50.0},
    )

    syms = [f"SYM{i:02d}" for i in range(50)]
    data = [
        {
            "symbol": s,
            "bars": sample_bars,
            "indicators": TechnicalIndicators(**{**sample_indicators.model_dump(), "symbol": s}),
        }
        for s in syms
    ]

    call_counter = {"n": 0}

    def _chunk_response(**kw):
        call_counter["n"] += 1
        chunk_syms = syms[:25] if call_counter["n"] == 1 else syms[25:]
        arr = [json.loads(_valid_response_for(s))[0] for s in chunk_syms]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 80_000  # realistic tech_analyst chunk
        resp.usage.output_tokens = 12_000
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _chunk_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-opus-4-7")
    _, merged = agent.analyze_batch(data)

    # 2 chunks × (80_000 × $10/M + 12_000 × $50/M) = 2 × $1.40 = $2.80
    expected = 2 * (80_000 * 10.0 + 12_000 * 50.0) / 1_000_000
    assert merged.cost_usd is not None
    assert abs(merged.cost_usd - expected) < 0.01
    assert merged.input_tokens == 80_000 * 2
    assert merged.output_tokens == 12_000 * 2


# ---------------------------------------------------------------------------
# 2026-08-19 Tech batch-response symbol-loss fix.
#
# Production incident: symbols passed the pre-filter and were sent to
# tech_analyst, but silently disappeared during batch parsing — one chunk
# parsed only 1/10 submitted symbols. The old code iterated over whatever
# the LLM returned and computed a `missing` set purely for a WARNING log,
# so an unrepresented symbol was simply absent from the returned dict:
# indistinguishable, downstream, from "never asked about".
#
# Contract now: every submitted symbol is a KEY in the returned dict —
# a TechAnalysisResult (parsed, incl. an explicitly neutral/sell rating)
# or None (visibly failed after a bounded retry). Never absent.
# ---------------------------------------------------------------------------


def _multi_sym_data(symbols, bars, indicators):
    return [
        {"symbol": s, "bars": bars, "indicators": TechnicalIndicators(**{**indicators.model_dump(), "symbol": s})}
        for s in symbols
    ]


@patch("anthropic.Anthropic")
def test_short_response_retries_and_recovers_the_missing_symbols(
    mock_cls,
    sample_indicators,
    sample_bars,
):
    """The exact incident shape: the first response covers only 1 of 10
    submitted symbols. The bounded retry re-asks for exactly the 9 missing
    ones and recovers them — no symbol silently dropped, and the retry
    prompt carries only the missing symbols (not the whole batch again)."""
    syms = [f"SYM{i:02d}" for i in range(10)]

    calls = {"n": 0, "retry_symbols": None}

    def _respond(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            arr = [json.loads(_valid_response_for("SYM00"))[0]]
        else:
            # Record what the retry actually asked about.
            calls["retry_symbols"] = [s for s in syms if f"### {s}" in kw.get("messages", [{}])[0].get("content", "")]
            arr = [json.loads(_valid_response_for(s))[0] for s in syms[1:]]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, merged = agent.analyze_batch(_multi_sym_data(syms, sample_bars, sample_indicators))

    assert set(results.keys()) == set(syms), "every submitted symbol must be a key"
    assert all(v is not None for v in results.values()), "retry must recover all 9"
    assert calls["n"] == 2, "exactly one bounded retry"
    assert calls["retry_symbols"] == syms[1:], "retry re-asks only the missing symbols"
    # Retry tokens/cost are merged into the reported result, never dropped.
    assert merged.input_tokens == 1000
    assert merged.output_tokens == 400


@patch("anthropic.Anthropic")
def test_symbols_unresolved_after_retry_are_explicit_none_not_absent(
    mock_cls,
    sample_indicators,
    sample_bars,
):
    """When the retry ALSO comes back short, the still-missing symbols are
    returned as explicit None keys — a visible terminal failure — rather
    than vanishing from the dict."""
    syms = ["AAA", "BBB", "CCC"]

    def _respond(**kw):
        # Always answers about AAA only, no matter what was asked.
        arr = [json.loads(_valid_response_for("AAA"))[0]]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(arr))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_multi_sym_data(syms, sample_bars, sample_indicators))

    assert set(results.keys()) == set(syms)
    assert results["AAA"] is not None
    assert results["BBB"] is None
    assert results["CCC"] is None


@patch("anthropic.Anthropic")
def test_explicitly_neutral_rating_is_a_terminal_outcome_not_a_loss(
    mock_cls,
    sample_indicators,
    sample_bars,
):
    """A considered-and-passed symbol (neutral/sell) is a successful
    terminal outcome — it must come back as a real result and must NOT
    trigger the retry path."""
    resp_json = json.dumps(
        [
            {
                "symbol": "SPY",
                "rating": "neutral",
                "conviction": "low",
                "reasoning_chain": {
                    "trend": "Flat.",
                    "momentum": "RSI mid.",
                    "volatility": "Quiet.",
                    "volume": "Average.",
                    "support_resistance": "Range-bound.",
                },
                "reasoning": "No edge here.",
            }
        ]
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=resp_json)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    assert results["SPY"] is not None
    assert results["SPY"].rating == "neutral"
    assert mock_client.messages.create.call_count == 1, "no retry for a valid neutral"


@patch("anthropic.Anthropic")
def test_schema_invalid_row_is_retried_then_marked_failed(
    mock_cls,
    sample_indicators,
    sample_bars,
):
    """A row that fails TechAnalysisResult validation counts as missing
    (it produced no usable analysis), gets retried, and — if still bad —
    ends as an explicit None rather than a silent omission."""
    bad_row = json.dumps(
        [
            {
                "symbol": "SPY",
                "rating": "buy",
                "conviction": "high",
                # Stop ABOVE entry on a buy: a contradiction the validator still
                # rejects. (A missing entry is recorded, not rejected, since
                # 2026-10-12 — tests/test_tech_missing_levels_reach_pm.py.)
                "entry_price": 100.0,
                "stop_loss": 105.0,
                "reasoning_chain": {
                    "trend": "x",
                    "momentum": "x",
                    "volatility": "x",
                    "volume": "x",
                    "support_resistance": "x",
                },
                "reasoning": "Broken row.",
            }
        ]
    )
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=bad_row)]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    assert results == {"SPY": None}
    assert mock_client.messages.create.call_count == 2


@patch("anthropic.Anthropic")
def test_actionable_row_missing_falsifier_is_recorded_not_retried(
    mock_cls,
    sample_indicators,
    sample_bars,
):
    """A buy that omits thesis_invalid_if is kept with the gap RECORDED
    (2026-10-12, XOM incident): no paid retry, nothing invented."""
    missing = json.loads(_valid_response_for("SPY"))[0]
    missing.pop("thesis_invalid_if")
    filled = json.loads(_valid_response_for("SPY"))[0]
    calls = {"n": 0}

    def _respond(**kw):
        calls["n"] += 1
        payload = [missing] if calls["n"] == 1 else [filled]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps(payload))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    assert results["SPY"] is not None
    assert results["SPY"].missing_levels == ["thesis_invalid_if"]
    assert calls["n"] == 1


@patch("anthropic.Anthropic")
def test_chunked_batch_never_loses_a_symbol_across_chunks(
    mock_cls,
    sample_indicators,
    sample_bars,
):
    """Chunk-level guarantee composes to the batch level: with 50 symbols
    (2 chunks) where the second chunk's response is entirely unusable,
    every one of the 50 is still a key — chunk 1's parsed, chunk 2's
    explicitly None."""
    syms = [f"SYM{i:02d}" for i in range(50)]

    call_counter = {"n": 0}

    def _respond(**kw):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            arr = [json.loads(_valid_response_for(s))[0] for s in syms[:25]]
            text = json.dumps(arr)
        else:
            text = "the model rambled instead of returning JSON"
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_multi_sym_data(syms, sample_bars, sample_indicators))

    assert set(results.keys()) == set(syms), "no symbol may vanish between chunks"
    assert all(results[s] is not None for s in syms[:25])
    assert all(results[s] is None for s in syms[25:])


@patch("anthropic.Anthropic")
def test_chunked_batch_shares_one_missing_symbol_retry_budget(
    mock_cls,
    fixed_chunks,
    sample_indicators,
    sample_bars,
):
    """Large batches must not spend one logical repair per chunk.

    The paid circuit's retry ceiling is session-wide.  When three chunks
    each omit one symbol, Tech consolidates all omissions into its single
    shared recovery and never requests the third per-chunk repair that would
    suspend the whole morning run.
    """
    syms = [f"SYM{i:02d}" for i in range(75)]
    asked_per_call = []

    def _respond(**kw):
        content = kw.get("messages", [{}])[0].get("content", "")
        asked = [symbol for symbol in syms if f"### {symbol}" in content]
        asked_per_call.append(asked)
        # Only a FULL primary chunk drops a symbol; the recovery call that
        # re-asks for the missing ones must answer in full or recovery can
        # never succeed. These tests pin the split at 25 (see fixed_chunks).
        returned = asked[1:] if len(asked) == _CHUNK_SIZE else asked
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps([json.loads(_valid_response_for(symbol))[0] for symbol in returned]))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_multi_sym_data(syms, sample_bars, sample_indicators))

    assert set(results) == set(syms)
    assert all(result is not None for result in results.values())
    # Split pinned by `fixed_chunks`, so the exact shape is meaningful here:
    # three full primary chunks, then ONE shared recovery call carrying every
    # omitted symbol — not three separate per-chunk repairs.
    assert [len(asked) for asked in asked_per_call] == [25, 25, 25, 3]
    assert asked_per_call[-1] == ["SYM00", "SYM25", "SYM50"]
    assert mock_client.messages.create.call_count == 4  # 3 chunks + 1 repair


@patch("anthropic.Anthropic")
def test_chunked_batch_bounds_shared_recovery_to_one_chunk(
    mock_cls,
    fixed_chunks,
    sample_indicators,
    sample_bars,
):
    """A severely incomplete batch still makes only one bounded repair."""
    syms = [f"SYM{i:02d}" for i in range(75)]
    asked_per_call = []

    def _respond(**kw):
        content = kw.get("messages", [{}])[0].get("content", "")
        asked = [symbol for symbol in syms if f"### {symbol}" in content]
        asked_per_call.append(asked)
        # All three primary calls fail to cover their symbols.  The sole
        # recovery call resolves only its deterministic first 25.
        returned = asked if len(asked_per_call) == 4 else []
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps([json.loads(_valid_response_for(symbol))[0] for symbol in returned]))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_multi_sym_data(syms, sample_bars, sample_indicators))

    assert [len(asked) for asked in asked_per_call] == [25, 25, 25, 25]
    assert asked_per_call[-1] == syms[:25]
    assert all(results[symbol] is not None for symbol in syms[:25])
    assert all(results[symbol] is None for symbol in syms[25:])


@patch("anthropic.Anthropic")
def test_chunked_batch_composes_with_session_retry_circuit(
    mock_cls,
    fixed_chunks,
    sample_indicators,
    sample_bars,
    tmp_path,
):
    """Production-scale Tech uses one of the session's two retry slots."""
    syms = [f"SYM{i:02d}" for i in range(75)]

    def _respond(**kw):
        content = kw.get("messages", [{}])[0].get("content", "")
        asked = [symbol for symbol in syms if f"### {symbol}" in content]
        # Only a FULL primary chunk drops a symbol; the recovery call that
        # re-asks for the missing ones must answer in full or recovery can
        # never succeed. These tests pin the split at 25 (see fixed_chunks).
        returned = asked[1:] if len(asked) == _CHUNK_SIZE else asked
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps([json.loads(_valid_response_for(symbol))[0] for symbol in returned]))]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client

    db_path = tmp_path / "tech-circuit.db"
    db = Database(str(db_path))
    db.initialize()
    db.close()
    circuit = LLMCostCircuitBreaker(
        str(db_path),
        SimpleNamespace(
            enabled=True,
            session_cost_limit_usd=100.0,
            daily_cost_limit_usd=200.0,
            # Item 14 (2026-09-02): the runaway-loop backstop is now a
            # plain call count, not a reservation-era retry-attempts
            # budget. 4 admits the 3 primary chunk calls plus exactly one
            # recovery -- the same "one of the session's slots" shape the
            # old max_retry_attempts_per_session=2 config produced here.
            max_calls_per_session=4,
            max_provider_attempts_per_call=2,
            input_chars_per_token=3.5,
            require_telegram_alerts=False,
        ),
        MagicMock(enabled=True),
    )
    circuit.activate_session("run-tech-shared-retry", "morning")

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    agent.set_cost_circuit(circuit)
    results, _ = agent.analyze_batch(_multi_sym_data(syms, sample_bars, sample_indicators))

    assert all(result is not None for result in results.values())
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT logical_calls, provider_attempts, retry_attempts, status FROM llm_budget_sessions WHERE run_id = ?",
            ("run-tech-shared-retry",),
        ).fetchone()
    assert row == (4, 4, 1, "active")


@patch("anthropic.Anthropic")
def test_chunked_batch_retains_primaries_when_retry_budget_already_spent(
    mock_cls,
    fixed_chunks,
    sample_indicators,
    sample_bars,
    tmp_path,
):
    syms = [f"SYM{i:02d}" for i in range(75)]

    def _respond(**kw):
        content = kw.get("messages", [{}])[0].get("content", "")
        asked = [symbol for symbol in syms if f"### {symbol}" in content]
        resp = MagicMock()
        resp.content = [
            MagicMock(text=json.dumps([json.loads(_valid_response_for(symbol))[0] for symbol in asked[1:]]))
        ]
        resp.usage.input_tokens = 500
        resp.usage.output_tokens = 200
        return resp

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _respond
    mock_cls.return_value = mock_client
    db_path = tmp_path / "spent-retries.db"
    db = Database(str(db_path))
    db.initialize()
    db.close()
    circuit = LLMCostCircuitBreaker(
        str(db_path),
        SimpleNamespace(
            enabled=True,
            session_cost_limit_usd=100.0,
            daily_cost_limit_usd=200.0,
            # Item 14 (2026-09-02): "retry budget already spent" is now
            # modeled as the plain call-count cap being exhausted by the 3
            # primary chunk calls alone, leaving no room for any recovery
            # -- replacing the old direct `retry_attempts=2` DB seed.
            max_calls_per_session=3,
            max_provider_attempts_per_call=2,
            input_chars_per_token=3.5,
            require_telegram_alerts=False,
        ),
        MagicMock(enabled=True),
    )
    circuit.activate_session("run-tech-spent-retries", "morning")

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6")
    agent.set_cost_circuit(circuit)
    results, merged = agent.analyze_batch(_multi_sym_data(syms, sample_bars, sample_indicators))

    assert mock_client.messages.create.call_count == 3
    assert sum(result is not None for result in results.values()) == 72
    assert [symbol for symbol in syms if results[symbol] is None] == [
        "SYM00",
        "SYM25",
        "SYM50",
    ]
    assert merged is not None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT logical_calls, provider_attempts, retry_attempts, status FROM llm_budget_sessions WHERE run_id=?",
            ("run-tech-spent-retries",),
        ).fetchone()
    # No recovery call was ever admitted -- the 3 primaries alone exhaust
    # the call-count cap -- so retry_attempts stays 0, not the old
    # pre-seeded reservation-era counter value.
    assert row == (3, 3, 0, "active")


# ==========================================================================
# §12.1 — `computed_levels` is PYTHON's field. The model cannot write it.
# ==========================================================================
#
# Since spec §12.1 this field decides whether a tight stop is honoured or
# widened to the ATR noise band. If a language model could set it, the
# verification would be worthless: a model would only have to assert a level
# beside its stop to buy itself an exemption from the noise floor. Two things
# have to hold, and both are pinned here — the field is not in the schema the
# model is shown, and whatever the model sends is overwritten in code after
# parsing.

PROMPT_DIR = pathlib.Path(__file__).resolve().parent.parent / "config" / "prompts"


def _structured_bars() -> list[OHLCV]:
    """A chart that repeatedly turns at $90 and $110 — enough clean history
    for `find_structural_levels` to report real levels."""
    path: list[float] = []
    for _ in range(20):
        path += [90.0 + 20.0 * i / 6 for i in range(6)]
        path += [110.0 - 20.0 * i / 6 for i in range(6)]
    start = date(2024, 1, 1)
    return [
        OHLCV(date=start + timedelta(days=i), open=c, high=c + 0.4, low=c - 0.4, close=c, volume=1_000_000)
        for i, c in enumerate(path)
    ]


def _response_asserting_levels(symbol: str, claimed: list[float]) -> str:
    """The full valid schema, plus a `computed_levels` the model made up."""
    payload = json.loads(_valid_response_for(symbol))
    payload[0]["computed_levels"] = claimed
    return json.dumps(payload)


def test_the_prompt_never_shows_the_model_the_computed_levels_field():
    """Half of the guarantee: it is not in the schema the model is given, so
    a well-behaved model has no reason to emit it at all."""
    prompt = (PROMPT_DIR / "tech_analyst.md").read_text()
    assert "computed_levels" not in prompt


@patch("anthropic.Anthropic")
def test_a_model_asserted_computed_levels_field_is_overwritten_by_code(
    mock_cls,
    sample_indicators,
):
    """The other half, and the one that matters: a MISbehaving model emits
    the field anyway, and code overwrites it unconditionally with what
    `find_structural_levels` actually found in the bars. The model's numbers
    reach nothing downstream. Without this, §12.1's 'verified level' check
    could be satisfied by a model simply saying so."""
    bars = _structured_bars()
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text=_response_asserting_levels("SPY", [999.0, 1000.0]),
        )
    ]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", bars, sample_indicators))

    supports, resistances = find_structural_levels(bars)
    expected = sorted(lv.price for lv in (*supports, *resistances))
    assert expected, "fixture must produce real structure for this to mean anything"

    spy = results["SPY"]
    assert spy.computed_levels == expected
    assert 999.0 not in spy.computed_levels
    assert 1000.0 not in spy.computed_levels
    # 2026-09-03, Phase 12.1 — `computed_level_touches` is the same
    # PYTHON-set, never-model-writable data, carrying each level's touch
    # count as data (no touch count gates a stop since 2026-10-14).
    assert spy.computed_level_touches == {lv.price: lv.touches for lv in (*supports, *resistances)}
    # 2026-09-12: enough clean bars for the scan to run — "measured".
    assert spy.levels_coverage == "measured"
    # 2026-09-12, docs/WORK.md item 54 — the signal bar's edges and the
    # completed-session count are set the same way, from the same bars:
    # the constructor's instrument-read fallback stop and its "too young
    # to measure" refusal read these, never anything the model typed.
    assert spy.signal_bar_low == bars[-1].low
    assert spy.signal_bar_high == bars[-1].high
    assert spy.bars_available == len(bars)


@patch("anthropic.Anthropic")
def test_asserted_levels_are_wiped_even_when_the_chart_yields_none(
    mock_cls,
    sample_indicators,
    sample_bars,
):
    """Fails closed. One bar is not enough history for any level, and
    `find_structural_levels` says so by returning nothing. The model's
    invented levels must not survive into that gap — an empty list means
    'no structure was computed', which denies the §12.1 exemption rather
    than granting it on the model's say-so."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text=_response_asserting_levels("SPY", [494.0, 530.0]),
        )
    ]
    mock_response.usage.input_tokens = 500
    mock_response.usage.output_tokens = 200
    mock_client.messages.create.return_value = mock_response
    mock_cls.return_value = mock_client

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_sym_data("SPY", sample_bars, sample_indicators))

    # The model named its own stop ($494) as a computed level. It is gone.
    assert results["SPY"].stop_loss == 494.0
    assert results["SPY"].computed_levels == []
    # 2026-09-12: and the reason it is empty travels with it — one bar
    # cannot run the scan, a DATA fault downstream, not a measured chart.
    assert results["SPY"].levels_coverage == "unusable_bars"
