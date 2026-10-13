"""src.intraday.session_gate -- has the exchange's real close for today already passed?"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.session_calendar import SessionCalendarUnavailable, session_for
from src.trading_calendar import ET, et_now


def session_has_ended(broker: Any, now: datetime | None = None) -> bool:
    """True once the exchange's real close for `now`'s date has passed.

    The close comes from the exchange calendar (13:00 ET on an early-close
    day), never a fixed 16:00. An unreadable calendar or a non-trading day
    answers False: this gate only ever stops work the calendar proves is late.
    """
    now = now or et_now()
    try:
        close_at = session_for(broker, now.astimezone(ET).date()).close_at
    except SessionCalendarUnavailable:
        return False
    return isinstance(close_at, datetime) and now > close_at
