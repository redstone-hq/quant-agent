"""Read-only Trade Review: closed-trade analysis over the clean record. Display only.

One GET route. All arithmetic lives in `src.storage.analytics.trade_review`, which
reuses the ledger's pairing, `r_multiple` and the clean-record filter; this module
only reads rows and returns the result. Nothing here feeds sizing, stops or exits.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api import db_reads
from src.storage.analytics.trade_review import build_trade_review

router = APIRouter()


@router.get("/trade-review")
def get_trade_review() -> dict:
    """Closed trades since the clean-record start, with R statistics. HTTP 200 even on a read error."""
    read = db_reads.get_trade_review_rows()
    if read["read_error"]:
        return {"state": "error", "read_error": read["read_error"]}
    return {"state": "ok", "read_error": None, **build_trade_review(read["rows"])}
