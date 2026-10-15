"""The news seat is asked for per-stock rows; stored reports keep the maps."""

from src.agents.llm_schema import _has_free_form_map, _response_format_for
from src.models.news import NewsIntelligenceReport, StateChange
from tests.test_news import _valid_news_json

_ITEM = {"headline": "h", "sentiment": "bullish", "conviction": "high", "impact_summary": "s"}
_STATE = {
    "event": "e",
    "previous_state": "a",
    "new_state": "b",
    "market_impact": "m",
    "affected_symbols": ["XOM", "COST"],
    "conviction": "high",
}


def _base(**over):
    base = {k: v for k, v in _valid_news_json().items() if k not in ("stock_news", "state_changes")}
    return {**base, **over}


def test_rows_convert_to_the_maps():
    directions = [{"symbol": "XOM", "direction": "bearish"}, {"symbol": "COST", "direction": "bullish"}]
    report = NewsIntelligenceReport(
        **_base(
            stock_news=[{"symbol": "NVDA", "items": [_ITEM]}, {"symbol": "GOOGL", "items": []}],
            state_changes=[{**_STATE, "symbol_direction": directions}],
        )
    )
    assert list(report.stock_news) == ["NVDA", "GOOGL"]
    assert report.stock_news["NVDA"][0].headline == "h"
    assert report.stock_news["GOOGL"] == []
    assert report.state_changes[0].symbol_direction == {"XOM": "bearish", "COST": "bullish"}


def test_the_old_map_shape_still_loads():
    report = NewsIntelligenceReport(
        **_base(
            stock_news={"NVDA": [_ITEM]},
            state_changes=[{**_STATE, "symbol_direction": {"XOM": "bearish"}}],
        )
    )
    assert report.stock_news["NVDA"][0].sentiment == "bullish"
    assert report.state_changes[0].symbol_direction == {"XOM": "bearish"}
    again = NewsIntelligenceReport.model_validate_json(report.model_dump_json())
    assert list(again.stock_news) == ["NVDA"]


def test_a_strict_schema_with_rows_is_sent():
    fmt = _response_format_for(NewsIntelligenceReport)["json_schema"]
    assert fmt["strict"] is True
    assert not _has_free_form_map(fmt["schema"])
    assert fmt["schema"]["properties"]["stock_news"]["type"] == "array"
    assert StateChange.model_json_schema()["properties"]["symbol_direction"]["type"] == "array"
