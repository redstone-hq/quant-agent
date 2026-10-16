import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from src.agents.base import AgentResult
from src.agents.smart_money_analyst import SmartMoneyAnalystAgent
from src.data.smart_money import SECForm4Provider
from src.models import SmartMoneyFinding, SmartMoneyObservation


ET = ZoneInfo("America/New_York")


def _congress(*, lag=40, age=0, actor="Example Member", direction="buy"):
    disclosed = date.today() - timedelta(days=age)
    return SmartMoneyObservation(
        symbol="NVDA",
        actor=actor,
        direction=direction,
        transaction_date=disclosed - timedelta(days=lag),
        disclosure_date=disclosed,
        source_url="https://example.test/filing",
        lag_days=lag,
        disclosure_age_days=age,
        freshness="stale" if age > 30 or lag > 45 else "fresh",
        economic_role="historical",
    )


def _insider(
    *,
    symbol="NVDA",
    owner="1",
    direction="buy",
    value=300_000,
    age=0,
    accession="0000000001-26-000001",
    row=0,
):
    disclosed = date.today() - timedelta(days=age)
    code = "P" if direction == "buy" else "S"
    return SmartMoneyObservation(
        symbol=symbol,
        stream="insider",
        actor=f"Owner {owner}",
        actor_cik=owner,
        actor_roles=["director"],
        direction=direction,
        transaction_date=disclosed - timedelta(days=2),
        disclosure_date=disclosed,
        accepted_at=datetime.combine(disclosed, datetime.min.time(), tzinfo=ET),
        source_url=f"https://www.sec.gov/{accession}.txt",
        accession_number=accession,
        filing_form="4",
        transaction_code=code,
        transaction_row=row,
        security_title="Common Stock",
        shares=value / 100,
        price_per_share=100,
        transaction_value_usd=value,
        post_transaction_shares=10_000,
        ownership_nature="direct",
        listed_exchange="Nasdaq",
        lag_days=2,
        disclosure_age_days=age,
        freshness="fresh" if age <= 7 else "delayed",
        economic_role="confirmatory",
    )


def _submission(*, code="P", acquired="A", form="4", symbol="NVDA"):
    return f"""<SEC-DOCUMENT>0000000001-26-000001.txt
<ACCEPTANCE-DATETIME>20260824173015
<DOCUMENT><TYPE>{form}<TEXT><XML><?xml version="1.0"?>
<ownershipDocument>
  <documentType>{form}</documentType>
  <issuer><issuerCik>0001045810</issuerCik><issuerTradingSymbol>{symbol}</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001234567</rptOwnerCik><rptOwnerName>Example Insider</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>true</isDirector><isOfficer>true</isOfficer><isTenPercentOwner>false</isTenPercentOwner><isOther>false</isOther><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>0</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-08-22</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>2500</value></transactionShares><transactionPricePerShare><value>100.50</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>{acquired}</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>10000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-08-22</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>999999</value></transactionShares><transactionPricePerShare><value>0</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument></XML></TEXT></DOCUMENT>"""


def _write_rows(provider: SECForm4Provider, rows):
    provider.stores.observations_path.write_text(json.dumps([row.model_dump(mode="json") for row in rows]))


def test_congressional_compatibility_remains_conservative():
    finding = SmartMoneyFinding(
        symbol="NVDA",
        stance="bullish",
        economic_role="confirmatory",
        summary="one buy",
        why_now="recently disclosed",
        observations=[_congress(lag=2)],
    )
    assert finding.economic_role == "historical"
    assert finding.support_eligible is False

    cluster = SmartMoneyFinding(
        symbol="NVDA",
        stance="bullish",
        economic_role="confirmatory",
        summary="cluster",
        why_now="two disclosures",
        observations=[_congress(lag=2, actor="A"), _congress(lag=3, actor="B")],
    )
    assert cluster.support_eligible is True


