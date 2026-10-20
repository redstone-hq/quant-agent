"""The smart-money seat reads one summary line per shortlisted name (2026-10-19).

Before this the seat saw the top 40 rows by dollar value market-wide (plus up
to 5 reserved cluster slots): 21,467 insider rows came in and 21,427 were cut,
and 58 of the 105 names the chart seat read had filings it never saw.
Admission keeps reading every fetched row.
"""

import json

from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from src.data.smart_money import SECForm4Provider
from src.data.smart_money_summary import NO_FILINGS_REASON, no_data_reasons, shortlist_rows
from tests.test_smart_money import _insider


def _provider_with(tmp_path, rows):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    provider.stores.observations_path.parent.mkdir(parents=True, exist_ok=True)
    provider.stores.observations_path.write_text(json.dumps([r.model_dump(mode="json") for r in rows]))
    return provider


def _crowd(n=60, value=50_000_000):
    """``n`` big off-shortlist admission-eligible buys that out-rank everything."""
    return [
        _insider(symbol=f"ZZB{i:03d}", owner=f"c{i}", value=value - i, accession=f"{i + 1:010d}-26-000900")
        for i in range(n)
    ]


def _payload(message: str) -> dict:
    return json.loads(message[message.index("{") :])


def test_shortlisted_name_outside_the_old_top_40_reaches_the_seat(tmp_path):
    small = _insider(symbol="ZZZQ", owner="q", value=12_000, accession="0000000777-26-000001")
    provider = _provider_with(tmp_path, [*_crowd(), small])
    observations, _err = provider.fetch(["ZZZQ"])
    shortlist = ["ZZZQ"]
    rows = shortlist_rows(observations, shortlist)
    message = SmartMoneyAnalystAgent.build_user_message(
        object.__new__(SmartMoneyAnalystAgent), observations=rows, order=shortlist
    )
    facts = _payload(message)["symbol_facts"]
    assert [f["symbol"] for f in facts] == ["ZZZQ"]
    assert facts[0]["insider_buys"] == 1
    assert facts[0]["latest_insider_buy"]["usd"] == 12_000


def test_no_data_name_carries_its_reason():
    rows = [_insider(symbol="ZZZA")]
    shortlist = ["ZZZA", "ZZZN"]
    no_data = no_data_reasons(rows, shortlist)
    message = SmartMoneyAnalystAgent.build_user_message(
        object.__new__(SmartMoneyAnalystAgent), observations=rows, order=shortlist, no_data=no_data
    )
    payload = _payload(message)
    assert payload.get("names_without_filings") == [{"symbol": "ZZZN", "reason": NO_FILINGS_REASON}]
    assert [f["symbol"] for f in payload["symbol_facts"]] == ["ZZZA"]


def test_admission_still_sees_an_off_shortlist_big_dollar_buy(tmp_path):
    target = _insider(symbol="ZZZX", owner="x", value=1_000_000, accession="0000000888-26-000001")
    provider = _provider_with(tmp_path, [*_crowd(), target])
    observations, _err = provider.fetch(["ZZZA"])
    hit = [row for row in observations if row.symbol == "ZZZX"]
    assert len(hit) == 1 and hit[0].transient_admission_eligible
    assert provider.last_cluster_census["rows_in"] == len(observations) == 61


def test_order_is_held_then_chart_then_rest():
    # Rank order alone would put the biggest buy (ZZZR, the "rest" name) first.
    rows = [
        _insider(symbol="ZZZR", owner="r", value=9_000_000, accession="0000000001-26-000101"),
        _insider(symbol="ZZZC", owner="c", value=100_000, accession="0000000001-26-000102"),
        _insider(symbol="ZZZH", owner="h", value=10_000, accession="0000000001-26-000103"),
    ]
    order = ["ZZZH", "ZZZC", "ZZZR"]
    message = SmartMoneyAnalystAgent.build_user_message(
        object.__new__(SmartMoneyAnalystAgent), observations=rows, order=order
    )
    assert [f["symbol"] for f in _payload(message)["symbol_facts"]] == order


def test_prompt_for_105_names_is_far_under_200k_tokens(tmp_path):
    # Realistic fixture [estimate from 19 Oct: 21,467 insider rows, 105 shortlisted]:
    # 105 shortlisted names x 30 rows each plus 18,000 off-shortlist rows.
    shortlist = [f"ZZS{i:03d}" for i in range(105)]
    rows = [
        _insider(symbol=s, owner=f"{i}-{j}", value=10_000 + j, accession=f"{i * 100 + j + 1:010d}-26-000001", row=j)
        for i, s in enumerate(shortlist)
        for j in range(30)
    ]
    off = [
        _insider(symbol=f"ZZO{k % 3000:04d}", owner=f"o{k}", value=5_000, accession=f"{k + 1:010d}-26-000555")
        for k in range(18_000)
    ]
    seat_rows = shortlist_rows([*rows, *off], shortlist)
    agent = object.__new__(SmartMoneyAnalystAgent)
    after = agent.build_user_message(observations=seat_rows, order=shortlist)
    after_tokens = len(after) // 4  # [estimate] 4 chars per token
    print(f"smart-money summary prompt tokens for 105 names [estimate, chars/4]: {after_tokens}")
    assert len(_payload(after)["symbol_facts"]) == 105
    assert after_tokens < 50_000


def test_exchange_rows_are_not_counted_as_sells():
    """A Form 4 exchange (or unknown direction) is neither a buy nor a sell;
    counting it as a sale would tell the seat insiders sold when they did not."""
    from src.data.smart_money_summary import summary_lines

    sale = _insider(symbol="ZZZA", direction="sell", value=100_000)
    exchange = _insider(symbol="ZZZA", direction="buy", value=900_000, row=1).model_copy(
        update={"direction": "exchange"}
    )
    (line,) = summary_lines([sale, exchange], ["ZZZA"])
    assert line["insider_sells"] == 1
    assert line["insider_buys"] == 0
    assert line["insider_net_usd"] == -100_000
