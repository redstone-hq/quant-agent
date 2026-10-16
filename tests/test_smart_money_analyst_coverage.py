"""The smart-money seat answers every symbol it is sent, or the gap is recorded.

Before this, the seat was sent at most 8 symbols and 3 transactions each (both
made-up caps) and a symbol the reply skipped only produced a log warning. Now
every symbol and every transaction is sent, each symbol must come back as a
finding or an explicit "no read", the unanswered ones are re-asked once in one
follow-up request, and anything still unanswered is returned as a gap the
stage records per symbol.
"""

import json

from src.agents.base import AgentResult
from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from src.models import SmartMoneySynthesis
from tests.test_smart_money import _insider

SYMBOLS = [f"ZZZ{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(39)]


def _observations(per_symbol: int = 4):
    return [
        _insider(
            symbol=symbol,
            owner=f"{s_index}-{t_index}",
            value=(s_index + 1) * 10_000 + t_index,
            accession=f"{s_index * 10 + t_index + 1:010d}-26-000001",
            row=t_index,
        )
        for s_index, symbol in enumerate(SYMBOLS)
        for t_index in range(per_symbol)
    ]


def _finding(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "stance": "bullish",
        "economic_role": "confirmatory",
        "summary": "director bought",
        "why_now": "filed today",
    }


def _analyst(tmp_path, replies):
    """An analyst whose provider returns `replies` in order and records the
    symbols each request carried."""
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.model = "test"
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    sent: list[list[str]] = []

    def run(**kwargs):
        message = analyst.build_user_message(**kwargs)
        payload = json.loads(message.split("\n", 1)[1])
        sent.append([row["symbol"] for row in payload["symbol_facts"]])
        return AgentResult(json.dumps(replies[len(sent) - 1]), 10, "test", input_tokens=5, output_tokens=5)

    analyst.run = run
    return analyst, sent


def test_every_symbol_and_every_transaction_is_sent():
    analyst = object.__new__(SmartMoneyAnalystAgent)
    message = analyst.build_user_message(observations=_observations())
    payload = json.loads(message.split("\n", 1)[1])

    assert sorted(row["symbol"] for row in payload["symbol_facts"]) == sorted(SYMBOLS)
    assert all(len(row["transactions"]) == 4 for row in payload["symbol_facts"])
    assert "at most" not in message


def test_reply_missing_two_symbols_is_reasked_once_for_only_those_two(tmp_path):
    missing = SYMBOLS[5:7]
    first = {"findings": [_finding(s) for s in SYMBOLS if s not in missing]}
    second = {"findings": [_finding(missing[0])], "no_reads": [{"symbol": missing[1]}]}
    analyst, sent = _analyst(tmp_path, [first, second])

    findings, result, error, gaps = analyst.analyze_with_gaps(_observations())

    assert len(sent) == 2
    assert len(sent[0]) == 39
    assert sorted(sent[1]) == sorted(missing)
    assert error is None
    assert sorted(g["symbol"] for g in gaps) == sorted(missing)
    assert all(g["first_pass"] == "missing" and g["after_reask"] == "answered" for g in gaps)
    # Both provider requests are booked on the one logged result.
    assert result.provider_requests == 2 and result.tokens_used == 20
    assert len(findings) == 38


def test_symbols_still_missing_after_the_reask_are_returned_as_gaps(tmp_path):
    missing = SYMBOLS[:2]
    first = {"findings": [_finding(s) for s in SYMBOLS if s not in missing]}
    analyst, sent = _analyst(tmp_path, [first, {"findings": []}])

    findings, _result, error, gaps = analyst.analyze_with_gaps(_observations())

    assert len(sent) == 2  # exactly one re-ask, never a loop
    assert sorted(g["symbol"] for g in gaps) == sorted(missing)
    assert all(g["first_pass"] == "missing" and g["after_reask"] == "missing" for g in gaps)
    assert error == "analysis_partial_schema_error"
    assert len(findings) == 37


def test_rejected_finding_is_reasked_and_recorded_as_rejected(tmp_path):
    bad = dict(_finding(SYMBOLS[0]), stance="bearish")  # a purchase cannot be bearish
    first = {"findings": [bad] + [_finding(s) for s in SYMBOLS[1:]]}
    analyst, sent = _analyst(tmp_path, [first, {"findings": [bad]}])

    _findings, _result, _error, gaps = analyst.analyze_with_gaps(_observations())

    assert sent[1] == [SYMBOLS[0]]
    assert gaps == [{"symbol": SYMBOLS[0], "first_pass": "rejected", "after_reask": "rejected"}]


def test_no_read_answers_the_symbol_but_is_never_a_verdict(tmp_path):
    no_read = SYMBOLS[3]
    reply = {
        "findings": [_finding(s) for s in SYMBOLS if s != no_read],
        "no_reads": [{"symbol": no_read, "reason": "routine only"}],
    }
    analyst, sent = _analyst(tmp_path, [reply])

    findings, _result, error, gaps = analyst.analyze_with_gaps(_observations())

    assert len(sent) == 1  # answered, so no re-ask
    assert error is None and gaps == []
    assert no_read not in {f.symbol for f in findings}
    # A no-read is a separate schema entry with no stance: nothing that reads
    # findings (ranking, the PM, the evidence registry) can see it.
    schema = SmartMoneySynthesis.model_json_schema()
    assert "no_reads" in schema["properties"]
    no_read_schema = schema["$defs"]["SmartMoneyNoRead"]["properties"]
    assert "stance" not in no_read_schema and "economic_role" not in no_read_schema

    # A cache hit replays the no-read without a provider call.
    again, cached, error = analyst.analyze(_observations())
    assert len(sent) == 1 and error is None and cached.provider_requests == 0
    assert no_read not in {f.symbol for f in again}
