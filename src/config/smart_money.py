"""Smart-money feed settings: insider, congress and cluster thresholds.

Moved verbatim from src/config/__init__.py (pure move; bodies AST-identical).
"""

from pydantic import BaseModel, Field, model_validator


class SmartMoneyConfig(BaseModel):
    enabled: bool = False
    search_url: str = "https://efts.sec.gov/LATEST/search-index"
    archives_url: str = "https://www.sec.gov/Archives/edgar/data"
    data_dir: str = "data/smart_money"
    user_agent: str = "QAMC research-intelligence qamc-contact@proton.me"
    request_timeout_s: float = Field(default=15.0, ge=1, le=60)
    refresh_deadline_s: float = Field(default=180.0, ge=10, le=600)
    # The watched-name Form 4 drain's OWN budget, started only after the
    # market-wide pass above has finished with `refresh_deadline_s`. Until
    # 2026-09-19 the drain shared that 180 s with the market-wide pass, which
    # ran first and measured ~153 s on its own (journal, 2026-09-18
    # 12:00:41 -> 12:03:14 UTC), so the drain could never finish.
    #
    # Sized to clear the MEASURED watched backlog in one pre-market run, read
    # against SEC read-only on 2026-09-19 with the desk's own User-Agent and
    # rate limiter:
    #   82 watched issuers, one filing-history GET each: 11.0 s measured;
    #   5,431 unread Form 4s inside `lookback_days` on those issuers;
    #   0.156 s per filing read, measured over 40 reads at the 8 req/s
    #   limiter below (SEC's published maximum is 10 req/s:
    #   https://www.sec.gov/os/accessing-edgar-data).
    #   11.0 + 5,431 x 0.156 = 858.2 s -> 859.
    # It must also fit inside the job that runs it: TimeoutStartSec=1260 in
    # scripts/systemd/quant-agent-earnings_preprocess.service. Measured job
    # parts: ~2 s startup, `refresh_deadline_s` 180, and at most 147 s of
    # work after the refresh (2026-09-17 journal, 12:03:14 -> 12:05:41) —
    # 2 + 180 + 859 + 147 = 1,188 <= 1,260. tests/test_form4_backlog_order.py
    # keeps that sum honest if any term changes.
    #
    # It binds only on the one-time catch-up: the steady-state inflow on
    # those issuers is ~16 filings a day (5,801 in-window / 365), ~3 s.
    # Progress is kept per issuer, so a drain that does not finish loses
    # nothing and the next morning resumes where it stopped.
    # Upper bound = the room that sum leaves: 1,260 - 2 - 180 - 147 = 931.
    # Lower bound mirrors `refresh_deadline_s`'s.
    watched_drain_deadline_s: float = Field(default=859.0, ge=10, le=931)
    requests_per_second: float = Field(default=8.0, ge=0.5, le=10.0)
    # 7 -> 90 -> 365 on 2026-09-11. This bounds how far back an insider/SEC
    # observation is FETCHED and RETAINED at full detail — a trade older
    # than this is invisible to correlation entirely, not just discounted.
    #
    # 365 days is a real BEHAVIORAL bound, not a calendar guess: per the
    # owner directly, someone acting on genuine inside information has no
    # logical reason to sit on it for more than a year before trading —
    # if they haven't acted within a year, the information itself either
    # played out already or was never that actionable. That's what sets
    # this number, not a storage/network cost tradeoff.
    #
    # (7 days matched nothing real to begin with: Seyhun (1986) found only
    # ~1/4 of an insider purchase's eventual abnormal return realizes in
    # the first 5 days and ~1/2 is still unrealized after a full month;
    # real M&A run-ups start MONTHS before the announcement. 90 was an
    # intermediate step, matching `EARNINGS_STANCE_MAX_AGE_DAYS`.)
    #
    # Neither real infra cost binds at 365: NETWORK cost is already
    # bounded elsewhere — `refresh()` is accession-keyed and resumable, a
    # filing already processed is never re-fetched, so this number only
    # sets how many PAST DAYS get a "anything new here?" search query each
    # refresh, with headroom left in this file's own rate/deadline budget.
    #
    # RE-CHECKED 2026-09-18, and the paragraph above was TEMPORARILY FALSE
    # for one day. It rests on the claim that only `refresh()` walks the
    # window — once a day, pre-market. `peek_accessions` was added
    # 2026-09-17 and called the same day-by-day discovery from inside every
    # intraday decision tick, so the cost this number was cleared against
    # was being paid ~13 times a day inside the decision path, where it ran
    # the tick out of its deadline. The derivation is sound again because
    # the intraday freshness check no longer walks the window at all: it
    # reads each watched issuer's own filing history
    # (`SECForm4Provider.form4_freshness`), which is O(watched names) and
    # independent of this number. Anything added later that walks the
    # lookback window from inside a decision tick falsifies this paragraph
    # again — that is the thing to check, not the value.
    #
    # RE-CHECKED 2026-09-19: "a filing already processed is never
    # re-fetched" still holds, but the claim that this number only sets how
    # many search queries run does NOT. Since PR #529 the watched-name drain
    # must READ every unread Form 4 inside this window on every watched
    # issuer before that issuer's evidence can be called current. Raising
    # 7 -> 365 therefore created a one-time read of 5,431 filings on the
    # desk's 82 watched issuers (measured 2026-09-19), which the drain
    # could not do inside the 180 s it shared with the market-wide pass.
    # That cost now has its own budget, `watched_drain_deadline_s`, sized
    # from the measurement. The value 365 is unchanged — it is the owner's
    # behavioural bound, and draining it is cheaper than seeding a claim
    # of coverage the desk has not read.
    # STORAGE cost is small: measured directly against the live server's
    # actual cache 2026-09-11 — 4,324 records / 5.76 MB at the old 7-day
    # window, roughly ~300 MB at 365 days on a straight scale-up — trivial
    # for a server either way.
    #
    # This is a FETCH/RETENTION bound, not a support-eligibility gate —
    # whether an old observation can actually support a target is decided
    # by correlation with other current evidence (see
    # `PortfolioManagerAgent`'s grounding validator), not by this number.
    lookback_days: int = Field(default=365, ge=1, le=365)
    max_filings_per_refresh: int = Field(default=1000, ge=1, le=5000)
    max_observations: int = Field(default=40, ge=1, le=200)
    # ROW-RETENTION window for `cluster_survivors`, NOT the research cluster
    # (corrected 2026-09-19, board item 124). Alldredge & Blank's abstract
    # (J. Financial Research, 2019) measures SAME-DAY purchases; "within two
    # days" appears only in a secondary summary (IBKR Campus). The
    # research-defined same-day opportunistic purchase cluster is
    # `src.data.smart_money_cluster.insider_purchase_clusters`. Was 14 days
    # with no documented rationale until the 2026-09-04 audit fix.
    cluster_window_days: int = Field(default=2, ge=1, le=45)
    min_cluster_owners: int = Field(default=2, ge=2, le=10)
    max_external_candidates: int = Field(default=3, ge=1, le=10)

    # --- Routine-versus-opportunistic Form 4 classification ---------------
    # `src/data/insider_signal.py::classify_transaction`. Evidence basis is
    # Cohen, Malloy & Pomorski, *Decoding Inside Information* (JF 2012), via
    # `docs/RESEARCH_FINDINGS.md` section 1. These were module-level
    # constants during initial development; moved here 2026-08-28 per the
    # standing rule that a threshold able to change classification output is
    # an operator-tunable setting, not a fixed number buried in code.
    #
    # A routine insider trades the same issuer in the same calendar month in
    # each of this many consecutive preceding years. This is Cohen/Malloy/
    # Pomorski's own definition, so 3 is the literature's number, not a
    # guess — but it is still exposed here rather than hardcoded, since a
    # future re-derivation against QAMC's own filing history may want a
    # different value.
    insider_calendar_routine_years: int = Field(default=3, ge=1, le=10)
    # Fallback cadence test for insiders who lack the full calendar-year
    # history above (the common case on a fresh cache — see the 2026-08-28
    # measurement note in `docs/WORK.md`, where zero of 2,188 filings matched
    # the calendar rule because the history index was brand new). Needs at
    # least this many prior same-direction trades before the gap statistics
    # are trusted.
    insider_min_cadence_trades: int = Field(default=3, ge=2, le=20)
    # Mean gap between trades, in days, that reads as a scheduled programme
    # rather than a one-off. 20-120 days admits a monthly-to-quarterly
    # cadence; narrower or wider than that is either noise (too frequent to
    # be a real event) or too sparse to call a pattern.
    insider_cadence_min_mean_gap_days: float = Field(default=20.0, gt=0)
    insider_cadence_max_mean_gap_days: float = Field(default=120.0, gt=0)
    # Coefficient of variation (population stdev / mean) of the trade gaps.
    # 0.25 admits a monthly or quarterly programme that drifts by a few days;
    # it rejects lumpy, irregularly-spaced discretionary trading.
    insider_cadence_max_gap_dispersion: float = Field(default=0.25, gt=0, le=2.0)
    # REMOVED 2026-09-13: `insider_min_material_sell_fraction`. It relabelled
    # a sale below some fraction of the insider's holding as ROUTINE, weight
    # 0.0 — dropping it out of the seat's ranking entirely. Its 0.05 default
    # matched no published boundary for a single Form 4 row. Scott & Xu (FAJ
    # 2004) use aggregate stock-wide net trades and holdings over six months,
    # so their bands cannot be transferred to this per-filing unit. The exact
    # ratio is now reported on every observation and gates nothing; its legacy
    # band is explicit unsourced debt under item 90. Do not reintroduce a
    # cutoff here without a source for this exact unit; the
    # open question is WORK.md item 63. See `src/data/insider_signal.py`
    # departure #3 and the 2026-09-13 `docs/INCIDENT_HISTORY.md` entry.
    #
    # How long `data/smart_money/insider_history.json` retains a trade date
    # before it is pruned. Must comfortably exceed the calendar-routine
    # lookback (`insider_calendar_routine_years` years) with slack for late
    # and amended filings — `observations.json` itself is pruned to
    # `lookback_days`, far too short for the calendar test, which is the
    # entire reason a separate long-horizon index exists. Default is 5
    # years (5 * 366 days, leap-safe).
    insider_history_retention_days: int = Field(default=5 * 366, ge=366, le=20 * 366)

    # --- Congress (House + Senate) trading-disclosure cross-check ---------
    # `src/data/congressional_trading.py::CongressionalTradingProvider`.
    # Two independent free, credentialless sources are cross-checked against
    # each other rather than trusted singly: both are single-operator, young
    # projects with no track record. Switched on 2026-09-20 per owner
    # ruling 2026-09-19 (docs/INCIDENT_HISTORY.md, that date): congressional
    # trading disclosures are evidence and must be weighted by the PM, never
    # zeroed out on research grounds — see `SmartMoneyFinding
    # .deterministic_eligibility`'s congressional branch (src/models.py) for
    # the confirmatory-ceiling and cluster-cap enforcement that ships with
    # this flip.
    congress_enabled: bool = True
    congress_kadoa_url: str = (
        "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json"
    )
    congress_congresswatch_url: str = "https://congresswatch.us/data/trades.json"
    congress_data_dir: str = "data/smart_money/congressional"
    congress_request_timeout_s: float = Field(default=15.0, ge=1, le=60)
    congress_refresh_deadline_s: float = Field(default=60.0, ge=10, le=300)
    # kadoa's top-level `trades.json` is itself capped at a 5,000-row recent
    # slice (not our choice, theirs); congresswatch's bulk file is ~8,000
    # rows. This just bounds how many of either we hold in memory per
    # refresh, as a sanity ceiling rather than a real limiter.
    congress_max_trades_per_source: int = Field(default=10_000, ge=100, le=50_000)
    # Congressional disclosures can lag up to ~45 days after the transaction
    # (already documented in src/agents/smart_money_analyst.py's module
    # docstring — not a new number invented here). congresswatch.us's live
    # schema carries no filing/disclosure-date field at all, so when a
    # congresswatch-only trade cannot be cross-matched against kadoa (which
    # does carry a real filing_date), this ceiling is used as the
    # conservative disclosure-date estimate: assume the latest date the
    # statute allows, never an earlier one that would overstate freshness.
    congress_assumed_max_disclosure_lag_days: int = Field(default=45, ge=1, le=90)
    # How recent a congressional disclosure must be to stay in `fetch()`'s
    # output. Deliberately MUCH looser than `lookback_days` (7): that window
    # is sized for SEC Form 4's ~2-business-day filing deadline, and applying
    # it to a stream that can legally lag 45 days would silently discard
    # nearly every real disclosure. Do NOT "harmonise" the two — they measure
    # two different statutory regimes and the Form 4 one is intentionally
    # tighter.
    #
    # Why 180 and not the 30 this shipped with:
    #   * The STOCK Act deadline is "no later than 45 days after the
    #     transaction" (House Ethics / Senate Select Committee on Ethics PTR
    #     instructions, re-verified 2026-09-04) — so a 30-day window cannot
    #     even cover the LEGAL lag, let alone real behaviour.
    #   * Filers in practice file at or near the deadline, and late filings
    #     (past 45 days) are common and still legitimate, recent trades.
    #   * Corroborating real-world datapoint: the author of a comparable free
    #     congressional-trading tool documented choosing a 180-day default for
    #     exactly this reason — a short window silently returned almost
    #     nothing. 180 is that observed-in-the-wild figure, not one invented
    #     here.
    # This is a data-COVERAGE window, not a signal-strength one. Corrected
    # 2026-09-19: this comment used to say `SmartMoneyFinding.
    # deterministic_eligibility` (src/models.py) requires congressional-only
    # evidence to be <=7 days old. That age cutoff was removed by the
    # 2026-09-11 redesign; what that validator still checks is structure (two
    # or more members, one direction, each filed within the STOCK Act's 45
    # days). Age is now weighed downstream by correlation with current
    # evidence, and the congressional refresh reports how old the newest
    # disclosure and the newest trade are.
    congress_lookback_days: int = Field(default=180, ge=1, le=365)

    @model_validator(mode="after")
    def _insider_cadence_window_is_well_formed(self):
        if self.insider_cadence_min_mean_gap_days >= self.insider_cadence_max_mean_gap_days:
            raise ValueError(
                "smart_money.insider_cadence_min_mean_gap_days must be less "
                "than insider_cadence_max_mean_gap_days; got "
                f"{self.insider_cadence_min_mean_gap_days} >= "
                f"{self.insider_cadence_max_mean_gap_days}"
            )
        required_days = self.insider_calendar_routine_years * 366
        if self.insider_history_retention_days < required_days:
            raise ValueError(
                "smart_money.insider_history_retention_days "
                f"({self.insider_history_retention_days}) is shorter than "
                f"insider_calendar_routine_years ({self.insider_calendar_routine_years}) "
                f"requires (>= {required_days} days) — the calendar-routine "
                "test would silently lose its own history before it could "
                "ever match."
            )
        return self
