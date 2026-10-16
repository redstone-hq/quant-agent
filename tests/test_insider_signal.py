"""Routine/opportunistic Form 4 classification.

Evidence basis is ``docs/RESEARCH_FINDINGS.md`` section 1. The tests below
pin the rules that document actually supports — including that a 10b5-1 flag
is not a noise marker and that trade size relative to the insider's own
holding is reported rather than turned into a cutoff. A single Form 4 row is
not the aggregate unit in Scott & Xu, so its ratio remains descriptive and
must not inherit that study's return claims. See the module docstring,
departure #3.
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.data.insider_signal import (
    InsiderHistory,
    InsiderPriorTrade,
    InsiderSignalThresholds,
    classify_transaction,
    holdings_fraction,
)
from src.data.smart_money import SECForm4Provider
from src.models import SmartMoneyObservation
from src.trading_calendar import et_today

ET = ZoneInfo("America/New_York")


def _row(
    *,
    symbol="NVDA",
    owner="1",
    direction="buy",
    shares=1_000.0,
    price=100.0,
    post_shares=50_000.0,
    transaction_date=date(2026, 8, 20),
    is_10b5_1=None,
    roles=("officer", "Chief Financial Officer"),
    accession="0000000001-26-000001",
    row=0,
    age=0,
    stream="insider",
    code=None,
):
    disclosed = transaction_date + timedelta(days=2)
    return SmartMoneyObservation(
        symbol=symbol,
        stream=stream,
        actor=f"Owner {owner}",
        actor_cik=owner,
        actor_roles=list(roles),
        direction=direction,
        transaction_date=transaction_date,
        disclosure_date=disclosed,
        accepted_at=datetime.combine(disclosed, datetime.min.time(), tzinfo=ET),
        source_url=f"https://www.sec.gov/{accession}.txt",
        accession_number=accession,
        filing_form="4",
        transaction_code=code if code is not None else ("P" if direction == "buy" else "S"),
        transaction_row=row,
        security_title="Common Stock",
        shares=shares,
        price_per_share=price,
        transaction_value_usd=None if shares is None or price is None else shares * price,
        post_transaction_shares=post_shares,
        ownership_nature="direct",
        listed_exchange="Nasdaq",
        lag_days=2,
        disclosure_age_days=age,
        freshness="fresh",
        economic_role="confirmatory",
        is_10b5_1=is_10b5_1,
    )


def _established(owner="1", symbol="NVDA"):
    """Three preceding calendar years of trades, none in August: classifiable, not routine."""
    return _history(owner=owner, symbol=symbol, days=[date(2025, 1, 9), date(2024, 1, 9), date(2023, 1, 9)])


def _history(owner="1", symbol="NVDA", direction="buy", days=()):
    return InsiderHistory(
        {(owner, symbol): [InsiderPriorTrade(transaction_date=day, direction=direction) for day in days]}
    )


# --- opportunistic ---------------------------------------------------------


def test_discretionary_officer_purchase_is_opportunistic():
    verdict = classify_transaction(_row(), _established())

    assert verdict.label == "opportunistic"
    assert verdict.reason == "opportunistic_purchase"
    assert "$100,000" in verdict.detail


def test_purchase_by_owner_with_no_inside_role_is_still_opportunistic_but_flagged():
    verdict = classify_transaction(_row(roles=("other",)), _established())

    assert verdict.label == "opportunistic"
    assert "no officer, director or 10% role" in verdict.detail


def test_sale_large_relative_to_the_holding_is_opportunistic():
    verdict = classify_transaction(
        _row(direction="sell", shares=40_000.0, post_shares=10_000.0),
        _established(),
    )

    assert verdict.label == "opportunistic"
    assert verdict.reason == "discretionary_sale"
    assert "80.0%" in verdict.detail


# --- routine categories ----------------------------------------------------


def test_same_calendar_month_for_three_years_is_routine():
    """Cohen/Malloy/Pomorski's own definition of a routine trader."""
    verdict = classify_transaction(
        _row(transaction_date=date(2026, 8, 20)),
        _history(days=[date(2025, 8, 14), date(2024, 8, 3), date(2023, 8, 28)]),
    )

    assert verdict.label == "routine"
    assert verdict.reason == "calendar_routine"
    assert "August" in verdict.detail


