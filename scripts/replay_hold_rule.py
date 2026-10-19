#!/usr/bin/env python3
"""Replay the keep rule on today's book: what the retired rule did, what the
desk archetype (owner ruling 19 Oct 2026) does, and why.

Usage:  python scripts/replay_hold_rule.py PATH/TO/quant_agent.db [--since "2026-10-12 13:30:00"]

Read-only: the database is opened with SQLite's URI `mode=ro`, so nothing is
ever written. For each held symbol it takes each seat's NEWEST recorded stance
(`seat_stance` rows) since the clean-record start, and any written objection
answers (`objection_answer` rows), then prints one line per symbol.

Both rules are judged from recorded stances only. A stance row carries no
evidence text, so the retired rule is the stance-level reading of it:
  * OLD — sells on the chart against, on ANY name-level seat against, or on a
    net seat count at or below zero (the retired net-evidence floor, which
    counted macro).
  * NEW — sells only on the chart against; a name-level seat against with no
    written "overrule" on record goes to the position reviewer (REVIEW); macro
    never sells; a quiet name is kept.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.risk.rules import NAME_LEVEL_SEATS, signed_source_score  # noqa: E402
from src.risk.seat_agreement import stance_is_aligned  # noqa: E402

CLEAN_RECORD_START = "2026-10-12 13:30:00"


def open_read_only(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def latest_stances(conn: sqlite3.Connection, symbol: str, since: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT agent_name, evidence_json FROM specialist_evidence"
        " WHERE kind = 'seat_stance' AND symbol = ? AND timestamp >= ? ORDER BY timestamp, id",
        (symbol, since),
    ).fetchall()
    out: dict[str, str] = {}
    for row in rows:  # oldest first, so the newest row per seat wins
        stance = str(json.loads(row["evidence_json"]).get("stance") or "").strip().lower()
        out[str(row["agent_name"]).strip().lower()] = stance
    return out


def overruled_seats(conn: sqlite3.Connection, symbol: str) -> set[str]:
    row = conn.execute(
        "SELECT evidence_json FROM specialist_evidence WHERE kind = 'objection_answer' AND symbol = ?"
        " ORDER BY timestamp DESC, id DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if row is None:
        return set()
    answers = json.loads(row["evidence_json"]).get("answers") or []
    return {str(a.get("seat", "")).lower() for a in answers if str(a.get("decision", "")).lower() == "overrule"}


def _against(seat: str, symbol: str, stance: str, long: bool) -> bool:
    return stance_is_aligned(seat, symbol, stance, wants_bullish=not long)


def judge(symbol: str, qty: float, stances: dict[str, str], overruled: set[str]) -> tuple[str, str, str]:
    """(old verdict, new verdict, why) for one holding."""
    long = qty > 0
    chart_against = _against("technical", symbol, stances.get("technical", ""), long)
    names_against = [s for s in NAME_LEVEL_SEATS if _against(s, symbol, stances.get(s, ""), long)]
    net = signed_source_score(symbol, stances, "long" if long else "short")
    old_why = []
    if chart_against:
        old_why.append("chart against")
    old_why += [f"{s} against" for s in names_against]
    if net <= 0:
        old_why.append(f"net seat count {net:+d}")
    old = "SELL" if old_why else "KEEP"
    open_ = [s for s in names_against if s not in overruled]
    if chart_against:
        new, why = "SELL", "chart against"
    elif open_:
        new, why = "REVIEW", "unanswered objection: " + ", ".join(open_)
    else:
        new = "KEEP"
        why = "chart not against; " + ("objections overruled on record" if names_against else "no open objection")
    return old, new, f"old: {'; '.join(old_why) or 'passed'} | new: {why}"


def replay(path: str, since: str = CLEAN_RECORD_START) -> list[tuple[str, str, str, str]]:
    conn = open_read_only(path)
    try:
        held = conn.execute("SELECT symbol, qty FROM positions WHERE qty != 0 ORDER BY symbol").fetchall()
        return [
            (
                str(r["symbol"]),
                *judge(
                    str(r["symbol"]),
                    float(r["qty"]),
                    latest_stances(conn, r["symbol"], since),
                    overruled_seats(conn, r["symbol"]),
                ),
            )
            for r in held
        ]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", help="path to the desk database (opened read-only)")
    parser.add_argument("--since", default=CLEAN_RECORD_START, help="UTC timestamp of the first stance to read")
    args = parser.parse_args(argv)
    for symbol, old, new, why in replay(args.db, args.since):
        print(f"{symbol:<6} old={old:<6} new={new:<6} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
