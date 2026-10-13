"""Fund category store and name backstop (src/data/fund_category.py)."""

from datetime import date

import pytest

from src.data.fund_category import FundCategoryStore, is_leveraged_fund_name


def test_store_keeps_one_row_per_symbol_across_connections(tmp_path):
    path = tmp_path / "desk.db"
    store = FundCategoryStore(path)
    assert store.get("SPY") is None
    store.put("SPY", "  Large Blend ", date(2026, 10, 13))
    store.put("SPY", "Large Growth", date(2026, 10, 14), source="yahoo")
    store.close()
    kept = FundCategoryStore(path).get("SPY")
    assert (kept.symbol, kept.category, kept.fetched_at, kept.source) == (
        "SPY",
        "Large Growth",
        date(2026, 10, 14),
        "yahoo",
    )


def test_store_never_keeps_a_blank_category(tmp_path):
    with pytest.raises(ValueError):
        FundCategoryStore(tmp_path / "desk.db").put("SPY", "   ", date(2026, 10, 13))


def test_no_database_means_every_read_misses(tmp_path):
    store = FundCategoryStore(None)
    store.put("SPY", "Large Blend", date(2026, 10, 13))
    assert store.get("SPY") is None


@pytest.mark.parametrize(
    "name",
    [
        "ProShares UltraPro QQQ",
        "ProShares Ultra S&P500",
        "ProShares UltraShort QQQ",
        "Direxion Daily Semiconductor Bull 3X Shares",
        "Direxion Daily Financial Bear 3X Shares",
        "ProShares Short S&P500 -1X",
        "MicroSectors FANG+ Index 2X Leveraged ETN",
        "T-Rex 2X Long Tesla Daily Target ETF",
        "Tuttle Capital Daily Inverse Cramer ETF",
        "AXS 1.25X NVDA Bear Daily ETF",
    ],
)
def test_leveraged_or_inverse_names_are_refused(name):
    assert is_leveraged_fund_name(name)


@pytest.mark.parametrize(
    "name",
    ["SPDR S&P 500 ETF Trust", "Invesco QQQ Trust", "iShares Russell 2000 ETF", "Daily Journal Corp", "", None],
)
def test_plain_names_are_not_refused(name):
    assert not is_leveraged_fund_name(name)
