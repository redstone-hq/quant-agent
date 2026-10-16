"""Per-filing insider ratios must not borrow aggregate-study outcomes."""

from datetime import date

from src.data.insider_signal import InsiderHistory, InsiderPriorTrade, classify_transaction
from src.models import SmartMoneyObservation


def _row(direction: str, shares: float, post_shares: float) -> SmartMoneyObservation:
    transaction_date = date(2026, 8, 20)
    return SmartMoneyObservation(
        symbol="NVDA",
        stream="insider",
        actor="Owner 1",
        actor_cik="1",
        actor_roles=["officer", "Chief Financial Officer"],
        direction=direction,
        transaction_date=transaction_date,
        disclosure_date=transaction_date,
        source_url="https://www.sec.gov/Archives/placeholder.txt",
        filing_form="4",
        transaction_code="P" if direction == "buy" else "S",
        shares=shares,
        price_per_share=100.0,
        transaction_value_usd=shares * 100.0,
        post_transaction_shares=post_shares,
        lag_days=0,
        disclosure_age_days=0,
        freshness="fresh",
        economic_role="confirmatory",
    )


def _established() -> InsiderHistory:
    """A trade in each of the 3 preceding calendar years, none in August: classifiable."""
    trades = [InsiderPriorTrade(transaction_date=date(year, 1, 9), direction="buy") for year in (2023, 2024, 2025)]
    return InsiderHistory({("1", "NVDA"): trades})


def test_per_filing_detail_does_not_borrow_aggregate_study_outcomes():
    """One Form 4 row is not Scott/Xu's six-month stock-wide aggregate."""

    def detail_for(direction: str, shares: float, post: float) -> str:
        return classify_transaction(_row(direction, shares, post), _established()).detail

    details = [
        detail_for("sell", 9_999.0, 90_001.0),
        detail_for("sell", 30_000.0, 70_000.0),
        detail_for("sell", 60_000.0, 40_000.0),
        detail_for("buy", 5_000.0, 5_000.0),
    ]
    assert "10.0%" in details[0]
    assert "30.0%" in details[1]
    assert "60.0%" in details[2]
    for detail in details:
        assert "Scott & Xu" not in detail
        assert "quarterly excess return" not in detail
        assert "BULLISH" not in detail
        assert "predicts negative" not in detail