def test_two_years_of_same_month_history_is_not_yet_routine():
    """Boundary: the rule needs three preceding years, not two."""
    verdict = classify_transaction(
        _row(transaction_date=date(2026, 8, 20)),
        _history(days=[date(2025, 8, 14), date(2024, 8, 3)]),
    )

    assert verdict.label == "indeterminate"
    assert verdict.reason == "insufficient_history"


def test_a_gap_year_breaks_the_consecutive_streak():
    """2023 is missing, so the three preceding years are not consecutive."""
    verdict = classify_transaction(
        _row(transaction_date=date(2026, 8, 20)),
        _history(
            days=[
                date(2025, 8, 14),
                date(2022, 8, 3),
                date(2021, 8, 28),
                date(2020, 8, 9),
            ]
        ),
    )

    assert verdict.label == "indeterminate"
    assert verdict.reason == "insufficient_history"


def test_history_in_the_other_direction_counts_because_the_paper_does_not_split_by_direction():
    verdict = classify_transaction(
        _row(direction="buy", transaction_date=date(2026, 8, 20)),
        _history(
            direction="sell",
            days=[
                date(2025, 8, 14),
                date(2024, 8, 3),
                date(2023, 8, 28),
            ],
        ),
    )

    assert verdict.label == "routine"


def test_a_small_proportional_sale_is_not_discarded_as_routine():
    """The defect this replaced: a sub-10% sale was labelled ROUTINE at
    weight 0.0 and dropped out of the seat's ranking entirely. Transaction
    size does not establish whether a sale follows the insider's routine; the
    ratio is reported as descriptive context and nothing is discarded."""
    verdict = classify_transaction(
        _row(direction="sell", shares=1_000.0, post_shares=99_000.0),
        _established(),
    )

    assert verdict.label == "opportunistic"
    assert verdict.reason == "discretionary_sale"
    assert "1.0%" in verdict.detail


def test_a_small_planned_sale_is_no_longer_demoted_by_the_10b5_1_flag():
    """The 10b5-1 branch existed only to reinforce a proportional-immateriality
    cutoff that is now gone. RESEARCH_FINDINGS is explicit that the flag is not
    a clean noise filter, and nothing in it licenses demoting a sale on the flag
    alone — so with the cutoff removed the flag demotes nothing, and is instead
    reported in the detail text for the seat to weigh."""
    verdict = classify_transaction(
        _row(direction="sell", shares=1_000.0, post_shares=99_000.0, is_10b5_1=True),
        _established(),
    )

    assert verdict.label == "opportunistic"
    assert verdict.reason == "discretionary_sale"
    assert "10b5-1" in verdict.detail


# --- every SEC Form 4 transaction code, pinned individually --------------
#
# ``SmartMoneyObservation.transaction_code`` is itself typed
# ``Literal["", "P", "S"]`` (`src/models.py`) — a row carrying any other SEC
# code cannot be constructed at all, because ``SECForm4Provider._parse_
# submission`` filters ``transactionCode not in {"P", "S"}`` before a
# ``SmartMoneyObservation`` is ever built (see ``tests/test_smart_money.py``,
# which pins the parser boundary for codes A/M/F/G/D/X — the grant/award,
# option-exercise, tax-withholding, gift and issuer-disposition codes never
# reach this module). What *this* module can be asked to classify is P, S,
# or the empty string a defensive caller might pass; ``non_open_market_code``
# is a contract guard for that last case, not a live filter on real SEC
# codes. Each of the three is pinned below with a hard literal.
def test_code_empty_is_routine_via_the_contract_guard():
    verdict = classify_transaction(_row(code="", direction="buy"), _established())
    assert verdict.label == "routine"
    assert verdict.reason == "non_open_market_code"


