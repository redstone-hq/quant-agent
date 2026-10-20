"""The stance validator reads Form 4 insider rows only; congress never vetoes.

Measured 2026-10-20 live: COST and WMT (insider sales only) came back bearish
and UBER (insider purchases only) bullish, and all three were rejected
because a congressional row for the same name pointed the other way. The
seat's prompt states the P-only / S-only / mixed rule on Form 4 open-market
purchases and sales and calls the congressional stream confirmatory context,
so the code is made to match the written rule. A name with no insider rows
keeps the old all-rows behaviour.
"""

from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from tests.test_smart_money import _congress, _insider
from tests.test_smart_money_analyst_coverage import _analyst


def _cong(symbol, direction):
    return _congress(direction=direction).model_copy(update={"symbol": symbol})


def _finding(symbol, stance):
    return {
        "symbol": symbol,
        "stance": stance,
        "economic_role": "confirmatory",
        "summary": "one sentence",
        "why_now": "filed today",
    }


matches = SmartMoneyAnalystAgent._stance_matches_source


def test_insider_purchases_only_with_congress_sale_allows_bullish():
    rows = [_insider(symbol="UBER", direction="buy"), _cong("UBER", "sell")]
    assert matches("bullish", rows)
    assert matches("neutral", rows)
    assert not matches("bearish", rows)
    assert not matches("mixed", rows)


def test_insider_sales_only_with_congress_buy_allows_bearish():
    rows = [_insider(symbol="COST", direction="sell"), _cong("COST", "buy")]
    assert matches("bearish", rows)
    assert matches("neutral", rows)
    assert not matches("bullish", rows)
    assert not matches("mixed", rows)


def test_insider_purchases_and_sales_rejects_one_sided_accepts_mixed():
    rows = [
        _insider(symbol="UBER", owner="1", direction="buy"),
        _insider(symbol="UBER", owner="2", direction="sell", accession="0000000002-26-000001"),
    ]
    assert not matches("bullish", rows)
    assert not matches("bearish", rows)
    assert matches("mixed", rows)
    assert matches("neutral", rows)


def test_congress_only_buy_and_sell_still_allows_only_mixed_or_neutral():
    rows = [_cong("NVDA", "buy"), _cong("NVDA", "sell")]
    assert not matches("bullish", rows)
    assert not matches("bearish", rows)
    assert matches("mixed", rows)
    assert matches("neutral", rows)


def test_reask_names_the_allowed_stances_for_a_rejected_symbol(tmp_path):
    rows = [
        _insider(symbol="UBER", owner="1", direction="buy"),
        _insider(symbol="UBER", owner="2", direction="sell", accession="0000000002-26-000001"),
        _insider(symbol="COST", owner="3", direction="sell", accession="0000000003-26-000001"),
        _cong("COST", "buy"),
    ]
    first = {"findings": [_finding("UBER", "bullish"), _finding("COST", "bearish")]}
    second = {"findings": [_finding("UBER", "mixed")]}
    analyst, sent = _analyst(tmp_path, [first, second])
    messages: list[str] = []
    real_build = analyst.build_user_message

    def build(**kwargs):
        message = real_build(**kwargs)
        messages.append(message)
        return message

    analyst.build_user_message = build

    findings, _result, error, gaps = analyst.analyze_with_gaps(rows)

    assert error is None
    assert {f.symbol for f in findings} == {"COST", "UBER"}  # COST bearish accepted first pass
    assert sent[1] == ["UBER"]
    assert "UBER: insider rows include both purchases and sales, so answer mixed or neutral." in messages[1]
    assert "COST:" not in messages[1]
    assert gaps == [
        {"kind": "unanswered_symbol", "symbol": "UBER", "first_pass": "rejected", "after_reask": "answered"}
    ]
