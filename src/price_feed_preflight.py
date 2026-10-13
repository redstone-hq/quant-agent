"""Prove the price feed reads before a session places any new entry.

Owner ruling 2026-10-08: a desk that cannot read price cannot trade. Before
the entry stage sizes anything, ONE read of a reference symbol -- the desk's
configured cash vehicle, never an approved entry -- must come back
(absence is fine: the feed answered). The read carries the broker's own
retry; when it still fails the session places no new entries, every approved
entry is recorded as skipped under ``price_feed_unreadable``, and the fault is
counted durably. Existing positions are not touched by any of this.

`price_feed_session_start` runs at the stage's start, `preflight_price_feed`
on the approved entries before the viability preflight.

`wait_for_today_prints` is the other half of the same ruling, restated by the
owner the same day: "this is a swing trading desk, not scalping. The desk can
keep re-asking until a price is received. It should not be hammered because
that will get us banned." Names with no today print are held in a waiting set
and re-asked for in ONE batched latest-trades request for every waiting name,
on a rising backoff, until each prints or the session's own slot ends. A
failed batch is never a feed fault by itself: the reference symbol decides.
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import timedelta

from src.config.llm_cost import INTRA_CHECK_TICK_MINUTES
from src.config.risk_adjuncts import CashSweepConfig
from src.infra_retry_policy import BACKOFF_BASE_S, BACKOFF_MAX_S, read_worst_case_s
from src.pipeline_candidate_records import _record_execution_skip
from src.refusal_errors import PriceReadFailed
from src.sizing_refusal import (
    NO_PRINT_BY_WINDOW_END,
    NO_SIZING_PRINT,
    PRICE_FEED_UNREADABLE,
    PRICE_READ_FAILED,
    classify_price_read_failure,
    declare_price_feed_fault,
    price_feed_fault,
    reference_read_ok,
    reset_price_feed,
)
from src.trading_calendar import SESSION_WINDOWS, et_now, utc_now

logger = logging.getLogger(__name__)
_sleep = time.sleep  # module attribute so a test can stub the backoff


def reference_symbol(pipeline) -> str:
    """The symbol whose read decides "this name" versus "the feed".

    It is the desk's configured cash vehicle (`cash_sweep.symbol`, default
    `CashSweepConfig.symbol`, SGOV): a heavily traded T-bill ETF the desk
    already reads to park and fund cash. It is NEVER an approved entry or a
    held name, so one bad ticker can never become its own reference and halt
    every entry; and `reference_read_ok` refuses to use it for its own name.
    """
    sweep = getattr(getattr(pipeline, "config", None), "cash_sweep", None)
    symbol = getattr(sweep, "symbol", None)
    if isinstance(symbol, str) and symbol:
        return symbol
    return CashSweepConfig().symbol


def price_feed_session_start(pipeline, ctx):
    """Entry-stage start: last session's fault does not carry over, and this
    session's reference symbol is pinned. Returns `ctx` so it nests in place."""
    reset_price_feed(pipeline, reference_symbol(pipeline))
    return ctx


def preflight_price_feed(pipeline, ctx, decisions: list) -> list:
    """The approved entries, or `[]` (each recorded) when the feed does not read."""
    if not decisions:
        return decisions
    fault = price_feed_fault(pipeline)
    if fault is None:
        reference = getattr(pipeline, "price_feed_reference", None)
        getter = getattr(getattr(pipeline, "broker", None), "get_latest_price_stamped", None)
        if isinstance(reference, str) and reference and callable(getter):
            try:
                getter(reference)  # None is absence: the feed answered
            except PriceReadFailed as exc:
                fault = declare_price_feed_fault(pipeline, "preflight", exc, symbol=reference)
    if fault is None:
        return decisions
    for decision in decisions:
        _record_execution_skip(pipeline, ctx, decision.symbol, PRICE_FEED_UNREADABLE, fault)
    return []


# Seconds the session needs AFTER the no-print wait: the cash-sweep release,
# the submit loop and each buy's stop placement, through to process exit.
# Measured, not chosen -- the longest such stretch in the desk's own logs
# (ledgered with its source in config/number_ledger.yaml).
POST_WAIT_RESERVE_S = 700
SESSION_DEADLINE_ENV = "SESSION_DEADLINE_EPOCH"