def test_sec_insider_actionable_role_is_downgraded_when_directions_conflict():
    """Mirror of `test_congressional_compatibility_remains_conservative`
    above, but for the SEC/insider branch of `deterministic_eligibility`.

    Before this fix, only the congressional branch force-downgraded
    `economic_role` to "historical" when `support_eligible` came back
    False. The SEC/insider branch computed `support_eligible` but never
    applied the same downgrade, so a genuinely contradictory (mixed-
    direction) insider finding could still carry the model's self-reported
    economic_role="actionable" / stance="bullish" all the way to
    `to_verdict()` at full magnitude and "high" conviction -- exactly the
    "everything's good, just plain lying" case this seat must not allow.

    2026-09-11: no longer built on AGE -- see
    test_insider_age_alone_does_not_disqualify_support below for why. The
    genuinely thin/contradictory case is mixed buy-and-sell direction,
    which the SEC/insider branch has always required a single direction
    to rule out.
    """
    buy_obs = SmartMoneyObservation(
        symbol="NVDA",
        stream="insider",
        actor="Owner 1",
        actor_cik="1",
        actor_roles=["director"],
        direction="buy",
        transaction_date=date.today() - timedelta(days=5),
        disclosure_date=date.today() - timedelta(days=3),
        source_url="https://www.sec.gov/0000000001-26-000001.txt",
        accession_number="0000000001-26-000001",
        filing_form="4",
        transaction_code="P",
        transaction_row=0,
        security_title="Common Stock",
        shares=3_000,
        price_per_share=100,
        transaction_value_usd=300_000,
        post_transaction_shares=10_000,
        ownership_nature="direct",
        listed_exchange="Nasdaq",
        lag_days=2,
        disclosure_age_days=3,
        freshness="fresh",
        economic_role="confirmatory",
    )
    sell_obs = buy_obs.model_copy(
        update={
            "actor": "Owner 2",
            "actor_cik": "2",
            "direction": "sell",
            "transaction_code": "S",
            "accession_number": "0000000002-26-000001",
        }
    )

    finding = SmartMoneyFinding(
        symbol="NVDA",
        stance="bullish",
        economic_role="actionable",
        summary="conflicting insider activity",
        why_now="one insider bought, another sold, same week",
        observations=[buy_obs, sell_obs],
    )

    assert finding.support_eligible is False
    assert finding.economic_role == "historical"
    verdict = finding.to_verdict()
    assert verdict.conviction == "low"
    # The downgrade shows up in CONVICTION, which is the only thing this seat
    # states: smart_money has no strength scale, so its magnitude is the
    # absence (None) and always was the same value on every call — asserting
    # it is "below the maximum" never tested the downgrade (item 65,
    # 2026-09-26; before that date the same assertion read 0.0 < 1.0).
    assert verdict.magnitude is None


def test_insider_age_alone_does_not_disqualify_support():
    """2026-09-11 owner redesign: a calendar-age cutoff no longer decides
    whether smart-money evidence CAN support a thesis -- only structural
    validity does (real, single-direction, legally-disclosed). Whether an
    aged finding actually counts as support is decided downstream by
    correlation with other current evidence
    (`PortfolioManagerAgent`'s grounding validator), not by this model.

    A single-actor, single-direction insider buy from 60 days ago -- well
    past the old 7-day cutoff -- must still be structurally eligible.
    """
    old_obs = SmartMoneyObservation(
        symbol="NVDA",
        stream="insider",
        actor="Owner 1",
        actor_cik="1",
        actor_roles=["director"],
        direction="buy",
        transaction_date=date.today() - timedelta(days=62),
        disclosure_date=date.today() - timedelta(days=60),
        source_url="https://www.sec.gov/0000000001-26-000001.txt",
        accession_number="0000000001-26-000001",
        filing_form="4",
        transaction_code="P",
        transaction_row=0,
        security_title="Common Stock",
        shares=3_000,
        price_per_share=100,
        transaction_value_usd=300_000,
        post_transaction_shares=10_000,
        ownership_nature="direct",
        listed_exchange="Nasdaq",
        lag_days=2,
        disclosure_age_days=60,
        freshness="stale",
        economic_role="confirmatory",
    )

    finding = SmartMoneyFinding(
        symbol="NVDA",
        stance="bullish",
        economic_role="actionable",
        summary="an insider bought two months ago",
        why_now="old but real, single-direction evidence",
        observations=[old_obs],
    )

    assert finding.support_eligible is True
    assert finding.economic_role == "actionable"


