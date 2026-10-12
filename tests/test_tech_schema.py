"""Item 157 (docs/WORK.md; from #538's write-up): the technical seat now
declares a `result_model` (`TechAnalystAnswer`) and sends a strict schema on
both routes. WHAT THIS ACTUALLY BUYS TODAY, honestly stated: pydantic still
validates every row exactly as before (a semantically-invalid row is caught
at construction, same as pre-existing behaviour), and the wrapper lets the
#538 per-row salvage keep working after unwrapping `results`. What it does
NOT yet buy — because it has never been confirmed live (see
tests/test_tech_schema_live.py) — is a provider that refuses to emit a
malformed row IN THE FIRST PLACE via constrained decoding; that half of
item 157 stays open on the board. This file covers the new pieces:

  - `TechAnalystAnswerItem` carries only what the model fills in — the eight
    desk-filled (Python-set) fields from `TechAnalysisResult` are absent.
  - `TechAnalystAnswer` is the wrapper object (`{"results": [...]}`) sent as
    the schema root, since a strict json_schema response format requires an
    object, not the seat's old bare list.
  - `_response_format_for(TechAnalystAnswer)` — verified, not assumed — comes
    out strict=true: excluding the eight desk-filled fields also excludes
    `computed_level_touches`, the one free-form map that used to force
    strict=false on the old (unused) full-model schema.
  - `AgentResult.parse_json_rows(list_field="results")` unwraps the wrapper
    before the existing #538 per-row salvage runs, and still accepts a bare
    list (a legacy stored answer, or a route that ignores the schema) so
    nothing that worked before regresses.
  - The old "one malformed row costs only itself" scenario, replayed under
    the NEW wrapper-object answer shape.
"""

import json
import re
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.agents.base import AgentResult, _response_format_for
from src.agents.tech_analyst import TechAnalystAgent
from src.models import (
    OHLCV,
    TechAnalysisResult,
    TechAnalystAnswer,
    TechAnalystAnswerItem,
    TechnicalIndicators,
)

# The fields that are desk-filled: Python-set after the call, never emitted
# by the model. Item 157 / #538's write-up named eight; board items 55 and
# 215 added two more, because deciding whether a stop rests on a level now
# needs the BARS that drew the level and the level's ZONE, both computed
# here and neither ever asked of the model.
_DESK_FILLED_FIELDS = {
    "atr_14",
    "computed_levels",
    "computed_level_touches",
    "levels_coverage",
    "signal_bar_low",
    "signal_bar_high",
    "bars_available",
    "signal_age_days",
    "computed_level_bars",
    "computed_level_zones",
    "input_fingerprint",
    "read_state",
    "reference_target_wrong_side",
    "missing_levels",
}

_VALID_ITEM = {
    "symbol": "SPY",
    "rating": "buy",
    "conviction": "high",
    "entry_price": 505.0,
    "reference_target": 530.0,
    "stop_loss": 494.0,
    "support_levels": [500.0],
    "resistance_levels": [520.0],
    "setup_type": "range",
    "expected_horizon_sessions": 10,
    "reasoning_chain": {
        "trend": "up",
        "momentum": "up",
        "volatility": "calm",
        "volume": "confirming",
        "support_resistance": "clear",
    },
    "reasoning": "clean setup",
    "thesis_invalid_if": "closes below 494",
}


def _result(text: str) -> AgentResult:
    return AgentResult(raw_text=text, tokens_used=0, model="m")


# --- the model split ---------------------------------------------------------


def test_the_desk_filled_fields_are_not_on_the_model_facing_schema():
    model_fields = set(TechAnalystAnswerItem.model_fields)
    assert not (model_fields & _DESK_FILLED_FIELDS), (
        f"leaked desk-filled fields into the model-facing schema: {model_fields & _DESK_FILLED_FIELDS}"
    )
    # Every field TechAnalysisResult carries beyond the model-facing subset
    # must be exactly the named ones -- nothing else quietly moved.
    extra = set(TechAnalysisResult.model_fields) - model_fields
    assert extra == _DESK_FILLED_FIELDS


def test_tech_analysis_result_still_builds_from_a_model_answer_plus_desk_fields():
    """TechAnalysisResult is still what the rest of the codebase consumes;
    splitting the schema must not change how a row is constructed."""
    r = TechAnalysisResult(**_VALID_ITEM)
    assert r.symbol == "SPY"
    assert r.atr_14 is None and r.computed_levels == []
    r.atr_14 = 8.5
    r.computed_levels = [494.0, 530.0]
    assert r.atr_14 == 8.5


