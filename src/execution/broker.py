import asyncio
import functools
import fcntl
import json
import logging
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date
from dataclasses import dataclass
from pathlib import Path

import yfinance as yf
from alpaca.trading.client import TradingClient

try:
    from alpaca.trading.stream import TradingStream
except ImportError:  # pragma: no cover - optional dependency surface
    TradingStream = None
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    ReplaceOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus

from src.stop_cancel_outcome import StopCancelOutcome, StopCoverageLost, settle_cancel
from src.models import Position
from src import sector_reference as _sector_reference

# THE stop-value judgement (docs/WORK.md item 88). `src.execution.stop_records`
# imports nothing from this module, so this is a leaf dependency.
from src.execution.stop_records import STOP_USABLE, classify_stop_price
from src.execution.held_qty import cover_qty_for_rearm
from src.execution.broker_parts.stop_amend import (  # noqa: F401 (re-exports keep patch targets)
    StopAmender,
    _AMEND_NOT_ATTEMPTED,
    _is_terminal_broker_rejection,
    _quantize_price,
)
from src.execution.broker_parts.stop_place import (  # noqa: F401 (re-exports keep patch targets)
    StopPlacer,
    PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES,
    PROTECTIVE_ORDER_HOLDS_SHARES_STATUSES,
    _FRACTIONAL_QTY_EPSILON,
    PROTECTIVE_ORDER_ACTIVE_STATUSES,
    _is_held_for_orders_error,
    _split_protective_qty,
    _derive_stop_tif,
    _alpaca_symbol,
    _internal_symbol,
    real_broker_order_id,
)
from src.execution.broker_parts.order_desk import (  # noqa: F401 (re-exports keep patch targets)
    OrderDesk,
    _PLAIN_PRICE_LABELS,
    _outlier_refusal_detail,
    _is_terminal_submission_rejection,
)
from src.execution.broker_parts.account_reads import AccountReads
from src.sentinel.guarded import attach_reconciliation_db, record_guarded_pass  # noqa: F401 (re-export)
from src.execution.order_gates import BadOrderQuantity, QTY_REJECTED, check_order_quantity  # noqa: F401 (re-exports keep patch targets)
from src.execution.order_idempotency import _client_order_id, _is_dead_stop_result, _session_date_key  # noqa: F401 (re-exports keep patch targets)
from src.execution.broker_parts.trade_stream import (  # noqa: F401 (re-exports keep patch targets)
    TradeStreamAuthRejected,
    TradeStreamGaveUp,
    TradeStreamWarmup,
    _ALPACA_STREAM_AUTH_DEADLINE_S,
    _ALPACA_STREAM_RECONNECT_MAX_S,
    _ALPACA_STREAM_RECONNECT_MIN_S,
    _HubWaiter,
    _STREAM_ATTEMPT_BUDGET,
    _STREAM_ATTEMPT_CEILING_PER_DAY,
    _STREAM_ATTEMPT_CEILING_PER_SESSION,
    _STREAM_AUTH_DEPRECATION_MARKER,
    _STREAM_RATE_LIMIT_STAND_DOWN_S,
    _StreamAttemptBudget,
    _TRADE_UPDATES_STREAM_LOCK,
    _TradeUpdatesHub,
    _TradeUpdatesLease,
    _alert_stream_gave_up,
    _credential_fingerprint,
    _default_trade_updates_lease_path,
    _equal_jitter_backoff,
    _fell_back_to_deprecated_auth,
    _install_trading_stream_auth_diagnostics,
    _install_trading_stream_reconnect_guard,
    _note_current_auth_format_accepted,
    _note_stream_auth_deprecation,
    _parse_stream_auth_reply,
    _stream_giveup_owner_message,
    _stream_http_status,
    _stream_retry_after_seconds,
    _trading_stream_reconnect_delay,
    TradeStreamWaits,
)
from src.execution.broker_parts.market_data import (  # noqa: F401 (re-exports keep patch targets)
    LivePrice,
    _BROKER_HTTP_TIMEOUT,
    _install_http_timeout,
    MarketData,
)

from src.execution.broker_parts.stop_order_snapshot import snapshot_stop_order
from src.execution.broker_parts import entry_protection as _entry_protection
from src.execution.broker_parts.entry_protection import _ENTRY_SIDES  # noqa: F401 (re-export keeps the name importable)
from src.execution.broker_parts import stop_cancel as _stop_cancel
from src.execution.broker_parts import trim_book as _trim_book
from src.execution.broker_parts import stop_invariant as _stop_invariant
from src.execution.broker_parts import cancel_confirm as _cancel_confirm
from src.execution.broker_parts.cancel_confirm import (  # noqa: F401 (re-exports: callers outside src.execution reach them here)
    ORDER_TERMINAL_CEILING_S,
    now_iso,
    settle_legs,
)

logger = logging.getLogger(__name__)

# `_PLAIN_PRICE_LABELS` moved to src/execution/broker_parts/order_desk.py (re-exported above).


# `_outlier_refusal_detail` moved to src/execution/broker_parts/order_desk.py (re-exported above).

# The trade_updates stream plumbing moved to src/execution/broker_parts/trade_stream.py (re-exported above).


# `_BROKER_HTTP_TIMEOUT` moved to src/execution/broker_parts/market_data.py (re-exported above).