def test_code_p_open_market_purchase_is_opportunistic():
    """Pinned separately from ``test_discretionary_officer_purchase_is_
    opportunistic`` so every code in the reference list above has its own
    literal-asserted test, P included."""
    verdict = classify_transaction(_row(code="P", direction="buy"), _established())
    assert verdict.label == "opportunistic"
    assert verdict.reason == "opportunistic_purchase"


def test_code_s_open_market_sale_is_opportunistic_when_material():
    """Pinned separately from ``test_sale_large_relative_to_the_holding_is_
    opportunistic`` for the same reason — code S gets its own literal test."""
    verdict = classify_transaction(
        _row(code="S", direction="sell", shares=40_000.0, post_shares=10_000.0),
        _established(),
    )
    assert verdict.label == "opportunistic"
    assert verdict.reason == "discretionary_sale"


def test_zero_price_transaction_is_routine():
    verdict = classify_transaction(_row(price=0.0), _established())

    assert verdict.label == "routine"
    assert verdict.reason == "zero_price_transaction"


# --- the RESEARCH_FINDINGS contradictions ----------------------------------


def test_large_10b5_1_sale_is_not_demoted_for_being_planned():
    """RESEARCH_FINDINGS: 10b5-1 is not a clean noise filter.

    "For high-value sales, planned and discretionary transactions show similar
    opportunism, and the 2022 SEC reform did not reduce abnormal returns on
    insider selling." A large planned sale therefore stays opportunistic, and
    the reason says the flag was seen and deliberately not acted on.
    """
    verdict = classify_transaction(
        _row(direction="sell", shares=40_000.0, post_shares=10_000.0, is_10b5_1=True),
        _established(),
    )

    assert verdict.label == "opportunistic"
    assert verdict.reason == "discretionary_sale"
    assert "deliberately not treated as a noise marker" in verdict.detail


def test_10b5_1_purchase_is_not_routine():
    verdict = classify_transaction(_row(is_10b5_1=True), _established())

    assert verdict.label == "opportunistic"


# --- boundaries ------------------------------------------------------------


def test_no_sell_fraction_anywhere_changes_the_label():
    """There is no materiality cutoff left to sit either side of. A 1-in-1000
    sale and a near-total liquidation reach the seat with the same label and
    the same weight, differing only in the evidence they carry — which is the
    whole point: the ranking no longer silently deletes one of them."""
    tiny = classify_transaction(
        _row(direction="sell", shares=100.0, post_shares=99_900.0),
        _established(),
    )
    huge = classify_transaction(
        _row(direction="sell", shares=99_000.0, post_shares=1_000.0),
        _established(),
    )

    assert tiny.label == huge.label == "opportunistic"
    assert tiny.reason == huge.reason == "discretionary_sale"
    assert tiny.detail != huge.detail


def test_missing_post_transaction_holding_is_indeterminate_not_routine():
    verdict = classify_transaction(
        _row(direction="sell", post_shares=None),
        _established(),
    )

    assert verdict.label == "indeterminate"
    assert verdict.reason == "unknown_holding"


def test_missing_amounts_are_indeterminate():
    verdict = classify_transaction(_row(shares=None, price=None), _established())

    assert verdict.label == "indeterminate"
    assert verdict.reason == "incomplete_amounts"


def test_congressional_stream_is_out_of_scope():
    verdict = classify_transaction(
        _row(stream="congressional", code=""),
        _established(),
    )

    assert verdict.label == "indeterminate"
    assert verdict.reason == "not_form4"


def test_history_only_counts_trades_strictly_before_the_transaction():
    """A same-day duplicate row must not be read as its own precedent."""
    same_day = date(2026, 8, 20)
    verdict = classify_transaction(
        _row(transaction_date=same_day),
        _history(days=[same_day, date(2025, 8, 14), date(2024, 8, 3), date(2023, 8, 28)]),
    )

    assert verdict.reason == "calendar_routine"
    assert "3 preceding years" in verdict.detail


# --- provider wiring -------------------------------------------------------


