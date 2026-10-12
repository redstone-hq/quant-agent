"""src.intraday.no_room_text -- the owner's plain sentence for a skipped paid PM call.

Pure text, a leaf module: imports nothing from the money path, so the read-only
API feed can use it without a door into trading code.
"""

from __future__ import annotations


def no_room_sentence(record: dict) -> str:
    """The owner's plain sentence for a check whose paid PM call was skipped."""
    headroom = record.get("headroom_usd")
    head = f"${headroom:,.0f}" if isinstance(headroom, (int, float)) else "unknown"
    ceiling = record.get("gross_ceiling_x")
    where = f"under the {ceiling:g}x limit" if isinstance(ceiling, (int, float)) else "of settled cash"
    needs = [
        c["min_notional_usd"]
        for c in record.get("candidates_considered") or []
        if isinstance(c.get("min_notional_usd"), (int, float))
    ]
    tail = f"; the smallest needed ${min(needs):,.0f}" if needs and min(needs) > 0 else ""
    if not needs and record.get("candidates_considered"):
        tail = "; no mover had a readable stop"
    return f"No paid buy check: no room to open even a minimum-size trade (headroom {head} {where}{tail})."