def test_the_wrapper_object_holds_a_list_of_the_model_facing_item():
    wrapper = TechAnalystAnswer(results=[_VALID_ITEM])
    assert len(wrapper.results) == 1
    assert isinstance(wrapper.results[0], TechAnalystAnswerItem)


def test_the_seat_declares_the_wrapper_as_its_result_model():
    assert TechAnalystAgent.result_model is TechAnalystAnswer


# --- the schema itself: strict, and object-rooted ----------------------------


def test_the_schema_roots_at_an_object_not_a_list():
    schema = TechAnalystAnswer.model_json_schema()
    assert schema.get("type") == "object"
    assert "results" in schema.get("properties", {})


def test_the_schema_is_strict_now_that_the_free_form_map_is_excluded():
    """Measured, not assumed (2026-09-20): removing the eight desk-filled
    fields from the model-facing schema also removes `computed_level_touches`
    — the only free-form map anywhere near this seat's answer — so
    `_response_format_for` no longer needs to fall back to strict=false."""
    response_format = _response_format_for(TechAnalystAnswer)
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


# --- the wire schema must carry no internal engineering markers -------------
#
# Adversary review, 2026-09-23: the schema actually sent to the model was
# 5,237 bytes, of which roughly 3,200 came from class docstrings — pydantic
# emits a class's `__doc__` verbatim as that node's "description" — and
# those docstrings named "item 157", "docs/WORK.md", "#538's write-up",
# "tests/test_tech_schema_live.py" and internal decision narrative, on every
# single tech-seat call, on both routes. This desk has an open item about
# prompt text rotting unchecked (docs/WORK.md item 171); this is that same
# failure mode reaching the model-facing SCHEMA rather than the prompt.
# Engineering context now lives in comments beside each class, which do not
# reach `model_json_schema()`. This check is mechanical, not a spot check:
# it fails on any of these markers reappearing anywhere in the schema that
# is actually transmitted, not just in the two classes fixed here.
_INTERNAL_MARKER_PATTERNS = [
    re.compile(r"#\d+"),  # a PR/issue reference like #538
    re.compile(r"\bitems?\s+\d+\b", re.I),  # "item 157" or "items 141 and 168"
    re.compile(r"docs/"),  # a repo doc path
    re.compile(r"\b(?:src|tests|config)/[\w./-]*"),  # a repo source path
    re.compile(r"\.py\b"),  # a bare filename
    re.compile(r"\b\w+\.md\b"),  # a bare doc filename ("WORK.md")
    re.compile(r"\b20\d{2}-\d{2}-\d{2}\b"),  # an internal decision date
]


def test_schema_sent_to_model_has_no_engineering_markers():
    response_format = _response_format_for(TechAnalystAnswer)
    schema_text = json.dumps(response_format)
    hits = {
        pattern.pattern: pattern.findall(schema_text)
        for pattern in _INTERNAL_MARKER_PATTERNS
        if pattern.search(schema_text)
    }
    assert not hits, (
        f"the schema sent to the model on every tech-seat call carries "
        f"internal engineering markers, not just human-facing description: "
        f"{hits}. Move this text into a comment beside the field/class "
        f"instead of its docstring."
    )


# --- parse_json_rows: wrapper-aware, backward-compatible ---------------------


def test_parse_json_rows_unwraps_the_results_key():
    text = json.dumps({"results": [{"symbol": "A"}, {"symbol": "B"}]})
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["A", "B"]
    assert salvage.malformed == []


def test_parse_json_rows_still_accepts_a_bare_list_with_list_field_set():
    """A route that ignores the schema (or a legacy stored answer) must not
    regress just because the caller now passes list_field."""
    text = json.dumps([{"symbol": "A"}, {"symbol": "B"}])
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["A", "B"]


def test_parse_json_rows_without_list_field_is_unchanged():
    """Every other opt-in caller (there are none yet, but the contract
    matters) must see byte-identical behaviour to before this change."""
    text = json.dumps([{"symbol": "A"}])
    assert _result(text).parse_json_rows().rows == [{"symbol": "A"}]


def test_a_dict_answer_missing_the_list_field_falls_back_to_one_row():
    text = json.dumps({"symbol": "A"})
    salvage = _result(text).parse_json_rows(list_field="results")
    assert salvage.rows == [{"symbol": "A"}]