def _cached(provider, rows):
    provider.stores.observations_path.write_text(json.dumps([row.model_dump(mode="json") for row in rows]))


def _provider(tmp_path, **kwargs):
    return SECForm4Provider(data_dir=str(tmp_path), **kwargs)


def _seed_established(provider, owner, symbol, direction="buy"):
    """Trades in each of the 3 preceding calendar years, six months off today's month."""
    today = et_today()
    month = (today.month + 5) % 12 + 1
    provider._record_history(
        [
            {
                "actor_cik": owner,
                "symbol": symbol,
                "direction": direction,
                "transaction_date": date(today.year - n, month, 1).isoformat(),
            }
            for n in (1, 2, 3)
        ]
    )


def test_routine_purchase_never_becomes_admission_eligible(tmp_path):
    today = date.today()
    provider = _provider(tmp_path)
    routine = _row(
        symbol="ABCD",
        shares=5_000.0,
        price=100.0,
        transaction_date=today,
        is_10b5_1=True,
    )
    _cached(provider, [routine])
    provider.stores.history_path.write_text(
        json.dumps({"1|ABCD": [f"{today.replace(year=today.year - offset).isoformat()}|buy" for offset in (1, 2, 3)]})
    )

    observations, error = provider.fetch(["NVDA"])

    assert error is None
    assert [row.signal_class for row in observations] == ["routine"]
    assert observations[0].signal_class_reason == "calendar_routine"
    assert observations[0].admission_eligible is False
    assert observations[0].transient_admission_eligible is False
    assert observations[0].economic_role == "confirmatory"


def test_opportunistic_purchase_still_gets_admission(tmp_path):
    provider = _provider(tmp_path)
    _seed_established(provider, "1", "ABCD")
    _cached(provider, [_row(symbol="ABCD", transaction_date=date.today())])

    observations, _ = provider.fetch(["NVDA"])

    assert [row.signal_class for row in observations] == ["opportunistic"]
    assert observations[0].admission_eligible is True


def test_model_validator_refuses_to_mark_a_routine_row_eligible():
    """Belt and braces: the gate holds even if a caller sets the flag by hand."""
    row = _row().model_dump()
    row.update(
        {
            "admission_eligible": True,
            "transient_admission_eligible": True,
            "signal_class": "routine",
            "signal_class_reason": "calendar_routine",
        }
    )

    rebuilt = SmartMoneyObservation(**row)

    assert rebuilt.admission_eligible is False
    assert rebuilt.transient_admission_eligible is False


def test_routine_rows_sort_behind_opportunistic_ones(tmp_path):
    """A large routine sale must not crowd out a smaller real purchase.

    The routine row here is routine for Cohen/Malloy/Pomorski's own reason —
    the same insider selling the same stock in the same calendar month three
    years running. It used to be a proportionally small sale, which no longer
    demotes anything (2026-09-13): the seat is shown the ratio instead of
    having the row deleted from its ranking. Weight-based ordering itself is
    unchanged, which is what this test exists to pin."""
    today = date.today()
    provider = _provider(tmp_path)
    provider._record_history(
        [
            {
                "actor_cik": "1",
                "symbol": "NVDA",
                "direction": "sell",
                "transaction_date": today.replace(year=today.year - n).isoformat(),
            }
            for n in (1, 2, 3)
        ]
    )
    _seed_established(provider, "2", "NVDA")
    _cached(
        provider,
        [
            _row(
                symbol="NVDA",
                owner="1",
                direction="sell",
                shares=1_000.0,
                price=900.0,
                post_shares=999_000.0,
                transaction_date=today,
                accession="0000000001-26-000009",
            ),
            _row(
                symbol="NVDA",
                owner="2",
                direction="buy",
                shares=200.0,
                price=100.0,
                transaction_date=today,
                accession="0000000001-26-000010",
            ),
        ],
    )

    observations, _ = provider.fetch(["NVDA"])

    assert [row.signal_class for row in observations] == ["opportunistic", "routine"]
    assert observations[1].signal_class_reason == "calendar_routine"
    assert observations[1].transaction_value_usd > observations[0].transaction_value_usd


