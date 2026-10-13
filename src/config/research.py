"""Research-input settings: intraday scan, nominations, universe screen and news.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, Field


class IntradayScanConfig(BaseModel):
    """2026-08-19 intraday opportunity-discovery fix.

    The full opportunity-generation chain (macro/news/tech/earnings ->
    PM -> RM -> deterministic gate -> execution) runs once each morning.
    Tech's data is completed-daily-bar-as-of-prior-close; `intra_check`
    (every 30 min) is loss-protection only; midday/close review existing
    holdings only. A material move developing after the morning run could
    not generate a new trade. This adds a bounded, cheap trigger onto the
    EXISTING intra_check cadence — no new systemd timer, no full research
    stack rerun: one bulk current-session snapshot call flags symbols that
    moved materially since the last close; those movers (capped) PLUS
    currently held investable names get real daily bars/indicators and a
    real tech_analyst call, then the SAME DecisionStage -> RiskStage ->
    ExecutionStage chain morning uses. Held names are coverage so an
    increase on a quiet hold can ground; they do not consume the mover cap.
    """

    enabled: bool = False
    """Master switch. False = intra_check's existing loss-protection-only
    behavior is completely unchanged. Off by default: this is new
    autonomous-decision surface added mid-tranche, not yet operator-
    reviewed in production — flip on deliberately after reviewing the PR,
    the same rollout pattern cash_sweep followed."""

    cooldown_hours: float = Field(default=3.0, ge=0.5, le=24)
    """Minimum hours between two intraday-scan decisions for the SAME
    symbol — prevents repeated scans from churning the same setup every
    30-minute tick while a move is still developing."""

    pm_min_interval_minutes: float = Field(default=60.0, ge=0, le=390)
    """Owner ruling 2026-10-13: the PAID portfolio-manager ("what to buy")
    call runs at most once per this many minutes during the intraday cycle,
    which fires every 30 minutes. Measured from the durable agent_logs
    record of the last intraday portfolio_manager call, so it survives a
    restart. A check inside the interval still runs the free held-book
    review and stops; only the paid call is skipped. 0 disables the gate."""


class NominationConfig(BaseModel):
    """Phase 9 (`docs/QAMC_REMEDIATION_SPEC.md` §9.1/§9.2) — bounds on how
    many candidates the News/Earnings/Macro seats may put in front of
    Technical each run. Mirrors the SEC Form 4 smart-money admission cap
    (`SmartMoneyConfig.max_external_candidates`), the working precedent
    this generalises: a bounded, deterministic cap is what keeps an
    on-demand responder call affordable, not a judgment call made per run.
    """

    # Applied FIRST, per seat, before cross-seat dedupe: a single seat
    # cannot flood the responder pass. Same default (3) as
    # smart_money.max_external_candidates by design — one seat's bounded
    # nomination budget should look like the existing external-admission
    # budget an operator already understands.
    max_per_seat_per_run: int = Field(default=3, ge=1, le=10)
    # Applied AFTER cross-seat dedupe: the hard ceiling on how many
    # DISTINCT symbols may reach the on-demand Technical responder call in
    # one run, regardless of how many seats nominated or how many raw
    # nominations survived the per-seat cap.
    max_total_per_run: int = Field(default=6, ge=1, le=20)


class UniverseScreenConfig(BaseModel):
    """Universe expansion and pruning (`src/universe_screen.py`).

    The design agreed with the owner 2026-09-01 (docs/INCIDENT_HISTORY.md,
    "Universe expansion and pruning"), built 2026-09-19. With `enabled` off
    NOTHING changes: no weekly screen runs, no screened symbol reaches a
    session, and the SEC Form 4 and nomination side doors keep their
    pre-existing gates. With it on, both side doors run the same screen and
    the Form 4 door gets its age gate back.

    The spread and volatility thresholds have no field here on purpose: they
    are DERIVED at run time from `execution.max_entry_slippage_bps` and
    `risk.min_stop_atr_multiple` (see the module docstring), and the cap on
    screened names per session is `nominations.max_per_seat_per_run` — the
    screen is one more source of candidates, capped like one seat.

    The daily record-only run (`src/universe_daily.py`, owner ruling
    2026-10-09, step A) reads `data_dir`, `screen_deadline_s` and
    `bars_batch_size` too, but never `enabled`: it records, it never admits.
    """

    enabled: bool = False
    data_dir: str = "data/universe"
    # SEC Rule 3a51-1(d), 17 CFR 240.3a51-1: an equity security "that has a
    # price of five dollars or more" is not a penny stock
    # (https://www.law.cornell.edu/cfr/text/17/240.3a51-1, fetched
    # 2026-09-19). The owner's words were "filter out ... the penny stocks";
    # this is the legal line for what a penny stock is.
    min_price_usd: float = Field(default=5.0, gt=0)
    # FTSE Russell US indexes methodology: ineligible — "Companies under $30
    # Million in total market capitalization" (https://www.lseg.com/content/
    # dam/ftse-russell/en_us/documents/other/ftse-russell-us-indexes-
    # methodology-overview-cut-sheet.pdf, fetched 2026-09-19). The floor of
    # the broadest published US investable-equity index.
    min_market_cap_usd: float = Field(default=30_000_000, gt=0)
    # Wall-clock budget for one incremental pass. It runs at the end of the
    # evening session: TimeoutStartSec=1260 in
    # scripts/systemd/quant-agent-evening.service, and the evening body
    # measured 173 s on 2026-09-19 (journal, 00:00:10 -> 00:03:03 UTC).
    # 173 + 900 = 1,073 <= 1,260, leaving 187 s — more than the whole
    # measured body again. A pass that does not finish loses nothing: the
    # next evening resumes with whoever is still due.
    screen_deadline_s: float = Field(default=900.0, ge=10, le=1080)
    # Symbols per daily-bar download request (yfinance multi-ticker).
    bars_batch_size: int = Field(default=50, ge=1, le=200)


class NewsConfig(BaseModel):
    """Prompt-size control for the news seat (src/data/news.py).

    Added 2026-08-29 when RSS_FEEDS was widened from 8 to 11 sources (see
    the audit comment block at the top of src/data/news.py). More feeds
    means more raw items per fetch; `max_prompt_items` is the one knob that
    keeps what actually reaches the LLM bounded regardless of how many
    wires are configured. Previously this was a hardcoded
    `max_items=50` default on NewsDataProvider.format_for_prompt() — moved
    here per the repo's standing rule that any cap/threshold lives in
    config, not a module constant, so it can be tuned without a code
    change and is visible next to the other cost-relevant knobs.
    """

    max_prompt_items: int = Field(default=50, ge=1, le=500)
    """Max news items placed in the analyst's prompt after dedup. 50 is the
    pre-existing behavior (the old hardcoded default) — widening the feed
    set does not by itself raise this, so prompt size does not grow just
    because more wires are configured."""

    # --- Per-symbol news (2026-08-30 owner decision) -----------------------
    # The 2026-08-29 audit (src/data/news.py comment block) verified Yahoo
    # Finance's per-symbol RSS live and working, but deliberately left it
    # unwired: at the full ~101-symbol trading.universe it would be
    # 101-202 extra requests/run to a free endpoint with no documented
    # rate-limit tolerance — a real hammering risk — and scoping it to
    # "only the symbols this run actually cares about" needed portfolio
    # state threaded into the fetch call, which was a scope decision for the
    # owner rather than something to bolt on silently. The owner has now
    # made that call: free sources only, scoped to held positions + this
    # run's admitted candidates. These four settings are the caps that make
    # that safe — see `src/data/news.py::NewsDataProvider.fetch_news`.
    per_symbol_enabled: bool = True
    """Master switch. False disables per-symbol fetching entirely (zero
    added requests) regardless of the caps below — an operator emergency-off
    that doesn't require also zeroing out per_symbol_max_symbols."""

    per_symbol_max_symbols: int = Field(default=15, ge=0, le=30)
    """Hard cap on how many symbols get an individual per-symbol RSS fetch in
    one run. This is the one knob standing between this feature and the
    101-request hammering risk the 2026-08-29 audit flagged and refused to
    ship without — and it is enforced a second time inside
    NewsDataProvider itself (not only by the caller's symbol selection), so
    a future caller bug that passes the whole ~101-symbol universe still
    cannot regress to anywhere near 101 requests. Default 15: the live book
    measured 2026-08-30 held 6 positions, and the run's candidate budgets
    (smart_money.max_external_candidates=3,
    nominations.max_total_per_run=6) bound how many more can be admitted in
    one run — 15 covers that combined worst case with headroom for the book
    to grow, at one request per symbol per run. The ge=0/le=30 bounds keep an
    operator typo from silently reopening the 101-request risk (le=30 is
    already generous — it is under a third of the ~101-symbol universe)."""

    per_symbol_max_prompt_items: int = Field(default=15, ge=0, le=100)
    """Of the items that make it into the analyst's prompt (bounded overall
    by `max_prompt_items`), at most this many may be per-symbol-sourced.
    Keeps a flood of single-name headlines (e.g. every held position
    publishing something the same morning) from crowding out the general
    wire feeds that the rest of `max_prompt_items` exists to carry."""

    per_symbol_requests_per_second: float = Field(default=2.0, ge=0.2, le=10.0)
    """Politeness throttle for per-symbol Yahoo Finance requests, same
    request-interval-from-rate convention as
    `smart_money.requests_per_second` (see `SECForm4Provider`'s
    `request_interval_s` / `_RATE_LOCK` in src/data/smart_money.py, mirrored
    for this feed in src/data/news.py). Yahoo's per-symbol RSS endpoint has
    no documented rate-limit tolerance (2026-08-29 audit), so this defaults
    far below smart_money's SEC-sanctioned 8 req/s."""
