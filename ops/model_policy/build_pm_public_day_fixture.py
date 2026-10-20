#!/usr/bin/env python3
"""Build `fixtures/pm_public_day_pm_input.json` for the `pm_public_day` exam.

Runs the REAL analyst agent classes, fresh, over raw-public-fact exam
fixtures, using ONLY the free Google-direct route with
`gemini-3.5-flash-lite` (owner rule, docs/architecture — no OpenRouter or
other paid call here). Every analyst-output section it writes carries a
`kind: "fresh_analyst_output"` provenance entry
(`ops/model_policy/fixture_policy.py`) naming the model, route, timestamp and
the raw-facts source fixture it was computed from.

**2026-09-14 OWNER RULING — real-day scale.** tech_analyst, earnings_analyst
and smart_money_analyst run over dedicated, real-day-scale raw-fact fixtures
built fresh for THIS exam (`yf_daily_bars_pm_public_day_2026-09-14.json`:
yfinance bars for the full live 101-symbol universe, config/settings.yaml:
1192; `sec_10q10k_pm_public_day_2026-09-14.json`: real 10-Q/10-K HTML for
every universe symbol with a filing inside the live 45-day earnings window,
src/data/earnings.py:89; `sec_form4_pm_public_day_2026-09-14.json`: real
market-wide SEC Form 4 disclosures via the live 365-day-configured insider
window, config/settings.yaml:1088 — actual coverage achieved is partial and
is stated plainly in that fixture's `_exam.actual_coverage`). macro_analyst
and news_analyst still run over the pre-existing `fred_macro_2026-09-14.json`
/ `rss_feeds_2026-09-14.json` fixtures — neither seat's input scales with
the trading universe, so no rebuild was needed there.

Account/positions cannot come from either a public source or an analyst
seat — a real account is desk state. This fixture uses a flat, clearly
labelled SYNTHETIC ACCOUNT STATE (all cash, no positions), never an invented
price or news item.

Run:
    .venv/bin/python ops/model_policy/build_pm_public_day_fixture.py

Stops and reports on the first Google call failure or quota hit — never
substitutes another route or model.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.model_policy import fixture_policy as fp
from ops.model_policy.benchmark_models import (
    PLACEHOLDER_KEY,
    build_benchmark_circuit,
    load_env_if_keys_missing,
    wire_from_onecli,
)
from ops.model_policy.scenarios import (
    _MACRO_FIXTURE,
    _NEWS_FIXTURE,
    _public_macro_summary,
    _public_news_report,
    production_max_tokens,
)

MODEL = "gemini-3.5-flash-lite"
ROUTE = "google-direct"
OUT_PATH = fp.FIXTURES_DIR / "pm_public_day_pm_input.json"

# 2026-09-14 OWNER RULING follow-up: the PM exam must be real-day scale, not
# a 5-actionable/2-neutral hand-pick. These three fixtures replace
# scenarios.py's single-symbol/7-symbol/3-day exam fixtures for THIS
# builder only -- the shared `_TECH_FIXTURE` / `_EARNINGS_FIXTURE` /
# `_SMART_MONEY_FIXTURE` scenarios (`tech_batch`, `earnings_filing`,
# `smart_money_form4`) are untouched.
_PM_TECH_FIXTURE = "yf_daily_bars_pm_public_day_2026-09-14.json"
_PM_TECH_BLOB = "yf_daily_bars_pm_public_day_2026-09-14.json.gz"
_PM_EARNINGS_FIXTURE = "sec_10q10k_pm_public_day_2026-09-14.json"
_PM_EARNINGS_BLOB = "sec_10q10k_pm_public_day_2026-09-14.json.gz"
_PM_FORM4_FIXTURE = "sec_form4_pm_public_day_2026-09-14.json"
_PM_FORM4_BLOB = "sec_form4_pm_public_day_2026-09-14.json.gz"


def pm_public_day_tech_symbols_data() -> list[dict]:
    """Real bars for the full live 101-symbol universe, reduced to the same
    actionable subset the live morning session would send to `tech_analyst`
    -- `TradingPipeline._has_actionable_signal_fn` (src/pipeline.py:2694),
    replayed with no held positions (a flat synthetic account holds none)."""
    from src.data.technical import compute_indicators
    from src.models import OHLCV
    from src.pipeline import TradingPipeline

    manifest = fp.FIXTURES_DIR / _PM_TECH_FIXTURE
    manifest_data = json.loads(manifest.read_text())
    raw = json.loads(fp.load_blob(_PM_TECH_FIXTURE, _PM_TECH_BLOB))
    out = []
    for symbol in manifest_data["symbols"]:
        bars = [
            OHLCV(date=b["date"], open=b["open"], high=b["high"], low=b["low"],
                  close=b["close"], volume=int(b["volume"]))
            for b in raw[symbol]
        ]
        indicators = compute_indicators(symbol, bars)
        if TradingPipeline._has_actionable_signal_fn(indicators, symbol, bars, []):
            out.append({"symbol": symbol, "bars": bars, "indicators": indicators})
    return out


def pm_public_day_earnings_reports(scratch) -> list:
    """Real `EarningsReport`s for every universe symbol with a live-window
    (45-day, src/data/earnings.py:89) 10-Q/10-K filing, rebuilt from the
    pinned raw filing HTML by today's `extract_text`.
    No XBRL cross-check at this scale (see the fixture's own provenance
    note) -- `xbrl_facts` stays {}, which `EarningsAnalystAgent` treats as
    nothing to cross-check, same as a filer with no XBRL at all."""
    from src.data.earnings import EarningsDataProvider, EarningsReport, extract_text

    manifest_data = json.loads((fp.FIXTURES_DIR / _PM_EARNINGS_FIXTURE).read_text())
    html_by_key = json.loads(fp.load_blob(_PM_EARNINGS_FIXTURE, _PM_EARNINGS_BLOB))
    provider = EarningsDataProvider(data_dir=str(scratch / "earnings_provider"))
    reports = []
    for filing in manifest_data["filings"]:
        html_path = scratch / f"{filing['key']}.html"
        html_path.write_text(html_by_key[filing["key"]], encoding="utf-8")
        text = extract_text(str(html_path))
        analysis_dir = scratch / "analyses" / filing["symbol"]
        analysis_dir.mkdir(parents=True, exist_ok=True)
        reports.append(EarningsReport(
            symbol=filing["symbol"], form_type=filing["form_type"],
            filing_date=filing["filing_date"], filing_path=str(html_path),
            analysis_path=str(analysis_dir / f"analysis_{filing['form_type']}_{filing['filing_date']}.md"),
            text_excerpt=text, is_new=True, xbrl_facts={},
        ))
    return reports


def pm_public_day_smart_money_observations(scratch, app_config) -> list:
    """Real Form 4 disclosures re-classified/admitted by today's
    `SECForm4Provider.fetch()` (src/data/smart_money.py:688) over the
    pinned raw parsed-disclosure rows -- market-wide discovery, live
    smart_money config (config/settings.yaml:1075). See the fixture's
    `_exam.actual_coverage` for the real (partial) window achieved."""
    import src.data.smart_money as smart_money

    manifest_data = json.loads((fp.FIXTURES_DIR / _PM_FORM4_FIXTURE).read_text())
    rows = json.loads(fp.load_blob(_PM_FORM4_FIXTURE, _PM_FORM4_BLOB))
    sm_cfg = app_config.smart_money
    provider = smart_money.SECForm4Provider(
        search_url=sm_cfg.search_url, archives_url=sm_cfg.archives_url,
        data_dir=str(scratch / "smart_money_provider"), user_agent=sm_cfg.user_agent,
        request_timeout_s=sm_cfg.request_timeout_s,
        requests_per_second=sm_cfg.requests_per_second, lookback_days=sm_cfg.lookback_days,
        min_transaction_value_usd=sm_cfg.min_transaction_value_usd,
        external_min_transaction_value_usd=sm_cfg.external_min_transaction_value_usd,
        cluster_window_days=sm_cfg.cluster_window_days, min_cluster_owners=sm_cfg.min_cluster_owners,
        refresh_deadline_s=sm_cfg.refresh_deadline_s,
        max_filings_per_refresh=sm_cfg.max_filings_per_refresh,
    )
    (scratch / "smart_money_provider").mkdir(parents=True, exist_ok=True)
    (scratch / "smart_money_provider" / "observations.json").write_text(json.dumps(rows))
    observations, error = provider.fetch(app_config.trading.universe)
    if error:
        raise RuntimeError(f"pm_public_day Form4 reconstruction did not re-derive cleanly: {error}")
    print(f"    -> _exam.actual_coverage: {manifest_data['_exam']['actual_coverage']}")
    return observations


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh_section(value, *, model: str, route: str, timestamp: str, source_fixture: str) -> dict:
    return {"value": value, "kind": "fresh_analyst_output", "model": model,
            "route": route, "timestamp": timestamp, "source_fixture": source_fixture}


def _agent(agent_path: str, role: str, cost_circuit):
    module_name, cls_name = agent_path.split(":")
    import importlib

    cls = getattr(importlib.import_module(module_name), cls_name)
    agent = cls(api_key=PLACEHOLDER_KEY, model=MODEL, max_tokens=production_max_tokens(role),
                provider="google")
    agent.set_cost_circuit(cost_circuit)
    return agent


def build() -> dict:
    load_env_if_keys_missing()
    print(f"[{_now()}] wiring OneCLI gateway...")
    wire_from_onecli()

    from src.config import load_config

    app_config = load_config(PROJECT_ROOT / "config" / "settings.yaml")
    # 5 free-tier google-direct calls, one per analyst seat. A tiny budget is
    # enough — gemini-3.5-flash-lite is a free model, and this is not a
    # sweep. Never raised above the live daily cap (the circuit itself
    # refuses that).
    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="pm-public-day-build-"))

    # Real-day scale (2026-09-14 OWNER RULING): tech gets the full live
    # 101-symbol universe's actionable subset (chunked internally by
    # analyze_batch at 30/call, src/agents/tech_analyst.py:76), earnings
    # gets every real filing inside the live 45-day window (49 measured),
    # smart_money gets the live-reconstructed admitted set. Plan enough
    # trials that the benchmark's own call-count breaker (derived from
    # planned_trials, not the live desk's session cap) never binds.
    tech_symbols_data = pm_public_day_tech_symbols_data()
    earnings_reports = pm_public_day_earnings_reports(scratch)
    planned_trials = 8 + -(-len(tech_symbols_data) // 30) + len(earnings_reports)
    cost_circuit = build_benchmark_circuit(
        app_config, budget_usd=min(1.0, float(app_config.llm_cost_circuit.daily_cost_limit_usd)),
        planned_trials=planned_trials,
        run_id=f"pm-public-day-{int(datetime.now(timezone.utc).timestamp())}",
    )
    cost_circuit.require_paid_analysis("pm_public_day_build_start")

    sections: dict[str, dict] = {}
    manifest: dict = {}

    # 1. tech_analyst -> analyses (full live-universe actionable subset)
    print(f"[{_now()}] tech_analyst over {_PM_TECH_FIXTURE} "
          f"({len(tech_symbols_data)} actionable of the 101-symbol universe) ...")
    tech_agent = _agent("src.agents.tech_analyst:TechAnalystAgent", "tech_analyst", cost_circuit)
    analyses, _ = tech_agent.analyze_batch(symbols_data=tech_symbols_data)
    resolved = {s: a for s, a in (analyses or {}).items() if a is not None}
    if not resolved:
        raise RuntimeError("tech_analyst returned no usable analyses — stopping, not substituting")
    ts = _now()
    manifest["analyses"] = [a.model_dump(mode="json") for a in resolved.values()]
    sections["analyses"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                             "timestamp": ts, "source_fixture": _PM_TECH_FIXTURE}
    print(f"    -> {len(resolved)} symbols resolved")

    # 2. macro_analyst -> macro_analysis
    print(f"[{_now()}] macro_analyst over {_MACRO_FIXTURE} ...")
    macro_agent = _agent("src.agents.macro_analyst:MacroAnalystAgent", "macro_analyst", cost_circuit)
    macro_analysis, _ = macro_agent.analyze(
        macro_summary=_public_macro_summary(),
        universe=["SPY", "QQQ", "XLE", "XLU", "XLP", "XLF", "SMH", "AAPL", "NVDA"],
        last_state=None, news_narrative=None,
    )
    if macro_analysis is None:
        raise RuntimeError("macro_analyst returned no analysis — stopping, not substituting")
    ts = _now()
    manifest["macro_analysis"] = macro_analysis.model_dump(mode="json")
    sections["macro_analysis"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                                   "timestamp": ts, "source_fixture": _MACRO_FIXTURE}

    # 3. news_analyst -> news_intel
    print(f"[{_now()}] news_analyst over {_NEWS_FIXTURE} ...")
    news_universe = [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "TSLA", "META",
        "AMD", "MU", "AVGO", "XLE", "XLF", "XLU", "XLK",
    ]
    news_text, stock_mentions, coverage = _public_news_report()
    news_agent = _agent("src.agents.news_analyst:NewsAnalystAgent", "news_analyst", cost_circuit)
    news_report, _ = news_agent.analyze(
        news_text=news_text, universe=news_universe, stock_mentions=stock_mentions,
        previous_narrative=None, session="morning", prior_session_report=None,
        news_coverage=coverage,
    )
    if news_report is None:
        raise RuntimeError("news_analyst returned no report — stopping, not substituting")
    ts = _now()
    manifest["news_intel"] = news_report.model_dump(mode="json")
    sections["news_intel"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                               "timestamp": ts, "source_fixture": _NEWS_FIXTURE}

    # 4. earnings_analyst -> earnings_analyses (every real filing in the
    # live 45-day window across the full universe, not one hand-picked name)
    print(f"[{_now()}] earnings_analyst over {_PM_EARNINGS_FIXTURE} "
          f"({len(earnings_reports)} real filings) ...")
    earnings_agent = _agent("src.agents.earnings_analyst:EarningsAnalystAgent", "earnings_analyst", cost_circuit)
    results = earnings_agent.analyze_reports(earnings_reports)
    earnings_analyses = [r["analysis"] for r in results if r.get("analysis") is not None]
    if not earnings_analyses:
        raise RuntimeError("earnings_analyst returned no usable analyses — stopping, not substituting")
    ts = _now()
    manifest["earnings_analyses"] = earnings_analyses
    sections["earnings_analyses"] = {"kind": "fresh_analyst_output", "model": MODEL, "route": ROUTE,
                                      "timestamp": ts, "source_fixture": _PM_EARNINGS_FIXTURE}
    print(f"    -> {len(earnings_analyses)}/{len(earnings_reports)} analyses resolved")

    # 5. smart_money_analyst -> smart_money_findings (live-reconstructed,
    # market-wide-discovered, full universe -- see fixture _exam.actual_coverage)
    print(f"[{_now()}] smart_money_analyst over {_PM_FORM4_FIXTURE} ...")
    sm_agent = _agent("src.agents.smart_money_analyst:SmartMoneyAnalystAgent", "smart_money_analyst", cost_circuit)
    sm_agent.synthesis_cache_path = scratch / "synthesis_cache.json"
    observations = pm_public_day_smart_money_observations(scratch, app_config)
    findings, _result, error = sm_agent.analyze(observations)
    if error:
        raise RuntimeError(f"smart_money_analyst error={error} — stopping, not substituting")
    ts = _now()
    manifest["smart_money_findings"] = [f.model_dump(mode="json") for f in (findings or [])]
    sections["smart_money_findings"] = {"kind": "fresh_analyst_output", "model": MODEL,
                                         "route": ROUTE, "timestamp": ts,
                                         "source_fixture": _PM_FORM4_FIXTURE}
    print(f"    -> {len(findings or [])} findings over {len(observations)} observations")

    # 6. Synthetic account state — explicitly labelled, not desk data.
    manifest["account_state"] = {
        "label": "SYNTHETIC ACCOUNT STATE — not a recorded desk balance",
        "cash_balance": 100000.0,
        "reserve_balance": 0.0,
        "total_value": 100000.0,
        "positions": [],
        "allow_margin": False,
        "session_type": "morning",
    }
    sections["account_state"] = {
        "kind": "synthetic_account_state",
        "label": "SYNTHETIC ACCOUNT STATE — flat $100,000 cash, no positions; not a desk recording",
    }

    manifest["_provenance"] = {
        "note": (
            "pm_public_day exam fixture. analyses/macro_analysis/news_intel/"
            "earnings_analyses/smart_money_findings are FRESH analyst-seat output "
            "(gemini-3.5-flash-lite, google-direct route) computed at build time "
            "over the existing raw-public-fact exam fixtures named per section. "
            "account_state is a labelled synthetic starting state, not a desk "
            "recording. No desk data anywhere in this file."
        ),
        "sections": sections,
    }
    return manifest


def main() -> int:
    manifest = build()
    OUT_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    verdict = fp.check_fixture(OUT_PATH)
    print(f"[{_now()}] wrote {OUT_PATH}")
    if not verdict.admissible:
        print("QUARANTINED:")
        for p in verdict.problems:
            print(f"  - {p}")
        return 1
    print("fixture_policy: admissible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
