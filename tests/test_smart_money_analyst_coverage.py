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
        reply = replies[len(sent) - 1]
        if isinstance(reply, Exception):
            raise reply
        raw = reply if isinstance(reply, str) else json.dumps(reply)
        truncated = isinstance(reply, str)
        return AgentResult(
            raw,
            10,
            "test",
            input_tokens=5,
            output_tokens=5,
            truncated=truncated,
            finish_reason="length" if truncated else "stop",
        )

    analyst.run = run
    return analyst, sent


def test_every_symbol_and_every_transaction_is_sent():
    analyst = object.__new__(SmartMoneyAnalystAgent)
    message = analyst.build_user_message(observations=_observations())
    payload = json.loads(message.split("\n", 1)[1])

    assert sorted(row["symbol"] for row in payload["symbol_facts"]) == sorted(SYMBOLS)
    # Every transaction is counted in its name's one summary line (2026-10-19).
    assert all(row["insider_buys"] == 4 for row in payload["symbol_facts"])
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
    # Answered symbols stand; the gap rows carry the two, not a seat failure.
    assert error is None
    assert len(findings) == 37


def test_rejected_finding_is_reasked_and_recorded_as_rejected(tmp_path):
    bad = dict(_finding(SYMBOLS[0]), stance="bearish")  # a purchase cannot be bearish
    first = {"findings": [bad] + [_finding(s) for s in SYMBOLS[1:]]}
    analyst, sent = _analyst(tmp_path, [first, {"findings": [bad]}])

    _findings, _result, _error, gaps = analyst.analyze_with_gaps(_observations())

    assert sent[1] == [SYMBOLS[0]]
    assert gaps == [
        {"kind": "unanswered_symbol", "symbol": SYMBOLS[0], "first_pass": "rejected", "after_reask": "rejected"}
    ]


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


def _gaps(records):
    return [r for r in records if r["kind"] == "unanswered_symbol"]


def _anomalies(records):
    return [r for r in records if r["kind"] == "reply_anomaly"]


def test_truncated_reply_is_reasked_once_and_its_finish_reason_is_logged(tmp_path):
    from src.agents.base import agent_log_kwargs

    cut_off = '{"findings":[{"symbol":"ZZZA'  # hit the token cap mid-answer
    analyst, sent = _analyst(tmp_path, [cut_off, {"findings": [_finding(s) for s in SYMBOLS]}])

    findings, result, error, records = analyst.analyze_with_gaps(_observations())

    assert len(sent) == 2 and sorted(sent[1]) == sorted(SYMBOLS)
    assert error is None and len(findings) == 39
    assert {r["first_pass"] for r in _gaps(records)} == {"unparseable"}
    assert {r["after_reask"] for r in _gaps(records)} == {"answered"}
    logged = agent_log_kwargs(result)
    assert logged["truncated"] is True
    assert logged["finish_reason"] == "stop"


def test_failed_reask_keeps_the_first_pass_and_records_why(tmp_path):
    from src.cost_circuit.refusal import PaidAnalysisSuspended

    missing = SYMBOLS[:2]
    first = {"findings": [_finding(s) for s in SYMBOLS if s not in missing]}
    analyst, sent = _analyst(tmp_path, [first, PaidAnalysisSuspended("daily cap")])

    findings, _result, error, records = analyst.analyze_with_gaps(_observations())

    assert len(sent) == 2
    assert error is None and len(findings) == 37
    assert sorted(r["symbol"] for r in _gaps(records)) == sorted(missing)
    assert {r["after_reask"] for r in _gaps(records)} == {"reask_failed:PaidAnalysisSuspended"}


def test_duplicates_and_never_sent_symbols_are_recorded(tmp_path):
    dup, clash = SYMBOLS[1], SYMBOLS[2]
    first = {
        "findings": [_finding(s) for s in SYMBOLS]
        + [_finding(dup), dict(_finding(clash), stance="neutral"), _finding("ZZZNOTSENT")],
    }
    analyst, sent = _analyst(tmp_path, [first, {"findings": [_finding(clash)]}])

    findings, _result, error, records = analyst.analyze_with_gaps(_observations())

    reasons = {(r["symbol"], r["reason"]) for r in _anomalies(records)}
    assert (dup, "duplicate") in reasons
    assert (clash, "contradictory_duplicate") in reasons
    assert ("ZZZNOTSENT", "not_sent") in reasons
    # The contradictory symbol is rejected, re-asked, then answered once.
    assert sent[1] == [clash]
    assert [f.symbol for f in findings].count(clash) == 1
    assert error is None


def test_nothing_answered_is_still_a_seat_failure(tmp_path):
    analyst, _sent = _analyst(tmp_path, [{"findings": []}, {"findings": []}])

    findings, _result, error, records = analyst.analyze_with_gaps(_observations())

    assert findings == [] and error == "analysis_schema_error"
    assert len(_gaps(records)) == 39


def test_exam_coverage_compares_sent_with_answered_not_the_agents_gap_list():
    from types import SimpleNamespace

    from ops.model_policy.scenarios import _smart_money_grade

    output = SimpleNamespace(
        findings=[SimpleNamespace(symbol=SYMBOLS[0])],
        error=None,
        gaps=[],  # the agent claims no gaps
        no_read_symbols={SYMBOLS[1]},
        presented=tuple(SYMBOLS[:3]),
    )
    check = next(c for c in _smart_money_grade(output) if c.name == "covers_every_presented_symbol")
    assert check.passed is False
    assert SYMBOLS[2] in check.detail