def session_seconds_left(now_epoch: float | None = None) -> float:
    """Seconds the no-print wait may spend before the session's hard kill.

    The deadline is the absolute epoch the wrapper exports
    (`scripts/run_if_et_window.sh`, the one place the run ceiling is
    written), less `POST_WAIT_RESERVE_S` for everything that must still run
    after the wait. FAILS CLOSED: with no readable deadline (a manual run, a
    test) the answer is zero -- no wait at all, never an unbounded one."""
    raw = os.environ.get(SESSION_DEADLINE_ENV, "")
    try:
        deadline = float(raw)
    except ValueError:
        return 0.0
    if not math.isfinite(deadline):
        return 0.0
    now_epoch = utc_now().timestamp() if now_epoch is None else now_epoch
    return max(0.0, deadline - POST_WAIT_RESERVE_S - now_epoch)


def slot_seconds_left(ctx, now=None) -> float:
    """Seconds until this pass's own slot ends: the earliest of the session's
    published window end (`SESSION_WINDOWS`, ET minutes), the desk's next
    scheduled pass (`INTRA_CHECK_TICK_MINUTES` from now) -- the next pass
    carries the stop checks and must not find this one still waiting -- and
    the process's own hard kill less the measured post-wait time
    (`session_seconds_left`), so the kill can never land mid-submit. Zero
    when the session has no window, it has ended, or no deadline is known."""
    now = now or et_now()
    tick_end = now + timedelta(minutes=INTRA_CHECK_TICK_MINUTES)
    window = SESSION_WINDOWS.get(str(getattr(ctx, "session", "")))
    if window is None:
        return 0.0
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = midnight + timedelta(minutes=window[1])
    slot_s = (min(window_end, tick_end) - now).total_seconds()
    return max(0.0, min(slot_s, session_seconds_left(now.timestamp())))


def _no_batched_reader(pending: dict) -> list:
    """A stub broker that declares no batched reader: nothing to wait on, so
    the plain measured-absence refusal stands for every waiting name."""
    return [
        (d, NO_SIZING_PRINT, "no today trade print to size against and no batched re-ask available on this broker")
        for d in pending.values()
    ]


class _TodayPrintWait:
    """State of one pass's batched re-ask. Every ask -- and every reference
    read a failed ask triggers -- is admitted only while the time left in the
    slot covers one read's worst case (`read_worst_case_s`), so the wait can
    never run into the desk's next pass."""

    def __init__(self, ctx, waiting: list):
        self.budget_s = slot_seconds_left(ctx)
        self.deadline = time.monotonic() + self.budget_s
        self.read_s = read_worst_case_s()
        # backoff asks; the first ask is immediate
        self.ask_ceiling = math.ceil(self.budget_s / BACKOFF_MAX_S)
        self.pending = {d.symbol: d for d in waiting}
        self.printed: dict[str, object] = {}
        self.failed: list[tuple] = []
        self.one_by_one: list[str] = []  # names left in a single-name round; empty = batched
        self.asks = 0
        self.wait_s = BACKOFF_BASE_S
        self.fault: str | None = None
        self.stopped: tuple[str, str] | None = None  # (reason, detail) for names left

    def _room(self, wait_s: float = 0.0) -> bool:
        return time.monotonic() + wait_s + self.read_s <= self.deadline

    def pace(self) -> bool:
        """Wait the rising backoff before a later ask; False ends the wait."""
        if not self.asks:
            return self._room()  # the first ask is immediate
        if self.asks > self.ask_ceiling or not self._room(self.wait_s):
            return False
        _sleep(self.wait_s)
        self.wait_s = min(self.wait_s * 2, BACKOFF_MAX_S)
        return True

    def ask(self, pipeline, broker) -> None:
        self.asks += 1
        single = bool(self.one_by_one)
        asked = [self.one_by_one.pop(0)] if single else list(self.pending)
        try:
            prints = broker.read_latest_trade_prints(asked)
        except PriceReadFailed as exc:
            self._after_failed_ask(pipeline, exc, asked, single)
            return
        if not isinstance(prints, dict):
            self.stopped = (
                PRICE_READ_FAILED,
                (
                    f"{PRICE_READ_FAILED}: the batched today-print re-ask answered "
                    f"{type(prints).__name__}, not a price map -- not sized rather "
                    "than sized on an unread price; the desk's next pass re-decides"
                ),
            )
            return
        for symbol in [s for s in self.pending if s in prints]:
            self.printed[symbol] = self.pending.pop(symbol)
        self.one_by_one = [s for s in self.one_by_one if s in self.pending]

    def _after_failed_ask(self, pipeline, exc, asked: list, single: bool) -> None:
        """A single-name ask is classified like any single-name failure (the
        reference decides name or feed); a skipped name joins ``failed``. A
        failed BATCH declares the feed down only when the reference fails
        too; otherwise every waiting name is queued to be re-asked alone."""
        if not self._room():
            self.stopped = (
                NO_PRINT_BY_WINDOW_END,
                (
                    f"{NO_PRINT_BY_WINDOW_END}: a re-ask failed ({exc}) with too "
                    f"little of this pass's slot left for one more read "
                    f"({self.read_s:.0f}s worst case); not sized -- the desk's next "
                    "pass re-decides the name"
                ),
            )
            return
        if single:
            reason, detail = classify_price_read_failure(pipeline, exc, symbol=asked[0], what="buy")
            if reason == PRICE_FEED_UNREADABLE:
                self.fault = detail
            else:
                self.failed.append((self.pending.pop(asked[0]), reason, detail))
            return
        if reference_read_ok(pipeline, exclude=tuple(asked)) is False:
            self.fault = declare_price_feed_fault(pipeline, "batched_reask", exc, symbol=",".join(asked))
            return
        # The feed answers: the batch failed on something in it. Re-ask each
        # waiting name alone, at the same pace, to find which.
        logger.warning(
            "batched today-print re-ask failed while the reference reads; re-asking %s one at a time: %s",
            sorted(self.pending),
            exc,
        )
        self.one_by_one = list(self.pending)

    def unprinted(self) -> list:
        """The skip rows for every name still waiting when the re-ask stopped."""
        if self.fault:
            reason, detail = PRICE_FEED_UNREADABLE, self.fault
        elif self.stopped:
            reason, detail = self.stopped
        else:
            reason, detail = (
                NO_PRINT_BY_WINDOW_END,
                (
                    f"{NO_PRINT_BY_WINDOW_END}: no today trade print after {self.asks} "
                    f"ask(s) across this pass's slot ({self.budget_s:.0f}s, each ask "
                    f"admitted only with {self.read_s:.0f}s left); not sized -- the "
                    "desk's next pass re-decides the name"
                ),
            )
        return [(d, reason, detail) for d in self.pending.values()]


