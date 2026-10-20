"""Account, asset, calendar and resting-stop reads, lifted verbatim from AlpacaBroker.

Third broker instalment. An `AccountReads` is built from its collaborators alone
(keyword-only): the trading client and the four per-process caches the bodies
mutate in place. `AlpacaBroker` keeps same-named thin shims that build one per
call, so a test that swaps the client after construction still hits the swap,
and the caches stay the broker's own dicts (the same objects, mutated here).
"""

from __future__ import annotations

import logging
from src.execution.stop_read import StopReadUnavailable, classify_stop_orders
from datetime import date

from alpaca.trading.enums import QueryOrderStatus

from src.execution.broker_parts.stop_place import _alpaca_symbol, _internal_symbol

# No ledger handle on this per-call object: traceback is logged, the counted row is skipped until
# one is lent (flagged, no new channel).
from src.sentinel.guarded import record_guarded_pass
from src.execution.broker_parts.account_asset_eligibility import AssetEligibilityReads

# Same log channel as before the move: operators and tests filter on the
# broker's logger name, and the move must not change what they see.
logger = logging.getLogger("src.execution.broker")


class AccountReads(AssetEligibilityReads):
    """The broker's read-only account/asset/calendar cluster, standalone.

    Every collaborator is a keyword-only constructor argument. `session_edge`
    is a body this class already owns; pass one only to replace it (a test
    stand-in), never the broker's shim for it -- the broker's `_account_reads`
    factory guards that.
    """

    def __init__(
        self,
        *,
        client,
        shortable_cache,
        fractionable_cache,
        trading_day_cache,
        session_open_cache,
        session_edge=None,
        sessions_held_cache=None,
    ):
        self.client = client
        self._shortable_cache = shortable_cache
        self._fractionable_cache = fractionable_cache
        self._trading_day_cache = trading_day_cache
        self._session_open_cache = session_open_cache
        # Per-(start, end) memo for `trading_sessions_held`, same lifetime and
        # invariance argument as `_trading_day_cache`. The broker lends its own
        # dict; a standalone instance gets a private one.
        self._sessions_held_cache: dict[tuple[date, date], int] = (
            {} if sessions_held_cache is None else sessions_held_cache
        )
        if session_edge is not None:
            self._session_edge = session_edge

    def get_account(self) -> dict:
        acct = self.client.get_account()
        portfolio_value = float(acct.portfolio_value)
        # last_equity = equity at previous trading-day close (Alpaca-provided).
        # Fall back to current portfolio value for brand-new accounts where
        # Alpaca hasn't stamped a prior close yet.
        raw_last = getattr(acct, "last_equity", None)
        last_equity = float(raw_last) if raw_last else portfolio_value
        if last_equity <= 0:
            last_equity = portfolio_value
        # `cash` can include same-day sale proceeds that are not yet
        # settled (T+1 for equities) and therefore not safely spendable on
        # a new BUY without implicitly drawing broker margin — Alpaca does
        # not offer a true cash-account product; every account is a margin
        # account, and accounts >= $2,000 equity get no unsettled-funds
        # allowance. `non_marginable_buying_power` is Alpaca's own settled,
        # non-margin-eligible buying-power figure — the correct "safe to
        # spend right now, no margin" number for a cash-only design (2026-
        # 08-19 SGOV/deployable-liquidity forensic).
        raw_nmbp = getattr(acct, "non_marginable_buying_power", None)
        non_marginable_buying_power = float(raw_nmbp) if raw_nmbp is not None else float(acct.cash)
        return {
            "cash": float(acct.cash),
            "portfolio_value": portfolio_value,
            "last_equity": last_equity,
            "non_marginable_buying_power": non_marginable_buying_power,
        }

    def get_margin_interest_activities(self, after: str | None = None) -> list[dict]:
        """Broker-truth `INT` (margin interest) account activity records.

        Spec §11.2's empirical check: paper trading's own docs don't say
        whether margin interest is simulated, so this reads Alpaca's
        account-activities ledger directly rather than guessing. Feeds
        `src.margin_interest.compare_estimate_to_broker_activity`,
        which does the actual "confirmed / not confirmed" judgement — this
        method only fetches and normalizes the raw records.

        `after` is an optional ISO date/datetime string (Alpaca's
        `after` query param) to scope the lookup to the relevant
        overnight period; omitted, Alpaca returns its own recent-activity
        default window.

        The SDK version pinned here (alpaca-py) has no typed wrapper for
        the activities endpoint, so this uses the low-level
        `TradingClient.get()` REST passthrough against
        `/v2/account/activities/INT` directly. Never raises — a broker
        read failure here must not be able to break the caller (the
        morning alert / dashboard read); it degrades to an empty list,
        which `compare_estimate_to_broker_activity` reports as "not
        confirmed", never as a fabricated "confirmed absent".
        """
        try:
            params: dict = {}
            if after:
                params["after"] = after
            raw = self.client.get("/account/activities/INT", params or None)
            record_guarded_pass(self, "account_reads.margin_interest_activities", context={})
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.margin_interest_activities",
                exc,
                log=logger,
                context={**{}, "effect": "returned an empty list"},
            )
            return []
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for item in raw:
            try:
                if not isinstance(item, dict):
                    continue
                out.append(
                    {
                        "date": item.get("date"),
                        "net_amount": float(item.get("net_amount") or 0.0),
                        "description": item.get("description", ""),
                        "activity_type": item.get("activity_type", "INT"),
                    }
                )
            except (TypeError, ValueError):
                continue
        return out

    def get_all_account_activities(self, page_size: int = 100) -> list[dict]:
        """The account's FULL activity ledger — every `JNLC` deposit/
        withdrawal, `FILL`, `FEE`, `WH` withholding, `CFEE`, `DIV`, `INT`,
        etc., for the life of the account. Unlike
        `get_margin_interest_activities`, no `activity_type` filter — this
        is the raw feed the margin-interest HISTORICAL BACKFILL replays to
        reconstruct a daily cash balance (`src.margin_interest.
        reconstruct_daily_cash_balances`), since Alpaca has no historical
        cash or positions endpoint at all.

        Same low-level `TradingClient.get()` REST passthrough as
        `get_margin_interest_activities` (no typed SDK wrapper for this
        endpoint), paged forward with Alpaca's own `page_token` cursor
        (ascending by `id`, its documented order) until a short page ends
        the list. Returns raw dicts, unfiltered and unnormalized — the
        caller picks whichever fields it needs per activity type, since
        different types carry different shapes (a `FILL` has `price`/
        `qty`/`side`; a `JNLC`/`FEE`/`WH` has `net_amount`).

        Never raises — a broker read failure here must not be able to
        break a caller; it degrades to whatever was fetched before the
        failure (empty list, on a first-page failure).
        """
        activities: list[dict] = []
        page_token: str | None = None
        try:
            while True:
                params: dict = {"direction": "asc", "page_size": page_size}
                if page_token:
                    params["page_token"] = page_token
                page = self.client.get("/account/activities", params)
                if not isinstance(page, list) or not page:
                    break
                activities.extend(a for a in page if isinstance(a, dict))
                if len(page) < page_size:
                    break
                last_id = page[-1].get("id")
                if not last_id:
                    break
                page_token = last_id
            record_guarded_pass(
                self,
                "account_reads.all_account_activities",
                context={"rows_before_failure": len(activities)},
            )
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.all_account_activities",
                exc,
                log=logger,
                context={**{"rows_before_failure": len(activities)}, "effect": "returned the rows fetched so far"},
            )
        return activities

    def get_transient_equity_eligibility(self, symbol: str) -> dict:
        """Fail-closed broker eligibility for an out-of-universe candidate.

        This is a read-only asset-directory lookup. It grants no trading
        permission by itself; the Smart Money admission reducer combines it
        with SEC provenance, price/history/liquidity checks, and a per-run cap.
        """
        canonical = _internal_symbol(_alpaca_symbol(symbol))
        alpaca_symbol = _alpaca_symbol(canonical)
        non_equity_suffixes = (".WS", ".WSA", ".WSB", ".U", ".UN", ".RT")
        if alpaca_symbol.endswith(non_equity_suffixes):
            return {"eligible": False, "reason": "unsupported_security_suffix"}
        try:
            asset = self.client.get_asset(alpaca_symbol)
            record_guarded_pass(self, "account_reads.asset_eligibility", context={"symbol": canonical})
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.asset_eligibility",
                exc,
                log=logger,
                context={**{"symbol": canonical}, "effect": "reported not eligible"},
            )
            return {"eligible": False, "reason": "asset_lookup_failed"}

        def _field(name, default=None):
            if isinstance(asset, dict):
                return asset.get(name, default)
            return getattr(asset, name, default)

        def _enum_text(value) -> str:
            return str(getattr(value, "value", value) or "").strip().lower()

        status = _enum_text(_field("status"))
        asset_class = _enum_text(_field("asset_class", _field("class")))
        exchange = _enum_text(_field("exchange"))
        tradable = bool(_field("tradable", False))
        name = str(_field("name", "") or "").strip()
        name_lower = name.casefold()
        unsupported_name_terms = (
            " exchange traded fund",
            " etf",
            "fund shares",
            "warrant",
            "preferred",
            "depositary",
            " american deposit",
            " unit",
            " rights",
        )

        reason = None
        if status != "active":
            reason = "asset_not_active"
        elif asset_class not in {"us_equity", "assetclass.us_equity"}:
            reason = "not_us_equity"
        elif not tradable:
            reason = "asset_not_tradable"
        elif exchange not in {
            "nyse",
            "nasdaq",
            "amex",
            "arca",
            "bats",
            "assetexchange.nyse",
            "assetexchange.nasdaq",
            "assetexchange.amex",
            "assetexchange.arca",
            "assetexchange.bats",
        }:
            reason = "unsupported_exchange"
        elif any(term in name_lower for term in unsupported_name_terms):
            reason = "not_common_stock"

        return {
            "eligible": reason is None,
            "reason": reason or "eligible",
            "symbol": canonical,
            "name": name,
            "exchange": exchange,
        }

    def list_assets(self) -> list[dict]:
        """Every ACTIVE US-equity asset record, raw, one read-only GET.

        The universe screen's candidate source (`src/universe_screen.py`).
        Raw REST rather than the SDK's `get_all_assets` because the pinned
        alpaca-py (0.44) `Asset` model has no `borrow_status`, the field
        Alpaca now names as the borrow flag (it deprecated `easy_to_borrow`
        on 2026-06-22 with a 2026-09-22 sunset —
        https://docs.alpaca.markets/reference/get-v2-assets-1). Raises on
        failure: an empty list would read as "every admitted name was
        delisted", which it is not.
        """
        raw = self.client.get(
            "/assets",
            {"status": "active", "asset_class": "us_equity"},
        )
        if not isinstance(raw, list):
            raise RuntimeError(f"asset list returned {type(raw).__name__}, not a list")
        return [item for item in raw if isinstance(item, dict)]

    def get_asset_record(self, symbol: str) -> dict | None:
        """One raw asset record; None ONLY when the broker says it does not
        exist (HTTP 404/422). Any other failure raises, so a network blip is
        never mistaken for a delisting."""
        alpaca_symbol = _alpaca_symbol(_internal_symbol(_alpaca_symbol(symbol)))
        try:
            raw = self.client.get(f"/assets/{alpaca_symbol}")
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (404, 422):
                return None
            raise
        return raw if isinstance(raw, dict) else None

    def get_recent_daily_closes(self, lookback_days: int = 10) -> list[tuple[str, float]]:
        """Official regular-session daily CLOSE equity for recent trading days.

        Source: Alpaca portfolio_history at 1D timeframe with
        ``extended_hours=False`` — the broker-side source of truth for
        end-of-regular-session equity. Crucially, unlike ``account.last_equity``
        (which is the PRIOR trading day's close, and so is one day stale at the
        20:00 ET evening run), the LAST point here is TODAY's 4pm close. That
        lets the evening report show a true close-to-close ("4pm-to-4pm") P&L
        instead of a close-to-8pm-after-hours broker diff.

        Returns ``[(et_date_str, close_equity), ...]`` oldest-first, or ``[]``
        on any failure (caller falls back to the real-time P&L). Best-effort —
        never raises. ET-date mapping mirrors scripts/export_alpaca_trades.py.
        """
        from datetime import datetime, timedelta, timezone
        from src.util.time import ET

        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            from src.trading_calendar import utc_now

            now = utc_now()
            req = GetPortfolioHistoryRequest(
                timeframe="1D",
                extended_hours=False,
                start=now - timedelta(days=lookback_days * 2 + 10),
                end=now,
            )
            history = self.client.get_portfolio_history(history_filter=req)
            record_guarded_pass(self, "account_reads.recent_daily_closes", context={})
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.recent_daily_closes",
                exc,
                log=logger,
                context={**{}, "effect": "returned an empty list"},
            )
            return []
        timestamps = getattr(history, "timestamp", None) or []
        equities = getattr(history, "equity", None) or []
        out: list[tuple[str, float]] = []
        for i, ts in enumerate(timestamps):
            if i >= len(equities) or equities[i] is None:
                continue
            try:
                d = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
                eq = float(equities[i])
            except (TypeError, ValueError, OSError):
                continue
            out.append((d, eq))
        return out

    def get_full_portfolio_history(self) -> list[tuple[str, float]]:
        """All available 1D equity history from Alpaca portfolio_history.

        Returns [(et_date_str, equity), ...] oldest-first, skipping zero
        rows (pre-funding). Best-effort — never raises.
        """
        from datetime import datetime, timedelta, timezone
        from src.util.time import ET

        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            from src.trading_calendar import utc_now

            now = utc_now()
            req = GetPortfolioHistoryRequest(
                timeframe="1D",
                extended_hours=False,
                start=now - timedelta(days=365 * 5),
                end=now,
            )
            history = self.client.get_portfolio_history(history_filter=req)
            record_guarded_pass(self, "account_reads.full_portfolio_history", context={})
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.full_portfolio_history",
                exc,
                log=logger,
                context={**{}, "effect": "returned an empty list"},
            )
            return []
        timestamps = getattr(history, "timestamp", None) or []
        equities = getattr(history, "equity", None) or []
        out: list[tuple[str, float]] = []
        for i, ts in enumerate(timestamps):
            if i >= len(equities) or equities[i] is None:
                continue
            try:
                d = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
                eq = float(equities[i])
            except (TypeError, ValueError, OSError):
                continue
            if eq == 0.0:
                continue  # skip pre-funding rows
            out.append((d, eq))
        return out

    def is_trading_day(self, on_date: date | None = None) -> bool:
        from src.util.time import et_today

        target_date = on_date or et_today()  # ET trading-day, not host-local
        # Per-date result cache. is_trading_day is hit on every session
        # entry, in scheduler `_run_safe`, in some agent helpers — easily
        # 20+ Alpaca calendar lookups per session for a fact that's
        # invariant within the day. The result is also stable: a date
        # either is or isn't a trading day, decided by the exchange
        # calendar months in advance, so per-date cache is safe.
        cached = self._trading_day_cache.get(target_date)
        if cached is not None:
            return cached
        try:
            from alpaca.trading.requests import GetCalendarRequest

            calendar = self.client.get_calendar(GetCalendarRequest(start=target_date, end=target_date))
            result = bool(calendar)
            record_guarded_pass(self, "account_reads.trading_calendar_confirm", context={"date": str(target_date)})
        except Exception:
            # Only a successful empty response means holiday/weekend. Do not
            # cache or disguise an unavailable calendar as a closed market.
            logger.exception(
                "Trading-calendar lookup failed for %s; refusing to assume closed",
                target_date,
            )
            raise
        self._trading_day_cache[target_date] = result
        return result

    def trading_sessions_held(self, start: date, end: date) -> int:
        """Holiday-aware companion to `trading_calendar.trading_sessions_held`.

        Same semantics — trading sessions strictly AFTER `start` up to and
        including `end` — but backed by Alpaca's real market calendar
        instead of a Mon-Fri weekday heuristic, so a market holiday inside
        the range (Thanksgiving, July 4, Christmas, Good Friday, etc.) is
        correctly excluded instead of silently counted as a session.

        Item 165: `trading_calendar.trading_sessions_held` documents this
        exact gap (a holiday-crossing week overstates the count by one per
        holiday) as an accepted CHEAP approximation for callers with no
        broker connection. Callers that hold a broker instance — this one —
        should prefer this method instead.

        Falls back to the weekday approximation on a calendar-query failure
        because this is a reporting count. Unlike this helper, the session-entry
        `is_trading_day` check raises: an outage cannot look like a holiday.

        A successful answer is memoized per (start, end) in a plain dict the
        broker owns, so repeated asks (the insider-filing age gate asks once
        per observation with the same end date) cost one calendar call and
        one guarded-pass row; the fallback answer is never memoized so an
        outage is retried on the next call.

        Returns 0 if `end` is not after `start`.
        """
        if end <= start:
            return 0
        cached = self._sessions_held_cache.get((start, end))
        if cached is not None:
            return cached
        from datetime import timedelta as _td

        from alpaca.trading.requests import GetCalendarRequest

        query_start = start + _td(days=1)
        try:
            calendar = self.client.get_calendar(GetCalendarRequest(start=query_start, end=end)) or []
            record_guarded_pass(
                self,
                "account_reads.trading_sessions_held",
                context={"start": str(start), "end": str(end)},
            )
            self._sessions_held_cache[(start, end)] = len(calendar)
            return len(calendar)
        except Exception as exc:  # noqa: BLE001
            record_guarded_pass(
                self,
                "account_reads.trading_sessions_held",
                exc,
                log=logger,
                context={**{"start": str(start), "end": str(end)}, "effect": "fell back to weekday count"},
            )
            from src.trading_calendar import (
                trading_sessions_held as _weekday_sessions_held,
            )

            return _weekday_sessions_held(start, end)

    def is_last_trading_day_of_quarter(self, on_date: date | None = None) -> bool:
        """True when `on_date` (default today-ET) is the last OPEN session
        of the current quarter — respects holidays and early closes.

        Uses Alpaca's calendar. For Mar/Jun/Sep/Dec only (other months
        short-circuit to False, saving the API call). Queries the
        calendar from today through month-end; we're the last trading
        day iff no later entry exists.

        The quarterly meta-reflector launchd wrapper relies on this:
        Dec 31 is often Sunday, and the real "last trading day" can be
        Dec 29 or Dec 30 depending on the calendar. Weekday heuristic
        alone gets this wrong.
        """
        from src.trading_calendar import _QUARTER_END_MONTHS, et_today
        from datetime import date as _date, timedelta as _td

        target = on_date or et_today()
        if target.month not in _QUARTER_END_MONTHS:
            return False
        # Build month-end date for range query (last day of target.month).
        if target.month == 12:
            next_month_start = _date(target.year + 1, 1, 1)
        else:
            next_month_start = _date(target.year, target.month + 1, 1)
        month_end = next_month_start - _td(days=1)
        try:
            from alpaca.trading.requests import GetCalendarRequest

            calendar = self.client.get_calendar(GetCalendarRequest(start=target, end=month_end)) or []
            record_guarded_pass(self, "account_reads.last_trading_day_of_quarter", context={"target": str(target)})
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.last_trading_day_of_quarter",
                exc,
                log=logger,
                context={**{"target": str(target)}, "effect": "answered not last day"},
            )
            return False
        if not calendar:
            return False
        # Alpaca returns one entry per trading day in [start, end]. We are the
        # last iff the LAST entry's date equals target.
        last_entry = calendar[-1]
        last_date = getattr(last_entry, "date", None)
        if last_date is None:
            return False
        return last_date == target

    def get_session_close(self, on_date: date | None = None):
        """Return the ET-aware datetime when the regular cash session closes
        today, or None if today is not a trading day (weekend / holiday) or
        the calendar lookup fails.

        Distinct from `is_trading_day` because it answers a different
        question: "WHEN does today close?" — needed to detect early-close
        days (Thanksgiving Friday 13:00, July 3 half-day) where the
        launchd-scheduled midday (13:00-14:30 ET) and close (15:30-15:55 ET)
        sessions would otherwise keep running against an already-shut market.
        """
        return self._session_edge(on_date, "close")

    def get_session_open(self, on_date: date | None = None):
        """Return the ET-aware datetime when the regular cash session OPENS
        today, or None if today is not a trading day or the calendar lookup
        fails.

        The mirror of `get_session_close`, and added for the same reason it
        was: a caller that needs to know whether the market is open right
        now must read BOTH edges from the exchange calendar rather than
        assume 09:30. Late opens exist, and the desk's rule is that a timing
        boundary comes from the calendar the broker publishes, never from a
        number typed here. `src/coverage_watchdog.py` is the caller that
        needs it — it may only place an order while the session is genuinely
        open, and it runs from a unit that fires hours before the bell.
        """
        return self._session_edge(on_date, "open")

    def _session_edge(self, on_date: date | None, attr: str):
        """Shared body of `get_session_close` / `get_session_open` — one
        calendar read, one attribute. Two copies of this would be two places
        for the naive-datetime bug below to be fixed in."""
        from src.trading_calendar import ET, et_today
        from datetime import datetime as _dt

        target_date = on_date or et_today()
        try:
            from alpaca.trading.requests import GetCalendarRequest

            calendar = self.client.get_calendar(GetCalendarRequest(start=target_date, end=target_date))
            record_guarded_pass(
                self,
                "account_reads.session_edge_calendar",
                context={"attr": attr, "date": str(target_date)},
            )
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.session_edge_calendar",
                exc,
                log=logger,
                context={**{"attr": attr, "date": str(target_date)}, "effect": "returned None"},
            )
            return None
        if not calendar:
            return None
        entry = calendar[0]
        entry_date = getattr(entry, "date", None)
        entry_edge = getattr(entry, attr, None)
        if entry_date is None or entry_edge is None:
            return None
        try:
            # alpaca-py's Calendar.close/.open is a full naive DATETIME
            # (already carrying the session date + ET wall clock), NOT a
            # time. The old code called datetime.combine(date, datetime),
            # which ALWAYS raised TypeError → logged → returned None → the
            # early-close guard never fired and midday/close ran against a
            # shut market on half-days, submitting orders that can only be
            # rejected (2026-07-16 audit: dead code since it was written;
            # the test that was supposed to cover it used a MagicMock with
            # a `time`). Keep the `time` branch for the older SDK shape.
            if isinstance(entry_edge, _dt):
                result = entry_edge.replace(tzinfo=ET)
            else:
                result = _dt.combine(entry_date, entry_edge).replace(tzinfo=ET)
            record_guarded_pass(
                self,
                "account_reads.session_edge_resolve",
                context={"attr": attr, "date": str(entry_date)},
            )
            return result
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.session_edge_resolve",
                exc,
                log=logger,
                context={**{"attr": attr, "date": str(entry_date)}, "effect": "returned None"},
            )
            return None

    def get_session_open(self, on_date: date | None = None):
        """Return the ET-aware datetime when the regular cash session opens
        on `on_date` (default today), or None if `on_date` is not a trading
        day (weekend / holiday) or the calendar lookup fails.

        Mirrors `get_session_close` (same Alpaca calendar entry, same
        naive-datetime-vs-time SDK-shape handling), but answers "when did
        the session START" rather than "when does it end" — needed to tell
        a genuinely stale quote from a live one (docs/WORK.md item 15):
        a last-trade timestamp from BEFORE today's session open, seen while
        the market is open, means nothing has traded for this symbol yet
        today, not that the feed is current. This is an exchange session
        boundary Alpaca's own calendar supplies — never a fitted duration.

        Cached per-date (a trading day's open time is fixed by the exchange
        calendar in advance, same invariance argument as `is_trading_day`)
        since this may be called on every `/quotes` read.
        """
        from src.trading_calendar import ET, et_today
        from datetime import datetime as _dt

        target_date = on_date or et_today()
        if target_date in self._session_open_cache:
            return self._session_open_cache[target_date]
        try:
            from alpaca.trading.requests import GetCalendarRequest

            calendar = self.client.get_calendar(GetCalendarRequest(start=target_date, end=target_date))
            record_guarded_pass(self, "account_reads.session_open_calendar", context={"date": str(target_date)})
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.session_open_calendar",
                exc,
                log=logger,
                context={**{"date": str(target_date)}, "effect": "returned None, not cached"},
            )
            return None  # transient failure — do not cache
        if not calendar:
            self._session_open_cache[target_date] = None
            return None
        entry = calendar[0]
        entry_date = getattr(entry, "date", None)
        entry_open = getattr(entry, "open", None)
        if entry_date is None or entry_open is None:
            self._session_open_cache[target_date] = None
            return None
        try:
            if isinstance(entry_open, _dt):
                result = entry_open.replace(tzinfo=ET)
            else:
                result = _dt.combine(entry_date, entry_open).replace(tzinfo=ET)
            record_guarded_pass(self, "account_reads.session_open_resolve", context={"date": str(entry_date)})
        except Exception as exc:
            record_guarded_pass(
                self,
                "account_reads.session_open_resolve",
                exc,
                log=logger,
                context={**{"date": str(entry_date)}, "effect": "cached None"},
            )
            result = None
        self._session_open_cache[target_date] = result
        return result

    def get_current_stop_price(self, symbol: str) -> float | None:
        """Return the price of the current open protective stop for a symbol.

        Used by ex-dividend / trailing-stop logic that needs to read the
        existing stop. None = broker answered, no stop; an unreadable or
        ambiguous read raises StopReadUnavailable (use stop_read.read_stop).

        A long's protective stop is a SELL stop (fires as price falls); a
        short's is a BUY stop (fires as price rises) — Alpaca has no notion
        of "protective" on the order itself, only a side. Pre-shorts this
        method only ever looked for SELL stops, so a short's live BUY stop
        was invisible here: every downstream caller (ex-div shift,
        deterministic trailing, coverage repair) would treat a perfectly
        protected short as unprotected. This reads BOTH sides and reports
        whichever one is actually present, rather than asking the caller to
        already know the position's direction.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_alpaca_symbol(symbol)],
                    nested=True,
                )
            )
        except Exception as exc:
            raise StopReadUnavailable(f"{symbol}: {exc}") from exc
        return classify_stop_orders(symbol, orders)
