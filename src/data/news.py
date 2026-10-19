import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from xml.etree import ElementTree

import feedparser

from src.data.news_dedup import NewsCluster, cluster_news, normalize_link
from src.session_calendar import SessionCalendarUnavailable, session_for
from src.trading_calendar import et_now, utc_now

logger = logging.getLogger(__name__)

RSS_FEEDS = {
    # Financial / Markets
    "CNBC Top News": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC Economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "MarketWatch Top": "https://feeds.marketwatch.com/marketwatch/topstories/",
    # "MarketWatch Markets" (marketpulse) removed 2026-08-29 — see the
    # audit note below. It 200s but stopped publishing over a year ago.
    # "Yahoo Finance News" (finance.yahoo.com/news/rssindex) removed
    # 2026-10-16: HTTP 404 on every fetch since 15 Oct (13 failures in the
    # desk log 15:15-00:00 UTC); /rss/topstories and /news/rss also 404.
    # Seeking Alpha's editorial wire — company events (M&A, FDA approvals,
    # contract wins, share actions), not just macro. Verified live
    # 2026-08-29: HTTP 200, valid RSS, 7 items, newest ~minutes old.
    "Seeking Alpha Market Currents": "https://seekingalpha.com/market_currents.xml",
    # High-frequency general markets wire: macro, geopolitical, insider
    # buy/sell headlines. Verified live 2026-08-29: HTTP 200, valid RSS,
    # 10 items, newest ~9 minutes old (the freshest feed in this set).
    "Investing.com News": "https://www.investing.com/rss/news.rss",
    # Verified live 2026-08-29 via the exact fetch path this module uses
    # (urlopen + this module's USER_AGENT, 10s timeout): HTTP 200 in ~3s,
    # valid RSS, 15 items, newest ~minutes old. Despite the URL, content is
    # general market/company news (commodities, FDA approvals, single-name
    # comparisons), not just Nasdaq corporate announcements.
    "Nasdaq News": "https://www.nasdaq.com/feed/rssoutbound?category=Press-Release",
    # Macro / Policy / Politics
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "NPR Economy": "https://feeds.npr.org/1017/rss.xml",
    # Fed / Treasury
    "Fed Press Releases": "https://www.federalreserve.gov/feeds/press_all.xml",
    # Regulatory / legal — fills a gap the original 8 feeds had no
    # coverage for at all. Verified live 2026-08-29: HTTP 200, valid RSS,
    # 25 items (rule proposals, enforcement themes, market-structure
    # actions), newest ~10h old. Fetched with the SEC-compliant User-Agent
    # (see SEC_USER_AGENT_HOSTS / _user_agent_for below) — same politeness
    # convention config.smart_money.user_agent already uses for EDGAR.
    "SEC Press Releases": "https://www.sec.gov/news/pressreleases.rss",
}