def test_a_sibling_array_after_results_does_not_win_over_the_real_rows():
    """Adversary review, item 157 (2026-09-20): before this test existed, a
    broken answer with ANY array-of-objects appearing after `results` in the
    raw text — here, a stray self-correction fragment — would win outright
    under the old "last array-of-objects anywhere" rule, silently discarding
    the real rows entirely. The scan must prefer the array that is actually
    the VALUE of `results`."""
    text = (
        '{"results": [\n'
        '  {"symbol": "AAA", "reasoning": "fine"},\n'
        '  {"symbol": "BBB", "reasoning": "fine"}\n'
        "],\n"
        '"unrelated_note": "actually let me redo that as"}\n'
        '[{"symbol": "ZZZ-not-a-real-row", "reasoning": "junk"}]'
    )
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["AAA", "BBB"]


# --- a valid object under the WRONG key (adversary review, 2026-09-23) ------
#
# The old bare-array prompt made this impossible — all 292 production tech
# answers stored 2026-08-17 to 2026-09-22, replayed against both the old and
# new parser, are bare arrays with zero differences (see the PR body for the
# measured round-trip). The wrapper schema makes a mis-keyed object a real,
# reproduced failure mode: the model can emit a perfectly valid JSON object
# whose one list-of-dicts value simply isn't named "results". Before this
# fix, `_rows_from` fell through to "treat the whole object as one row",
# which is neither a real row nor a per-symbol MalformedRow — every name in
# the chunk vanishes silently, with no reason logged against any symbol.


def test_wrong_key_signals_is_recovered_as_the_row_list():
    text = json.dumps({"signals": [{"symbol": "AAA"}, {"symbol": "BBB"}]})
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["AAA", "BBB"]
    assert salvage.malformed == []


def test_wrong_key_analysis_is_recovered_as_the_row_list():
    text = json.dumps({"analysis": [{"symbol": "AAA"}, {"symbol": "BBB"}]})
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["AAA", "BBB"]
    assert salvage.malformed == []


def test_null_results_with_the_real_list_under_a_sibling_key_is_recovered():
    text = json.dumps(
        {
            "results": None,
            "data": [{"symbol": "AAA"}, {"symbol": "BBB"}],
        }
    )
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["AAA", "BBB"]
    assert salvage.malformed == []


def test_two_candidate_lists_is_ambiguous_and_does_not_guess():
    """With two equally plausible list-of-dicts values and no `results` key,
    there is no principled way to tell which one is the real answer.
    Guessing risks silently discarding the real rows in favour of an
    unrelated array; this must fall back to the same conservative
    whole-object behaviour parse_json_rows has always had for an
    unrecognised dict, not pick one arbitrarily."""
    text = json.dumps(
        {
            "signals": [{"symbol": "AAA"}],
            "watchlist": [{"symbol": "ZZZ"}],
        }
    )
    salvage = _result(text).parse_json_rows(list_field="results")
    assert salvage.rows == [json.loads(text)]


def test_a_single_row_that_happens_to_nest_a_list_is_not_mistaken_for_rows():
    """2nd adversary pass, 2026-09-23: the fix for the wrong-key case above
    must not itself break the far more common case — a single, ordinary
    row answer that happens to carry ANY nested list-of-objects field of
    its own. Nothing in today's schema does this, but the recovery logic
    must not assume that stays true forever: a single row is recognisable
    because IT carries `key_field` directly, which a mis-keyed wrapper of
    rows never does."""
    text = json.dumps(
        {
            "symbol": "SPY",
            "rating": "buy",
            "levels": [{"price": 1}],
        }
    )
    salvage = _result(text).parse_json_rows(list_field="results")
    assert salvage.rows == [json.loads(text)], (
        "the real single-row answer was discarded in favour of an unrelated nested list"
    )


# --- the old "malformed row needs salvaging" scenario, under the NEW wrapper -


def test_one_broken_row_inside_the_wrapper_still_costs_only_itself():
    """The #538 scenario (one garbled stock must not sink the whole answer)
    replayed with the model actually answering in the new wrapper shape.
    The malformed row is caught by the per-row salvage as before; the schema
    change does not remove that safety net, it adds a stricter one in front
    of it."""
    text = (
        '{"results": [\n'
        '  {"symbol": "AAA", "reasoning": "fine"},\n'
        '  {"symbol": "BBB", "reasoning": "unterminated,\n},\n'
        '  {"symbol": "CCC", "reasoning": "fine"}\n'
        "]}"
    )
    salvage = _result(text).parse_json_rows(list_field="results")
    assert [r["symbol"] for r in salvage.rows] == ["AAA", "CCC"]
    assert [m.key for m in salvage.malformed] == ["BBB"]


