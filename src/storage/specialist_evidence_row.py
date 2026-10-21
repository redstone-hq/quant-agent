"""The one `specialist_evidence` INSERT, as a plain function of a connection.

`Database.insert_specialist_evidence` wraps it in the retrying write lock;
a caller holding only a lent sqlite connection (the broker's ledger handle)
calls it directly. One SQL statement, so the two cannot drift apart.
"""

import sqlite3


def insert_specialist_evidence_row(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    agent_name: str,
    kind: str,
    scope: str,
    evidence_json: str,
    symbol: str | None = None,
    decision_id: str | None = None,
) -> int:
    """Insert and commit one evidence row on `conn`; returns its rowid."""
    cur = conn.execute(
        "INSERT INTO specialist_evidence "
        "(run_id, decision_id, agent_name, kind, scope, symbol, evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, decision_id, agent_name, kind, scope, symbol, evidence_json),
    )
    conn.commit()
    return cur.lastrowid or 0
