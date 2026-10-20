"""EDGAR's own filing count, used as the coverage signal — board item 126.

THE DEFECT
----------
`data_status["smart_money"]` was set to "ok" whenever no provider error was
raised, with no reference to how much was actually fetched. So these two
produced identical evidence — zero rows, no error, a clean seat:

  * a genuinely quiet day, on which nobody filed a Form 4 on a listed
    issuer; and
  * a fetch that came back broken — an empty body, a body with no usable
    count in it, a page that stopped short of what EDGAR said was there.

The denominator was already in hand: EDGAR's EFTS search returns
`hits.total.value`, its own count of the Form 4s filed on the queried day.
`_discover` read it for pagination and threw it away.

WHAT IS TESTED HERE
-------------------
Every case is driven through `_discover` / `refresh` against EFTS-shaped
bodies, not through a mocked-out coverage dict, because the thing under
test IS the reading of those bodies.

  * a quiet day stays CLEAN (the loudest requirement item 126 makes);
  * a scan bounded by the desk's own cap or deadline stays CLEAN, because
    those are choices, not failures, and their residue is reported
    elsewhere;
  * every broken shape reads as NOT verified, with a named reason.

THE TWO JUDGEMENTS, AND WHY NEITHER IS A CHOSEN NUMBER
------------------------------------------------------
`verified` is an exact condition, not a cut point, and it is made of two:

  * every day slice queried handed back a count of its own that this code
    could read — and EFTS's capped `relation: "gte"` count is EDGAR
    declining to give one, not a count;
  * every day the scan paged all the way through that count yielded as many
    distinct readable filings as the count said were there.

Both are arithmetic. Nothing here picks a fraction, and the cost of the
second — one unreadable row is a shortfall — is stated in the test that
covers it rather than softened into a percentage.
"""

from datetime import timedelta
from unittest.mock import Mock

import pytest

from src.data.smart_money import SECForm4Provider, et_today
from src.data.smart_money_edgar_coverage import (
    UNVERIFIED_EDGAR_REASONS,
    blank_edgar_coverage,
    edgar_coverage,
)

NVDA_CIK = "1045810"
LISTED = {NVDA_CIK: {"NVDA": "Nasdaq"}}


def _hit(accession: str, cik: str = NVDA_CIK, form: str = "4") -> dict:
    return {"_source": {"adsh": accession, "form": form, "ciks": [cik]}}


def _accession(n: int) -> str:
    return f"{n:010d}-26-000001"


def _provider(tmp_path, **kwargs):
    provider = SECForm4Provider(data_dir=str(tmp_path), **kwargs)
    provider.session.get = Mock(side_effect=AssertionError("no live SEC calls"))
    return provider


def _bodies(by_page):
    """Fake EFTS returning a caller-chosen body per (day, from) page.

    `by_page` maps (startdt, from) -> the JSON body. A page with no entry
    gets the empty-but-well-formed body EDGAR sends for a day with nothing
    on it, which is the case that must stay clean.
    """

    def _get(url, *, params, deadline):
        key = (str(params.get("startdt")), int(params.get("from") or 0))
        response = Mock()
        response.json.return_value = by_page.get(
            key,
            {"hits": {"hits": [], "total": {"value": 0}}},
        )
        return response

    return _get


def _scan(provider, monkeypatch, by_page, symbols=("NVDA",)):
    monkeypatch.setattr(provider, "_get", _bodies(by_page))
    stats: dict = {}
    priority = provider._ciks_for_symbols(LISTED, list(symbols))
    found = provider._discover(LISTED, float("inf"), set(), priority, stats)
    return found, stats, edgar_coverage(stats)


# ---------------------------------------------------------------------------
# the quiet day, which must stay clean
# ---------------------------------------------------------------------------


def test_a_genuinely_quiet_day_is_verified_and_clean(tmp_path, monkeypatch):
    """EDGAR says zero filings and hands back zero rows. That is an ANSWER,
    and reporting it as degraded is the harm item 126 names out loud: the
    evidence gate treats a degraded seat as a loss, and on 2026-09-16 that
    made Risk veto a whole intraday plan."""
    provider = _provider(tmp_path, lookback_days=2)
    found, stats, coverage = _scan(provider, monkeypatch, {})

    assert found == []
    assert coverage["verified"] is True
    assert coverage["known"] is True
    assert coverage["edgar_total"] == 0
    assert coverage["enumerated"] == 0
    # "0 of 0" is not a fraction; claiming 1.0 would assert a completeness
    # nothing measured.
    assert coverage["ratio"] is None
    assert coverage["days_queried"] == 3 == coverage["days_with_total"]
    assert not (set(coverage["reasons"]) & UNVERIFIED_EDGAR_REASONS)


