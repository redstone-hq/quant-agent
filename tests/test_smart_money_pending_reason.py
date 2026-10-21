"""A shortlisted name with Form 4 filings still queued is not told 'no filings'."""

from src.data.smart_money_summary import NO_FILINGS_REASON, PENDING_REASON, no_data_reasons


def test_pending_name_gets_pending_reason():
    out = no_data_reasons([], ["ZZZP", "ZZZE"], unread_symbols=["zzzp"])
    assert out == {"ZZZP": PENDING_REASON, "ZZZE": NO_FILINGS_REASON}
    assert "not yet read" in PENDING_REASON


def test_truly_empty_name_unchanged():
    assert no_data_reasons([], ["ZZZE"]) == {"ZZZE": NO_FILINGS_REASON}
    assert no_data_reasons([], ["ZZZE"], unread_symbols=[]) == {"ZZZE": NO_FILINGS_REASON}


def test_provider_error_wins():
    out = no_data_reasons([], ["ZZZP"], provider_error="boom", unread_symbols=["ZZZP"])
    assert out["ZZZP"] == "smart-money fetch failed: boom"