def test_history_index_survives_the_observation_cache_prune(tmp_path):
    """The lookback prune is exactly why a separate history file exists."""
    provider = _provider(tmp_path)
    old = date.today() - timedelta(days=400)
    provider._record_history(
        [
            {"actor_cik": "1", "symbol": "NVDA", "direction": "buy", "transaction_date": old.isoformat()},
            {"actor_cik": "1", "symbol": "NVDA", "direction": "buy", "transaction_date": "not-a-date"},
        ]
    )

    history = provider._load_history()

    assert history.prior_trades(
        "1",
        "NVDA",
        direction="buy",
        before=date.today(),
    ) == [InsiderPriorTrade(transaction_date=old, direction="buy")]


def test_history_index_prunes_beyond_retention(tmp_path):
    provider = _provider(tmp_path)
    ancient = date.today() - timedelta(days=6 * 366)
    provider._record_history(
        [
            {"actor_cik": "1", "symbol": "NVDA", "direction": "buy", "transaction_date": ancient.isoformat()},
        ]
    )

    assert json.loads(provider.stores.history_path.read_text()) == {}


def test_history_index_is_append_only_across_refreshes(tmp_path):
    provider = _provider(tmp_path)
    for day in (date(2024, 8, 3), date(2025, 8, 14)):
        provider._record_history(
            [
                {
                    "actor_cik": "1",
                    "symbol": "NVDA",
                    "direction": "buy",
                    "transaction_date": day.isoformat(),
                }
            ]
        )

    assert json.loads(provider.stores.history_path.read_text()) == {
        "1|NVDA": ["2024-08-03|buy", "2025-08-14|buy"],
    }


# --- fail closed: an unclassifiable filing is kept, never dropped ---------
#
# The owner's rule: a filing the classifier cannot place (missing data, an
# unrecognised stream, ...) comes back ``indeterminate``, never ``routine``
# — losing a real signal is worse than keeping noise. This section checks
# that guarantee survives the full ``SECForm4Provider.fetch`` pipeline, not
# just ``classify_transaction`` in isolation: an indeterminate row must
# still clear materiality/cluster gating and appear in the returned
# observations exactly like an opportunistic one would.


def test_indeterminate_filing_is_kept_by_fetch_not_dropped(tmp_path):
    """A sale with no reported post-transaction holding cannot be sized
    against the insider's position (``unknown_holding``), so the classifier
    returns ``indeterminate`` rather than guessing. Fail-closed means this
    row must still reach the operator — it is materially large enough to
    matter and nothing else about it is invalid."""
    provider = _provider(tmp_path)
    unclassifiable = _row(
        symbol="NVDA",
        direction="sell",
        shares=2_000.0,
        price=100.0,
        post_shares=None,
        transaction_date=date.today(),
    )
    _seed_established(provider, "1", "NVDA", "sell")
    _cached(provider, [unclassifiable])

    observations, error = provider.fetch(["NVDA"])

    assert error is None
    assert len(observations) == 1  # kept, not dropped
    assert observations[0].signal_class == "indeterminate"
    assert observations[0].signal_class_reason == "unknown_holding"


def test_indeterminate_filing_from_missing_amounts_is_not_downgraded_to_routine():
    """Same guarantee, different unclassifiable cause: shares/value missing
    entirely (``incomplete_amounts``) rather than just the post-transaction
    holding.

    Note on scope: a row with ``transaction_value_usd is None`` can never
    reach ``SECForm4Provider.fetch()``'s returned observations at all —
    the survivors loop (both the individual-materiality and the cluster
    path) requires ``transaction_value_usd is not None`` before a row is
    even added to the candidate window, so a value-unknown row is dropped
    there regardless of its signal_class. That gate is a materiality filter
    inherited unchanged from `b1944cd` (pre-dates this branch and
    routine/opportunistic classification entirely), not something this
    classifier introduced or can fail-closed around — you cannot assess
    dollar materiality for a transaction of unknown dollar value. It is
    flagged in the PR description as a separate, pre-existing gap rather
    than fixed here. What this test pins is the part that *is* this
    classifier's contract: at the classification layer itself, missing
    amounts produce ``indeterminate``, never ``routine`` — so nothing
    silently zeroes this row's weight or reason before the materiality gate
    even gets a chance to run.
    """
    verdict = classify_transaction(_row(shares=None, price=None), _established())

    assert verdict.label == "indeterminate"
    assert verdict.reason == "incomplete_amounts"


