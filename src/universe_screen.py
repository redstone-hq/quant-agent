"""Universe expansion and pruning — THE admission screen.

Built 2026-09-19 from the design agreed with the owner on 2026-09-01
(docs/INCIDENT_HISTORY.md, "Universe expansion and pruning — DESIGN AGREED
WITH THE OWNER 2026-09-01"). Owner, restating it 2026-09-19: add stocks that
pass standard desk filters — "a certain amount of liquidity, a certain
minimum price ... filter out the garbage, the penny stocks, the highly
speculative" — and remove the ones that stop passing.

ONE SCREEN. Every path that can put a symbol outside the hand-typed
`trading.universe` in front of the desk calls `screen_symbol` (or its batch
form in `run_screen`): the weekly broker-asset-list screen, the SEC Form 4
smart-money side door and the Phase 9 nomination side door. Behind
`universe_screen.enabled`, default OFF; with it off every caller keeps its
pre-existing behaviour.

THE CRITERIA, and where each threshold comes from (every number is in
`config/number_ledger.yaml`; nothing below is chosen here):

  liquidity   FIRST filter (owner ruling 2026-10-13): S&P's index-entry
              float-adjusted liquidity ratio (FALR). Annual dollar value
              traded = average closing price x total volume over the 365
              calendar days before the evaluation date (the last bar);
              FALR = that / float-adjusted market capitalisation, and a
              name passes at FALR >= 0.75 (S&P Composite 1500 addition
              rule since 2023-01-04; definition per S&P's 2024-12-09 FALR
              clarification). No float-share source exists in the desk's
              data, so the denominator is TOTAL market capitalisation
              (shares outstanding x price, Nasdaq listing): total >= float,
              so the ratio is never overstated on that side (stricter).
              Volume is Alpaca SIP consolidated volume, which includes
              off-exchange (dark-pool / TRF) prints S&P excludes -- that
              side overstates the ratio (looser). Both are known limits.
              Drops, each with its reason: ratio below 0.75; market cap
              absent from a readable profile (`market_cap_unknown`); under 365
              calendar days of bars. An unreadable profile stays the
              inconclusive `profile_unavailable` (never admitted). Not
              applied to an ETF: a fund has no float-adjusted market cap.
              Replaces any dollar-volume floor; the screen has none.

  asset       broker says the symbol exists, is `active`, `tradable`, a US
              equity on a listed exchange, and is not a warrant, unit,
              right, preferred or depositary receipt. Delisted /
              inactive / untradable is PERMANENT: an admitted name is
              removed at once, no second chance.
  history     a year of daily bars: the first bar is at least a calendar
              year before the last, AND there are enough bars for the
              200-session average plus the 10-session slope the analyst
              reads (`technical.LONGEST_INDICATOR_WINDOW` +
              `context._SLOPE_LOOKBACK`).
  price       last close >= `min_price_usd` ($5, SEC Rule 3a51-1(d): a
              security priced at five dollars or more is not a penny stock).
  spread      the Corwin & Schultz (J. Finance 2012) high-low spread
              estimate over that year of bars. Half of it — the cost of
              crossing from mid to the far side — must not exceed the
              desk's own entry-slippage belt (`execution.
              max_entry_slippage_bps`): a name whose ordinary half-spread
              is already past the belt is one the execution stage would
              refuse to cross. Replaces the $10M dollar-volume floor, as the
              design required. Estimated from bars, not quotes, because
              this account sees IEX quotes only and those are routinely
              absurd (src/pipeline_stages.py, the CCJ 15%-spread note).
  traded      the name TRADED on every session of that screening year: no
              bar with zero volume or high == low, and no session missing
              from its bars (checked against the dates any name in the same
              batch has a bar on, so no holiday calendar is needed; a lone
              name with no batch is checked for the first two only). Why:
              the spread estimate above floors a no-trade day at zero and
              averages it in, so a thinly traded name looks cheap. Alpaca
              omits a day with no trades rather than returning a flat bar,
              so a skipped session is a missing bar. Needs no new number.
  volatility  ATR(14) / price < 1 / the widest stop multiple the desk's
              own rules can reach (`portfolio_constructor.
              widest_reachable_stop_atr_multiple`, 1/2.5 = 40% at
              today's ratified settings). At or past it the widest
              legitimate stop — that multiple of ATR under price — sits at
              or below ZERO, so no stop this desk would place can be
              placed at all and the name fails by construction. Nothing is
              chosen here: the bound is arithmetic, and it moves by itself
              if the owner-ruled stop multiple moves.
  size        market capitalisation >= `min_market_cap_usd` ($30M, the
              Russell US indexes' eligibility floor).
  sector      resolves to a real sector — the sector cap needs one. Kept
              from the pre-existing side-door gate.
  takeover    no pending acquisition: the issuer is not the TARGET of a
              takeover filing (merger proxy / tender offer) still open,
              i.e. not followed by a termination (8-K Item 1.02) or a
              close (Form 25, Form 15, 8-K Item 2.01). The role comes
              from the filing header or the golden-parachute vote
              (`src/data/sec_filings.py`); a bidder is never pending and
              an unattributable role is inconclusive.
  fund        an exchange-traded fund (Yahoo `quoteType == "ETF"`; owner
              ruling 2026-10-09: expand the universe including ETFs) is
              admitted ONLY as an unlevered equity fund. Refused: a
              leveraged or inverse fund (SEC Rule 18f-4(a), 17 CFR
              270.18f-4(a)) -- the order layer's leverage table counts an
              unlisted fund as 1x, so the screen is where one must stop;
              a bond, money-market, ultrashort, treasury or other
              non-equity category -- the owner's mandate forbids parking
              cash in bonds or T-bills (SGOV files as "Ultrashort Bond");
              and a fund with no category at all (fail closed). The
              fund's category string is its sector key. Company size and
              takeover do not apply to a fund and are recorded as "not
              applicable: fund"; liquidity is the same spread check every
              stock passes. Any other fund (Yahoo quote type not EQUITY or
              ETF, or a fund-named security Yahoo calls EQUITY) is refused
              as not a common share, as before.

Short borrow is NOT an admission criterion (owner ruling 2026-10-09): a
name the desk can only buy is still an opportunity. Whether a short can be
borrowed is checked at the order seam, where a short is placed.

NOT criteria, on the owner's ruling: no earnings-date requirement, no
maximum price.

A screen that could not READ something (a data outage) is INCONCLUSIVE: it
admits nothing and counts neither for nor against an admitted name.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from src.data.fund_category import is_leveraged_fund_name
from src.quantities import sp_traded_value

logger = logging.getLogger(__name__)

#: Calendar days of daily bars requested per symbol. A fetch size, not a
#: threshold: it only has to exceed the one-year history test with room for
#: holidays and a missing first week. Ledger: not-trade-governing.
HISTORY_FETCH_DAYS = 400

#: S&P float-adjusted liquidity ratio floor for addition to the S&P
#: Composite 1500 ("must be greater than or equal to 0.75 at the time of
#: addition", S&P DJI 2023-01-04). Ledger: sourced.
SP_MIN_LIQUIDITY_RATIO = 0.75

#: S&P's FALR window: "the 365 calendar days prior to the evaluation date"
#: (S&P DJI FALR clarification, 2024-12-09). Ledger: sourced.
SP_LIQUIDITY_LOOKBACK_DAYS = 365

#: Exchanges a listed US common stock trades on, as Alpaca spells them
#: (lower-cased; the SDK's enum text is `assetexchange.<name>`). Same set as
#: the pre-existing side-door gate in `AlpacaBroker.
#: get_transient_equity_eligibility`. OTC is deliberately absent.
LISTED_EXCHANGES = frozenset({"nyse", "nasdaq", "amex", "arca", "bats"})

#: Ticker suffixes that mark a warrant, unit or right (design: "exclude
#: warrants, units and rights"; a delisted warrant once reached the data
#: layer and caused a recursion fault). Same list as the pre-existing gate.
NON_EQUITY_SUFFIXES = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT", "-WS", "-U", "-RT")

#: Security-name words that mark something other than a common share. Word
#: boundaries matter: the pre-existing gate's `" unit"` substring also
#: matches "First United", which this does not.
_NON_COMMON_NAME = re.compile(
    r"\b(warrants?|units?|rights?|preferred|pfd|notes?|debentures?)\b",
    re.IGNORECASE,
)

#: A foreign ADR is common equity in a depositary wrapper (owner ruling
#: 2026-10-10: ADRs are in the universe). Plain "Depositary Shares" is NOT
#: matched: on the real listings it is the wrapper of a preferred (CDZIP,
#: MNSBP, WAFDP, EQH$A, MET$E, TFC$I) unless it says "Global".
_ADR_NAME = re.compile(
    r"\b(american\s+deposit\w*|global\s+deposit\w*|ads|adr)\b",
    re.IGNORECASE,
)
_DEPOSITARY_WORD = re.compile(r"\bdeposit\w*\b", re.IGNORECASE)

#: Markers that keep a depositary name out even when it says ADS/ADR. "Series"
#: alone is not one when it names an underlying share class ("8 Series B
#: shares"); "right to receive" is how ADS describe themselves, not a right.
_ADR_REFUSE = re.compile(
    r"\b(warrants?|units?|rights?(?!\s+to\b)|preferred|pfd|notes?|debentures?)\b"
    r"|%|\bseries\b(?!\s+\w+\s+(?:ordinary\s+)?shares?\b)",
    re.IGNORECASE,
)


def is_non_common_name(name: str | None) -> bool:
    """True when the security name marks something other than a common share.

    One rule for the screen and the Nasdaq listing. An ADR/ADS name is common
    equity unless it also marks a preferred, note, warrant, unit or right; any
    other depositary name (plain "Depositary Shares") is a preferred wrapper.
    """
    text = str(name or "")
    if _ADR_NAME.search(text):
        return bool(_ADR_REFUSE.search(text))
    return bool(_NON_COMMON_NAME.search(text) or _DEPOSITARY_WORD.search(text))


#: Security-name words that mark a fund. A fund is not refused at the asset
#: stage: Yahoo's `quoteType` decides at the profile stage whether it is an
#: exchange-traded fund (screened as one) or another kind (refused).
_FUND_NAME = re.compile(r"\b(etf|exchange traded fund|fund shares)\b", re.IGNORECASE)

#: Yahoo `quoteType` values the screen admits. Anything else (MUTUALFUND,
#: CLOSEDEND, INDEX, ...) is not a listed common share or ETF.
EQUITY_QUOTE_TYPE = "EQUITY"
ETF_QUOTE_TYPE = "ETF"

#: Category words that mark a leveraged or inverse fund, the SEC Rule
#: 18f-4(a) definition (https://www.law.cornell.edu/cfr/text/17/270.18f-4):
#: a fund seeking a multiple, or the inverse, of an index's return. Yahoo
#: reports Morningstar categories, which file these under "Trading--"
#: ("Trading--Leveraged Equity", "Trading--Inverse Equity", ...).
LEVERAGED_CATEGORY_WORDS = ("trading--", "leveraged", "inverse")

#: Category words that mark a fund that is not an equity fund: fixed
#: income, cash and the other non-stock families. The owner's 2026-10-09
#: mandate (docs/OUTCOME.md): equity funds only, no parking in bonds or
#: T-bills. Matched against the Morningstar category name Yahoo reports
#: (e.g. SGOV "Ultrashort Bond", TLT "Long Government").
NON_EQUITY_CATEGORY_WORDS = (
    "bond",
    "money market",
    "ultrashort",
    "treasury",
    "government",
    "muni",
    "bank loan",
    "inflation",
    "preferred",
    "allocation",
    "target-date",
    "commodit",
    "currency",
    "digital asset",
)

#: What a fund records for the checks that do not apply to it.
NOT_APPLICABLE_FUND = "not applicable: fund"

#: SEC submission types that mean the issuer is the subject of a pending
#: acquisition: merger proxies and information statements (Schedule 14A /
#: 14C "M" variants), the target's tender-offer response (SC 14D9 and its
#: pre-commencement SC14D9C), a third-party tender offer (SC TO-T, SC TO-C)
#: and a going-private transaction (SC 13E3). SEC lists these as proxy and
#: Williams Act submission types:
#: https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/understand-edgarlink-online-submission-types
#: Spellings checked against EDGAR full-text search on 2026-09-19 (each
#: returned live filings in 2026-06..09).
TAKEOVER_FORMS = frozenset(
    {
        "PREM14A",
        "DEFM14A",
        "PREM14C",
        "DEFM14C",
        "SC 14D9",
        "SC14D9C",
        "SC TO-T",
        "SC TO-C",
        "SC 13E3",
    }
)

#: 8-K item "Termination of a Material Definitive Agreement"
#: (https://www.sec.gov/files/form8-k.pdf). A later 1.02 is read as the deal
#: having died. It can also be an unrelated agreement — that errs toward
#: admitting a name, and is recorded as a known limit.
TERMINATION_ITEM = "1.02"

#: A deal is CLOSED when any of these follows the takeover filing: Form 25
#: (delisting), Form 15 (deregistration, any suffix) or 8-K item 2.01
#: "Completion of Acquisition or Disposition of Assets". A closed deal means
#: the name is gone or going from the exchange, which the asset check
#: handles; it is no longer "pending".
CLOSING_FORMS = frozenset({"25", "25-NSE", "15", "15-12B", "15-12G", "15-15D"})
CLOSING_ITEM = "2.01"

#: Role of the issuer in its latest open takeover filing, read from the
#: filing header (SUBJECT COMPANY = target, FILED BY = bidder) or, for a
#: self-filed merger proxy, from the Rule 14a-21(c) golden-parachute vote.
#: See `src/data/sec_filings.py`. A bidder is never pending. A role that
#: cannot be attributed (a joint proxy) is INCONCLUSIVE, never dropped and
#: never admitted. A row with no locator, or a screen with no role reader,
#: is also undetermined: the form alone never decides.
ROLE_TARGET = "target"
ROLE_UNDETERMINED = "undetermined"

#: Failure codes that mean "could not read", never "failed the screen".
INCONCLUSIVE = frozenset(
    {
        "asset_lookup_failed",
        "market_data_unavailable",
        "profile_unavailable",
        "fund_category_not_listed",
        "takeover_lookup_failed",
        "takeover_role_undetermined",
        "screen_deadline",
    }
)

#: Failure codes that remove an admitted name immediately.
PERMANENT = frozenset({"asset_not_found", "asset_inactive", "asset_not_tradable"})

#: Plain words for every code, for the owner's morning message.
PLAIN_REASON = {
    "asset_not_found": "the broker no longer lists it (delisted)",
    "asset_inactive": "the broker marks it inactive (delisted)",
    "asset_not_tradable": "the broker marks it not tradable (halted)",
    "asset_lookup_failed": "the broker could not be read",
    "not_us_equity": "not a US stock",
    "unsupported_exchange": "not on a main US exchange",
    "not_common_stock": "a warrant, unit, right, preferred, receipt or non-ETF fund",
    "fund_category_not_listed": "a fund with no category on record yet (the nightly pass reads it from Yahoo)",
    "fund_category_unknown": "a fund with no category on record",
    "leveraged_or_inverse_fund": "a leveraged or inverse fund",
    "not_equity_fund": "a bond, cash or other non-stock fund",
    "liquidity_below_sp_ratio": "trades too little for its size (S&P: a year's trading under 0.75x its market value)",
    "liquidity_history_short": "under a year of trading to measure its trading-to-size ratio",
    # No longer screen criteria (2026-10-09); kept so older recorded
    # events still read in plain words.
    "not_shortable": "cannot be sold short",
    "hard_to_borrow": "hard to borrow for a short",
    "no_price_history": "no price history",
    "insufficient_history": "less than a year of price history",
    "price_below_minimum": "a penny stock (under $5)",
    "spread_too_wide": "too costly to trade (wide bid-ask spread)",
    "not_traded_every_session": "did not trade on every session of the past year",
    "spread_unmeasurable": "its trading cost could not be measured",
    "volatility_above_ceiling": "so volatile the desk's widest stop would sit at or below zero",
    "company_too_small": "company too small (under $30M)",
    "market_cap_unknown": "company size unknown",
    "unresolved_sector": "no sector",
    "pending_takeover": "being taken over",
    "takeover_status_unknown": "no SEC filing record to check for a takeover",
    "market_data_unavailable": "price data could not be read",
    "profile_unavailable": "company data could not be read",
    "takeover_lookup_failed": "SEC filings could not be read",
    "takeover_role_undetermined": "role undetermined",
    "screen_deadline": "ran out of time",
    "passed": "passes every check",
}


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenThresholds:
    min_price_usd: float
    min_market_cap_usd: float
    max_half_spread_bps: float
    max_atr_fraction: float
    min_history_bars: int

    @classmethod
    def from_config(cls, config, constructor_cfg=None) -> "ScreenThresholds":
        """`constructor_cfg` is the LIVE `ConstructorConfig` the desk places
        stops with; the pipeline passes `self.portfolio_constructor.cfg`.

        It is a parameter rather than something re-resolved here because the
        volatility ceiling below is 1 / the widest stop the desk can place,
        and "the desk" means the constructor that will actually place it. The
        two used to resolve the base multiple by DIFFERENT routes -- the
        screen straight off `config.risk.min_stop_atr_multiple`, the
        constructor through `pipeline.build_constructor_config`'s
        `_risk_setting` (whose own fallback is a different number) -- and
        neither passed the setup/regime scalers at all, so the two agreed
        only because both happened to land on the class defaults. Passing the
        live object removes the second route. When it is absent (tests, and
        any caller with no constructor to hand) the old route is used and
        the values are the class defaults.
        """
        from src.data.context import _SLOPE_LOOKBACK
        from src.data.technical import LONGEST_INDICATOR_WINDOW

        # Imported here, not at module scope: `portfolio_constructor` is a
        # heavy module and the screen is otherwise independent of it.
        from src.portfolio_constructor import widest_reachable_stop_atr_multiple

        screen = config.universe_screen
        return cls(
            min_price_usd=float(screen.min_price_usd),
            min_market_cap_usd=float(screen.min_market_cap_usd),
            max_half_spread_bps=float(config.execution.max_entry_slippage_bps),
            # NOT a chosen ceiling and no longer a borrowed one. Board
            # item 185: this used to read `0.5 / min_stop_atr_multiple`,
            # borrowing the midday typo guard's flat half-of-price floor
            # and dividing it by the BASE stop multiple (2.5) rather than
            # the widest one the desk could then reach (3.00). Since
            # 2026-10-09 no scaler widens the stop (owner ruling 2026-10-04:
            # 2.5 ATR from the stock's own ATR only), so widest == base.
            # Both halves are gone:
            # the guard now reads the instrument, and this ceiling is the
            # arithmetic point where the widest legitimate stop would sit
            # at or below zero.
            max_atr_fraction=1.0
            / (
                widest_reachable_stop_atr_multiple(
                    constructor_cfg.min_stop_atr_multiple,
                )
                if constructor_cfg is not None
                else widest_reachable_stop_atr_multiple(
                    float(config.risk.min_stop_atr_multiple),
                )
            ),
            min_history_bars=int(LONGEST_INDICATOR_WINDOW + _SLOPE_LOOKBACK),
        )


# --------------------------------------------------------------------------
# Measures
# --------------------------------------------------------------------------


def corwin_schultz_spread(bars: list) -> float | None:
    """Mean Corwin–Schultz (2012) bid-ask spread estimate, as a fraction.

    Corwin & Schultz, "A Simple Way to Estimate Bid-Ask Spreads from Daily
    High and Low Prices", J. Finance 67(2), 719-760
    (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1106193). For each
    pair of consecutive days: beta = ln(H1/L1)^2 + ln(H2/L2)^2,
    gamma = ln(max(H1,H2)/min(L1,L2))^2,
    alpha = (sqrt(2 beta) - sqrt(beta)) / (3 - 2 sqrt 2) - sqrt(gamma / (3 - 2 sqrt 2)),
    S = 2 (e^alpha - 1) / (1 + e^alpha). Day 2's range is shifted by any
    overnight gap past day 1's close, as in the authors' procedure
    (replication of their SAS code:
    https://github.com/ioannisrpt/Corwin_Schultz_2012).

    Negative estimates: the replication offers two treatments — floor each
    two-day estimate at zero and then average (its SPREAD_0), or average the
    raw estimates and floor the AVERAGE at zero (its XSPREAD_0). This uses
    the second. Measured 2026-09-19 on a year of daily bars, the first
    reads AAPL at 25.6 bps half-spread, TSLA 44.4, AMD 51.4 — volatility,
    not trading cost, because flooring each day discards every negative
    that would cancel a positive; the second reads them 0.2, 5.7, 0.0 and
    still reads an illiquid name (ANL) at 64 bps. Averaged over every pair
    in `bars` (the screening year). None when no pair is usable.
    """
    const = 3.0 - 2.0 * math.sqrt(2.0)
    estimates: list[float] = []
    for prev, cur in zip(bars, bars[1:]):
        try:
            h1, l1, c1 = float(prev.high), float(prev.low), float(prev.close)
            h2, l2 = float(cur.high), float(cur.low)
        except (AttributeError, TypeError, ValueError):
            continue
        if min(h1, l1, h2, l2, c1) <= 0 or h1 < l1 or h2 < l2:
            continue
        if c1 < l2:  # gapped up overnight
            h2, l2 = h2 - (l2 - c1), c1
        elif c1 > h2:  # gapped down overnight
            h2, l2 = c1, l2 + (c1 - h2)
        beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
        gamma = math.log(max(h1, h2) / min(l1, l2)) ** 2
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / const - math.sqrt(gamma / const)
        estimates.append(2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)))
    if not estimates:
        return None
    return max(sum(estimates) / len(estimates), 0.0)


def _one_year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 Feb
        return day.replace(year=day.year - 1, day=day.day - 1)


def _field(record, name, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _enum_text(value) -> str:
    """Lower-cased enum text with any SDK prefix (`assetexchange.nyse`) removed."""
    text = str(getattr(value, "value", value) or "").strip().lower()
    for prefix in ("assetexchange.", "assetclass.", "assetstatus."):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def is_non_common_security(symbol: str, name: str) -> bool:
    upper = str(symbol or "").upper()
    return upper.endswith(NON_EQUITY_SUFFIXES) or is_non_common_name(name)


def is_fund_name(name: str) -> bool:
    return bool(_FUND_NAME.search(str(name or "")))


# --------------------------------------------------------------------------
# The criteria
# --------------------------------------------------------------------------


def check_asset(symbol: str, asset) -> list[str]:
    """Broker asset-directory checks. `asset` None = the broker says it does not exist."""
    if asset is None:
        return ["asset_not_found"]
    if _enum_text(_field(asset, "status")) != "active":
        return ["asset_inactive"]
    if not bool(_field(asset, "tradable", False)):
        return ["asset_not_tradable"]
    failures = []
    if _enum_text(_field(asset, "class", _field(asset, "asset_class"))) != "us_equity":
        failures.append("not_us_equity")
    if _enum_text(_field(asset, "exchange")) not in LISTED_EXCHANGES:
        failures.append("unsupported_exchange")
    if is_non_common_security(_field(asset, "symbol", symbol) or symbol, _field(asset, "name", "")):
        failures.append("not_common_stock")
    return failures


def check_bars(
    bars: list, th: ScreenThresholds, reference_dates: Iterable[date] | None = None
) -> tuple[list[str], dict]:
    """History, price, traded-every-session, spread and volatility —
    everything read off the bars. `reference_dates`: sessions known to have
    happened (dates other names have bars on); a screening-year session
    absent from `bars` refuses the name."""
    from src.data.technical import atr_series

    measured: dict[str, Any] = {}
    if not bars:
        return ["no_price_history"], measured
    first, last = bars[0], bars[-1]
    measured["history_bars"] = len(bars)
    measured["first_bar"] = str(getattr(first, "date", ""))
    failures: list[str] = []
    try:
        long_enough = getattr(first, "date") <= _one_year_before(getattr(last, "date"))
    except (AttributeError, TypeError):
        long_enough = False
    if not long_enough or len(bars) < th.min_history_bars:
        failures.append("insufficient_history")
    try:
        price = float(last.close)
    except (AttributeError, TypeError, ValueError):
        return failures + ["no_price_history"], measured
    measured["last_price"] = round(price, 4)
    if price < th.min_price_usd:
        failures.append("price_below_minimum")
    # The screening year only — older bars are not today's trading cost.
    try:
        year_start = _one_year_before(getattr(last, "date"))
        recent = [b for b in bars if getattr(b, "date") >= year_start]
    except (AttributeError, TypeError):
        recent = list(bars)
    dead = [b for b in recent if not (getattr(b, "volume", 0) or 0) or getattr(b, "high", 0) == getattr(b, "low", 0)]
    have = {getattr(b, "date") for b in recent}
    missing = (
        [d for d in set(reference_dates) if d >= year_start and d not in have] if reference_dates is not None else []
    )
    if dead or missing:
        measured["no_trade_days"] = len(dead)
        measured["missing_sessions"] = len(missing)
        failures.append("not_traded_every_session")
    spread = corwin_schultz_spread(recent)
    if spread is None:
        failures.append("spread_unmeasurable")
    else:
        half_bps = spread / 2 * 10_000
        measured["half_spread_bps"] = round(half_bps, 1)
        if half_bps > th.max_half_spread_bps:
            failures.append("spread_too_wide")
    atr = atr_series(bars)
    if len(atr) and price > 0:
        atr_fraction = float(atr[-1]) / price
        measured["atr_pct"] = round(atr_fraction * 100, 2)
        # `>=`, not `>`: at exactly the ceiling the widest legitimate stop
        # sits at exactly zero, which is not a placeable stop either.
        if atr_fraction >= th.max_atr_fraction:
            failures.append("volatility_above_ceiling")
    return failures, measured


def check_liquidity_ratio(bars: list, profile, today: date | None = None) -> tuple[list[str], dict]:
    """S&P float-adjusted liquidity ratio (module docstring, "liquidity").

    The window is the 365 calendar days before the EVALUATION date,
    S&P's wording ("prior to the evaluation date"): `today` when the
    caller gives it, else the last bar's date. Anchored on today, a dead
    name with no bar inside the window records `no_price_history` (its
    true reason), not "trades too little" measured on a stale year.

    `profile` None (could not read) returns no verdict here: the profile
    check records it inconclusive. A fund is not applicable."""
    if profile is None:
        return [], {}
    if isinstance(profile, dict) and str(profile.get("quote_type") or "").strip().upper() == ETF_QUOTE_TYPE:
        return [], {"liquidity_ratio": NOT_APPLICABLE_FUND}
    measured: dict[str, Any] = {"liquidity_cap_basis": "total market cap (no float source; stricter)"}
    cap = profile.get("market_cap_usd") if isinstance(profile, dict) else None
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not math.isfinite(cap) or cap <= 0:
        return ["market_cap_unknown"], measured
    if not bars:
        return ["liquidity_history_short"], measured
    try:
        anchor = today if today is not None else getattr(bars[-1], "date")
        start = anchor - timedelta(days=SP_LIQUIDITY_LOOKBACK_DAYS)
        if getattr(bars[0], "date") > start:
            return ["liquidity_history_short"], measured
        window = [b for b in bars if start < getattr(b, "date") <= anchor]
    except (AttributeError, TypeError, ValueError):
        return ["liquidity_history_short"], measured
    traded = sp_traded_value(window)
    if traded is None:
        # Not one usable bar in the evaluation year: no trading to measure.
        return ["no_price_history"], measured
    ratio = traded / float(cap)
    measured["traded_value_365d_usd"] = round(traded, 0)
    measured["liquidity_ratio"] = round(ratio, 4)
    if ratio < SP_MIN_LIQUIDITY_RATIO:
        return ["liquidity_below_sp_ratio"], measured
    return [], measured


def check_fund(category) -> tuple[list[str], dict]:
    """An ETF: unlevered equity funds only; its category is its sector key."""
    text = str(category or "").strip()
    measured = {
        "fund": True,
        "fund_category": text or None,
        "company_size": NOT_APPLICABLE_FUND,
        "takeover": NOT_APPLICABLE_FUND,
    }
    if not text:
        return ["fund_category_unknown"], measured
    lowered = text.lower()
    if any(word in lowered for word in LEVERAGED_CATEGORY_WORDS):
        return ["leveraged_or_inverse_fund"], measured
    if any(word in lowered for word in NON_EQUITY_CATEGORY_WORDS):
        return ["not_equity_fund"], measured
    measured["sector"] = text
    return [], measured


def check_profile(profile, th: ScreenThresholds, name: str = "") -> tuple[list[str], dict]:
    if profile is None:
        return ["profile_unavailable"], {}
    if not isinstance(profile, dict):
        profile = {}
    quote_type = str(profile.get("quote_type") or "").strip().upper()
    if quote_type == ETF_QUOTE_TYPE:
        # Refuse-only name backstop (src/data/fund_category.py): a "3X",
        # "Ultra" or "Bear" name is refused before its category is looked at.
        # It never admits: a plain name decides nothing.
        if is_leveraged_fund_name(name) or is_leveraged_fund_name(profile.get("name")):
            return ["leveraged_or_inverse_fund"], {
                "fund": True,
                "fund_category": profile.get("category"),
                "leveraged_by_name": True,
                "company_size": NOT_APPLICABLE_FUND,
                "takeover": NOT_APPLICABLE_FUND,
            }
        if profile.get("fund_category_not_listed"):
            return ["fund_category_not_listed"], {}
        return check_fund(profile.get("category"))
    if (quote_type and quote_type != EQUITY_QUOTE_TYPE) or is_fund_name(name):
        return ["not_common_stock"], {}
    failures = []
    measured = {}
    cap = profile.get("market_cap_usd")
    if not isinstance(cap, (int, float)) or cap <= 0:
        failures.append("market_cap_unknown")
    else:
        measured["market_cap_usd"] = float(cap)
        if cap < th.min_market_cap_usd:
            failures.append("company_too_small")
    sector = profile.get("sector") or "Unknown"
    measured["sector"] = sector
    if sector == "Unknown":
        failures.append("unresolved_sector")
    return failures, measured


def _items(items) -> set[str]:
    return {part.strip() for part in str(items or "").split(",")}


def _base_form(form) -> str:
    base = str(form or "").strip().upper()
    return base[:-2] if base.endswith("/A") else base


def latest_open_takeover(filings) -> tuple[str | None, Any]:
    """(date, locator) of the latest takeover filing that no closing or
    termination filing follows; (None, None) when there is none.

    Rows are (form, date, items) or (form, date, items, locator)."""
    latest_deal: tuple[str, Any] | None = None
    latest_end: str | None = None
    for row in filings or []:
        form, filed, items = row[0], row[1], row[2]
        ref = row[3] if len(row) > 3 else None
        base = _base_form(form)
        day = str(filed or "")[:10]
        if base in TAKEOVER_FORMS:
            if latest_deal is None or day >= latest_deal[0]:
                latest_deal = (day, ref)
        elif base in CLOSING_FORMS or (base == "8-K" and _items(items) & {TERMINATION_ITEM, CLOSING_ITEM}):
            latest_end = max(latest_end or day, day)
    if latest_deal is None:
        return None, None
    if latest_end is not None and latest_end >= latest_deal[0]:
        return None, None
    return latest_deal


def pending_takeover(filings, role_of: Callable[[Any], str] | None = None) -> tuple[bool | None, str | None]:
    """(pending, latest takeover filing date) from filing rows.

    pending is True for the target of an open deal, False for no open deal
    or for the bidder, and None when the issuer's role could not be told
    (recorded inconclusive by `check_takeover`). `role_of(locator)` reads
    the role; it may raise, and the caller fails closed."""
    day, ref = latest_open_takeover(filings)
    if day is None:
        return False, None
    if ref is None or role_of is None:
        return None, day  # the form alone never decides
    role = role_of(ref)
    if role == ROLE_TARGET:
        return True, day
    if role == ROLE_UNDETERMINED:
        return None, day
    return False, day


def check_takeover(filings, role_of: Callable[[Any], str] | None = None) -> tuple[list[str], dict]:
    if filings is None:
        return ["takeover_status_unknown"], {}
    pending, filed = pending_takeover(filings, role_of)
    if pending:
        return ["pending_takeover"], {"takeover_filing": filed}
    if pending is None:
        return ["takeover_role_undetermined"], {"takeover_filing": filed}
    return [], {}


# --------------------------------------------------------------------------
# One symbol
# --------------------------------------------------------------------------


@dataclass
class ScreenResult:
    symbol: str
    failures: list[str] = field(default_factory=list)
    measured: dict = field(default_factory=dict)

    @property
    def inconclusive(self) -> bool:
        return any(code in INCONCLUSIVE for code in self.failures)

    @property
    def permanent(self) -> bool:
        return any(code in PERMANENT for code in self.failures)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def reason(self) -> str:
        return self.failures[0] if self.failures else "passed"

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "failures": list(self.failures), "measured": dict(self.measured)}


@dataclass
class ScreenSources:
    """Where the screen reads from. Each callable may raise for "could not read".

    get_asset(symbol) -> record | None   (None = broker says it does not exist)
    get_bars(symbol) -> list[OHLCV]
    get_profile(symbol) -> {"market_cap_usd", "sector"} | None
    get_filings(symbol) -> [(form, filing_date, items[, locator])] | None (None = no SEC issuer)
    get_role(locator) -> "target" | "bidder" | "not_target" | "undetermined"
        (None = no role reader; an open takeover filing is then undetermined)
    """

    get_asset: Callable[[str], Any]
    get_bars: Callable[[str], list]
    get_profile: Callable[[str], dict | None]
    get_filings: Callable[[str], list | None]
    get_role: Callable[[Any], str] | None = None


def screen_symbol(
    symbol: str,
    sources: ScreenSources,
    th: ScreenThresholds,
    *,
    asset: Any = ...,
    bars: list | None = None,
    reference_dates: Iterable[date] | None = None,
    today: date | None = None,
) -> ScreenResult:
    """Run every criterion on one symbol, cheapest first; stop at the first
    failing stage so a name that fails for free costs no network reads.
    `today` is the evaluation date the S&P liquidity window ends on."""
    result = ScreenResult(symbol=symbol)
    if asset is ...:
        try:
            asset = sources.get_asset(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe screen: asset lookup failed for %s: %s", symbol, exc)
            result.failures = ["asset_lookup_failed"]
            return result
    result.failures = check_asset(symbol, asset)
    if result.failures:
        return result
    if bars is None:
        try:
            bars = sources.get_bars(symbol) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe screen: bars failed for %s: %s", symbol, exc)
            result.failures = ["market_data_unavailable"]
            return result
    # The profile is read before the bar checks: the S&P liquidity ratio is
    # the FIRST filter (owner ruling 2026-10-13) and needs market cap.
    try:
        profile = sources.get_profile(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe screen: profile failed for %s: %s", symbol, exc)
        profile = None
    failures, measured = check_liquidity_ratio(bars, profile, today) if bars else ([], {})
    result.measured.update(measured)
    if failures:
        result.failures = failures
        return result
    failures, measured = check_bars(bars, th, reference_dates)
    result.measured.update(measured)
    if failures:
        result.failures = failures
        return result
    failures, measured = check_profile(profile, th, _field(asset, "name", "") or "")
    result.measured.update(measured)
    if failures or measured.get("fund"):
        # A fund has no issuer takeover to check: it stops here either way.
        result.failures = failures
        return result
    try:
        filings = sources.get_filings(symbol)
        failures, measured = check_takeover(filings, sources.get_role)
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe screen: SEC filings failed for %s: %s", symbol, exc)
        result.failures = ["takeover_lookup_failed"]
        return result
    result.measured.update(measured)
    result.failures = failures
    return result


# --------------------------------------------------------------------------
# Persisted state and the prune state machine
# --------------------------------------------------------------------------


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def empty_state() -> dict:
    return {"admitted": {}, "screened": {}, "removed": {}, "events": [], "atr_cross_section": []}


def record_atr_cross_section(state: dict, *, week: str, today: date, symbol: str, measured: dict) -> None:
    """THE RECORDING board item 185 is blocked on.

    The volatility ceiling (`max_atr_fraction`, 1 / the widest reachable stop
    multiple) is an arithmetic non-degeneracy floor and NOT a statement of how
    volatile a name this desk is willing to hold. The published form of a
    volatility eligibility bound is cross-sectional - a quantile of the names
    actually screened - and this desk has never had a cross-section to take a
    quantile of, because the screen has never executed. Everything measured so
    far was measured on names the desk had ALREADY admitted, which says what it
    held and never what it should have refused.

    So: every screened symbol's ATR(14)/price is persisted here, per run, with
    the date and the name. Once this file has runs in it the quantile form has
    a measured distribution behind it and item 185 can close on evidence rather
    than on another derivation attempt. Nothing reads this back yet; it is a
    recording, deliberately, and it governs no trade.

    Retention is deliberately unbounded: a cap would be another chosen number,
    and the distribution is the point.
    """
    atr_pct = (measured or {}).get("atr_pct")
    if atr_pct is None:
        return
    runs = state.setdefault("atr_cross_section", [])
    stamp = today.isoformat()
    if not runs or runs[-1].get("date") != stamp or runs[-1].get("week") != week:
        runs.append({"date": stamp, "week": week, "readings": {}})
    runs[-1]["readings"][symbol] = float(atr_pct)


class UniverseStore:
    """`<data_dir>/universe_state.json` — the admitted set and its history.

    admitted[SYM]  = {admitted_on, status: active|flagged, flagged_on,
                      last_screen_week, last_screened_on, failures,
                      measured, last_offered_on}
    screened[SYM]  = {week, failures}   (candidates that did not pass)
    removed[SYM]   = {removed_on, reason}
    events         = changes not yet shown in a morning message
    """

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "universe_state.json"

    def load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("universe state unreadable at %s: %s", self.path, exc)
            raw = None
        state = empty_state()
        if isinstance(raw, dict):
            for key in state:
                if isinstance(raw.get(key), type(state[key])):
                    state[key] = raw[key]
        return state

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".universe.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _event(state: dict, action: str, symbol: str, reasons: list[str], today: date, measured=None):
    event = {
        "date": today.isoformat(),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "symbol": symbol,
        "reasons": list(reasons),
    }
    if measured:
        event["measured"] = dict(measured)
    state["events"].append(event)
    logger.info(
        "UNIVERSE_CHANGE %s %s: %s",
        action.upper(),
        symbol,
        ", ".join(reasons) or "passed",
    )
    return event


def apply_result(
    state: dict,
    result: ScreenResult,
    *,
    today: date,
    held: set[str],
) -> dict | None:
    """Fold one screen result into the state. Returns the event, if any.

    Admitted name: pass clears a flag; a PERMANENT failure removes it at once;
    any other failure flags it the first week and removes it the second
    consecutive week. A held name is never removed — the removal is deferred
    and reported, because removing it would cut a live position off from
    analysis while its stop still sits at the broker.
    Candidate: pass admits it; a failure is cached for the week.
    Inconclusive (a read failed): nothing changes, and it is re-read next run.
    """
    symbol = result.symbol
    week = iso_week(today)
    admitted = state["admitted"].get(symbol)
    if result.inconclusive:
        return None
    if admitted is None:
        if result.passed:
            state["admitted"][symbol] = {
                "admitted_on": today.isoformat(),
                "status": "active",
                "flagged_on": None,
                "last_screen_week": week,
                "last_screened_on": today.isoformat(),
                "failures": [],
                "measured": dict(result.measured),
                "last_offered_on": None,
            }
            state["screened"].pop(symbol, None)
            state["removed"].pop(symbol, None)
            return _event(state, "added", symbol, ["passed"], today, result.measured)
        state["screened"][symbol] = {"week": week, "failures": list(result.failures)}
        return None

    already_this_week = admitted.get("last_screen_week") == week
    if already_this_week and not result.permanent:
        return None
    admitted.update(
        last_screen_week=week,
        last_screened_on=today.isoformat(),
        failures=list(result.failures),
        measured=dict(result.measured),
    )
    if result.passed:
        if admitted.get("status") == "flagged":
            admitted.update(status="active", flagged_on=None)
            return _event(state, "cleared", symbol, ["passed"], today)
        return None
    remove = result.permanent or admitted.get("status") == "flagged"
    if not remove:
        admitted.update(status="flagged", flagged_on=today.isoformat())
        return _event(state, "flagged", symbol, result.failures, today, result.measured)
    if symbol in held:
        admitted.update(status="flagged", flagged_on=admitted.get("flagged_on") or today.isoformat())
        return _event(state, "removal_deferred_held", symbol, result.failures, today)
    del state["admitted"][symbol]
    state["removed"][symbol] = {"removed_on": today.isoformat(), "reason": result.reason}
    return _event(state, "removed", symbol, result.failures, today)


# --------------------------------------------------------------------------
# The weekly screen
# --------------------------------------------------------------------------


@dataclass
class ScreenRun:
    candidates: int = 0
    screened: int = 0
    passed: int = 0
    inconclusive: int = 0
    deadline_hit: bool = False
    events: list[dict] = field(default_factory=list)
    failures_by_reason: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "candidates": self.candidates,
            "screened": self.screened,
            "passed": self.passed,
            "inconclusive": self.inconclusive,
            "deadline_hit": self.deadline_hit,
            "events": list(self.events),
            "failures_by_reason": dict(sorted(self.failures_by_reason.items())),
        }


def _asset_symbol(asset) -> str:
    raw = str(_field(asset, "symbol", "") or "").strip().upper()
    # Alpaca spells a class share BRK.B; the desk (and yfinance) BRK-B.
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", raw)


def run_screen(
    state: dict,
    *,
    assets: list,
    sources: ScreenSources,
    get_bars_batch: Callable[[list[str]], dict[str, list]],
    th: ScreenThresholds,
    today: date,
    held: Iterable[str],
    configured: Iterable[str],
    deadline: float,
    batch_size: int,
    confirm_missing_asset: Callable[[str], Any] | None = None,
) -> ScreenRun:
    """One incremental pass. Safe to run every evening: each symbol is
    screened at most once per ISO week, admitted names first, then the
    candidates never screened, then the oldest-screened. Delisted/halted
    admitted names are checked on EVERY pass (immediate removal)."""
    run = ScreenRun()
    held_set = {str(s).strip().upper() for s in held}
    configured_set = {str(s).strip().upper() for s in configured}
    week = iso_week(today)
    by_symbol = {_asset_symbol(a): a for a in assets if _asset_symbol(a)}

    def _record(result: ScreenResult) -> None:
        run.screened += 1
        if result.inconclusive:
            run.inconclusive += 1
        elif result.passed:
            run.passed += 1
        for code in result.failures[:1]:
            run.failures_by_reason[code] = run.failures_by_reason.get(code, 0) + 1
        record_atr_cross_section(
            state,
            week=week,
            today=today,
            symbol=result.symbol,
            measured=result.measured,
        )
        event = apply_result(state, result, today=today, held=held_set)
        if event is not None:
            run.events.append(event)

    # 1. Delisted / halted admitted names: every pass, no weekly wait.
    for symbol in sorted(state["admitted"]):
        asset = by_symbol.get(symbol)
        if asset is None and confirm_missing_asset is None:
            # Absent from a LIST is not proof of delisting; without a
            # per-symbol confirmation nothing is removed.
            continue
        if asset is None:
            try:
                asset = confirm_missing_asset(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("universe screen: could not confirm %s: %s", symbol, exc)
                continue
            if asset is not None:
                by_symbol[symbol] = asset
        failures = check_asset(symbol, asset)
        if any(code in PERMANENT for code in failures):
            _record(ScreenResult(symbol=symbol, failures=failures))

    # 2. Who is due this week.
    due_admitted = [
        s for s in sorted(state["admitted"]) if state["admitted"][s].get("last_screen_week") != week and s in by_symbol
    ]
    candidates = [s for s in sorted(by_symbol) if s not in state["admitted"] and s not in configured_set]
    run.candidates = len(candidates)
    fresh = [s for s in candidates if s not in state["screened"]]
    stale = sorted(
        (s for s in candidates if s in state["screened"] and state["screened"][s].get("week") != week),
        key=lambda s: (state["screened"][s].get("week") or "", s),
    )

    # 3. Asset-level checks are free (the list is already in hand): settle
    # every candidate that fails them without a single market-data read.
    needs_bars: list[str] = list(due_admitted)
    for symbol in fresh + stale:
        asset = by_symbol[symbol]
        failures = check_asset(symbol, asset)
        if failures:
            _record(ScreenResult(symbol=symbol, failures=failures))
        else:
            needs_bars.append(symbol)

    # 4. Bars in batches, then the per-symbol reads, until the deadline.
    for start in range(0, len(needs_bars), max(1, int(batch_size))):
        if time.monotonic() >= deadline:
            run.deadline_hit = True
            break
        chunk = needs_bars[start : start + max(1, int(batch_size))]
        try:
            bars_by_symbol = get_bars_batch(chunk) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe screen: bar batch failed (%d symbols): %s", len(chunk), exc)
            for symbol in chunk:
                _record(ScreenResult(symbol=symbol, failures=["market_data_unavailable"]))
            continue
        # Sessions that happened: any date any name in this batch has a bar on.
        batch_dates = {getattr(b, "date") for rows in bars_by_symbol.values() for b in rows or []}
        for symbol in chunk:
            if time.monotonic() >= deadline:
                run.deadline_hit = True
                break
            result = screen_symbol(
                symbol,
                sources,
                th,
                asset=by_symbol[symbol],
                bars=bars_by_symbol.get(symbol) or [],
                reference_dates=batch_dates,
                today=today,
            )
            _record(result)
        if run.deadline_hit:
            break
    if run.deadline_hit:
        logger.info(
            "universe screen: deadline reached after %d symbols; the rest resume next run",
            run.screened,
        )
    return run


# --------------------------------------------------------------------------
# Per-run selection (the cap on what reaches the portfolio manager)
# --------------------------------------------------------------------------


def select_for_run(state: dict, *, held: Iterable[str], cap: int, today: date) -> dict[str, dict]:
    """Admitted names that join THIS run's research surface.

    Every held admitted name (coverage, not discovery — never capped), plus
    at most `cap` others, least-recently-offered first so the whole admitted
    set is rotated through rather than the same few names every morning.
    Stamps `last_offered_on` on the ones chosen.
    """
    held_set = {str(s).strip().upper() for s in held}
    admitted = state.get("admitted", {})
    chosen: dict[str, dict] = {}
    for symbol in sorted(admitted):
        if symbol in held_set:
            chosen[symbol] = admitted[symbol]
    others = sorted(
        (s for s in admitted if s not in held_set),
        key=lambda s: (admitted[s].get("last_offered_on") or "", admitted[s].get("admitted_on") or "", s),
    )
    for symbol in others[: max(0, int(cap))]:
        chosen[symbol] = admitted[symbol]
    for symbol in chosen:
        admitted[symbol]["last_offered_on"] = today.isoformat()
    return {
        symbol: {
            "temporary": True,
            "reason": "universe_screen_admission",
            "admitted_on": record.get("admitted_on"),
            "status": record.get("status"),
            "held": symbol in held_set,
            **{k: v for k, v in (record.get("measured") or {}).items()},
        }
        for symbol, record in chosen.items()
    }


# --------------------------------------------------------------------------
# The owner's words
# --------------------------------------------------------------------------


def plain_reasons(codes: Iterable[str]) -> str:
    return "; ".join(PLAIN_REASON.get(code, code.replace("_", " ")) for code in codes)


def describe_event(event: dict) -> str:
    symbol = event.get("symbol", "?")
    action = event.get("action")
    why = plain_reasons(event.get("reasons") or [])
    if action == "added":
        return f"{symbol} added — passes every check"
    if action == "flagged":
        return f"{symbol} flagged (removed if it fails again next week) — {why}"
    if action == "removed":
        return f"{symbol} removed — {why}"
    if action == "cleared":
        return f"{symbol} flag cleared — passes again"
    if action == "removal_deferred_held":
        return f"{symbol} should be removed but is HELD, so kept and watched — {why}"
    return f"{symbol} {action} — {why}"
