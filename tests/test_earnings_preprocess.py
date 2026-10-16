"""run_earnings_preprocess — Phase 4 #6 pre-market earnings mode."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.agents.base import AgentResult
from src.cost_circuit import PaidAnalysisSuspended
from src.data.earnings import EarningsReport
from src.storage.db import Database
from tests.pipeline_factory import build_pipeline


def _mk_pipeline(tmp_path, earnings_provider, earnings_analyst):
    pipeline = build_pipeline(db=Database(str(tmp_path / "t.db")))
    pipeline.db.initialize()
    pipeline.broker = MagicMock()
    pipeline.broker.is_trading_day.return_value = True
    pipeline.earnings_provider = earnings_provider
    pipeline.earnings_analyst = earnings_analyst
    pipeline.config = MagicMock()
    pipeline.config.trading.universe = ["NVDA", "AAPL"]
    pipeline.config.llm.earnings_analyst_model = "test-model"
    return pipeline


def test_preprocess_analyzes_new_filings_synchronously(tmp_path):
    """Fresh filings → full LLM analysis → confirm. No background thread."""
    new_filing = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda.html",
        analysis_path="/tmp/nvda.md",
        text_excerpt="...",
        is_new=True,
    )
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = [new_filing]
    earnings_provider.confirm_filing.return_value = None

    earnings_analyst = MagicMock()
    agent_result = AgentResult(raw_text="{}", tokens_used=50, model="test", user_message="x")
    earnings_analyst.analyze_reports.return_value = [
        {
            "symbol": "NVDA",
            "is_new": True,
            "form_type": "10-Q",
            "filing_date": "2026-04-20",
            "agent_result": agent_result,
            "analysis": {"investment_implications": {"sentiment": "bullish", "conviction": "high"}},
        }
    ]

    pipeline = _mk_pipeline(tmp_path, earnings_provider, earnings_analyst)
    result = pipeline.run_earnings_preprocess()

    assert result["status"] == "preprocessed"
    assert result["analyzed"] == 1
    assert result["confirmed"] == 1

    earnings_provider.check_and_fetch.assert_called_once()
    earnings_analyst.analyze_reports.assert_called_once_with([new_filing])
    earnings_provider.confirm_filing.assert_called_once_with(new_filing)


def test_preprocess_returns_nothing_new_when_no_filings(tmp_path):
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = []  # nothing new
    earnings_analyst = MagicMock()

    pipeline = _mk_pipeline(tmp_path, earnings_provider, earnings_analyst)
    result = pipeline.run_earnings_preprocess()

    assert result["status"] == "nothing_new"
    assert result["count"] == 0
    earnings_analyst.analyze_reports.assert_not_called()


def test_preprocess_unions_form4_hot_names_without_an_invented_cap(tmp_path):
    """2026-09-16: 08:00 preprocess returned nothing_new against the
    configured universe while FTK/RSG were already Form-4 hot. Morning
    then saw those filings as placeholders. Union the provider's own
    admission_eligible names — no new 'preprocess N names' constant."""
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = []
    pipeline = _mk_pipeline(tmp_path, earnings_provider, MagicMock())
    pipeline.config.smart_money.enabled = True
    pipeline.smart_money_provider = MagicMock()
    pipeline.smart_money_provider.fetch.return_value = (
        [
            SimpleNamespace(admission_eligible=True, symbol="FTK"),
            SimpleNamespace(admission_eligible=True, symbol="RSG"),
            SimpleNamespace(admission_eligible=False, symbol="ZZZ"),
            SimpleNamespace(admission_eligible=True, symbol="nvda"),
        ],
        None,
    )
    pipeline.smart_money_provider.refresh.return_value = {"status": "ok"}
    symbols = pipeline._earnings_preprocess_symbols()
    assert symbols == ["NVDA", "AAPL", "FTK", "RSG"]
    result = pipeline.run_earnings_preprocess()
    assert result["status"] == "nothing_new"
    called = earnings_provider.check_and_fetch.call_args[0][0]
    assert called == ["NVDA", "AAPL", "FTK", "RSG"]


def test_prelatched_preprocess_fetches_filing_but_never_marks_it_failed(tmp_path):
    new_filing = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-08-25",
        filing_path="/tmp/nvda.html",
        analysis_path="/tmp/nvda.md",
        text_excerpt="...",
        is_new=True,
    )
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = [new_filing]
    earnings_analyst = MagicMock()
    pipeline = _mk_pipeline(tmp_path, earnings_provider, earnings_analyst)
    pipeline._drain_pending_protection_restores = MagicMock()
    pipeline._reconcile_orphan_pending_submits = MagicMock()
    pipeline.cost_circuit = MagicMock()
    pipeline.cost_circuit.activate_session.return_value = {"suspended": True}
    pipeline.cost_circuit.require_paid_analysis.side_effect = PaidAnalysisSuspended(
        "prelatched",
        {"suspended": True},
    )

    result = pipeline.run_earnings_preprocess()

    assert result["status"] == "paid_analysis_suspended"
    pipeline._drain_pending_protection_restores.assert_called_once()
    pipeline._reconcile_orphan_pending_submits.assert_called_once()
    earnings_provider.check_and_fetch.assert_called_once()
    earnings_analyst.analyze_reports.assert_not_called()
    earnings_provider.record_failure.assert_not_called()
    earnings_provider.confirm_filing.assert_not_called()
    # 2026-09-24: the backlog computed just before the suspended circuit
    # call must survive onto the suspended payload — dropping it made a
    # real backlog of new filings render as "nothing happened".
    assert result["filings_waiting_count"] == 1
    assert result["filings_waiting"][0]["symbol"] == "NVDA"
    assert result["filings_waiting"][0]["outcome"] == "waiting"


def test_preprocess_skips_when_market_closed(tmp_path):
    pipeline = build_pipeline(
        db=MagicMock(), broker=MagicMock(), earnings_provider=MagicMock(), earnings_analyst=MagicMock()
    )
    pipeline.broker.is_trading_day.return_value = False
    pipeline.config = MagicMock()

    result = pipeline.run_earnings_preprocess()
    assert result["status"] == "market_holiday"
    pipeline.earnings_provider.check_and_fetch.assert_not_called()


def test_record_failure_abandons_after_max_attempts_with_et_timestamp(tmp_path):
    """When a filing's LLM analysis fails 3 times, it is marked abandoned with
    an ET-tzaware ISO timestamp (not naive UTC). Every other day/session key
    in the system is ET — drifting to UTC on this one field desyncs
    operator-facing logs from trading-day reality."""
    from src.data.earnings import EarningsDataProvider, EarningsReport

    provider = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    report = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda.html",
        analysis_path=None,
        text_excerpt="...",
        is_new=True,
    )

    # Two failures — not yet abandoned.
    assert provider.record_failure(report, max_attempts=3) is False
    assert provider.record_failure(report, max_attempts=3) is False
    entry = provider.manifest["NVDA_10-Q"]
    assert entry["failed_attempts"] == 2
    assert entry.get("abandoned") is not True

    # Third failure — abandoned + timestamped.
    assert provider.record_failure(report, max_attempts=3) is True
    entry = provider.manifest["NVDA_10-Q"]
    assert entry["abandoned"] is True
    assert "abandoned_at" in entry

    # Timestamp must be ET-aware — parse-round-trip and confirm tz offset.
    from datetime import datetime as _dt
    from src.trading_calendar import ET

    parsed = _dt.fromisoformat(entry["abandoned_at"])
    assert parsed.tzinfo is not None, "abandoned_at must carry a timezone; naive utcnow drifts from ET day keys"
    # The offset must equal ET's offset at that same instant (ET shifts DST —
    # compare offsets at the exact same moment rather than asserting a fixed
    # number of hours).
    et_offset_at_that_instant = parsed.astimezone(ET).utcoffset()
    assert parsed.utcoffset() == et_offset_at_that_instant


def test_record_failure_resets_retry_budget_when_filing_date_changes(tmp_path):
    """Codex r11 P2: the manifest is keyed by symbol+form_type, but a single
    key spans every quarter's 10-Q. Without a filing_date check, Q1's 3
    failures (abandoned) leave failed_attempts=3 / abandoned=True in the
    entry. When Q2 lands and its first failure runs record_failure, the
    code reads attempts=3 → +1 = 4 → abandoned again immediately.

    Pin: when prior_filing_date differs from incoming, reset
    failed_attempts to 0 and clear abandoned/abandoned_at. Q2 then gets
    its full 3-attempt budget."""
    from src.data.earnings import EarningsDataProvider, EarningsReport

    provider = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    q1_report = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-01-20",
        filing_path="/tmp/nvda_q1.html",
        analysis_path=None,
        text_excerpt="...",
        is_new=True,
    )
    # Burn through Q1 retry budget: 3 failures → abandoned.
    for _ in range(3):
        provider.record_failure(q1_report, max_attempts=3)
    entry = provider.manifest["NVDA_10-Q"]
    assert entry["failed_attempts"] == 3
    assert entry["abandoned"] is True

    # Q2 lands on the same key with a NEW filing_date. The first failure
    # must NOT inherit Q1's abandoned state — it should start fresh.
    q2_report = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda_q2.html",
        analysis_path=None,
        text_excerpt="...",
        is_new=True,
    )
    abandoned = provider.record_failure(q2_report, max_attempts=3)

    assert abandoned is False, "Q2's first failure must NOT abandon — that's Q1's history incorrectly carried forward"
    entry = provider.manifest["NVDA_10-Q"]
    assert entry["filing_date"] == "2026-04-20"
    assert entry["failed_attempts"] == 1, f"Q2 should be on attempt 1 of its own budget; got {entry['failed_attempts']}"
    assert entry.get("abandoned") is not True
    assert "abandoned_at" not in entry


def test_record_failure_does_not_reset_within_same_filing_date(tmp_path):
    """Sanity: the reset only fires across filing_dates. Multiple failures
    on the SAME filing_date must accumulate normally."""
    from src.data.earnings import EarningsDataProvider, EarningsReport

    provider = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    report = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda.html",
        analysis_path=None,
        text_excerpt="...",
        is_new=True,
    )
    provider.record_failure(report, max_attempts=3)
    provider.record_failure(report, max_attempts=3)
    entry = provider.manifest["NVDA_10-Q"]
    assert entry["failed_attempts"] == 2  # NOT reset


def test_preprocess_records_failures_on_llm_error(tmp_path):
    """If analyze_reports raises, each new filing gets record_failure called."""
    new_filing = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda.html",
        analysis_path="/tmp/nvda.md",
        text_excerpt="...",
        is_new=True,
    )
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = [new_filing]
    earnings_analyst = MagicMock()
    earnings_analyst.analyze_reports.side_effect = RuntimeError("rate limit")

    pipeline = _mk_pipeline(tmp_path, earnings_provider, earnings_analyst)
    result = pipeline.run_earnings_preprocess()

    assert result["status"] == "analysis_error"
    earnings_provider.record_failure.assert_called_once_with(new_filing)


def test_preprocess_records_per_filing_validation_failures(tmp_path):
    """A silently dropped filing still consumes retry budget and is not confirmed."""
    good = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda.html",
        analysis_path="/tmp/nvda.md",
        text_excerpt="...",
        is_new=True,
    )
    bad = EarningsReport(
        symbol="AAPL",
        form_type="10-K",
        filing_date="2026-04-20",
        filing_path="/tmp/aapl.html",
        analysis_path="/tmp/aapl.md",
        text_excerpt="...",
        is_new=True,
    )
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = [good, bad]

    earnings_analyst = MagicMock()
    agent_result = AgentResult(raw_text="{}", tokens_used=50, model="test", user_message="x")
    earnings_analyst.analyze_reports.return_value = [
        {
            "symbol": "NVDA",
            "is_new": True,
            "form_type": "10-Q",
            "filing_date": "2026-04-20",
            "agent_result": agent_result,
            "analysis": {"investment_implications": {"sentiment": "bullish", "conviction": "high"}},
        }
    ]

    pipeline = _mk_pipeline(tmp_path, earnings_provider, earnings_analyst)
    result = pipeline.run_earnings_preprocess()

    assert result["status"] == "preprocessed"
    assert result["analyzed"] == 1
    assert result["confirmed"] == 1
    assert result["failed"] == 1
    earnings_provider.confirm_filing.assert_called_once_with(good)
    earnings_provider.record_failure.assert_called_once_with(bad)


def test_preprocess_keys_results_by_symbol_form_filing_date_not_just_symbol(tmp_path):
    """Same-symbol multiple-form-type-day is rare but real (10-Q + 10-K can
    land the same fiscal-year-end day for some issuers). With symbol-only
    matching, a successful 10-K silently flagged a failed same-day 10-Q
    as confirmed — the failed filing then never consumed its retry budget
    and would re-queue every preprocess run forever. Pin the
    (symbol, form_type, filing_date) key for both result-matching and
    confirm-filing decisions."""
    good_10k = EarningsReport(
        symbol="NVDA",
        form_type="10-K",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda_10k.html",
        analysis_path="/tmp/nvda_10k.md",
        text_excerpt="...",
        is_new=True,
    )
    bad_10q = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda_10q.html",
        analysis_path="/tmp/nvda_10q.md",
        text_excerpt="...",
        is_new=True,
    )
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = [good_10k, bad_10q]

    earnings_analyst = MagicMock()
    agent_result = AgentResult(raw_text="{}", tokens_used=50, model="test", user_message="x")
    # Only the 10-K result is in the response — 10-Q analysis silently
    # validation-failed and was dropped by analyze_reports.
    earnings_analyst.analyze_reports.return_value = [
        {
            "symbol": "NVDA",
            "is_new": True,
            "form_type": "10-K",
            "filing_date": "2026-04-20",
            "agent_result": agent_result,
            "analysis": {"investment_implications": {"sentiment": "bullish", "conviction": "high"}},
        }
    ]

    pipeline = _mk_pipeline(tmp_path, earnings_provider, earnings_analyst)
    result = pipeline.run_earnings_preprocess()

    assert result["status"] == "preprocessed"
    assert result["analyzed"] == 1
    # Critical: only the 10-K is confirmed; the failed 10-Q must NOT be
    # bundled with it via symbol-collision.
    earnings_provider.confirm_filing.assert_called_once_with(good_10k)
    # Critical: the failed 10-Q must consume retry budget.
    earnings_provider.record_failure.assert_called_once_with(bad_10q)
    assert result["confirmed"] == 1
    assert result["failed"] == 1


def test_load_earnings_analyses_never_confirms_or_spawns_threads(tmp_path):
    """Hot-path invariant: `_load_earnings_analyses` is read-only.

    It may return placeholders for `is_new` filings that preprocessing missed,
    but it MUST NOT spawn a background thread, call `confirm_filing`, or
    record any failure — those side-effects belong to run_earnings_preprocess.
    """
    import threading

    new_filing = EarningsReport(
        symbol="NVDA",
        form_type="10-Q",
        filing_date="2026-04-20",
        filing_path="/tmp/nvda.html",
        analysis_path="/tmp/nvda.md",
        text_excerpt="...",
        is_new=True,
    )
    cached_filing = EarningsReport(
        symbol="AAPL",
        form_type="10-K",
        filing_date="2026-04-15",
        filing_path="/tmp/aapl.html",
        analysis_path="/tmp/aapl.md",
        text_excerpt="",
        is_new=False,
    )
    earnings_provider = MagicMock()
    earnings_provider.check_and_fetch.return_value = [new_filing, cached_filing]
    earnings_analyst = MagicMock()
    earnings_analyst.analyze_reports.return_value = [
        {
            "symbol": "AAPL",
            "is_new": False,
            "form_type": "10-K",
            "filing_date": "2026-04-15",
            "agent_result": None,
            "analysis": {"investment_implications": {"sentiment": "neutral"}},
        }
    ]

    pipeline = _mk_pipeline(tmp_path, earnings_provider, earnings_analyst)

    threads_before = threading.active_count()
    reports, results = pipeline._load_earnings_analyses("r1", session="morning")
    threads_after = threading.active_count()

    # Thread-count invariant — nothing spawned.
    assert threads_after == threads_before
    # LLM analyze_reports called ONLY on the already-confirmed cached slice.
    earnings_analyst.analyze_reports.assert_called_once_with([cached_filing])
    # No confirm/failure calls — those are preprocess-only.
    earnings_provider.confirm_filing.assert_not_called()
    earnings_provider.record_failure.assert_not_called()
    # NVDA surfaces as a placeholder (queued=True) so PM can size down.
    nvda_entries = [r for r in results if r["symbol"] == "NVDA"]
    assert len(nvda_entries) == 1
    assert nvda_entries[0]["queued"] is True
    assert nvda_entries[0]["analysis"] is None
    # AAPL comes through with a real analysis.
    aapl_entries = [r for r in results if r["symbol"] == "AAPL"]
    assert len(aapl_entries) == 1
    assert aapl_entries[0]["analysis"] is not None

    # No IndexError, no crash — proves the misalignment was tolerated.


def test_earnings_provider_prune_removes_old_raw_html_keeps_analyses(tmp_path, monkeypatch):
    """prune() deletes old raw filing HTML but never the analysis markdown
    (what evening's thesis_health reads), and keeps recent HTML."""
    import src.data.earnings as earnings_mod
    from datetime import date
    from src.data.earnings import EarningsDataProvider

    monkeypatch.setattr(earnings_mod, "et_today", lambda: date(2026, 5, 28))
    prov = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    sdir = prov.data_dir / "NVDA"
    sdir.mkdir(parents=True)
    old_html = sdir / "10-Q_2024-01-15.html"  # >400d old → prune
    recent_html = sdir / "10-Q_2026-03-15.html"  # recent → keep
    old_analysis = sdir / "analysis_10-Q_2024-01-15.md"  # analysis → always keep
    old_html.write_text("<html>old</html>")
    recent_html.write_text("<html>recent</html>")
    old_analysis.write_text("# analysis")

    removed = prov.prune(keep_days=400)

    assert removed == 1
    assert not old_html.exists()  # old raw HTML pruned
    assert recent_html.exists()  # recent raw HTML kept
    assert old_analysis.exists()  # analysis markdown never touched


# --------------------------------------------------------------------------
# `_get_existing_analysis` serves only the LATEST periodic report (2026-10-16)
#
# The 2026-09-02 90-day age bound tested here before was a made-up number and
# is gone; currency is now "is it the latest 10-Q/10-K" plus the statutory
# backstop, tested in tests/test_earnings_stance_currency.py. What stays here:
# with no periodic report in the list, nothing is served.
# --------------------------------------------------------------------------


def _write_analysis(symbol_dir, form_type, filing_date_str, body="# analysis\n"):
    symbol_dir.mkdir(parents=True, exist_ok=True)
    path = symbol_dir / f"analysis_{form_type}_{filing_date_str}.md"
    path.write_text(body)
    return path


def test_check_symbol_degrades_to_no_coverage_when_only_a_stale_analysis_exists(tmp_path, monkeypatch):
    """End-to-end through `_check_symbol`: an empty 45-day scan window plus
    an over-age cached analysis must land on the SAME `None` `_check_symbol`
    already returns for a symbol it has never covered at all (CIK miss, no
    filings ever) — not a special "stale" value some caller has to know to
    handle differently. That is what makes the degradation graceful: nothing
    downstream needs new handling because nothing downstream can tell the
    difference from ordinary no-coverage."""
    import src.data.earnings as earnings_mod
    from datetime import date
    from src.data.earnings import EarningsDataProvider

    monkeypatch.setattr(earnings_mod, "et_today", lambda: date(2026, 9, 2))
    provider = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    _write_analysis(provider.data_dir / "NVDA", "10-Q", "2026-02-14")  # 200d
    monkeypatch.setattr(provider.sec, "cik_for", lambda symbol: "0001234567")
    monkeypatch.setattr(provider.sec, "recent_filings", lambda cik, ticker: [])

    assert provider._check_symbol("NVDA") is None
    # And the never-covered symbol takes the identical path to the identical
    # value — proving there is no separate "stale" branch to drift from this
    # one.
    assert provider._check_symbol("NEVER_FILED_ANYTHING") is None


def test_check_and_fetch_omits_the_symbol_entirely_when_only_a_stale_analysis_exists(tmp_path, monkeypatch):
    """The outermost public entry point: a stale-only symbol simply does not
    appear in `check_and_fetch`'s result, the same as a symbol with no
    earnings coverage. Every consumer downstream of this list (evidence
    registry, PM sizing) already treats an absent symbol as ordinary."""
    import src.data.earnings as earnings_mod
    from datetime import date
    from src.data.earnings import EarningsDataProvider

    monkeypatch.setattr(earnings_mod, "et_today", lambda: date(2026, 9, 2))
    provider = EarningsDataProvider(data_dir=str(tmp_path / "earnings"))
    _write_analysis(provider.data_dir / "NVDA", "10-Q", "2026-02-14")  # 200d
    monkeypatch.setattr(provider.sec, "cik_for", lambda symbol: "0001234567")
    monkeypatch.setattr(provider.sec, "recent_filings", lambda cik, ticker: [])

    reports = provider.check_and_fetch(["NVDA"])

    assert reports == []