def test_a_full_day_read_to_edgars_own_count_is_verified(tmp_path, monkeypatch):
    """The ordinary good day: one page, EDGAR's count matches what came
    back, ratio 1.0."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=50)
    day = et_today().isoformat()
    hits = [_hit(_accession(i)) for i in range(1, 8)]
    found, stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": hits, "total": {"value": 7}}},
        },
    )

    assert len(found) == 7
    assert coverage["verified"] is True
    assert (coverage["edgar_total"], coverage["enumerated"]) == (7, 7)
    assert coverage["ratio"] == 1.0
    assert coverage["reasons"] == []


def test_realistic_multi_page_pagination_walks_to_the_count(tmp_path, monkeypatch):
    """EFTS pages at 100. A 250-filing day is three requests, and the scan
    is only finished when `from` reaches EDGAR's own count — not when a page
    happens to come back short."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=500)
    day = et_today().isoformat()

    def page(start, count):
        return {
            "hits": {
                "hits": [_hit(_accession(i)) for i in range(start, start + count)],
                "total": {"value": 250},
            }
        }

    found, stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): page(1, 100),
            (day, 100): page(101, 100),
            (day, 200): page(201, 50),
        },
    )

    assert len(found) == 250
    assert coverage["verified"] is True
    assert (coverage["edgar_total"], coverage["enumerated"]) == (250, 250)
    assert coverage["ratio"] == 1.0


def test_edgar_total_given_as_a_bare_integer_is_read(tmp_path, monkeypatch):
    """EFTS has answered with both `{"total": {"value": N}}` and a bare
    `{"total": N}`. Both are EDGAR telling us the count; neither may be
    read as "it did not say"."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": [_hit(_accession(1))], "total": 1}},
        },
    )
    assert coverage["verified"] is True
    assert coverage["edgar_total"] == 1


# ---------------------------------------------------------------------------
# the desk's OWN bounds are not failures
# ---------------------------------------------------------------------------


def test_a_cap_bound_scan_is_still_verified(tmp_path, monkeypatch):
    """`max_filings_per_refresh` binding is the budget working as designed,
    and its residue is already reported through `pending_filings` and the
    per-issuer read-through map. If it flipped coverage to unverified the
    seat would read degraded every single morning, which is worse than the
    defect being fixed."""
    provider = _provider(tmp_path, lookback_days=3, max_filings_per_refresh=2)
    day = et_today().isoformat()
    hits = [_hit(_accession(i)) for i in range(1, 11)]
    # No watched names: every hit here is on NVDA, and the cap bounds the
    # MARKET-WIDE bucket, so a stream made entirely of the desk's own names
    # would never reach it. That is by design as of 2026-09-23 — watched
    # coverage is the drain's job — and it is not what this test is about.
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": hits, "total": {"value": 10}}},
        },
        symbols=(),
    )

    assert "scan_cap_reached" in coverage["reasons"]
    assert "days_not_queried" in coverage["reasons"]
    assert coverage["verified"] is True, coverage["reasons"]
    # The ratio is honest about it: 10 rows walked out of 10 EDGAR
    # reported FOR THE DAYS QUERIED, and the days not reached are named.
    assert coverage["days_queried"] < coverage["days_in_window"]


def test_a_day_the_budget_never_reached_is_not_counted_as_queried(
    tmp_path,
    monkeypatch,
):
    """A day the scan never asked about must not count toward the days it
    did ask about — "I checked nothing, so nothing is outstanding" is the
    exact silent-degradation shape this record exists to remove."""
    provider = _provider(tmp_path, lookback_days=9, max_filings_per_refresh=1)
    day = et_today().isoformat()
    # `symbols=()` for the same reason as the cap test above.
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": [_hit(_accession(1))], "total": {"value": 1}}},
        },
        symbols=(),
    )
    assert coverage["days_queried"] == 1
    assert coverage["days_in_window"] == 10


# ---------------------------------------------------------------------------
# every broken shape — the point of the item
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "body", "reason"),
    [
        # HTTP 200, body says nothing at all. The old code coerced the
        # missing count to 0, decided the day was finished, and reported a
        # quiet day.
        ("empty object", {}, "edgar_total_unreadable"),
        ("no hits block", {"other": 1}, "edgar_total_unreadable"),
        ("hits block is not a dict", {"hits": []}, "edgar_total_unreadable"),
        ("no total key", {"hits": {"hits": []}}, "edgar_total_unreadable"),
        ("total is null", {"hits": {"hits": [], "total": None}}, "edgar_total_unreadable"),
        ("total value is a word", {"hits": {"hits": [], "total": {"value": "many"}}}, "edgar_total_unreadable"),
        ("total value is negative", {"hits": {"hits": [], "total": {"value": -1}}}, "edgar_total_unreadable"),
        ("total value is a bool", {"hits": {"hits": [], "total": {"value": True}}}, "edgar_total_unreadable"),
        # A count is present and non-zero and NOTHING came back with it.
        (
            "count with no rows",
            {"hits": {"hits": [], "total": {"value": 412}}},
            "edgar_returned_no_hits_for_nonzero_total",
        ),
        # `hits.hits` is not a list — a 200 whose body this code cannot walk.
        ("hits is a string", {"hits": {"hits": "oops", "total": {"value": 5}}}, "edgar_hits_unreadable"),
    ],
)
def test_a_broken_fetch_is_never_verified(tmp_path, monkeypatch, name, body, reason):
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(provider, monkeypatch, {(day, 0): body})

    assert coverage["verified"] is False, f"{name}: {coverage}"
    assert reason in coverage["reasons"], f"{name}: {coverage['reasons']}"


def test_a_page_of_garbage_rows_cannot_buy_a_clean_status(tmp_path, monkeypatch):
    """The gaming shape: HTTP 200, a technically non-empty body, a count
    that matches the number of rows returned. Counting rows alone would
    read ratio 1.0 and call it verified. A whole page of rows that could
    not have answered a `forms=4` query is a body dressed as an answer."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    junk = [{"_source": {"adsh": "not-an-accession", "form": "10-K"}}] * 5
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": junk, "total": {"value": 5}}},
        },
    )

    assert coverage["verified"] is False
    assert "edgar_hits_malformed" in coverage["reasons"]