def wait_for_today_prints(pipeline, ctx, waiting: list) -> tuple[list, list]:
    """Re-ask for every waiting name in ONE batched request per ask until each
    has a today print or the slot ends. Returns ``(printed, skipped)``: the
    decisions that printed, in their original order, and ``(decision,
    reason, detail)`` for EVERY other waiting name, for the caller to record.

    A batched read that FAILS after its own retry does not, by itself, say
    the feed is down: one bad symbol can fail the whole request. The feed is
    declared down ONLY by the session's reference-symbol classification
    (`reference_read_ok`, the same one every single-name failure uses). If
    the reference still reads, the waiting names are re-asked ONE AT A TIME
    at the same pace -- one request per paced ask -- and a name whose own
    read fails is skipped alone, classified and recorded like any
    single-name failure. Once that round has covered every waiting name the
    re-ask returns to one batched request.

    Pacing: the first ask is immediate (it also proves the batched path
    answers before any wait is spent); each later one waits
    `BACKOFF_BASE_S`, doubling to `BACKOFF_MAX_S` (the desk's ledgered
    transient-fault policy). At the cap that is 60 / BACKOFF_MAX_S = 7.5
    asks a minute, batched or single, 3.75% of Alpaca's published 200
    requests/minute market-data limit (ledgered at
    `src.execution.broker_parts.trade_stream_reconnect._STREAM_ATTEMPT_CEILING_PER_DAY`).
    Bound: each ask is one retried read -- `1 + MAX_RETRIES` attempts
    (`src.infra_retry_policy.MAX_RETRIES`), each up to the broker's HTTP
    timeout, with the policy's backoff between them. An ask (or the
    reference read after a failed one) starts only while the slot still
    holds that worst case, so the whole wait ends inside the slot; the ask
    count is logged.
    """
    broker = getattr(pipeline, "broker", None)
    if broker is None or not hasattr(broker, "read_latest_trade_prints"):
        # Explicit capability check: a stub broker that does not declare the
        # batched reader has nothing to re-ask (absence stands).
        return [], _no_batched_reader({d.symbol: d for d in waiting})
    wait = _TodayPrintWait(ctx, waiting)
    while wait.pending and wait.fault is None and wait.stopped is None and wait.pace():
        wait.ask(pipeline, broker)
    logger.info(
        "today-print wait: %d ask(s) (one immediate, then at most %d on backoff "
        "inside the %.0fs slot); printed %s, read failed %s, still waiting %s",
        wait.asks,
        wait.ask_ceiling,
        wait.budget_s,
        sorted(wait.printed) or "-",
        sorted(d.symbol for d, _r, _x in wait.failed) or "-",
        sorted(wait.pending) or "-",
    )
    skipped = wait.failed + wait.unprinted()
    return [d for d in waiting if d.symbol in wait.printed], skipped
