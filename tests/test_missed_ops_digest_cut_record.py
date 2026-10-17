"""Record-only: missed_ops_digest_cut_record logs how much each made-up cut removed.

Governs nothing -- the rendered digest must be identical with the recording on or off.
"""

import logging
from unittest.mock import patch

from tests.test_missed_opportunities import _pipeline_with, _mk_ohlcv

LOG = "src.pipeline"


def _build():
    # Six top movers (ZZZM1..6) requested with count 4; two are thin (ZZZM5, ZZZM6).
    closes = {f"ZZZM{i}": [100, 105, 110, 115, 120, 120 + i] for i in range(1, 7)}
    closes["ZZZU1"] = [100, 103, 106, 109, 112, 115]
    p = _pipeline_with(
        universe=["ZZZU1"],
        top_movers=[{"symbol": f"ZZZM{i}"} for i in range(1, 7)],
        market_closes_by_symbol=closes,
    )

    def _ohlcv(symbol, lookback_days=10):
        vol = 1000 if symbol in ("ZZZM5", "ZZZM6") else 5_000_000
        return _mk_ohlcv([(symbol, c) for c in closes[symbol]], volume=vol)

    p.market.get_ohlcv.side_effect = _ohlcv
    return p._build_missed_opportunities_digest(lookback_days=5, move_threshold_pct=8.0, top_n=3, top_movers_count=4)


@patch("src.execution.broker._get_sector", return_value="Technology")
def test_cut_record_counts(_sec, caplog):
    with caplog.at_level(logging.INFO, logger=LOG):
        out = _build()
    lines = [r.getMessage() for r in caplog.records if "missed_ops_digest_cut_record" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert "top_movers_count=4" in line
    assert "candidates=7" in line
    assert "dollar_volume_cut=2" in line
    assert "ZZZM5" in line and "ZZZM6" in line
    assert "before_top_n=5" in line
    assert "top_n=3 kept=3 top_n_cut=2" in line
    assert len(out) == 3


@patch("src.execution.broker._get_sector", return_value="Technology")
def test_cut_record_governs_nothing(_sec, caplog):
    with caplog.at_level(logging.INFO, logger=LOG):
        with_rec = repr(_build())
    logging.disable(logging.CRITICAL)
    try:
        without_rec = repr(_build())
    finally:
        logging.disable(logging.NOTSET)
    assert with_rec == without_rec