# ---------------------------------------------------------------------------
# Removed 2026-08-28: "Reuters Business" and "AP Business" — both wires were
# returning zero items (404 / 403 respectively) and, worse, failing SILENTLY:
# fetch_news() logged a warning per feed and reported the run as complete
# regardless. That silent-drop bug is fixed below (see NewsCoverage), but the
# two feeds themselves turned out not to be fixable for free. Investigated
# live the same day, with a plausible "just needs a real User-Agent" fix
# tried FIRST since a browser UA fixes the majority of anti-scraping 403s:
#
#   - Reuters ("https://www.reutersagency.com/feed/?best-topics=..."): a
#     browser UA changes nothing. Reuters killed public RSS in June 2020
#     (widely documented, e.g. https://news.ycombinator.com/item?id=23576022
#     and https://www.fivefilters.org/2021/reuters-rss-feeds/). The URL
#     redirects (301) to reutersagency.com, which 200s but is now a HubSpot
#     marketing page selling paid Reuters Connect licensing — there is no
#     feed link left in the page at all, so this isn't a moved URL, it's a
#     retired product. reuters.com itself 401s on every path, including the
#     homepage, behind a DataDome JS/CAPTCHA wall (captcha-delivery.com) —
#     also not a User-Agent problem.
#   - AP ("https://rsshub.app/apnews/topics/business"): this was never AP's
#     own feed — rsshub.app is a third-party scraper that reformats AP's
#     site into RSS, and it is now sitting behind a Cloudflare managed JS
#     challenge ("Just a moment...", cRay/cZone markers in the response
#     body) that no User-Agent string can pass, since it requires executing
#     JS. AP's own site DOES advertise a real feed
#     (apnews.com/index.rss, found via <link rel="alternate"> autodiscovery)
#     but it answers HTTP 401 "Invalid client credentials" — that endpoint
#     now sits behind AP's paid Content/Breaking News API
#     (developerapi.ap.org, API key + OAuth2 required per AP's own
#     developer docs). Both AP's official route and the free proxy route
#     are dead.
#
# Net: the UA hypothesis was WRONG for both — the failures are a retired
# product (Reuters) and a paid-API gate plus a bot-walled proxy (AP), not a
# blocked header. Per the standing "no new paid dependency without owner
# approval" rule, this is a STOP-and-report, not a sign-up. If dedicated
# Reuters/AP wire coverage is wanted, that is an owner decision (Reuters
# Connect or the AP Content API, both paid) — tracked in docs/WORK.md rather
# than silently worked around.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 2026-08-29 audit — every URL below was fetched live in this session,
# through the SAME code path production uses (urlopen + this module's
# USER_AGENT / SEC UA + FETCH_TIMEOUT), not just curled from a shell.
#
# REMOVED: "MarketWatch Markets" (feeds.marketwatch.com/marketwatch/
# marketpulse/) — returns HTTP 200 and parses as valid RSS, but its newest
# entry was dated 2025-07-03, i.e. it had been silently frozen for over a
# year. A 200 with year-old content is dead in every way that matters to
# this desk and NewsCoverage's "ok" would have hidden that fact, so it is
# removed rather than left in place returning stale-but-technically-valid
# data every run.
#
# ADDED (all verified live 2026-08-29, see the inline comment on each
# entry above): Seeking Alpha Market Currents, Investing.com News, Nasdaq
# News, SEC Press Releases.
#
# CHECKED AND REJECTED — fetched successfully but not added, or could not
# be fetched at all:
#   - SEC EDGAR "current events" filing feed (getcurrent&type=8-K, atom,
#     https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K
#     &company=&dateb=&owner=include&count=100&output=atom): live, 100
#     items/fetch, but it is EVERY public filer's 8-K, not scoped to this
#     desk's ~101-symbol universe, and items are keyed by company name/CIK
#     rather than ticker so tag_symbol_mentions' word-boundary ticker match
#     mostly misses them. Sorted newest-first into a capped prompt, 100
#     mostly-irrelevant micro-cap filings would crowd out real wire
#     headlines from the same window. Left out; a CIK->ticker filtered
#     variant would be a reasonable follow-up but is a bigger change than
#     "add a feed."
#   - SEC Litigation Releases (https://www.sec.gov/enforcement-litigation/
#     litigation-releases/rss): live, 25 items, but title AND summary are
#     just the defendant's name (e.g. "Ichcoin Tech Corp.", "Stephen E.
#     Buyer, et al.") with no case description in the feed itself — this
#     pipeline reads headlines/summaries, not the linked page, so the feed
#     carries no usable signal as configured.
#   - Benzinga (https://www.benzinga.com/feed): live, 200, 10 items — but
#     the content is crypto price-prediction SEO posts ("Toncoin (TON)
#     Price Prediction 2025, 2026, 2027-2030") and affiliate content
#     ("Credible Review"), not equities/macro news. Rejected on content
#     quality, not reachability.
#   - Yahoo Finance per-symbol RSS (https://finance.yahoo.com/rss/
#     headline?s=AAPL) and Seeking Alpha per-symbol
#     (https://seekingalpha.com/api/sa/combined/AAPL.xml): both verified
#     live and working for a single symbol. NOT wired in: at the full
#     101-symbol trading.universe this is 101-202 extra HTTP requests to a
#     free public endpoint every run, with no documented rate-limit
#     tolerance — real hammering risk. Capping to "the symbols the desk
#     actually cares about this run" (positions + active candidates) would
#     need portfolio state plumbed into NewsDataProvider, which doesn't
#     have it today and doesn't construct it here — that is a scope/design
#     decision for the owner, not something to bolt on silently. Tracked
#     in docs/WORK.md rather than half-wired.
#
#     2026-08-30 UPDATE — the owner made that scope decision: free sources
#     only, scoped every run to held positions + this run's admitted
#     candidates (never the full universe), with a hard symbol cap so this
#     can never regress toward 101-202 requests. Re-verified live the same
#     day (through this exact module's fetch path, urlopen + USER_AGENT):
#     https://finance.yahoo.com/rss/headline?s=<TICKER> still 301-redirects
#     to feeds.finance.yahoo.com and returns HTTP 200 with valid RSS 2.0
#     (13-20 items/symbol across the 6 symbols in the live book that day —
#     see the PR description for the full per-symbol measurement). Wired in
#     below as `YAHOO_PER_SYMBOL_URL_TEMPLATE` /
#     `NewsDataProvider.fetch_news(symbols=...)`. 2026-10-19: the name-count
#     and headline-count caps were removed — every shortlisted name is read,
#     each for the headlines since the previous session's close; only the
#     request throttle stays (`config.news.per_symbol_requests_per_second`). Seeking Alpha per-symbol remains
#     verified-available but is deliberately NOT enabled alongside it — one
#     request per symbol, not two, keeps the added request count halved.
#     This is a scope choice, not a disabled feature; flip it on only with
#     a matching second look at Seeking Alpha's own rate tolerance.
#   - GlobeNewswire (https://www.globenewswire.com/rss/list and the atom/
#     rss subject-code variants): every attempt (urlopen, curl, curl
#     --http1.1) either hung until timeout or dropped the HTTP/2 stream —
#     never got a parseable response in this session. Not added; not
#     reachable from here, not a content judgment.
#   - Business Wire (feed.businesswire.com/rss/home/?rss=...): HTTP 200
#     but 0 entries — the public example feed URL is a dead stub, not a
#     working general wire. Business Wire's real feeds are per-newsroom
#     and account-gated.
#   - U.S. Treasury press releases: every URL tried (home.treasury.gov/rss/
#     press-releases, /news/press-releases/rss.xml) 404s, and the live
#     press-releases HTML page has no RSS <link> autodiscovery tag either
#     — Treasury's public RSS appears to have been discontinued (their own
#     site carries a stale 2021 notice about the feed showing migrated old
#     releases). No working free URL found.
#   - BEA (bea.gov/rss.xml and variants): 404, no RSS discovered.
#   - BLS (bls.gov/feed/bls_latest.rss): returned HTTP 200 once, but a
#     repeat fetch in the same session got an "Access Denied" bot-block
#     page instead of the feed — BLS's WAF appears to rate-limit
#     automated fetches aggressively even at low volume. Combined with the
#     feed itself carrying exactly one generic aggregate item ("Major
#     Economic Indicators Latest Numbers", not real distinct headlines),
#     this is both a reliability risk and low content value. Not added.
#   - Nasdaq IR (ir.nasdaq.com/tools/rss-feeds) and NYSE: no reachable
#     public RSS found (ir.nasdaq.com did not respond in this session;
#     NYSE was not found to publish a public feed at all).
#   - Financial Modeling Prep / Finnhub / Alpha Vantage: all require an API
#     key for any endpoint beyond a bare ping/quota check. Per the
#     no-paid-signup rule this was not tested further and nothing was
#     signed up for — reported as needing a key, left out.
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (quant-agent/0.1)"
# SEC.gov asks (does not strictly require for plain RSS, but this repo
# already treats it as a hard requirement for EDGAR — see
# config.smart_money.user_agent / src/data/earnings.py / src/data/
# smart_money.py) that automated clients identify themselves with contact
# info. Reused here verbatim rather than inventing a second convention;
# Pipeline wires this from config.smart_money.user_agent at construction
# time (see NewsDataProvider.__init__), this literal is only the fallback
# for a NewsDataProvider built without a config (tests, scripts).
SEC_USER_AGENT = "QAMC research-intelligence qamc-contact@proton.me"
FETCH_TIMEOUT = 10