def test_a_single_unreadable_row_is_also_a_shortfall(tmp_path, monkeypatch):
    """The cost of the exact condition, stated plainly rather than hidden:
    ONE unreadable row on a day the scan claims to have read through makes
    that claim false, so the seat goes partial. Deliberate, and justified by
    measurement rather than by taste — a review walked three real EDGAR days
    (2026-09-18, 2026-09-17, 2026-01-05; 3,947 rows) and found every row's
    accession well formed, every form either 4 or 4/A, and distinct
    accessions exactly equal to EDGAR's own count on all three. An "EDGAR
    oddity" is a hypothesis; a shortfall is a measurement. If this ever does
    start firing on ordinary traffic, that is evidence about EDGAR worth
    having, not a reason to soften the condition into a fraction."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    rows = [_hit(_accession(1)), {"_source": {"adsh": "junk", "form": "3"}}]
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": rows, "total": {"value": 2}}},
        },
    )

    assert coverage["verified"] is False
    assert "edgar_rows_unreadable" in coverage["reasons"]
    # ...but NOT the whole-day guard, which is a different statement.
    assert "edgar_hits_malformed" not in coverage["reasons"]


def test_a_cap_bound_day_is_not_held_to_the_shortfall_check(tmp_path, monkeypatch):
    """The shortfall check applies only to a day the scan CLAIMS to have
    read through. A day the budget stopped part-way never made that claim,
    and holding it to one would mark the seat degraded every morning — the
    harm board item 126 names in its own text."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=2)
    day = et_today().isoformat()
    hits = [_hit(_accession(i)) for i in range(1, 11)]
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": hits, "total": {"value": 10}}},
        },
    )

    assert "scan_cap_reached" in coverage["reasons"]
    assert "edgar_rows_unreadable" not in coverage["reasons"]
    assert coverage["verified"] is True


def test_edgars_at_least_count_is_not_a_count(tmp_path, monkeypatch):
    """EFTS caps `hits.total.value` at 10,000 and says so with
    `relation: "gte"` — "at least this many", not "this many" [measured
    against efts.sec.gov during review: a month-wide forms=4 query returns
    exactly {"value": 10000, "relation": "gte"}]. Reading that as an exact
    count would let the scan page to the cap, take the "read through EDGAR's
    own count" branch, and report complete coverage of a day whose true size
    it never learned — this item's defect, reappearing inside the branch
    labelled clean. "At least N" is EDGAR declining to give a denominator."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=50_000)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {
                "hits": {
                    "hits": [_hit(_accession(i)) for i in range(1, 101)],
                    "total": {"value": 10000, "relation": "gte"},
                }
            },
        },
    )

    assert coverage["verified"] is False
    assert "edgar_total_unreadable" in coverage["reasons"]
    # ...and an explicit `eq` is read exactly as a bare value is.
    _found, _stats, exact = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {
                "hits": {
                    "hits": [_hit(_accession(1))],
                    "total": {"value": 1, "relation": "eq"},
                }
            },
        },
    )
    assert exact["verified"] is True
    assert exact["edgar_total"] == 1


def test_deep_pagination_cut_off_short_of_the_count_is_not_verified(
    tmp_path,
    monkeypatch,
):
    """EFTS caps deep pagination. A page that comes back shorter than the
    page size while EDGAR's own count says there is more is a truncated
    read — and the old loop treated exactly that as "finished"."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=500)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {
                "hits": {
                    "hits": [_hit(_accession(i)) for i in range(1, 101)],
                    "total": {"value": 250},
                }
            },
            (day, 100): {
                "hits": {
                    "hits": [_hit(_accession(i)) for i in range(101, 141)],
                    "total": {"value": 250},
                }
            },
        },
    )

    assert coverage["verified"] is False
    assert "edgar_page_short_of_total" in coverage["reasons"]
    # The ratio is reported, and it is the fact a reader wants: 140 of the
    # 250 EDGAR said were there.
    assert (coverage["edgar_total"], coverage["enumerated"]) == (250, 140)
    assert coverage["ratio"] == 0.56