# 2026-09-10: 15 -> 30 -> 90. `wait_for_order_terminal` now watches Alpaca's
# real-time trade_updates stream first (see that method) — a fill is
# detected the instant Alpaca reports it, not on the next poll tick, so this
# number no longer trades speed against safety in the common case. It is
# now purely the ceiling for the RARE path where the stream itself could
# not be used (import/auth/network failure) and the code falls back to REST
# polling exactly as before. 90 seconds is the originally-researched value
# (an ordinary marketable limit on a liquid US equity fills in seconds, but
# a stale/illiquid DAY order should be given real room before being pulled)
# — it was never actually shipped because the fallback-only framing didn't
# exist yet. Bounded well below a stale DAY order regardless. Later entries
# in a submission burst have already rested while earlier entries are
# finalized, so this is a conservative ceiling, not a blind per-order sleep
# added to every order.
_ENTRY_FILL_TIMEOUT_S = 90.0

# `_FRACTIONAL_QTY_EPSILON` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_AMEND_NOT_ATTEMPTED` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_is_held_for_orders_error` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_is_terminal_broker_rejection` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_is_terminal_submission_rejection` moved to src/execution/broker_parts/order_desk.py (re-exported above).


# `_split_protective_qty` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_derive_stop_tif` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_alpaca_symbol` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_internal_symbol` moved to src/execution/broker_parts/stop_place.py (re-exported above).


# `_quantize_price` moved to src/execution/broker_parts/stop_amend.py (re-exported above).


# `_install_http_timeout` moved to src/execution/broker_parts/market_data.py (re-exported above).


# `_ENTRY_SIDES` moved to src/execution/broker_parts/entry_protection.py (re-exported above).

# `PROTECTIVE_ORDER_ACTIVE_STATUSES` moved to src/execution/broker_parts/stop_place.py (re-exported above).

# `PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES` moved to src/execution/broker_parts/stop_place.py (re-exported above).

#: The set for the OTHER question: "would submitting another stop here
#: create a SECOND live order against the same shares?"
#:
#: Its reader (`TradingPipeline._reprotect_residual`'s idempotency check)
#: is looking at a stop this desk placed SECONDS ago on a prior attempt of
#: the same reprotect, after excluding by order id every stop this run
#: itself cancelled. Nothing in this codebase reconciles a duplicate
#: protective stop (see `src/coverage_watchdog.py`, which states it never
#: cancels or modifies; the only duplicate handling anywhere is a message
#: asking the owner to cancel one by hand), so the duplicate must be
#: prevented rather than cleaned up.
#:
#: CORRECTED 2026-10-01 (adversary round 2, defect 2): that reader no
#: longer treats this union as one answer. A `pending_new` stop can still
#: become `rejected`, so it is neither protection to bank nor an order to
#: place a second stop over; the reprotect path reads the two member sets
#: SEPARATELY and gives the in-flight case its own outcome — no write-back,
#: no drain of the recovery intent, re-read on the next pass. The union is
#: kept as the vocabulary for "neither terminal nor dying".
#:
#: `pending_cancel` stays OUT of both sets: a dying order is never
#: protection, whichever question is being asked.
PROTECTIVE_ORDER_ALIVE_STATUSES = PROTECTIVE_ORDER_ACTIVE_STATUSES | PROTECTIVE_ORDER_PLACEMENT_PENDING_STATUSES


# `real_broker_order_id` moved to src/execution/broker_parts/stop_place.py (re-exported above).


def _is_broker_class_shim(obj, attr: str) -> bool:
    """True when `obj` is AlpacaBroker's own thin shim for `attr`, however it was
    bound: a bound method (`__func__`), a `functools.partial` over the plain
    function (`func`, unwrapped through nested partials), or the plain function."""
    target = getattr(AlpacaBroker, attr, None)
    if target is None:
        return False
    seen = obj
    for _ in range(8):
        if seen is target:
            return True
        if isinstance(seen, functools.partial):
            seen = seen.func
            continue
        bound = getattr(seen, "__func__", None)
        if bound is None:
            return False
        seen = bound
    return False


