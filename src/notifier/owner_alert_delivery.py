"""Delivery discipline for the owner alert: retry, then a counted durable row.

Owner ruling 2026-10-02 (ACT, never just alert): a send that failed must not
be indistinguishable from one that landed. The funnel `send_owner_alert` runs
every caller through `deliver_with_retry`, so the ~30 call sites that discard
the return value get this behaviour without each one being edited.

13 Oct 2026: a resend only helps when the answer can change. Telegram refused
the morning report (HTTP 400, unbalanced HTML), the identical body went out
three times, and nothing told the owner. The transport now reports each
failed attempt as a `WireFailure` saying whether a resend can land; only
those are retried; the last failure is written in the undelivered row and
sent to the owner as one plain line.

Never raises: a failing alert path must not abort the money path it reports on.
"""

from __future__ import annotations

import time

from src.notifier.base import logger
from src.notifier.category import CATEGORY_RISK, was_suppressed
from src.notifier.credit_line import with_credit

from src import infra_retry_policy as _retry

#: Total attempts (first try plus retries) before the alert is recorded
#: undelivered. Read off the desk's existing transient-fault retry policy
#: (`src.infra_retry_policy`, also the cost circuit's defaults) rather than a new number.
MAX_ATTEMPTS = _retry.MAX_RETRIES + 1
#: Seconds to wait before each retry: the same policy's exponential backoff
#: (base doubling, capped at its max). Tests patch this to zero.
RETRY_DELAYS_S = tuple(
    min(
        _retry.BACKOFF_BASE_S * 2**i,
        _retry.BACKOFF_MAX_S,
    )
    for i in range(MAX_ATTEMPTS - 1)
)

UNDELIVERED_STATUS = "owner_alert_undelivered"
#: `kind` of the one-line notice telling the owner a message was not delivered.
FAILURE_NOTICE_KIND = "delivery_failure"


