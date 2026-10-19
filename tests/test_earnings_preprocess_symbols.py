"""The pre-market earnings list uses the morning's own admission rule (19 Oct).

19 Oct 08:05 ET: the earnings pass read filings for names that fail the
desk's universe screen, because its Form-4 list was a looser copy of the
morning's rule. These tests pin the list to the morning's rule, keep every
holding on it, and check a weeks-old filing is flagged as a catch-up read.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.data.earnings import EarningsReport, is_catch_up_read
from src.pipeline_admission import AdmissionService
from src.pipeline_parts.pnl_gaps import _earnings_preprocess_symbols


def _purchase(symbol, code="P", value=100_000.0):
    return SimpleNamespace(
        symbol=symbol,
        transaction_code=code,
        admission_eligible=True,
        transaction_value_usd=value,
        known_at="2026-10-18T20:00:00Z",
        disclosure_date="2026-10-17",
        accession_number=f"acc-{symbol}",
        actor="Owner",
        signal_class_reason="opportunistic",
    )


def _pipeline(observations, *, failing=(), cap=5, positions=()):
    config = SimpleNamespace(
        trading=SimpleNamespace(universe=["ZZZC"]),
        smart_money=SimpleNamespace(enabled=True, max_external_candidates=cap),
        universe_screen=SimpleNamespace(enabled=False),
        risk=SimpleNamespace(max_target_horizon_sessions=10),
    )
    admission = AdmissionService(config=config, broker=MagicMock(), market=MagicMock(), journal=MagicMock())
    admission._form4_admission_is_current = lambda observation: True
    admission._evaluate_external_admission_gates = lambda symbol, context="": (
        (False, "screen_failed", {}) if symbol in failing else (True, None, {})
    )
    broker = MagicMock()
    broker.get_positions.return_value = list(positions)
    provider = MagicMock()
    provider.fetch.return_value = (observations, None)
    return SimpleNamespace(config=config, admission=admission, smart_money_provider=provider, broker=broker)


def test_form4_candidate_failing_the_universe_gates_is_excluded():
    pipeline = _pipeline([_purchase("ZZZA"), _purchase("ZZZB")], failing={"ZZZB"})
    assert _earnings_preprocess_symbols(pipeline) == ["ZZZC", "ZZZA"]


def test_non_purchase_code_is_excluded():
    pipeline = _pipeline([_purchase("ZZZA"), _purchase("ZZZS", code="S")])
    assert _earnings_preprocess_symbols(pipeline) == ["ZZZC", "ZZZA"]


def test_external_cap_holds():
    observations = [_purchase(s, value=v) for s, v in [("ZZZA", 3e5), ("ZZZB", 2e5), ("ZZZD", 1e5)]]
    pipeline = _pipeline(observations, cap=2)
    assert _earnings_preprocess_symbols(pipeline) == ["ZZZC", "ZZZA", "ZZZB"]


def test_held_name_below_the_floor_is_still_included():
    # No Form-4 row, fails the gates: only the holding rule can put it on the list.
    pipeline = _pipeline([], failing={"ZZZH"}, positions=[SimpleNamespace(symbol="zzzh")])
    assert _earnings_preprocess_symbols(pipeline) == ["ZZZC", "ZZZH"]


def test_seat_evidence_carries_filing_date_and_catch_up_flag():
    today = date(2026, 10, 19)  # a Monday
    assert is_catch_up_read("2026-09-08", today) is True
    assert is_catch_up_read("2026-10-16", today) is False  # Friday is overnight for Monday
    report = EarningsReport(
        symbol="ZZZA",
        form_type="10-Q",
        filing_date="2026-09-08",
        filing_path="x.html",
        analysis_path=None,
        text_excerpt="",
        is_new=True,
        catch_up_read=is_catch_up_read("2026-09-08", today),
    )
    assert report.evidence_fields() == {
        "filing_date": "2026-09-08",
        "first_read_today": True,
        "catch_up_read": True,
    }
