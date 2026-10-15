"""The made-up six-per-run nomination cap is gone.

Total AI spend is bounded per call by the daily cost circuit
(`BaseAgent` calls `begin_call`, which enforces `daily_cost_limit_usd`),
so no separate count cap is needed; the per-seat cap stays.
"""

from src.config import NominationConfig
from src.models import Nomination
from src.nominations import select_nominations


def _nom(symbol: str) -> Nomination:
    return Nomination(symbol=symbol, conviction="high", observation=f"{symbol} obs")


def test_more_than_six_qualifying_nominations_all_pass_through():
    by_seat = {
        "news_analyst": [_nom("AAA"), _nom("BBB"), _nom("CCC")],
        "macro_analyst": [_nom("DDD"), _nom("EEE"), _nom("FFF")],
        "earnings_analyst": [_nom("GGG"), _nom("HHH")],
    }
    out = select_nominations(by_seat, max_per_seat=3)
    assert sorted(c.symbol for c in out) == ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]


def test_run_cap_field_no_longer_exists_but_per_seat_cap_does():
    assert "max_total_per_run" not in NominationConfig.model_fields
    assert "max_per_seat_per_run" in NominationConfig.model_fields