def test_null_thesis_invalid_if_is_still_tallied_in_parse_telemetry():
    """Regression, caught by the full test suite (2026-09-20): retyping
    `thesis_invalid_if` to `str | None` (so the wire schema can express the
    null the model legitimately sends) moved it out of `LLMOutputModel`'s
    generic null-droppable-field telemetry, since that mechanism only
    catches fields whose annotation still rejects None. The field's own
    validator now records the coercion directly so nothing about the null
    goes uncounted."""
    from src.models import parse_telemetry

    parse_telemetry.reset()
    item = dict(_VALID_ITEM)
    item["thesis_invalid_if"] = None
    item["rating"] = "neutral"
    TechAnalystAnswerItem(**item)
    assert parse_telemetry.snapshot().get(("TechAnalystAnswerItem", "thesis_invalid_if")) == 1


def test_a_schema_valid_wrapper_with_a_semantically_broken_row_is_caught_at_validation():
    """A row that parses as JSON but violates the pydantic schema (bad enum
    value) must be dropped as validation-failed, not silently accepted —
    this is the "caught before/during parsing, not patched after" guarantee
    item 157 asked for, exercised through the real construction path
    `_analyze_chunk` uses."""
    item = dict(_VALID_ITEM)
    item["rating"] = "extremely_buy"  # not in the Literal enum
    with pytest.raises(Exception):
        TechAnalystAnswerItem(**item)
    with pytest.raises(Exception):
        TechAnalysisResult(**item)


# --- end to end: the real seat, answering in the new wrapper shape -----------


def _symbols_data(symbols):
    bars = [OHLCV(date=date(2026, 9, 16), open=1.0, high=2.0, low=0.5, close=1.5, volume=1_000)]
    return [{"symbol": s, "bars": bars, "indicators": TechnicalIndicators(symbol=s, atr_14=1.0)} for s in symbols]


def _wrapped_response_for(symbol: str) -> str:
    item = dict(_VALID_ITEM)
    item["symbol"] = symbol
    return json.dumps({"results": [item]})


@patch("anthropic.Anthropic")
def test_analyze_batch_parses_the_new_wrapper_shape_end_to_end(mock_cls):
    """The seat's prompt now asks for `{"results": [...]}`; a well-behaved
    answer in that shape must resolve exactly as a bare-array one used to."""
    resp = MagicMock()
    resp.content = [MagicMock(text=_wrapped_response_for("SPY"))]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["SPY"]))

    assert results["SPY"] is not None
    assert results["SPY"].symbol == "SPY"
    assert results["SPY"].rating == "buy"


@patch("anthropic.Anthropic")
def test_analyze_batch_still_parses_a_bare_list_answer(mock_cls):
    """Backward compatibility: a route that ignores the schema, or a replay
    of a legacy stored answer, must not regress."""
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps([_VALID_ITEM]))]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["SPY"]))

    assert results["SPY"] is not None
    assert results["SPY"].rating == "buy"


# --- item 157's runtime hygiene check (2026-09-23) --------------------------
#
# Replaces the abandoned pytest-based live-enforcement DONE WHEN: no
# deployed process ever holds a real GOOGLE_API_KEY, so whether a strict
# schema is actually suppressing fenced markdown and extra keys has to be
# measured against real production answers instead, surfaced through the
# same parse_telemetry path dropped-item and null-coercion counts already
# use (src/pipeline_stages.py). These tests only prove the counters fire
# correctly, not that a real provider violates the schema (that is what the
# recorded counts are for, live).


@pytest.fixture(autouse=True)
def _reset_parse_telemetry():
    from src.models import parse_telemetry as pt

    pt.reset()
    yield
    pt.reset()


@patch("anthropic.Anthropic")
def test_fenced_markdown_around_the_answer_is_recorded_not_rejected(mock_cls):
    from src.models import parse_telemetry as pt

    resp = MagicMock()
    fenced = "```json\n" + _wrapped_response_for("SPY") + "\n```"
    resp.content = [MagicMock(text=fenced)]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["SPY"]))

    assert results["SPY"] is not None, "a hygiene violation must never cost the row — it is evidence, not a gate"
    assert pt.total_hygiene_violations() == 1
    assert "fenced_markdown" in pt.describe_hygiene_violations()


@patch("anthropic.Anthropic")
def test_an_undeclared_key_on_a_row_is_recorded_not_rejected(mock_cls):
    from src.models import parse_telemetry as pt

    item = dict(_VALID_ITEM)
    item["symbol"] = "SPY"
    item["hacked"] = True
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps({"results": [item]}))]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["SPY"]))

    assert results["SPY"] is not None
    assert pt.total_hygiene_violations() == 1
    assert "extra_keys" in pt.describe_hygiene_violations()


@patch("anthropic.Anthropic")
def test_a_clean_answer_records_no_hygiene_violations(mock_cls):
    from src.models import parse_telemetry as pt

    resp = MagicMock()
    resp.content = [MagicMock(text=_wrapped_response_for("SPY"))]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    agent.analyze_batch(_symbols_data(["SPY"]))

    assert pt.total_hygiene_violations() == 0


