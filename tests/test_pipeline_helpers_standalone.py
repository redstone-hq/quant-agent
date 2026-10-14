"""Three of the four helpers that lived only on `TradingPipeline` are usable
WITHOUT building a pipeline (conversion step 8, 2026-10-01).

`_sweeper` moved last (conversion step 9) as
`src.execution.cash_sweep.sweeper_or_none`, once its broad except recorded
the failure durably instead of swallowing it.

Each test imports the helper from its real home and calls it with plain
collaborators. `src.pipeline` is deliberately never imported here: the point
is that none of these needs the giant object any more.
"""

import sys
import types
from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _pipeline_never_imported():
    before = "src.pipeline" in sys.modules
    yield
    assert ("src.pipeline" in sys.modules) == before, (
        "a helper home pulled in src.pipeline -- the extraction is not real"
    )


def test_atr_for_symbol_needs_no_pipeline():
    from src.data.technical import atr_for_symbol
    from src.models import OHLCV

    class _Market:
        def __init__(self, bars):
            self.bars = bars

        def get_ohlcv(self, symbol, days):
            return self.bars

    bars = [
        OHLCV(date=date(2026, 1, d), open=100.0, high=102.0, low=98.0, close=100.0, volume=1_000) for d in range(1, 31)
    ]
    atr = atr_for_symbol(_Market(bars), "SYN")
    assert atr is not None and atr > 0
    assert atr_for_symbol(_Market(bars[:10]), "SYN") is None  # < 15 bars
    assert atr_for_symbol(_Market(None), "SYN") is None

    class _Broken:
        def get_ohlcv(self, symbol, days):
            raise RuntimeError("provider down")

    assert atr_for_symbol(_Broken(), "SYN") is None  # never raises


def test_live_constructor_cfg_or_none_needs_no_pipeline():
    from src.risk.constants import live_constructor_cfg_or_none

    assert live_constructor_cfg_or_none(None) is None
    assert live_constructor_cfg_or_none(object()) is None
    cfg = object()
    assert live_constructor_cfg_or_none(types.SimpleNamespace(cfg=cfg)) is cfg


def test_parse_logged_agent_response_needs_no_pipeline():
    from src.agents.logged_response import parse_logged_agent_response

    row = {"full_response": '{"action": "HOLD", "n": 2}', "model": "synthetic"}
    assert parse_logged_agent_response(row) == {"action": "HOLD", "n": 2}
    # Answer text is never patched: a fenced or prose-wrapped answer is not JSON.
    fenced = {"full_response": 'Here you go:\n```json\n{"action": "HOLD", "n": 2}\n```', "model": "synthetic"}
    assert parse_logged_agent_response(fenced) is None
    assert parse_logged_agent_response({}) is None


def test_sweeper_or_none_needs_no_pipeline():
    from types import SimpleNamespace
    from src.execution.cash_sweep import CashSweeper, sweeper_or_none

    assert sweeper_or_none(None) is None
    assert sweeper_or_none(object()) is None
    off = CashSweeper(
        pipeline=SimpleNamespace(config=SimpleNamespace(cash_sweep=SimpleNamespace(enabled=False, symbol="SGOV")))
    )
    assert sweeper_or_none(off) is None
    on = CashSweeper(
        pipeline=SimpleNamespace(config=SimpleNamespace(cash_sweep=SimpleNamespace(enabled=True, symbol="SGOV")))
    )
    assert sweeper_or_none(on) is on

    # A config that cannot even be read reads as disabled AND leaves a row.
    class _BrokenConfig:
        @property
        def cash_sweep(self):
            raise RuntimeError("config exploded")

    class _Db:
        rows = []

        def insert_specialist_evidence(self, **kw):
            self.rows.append(kw)

    db = _Db()
    broken = CashSweeper(pipeline=SimpleNamespace(config=_BrokenConfig(), db=db))
    assert sweeper_or_none(broken) is None
    assert len(db.rows) == 1 and "config exploded" in db.rows[0]["evidence_json"]