# --- configurable thresholds: no hardcoded constants -----------------------
#
# Every number the classifier compares against is an ``InsiderSignalThresholds``
# field, not a module constant — these tests change the thresholds and check
# the verdict actually moves, which a hardcoded number could not do. The right
# fix for a number nothing can source, though, is deletion rather than a knob:
# the first test below guards one that was deleted.


def test_there_is_no_sell_fraction_threshold_left_to_configure():
    """Guards the removal, not a value. `min_material_sell_fraction` was a
    cutoff no cited source measures; the fix was to delete it rather than to
    move it, so the field must not quietly reappear on either the classifier
    thresholds or the config model. If a future change needs one, it needs a
    source that measures a boundary first — WORK.md item 63."""
    from src.config import SmartMoneyConfig

    assert not hasattr(InsiderSignalThresholds(), "min_material_sell_fraction")
    assert "insider_min_material_sell_fraction" not in SmartMoneyConfig.model_fields


def test_custom_calendar_routine_years_changes_the_verdict():
    """One year of matching history is not routine under the literature's
    3-year default, but is routine under a 1-year threshold."""
    history = _history(days=[date(2025, 8, 14)])
    row = _row(transaction_date=date(2026, 8, 20))

    default_verdict = classify_transaction(row, history)
    assert default_verdict.label == "indeterminate"

    lenient_verdict = classify_transaction(
        row,
        history,
        InsiderSignalThresholds(calendar_routine_years=1),
    )
    assert lenient_verdict.label == "routine"
    assert lenient_verdict.reason == "calendar_routine"


def test_a_proportionally_tiny_sale_survives_the_provider_end_to_end(tmp_path):
    """End-to-end through the provider that ``src/pipeline.py`` builds: a 3%
    disposition used to come out of ``fetch`` labelled routine at weight 0.0.
    It now arrives with its ratio and band attached and nothing suppressed."""
    provider = _provider(tmp_path)
    row = _row(
        symbol="NVDA",
        direction="sell",
        shares=3_000.0,
        price=100.0,
        post_shares=97_000.0,
        transaction_date=date.today(),
    )
    _seed_established(provider, "1", "NVDA", "sell")
    _cached(provider, [row])
    observations, _ = provider.fetch(["NVDA"])

    assert [obs.signal_class for obs in observations] == ["opportunistic"]
    assert [obs.holdings_fraction for obs in observations] and observations[0].holdings_fraction < 0.1


# --- Trade size relative to the insider's own holding (WORK.md item 52) ----
#
# The board's item 52 assumed the desk had no holdings data for filers. It
# does: SEC Form 4 carries `postTransactionAmounts/sharesOwnedFollowingTransaction`
# on every open-market row, `SECForm4Provider._parse_submission` already
# parses it, and `SmartMoneyObservation.post_transaction_shares` already
# stores it. These tests pin the ratio that fact makes computable — reported
# for both directions, and never used as an admission cutoff.
#
# The legacy endpoints remain under 10%, 10-50%, and over 50%. These tests pin
# runtime stability only; item 90 records that applying the borrowed endpoints
# to one filing's ratio is unsupported.


def test_sell_fraction_is_measured_against_the_pre_transaction_holding():
    """Sold 25,000 of a 100,000-share holding is 25%, not 33% of what is left."""
    fraction, no_prior = holdings_fraction(_row(direction="sell", shares=25_000.0, post_shares=75_000.0))

    assert fraction == 0.25
    assert no_prior is False