def test_one_broken_day_in_a_window_of_good_ones_is_not_verified(
    tmp_path,
    monkeypatch,
):
    """Coverage is only as verified as the least verified day slice. A
    window that mostly worked is still a window the desk cannot account
    for."""
    provider = _provider(tmp_path, lookback_days=3, max_filings_per_refresh=500)
    today = et_today()
    from datetime import timedelta

    broken_day = (today - timedelta(days=2)).isoformat()
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (broken_day, 0): {"hits": {"hits": [], "total": {"value": 300}}},
        },
    )

    assert coverage["verified"] is False
    assert "edgar_returned_no_hits_for_nonzero_total" in coverage["reasons"]
    assert coverage["days_queried"] == 4


# ---------------------------------------------------------------------------
# the record survives to the places that read it
# ---------------------------------------------------------------------------


def test_refresh_records_coverage_where_the_seat_reads_it(tmp_path, monkeypatch):
    """The seat reads coverage from the manifest with no network. A figure
    taken after `refresh`'s own retention prune would measure the prune;
    this one is recorded during the fetch."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=50)
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: LISTED)
    monkeypatch.setattr(provider, "watched_form4_index", lambda *a, **k: ({}, []))
    monkeypatch.setattr(
        provider,
        "_submission",
        lambda *a, **k: ("<ownershipDocument/>", "u"),
    )
    monkeypatch.setattr(provider, "_parse_submission", lambda *a, **k: [])
    day = et_today().isoformat()
    monkeypatch.setattr(
        provider,
        "_get",
        _bodies(
            {
                (day, 0): {
                    "hits": {
                        "hits": [_hit(_accession(1)), _hit(_accession(2))],
                        "total": {"value": 2},
                    }
                },
            }
        ),
    )

    result = provider.refresh(["NVDA"])

    assert result["edgar_coverage"]["verified"] is True
    assert result["edgar_coverage"]["ratio"] == 1.0
    assert provider.form4_coverage()["edgar"]["verified"] is True


def test_a_broken_refresh_records_an_unverified_coverage(tmp_path, monkeypatch):
    """The whole point, end to end: nothing came back, no exception was
    raised, and the recorded coverage says so."""
    provider = _provider(tmp_path, lookback_days=0)
    monkeypatch.setattr(provider, "_listed_map", lambda _deadline: LISTED)
    monkeypatch.setattr(provider, "watched_form4_index", lambda *a, **k: ({}, []))
    day = et_today().isoformat()
    monkeypatch.setattr(provider, "_get", _bodies({(day, 0): {}}))

    result = provider.refresh(["NVDA"])

    # No provider error — this is precisely why the status alone could not
    # tell anyone anything.
    assert result["error"] is None
    assert result["status"] == "ok"
    assert result["edgar_coverage"]["verified"] is False
    assert "edgar_total_unreadable" in result["edgar_coverage"]["reasons"]
    assert provider.form4_coverage()["edgar"]["verified"] is False


def test_a_manifest_written_before_this_shipped_reads_as_unverified(tmp_path):
    """Never-recorded is not evidence of a clean fetch. A cache from before
    board item 126 must read as unverified, not as complete."""
    import json

    provider = _provider(tmp_path, lookback_days=0)
    provider.stores.manifest_path.write_text(
        json.dumps(
            {
                "coverage_as_of": et_today().isoformat(),
                "watched_names": 4,
                "watched_names_read_through": 4,
                "watched_names_unread": [],
            }
        )
    )

    coverage = provider.form4_coverage()
    assert coverage["known"] is True
    assert coverage["edgar"]["verified"] is False
    assert coverage["edgar"]["reasons"] == ["never_recorded"]


def test_stats_from_a_stubbed_discover_read_as_unverified():
    """`edgar_coverage` is handed whatever `_discover` recorded. A stub or
    an older caller that recorded nothing answers no question at all, and
    must not be read as answering it cleanly."""
    for stats in ({}, None, {"candidates": 3}, "not a dict"):
        coverage = edgar_coverage(stats)
        assert coverage["verified"] is False, stats
        assert coverage["reasons"] == ["never_recorded"], stats


def test_combined_provider_is_only_as_verified_as_its_least_verified_half():
    from src.data.congressional_trading import CombinedSmartMoneyProvider

    good = {
        "known": True,
        "verified": True,
        "reasons": [],
        "edgar_total": 10,
        "enumerated": 10,
        "ratio": 1.0,
        "days_queried": 1,
        "days_in_window": 1,
        "days_with_total": 1,
    }
    bad = {**good, "verified": False, "reasons": ["edgar_page_short_of_total"], "enumerated": 4, "ratio": 0.4}

    def _sub(edgar):
        class _P:
            def form4_coverage(self):
                return {
                    "known": True,
                    "as_of": "2026-09-21",
                    "watched": 2,
                    "read_through": 2,
                    "unread": [],
                    "edgar": edgar,
                }

        return _P()

    combined = CombinedSmartMoneyProvider.__new__(CombinedSmartMoneyProvider)
    combined.providers = [_sub(good), _sub(bad)]
    merged = combined.form4_coverage()

    assert merged["edgar"]["verified"] is False
    assert "edgar_page_short_of_total" in merged["edgar"]["reasons"]
    assert merged["edgar"]["enumerated"] == 14
    assert merged["edgar"]["edgar_total"] == 20

    combined.providers = [_sub(good), _sub(dict(good))]
    assert combined.form4_coverage()["edgar"]["verified"] is True

    # A sub-provider that reports no `edgar` key at all.
    combined.providers = [_sub(good), _sub(None)]
    assert combined.form4_coverage()["edgar"]["verified"] is False


def test_the_pre_open_alert_names_the_unreadable_count(monkeypatch):
    """Item 126's first half: the fetch's own counts must reach the OWNER
    surface, not only the log. The pre-open alert is that surface."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    today = et_today().isoformat()
    clean = {
        "watched_read_through": today,
        "watched_pending_filings": 0,
        "watched_unchecked_names": [],
        "discovery_cap_reached": False,
        "watched_names": 4,
        "watched_names_read_through": 4,
        "watched_drain_ran": True,
        "watched_drain_deadline_hit": False,
        "edgar_coverage": {
            "known": True,
            "verified": True,
            "reasons": [],
            "enumerated": 900,
            "edgar_total": 900,
            "days_queried": 2,
            "days_in_window": 366,
        },
    }
    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    alert(clean)
    assert sent == []

    alert(
        {
            **clean,
            "edgar_coverage": {
                "known": True,
                "verified": False,
                "reasons": ["edgar_returned_no_hits_for_nonzero_total"],
                "enumerated": 0,
                "edgar_total": 412,
                "days_queried": 2,
                "days_in_window": 366,
            },
        }
    )
    assert len(sent) == 1
    assert "could not be fully checked" in sent[0]
    # Reason codes stay in the log; the owner gets plain words and the counts.
    assert "edgar_returned_no_hits_for_nonzero_total" not in sent[0]
    assert "0 of 412" in sent[0]
    assert "2 most recent days of a 366-day" in sent[0]