# Per-symbol Yahoo Finance RSS — see the 2026-08-30 UPDATE note in the audit
# block above. `{symbol}` is formatted with the bare ticker (e.g. "AAPL"); the
# endpoint 301-redirects to feeds.finance.yahoo.com, which urlopen follows
# automatically. One request per symbol — Seeking Alpha's per-symbol
# endpoint is deliberately not also called, to keep the added request count
# halved (see the audit note).
YAHOO_PER_SYMBOL_URL_TEMPLATE = "https://finance.yahoo.com/rss/headline?s={symbol}"

# Shared across every NewsDataProvider instance in the process, same pattern
# as src/data/smart_money.py's module-level `_RATE_LOCK` / `_LAST_REQUEST_AT`
# — politeness to a given host is a property of the process talking to it,
# not of any one provider object. Kept separate from smart_money's globals
# (different host, different tolerance) rather than sharing them.
_PER_SYMBOL_RATE_LOCK = threading.Lock()
_PER_SYMBOL_LAST_REQUEST_AT = 0.0


def _is_sec_gov(url: str) -> bool:
    """True for any *.sec.gov feed URL, so it gets SEC_USER_AGENT instead
    of the generic USER_AGENT. Suffix-matched on the hostname (not a raw
    substring check) so a URL like "notsec.gov.evil.example" can't spoof
    this."""
    host = (urlparse(url).hostname or "").lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