# --- item 157 closing evidence: the schema is SENT on BOTH wire routes ------
#
# The board item's wording is "no enforced answer format on EITHER route".
# The two routes are the two OpenAI-wire providers this seat is ever given a
# response_format on — OpenRouter and Google's OpenAI-compat endpoint (see
# `_openai_wire_call` in src/agents/base.py). The morning batch and the
# intraday scan share ONE `TechAnalystAgent` instance (src/pipeline.py), so
# covering both providers covers both callers. These tests pin the send on
# each route, and pin that a malformed row arriving over each route is
# refused as a row (recorded, not salvaged into a verdict) while its
# well-formed neighbours survive.


def _fake_wire_client(captured: dict, text: str):
    """Minimal OpenAI-wire double: captures the create() kwargs and streams
    `text` back as one content chunk plus a terminal finish_reason."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    def _create(**kwargs):
        captured.update(kwargs)
        return iter(
            [
                SimpleNamespace(
                    id="gen-test",
                    error=None,
                    model_extra={},
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=None)],
                ),
                SimpleNamespace(
                    id="gen-test",
                    error=None,
                    model_extra={},
                    usage=None,
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")],
                ),
            ]
        )

    client = MagicMock()
    client.chat.completions.create.side_effect = _create
    return client


@pytest.mark.parametrize("route", ["openrouter", "google"])
def test_the_schema_is_sent_on_both_wire_routes(route):
    from src.agents.base import _response_format_for
    from src.models import TechAnalystAnswer

    agent = TechAnalystAgent(api_key="test", model="test-model")
    captured: dict = {}
    client = _fake_wire_client(captured, _wrapped_response_for("SPY"))

    agent._openai_wire_call(client, "test-model", route, "hello")

    sent = captured["extra_body"]["response_format"]
    assert sent == _response_format_for(TechAnalystAnswer)
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["strict"] is True
    # The root must be an object: a bare-array root cannot be sent strict.
    assert sent["json_schema"]["schema"]["type"] == "object"


@pytest.mark.parametrize("route", ["openrouter", "google"])
def test_a_malformed_row_over_each_route_is_refused_not_salvaged_into_a_verdict(route):
    """A row that does not conform is REFUSED at the boundary: it yields no
    verdict for its symbol. The neighbouring good row is unaffected, and the
    refusal is recorded rather than silently dropped."""
    from src.agents.base import AgentResult

    good = dict(_VALID_ITEM, symbol="AAA")
    bad = dict(_VALID_ITEM, symbol="BBB", rating="not-a-rating")
    payload = json.dumps({"results": [good, bad]})

    agent = TechAnalystAgent(api_key="test", model="test-model")
    captured: dict = {}
    client = _fake_wire_client(captured, payload)

    content, _, _, _, _ = agent._openai_wire_call(client, "test-model", route, "hello")

    result = AgentResult(raw_text=content, model="test-model", tokens_used=0)
    salvage = result.parse_json_rows(key_field="symbol", list_field="results")
    parsed_symbols = {row.get("symbol") for row in salvage.rows if isinstance(row, dict)}

    assert "AAA" in parsed_symbols
    # The bad row survives JSON parsing but must not become a usable verdict.
    from src.models import TechAnalystAnswerItem
    import pydantic

    bad_rows = [r for r in salvage.rows if isinstance(r, dict) and r.get("symbol") == "BBB"]
    for row in bad_rows:
        with pytest.raises(pydantic.ValidationError):
            TechAnalystAnswerItem(**row)


@patch("anthropic.Anthropic")
def test_a_refused_row_leaves_the_symbol_with_NO_verdict_not_an_approval(mock_cls):
    """The safety property: refusing more answers must never read as the
    seat approving. An unusable row resolves to `None` for that symbol —
    the desk's "no answer" state — never to a rating."""
    good = dict(_VALID_ITEM, symbol="AAA")
    bad = dict(_VALID_ITEM, symbol="BBB", rating="not-a-rating")
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps({"results": [good, bad]}))]
    resp.usage.input_tokens = 1
    resp.usage.output_tokens = 1
    mock_cls.return_value.messages.create.return_value = resp

    agent = TechAnalystAgent(api_key="test", model="claude-sonnet-4-6-20250514")
    results, _ = agent.analyze_batch(_symbols_data(["AAA", "BBB"]))

    assert results["AAA"] is not None
    assert results["AAA"].rating == "buy"
    assert "BBB" in results
    assert results["BBB"] is None