def test_the_pre_open_alert_fails_closed_on_a_missing_coverage_record(monkeypatch):
    """The alert and the morning seat must agree about a missing record.
    Written after a review found them reading the SAME fact in opposite
    directions: the seat treated an absent `edgar_coverage` as unverified
    and this alert treated it as nothing to say."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    today = et_today().isoformat()
    base = {
        "watched_read_through": today,
        "watched_pending_filings": 0,
        "watched_unchecked_names": [],
        "discovery_cap_reached": False,
        "watched_names": 4,
        "watched_names_read_through": 4,
        "watched_drain_ran": True,
        "watched_drain_deadline_hit": False,
    }
    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    alert(base)  # a Form 4 pass ran and recorded no coverage at all
    assert len(sent) == 1
    assert "no coverage was recorded at all" in sent[0]

    # ...but a refresh with no Form 4 half at all has nothing to say about
    # EDGAR coverage. It still alerts for the reason it always did — no
    # confirmed read-through — and must not add a coverage complaint on top.
    sent.clear()
    alert({"status": "ok", "congressional": {}})
    assert len(sent) == 1
    assert "filing service did not account" not in sent[0]


# ---------------------------------------------------------------------------
# what a review of this change found, and what was done about it
# ---------------------------------------------------------------------------


def test_a_replayed_page_cannot_buy_full_coverage(tmp_path, monkeypatch):
    """A cache or proxy that answers every offset with the SAME page used to
    walk `from` all the way to EDGAR's own count and report ratio 1.0 on
    100 real filings. Coverage counts DISTINCT readable rows, so it now
    reports what it actually saw."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=5000)
    day = et_today().isoformat()
    same_page = {
        "hits": {
            "hits": [_hit(_accession(i)) for i in range(1, 101)],
            "total": {"value": 1000},
        }
    }
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {(day, start): same_page for start in range(0, 1000, 100)},
    )

    assert coverage["edgar_total"] == 1000
    assert coverage["rows_received"] == 1000
    assert coverage["enumerated"] == 100
    assert coverage["ratio"] == 0.1
    # And the day claimed to be read through while 900 of the 1,000 filings
    # EDGAR reported were never seen, so the claim is refused outright.
    assert coverage["verified"] is False
    assert "edgar_rows_unreadable" in coverage["reasons"]