class AlpacaBroker:
    #: Set in __init__. Declared here so an instance built without __init__
    #: reads None rather than raising; `_kill_switch_active` already treats
    #: None as "no switch configured", i.e. inert.
    _kill_switch_path: "Path | None" = None
    #: Set in __init__ (RiskConfig via src/pipeline.py). None = no notional
    #: check; declared here so an instance built without __init__ reads None.
    _max_position_pct: "float | None" = None
    #: Called with the facts of every protective stop the kill switch
    #: refuses (see `_submit_stop_limit_order`). The broker holds no
    #: database, so the owner of one wires this — `TradingPipeline` does, to
    #: `src/execution/exit_path_records.record_protective_stop_blocked`.
    #: None (the default) records nothing, exactly as before. Recording
    #: only: its result and any exception it raises are ignored.
    protective_stop_block_recorder: "object | None" = None
    #: Live `trade_updates` websocket feed. Declared here, FALSE, for the
    #: same reason as `_kill_switch_path` above: an instance built without
    #: __init__ must read the safe value rather than raise. Fail-closed
    #: direction is OFF — a construction site that never threads the flag
    #: through (the read-only broker in src/api/broker_reads.py, the
    #: heartbeat, one-off scripts, isolated unit tests) must not be able to
    #: open the account's single socket by omission. See
    #: `ExecutionConfig.fill_stream_enabled` for the flag's history (off
    #: 2026-09-17, on in production again since 2026-09-18);
    #: src/pipeline.py is the only site that passes it.
    _fill_stream_enabled: bool = False

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        kill_switch_path: str | None = None,
        trade_updates_lease_path: str | None = None,
        fill_stream_enabled: bool = False,
        max_position_pct: float | None = None,
    ):
        self.api_key = api_key
        self._max_position_pct = max_position_pct  # RiskConfig via src/pipeline.py; None = no notional check
        self.secret_key = secret_key
        self._paper = paper
        self.client = TradingClient(api_key, secret_key, paper=paper)
        _install_http_timeout(self.client)
        self._data_client = None
        # In-memory cache for completed (closed) price bars only — see
        # get_bars / get_intraday_chart_bars. Keyed so that the current,
        # still-forming trading day is never stored and always refetched
        # fresh; only prior, fully-closed days are ever served from here.
        self._closed_bars_cache: dict = {}
        self._closed_bars_cache_lock = threading.Lock()
        # Guard 1 (2026-09-02 operational safety guard — see
        # RiskConfig.kill_switch_path). `None` leaves the guard disabled,
        # which is only reachable from a construction site that predates
        # this parameter and never threads a path through (e.g. an isolated
        # unit test building `AlpacaBroker` directly) — `src/pipeline.py`
        # always passes the configured path. See `_kill_switch_active`.
        self._kill_switch_path = Path(kill_switch_path) if kill_switch_path else None
        # Per-date cache for is_trading_day. Trading-day status is set by
        # the exchange calendar months in advance — invariant within the
        # day — so a per-date dict that grows unbounded over a multi-year
        # process lifetime is still fine (1 entry per calendar day ≈ a
        # few KB / year).
        self._trading_day_cache: dict[date, bool] = {}
        # Per-date cache for get_session_open, same lifetime/invariance
        # argument as `_trading_day_cache` above — the exchange's regular
        # session open (including early-close-day exceptions) is fixed by
        # the calendar in advance. Backs `broker_reads._quote_freshness`
        # (docs/WORK.md item 15), which may run on every /quotes read.
        self._session_open_cache: dict[date, "datetime | None"] = {}
        # Per-(start, end) cache for trading_sessions_held, same lifetime/invariance
        # argument as `_trading_day_cache` above: a session count between two past
        # dates is fixed by the calendar. Measured 20 Oct: 5,466 calendar calls in
        # one morning run from the insider age gate re-asking the same pair.
        self._sessions_held_cache: dict[tuple[date, date], int] = {}
        # Stage 3 (shorts, D6). Per-run cache, same shape/lifetime as
        # `_trading_day_cache` above — a symbol's shortable/easy_to_borrow
        # flags don't change intra-session, so one asset-directory lookup
        # per symbol per process is enough.
        self._shortable_cache: dict[str, dict] = {}
        # Spec §11.1. Same shape/lifetime and same reasoning as
        # `_shortable_cache`: `fractionable` is an asset-directory fact that
        # does not change intra-session.
        self._fractionable_cache: dict[str, dict] = {}
        self._last_stream_warmup: TradeStreamWarmup | None = None
        self._trade_hub: _TradeUpdatesHub | None = None
        self._trade_hub_lock = threading.Lock()
        self._trade_lease = _TradeUpdatesLease(
            Path(trade_updates_lease_path or _default_trade_updates_lease_path()),
            owner=self,
        )
        self._trade_slot_held = False
        self._trade_lease_contended = False
        self._fill_stream_enabled = bool(fill_stream_enabled)
        self._fill_stream_off_logged = False

    def _kill_switch_active(self) -> bool:
        """Guard 1: True once ops has halted the desk by `touch`-ing the
        configured flag file.

        `path.exists()` and NOTHING else — no read, no parse, no schema —
        so a zero-byte file, a file full of garbage, and a file the operator
        can no longer remember the format of all halt identically. The
        check cannot fail open on bad content because it never looks at any
        content.

        Called at the top of every method on this class that places or
        replaces an order at the broker (`submit_order`,
        `_submit_stop_limit_order`, `replace_entry_limit`) — deliberately
        including the exit and protective-stop paths. See
        RiskConfig.kill_switch_path for why this is the one guard in the
        codebase that also blocks a risk-reducing order.
        """
        return self._kill_switch_path is not None and self._kill_switch_path.exists()

    def _account_reads(self) -> AccountReads:
        """Thin shim: builds the standalone reads object from this broker's collaborators
        (bodies moved to src/execution/broker_parts/account_reads.py). Built per call so a
        client swapped after construction is what the body sees; the caches are this
        broker's own dicts, mutated in place."""
        return AccountReads(
            client=self.client,
            shortable_cache=self._shortable_cache,
            fractionable_cache=self._fractionable_cache,
            trading_day_cache=self._trading_day_cache,
            session_open_cache=self._session_open_cache,
            sessions_held_cache=self._sessions_held_cache,
            # `_session_edge` is itself a moved body -- same recursion guard as
            # `_stop_placer`: pass it ONLY when it is NOT this broker's shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (("session_edge", "_session_edge"),)
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def get_account(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_account(*args, **kwargs)

    def get_margin_interest_activities(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_margin_interest_activities(*args, **kwargs)

    def get_all_account_activities(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_all_account_activities(*args, **kwargs)

    def get_transient_equity_eligibility(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_transient_equity_eligibility(*args, **kwargs)

    def list_assets(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().list_assets(*args, **kwargs)

    def get_asset_record(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_asset_record(*args, **kwargs)

    def get_shortability(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_shortability(*args, **kwargs)

    def get_fractionability(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_fractionability(*args, **kwargs)

    def get_recent_daily_closes(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_recent_daily_closes(*args, **kwargs)

    def get_full_portfolio_history(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_full_portfolio_history(*args, **kwargs)

    def get_positions(self) -> list[Position]:
        raw_positions = self.client.get_all_positions()
        positions = []
        for p in raw_positions:
            symbol = _internal_symbol(p.symbol)
            positions.append(
                Position(
                    symbol=symbol,
                    qty=float(p.qty),
                    avg_entry=float(p.avg_entry_price),
                    current_price=float(p.current_price),
                    market_value=float(p.market_value),
                    unrealized_pnl=float(p.unrealized_pl),
                    unrealized_intraday_pnl=float(getattr(p, "unrealized_intraday_pl", 0) or 0),
                    sector=_sector_reference._get_sector(symbol),
                    qty_available=_reported_float(getattr(p, "qty_available", None)),
                )
            )
        return positions

    def is_trading_day(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().is_trading_day(*args, **kwargs)

    def trading_sessions_held(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().trading_sessions_held(*args, **kwargs)

    def is_last_trading_day_of_quarter(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().is_last_trading_day_of_quarter(*args, **kwargs)

    def get_session_close(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_session_close(*args, **kwargs)

    def get_session_open(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_session_open(*args, **kwargs)

    def _session_edge(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads()._session_edge(*args, **kwargs)

    def _market_data(self) -> MarketData:
        """Thin shim: builds the standalone object from this broker's collaborators
        (bodies moved to src/execution/broker_parts/market_data.py). Built per call so a
        collaborator set or swapped after construction is what the body sees."""
        return MarketData(
            state=self,
            api_key=self.api_key,
            secret_key=self.secret_key,
            closed_bars_cache=self._closed_bars_cache,
            closed_bars_cache_lock=self._closed_bars_cache_lock,
            # The collaborators below are themselves moved bodies, so the object
            # already owns them. Passing this broker's same-named shim would
            # overwrite its own method with a function that calls straight back
            # into it -- infinite recursion. Same guard as `_stop_placer`: pass one
            # ONLY when it is NOT that shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("get_latest_price_stamped", "get_latest_price_stamped"),
                    ("_extract_symbol_payload", "_extract_symbol_payload"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def get_top_movers(self, *args, **kwargs):
        return self._market_data().get_top_movers(*args, **kwargs)

    def get_bars(self, *args, **kwargs):
        return self._market_data().get_bars(*args, **kwargs)

    def get_intraday_chart_bars(self, *args, **kwargs):
        return self._market_data().get_intraday_chart_bars(*args, **kwargs)

    def get_latest_price_stamped(self, *args, **kwargs):
        return self._market_data().get_latest_price_stamped(*args, **kwargs)

    def get_latest_price(self, *args, **kwargs):
        return self._market_data().get_latest_price(*args, **kwargs)

    def get_latest_quote(self, *args, **kwargs):
        return self._market_data().get_latest_quote(*args, **kwargs)

    def read_latest_trade_prints(self, *args, **kwargs):
        return self._market_data().read_latest_trade_prints(*args, **kwargs)

    def get_bars_batch(self, *args, **kwargs):
        return self._market_data().get_bars_batch(*args, **kwargs)

    def get_quotes_batch(self, *args, **kwargs):
        return self._market_data().get_quotes_batch(*args, **kwargs)

    def get_intraday_snapshots(self, *args, **kwargs):
        return self._market_data().get_intraday_snapshots(*args, **kwargs)

    def _extract_symbol_payload(self, *args, **kwargs):
        return self._market_data()._extract_symbol_payload(*args, **kwargs)

    def get_current_stop_price(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/account_reads.py."""
        return self._account_reads().get_current_stop_price(*args, **kwargs)

    def _order_desk(self) -> OrderDesk:
        """Thin shim: builds the standalone order desk from this broker's collaborators
        (bodies moved to src/execution/broker_parts/order_desk.py). Built per call so a
        client or cluster method swapped after construction is what the body sees."""
        return OrderDesk(
            client=self.client,
            kill_switch_active=self._kill_switch_active,
            kill_switch_path=self._kill_switch_path,
            wait_for_order_status=self._wait_for_order_status,
            wait_for_order_status_via_stream=self._wait_for_order_status_via_stream,
            get_latest_price=self.get_latest_price,
            order_terminal_states=self._ORDER_TERMINAL_STATES,
            order_replaceable_states=self._ORDER_REPLACEABLE_STATES,
            max_replacement_hops=self._MAX_REPLACEMENT_HOPS,
            get_fractionability=self.get_fractionability,
            get_account=self.get_account,
            max_position_pct=self._max_position_pct,
            # Four collaborators below are themselves moved bodies, so the desk
            # already owns them. Passing this broker's same-named shim would
            # overwrite the desk's own method with a function that calls
            # straight back into the desk -- infinite recursion. Same guard
            # as `_stop_placer`: pass one ONLY when it is NOT that shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("wait_for_order_terminal", "wait_for_order_terminal"),
                    ("resolve_replacement_chain", "resolve_replacement_chain"),
                    ("wait_for_order_status_via_polling", "_wait_for_order_status_via_polling"),
                    ("list_open_entry_orders_checked", "list_open_entry_orders_checked"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def cancel_open_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_open_orders(*args, **kwargs)

    def snapshot_protective_stops(
        self,
        symbol: str,
        *,
        side: str = "sell",
    ) -> tuple[bool, list[dict]]:
        """List + snapshot open protective stop orders WITHOUT cancelling them.

        Thin shim: body moved to src/execution/broker_parts/stop_cancel.py."""
        return _stop_cancel.snapshot_protective_stops(self, symbol, side=side)

    def trim_keeps_shares(self, label: str, position_qty_before_sell: float, qty: float) -> bool:
        """True when `label` is a trim AND shares remain. Body in broker_parts/trim_book.py."""
        return _trim_book.trim_keeps_shares(label, position_qty_before_sell, qty)

    def clear_stops_for_trim(
        self, symbol: str, held: float, sell_qty: float, *, side: str = "sell", cancel_specs_with_write_ahead, state
    ) -> tuple[bool, list[dict], int | None, dict | None]:
        """Shrink the resting stops in place for ONE trim (src.protection reaches it here).

        Thin shim: body in src/execution/broker_parts/trim_book.py."""
        return _trim_book.TrimAmend(
            broker=self, cancel_specs_with_write_ahead=cancel_specs_with_write_ahead, state=state
        ).clear_for_trim(symbol, held, sell_qty, side=side)

    def settle_trim_book(self, symbol: str, side: str = "sell") -> str:
        """Settle both stop legs after a trim. Thin shim: body in broker_parts/trim_book.py."""
        return _trim_book.settle_book(self, symbol, side)

    def settle_legs(self, specs: list[dict] | None, *, started: float | None = None):
        """Wait until every cancelled leg is terminal at the broker (one shared deadline).

        Thin shim (src.protection and src.delever reach it here): body in broker_parts/cancel_confirm.py."""
        return _cancel_confirm.settle_legs(self, specs, started=started)

    def cover_to_held(self, symbol: str, specs: list[dict], *, side: str = "sell") -> bool:
        """Put protective cover back up to what is held now, never above it.

        Thin shim (src.protection reaches it here): body in broker_parts/stop_invariant.py."""
        return _stop_invariant.cover_to_held(self, symbol, specs, side=side)

    def order_terminal_ceiling_s(self) -> float:
        """The one order-terminal ceiling, for a caller outside src.execution that budgets its waits on it."""
        return ORDER_TERMINAL_CEILING_S

    def cancel_snapshotted_stops(
        self,
        symbol: str,
        specs: list[dict],
    ) -> StopCancelOutcome:
        """Cancel pre-snapshotted protective stops by id.

        Thin shim: body moved to src/execution/broker_parts/stop_cancel.py."""
        return _stop_cancel.cancel_snapshotted_stops(self, symbol, specs)

    def cancel_protective_stops(self, symbol: str) -> tuple[bool, list[dict]]:
        """Cancel all open SELL stop orders for one symbol so a fresh exit

        Thin shim: body moved to src/execution/broker_parts/stop_cancel.py."""
        return _stop_cancel.cancel_protective_stops(self, symbol)

    def cancel_stray_protective_stops(
        self,
        symbol: str,
        *,
        side: str = "sell",
    ) -> int:
        """Cancel every protective stop still resting on a symbol that is

        Thin shim: body moved to src/execution/broker_parts/stop_cancel.py."""
        return _stop_cancel.cancel_stray_protective_stops(self, symbol, side=side)

    def cancel_open_entry_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_open_entry_orders(*args, **kwargs)

    def list_open_entry_order_ids(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_open_entry_order_ids(*args, **kwargs)

    def list_open_entry_orders_checked(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_open_entry_orders_checked(*args, **kwargs)

    def open_buy_notional(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().open_buy_notional(*args, **kwargs)

    def sweep_frozen_resting_orders(self, db_path):
        """FREEZE step 2 at session start: settle resting exposure-adding orders when frozen.

        Lives on the broker so callers outside the broker seam reach it through
        the broker they already hold (see src/execution/freeze_cancel.py).
        """
        from src.execution.freeze_cancel import sweep_if_frozen

        return sweep_if_frozen(self, db_path)

    def list_open_orders_checked(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk_reads.py."""
        return self._order_desk().list_open_orders_checked(*args, **kwargs)

    def replace_order_qty(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().replace_order_qty(*args, **kwargs)

    def list_recent_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_recent_orders(*args, **kwargs)

    def list_filled_sell_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().list_filled_sell_orders(*args, **kwargs)

    def get_order_fill_info(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().get_order_fill_info(*args, **kwargs)

    #: `OrderStatus`/`TradeEvent` values that mean "this order will not
    #: change again" — shared between the stream and polling paths so the
    #: two mechanisms can never quietly disagree about what "terminal" means.
    _ORDER_TERMINAL_STATES = frozenset(
        {
            "filled",
            "canceled",
            "cancelled",
            "expired",
            "rejected",
            "done_for_day",
            "replaced",
        }
    )

    #: Statuses in which Alpaca has the order but the EXECUTION VENUE does
    #: not yet. Source: Alpaca's own order-lifecycle reference
    #: (docs.alpaca.markets/docs/orders-at-alpaca, "Order Lifecycle"):
    #:   accepted    — "received by Alpaca, but hasn't yet been routed to the
    #:                  execution venue"
    #:   pending_new — "received by Alpaca, and routed to the exchanges, but
    #:                  has not yet been accepted"
    #: `new` is the first status that means "routed to exchanges for
    #: execution". A replace PATCH against an order still in one of these
    #: states is rejected by the broker ("unable to replace order, order
    #: isn't sent to exchange yet" / "cannot replace order in accepted
    #: status" in Alpaca's own community forum) — and the two transitional
    #: statuses `pending_cancel`/`pending_replace` are likewise listed by
    #: Alpaca's Replace-Order reference as non-replaceable. These are a
    #: distinct set from `_ORDER_TERMINAL_STATES`: not "done", just "not
    #: there yet". At the market open — the slowest acknowledgement and the
    #: time this desk trades most — an order can sit here for seconds.
    _ORDER_PRE_EXCHANGE_STATES = frozenset({"accepted", "pending_new"})

    #: The only working status a replace is documented AND observed to
    #: succeed against. `partially_filled` is deliberately excluded: it is
    #: replaceable at the broker, but this desk never replaces a partially
    #: filled order (see `_repeg_entry_order`'s partial-fill guard).
    _ORDER_REPLACEABLE_STATES = frozenset({"new"})

    def _trade_stream_waits(self) -> TradeStreamWaits:
        """Thin shim: builds the standalone object from this broker's collaborators
        (bodies moved to src/execution/broker_parts/trade_stream.py). Built per call so a
        collaborator set or swapped after construction is what the body sees."""
        return TradeStreamWaits(
            state=self,
            get_order_status_once=self._get_order_status_once,
            wait_for_order_status_via_polling=self._wait_for_order_status_via_polling,
            # The collaborators below are themselves moved bodies, so the object
            # already owns them. Passing this broker's same-named shim would
            # overwrite its own method with a function that calls straight back
            # into it -- infinite recursion. Same guard as `_stop_placer`: pass one
            # ONLY when it is NOT that shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("start_trade_updates", "start_trade_updates"),
                    ("_wait_for_order_status_via_stream", "_wait_for_order_status_via_stream"),
                    ("_wait_for_order_status_via_stream_locked", "_wait_for_order_status_via_stream_locked"),
                    ("fill_stream_enabled", "fill_stream_enabled"),
                    ("_release_trade_updates_slot", "_release_trade_updates_slot"),
                    ("_acquire_trade_updates_slot", "_acquire_trade_updates_slot"),
                    ("_hub_warmup", "_hub_warmup"),
                    ("_fill_stream_off_warmup", "_fill_stream_off_warmup"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def _acquire_trade_updates_slot(self, *args, **kwargs):
        return self._trade_stream_waits()._acquire_trade_updates_slot(*args, **kwargs)

    def _release_trade_updates_slot(self, *args, **kwargs):
        return self._trade_stream_waits()._release_trade_updates_slot(*args, **kwargs)

    def fill_stream_enabled(self, *args, **kwargs):
        return self._trade_stream_waits().fill_stream_enabled(*args, **kwargs)

    def _fill_stream_off_warmup(self, *args, **kwargs):
        return self._trade_stream_waits()._fill_stream_off_warmup(*args, **kwargs)

    def ensure_trade_updates(self, *args, **kwargs):
        return self._trade_stream_waits().ensure_trade_updates(*args, **kwargs)

    def trade_updates_lease_contended(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_lease_contended(*args, **kwargs)

    def trade_updates_started(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_started(*args, **kwargs)

    def trade_updates_authed(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_authed(*args, **kwargs)

    def trade_updates_auth_remaining_s(self, *args, **kwargs):
        return self._trade_stream_waits().trade_updates_auth_remaining_s(*args, **kwargs)

    def _hub_warmup(self, *args, **kwargs):
        return self._trade_stream_waits()._hub_warmup(*args, **kwargs)

    def start_trade_updates(self, *args, **kwargs):
        return self._trade_stream_waits().start_trade_updates(*args, **kwargs)

    def stop_trade_updates(self, *args, **kwargs):
        return self._trade_stream_waits().stop_trade_updates(*args, **kwargs)

    def _wait_for_order_status(self, *args, **kwargs):
        return self._trade_stream_waits()._wait_for_order_status(*args, **kwargs)

    def _wait_for_order_status_via_stream(self, *args, **kwargs):
        return self._trade_stream_waits()._wait_for_order_status_via_stream(*args, **kwargs)

    def _wait_for_order_status_via_stream_locked(self, *args, **kwargs):
        return self._trade_stream_waits()._wait_for_order_status_via_stream_locked(*args, **kwargs)

    def wait_for_order_at_exchange(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().wait_for_order_at_exchange(*args, **kwargs)

    def wait_for_order_terminal(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().wait_for_order_terminal(*args, **kwargs)

    def _get_order_status_once(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._get_order_status_once(*args, **kwargs)

    def _wait_for_order_terminal_via_stream(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_terminal_via_stream(*args, **kwargs)

    def _wait_for_order_terminal_via_polling(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_terminal_via_polling(*args, **kwargs)

    def _wait_for_order_status_via_polling(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk()._wait_for_order_status_via_polling(*args, **kwargs)

    def submit_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().submit_order(*args, **kwargs)

    # Order states that mean "this order can never fill another share".
    _TERMINAL_ORDER_STATES = frozenset(
        {
            "filled",
            "canceled",
            "cancelled",
            "expired",
            "rejected",
            "done_for_day",
            "stopped",
            "suspended",
        }
    )

    # Bounded number of `replaced_by` hops to follow when resolving what a
    # replaced order became. Each re-peg adds exactly one hop and re-pegs are
    # capped in the low single digits, so 8 is generous; the bound exists so a
    # broker-side cycle or a pathological chain can never spin this forever.
    _MAX_REPLACEMENT_HOPS = 8

    def cancel_entry_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().cancel_entry_order(*args, **kwargs)

    def resolve_replacement_chain(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().resolve_replacement_chain(*args, **kwargs)

    def replace_entry_limit(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().replace_entry_limit(*args, **kwargs)

    def await_replacement_confirmed(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().await_replacement_confirmed(*args, **kwargs)

    def place_entry_protection(
        self,
        symbol: str,
        order_id: str,
        stop_price: float,
        *,
        requested_qty: float | None = None,
        side: str = "buy",
        superseded_filled_qty: float = 0.0,
        on_unfilled_cancel=None,
        cover_full_position: bool = False,
        held_qty_before: float = 0.0,
    ) -> dict | None:
        """Wait for an entry order to reach terminal, then place a GTC

        Thin shim: body moved to src/execution/broker_parts/entry_protection.py."""
        return _entry_protection.place_entry_protection(
            self,
            symbol,
            order_id,
            stop_price,
            requested_qty=requested_qty,
            side=side,
            superseded_filled_qty=superseded_filled_qty,
            on_unfilled_cancel=on_unfilled_cancel,
            cover_full_position=cover_full_position,
            held_qty_before=held_qty_before,
            _ENTRY_FILL_TIMEOUT_S=_ENTRY_FILL_TIMEOUT_S,
        )

    def _stop_placer(self) -> StopPlacer:
        """Thin shim: builds the standalone placer from this broker's collaborators
        (bodies moved to src/execution/broker_parts/stop_place.py). Built per call so a
        client or cluster method swapped after construction is what the body sees."""
        return StopPlacer(
            client=self.client,
            list_open_stop_orders_by_side=self._list_open_stop_orders_by_side,
            list_open_protective_stop_orders=self._list_open_protective_stop_orders,
            list_open_sell_stop_orders=self._list_open_sell_stop_orders,
            snapshot_stop_order=self._snapshot_stop_order,
            amend_one_stop_price=self._amend_one_stop_price,
            amend_resting_stop_price=self._amend_resting_stop_price,
            stop_order_amendable_in_place=self._stop_order_amendable_in_place,
            cancel_snapshotted_stops=self.cancel_snapshotted_stops,
            get_positions=self.get_positions,
            kill_switch_active=self._kill_switch_active,
            kill_switch_path=self._kill_switch_path,
            wait_for_order_terminal=self.wait_for_order_terminal,
            protective_stop_block_recorder=self.protective_stop_block_recorder,
            window_log=self.__dict__.setdefault("_unprotected_windows", []),
            # Six collaborators below are themselves moved bodies, so the
            # placer already owns them. Passing this broker's same-named shim
            # would overwrite the placer's own method with a function that
            # calls straight back into the placer -- infinite recursion. Pass
            # one ONLY when it is NOT that shim: a replacement bound on this
            # instance, or a stand-in on a test host that is not a broker at
            # all. Those are exactly the cases the placer cannot see itself.
            # The shim is recognised through a bound method (`__func__`) AND
            # through a `functools.partial` (`func`): tests bind the class
            # function onto a non-broker host with partial, and that is still
            # the shim.
            **{
                kw: getattr(self, attr)
                for kw, attr in (
                    ("submit_protective_stop_retrying", "_submit_protective_stop_retrying"),
                    ("submit_stop_leg_retrying", "_submit_stop_leg_retrying"),
                    ("existing_stop_covering_qty", "_existing_stop_covering_qty"),
                    ("submit_stop_limit_order", "_submit_stop_limit_order"),
                    ("submit_stop_legs", "_submit_stop_legs"),
                    ("restore_stop_orders", "_restore_stop_orders"),
                )
                if not _is_broker_class_shim(getattr(self, attr, None), attr)
            },
        )

    def _existing_stop_covering_qty(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._existing_stop_covering_qty(*args, **kwargs)

    def _submit_stop_leg_retrying(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_leg_retrying(*args, **kwargs)

    def _submit_protective_stop_retrying(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_protective_stop_retrying(*args, **kwargs)

    def close_position(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/order_desk.py."""
        return self._order_desk().close_position(*args, **kwargs)

    def _list_open_stop_orders_by_side(
        self,
        symbol: str,
        *,
        errors: list | None = None,
    ) -> tuple[list, list]:
        """Single order-book fetch for `symbol`, split into (sell_stops, buy_stops).

        A long's protective stop is a SELL stop; a short's is a BUY stop.
        `replace_stop_loss` needs to know WHICH side a symbol's live stop is
        on before it can decide what to list/cancel/ratchet-check — but it
        can't yet know the position's direction without a second API call.
        Fetching once and filtering both ways here answers "which side has a
        stop" from a single snapshot, rather than two separate fetches that
        could each see a different broker state.
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
            record_guarded_pass(self, "replace_stop_loss.list_open_orders", context={"symbol": symbol})
        except Exception as exc:
            record_guarded_pass(
                self,
                "replace_stop_loss.list_open_orders",
                exc,
                log=logger,
                context={"symbol": symbol, "effect": "no stop orders returned to the caller"},
            )
            # Board item 172 — same contract as the sell-side lister above.
            if errors is not None:
                errors.append(f"open-order listing failed: {exc}")
            return [], []

        sell_orders: list = []
        buy_orders: list = []
        for order in orders or []:
            order_type = str(
                getattr(getattr(order, "order_type", None), "value", getattr(order, "order_type", ""))
            ).lower()
            if "stop" not in order_type:
                continue
            order_side = str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).lower()
            if order_side == "sell":
                sell_orders.append(order)
            elif order_side == "buy":
                buy_orders.append(order)
        return sell_orders, buy_orders

    def _list_open_protective_stop_orders(
        self,
        symbol: str,
        *,
        side: str = "sell",
        errors: list | None = None,
    ) -> list:
        """List open stop orders on `side` for `symbol`.

        `side="sell"` (default) finds the stops protecting a long — the only
        case that existed before shorts were countable, and delegates to
        `_list_open_sell_stop_orders` (the name a long list of tests and
        call sites pin) rather than duplicating it. `side="buy"` finds the
        stops protecting a short: a short's protective order is a BUY stop,
        so a filter hardcoded to "sell" made a short's live stop invisible
        to every caller (coverage reconcile would report a perfectly
        protected short as NAKED and try to "repair" over it).
        """
        if side.lower() == "buy":
            _, buy_orders = self._list_open_stop_orders_by_side(
                symbol,
                errors=errors,
            )
            return buy_orders
        return self._list_open_sell_stop_orders(symbol, errors=errors)

    def _list_open_sell_stop_orders(self, symbol: str, *, errors: list | None = None) -> list:
        """Board item 172: `errors`, when given, receives the listing
        failure instead of it being swallowed into an empty list.

        The empty-list return is UNCHANGED for every caller that does not
        pass `errors`, because `replace_stop_loss` and its tests depend on
        it. What changes is that a caller who needs to tell "no stops" from
        "could not ask" can now do so — and `snapshot_protective_stops` is
        exactly that caller.
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
            record_guarded_pass(self, "replace_stop_loss.list_open_orders_buyside", context={"symbol": symbol})
        except Exception as exc:
            record_guarded_pass(
                self,
                "replace_stop_loss.list_open_orders_buyside",
                exc,
                log=logger,
                context={"symbol": symbol, "effect": "no stop orders returned to the caller"},
            )
            if errors is not None:
                errors.append(f"open-order listing failed: {exc}")
            return []

        stop_orders = []
        for order in orders or []:
            order_type = str(
                getattr(getattr(order, "order_type", None), "value", getattr(order, "order_type", ""))
            ).lower()
            order_side = str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).lower()
            if "stop" in order_type and order_side == "sell":
                stop_orders.append(order)
        return stop_orders

    _snapshot_stop_order = staticmethod(snapshot_stop_order)

    def _submit_stop_limit_order(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_limit_order(*args, **kwargs)

    def _submit_stop_legs(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._submit_stop_legs(*args, **kwargs)

    def _restore_stop_orders(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer()._restore_stop_orders(*args, **kwargs)

    def shift_stops_down(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer().shift_stops_down(*args, **kwargs)

    _AMEND_DEAD_STATES = StopAmender._AMEND_DEAD_STATES

    def _stop_amender(self) -> StopAmender:
        """Thin shim: builds the standalone amender from this broker's collaborators
        (bodies moved to src/execution/broker_parts/stop_amend.py)."""
        return StopAmender(
            client=self.client,
            list_open_stop_orders_by_side=self._list_open_stop_orders_by_side,
            snapshot_stop_order=self._snapshot_stop_order,
        )

    _failed_amend_payload = staticmethod(StopAmender._failed_amend_payload)

    def _classify_after_dead_replacement(self, *, symbol: str, spec: dict, new_price: float, leg: dict) -> str:
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._classify_after_dead_replacement(
            symbol=symbol, spec=spec, new_price=new_price, leg=leg
        )

    def _amend_one_stop_price(self, **kw) -> dict:
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._amend_one_stop_price(**kw)

    _stop_order_amendable_in_place = staticmethod(StopAmender._stop_order_amendable_in_place)

    def _amend_resting_stop_price(
        self, *, symbol: str, live_orders: list, stop_specs: list[dict], new_stop_price: float, position_qty: float
    ):
        """Thin shim: body moved to src/execution/broker_parts/stop_amend.py."""
        return self._stop_amender()._amend_resting_stop_price(
            symbol=symbol,
            live_orders=live_orders,
            stop_specs=stop_specs,
            new_stop_price=new_stop_price,
            position_qty=position_qty,
        )

    def replace_stop_loss(self, *args, **kwargs):
        """Thin shim: body moved to src/execution/broker_parts/stop_place.py."""
        return self._stop_placer().replace_stop_loss(*args, **kwargs)


# ---------------------------------------------------------------------------
# Patch-target mirror (the ONE module-level __getattr__ and the ONE write-through
# __setattr__ of this file). Tests patch `src.execution.broker.<name>` for names
# whose bodies now live in broker_parts.trade_stream / broker_parts.market_data;
# those bodies read their OWN module globals, so a write here is mirrored into
# every part that defines the name. The two flags the stream code rebinds with
# `global` are not imported above (an imported copy would go stale); reads of
# them fall through to the part and writes go only there.
import sys as _sys
import types as _types
from src.execution.broker_parts import market_data as _market_data_part
from src.execution.broker_parts import trade_stream as _trade_stream_part

# Sector cluster: OWNED by `src.sector_reference` (L0). Its names stay reachable
# here so tests that patch `src.execution.broker.<name>` keep working; a write
# is mirrored into the owning module (via _MIRRORED_PARTS) and a read is always
# live from it. The copy kept in this module's dict exists so `unittest.mock.patch`
# sees the name as local and restores it with a plain setattr, which writes through.
_SECTOR_MIRROR_NAMES = frozenset(
    {
        "_get_sector",
        "_sector_resolution_status_for",
        "_canonicalize_sector",
        "_sector_cache",
        "_sector_lock",
        "_sector_resolution_status",
        "_INDEX_ETFS",
        "_ETF_SECTORS",
        "_SECTOR_LOOKUP_TIMEOUT_S",
        "_ALLOWED_SECTORS",
        "_SECTOR_ALIASES",
    }
)
_MIRRORED_PARTS = (_trade_stream_part, *_trade_stream_part._SPLIT_PARTS, _market_data_part, _sector_reference)
_FORWARDED_GLOBALS = {
    "_stream_auth_deprecation_logged": _trade_stream_part._SPLIT_PARTS[1],
    "_stream_current_auth_format_logged": _trade_stream_part._SPLIT_PARTS[1],
}


def __getattr__(name):
    part = _FORWARDED_GLOBALS.get(name)
    if part is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(part, name)


class _MirrorModule(_types.ModuleType):
    def __setattr__(self, name, value):
        for part in _MIRRORED_PARTS:
            if name in vars(part):
                setattr(part, name, value)
        if name in _FORWARDED_GLOBALS:
            return
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _FORWARDED_GLOBALS:
            return
        super().__delattr__(name)

    def __getattribute__(self, name):
        if name in _SECTOR_MIRROR_NAMES:
            return getattr(_sector_reference, name)
        return _types.ModuleType.__getattribute__(self, name)


_sys.modules[__name__].__class__ = _MirrorModule
for _n in _SECTOR_MIRROR_NAMES:
    _types.ModuleType.__setattr__(_sys.modules[__name__], _n, getattr(_sector_reference, _n))
del _n


def _reported_float(value) -> float | None:
    """A broker numeric field as a float; None when absent or unparseable."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
