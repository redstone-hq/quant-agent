"""The max_observations cut is recorded: every dropped row, its class, and
whether its symbol survived. Record-only -- selection is unchanged."""

from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace

from src.data.smart_money_cluster import (
    cut_record,
    find_cluster_census,
    reserve_cluster_symbols,
)
from src.models import SmartMoneyObservation

CAP = 40


def _row(symbol: str, value: float, *, admit=False, core=False, direction="buy") -> SmartMoneyObservation:
    return SmartMoneyObservation(
        symbol=symbol,
        stream="insider",
        actor=f"Owner {symbol}",
        actor_cik=symbol,
        direction=direction,
        transaction_date=date(2026, 10, 1),
        disclosure_date=date(2026, 10, 2),
        source_url="https://www.sec.gov/x.txt",
        accession_number=f"acc-{symbol}-{value}",
        filing_form="4",
        transaction_code="P",
        transaction_row=0,
        shares=value / 100,
        price_per_share=100,
        transaction_value_usd=value,
        lag_days=1,
        disclosure_age_days=0,
        freshness="fresh",
        economic_role="confirmatory",
        transient_admission_eligible=admit,
        in_core_universe=core,
    )


def _rows(n: int) -> list[SmartMoneyObservation]:
    return [_row(f"ZZ{i:03d}", 1_000_000 - i, admit=i < 5, core=5 <= i < 10) for i in range(n)]


def test_over_cap_lists_exactly_cut_rows_with_classes():
    ordered = _rows(45)
    final = reserve_cluster_symbols(ordered, set(), max_observations=CAP, max_reserved_slots=3)
    assert final == ordered[:CAP]  # selection unchanged
    rec = cut_record(ordered, final, stream="insider", max_observations=CAP)
    assert [r["symbol"] for r in rec["cut"]] == [f"ZZ{i:03d}" for i in range(40, 45)]
    assert rec["rows_cut"] == 5 and rec["by_class"] == {"other": 5}
    assert all(r["symbol_kept"] is False and r["stream"] == "insider" and r["rows_cut"] == 1 for r in rec["cut"])
    assert rec["symbols_fully_cut"] == [f"ZZ{i:03d}" for i in range(40, 45)]


def test_classes_and_symbol_kept_flag():
    ordered = _rows(40) + [
        _row("ZZ000", 5.0, direction="sell"),
        _row("ZZA", 4.0, admit=True),
        _row("ZZB", 3.0, core=True),
    ]
    rec = cut_record(ordered, ordered[:CAP], stream="insider", max_observations=CAP)
    by_sym = {r["symbol"]: r for r in rec["cut"]}
    assert by_sym["ZZ000"]["symbol_kept"] is True and by_sym["ZZ000"]["direction"] == "sell"
    assert by_sym["ZZ000"]["class"] == "other"
    assert by_sym["ZZA"]["class"] == "admission-qualifying" and by_sym["ZZA"]["symbol_kept"] is False
    assert by_sym["ZZB"]["class"] == "core-universe" and by_sym["ZZB"]["value_usd_sum"] == 3.0
    assert rec["by_class"] == {"other": 1, "admission-qualifying": 1, "core-universe": 1}


def test_cut_is_grouped_per_symbol_direction_class():
    ordered = _rows(40) + [_row("ZZX", 10.0), _row("ZZX", 20.0), _row("ZZX", 5.0, direction="sell")]
    rec = cut_record(ordered, ordered[:CAP], stream="insider", max_observations=CAP)
    assert rec["rows_cut"] == 3 and len(rec["cut"]) == 2
    buy = next(r for r in rec["cut"] if r["direction"] == "buy")
    assert buy["rows_cut"] == 2 and buy["value_usd_sum"] == 30.0
    assert rec["symbols_fully_cut"] == ["ZZX"]


def test_reserved_row_is_not_reported_cut():
    ordered = _rows(41)
    final = reserve_cluster_symbols(ordered, {"ZZ040"}, max_observations=CAP, max_reserved_slots=3)
    rec = cut_record(ordered, final, stream="insider", max_observations=CAP)
    assert [r["symbol"] for r in rec["cut"]] == ["ZZ039"]


def test_at_or_under_cap_is_empty(caplog):
    ordered = _rows(40)
    with caplog.at_level(logging.INFO, logger="src.data.smart_money_cluster"):
        rec = cut_record(ordered, ordered[:CAP], stream="congress", max_observations=CAP)
    assert rec["cut"] == [] and rec["rows_cut"] == 0 and rec["by_class"] == {}
    assert any("smart_money_cut_record stream=congress" in m for m in caplog.messages)


def test_record_rides_with_persisted_census():
    ordered = _rows(42)
    rec = cut_record(ordered, ordered[:CAP], stream="congress", max_observations=CAP)
    provider = SimpleNamespace(
        providers=[
            SimpleNamespace(last_cluster_census={"clusters_found": 0}, last_cut_record=None),
            SimpleNamespace(last_cut_record=rec),
        ]
    )
    census = find_cluster_census(provider)
    assert census["clusters_found"] == 0
    assert census["cut_records"]["congress"]["rows_cut"] == 2
    assert find_cluster_census(SimpleNamespace()) is None
