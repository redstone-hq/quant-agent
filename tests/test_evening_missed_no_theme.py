from src.agents.evening_analyst import EveningAnalystAgent
from src.models.evening import MissedOpportunity

ROW = {
    "symbol": "CBRS",
    "move_pct": 12.0,
    "miss_category": "value_entry_missed",
    "lesson": "Value entry passed with no clear theme.",
}


def test_value_entry_missed_without_theme_validates():
    assert not (MissedOpportunity(**ROW).theme_if_any or "").strip()


def test_row_without_theme_survives_drop_filter():
    out = EveningAnalystAgent._drop_invalid_missed_opportunities(
        {"missed_opportunities": [dict(ROW), dict(ROW, symbol="ONDS", theme_if_any="")]}
    )
    assert [r["symbol"] for r in out["missed_opportunities"]] == ["CBRS", "ONDS"]