def test_junk_rows_do_not_count_as_coverage_even_beside_a_good_one(
    tmp_path,
    monkeypatch,
):
    """The first guard fired only when EVERY row on a page was junk, so 99
    junk rows plus one good one read as full coverage — a 100% rule wearing
    a structural shape, which this desk forbids. Readable rows are counted
    directly instead, so the ratio tells the truth with no cut point in it
    anywhere. The SECOND review round then pointed out that reporting an
    honest 0.01 while still calling the read verified leaves the original
    symptom alive in milder form, so the shortfall itself is now named:
    a day the scan paged all the way through EDGAR's own count and could
    not read that many filings out of it did not read that day through."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=5000)
    day = et_today().isoformat()
    rows = [{"_source": {"adsh": "junk", "form": "10-K"}} for _ in range(99)]
    rows.append(_hit(_accession(1)))
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": rows, "total": {"value": 100}}},
        },
    )

    assert coverage["rows_received"] == 100
    assert coverage["enumerated"] == 1
    assert coverage["ratio"] == 0.01
    assert coverage["verified"] is False
    assert "edgar_rows_unreadable" in coverage["reasons"]


def test_a_day_of_nothing_but_junk_is_not_verified(tmp_path, monkeypatch):
    """The garbage-body case that remains a coverage failure rather than a
    low ratio: rows came back for the day and not one of them could have
    answered a `forms=4` query."""
    provider = _provider(tmp_path, lookback_days=0)
    day = et_today().isoformat()
    junk = [{"_source": {"adsh": "nope", "form": "10-K"}} for _ in range(5)]
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {"hits": {"hits": junk, "total": {"value": 5}}},
        },
    )

    assert coverage["verified"] is False
    assert "edgar_hits_malformed" in coverage["reasons"]


def test_a_total_that_changes_mid_day_is_named(tmp_path, monkeypatch):
    """A later page disagreeing with the first about how much exists used
    to end the slice early with no reason recorded. Pagination now uses the
    PINNED first count and the disagreement is named."""
    provider = _provider(tmp_path, lookback_days=0, max_filings_per_refresh=5000)
    day = et_today().isoformat()
    _found, _stats, coverage = _scan(
        provider,
        monkeypatch,
        {
            (day, 0): {
                "hits": {
                    "hits": [_hit(_accession(i)) for i in range(1, 101)],
                    "total": {"value": 250},
                }
            },
            (day, 100): {
                "hits": {
                    "hits": [_hit(_accession(i)) for i in range(101, 201)],
                    "total": {"value": 100},
                }
            },
        },
    )

    assert coverage["verified"] is False
    assert "edgar_total_changed" in coverage["reasons"]
    assert coverage["edgar_total"] == 250


def test_a_production_shaped_window_reports_how_little_it_reached(
    tmp_path,
    monkeypatch,
):
    """The production window is 365 lookback days and the market-wide pass
    is bounded by a deadline measured at ~153 s, so it reaches only the
    first few day slices. A ratio of 1.0 over two days of a 366-day window
    is an honest statement about two days and a misleading one about the
    window — so the fraction is reported beside the ratio, everywhere the
    ratio goes."""
    from src.data.smart_money import _RefreshDeadline

    provider = _provider(tmp_path, lookback_days=365, max_filings_per_refresh=5000)
    calls = {"n": 0}

    def _get(url, *, params, deadline):
        calls["n"] += 1
        if calls["n"] > 2:
            raise _RefreshDeadline()
        response = Mock()
        response.json.return_value = {
            "hits": {
                "hits": [_hit(_accession(calls["n"]))],
                "total": {"value": 1},
            }
        }
        return response

    monkeypatch.setattr(provider, "_get", _get)
    stats: dict = {}
    provider._discover(
        LISTED,
        float("inf"),
        set(),
        provider._ciks_for_symbols(LISTED, ["NVDA"]),
        stats,
    )
    coverage = edgar_coverage(stats)

    assert coverage["ratio"] == 1.0
    assert coverage["days_queried"] == 2
    assert coverage["days_in_window"] == 366
    assert coverage["window_fraction"] == round(2 / 366, 4)
    assert "days_not_queried" in coverage["reasons"]
    assert "refresh_deadline_exceeded" in coverage["reasons"]
    # Deliberately still verified: the deadline is the desk's own bound,
    # and the fraction beside the ratio is what stops 1.0 being read as a
    # statement about the whole window.
    assert coverage["verified"] is True


def test_yesterdays_coverage_is_not_todays(tmp_path):
    """A coverage record from an earlier pass describes an earlier fetch.
    Accepting it would reintroduce this defect one day late: nothing
    fetched today, seat reports clean."""
    import json

    from datetime import timedelta

    provider = _provider(tmp_path, lookback_days=0)
    yesterday = (et_today() - timedelta(days=1)).isoformat()
    provider.stores.manifest_path.write_text(
        json.dumps(
            {
                "coverage_as_of": yesterday,
                "watched_names": 4,
                "watched_names_read_through": 4,
                "watched_names_unread": [],
                "edgar_coverage": {
                    "known": True,
                    "verified": True,
                    "reasons": [],
                    "edgar_total": 9,
                    "enumerated": 9,
                    "ratio": 1.0,
                    "days_queried": 1,
                    "days_in_window": 1,
                    "days_with_total": 1,
                },
            }
        )
    )

    edgar = provider.form4_coverage()["edgar"]
    assert edgar["verified"] is False
    assert "edgar_coverage_stale" in edgar["reasons"]


def test_the_alert_carries_the_coverage_counts_whatever_it_is_about(monkeypatch):
    """Second review round: the window figure only reached the owner on the
    branch where coverage itself was the complaint, so on an ordinary
    morning the honest "how much of the window did we actually check"
    number reached nobody. It now rides on every one of these alerts, and
    is logged on every pass."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    # The complaint is unread filings, NOT coverage — coverage is verified.
    alert(
        {
            "watched_read_through": et_today().isoformat(),
            "watched_pending_filings": 3,
            "watched_unchecked_names": [],
            "discovery_cap_reached": False,
            "watched_names": 4,
            "watched_names_read_through": 4,
            "watched_drain_ran": True,
            "watched_drain_deadline_hit": False,
            "edgar_coverage": {
                "known": True,
                "verified": True,
                "reasons": [],
                "enumerated": 435,
                "edgar_total": 435,
                "days_queried": 76,
                "days_in_window": 366,
                "window_fraction": 0.2077,
            },
        }
    )
    assert len(sent) == 1
    assert "still unread" in sent[0]
    assert "read 435 of 435 filings" in sent[0]
    assert "76 most recent days of a 366-day" in sent[0]


