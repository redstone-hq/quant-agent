"""Checklist n06: the deployment-gap warning is sized by the book's own drift.

The trade-picking seat used to be warned about idle cash whenever the book was
more than a typed 1.0pp (`deployment_gap.band_pct`, copied from the deleted
T-bill sweep reserve) under the fully-invested mandate. The threshold is now
today's book's own one-session drift: the sum over held positions of
(position weight x that stock's ATR(14)/price), re-read every session from the
desk's ATR reader. The gap itself is always shown, in points.
"""

from pathlib import Path
from unittest.mock import MagicMock

from src.models import Position
from src.pipeline_context import PMFacts
from src.risk.rules import DESK_INVESTED_TARGET_PCT
from tests.pipeline_factory import build_pipeline

ROOT = Path(__file__).resolve().parents[1]

# 90% of a $100k book in one name at $100 with ATR(14) $2: drift = 90 x 2/100 = 1.8pp.
HELD = [
    Position(
        symbol="NVDA",
        qty=900,
        avg_entry=95,
        current_price=100,
        market_value=90_000,
        unrealized_pnl=4_500,
        unrealized_intraday_pnl=0.0,
        sector="Technology",
    )
]


def _facts(gap_pp, drift_pp):
    f = PMFacts()
    f.invested_pct = DESK_INVESTED_TARGET_PCT + gap_pp
    f.invested_target_pct = DESK_INVESTED_TARGET_PCT
    f.deployment_gap_pp = gap_pp
    f.deployment_drift_pp = drift_pp
    return f.render()


def test_gap_below_measured_drift_shows_gap_without_warning():
    out = _facts(-1.2, 1.8)
    assert "gap -1.2pp" in out
    assert "1.8pp" in out
    assert "DEPLOYMENT GAP" not in out


def test_gap_above_measured_drift_warns():
    out = _facts(-2.5, 1.8)
    assert "DEPLOYMENT GAP" in out
    assert "2.5pp UNDER" in out
    assert "1.8pp" in out
    assert "THIS session" in out


def test_no_atr_says_drift_unmeasured_and_does_not_warn():
    out = _facts(-40.0, None)
    assert "gap -40.0pp" in out
    assert "unmeasured" in out
    assert "DEPLOYMENT GAP" not in out


def test_drag_sentence_is_gone():
    assert "largest P&L drag" not in _facts(-40.0, 1.0)
    assert "largest P&L drag" not in (ROOT / "src/pipeline_pm_facts.py").read_text()


def test_band_pct_no_longer_exists():
    import src.config as config_module
    import src.risk.rules as rules

    assert not hasattr(config_module, "DeploymentGapConfig")
    assert "deployment_gap" not in config_module.AppConfig.model_fields
    assert not hasattr(rules, "deployment_gap_band_pct")
    assert "band_pct" not in (ROOT / "config/settings.yaml").read_text()


def _facts_pipeline(atr):
    pipeline = build_pipeline(db=MagicMock(), _atr_for_symbol=atr)
    pipeline.db.compute_trade_calibration.return_value = {}
    pipeline.db.get_recent_agent_outputs.return_value = []
    pipeline._build_position_history = MagicMock(return_value={})
    return pipeline


def test_pm_facts_measure_drift_from_the_atr_reader():
    f = _facts_pipeline(lambda symbol: 2.0)._build_pm_facts(
        positions=HELD, analyses=[], total_value=100_000.0, cash=10_000.0, recent_performance={}
    )
    assert abs(f.deployment_drift_pp - 1.8) < 1e-9
    assert "DEPLOYMENT GAP" in f.render()  # 10pp gap > 1.8pp drift


def test_pm_facts_without_atr_leave_drift_unmeasured():
    f = _facts_pipeline(lambda symbol: None)._build_pm_facts(
        positions=HELD, analyses=[], total_value=100_000.0, cash=10_000.0, recent_performance={}
    )
    assert f.deployment_drift_pp is None
    assert "unmeasured" in f.render()


def _gate_rules(atr, market_value):
    pipeline = build_pipeline(
        _sweeper=lambda: None,
        risk_engine=MagicMock(check=MagicMock(return_value=[])),
        _atr_for_symbol=atr,
    )
    held = [HELD[0].model_copy(update={"market_value": market_value})]
    _, violations, _ = pipeline.risk_gate._filter_hard_risk_decisions(
        [],
        held,
        total_value=100_000.0,
        invested_target_pct=DESK_INVESTED_TARGET_PCT,
        cash=0.0,
    )
    return [v for v in violations if v.rule == "deployment_gap"]


def test_risk_gate_quiet_inside_drift_and_warns_outside():
    # 99% invested: drift 99 x 2/100 = 1.98pp > 1pp gap -> quiet.
    assert not _gate_rules(lambda symbol: 2.0, 99_000)
    # 90% invested: drift 1.8pp < 10pp gap -> advisory.
    fired = _gate_rules(lambda symbol: 2.0, 90_000)
    assert fired and "1.8pp" in fired[0].message


def test_risk_gate_withholds_advisory_when_drift_unmeasured():
    assert not _gate_rules(lambda symbol: None, 50_000)