class WireFailure(Exception):
    """One failed wire attempt, as the transport reports it to the retry loop.

    `retryable` says whether sending the SAME body again can land: a network
    error, an HTTP 5xx or a 429 can (Telegram names its wait in
    `retry_after`); a 4xx refusal — bad markup, wrong chat, revoked token —
    cannot, and resending only repeats it. `reason` keeps Telegram's own
    `error_code` and `description` for the durable record; `summary` is the
    short form that goes to the owner.
    """

    def __init__(
        self,
        reason: str,
        *,
        retryable: bool,
        code: int | None = None,
        retry_after: float | None = None,
        summary: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.code = code
        self.retry_after = retry_after
        self.summary = summary or reason


def _take_wire_failure(notifier) -> WireFailure | None:
    """The transport's report of the attempt that just failed, consumed.

    None for a notifier that has no transport (a duck-typed test double), or
    for a failure raised before the wire was reached: unknown, so retried.
    """
    take = getattr(notifier, "take_wire_failure", None)
    failure = take() if callable(take) else None
    return failure if isinstance(failure, WireFailure) else None


def _record_undelivered(
    notifier,
    text: str,
    attempts: int,
    *,
    kind: str = "owner_alert",
    run_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Write one counted durable row; the detail carries the running count
    and the last failure's reason.

    `kind` and `run_id` come from the call that failed, so the row can be
    tied back to its run and to the alert it was. A row nobody can attach
    to a run is close to useless.
    """
    try:
        import sqlite3

        from src.notifier.base import _DB_PATH

        count = 1
        try:
            conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
            try:
                count = (
                    1
                    + conn.execute(
                        "SELECT COUNT(*) FROM notifier_sends WHERE status = ?",
                        (UNDELIVERED_STATUS,),
                    ).fetchone()[0]
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001  (table may not exist yet)
            count = 1
        detail = f"undelivered after {attempts} attempts; undelivered_total={count}"
        if reason:
            detail += f"; last failure: {reason}"
        notifier._safe_record_send(
            kind=kind,
            status=UNDELIVERED_STATUS,
            text=text,
            run_id=run_id,
            detail=detail,
        )
        logger.critical(
            "OWNER ALERT [%s] run=%s UNDELIVERED after %d attempts (undelivered_total=%d): %s",
            kind,
            run_id,
            attempts,
            count,
            reason or "no reason recorded",
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record an undelivered owner alert")


def _tell_owner(notifier, kind: str, attempts: int, failure: WireFailure | None) -> None:
    """ONE short plain line so a lost message is not silent: which one, and why.

    A single attempt through `send_once`: plain text, fully escaped, no link,
    no credit line — tiny and always valid. Risk category, because only a
    message that passed the category filter can reach the wire and fail.
    A notifier without the real transport (a test double) gets none.
    """
    if getattr(getattr(notifier, "send", None), "_is_delivery_funnel", None) is not True:
        return
    why = failure.summary if failure is not None else "no reason recorded"
    line = f"Not delivered: {kind} message ({attempts} attempt{'s' if attempts != 1 else ''}). {why}"
    try:
        notifier.send_once(line, link_url="", kind=FAILURE_NOTICE_KIND, category=CATEGORY_RISK)
    except Exception:  # noqa: BLE001
        logger.exception("could not tell the owner about an undelivered %s message", kind)


def _attempt(notifier, text: str, attempts: int, send_kwargs: dict):
    """One delivery attempt; an exception is a failed attempt, never a raise."""
    try:
        # `send_once` where there is one: the real notifier's public
        # `send` IS this funnel, so calling it here would recurse.
        # A duck-typed test notifier with only `send` still works.
        attempt_send = notifier.send
        if getattr(attempt_send, "_is_delivery_funnel", None) is True:
            attempt_send = notifier.send_once
        return attempt_send(text, **send_kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("owner alert send raised (attempt %d)", attempts)
        return False


def _wait_before_retry(attempt: int, failure: WireFailure | None) -> None:
    """The policy's backoff, or Telegram's own `retry_after` when it gave one."""
    delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
    if failure is not None and failure.retry_after:
        delay = failure.retry_after
    try:
        time.sleep(delay)
    except Exception:  # noqa: BLE001
        pass


def _with_credit_line(notifier, text: str, send_kwargs: dict) -> str:
    """The real Telegram notifier: every message, ordinary or owner alert,
    gets the "AI credit" line here, once, before any retry."""
    if getattr(getattr(notifier, "send", None), "_is_delivery_funnel", None) is not True:
        return text
    return with_credit(notifier, text, **send_kwargs)


def deliver_with_outcome(
    notifier,
    text: str,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    **send_kwargs,
) -> tuple[bool, bool]:
    """`deliver_with_retry`, but also says whether the drop was deliberate.

    Returns (delivered, suppressed). Both False is a real failure that was
    retried where a retry could help, recorded undelivered, and reported to
    the owner in one line. A caller that keeps its own durable retry (the
    cost circuit) passes max_attempts=1. Never raises.
    """
    attempts = 0
    failure: WireFailure | None = None
    try:
        text = _with_credit_line(notifier, text, send_kwargs)
        for attempt in range(max_attempts):
            attempts = attempt + 1
            outcome = _attempt(notifier, text, attempts, send_kwargs)
            if outcome:
                return True, False
            if was_suppressed(outcome):
                return False, True
            if not getattr(notifier, "enabled", True):
                return False, False
            failure = _take_wire_failure(notifier)
            if failure is not None and not failure.retryable:
                # The same body would only be refused again: say so instead.
                break
            if attempt < max_attempts - 1:
                _wait_before_retry(attempt, failure)
        kind = send_kwargs.get("kind") or "owner_alert"
        _record_undelivered(
            notifier,
            text,
            attempts,
            kind=kind,
            run_id=send_kwargs.get("run_id"),
            reason=failure.reason if failure is not None else None,
        )
        _tell_owner(notifier, kind, attempts, failure)
    except Exception:  # noqa: BLE001
        logger.exception("owner alert delivery discipline failed")
    return False, False


def deliver_with_retry(notifier, text: str, **send_kwargs) -> bool:
    """Send via `notifier`, retrying a failure that a resend can fix; record
    it if all attempts fail.

    A deliberate suppression (mute / category filter) or a disabled notifier
    is settled, not a failure: it is returned at once, never retried.
    Returns True only when a send landed. Never raises.
    """
    return deliver_with_outcome(notifier, text, **send_kwargs)[0]
