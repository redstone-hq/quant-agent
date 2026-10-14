"""Delivery discipline for the owner alert: retry, then a counted durable row.

Owner ruling 2026-10-02 (ACT, never just alert): a send that failed must not
be indistinguishable from one that landed. The funnel `send_owner_alert` runs
every caller through `deliver_with_retry`, so the ~30 call sites that discard
the return value get this behaviour without each one being edited.

13 Oct 2026: a resend only helps when the answer can change. Telegram refused
the morning report (HTTP 400, unbalanced HTML), the identical body went out
three times, and nothing told the owner. Each attempt now RETURNS a
`SendResult`, carrying a `WireFailure` that says whether a resend can land;
only those are retried; the last failure is written in the undelivered row. A
refusal is also sent to the owner as one plain line, using an attempt the
refusal left unspent: the budget of `max_attempts` wire calls is never
exceeded.

Never raises: a failing alert path must not abort the money path it reports on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

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
    short form that goes to the owner. `description` is Telegram's own words
    alone (None when Telegram never answered, e.g. a network error).
    """

    def __init__(
        self,
        reason: str,
        *,
        retryable: bool,
        code: int | None = None,
        retry_after: float | None = None,
        summary: str | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.description = description
        self.retryable = retryable
        self.code = code
        self.retry_after = retry_after
        self.summary = summary or reason


@dataclass(frozen=True)
class SendResult:
    """What ONE Telegram send attempt returns: whether it landed, and why not.

    Truthy only when `ok`, so a caller that only asks "did it go?" is
    unchanged. `description` and `status_code` are Telegram's own answer to a
    refusal ("Bad Request: can't parse entities", 400); `message_id` is the
    id Telegram gave a message that landed. `failure` is the full wire report
    the retry loop reads (retryable, retry_after, summary): None for an
    attempt that never reached the wire, which is unknown and so retried.
    """

    ok: bool
    description: str | None = None
    status_code: int | None = None
    message_id: int | None = None
    failure: WireFailure | None = field(default=None, repr=False, compare=False)

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def failed(cls, exc: BaseException) -> SendResult:
        """The result of an attempt that raised `exc` (a `WireFailure` from the wire)."""
        if isinstance(exc, WireFailure):
            return cls(False, exc.description, exc.code, failure=exc)
        return cls(False)


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


def _tell_owner(notifier, kind: str, attempts: int, max_attempts: int, failure: WireFailure | None) -> None:
    """ONE short plain line so a refused message is not silent: which one, and why.

    The notice SPENDS the message's attempt budget, never extends it: it goes
    out only when a refusal (`retryable=False`) ended the loop with an attempt
    left. When the budget is spent, or no wire attempt failed (empty text, a
    pre-wire error), there is no notice — the undelivered row is the record.
    A single attempt through `send_once`: plain text, fully escaped, no link,
    no credit line — tiny and always valid. Risk category, because only a
    message that passed the category filter can reach the wire and fail.
    A notifier without the real transport (a test double) gets none.
    """
    if failure is None or failure.retryable or attempts >= max_attempts:
        return
    if getattr(getattr(notifier, "send", None), "_is_delivery_funnel", None) is not True:
        return
    why = failure.summary
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
    retried where a retry could help, recorded undelivered, and — for a
    refusal, within the same attempt budget — reported to the owner in one
    line. A caller that keeps its own durable retry (the
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
            failure = outcome.failure if isinstance(outcome, SendResult) else None
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
        _tell_owner(notifier, kind, attempts, max_attempts, failure)
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
