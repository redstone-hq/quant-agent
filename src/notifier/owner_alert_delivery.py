"""Delivery discipline for the owner alert: retry, then a counted durable row.

Owner ruling 2026-10-02 (ACT, never just alert): a send that failed must not
be indistinguishable from one that landed. The funnel `send_owner_alert` runs
every caller through `deliver_with_retry`, so the ~30 call sites that discard
the return value get this behaviour without each one being edited.

Never raises: a failing alert path must not abort the money path it reports on.
"""

from __future__ import annotations

import time
from typing import Callable

from src.notifier.base import logger
from src.notifier.category import was_suppressed

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


def _record_undelivered(
    notifier,
    text: str,
    attempts: int,
    *,
    kind: str = "owner_alert",
    run_id: str | None = None,
) -> None:
    """Write one counted durable row; the detail carries the running count.

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
        notifier._safe_record_send(
            kind=kind,
            status=UNDELIVERED_STATUS,
            text=text,
            run_id=run_id,
            detail=f"undelivered after {attempts} attempts; undelivered_total={count}",
        )
        logger.critical(
            "OWNER ALERT [%s] run=%s UNDELIVERED after %d attempts (undelivered_total=%d)",
            kind,
            run_id,
            attempts,
            count,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record an undelivered owner alert")


def _with_credit_line(notifier, text: str, send_kwargs: dict) -> str:
    """The real Telegram notifier: every message, ordinary or owner alert,
    gets the "AI credit" line here, once, before any retry."""
    if getattr(getattr(notifier, "send", None), "_is_delivery_funnel", None) is not True:
        return text
    from src.notifier.send_funnel import with_credit

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
    retried and recorded undelivered. A caller that keeps its own durable
    retry (the cost circuit) passes max_attempts=1. Never raises.
    """
    attempts = 0
    try:
        text = _with_credit_line(notifier, text, send_kwargs)
        for attempt in range(max_attempts):
            attempts = attempt + 1
            try:
                # `send_once` where there is one: the real notifier's public
                # `send` IS this funnel, so calling it here would recurse.
                # A duck-typed test notifier with only `send` still works.
                attempt_send = notifier.send
                if getattr(attempt_send, "_is_delivery_funnel", None) is True:
                    attempt_send = notifier.send_once
                outcome = attempt_send(text, **send_kwargs)
            except Exception:  # noqa: BLE001
                logger.exception("owner alert send raised (attempt %d)", attempts)
                outcome = False
            if outcome:
                return True, False
            if was_suppressed(outcome):
                return False, True
            if not getattr(notifier, "enabled", True):
                return False, False
            if attempt < max_attempts - 1:
                try:
                    time.sleep(RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)])
                except Exception:  # noqa: BLE001
                    pass
        _record_undelivered(
            notifier,
            text,
            attempts,
            kind=send_kwargs.get("kind") or "owner_alert",
            run_id=send_kwargs.get("run_id"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("owner alert delivery discipline failed")
    return False, False


def deliver_with_retry(notifier, text: str, **send_kwargs) -> bool:
    """Send via `notifier`, retrying a failure; record it if all attempts fail.

    A deliberate suppression (mute / category filter) or a disabled notifier
    is settled, not a failure: it is returned at once, never retried.
    Returns True only when a send landed. Never raises.
    """
    return deliver_with_outcome(notifier, text, **send_kwargs)[0]