def test_a_form4_provider_that_blew_up_still_answers_for_coverage(monkeypatch):
    """A sub-provider that raises outright is replaced with a
    `provider_error` dict carrying neither the drain keys nor a coverage
    record, so scoping the coverage clause on the drain keys alone left the
    loudest case saying nothing about coverage — in a change made to stop
    coverage failing silently."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    alert({"status": "provider_error", "error": "0:SECForm4Provider:boom"})
    assert len(sent) == 1
    assert "no coverage was recorded at all" in sent[0]


def test_the_combined_merge_treats_days_as_a_window_not_a_quantity():
    """Second review round: `days_queried` used 0 as both "unset" and a real
    value, so a provider that legitimately queried zero days was overwritten
    instead of min'd; and `days_with_total` was summed beside a min'd
    `days_queried`, which could claim more days with a readable count than
    days queried at all."""
    from src.data.congressional_trading import (
        CombinedSmartMoneyProvider,
        _form4_drain_summary,
    )

    wide = {
        "known": True,
        "verified": True,
        "reasons": [],
        "edgar_total": 10,
        "enumerated": 10,
        "rows_received": 10,
        "ratio": 1.0,
        "days_queried": 76,
        "days_in_window": 366,
        "days_with_total": 76,
        "window_fraction": 0.2077,
    }
    none_at_all = {
        **wide,
        "days_queried": 0,
        "days_with_total": 0,
        "days_in_window": 15,
        "edgar_total": 0,
        "enumerated": 0,
    }

    def _sub(edgar):
        class _P:
            def form4_coverage(self):
                return {
                    "known": True,
                    "as_of": "2026-09-21",
                    "watched": 2,
                    "read_through": 2,
                    "unread": [],
                    "edgar": edgar,
                }

        return _P()

    combined = CombinedSmartMoneyProvider.__new__(CombinedSmartMoneyProvider)
    combined.providers = [_sub(wide), _sub(none_at_all)]
    edgar = combined.form4_coverage()["edgar"]

    assert edgar["days_queried"] == 0
    assert edgar["days_in_window"] == 366
    assert edgar["days_with_total"] <= edgar["days_queried"]
    assert edgar["window_fraction"] == 0.0

    # The same fact, read the same way one layer down: a Form 4 result with
    # no coverage record must DRAG the merged verdict, not be skipped.
    summary = _form4_drain_summary(
        [
            {"watched_drain_ran": True, "watched_read_through": "2026-09-21", "edgar_coverage": dict(wide)},
            {"watched_drain_ran": True, "watched_read_through": "2026-09-21"},
        ]
    )
    assert summary["edgar_coverage"]["verified"] is False
    assert "never_recorded" in summary["edgar_coverage"]["reasons"]


def test_an_ordinary_clean_morning_logs_the_coverage_and_alerts_nobody(
    monkeypatch,
    caplog,
):
    """The case the whole `window_fraction` objection was about: nothing is
    wrong, so no alert fires — and the honest "how much of the window did we
    actually reach" figure must still be written down somewhere a person can
    find it. It goes to the log on every pass, alert or no alert."""
    import logging

    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    with caplog.at_level(logging.INFO, logger="src.pipeline"):
        alert(
            {
                "watched_read_through": et_today().isoformat(),
                "watched_pending_filings": 0,
                "watched_unchecked_names": [],
                "discovery_cap_reached": False,
                "watched_names": 82,
                "watched_names_read_through": 82,
                "watched_drain_ran": True,
                "watched_drain_deadline_hit": False,
                "edgar_coverage": {
                    "known": True,
                    "verified": True,
                    "reasons": [],
                    "enumerated": 435,
                    "edgar_total": 435,
                    "days_queried": 76,
                    "days_in_window": 366,
                    "window_fraction": 0.2077,
                },
            }
        )

    assert sent == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "76 most recent days of a 366-day" in logged
    assert "read 435 of 435 filings" in logged


def test_a_blank_coverage_record_reads_as_unknown_not_as_nothing_to_report(
    monkeypatch,
):
    """ "read 0 of 0 filings across 0 of 0 days" is what a record that was
    never written looks like, and to a human it reads as reassurance. It has
    to read as the opposite — the same trap `ratio` avoids by answering None
    to nought-of-nought rather than 1.0."""
    import src.notifier as notifier
    from src.pipeline import TradingPipeline

    sent: list[str] = []
    monkeypatch.setattr(notifier, "send_owner_alert", lambda text, **_: sent.append(text))
    alert = TradingPipeline._alert_form4_backlog_before_open.__get__(object())

    alert(
        {
            "watched_read_through": et_today().isoformat(),
            "watched_pending_filings": 0,
            "watched_unchecked_names": [],
            "discovery_cap_reached": False,
            "watched_names": 82,
            "watched_names_read_through": 82,
            "watched_drain_ran": True,
            "watched_drain_deadline_hit": False,
            "edgar_coverage": blank_edgar_coverage(),
        }
    )

    assert len(sent) == 1
    assert "NOT KNOWN" in sent[0]
    assert "0 of 0" not in sent[0]


def test_more_readable_filings_than_edgar_reported_is_named():
    """A coverage figure that reads better than complete is not a coverage
    figure. Contrived rather than observed, but a ratio above 1.0 with
    nothing attached to explain it is exactly the kind of number this desk
    ends up arguing about six weeks later."""
    coverage = edgar_coverage(
        {
            "edgar_total": 10,
            "edgar_enumerated": 12,
            "edgar_rows_received": 12,
            "edgar_days_queried": 1,
            "edgar_days_in_window": 1,
            "edgar_days_with_total": 1,
            "edgar_coverage_reasons": [],
        }
    )

    assert coverage["ratio"] == 1.2
    assert coverage["verified"] is False
    assert "edgar_enumerated_above_total" in coverage["reasons"]


# ---------------------------------------------------------------------------
# completed past days are not re-paged
# ---------------------------------------------------------------------------

ZZZ_CIK = "9990001"
ZZZ_LISTED = {ZZZ_CIK: {"ZZZA": "Nasdaq"}}


def _counting_scan(provider, monkeypatch, by_page, processed):
    """One `_discover` run that records which days were requested."""
    requested: list[str] = []
    inner = _bodies(by_page)

    def _get(url, *, params, deadline):
        requested.append(str(params["startdt"]))
        return inner(url, params=params, deadline=deadline)

    monkeypatch.setattr(provider, "_get", _get)
    stats: dict = {}
    found = provider._discover(ZZZ_LISTED, float("inf"), processed, set(), stats)
    return found, stats, requested


def _zzz_pages():
    today = et_today()
    yesterday = (today - timedelta(days=1)).isoformat()
    hits = [_hit(_accession(i), cik=ZZZ_CIK) for i in range(1, 4)]
    page = {"hits": {"hits": hits, "total": {"value": 3}}}
    return today.isoformat(), yesterday, {(today.isoformat(), 0): page, (yesterday, 0): page}


def test_a_completed_past_day_is_not_requested_again(tmp_path, monkeypatch):
    provider = _provider(tmp_path, lookback_days=1, max_filings_per_refresh=50)
    today, yesterday, pages = _zzz_pages()
    found, _s, first = _counting_scan(provider, monkeypatch, pages, set())
    assert yesterday in first
    processed = {f["accession"] for f in found}
    assert len(processed) == 3

    _f, _s, second = _counting_scan(provider, monkeypatch, pages, processed)
    assert yesterday not in second
    assert today in second


def test_a_day_with_an_unread_accession_is_not_marked_complete(tmp_path, monkeypatch):
    provider = _provider(tmp_path, lookback_days=1, max_filings_per_refresh=50)
    _today, yesterday, pages = _zzz_pages()
    found, _s, _r = _counting_scan(provider, monkeypatch, pages, set())
    processed = {f["accession"] for f in found}
    processed.discard(_accession(2))  # one filing never got read

    _f, _s, second = _counting_scan(provider, monkeypatch, pages, processed)
    assert yesterday in second  # still re-paged, so the unread one is found
    processed.add(_accession(2))
    _f, _s, third = _counting_scan(provider, monkeypatch, pages, processed)
    assert yesterday not in third  # and once read, the day is done


def test_today_is_never_marked_complete(tmp_path, monkeypatch):
    provider = _provider(tmp_path, lookback_days=1, max_filings_per_refresh=50)
    today, yesterday, pages = _zzz_pages()
    found, _s, _r = _counting_scan(provider, monkeypatch, pages, set())
    processed = {f["accession"] for f in found}
    _counting_scan(provider, monkeypatch, pages, processed)

    stored = provider.stores.load_completed_days()
    assert yesterday in stored
    assert today not in stored


def test_a_skipped_completed_day_reports_the_same_coverage_as_a_read_one(tmp_path, monkeypatch):
    provider = _provider(tmp_path, lookback_days=1, max_filings_per_refresh=50)
    _today, yesterday, pages = _zzz_pages()
    found, first_stats, _r = _counting_scan(provider, monkeypatch, pages, set())
    processed = {f["accession"] for f in found}
    _f, second_stats, second = _counting_scan(provider, monkeypatch, pages, processed)

    assert yesterday not in second
    for key in (
        "edgar_total",
        "edgar_enumerated",
        "edgar_rows_received",
        "edgar_days_queried",
        "edgar_days_with_total",
        "edgar_days_in_window",
    ):
        assert second_stats[key] == first_stats[key], key
    assert edgar_coverage(second_stats) == edgar_coverage(first_stats)
    assert "days_not_queried" not in second_stats["edgar_coverage_reasons"]