@dataclass
class FeedFailure:
    """One configured feed that did not return data on a fetch_news() call.

    ``reason`` is the exception text, truncated — feed failures are almost
    always short (HTTPError, URLError, timeout), but a feedparser bozo
    exception on a genuinely malformed document can ramble, and this string
    ends up both in a log line and in the analyst's prompt.
    """

    name: str
    reason: str


# Exception text longer than this is truncated before it reaches a log line
# or the analyst prompt. Generous enough for any real HTTP/parse error
# message seen in practice, short enough that one verbose exception can't
# blow up the coverage section of the prompt.
_FAILURE_REASON_MAX_LEN = 200


@dataclass
class NewsCoverage:
    """How much of the configured wire coverage actually came back on one
    fetch_news() call — the fix for the 2026-08-28 incident where two dead
    feeds (Reuters 404, AP 403) were dropped with a log warning and the
    pipeline still reported the news stage "ok" regardless. The desk was
    making decisions believing it had read every wire when two had
    returned nothing, and nothing downstream of the warning could tell.

    This object is the single source of truth for that fact from here on.
    It is threaded into the analyst's prompt (build_user_message) AND into
    the deterministic data_status the operator surface reads
    (MorningResearchStage / trader_feed / notifier) — a log line alone was
    exactly the failure mode being fixed, so this must never be logged only.
    """

    configured: int
    succeeded: int
    failed: list[FeedFailure]
    # Shortlisted names that reach the seat with no headline of their own,
    # each with WHY (2026-10-19): "fetch failed: ...", "no headlines since
    # ...", or "headlines merged into a wire copy". Insertion order is the
    # caller's name order. Empty for a run with no per-symbol pass.
    per_symbol_gaps: dict[str, str] = field(default_factory=dict)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def complete(self) -> bool:
        """True only when every configured feed returned successfully.

        Zero configured feeds is deliberately NOT complete — an empty feed
        dict is a configuration error, not full coverage of nothing.
        """
        return self.configured > 0 and self.failed_count == 0

    @property
    def status(self) -> str:
        """One word for data_status[...] / logs — mirrors the ok / partial /
        failed vocabulary MorningResearchStage already uses for `tech`
        (src/pipeline_stages.py), so this reuses an existing convention
        rather than inventing a parallel one.
        """
        if self.configured == 0 or self.succeeded == 0:
            return "failed"
        if self.failed:
            return "partial"
        return "ok"

    def describe(self) -> str:
        """Human-readable one-liner for the analyst prompt and log lines.

        Deliberately does not say "no news" or go quiet when coverage is
        bad — it says exactly what happened, by name, so a reader (human or
        model) cannot mistake missing input for a quiet news day.
        """
        if self.configured == 0:
            return "News coverage: NO feeds configured (misconfiguration)."
        if not self.failed:
            text = f"News coverage: {self.succeeded}/{self.configured} feeds returned data. Full coverage."
        else:
            names = ", ".join(f"{f.name} ({f.reason})" for f in self.failed)
            text = (
                f"News coverage: {self.succeeded}/{self.configured} feeds returned data "
                f"this run. FAILED: {names}. Treat this as a coverage GAP, not "
                f"confirmed silence — do not conclude a topic is quiet solely "
                f"because a failed feed would normally cover it."
            )
        if self.per_symbol_gaps:
            gaps = "; ".join(f"{sym}: {why}" for sym, why in self.per_symbol_gaps.items())
            text += f" Names with no headline of their own: {gaps}."
        return text


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    published: datetime | None
    link: str
    # Syndication breadth, filled in by the dedup stage (src/data/news_dedup).
    # `collapsed_count` is how many articles reported this same event —
    # including this one — and `source_count` how many distinct outlets. They
    # default to 1 so an item that never went through dedup reads correctly.
    #
    # These exist because "twelve outlets carried this" is real information
    # about SALIENCE, while being emphatically NOT twelve independent
    # confirmations. Dropping the duplicates without recording the count would
    # trade one distortion for another.
    collapsed_count: int = 1
    source_count: int = 1
    # True when this item (or, after dedup, its cluster's representative)
    # came from a per-symbol fetch (see NewsDataProvider.fetch_news) rather
    # than a general wire feed. format_for_prompt bounds only general-wire
    # items by count; per-symbol items are bounded by their time window.
    # Defaults False so every general-feed item is unaffected.
    per_symbol: bool = False
    # The ticker whose per-symbol feed produced this item ("" for a wire).
    symbol: str = ""


