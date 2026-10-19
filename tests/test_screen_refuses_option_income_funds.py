"""Owner ruling 2026-10-19 (checklist q20): option-income funds are refused
by the daily screen on Yahoo's own category, "Derivative Income"."""

from __future__ import annotations

from src import universe_screen as us
from tests.test_universe_screen import GOOD_ASSET, TH
from tests.test_universe_screen_etf import _sources

FUND_ASSET = {**GOOD_ASSET, "symbol": "ZZZY", "name": "ZZZ Option Income Strategy ETF"}


def test_a_derivative_income_fund_is_refused_with_its_reason():
    profile = {"quote_type": "ETF", "category": "Derivative Income"}
    result = us.screen_symbol("ZZZY", _sources(FUND_ASSET, profile, filings=None), TH)
    assert not result.passed
    assert result.failures == ["option_income_fund"]
    assert result.failures[0] in us.PLAIN_REASON


def test_an_ordinary_stock_is_not_refused():
    asset = {**GOOD_ASSET, "symbol": "ZZZS"}
    result = us.screen_symbol("ZZZS", _sources(asset, {"quote_type": "EQUITY"}), TH)
    assert "option_income_fund" not in result.failures


def test_a_plain_index_etf_is_not_refused():
    profile = {"quote_type": "ETF", "category": "Large Blend"}
    asset = {**GOOD_ASSET, "symbol": "ZZZI", "name": "ZZZ Total Market ETF"}
    result = us.screen_symbol("ZZZI", _sources(asset, profile, filings=None), TH)
    assert result.passed, result.failures
