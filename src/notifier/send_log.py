"""Durable record of every outgoing-message attempt (moved verbatim out of TelegramNotifier._record_send)."""

from __future__ import annotations

import sqlite3
from src.notifier.base import _DB_PATH, logger
from src.trading_calendar import sql_utc_stamp


def record_send(
    self,
    *,
    kind: str,
    status: str,
    text: str,
    detail: str | None = None,
    run_id: str | None = None,
    strict: bool = False,
) -> None:
    """Durably record one outgoing-message attempt (sent/failed/
    suppressed) so "what did the desk try to tell the owner, and did
    it arrive" has a single answer that does not depend on the next
    message happening to land.

    Table, not a log line (see this file's module docstring for why
    `send()` never logged a success): `session_reports` /
    `intra_check_reports` / `evening_reports` (src/storage/db.py) are
    this project's established home for a run's long, rendered text —
    never the application log, which is grepped/tailed for operational
    health and would drown in 4000-char message bodies. This table
    follows the same shape (payload text + timestamp + a key to find
    it by) rather than inventing a new convention.

    Same-protection guarantee as the rest of this class: this is
    called from inside `send()`/`send_document()`'s own try/except
    (or, for the failure path, adds one more try/except around
    itself), so a recording bug — a locked DB file, a full disk, a
    schema mismatch — degrades to a `logger.warning` and the message
    still sends and the caller still gets its True/False. Recording
    must never be the reason a send looks like it failed, or the
    reason a real failure looks like it succeeded.

    Redaction: `text` and `detail` both go through `self._redact`
    before they touch SQLite. `text` should never carry the token or
    chat id (they live in the URL/payload, not the message body), but
    redacting here anyway costs nothing and means one place — not
    every call site — is responsible for the guarantee tested by
    `test_record_send_output_never_contains_token_or_chat_id`.
    """
    try:
        import sqlite3

        safe_text = self._redact(text if text is not None else "")
        safe_detail = self._redact(detail) if detail is not None else None
        # Belt and suspenders: production's data/ dir always exists by
        # the time this fires (Database() has already created it), but
        # a notifier call can in principle be the very first thing a
        # fresh checkout does (e.g. the live-scheduler startup ping in
        # main.py, before TradingPipeline/Database is constructed) —
        # don't let a missing directory be the reason recording fails.
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifier_sends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    text TEXT NOT NULL,
                    detail TEXT,
                    timestamp TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notifier_sends_kind_ts ON notifier_sends(kind, timestamp)")
            conn.execute(
                "INSERT INTO notifier_sends (kind, status, run_id, text, detail, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, status, run_id, safe_text, safe_detail, sql_utc_stamp()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        # Never let a recording failure look like — or cause — a send
        # failure. See docstring above.
        logger.warning("notifier: failed to record send (%s/%s): %s", kind, status, exc)
        if strict:
            # `filtered_by_category` needs to know: an unrecordable drop is
            # unsent AND unrecorded, so it re-raises and delivers instead.
            raise