def test_buy_fraction_is_measured_against_what_the_insider_already_held():
    """Bought 20,000 on top of an existing 80,000 is 25% added, not 20%."""
    fraction, no_prior = holdings_fraction(_row(direction="buy", shares=20_000.0, post_shares=100_000.0))

    assert fraction == 0.25
    assert no_prior is False


def test_purchase_by_an_insider_holding_nothing_is_a_distinct_band_not_a_gap():
    """An insider who held none beforehand has no denominator; that distinct
    fact must not be reported as missing filing data."""
    fraction, no_prior = holdings_fraction(_row(direction="buy", shares=5_000.0, post_shares=5_000.0))

    assert fraction is None
    assert no_prior is True


def test_missing_post_transaction_shares_reports_no_band_at_all():
    fraction, no_prior = holdings_fraction(_row(direction="sell", shares=1_000.0, post_shares=None))

    assert fraction is None
    assert no_prior is False


def test_purchase_detail_names_the_added_fraction_for_the_seat():
    verdict = classify_transaction(
        _row(direction="buy", shares=20_000.0, post_shares=100_000.0),
        _established(),
    )

    assert verdict.label == "opportunistic"
    assert "25.0%" in verdict.detail


def test_the_ratio_never_admits_or_rejects_a_row(tmp_path):
    """Item 52's decision: report the ratio, do not build a second gate on
    it. Two purchases identical in dollars but far apart in holdings share
    must both survive the provider's admission and materiality filters."""
    today = date.today()
    tiny = _row(
        symbol="AAPL",
        owner="1",
        direction="buy",
        transaction_date=today,
        shares=1_000.0,
        price=100.0,
        post_shares=1_000_000.0,
        accession="0000000001-26-000101",
    )
    huge = _row(
        symbol="AAPL",
        owner="2",
        direction="buy",
        transaction_date=today,
        shares=1_000.0,
        price=100.0,
        post_shares=1_500.0,
        accession="0000000001-26-000102",
        row=1,
    )

    provider = _provider(tmp_path / "ratio")
    _cached(provider, [tiny, huge])
    observations, _ = provider.fetch(["AAPL"])

    assert {obs.accession_number for obs in observations} == {
        "0000000001-26-000101",
        "0000000001-26-000102",
    }
    ratios = {obs.accession_number: obs.holdings_fraction for obs in observations}
    assert ratios["0000000001-26-000101"] == 1_000.0 / 999_000.0
    assert ratios["0000000001-26-000102"] == 2.0


# --- Cohen, Malloy & Pomorski (2012) Table A3, per-trade rule ---------------


def test_cmp_routine_same_month_in_each_of_three_preceding_years():
    verdict = classify_transaction(
        _row(symbol="ZZZA", transaction_date=date(2026, 3, 10)),
        _history(symbol="ZZZA", days=[date(2025, 3, 2), date(2024, 3, 20), date(2023, 3, 5)]),
    )
    assert (verdict.label, verdict.reason) == ("routine", "calendar_routine")


def test_cmp_opportunistic_traded_each_year_but_different_months():
    verdict = classify_transaction(
        _row(symbol="ZZZB", transaction_date=date(2026, 3, 10)),
        _history(symbol="ZZZB", days=[date(2025, 3, 2), date(2024, 9, 20), date(2023, 3, 5)]),
    )
    assert verdict.label == "opportunistic"


def test_cmp_gap_year_is_indeterminate_insufficient_history():
    verdict = classify_transaction(
        _row(symbol="ZZZC", transaction_date=date(2026, 3, 10)),
        _history(symbol="ZZZC", days=[date(2025, 3, 2), date(2023, 3, 5), date(2022, 3, 9)]),
    )
    assert (verdict.label, verdict.reason) == ("indeterminate", "insufficient_history")
    assert verdict.label != "opportunistic"


def test_cmp_no_history_at_all_is_indeterminate_not_opportunistic():
    verdict = classify_transaction(_row(symbol="ZZZD"), InsiderHistory())
    assert (verdict.label, verdict.reason) == ("indeterminate", "insufficient_history")