@dataclass
class _PerSymbolPass:
    """What one per-symbol pass fetched, for coverage and gap reasons."""

    configured: int = 0
    items: list = field(default_factory=list)
    fetched: dict[str, int] = field(default_factory=dict)  # name -> headlines in window
    failed: dict[str, str] = field(default_factory=dict)  # name -> failure reason
    window_label: str = ""

    def gaps(self, deduped: list) -> dict[str, str]:
        """Why each name reaches the seat with no headline of its own."""
        surviving = {item.symbol for item in deduped if item.per_symbol and item.symbol}
        out: dict[str, str] = {sym: f"fetch failed: {why}" for sym, why in self.failed.items()}
        for sym, count in self.fetched.items():
            if count == 0:
                out[sym] = f"no headlines since {self.window_label}"
            elif sym not in surviving:
                out[sym] = "headlines merged into a wire copy of the same story"
        return out


class NewsDataProvider:
    def __init__(
        self,
        feeds: dict[str, str] | None = None,
        lookback_hours: int = 24,
        sec_user_agent: str = SEC_USER_AGENT,
        per_symbol_enabled: bool = True,
        per_symbol_feed_template: str = YAHOO_PER_SYMBOL_URL_TEMPLATE,
        per_symbol_requests_per_second: float = 2.0,
        session_broker: Callable[[], object] | None = None,
    ):
        self.feeds = feeds or RSS_FEEDS
        self.lookback_hours = lookback_hours
        # SEC.gov feeds (e.g. "SEC Press Releases") get the contact-bearing
        # UA the rest of this repo already uses for EDGAR
        # (config.smart_money.user_agent) — see the module-level
        # SEC_USER_AGENT comment. pipeline.py passes
        # config.smart_money.user_agent explicitly at construction; this
        # default only covers a NewsDataProvider built without a config.
        self.sec_user_agent = sec_user_agent
        # Per-symbol news (2026-08-30 owner decision — see the audit block
        # above `USER_AGENT`). pipeline.py passes every one of these
        # explicitly from config.news.per_symbol_* at construction time;
        # these defaults only cover a NewsDataProvider built without a
        # config (tests, scripts) and mirror NewsConfig's own defaults
        # (src/config.py) so the two never silently drift apart.
        self.per_symbol_enabled = bool(per_symbol_enabled)
        self.per_symbol_feed_template = per_symbol_feed_template
        # Returns the broker whose exchange calendar fixes the per-symbol
        # window (headlines since the previous session's close). A callable,
        # not the broker, because pipeline.py builds this provider before
        # its broker. None (tests, scripts) falls back to the lookback.
        self.session_broker = session_broker
        per_symbol_rps = min(10.0, max(0.1, float(per_symbol_requests_per_second)))
        self.per_symbol_request_interval_s = 1.0 / per_symbol_rps

    def fetch_news(
        self,
        lookback_hours_override: int | None = None,
        symbols: list[str] | None = None,
    ) -> tuple[list[NewsItem], NewsCoverage]:
        """Fetch recent news from all RSS feeds, plus an optional per-symbol
        pass.

        Default lookback is 24h, fine for Tue-Fri morning runs. On Monday
        morning the previous trading day was Friday, so a 24h window
        misses ~72h of weekend news (Fed pressers, geopolitical events,
        earnings pre-announcements all routinely land on weekends). The
        Monday-aware path: if today is Monday, automatically extend the
        lookback to cover the gap. The caller can also override via
        `lookback_hours_override` for hand-tuning / replay scenarios.

        `symbols`, when given, is an already-ordered list of tickers to also
        fetch individually from Yahoo Finance's per-symbol RSS (2026-08-30
        owner decision — see the audit block above `USER_AGENT`). This
        provider is deliberately kept portfolio-agnostic: the caller (the
        pipeline, which knows held positions and the run's admitted
        candidates) decides WHICH symbols and in what order — positions
        before candidates is the documented convention, see
        TradingPipeline._run_news_update. Every name given is fetched, one
        throttled request each (2026-10-19: the old 15-name cap starved the
        seat of names the chart seat had rated), and each name keeps every
        headline published since the previous trading session's close — see
        `_per_symbol_cutoff`. Pass `None` or `[]` (or set
        `per_symbol_enabled=False`) for zero added requests and byte-identical
        behavior to a NewsDataProvider that has never heard of per-symbol
        fetching.

        Returns `(items, coverage)`. Before 2026-08-28 this returned only
        `items`, and a feed that failed simply contributed zero of them —
        indistinguishable from a feed that fetched fine and had nothing new
        to say. `NewsCoverage` is the fix: every caller now gets an explicit
        accounting of how many feeds were configured, how many actually
        returned data, and which ones failed and why, so "the wires were
        read" and "two wires returned nothing" can never again look the
        same downstream. Per-symbol feeds are folded into this SAME
        `NewsCoverage` (each symbol is just another named feed) rather than
        a parallel reporting path, so a per-symbol feed that starts failing
        shows up exactly the same way a dead general wire does.
        """
        if lookback_hours_override is not None:
            effective_lookback = lookback_hours_override
        else:
            today = et_now()
            # weekday(): Monday=0 .. Sunday=6. Monday morning needs to
            # cover Fri close → Mon morning ≈ 72h. Tue after a Mon
            # holiday would also benefit but holiday awareness lives in
            # broker.is_trading_day; that's overkill here — Monday is
            # the 95% case.
            if today.weekday() == 0:  # Monday
                effective_lookback = max(self.lookback_hours, 72)
                logger.info(
                    "fetch_news: Monday detected — extending lookback from %dh to %dh to cover weekend news",
                    self.lookback_hours,
                    effective_lookback,
                )
            else:
                effective_lookback = self.lookback_hours
        cutoff = utc_now() - timedelta(hours=effective_lookback)
        all_items: list[NewsItem] = []
        succeeded = 0
        failures: list[FeedFailure] = []

        for source_name, url in self.feeds.items():
            try:
                items = self._fetch_feed(source_name, url, cutoff)
                all_items.extend(items)
                succeeded += 1
            except Exception as e:
                # This is the ONE place a dead feed becomes visible. Before
                # 2026-08-28 this branch logged the warning below and moved
                # on — the feed contributed zero items, identically to a
                # feed that fetched fine and simply had no fresh headlines,
                # and nothing past this loop could tell the two apart.
                # `failures` is what makes the difference reach the caller
                # instead of dying here as a log line only.
                logger.warning("Failed to fetch %s: %s", source_name, e)
                reason = str(e) or type(e).__name__
                failures.append(
                    FeedFailure(
                        name=source_name,
                        reason=reason[:_FAILURE_REASON_MAX_LEN],
                    )
                )

        # Per-symbol pass — every shortlisted name is read (2026-10-19).
        per_symbol = self._fetch_per_symbol(symbols, cutoff, failures)
        all_items.extend(per_symbol.items)
        succeeded += len(per_symbol.fetched)
        per_symbol_configured = per_symbol.configured

        # Deduplicate by title similarity and sort by time (newest first).
        # Per-symbol items flow through the SAME dedup as everything else —
        # a per-symbol story that a general wire already carried collapses
        # into one cluster like any other syndicated copy, rather than
        # reading as independent confirmation. See src/data/news_dedup.py.
        deduped = self._deduplicate(all_items)
        deduped.sort(key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        coverage = NewsCoverage(
            configured=len(self.feeds) + per_symbol_configured,
            succeeded=succeeded,
            failed=failures,
            per_symbol_gaps=per_symbol.gaps(deduped),
        )

        logger.info(
            "Fetched %d news items from %d/%d sources (%d per-symbol; after dedup from %d); coverage=%s%s",
            len(deduped),
            succeeded,
            len(self.feeds) + per_symbol_configured,
            per_symbol_configured,
            len(all_items),
            coverage.status,
            f" failed={sorted(f.name for f in failures)}" if failures else "",
        )
        return deduped, coverage

    def _throttle_per_symbol(self) -> None:
        """Politeness gate before each per-symbol request — same
        request-interval-from-rate convention as
        SECForm4Provider._get / _RATE_LOCK in src/data/smart_money.py,
        applied to the per-symbol Yahoo endpoint's own (much lower, since
        undocumented) tolerance rather than SEC's. Shared module-level state
        so it throttles across every NewsDataProvider instance in the
        process, not just calls on the same instance."""
        global _PER_SYMBOL_LAST_REQUEST_AT
        with _PER_SYMBOL_RATE_LOCK:
            wait = self.per_symbol_request_interval_s - (time.monotonic() - _PER_SYMBOL_LAST_REQUEST_AT)
            if wait > 0:
                time.sleep(wait)
            _PER_SYMBOL_LAST_REQUEST_AT = time.monotonic()

    def _fetch_per_symbol(
        self, symbols: list[str] | None, cutoff: datetime, failures: list[FeedFailure]
    ) -> "_PerSymbolPass":
        """Fetch every name given, in the caller's order, one throttled
        request each. The throttle bounds the request RATE; the caller's
        shortlist bounds the count. dict.fromkeys dedupes while preserving
        the caller's order. A failure is appended to `failures` — the SAME
        list (and so the same NewsCoverage the "Data degraded" banner reads)
        as the general wires — and recorded as that name's gap reason."""
        result = _PerSymbolPass()
        if not (self.per_symbol_enabled and symbols):
            return result
        ordered = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
        per_symbol_cutoff, result.window_label = self._per_symbol_cutoff(cutoff)
        for symbol in ordered:
            source_name = f"Yahoo Finance ({symbol})"
            url = self.per_symbol_feed_template.format(symbol=symbol)
            result.configured += 1
            try:
                self._throttle_per_symbol()
                items = self._fetch_feed(source_name, url, per_symbol_cutoff)
            except Exception as e:
                logger.warning("Failed to fetch per-symbol feed %s: %s", source_name, e)
                reason = (str(e) or type(e).__name__)[:_FAILURE_REASON_MAX_LEN]
                failures.append(FeedFailure(name=source_name, reason=reason))
                result.failed[symbol] = reason
                continue
            for item in items:
                item.per_symbol = True
                item.symbol = symbol
            result.items.extend(items)
            result.fetched[symbol] = len(items)
        return result

    def _per_symbol_cutoff(self, fallback: datetime) -> tuple[datetime, str]:
        """Start of each name's headline window: the close of the most recent
        trading session before today's ET date, read from the broker's
        exchange calendar (`src.session_calendar.session_for`). Morning and
        midday therefore read since the last close; evening reads all of today.

        The walk back stops at the first day of the previous calendar month —
        the same structural bound `session_calendar._calendar_is_answering`
        uses (an exchange always trades within a month). With no broker, or a
        calendar that is not answering, the general `fallback` lookback is
        used and the label says so, so the gap reasons stay truthful.
        """
        broker = self.session_broker() if self.session_broker is not None else None
        if broker is None:
            return fallback, f"{fallback:%Y-%m-%d %H:%M} UTC (lookback; no exchange calendar)"
        today = et_now().date()
        floor = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        probe = today - timedelta(days=1)
        try:
            while probe >= floor:
                session = session_for(broker, probe)
                if session.is_trading_day and session.close_at is not None:
                    close_utc = session.close_at.astimezone(timezone.utc)
                    return close_utc, f"previous close {close_utc:%Y-%m-%d %H:%M} UTC"
                probe -= timedelta(days=1)
        except SessionCalendarUnavailable as exc:
            logger.warning("per-symbol news window: exchange calendar unreadable (%s); using lookback", exc)
            return fallback, f"{fallback:%Y-%m-%d %H:%M} UTC (lookback; exchange calendar unreadable)"
        logger.warning("per-symbol news window: no session close found back to %s; using lookback", floor)
        return fallback, f"{fallback:%Y-%m-%d %H:%M} UTC (lookback; no session close found)"

    def _fetch_feed(self, source_name: str, url: str, cutoff: datetime) -> list[NewsItem]:
        """Fetch and parse a single RSS feed.

        Raises on a genuine failure (network/HTTP error, or a document
        feedparser can't parse at all) rather than swallowing it into an
        empty list. That distinction matters: `fetch_news` is the only place
        that builds `NewsCoverage`, and it can only tell "this feed is
        broken" apart from "this feed has nothing new right now" if broken
        actually raises. A feed that parses fine and simply has zero entries
        (or zero entries newer than `cutoff`) is NOT a failure and still
        returns `[]` normally below — that is a real, healthy "nothing new".
        """
        ua = self.sec_user_agent if _is_sec_gov(url) else USER_AGENT
        req = Request(url, headers={"User-Agent": ua})
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read()

        feed = feedparser.parse(raw)

        if feed.bozo and not feed.entries:
            # A document feedparser could not make sense of at all (HTML
            # error page served with a 200, truncated XML, etc.) — this is
            # the fetch equivalent of an HTTP error, not "no stories today".
            raise ValueError(f"unparseable feed: {feed.bozo_exception}")

        items = []
        for entry in feed.entries:
            published = self._parse_date(entry)
            if published and published < cutoff:
                continue

            title = entry.get("title", "").strip()
            if not title:
                continue

            summary = entry.get("summary", entry.get("description", "")).strip()
            # Truncate long summaries
            if len(summary) > 300:
                summary = summary[:297] + "..."

            items.append(
                NewsItem(
                    title=title,
                    summary=summary,
                    source=source_name,
                    published=published,
                    link=entry.get("link", ""),
                )
            )

        return items

    def _parse_date(self, entry) -> datetime | None:
        """Parse the published date from a feed entry."""
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            try:
                from calendar import timegm

                ts = timegm(parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OverflowError):
                return None
        return None

    @staticmethod
    def _normalize_link(link: str) -> str:
        """Normalize an article URL for cross-source dedup.

        Thin wrapper over :func:`src.data.news_dedup.normalize_link`, kept as
        a method because callers and tests reference it here.
        """
        return normalize_link(link)

    def cluster_news(self, items: list[NewsItem]) -> list[NewsCluster]:
        """Group articles reporting the same underlying event.

        This is the cascade's stage 1. Returns clusters rather than bare
        items so a future novelty stage (stage 2) can score whole events
        against a rolling buffer without re-deriving the grouping — see
        ``src/data/news_dedup.py`` for the full cascade note.
        """
        return cluster_news(items)

    def _deduplicate(self, items: list[NewsItem]) -> list[NewsItem]:
        """Collapse syndicated copies of one event down to one item each.

        Returns the cluster representatives, each stamped with
        ``collapsed_count`` / ``source_count`` so downstream consumers can
        see how widely a story was carried without mistaking that breadth
        for independent corroboration.

        The old implementation dropped duplicates via word-Jaccard > 0.7 on
        titles and discarded the count entirely. That threshold was measured
        against real cached headlines and does not separate: several genuine
        duplicates scored below distinct same-day events. See
        ``src/data/news_dedup.py`` for the replacement method and its
        calibration.
        """
        clusters = self.cluster_news(items)
        representatives: list[NewsItem] = []
        for cluster in clusters:
            rep = cluster.representative
            rep.collapsed_count = cluster.collapsed_count
            rep.source_count = cluster.source_count
            representatives.append(rep)
        collapsed = len(items) - len(representatives)
        if collapsed:
            logger.info(
                "news dedup: %d article(s) collapsed into %d event(s) (from %d raw)",
                collapsed,
                len(representatives),
                len(items),
            )
        return representatives

    def tag_symbol_mentions(self, items: list[NewsItem], universe: list[str]) -> dict[str, list[NewsItem]]:
        """Tag which news items mention symbols from the universe. Uses word-boundary matching."""
        import re

        # Short symbols (1-3 chars) are prone to false positives; require word boundaries
        patterns: dict[str, re.Pattern] = {}
        for s in universe:
            sym = s.upper()
            patterns[sym] = re.compile(r"\b" + re.escape(sym) + r"\b")
        result: dict[str, list[NewsItem]] = {}
        for item in items:
            text = f"{item.title} {item.summary}".upper()
            for sym, pat in patterns.items():
                if pat.search(text):
                    result.setdefault(sym, []).append(item)
        return result

    def format_for_prompt(self, items: list[NewsItem], max_items: int = 50) -> str:
        """Format news items into a text block for the LLM prompt.

        `max_items` bounds the GENERAL-wire items only. Per-symbol items are
        all kept: each name's set is already bounded by its time window (since
        the previous close) and by the feed's own length, and a count shared
        across names would let one busy name starve the rest (2026-10-19).
        """
        if not items:
            return "No recent news available."

        limited: list[NewsItem] = []
        general_kept = 0
        for item in items:
            if not getattr(item, "per_symbol", False):
                if general_kept >= max_items:
                    continue
                general_kept += 1
            limited.append(item)
        lines = []
        for item in limited:
            time_str = item.published.strftime("%Y-%m-%d %H:%M UTC") if item.published else "unknown"
            # Syndication breadth is rendered explicitly. Without it, the
            # analyst either sees N copies of one story (and reads them as N
            # confirmations) or sees one copy with no idea how widely it was
            # carried. Naming the count, and naming what it does NOT mean, is
            # the whole point of the dedup stage.
            breadth = ""
            collapsed = getattr(item, "collapsed_count", 1) or 1
            if collapsed > 1:
                sources = getattr(item, "source_count", 1) or 1
                breadth = (
                    f" [carried by {sources} outlet{'s' if sources != 1 else ''}"
                    f", {collapsed} articles - syndication breadth, "
                    f"NOT independent corroboration]"
                )
            lines.append(f"[{item.source}] ({time_str}) {item.title}{breadth}")
            if item.summary:
                lines.append(f"  > {item.summary}")

        return "\n".join(lines)