def test_congressional_eligibility_boundary_is_the_stock_act_45_day_deadline():
    """45 days is the actual legal filing deadline (STOCK Act) -- a filer
    right at the deadline is legally on-time, not stale evidence."""
    on_time = SmartMoneyFinding(
        symbol="NVDA",
        stance="bullish",
        economic_role="confirmatory",
        summary="cluster",
        why_now="two disclosures",
        observations=[
            _congress(lag=45, actor="A"),
            _congress(lag=45, actor="B"),
        ],
    )
    assert on_time.support_eligible is True

    late = SmartMoneyFinding(
        symbol="NVDA",
        stance="bullish",
        economic_role="confirmatory",
        summary="cluster",
        why_now="two disclosures",
        observations=[
            _congress(lag=46, actor="A"),
            _congress(lag=46, actor="B"),
        ],
    )
    assert late.support_eligible is False


def test_exact_non_derivative_purchase_parse_preserves_sec_facts(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = provider._parse_submission(
        _submission(),
        source_url=("https://www.sec.gov/Archives/edgar/data/1045810/000000000126000001/0000000001-26-000001.txt"),
        listed={"1045810": {"NVDA": "Nasdaq"}},
    )
    assert len(rows) == 1  # code A award was discarded before the LLM
    row = rows[0]
    assert row.transaction_code == "P" and row.direction == "buy"
    assert row.transaction_value_usd == 251_250
    assert row.accepted_at == datetime(2026, 8, 24, 17, 30, 15, tzinfo=ET)
    assert row.accession_number == "0000000001-26-000001"
    assert row.actor_cik == "1234567"
    assert row.actor_roles == ["director", "officer", "CEO"]
    assert row.post_transaction_shares == 10_000
    assert row.ownership_nature == "direct"
    assert row.is_10b5_1 is False


# Every SEC Form 4 Table I transaction code that is NOT an open-market P/S
# trade, pinned individually. `SmartMoneyObservation.transaction_code` is
# `Literal["", "P", "S"]` (src/models.py) specifically because none of these
# six ever survive `_parse_submission` to be handed to the routine/
# opportunistic classifier (`src/data/insider_signal.py`) — see that
# module's docstring, "departure" notes, for why the classifier itself still
# carries a `non_open_market_code` contract guard rather than assuming this
# parser boundary always holds. `code="A"` (grant/award) is already covered
# by `test_exact_non_derivative_purchase_parse_preserves_sec_facts` above,
# which additionally checks that a genuine P row alongside it survives; this
# test isolates the other five and confirms each drops on its own, with no
# survivor to piggyback on.
@pytest.mark.parametrize("code", ["A", "M", "F", "G", "D", "X"])
def test_every_non_open_market_code_is_dropped_before_the_classifier(tmp_path, code):
    # The transaction code filter (`if code not in {"P", "S"}: continue`) is
    # the first check `_parse_submission` applies, ahead of the acquired/
    # disposed consistency check — so ``acquired`` here is irrelevant to the
    # outcome and left at the fixture's default.
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = provider._parse_submission(
        _submission(code=code),
        source_url="https://www.sec.gov/x/0000000001-26-000001.txt",
        listed={"1045810": {"NVDA": "Nasdaq"}},
    )
    assert rows == []


def test_direction_inconsistent_sec_row_is_dropped(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = provider._parse_submission(
        _submission(code="P", acquired="D"),
        source_url="https://www.sec.gov/x/0000000001-26-000001.txt",
        listed={"1045810": {"NVDA": "Nasdaq"}},
    )
    assert rows == []


def test_amendment_marker_and_sale_are_preserved(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    rows = provider._parse_submission(
        _submission(code="S", acquired="D", form="4/A"),
        source_url="https://www.sec.gov/x/0000000001-26-000001.txt",
        listed={"1045810": {"NVDA": "Nasdaq"}},
    )
    assert rows[0].amendment is True
    assert rows[0].direction == "sell"


def test_amendment_cannot_independently_clear_materiality_or_admission(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    amended = _insider(symbol="XYZ", value=500_000)
    amended.amendment = True
    amended.filing_form = "4/A"
    _write_rows(provider, [amended])

    assert provider.fetch([]) == ([], None)


def test_fetch_is_broad_and_only_large_external_purchase_gets_admission(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    outside_buy = _insider(symbol="XYZ", direction="buy", value=300_000)
    outside_sell = _insider(
        symbol="SELL",
        direction="sell",
        value=500_000,
        accession="0000000002-26-000001",
    )
    core_buy = _insider(
        symbol="NVDA",
        direction="buy",
        value=150_000,
        accession="0000000003-26-000001",
    )
    _write_rows(provider, [outside_buy, outside_sell, core_buy])

    rows, error = provider.fetch(["NVDA"])
    assert error is None
    assert {row.symbol for row in rows} == {"XYZ", "SELL", "NVDA"}
    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["XYZ"].transient_admission_eligible is True
    assert by_symbol["SELL"].transient_admission_eligible is False
    assert by_symbol["NVDA"].in_core_universe is True


def test_small_dollar_buy_now_survives_no_size_floor(tmp_path):
    """Board item 52, resolved 2026-09-25: no published study supports
    single-transaction dollar size as an insider-buy predictor, so the old
    $100k/$250k floors were deleted rather than sourced. A $99,999 buy that
    used to be silently dropped now survives like any other row."""
    provider = SECForm4Provider(data_dir=str(tmp_path))
    _write_rows(provider, [_insider(value=99_999)])
    rows, error = provider.fetch(["NVDA"])
    assert error is None
    assert len(rows) == 1


def test_independent_owner_cluster_survives_but_repeat_owner_does_not(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    independent = [
        _insider(owner="1", value=60_000, accession="0000000001-26-000001"),
        _insider(owner="2", value=60_000, accession="0000000002-26-000001"),
    ]
    _write_rows(provider, independent)
    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 2


def test_cluster_window_default_matches_alldredge_blank_citation(tmp_path):
    """docs/RESEARCH_FINDINGS.md:19 cites ~2 days, not the old 14."""
    provider = SECForm4Provider(data_dir=str(tmp_path))
    assert provider.cluster_window_days == 2


def test_two_owners_two_days_apart_form_a_cluster(tmp_path):
    provider = SECForm4Provider(data_dir=str(tmp_path))
    two_days_apart = [
        _insider(owner="1", value=60_000, age=0, accession="0000000001-26-000001"),
        _insider(owner="2", value=60_000, age=2, accession="0000000002-26-000001"),
    ]
    _write_rows(provider, two_days_apart)
    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 2


def test_two_owners_three_days_apart_do_not_cluster(tmp_path):
    """Boundary case: one day past the corrected 2-day window.

    Both rows are $60,000, below the old $100k core floor, which used to be
    the only way this pair could be dropped once clustering failed. Board
    item 52 (resolved 2026-09-25) deleted that floor, so both rows now
    survive on their own -- the boundary is no longer observable through row
    count, only through the clustering that `cluster_survivors`'s row-
    retention window still governs for a caller with a real floor (see
    `CongressionalTradingProvider`, out of this item's scope)."""
    provider = SECForm4Provider(data_dir=str(tmp_path))
    three_days_apart = [
        _insider(owner="1", value=60_000, age=0, accession="0000000001-26-000001"),
        _insider(owner="2", value=60_000, age=3, accession="0000000002-26-000001"),
    ]
    _write_rows(provider, three_days_apart)
    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 2


def test_old_14_day_window_would_have_wrongly_clustered_these(tmp_path):
    """Same transactions as the boundary case above, with the old 14-day
    window restored explicitly. Demonstrates the bug: it incorrectly
    clustered trades 3 days apart, which the corrected 2-day window (and
    the cited research) does not support.

    Both parts below now survive on row count alone regardless of window or
    owner, because board item 52 (resolved 2026-09-25) deleted the $100k/
    $250k dollar floors that used to be the only thing distinguishing a
    correctly-excluded cluster from a materiality drop. The independent-vs-
    repeat-owner cluster distinction itself is a `cluster_survivors`
    property, unaffected by that deletion, but is no longer observable
    through this provider's row count now that every row clears materiality
    on its own; a caller that still carries a real floor (see
    `CongressionalTradingProvider`) is where that distinction remains
    testable end-to-end."""
    provider = SECForm4Provider(data_dir=str(tmp_path), cluster_window_days=14)
    three_days_apart = [
        _insider(owner="1", value=60_000, age=0, accession="0000000001-26-000001"),
        _insider(owner="2", value=60_000, age=3, accession="0000000002-26-000001"),
    ]
    _write_rows(provider, three_days_apart)
    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 2

    repeated = [
        _insider(owner="1", value=60_000, accession="0000000001-26-000001"),
        _insider(owner="1", value=60_000, accession="0000000002-26-000001"),
    ]
    _write_rows(provider, repeated)
    rows, _ = provider.fetch(["NVDA"])
    assert len(rows) == 2


def test_refresh_deduplicates_accession_and_uses_descriptive_header(tmp_path, monkeypatch):
    provider = SECForm4Provider(data_dir=str(tmp_path), max_filings_per_refresh=5)
    listed = {"1045810": {"NVDA": "Nasdaq"}}
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: listed)
    monkeypatch.setattr(
        provider,
        "_discover",
        lambda *_: [
            {
                "accession": "0000000001-26-000001",
                "form": "4",
                "cik": "1045810",
            }
        ],
    )
    response = Mock(status_code=200, content=_submission().encode())
    response.raise_for_status.return_value = None
    provider.session.get = Mock(return_value=response)

    assert provider.refresh()["new_observations"] == 1
    assert provider.refresh()["new_observations"] == 0
    assert provider.session.get.call_count == 1
    assert "QAMC/1.0" in provider.session.get.call_args.kwargs["headers"]["User-Agent"]


def test_analyst_rejects_direction_incompatible_stance(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    analyst.run = lambda **_: AgentResult(
        '{"findings":[{"symbol":"NVDA","stance":"bearish",'
        '"economic_role":"actionable","summary":"wrong",'
        '"why_now":"new filing"}]}',
        1,
        "test",
    )
    findings, _, error = analyst.analyze([_insider()])
    assert findings == []
    assert error == "analysis_schema_error"


def test_analyst_unchanged_evidence_uses_zero_token_cache(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.model = "test"
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    calls = []

    def run(**_):
        calls.append(1)
        return AgentResult(
            '{"findings":[{"symbol":"NVDA","stance":"bullish",'
            '"economic_role":"actionable","summary":"large purchase",'
            '"why_now":"accepted today"}]}',
            10,
            "test",
        )

    analyst.run = run
    first, first_result, first_error = analyst.analyze([_insider()])
    second, second_result, second_error = analyst.analyze([_insider()])
    assert first_error is None and second_error is None
    assert first[0].stance == second[0].stance == "bullish"
    assert len(calls) == 1
    assert first_result.tokens_used == 10
    assert second_result.tokens_used == 0
    assert second_result.provider_requests == 0


def test_analyst_cache_key_ignores_age_and_run_membership(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.model = "test"
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    calls = []

    def run(**_):
        calls.append(1)
        return AgentResult('{"findings":[]}', 10, "test")

    analyst.run = run
    first = _insider()
    analyst.analyze([first])
    changed_context = first.model_copy(
        update={
            "disclosure_age_days": first.disclosure_age_days + 1,
            "in_trading_universe": True,
            "transient_admitted": True,
        }
    )
    _, cached_result, error = analyst.analyze([changed_context])

    assert error is None
    assert calls == [1]
    assert cached_result.tokens_used == 0


def test_analyst_distinguishes_valid_empty_from_parse_failure(tmp_path):
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    analyst.run = lambda **_: AgentResult('{"findings":[]}', 1, "test")
    findings, _, error = analyst.analyze([_insider()])
    assert findings == [] and error is None

    analyst.synthesis_cache_path = tmp_path / "other.json"
    analyst.run = lambda **_: AgentResult("not json", 1, "test")
    findings, _, error = analyst.analyze([_insider()])
    assert findings == [] and error == "analysis_parse_error"


def _compact_payload(message: str) -> dict:
    return json.loads(message.split("\n", 1)[1])


def test_analyst_compacts_and_ranks_production_shaped_observations():
    observations = []
    for index in range(40):
        observation = _insider(
            symbol=f"S{index:02d}",
            owner=f"owner-{index}",
            value=(index + 1) * 10_000,
            age=index % 8,
            accession=f"{index + 1:010d}-26-000001",
        )
        observation.actor = f"Verbose reporting owner {index} " + ("x" * 200)
        observation.source_url = "https://www.sec.gov/Archives/" + ("y" * 300)
        observations.append(observation)

    # Actual admission wins over merely eligible, higher-value symbols;
    # remaining symbols rank by materiality. Reversing input must not change
    # the compact request.
    for observation in observations:
        observation.transient_admission_eligible = True
        observation.admission_eligible = True
    observations[0].transient_admitted = True
    analyst = object.__new__(SmartMoneyAnalystAgent)
    message = analyst.build_user_message(observations=observations)
    reversed_message = analyst.build_user_message(observations=list(reversed(observations)))
    payload = _compact_payload(message)

    assert message == reversed_message
    assert payload["input_observation_count"] == 40
    assert payload["input_symbol_count"] == 40
    assert payload["presented_symbol_count"] == 8
    assert payload["omitted_symbol_count"] == 32
    assert [row["symbol"] for row in payload["symbol_facts"]] == [
        "S00",
        "S39",
        "S38",
        "S37",
        "S36",
        "S35",
        "S34",
        "S33",
    ]
    assert all(len(row["representative_transactions"]) <= 3 for row in payload["symbol_facts"])
    assert all(len(row["representative_transactions"][0]["actor"]) <= 96 for row in payload["symbol_facts"])
    assert "https://www.sec.gov/Archives/" not in message
    assert len(message) < 15_000


def test_analyst_compaction_does_not_replace_canonical_finding_evidence(tmp_path):
    observations = [
        _insider(
            owner=str(index),
            value=100_000 + index,
            accession=f"{index + 1:010d}-26-000001",
            row=index,
        )
        for index in range(5)
    ]
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    captured = {}

    def run(**kwargs):
        captured["message"] = analyst.build_user_message(**kwargs)
        return AgentResult(
            '{"findings":[{"symbol":"NVDA","stance":"bullish",'
            '"economic_role":"confirmatory","summary":"owner cluster",'
            '"why_now":"recent accepted filings"}]}',
            10,
            "test",
        )

    analyst.run = run
    findings, _, error = analyst.analyze(observations)
    compact = _compact_payload(captured["message"])

    assert error is None
    assert len(compact["symbol_facts"][0]["representative_transactions"]) == 3
    assert len(findings[0].observations) == 5
    assert {row.accession_number for row in findings[0].observations} == {row.accession_number for row in observations}


def test_analyst_enforces_presented_unique_symbol_boundary(tmp_path):
    observations = [
        _insider(
            symbol=f"S{index:02d}",
            owner=f"owner-{index}",
            value=(index + 1) * 10_000,
            accession=f"{index + 1:010d}-26-000001",
        )
        for index in range(10)
    ]
    raw_findings = [
        {
            "symbol": observation.symbol,
            "stance": "bullish",
            "economic_role": "actionable",
            "summary": "purchase",
            "why_now": "recent filing",
        }
        for observation in observations
    ]
    raw_findings.append(dict(raw_findings[-1]))
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    analyst.run = lambda **_: AgentResult(
        json.dumps({"findings": raw_findings}),
        10,
        "test",
    )

    findings, _, error = analyst.analyze(observations)

    assert error == "analysis_partial_schema_error"
    assert len(findings) == 8
    assert len({finding.symbol for finding in findings}) == 8
    assert {finding.symbol for finding in findings} == {
        row["symbol"] for row in _compact_payload(analyst.build_user_message(observations=observations))["symbol_facts"]
    }


def test_analyst_cache_is_bound_to_run_scoped_presented_symbols(tmp_path):
    observations = [
        _insider(
            symbol=f"S{index:02d}",
            owner=f"owner-{index}",
            value=(index + 1) * 10_000,
            accession=f"{index + 1:010d}-26-000001",
        ).model_copy(
            update={
                "transient_admission_eligible": True,
                "admission_eligible": True,
                "transient_admitted": index == 0,
            }
        )
        for index in range(9)
    ]
    analyst = object.__new__(SmartMoneyAnalystAgent)
    analyst.model = "test"
    analyst.synthesis_cache_path = tmp_path / "cache.json"
    calls = []

    def run(**_):
        calls.append(1)
        return AgentResult('{"findings":[]}', 10, "test")

    analyst.run = run
    analyst.analyze(observations)
    changed_admission = [
        observation.model_copy(update={"transient_admitted": index == 1})
        for index, observation in enumerate(observations)
    ]
    _, result, error = analyst.analyze(changed_admission)

    assert error is None
    assert calls == [1, 1]
    assert result.tokens_used == 10


def test_compact_symbol_surfaces_estimated_disclosure_dates_to_the_seat():
    """Board item 170, DONE-WHEN #3: the seat must be able to tell a real
    congressional filing date from congresswatch.us's trade+45d guess
    wherever the lag it fed into reaches the prompt -- not just the
    eligibility gate the fix already covers."""
    real = _congress(lag=10, actor="Real Filer")
    estimated = _congress(lag=45, actor="Guessed Filer").model_copy(update={"disclosure_date_estimated": True})
    compact = SmartMoneyAnalystAgent._compact_symbol("NVDA", [real, estimated])
    assert compact["disclosure_date_estimated_count"] == 1

    # Both observations fit under the representative-transaction cap, so
    # each one's own estimated/real flag must be visible individually, not
    # just as an aggregate count.
    flags = {row["disclosure_date_estimated"] for row in compact["representative_transactions"]}
    assert flags == {True, False}


# --- Board item 63: signal_weight now carries a DIRECTION channel so a
# --- bearish/contra smart-money row cannot rank or size as a bullish buy. ---


def test_signal_direction_channel_signs_by_direction():
    """A buy is bullish (+1); a sale and non-directional codes carry no sign (0)."""
    assert _insider(direction="buy").signal_direction == 1
    assert _insider(direction="sell").signal_direction == 0
    exchange = _insider(direction="buy").model_copy(update={"direction": "exchange"})
    unknown = _insider(direction="buy").model_copy(update={"direction": "unknown"})
    assert exchange.signal_direction == 0
    assert unknown.signal_direction == 0


def test_large_sale_does_not_rank_as_a_bullish_buy_of_equal_size():
    """The item-63 bug: an equal-dollar sell used to tie/outrank a buy on the
    value*weight term. Now the buy (bullish) ranks strictly first even when its
    ticker sorts last, so direction — not spelling or magnitude — decides."""
    buy = _insider(symbol="ZZZZ", direction="buy", value=1_000_000, accession="0000000001-26-000009")
    sell = _insider(symbol="AAAA", direction="sell", value=1_000_000, accession="0000000001-26-000008")
    buy_rank = SmartMoneyAnalystAgent._symbol_rank("ZZZZ", [buy])
    sell_rank = SmartMoneyAnalystAgent._symbol_rank("AAAA", [sell])
    assert buy_rank < sell_rank
    # And at transaction granularity, on identical magnitude.
    assert SmartMoneyAnalystAgent._transaction_rank(buy) < SmartMoneyAnalystAgent._transaction_rank(sell)


def test_bullish_buy_value_term_is_unchanged_by_the_direction_channel():
    """Existing unambiguous signals (buys, +1) keep the exact ranking
    contribution they had before the sign channel existed: -(value*weight)."""
    buy = _insider(direction="buy", value=750_000)
    assert buy.signal_weight == 1.0
    # index 5 is the value-weighted term in _transaction_rank's tuple.
    assert SmartMoneyAnalystAgent._transaction_rank(buy)[5] == -(750_000 * 1.0)
    # The sale's contribution to that same term is neutralised to 0.
    sell = _insider(direction="sell", value=750_000)
    assert SmartMoneyAnalystAgent._transaction_rank(sell)[5] == 0


def test_sale_census_counts_and_samples_sales_by_holdings_band():
    """Board item 63: the sale side must be recorded even though it is
    truncated out of what the analyst ever sees."""
    from src.data.smart_money import _sale_census

    buy = _insider(direction="buy", accession="0000000001-26-000010")
    sale_small = _insider(
        direction="sell",
        accession="0000000001-26-000011",
    ).model_copy(
        update={
            "holdings_fraction": 0.04,
        }
    )
    sale_large = _insider(
        direction="sell",
        accession="0000000001-26-000012",
    ).model_copy(
        update={
            "holdings_fraction": 0.80,
        }
    )

    census = _sale_census([buy, sale_small, sale_large])

    assert census["parsed_rows"] == 3
    assert census["buy_rows"] == 1
    assert census["sale_rows"] == 2
    assert "band_counts" not in census
    # The forward return is forward: it is NOT computed here, and the
    # recording says so rather than implying it holds one.
    assert census["forward_return_joined"] is False
    assert {row["holdings_fraction"] for row in census["rows"]} == {0.04, 0.80}
    assert all(row["reference_price"] is not None for row in census["rows"])
    assert all(row["transaction_date"] for row in census["rows"])
    # No buy leaks into a recording whose whole purpose is the sale side.
    assert len(census["rows"]) == 2


def test_sale_census_is_empty_when_nothing_sold():
    from src.data.smart_money import _sale_census

    census = _sale_census([_insider(direction="buy")])
    assert census["sale_rows"] == 0
    assert census["rows"] == []


def test_sale_census_row_sample_is_bounded():
    from src.data.smart_money import MAX_SALE_CENSUS_ROWS, _sale_census

    sales = [
        _insider(
            direction="sell",
            accession=f"0000000001-26-{index:06d}",
        ).model_copy(update={"holdings_fraction_band": "over_50pct"})
        for index in range(MAX_SALE_CENSUS_ROWS + 25)
    ]
    census = _sale_census(sales)
    assert census["sale_rows"] == MAX_SALE_CENSUS_ROWS + 25
    assert len(census["rows"]) == MAX_SALE_CENSUS_ROWS


def test_fetch_stashes_the_sale_census_even_when_truncation_drops_sales(tmp_path):
    """The measured production failure, reproduced: with more
    admission-eligible buys than `max_observations` slots, no sale survives
    into what the analyst sees -- but the census still records it."""
    provider = SECForm4Provider(data_dir=str(tmp_path), max_observations=1)
    rows = [
        _insider(
            symbol="NVDA",
            owner="1",
            direction="buy",
            value=9_000_000,
            accession="0000000001-26-000021",
        ).model_dump(mode="json"),
        _insider(
            symbol="NVDA",
            owner="2",
            direction="sell",
            value=5_000_000,
            accession="0000000001-26-000022",
            row=1,
        ).model_dump(mode="json"),
    ]
    (tmp_path / "smart_money").mkdir(parents=True, exist_ok=True)
    provider.stores.observations_path.parent.mkdir(parents=True, exist_ok=True)
    provider.stores.observations_path.write_text(json.dumps(rows))

    observations, _error = provider.fetch(["NVDA"])

    assert [item.direction for item in observations] == ["buy"]
    assert provider.last_sale_census is not None
    assert provider.last_sale_census["sale_rows"] == 1
    assert provider.last_sale_census["buy_rows"] == 1
