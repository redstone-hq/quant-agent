"""The Telegram decision messages show an AI reason as its whole first sentence.

The owner's phone showed AI reasons stopping mid-sentence ("Close the held
long because current..."). Each field was cut to an invented length (140, 300,
420, 550) with a "..." marker. A reason now shows its complete first sentence:
never a cut inside a sentence, never a marker, and no length number. The full
reason stays in the full report. These tests drive the real builders with a
700-character, three-sentence reason.
"""

import json
import sqlite3

import pytest

from src import trader_feed
from src.storage.db import Database

from src.trader_feed.common import _first_sentence

FIRST = (
    "Close the held long because current momentum has faded against the sector "
    "and the stop is now close enough that one more small drop would trigger it anyway, "
    "which means the position has already stopped working as the idea we bought it for."
)
SECOND = (
    "Waiting for the stop to fill would only lock in a slightly worse price than selling today, "
    "so closing now keeps the loss a little smaller than the alternative "
    "and leaves the whole decision in our hands rather than in the market's."
)
THIRD = (
    "The capital freed can sit in cash until the sector shows a clear base again, "
    "and nothing about the original thesis argues for staying in the name "
    "while the evidence keeps moving against it and the stop sits so close by at the moment."
)
REASON = f"{FIRST} {SECOND} {THIRD}"


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "quant_agent.db"
    database = Database(str(db))
    database.initialize()
    database.close()
    monkeypatch.setattr(trader_feed, "_DB_PATH", db)
    return db


def _evidence(db, run_id, agent, kind, data, symbol=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO specialist_evidence "
        "(run_id, agent_name, kind, scope, symbol, evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, agent, kind, "symbol" if symbol else "run", symbol, json.dumps(data)),
    )
    conn.commit()
    conn.close()


def _morning(db, run):
    return trader_feed.format_session_result(
        "morning",
        {"status": "executed", "run_id": run, "orders": [{"symbol": "CRM"}]},
        20.0,
    )


def _line_with(msg, needle):
    return next(line for line in msg.splitlines() if needle in line)


def test_the_reason_is_700_characters_in_three_sentences():
    assert len(REASON) >= 700
    assert FIRST.endswith(".") and SECOND.endswith(".") and THIRD.endswith(".")


def test_pm_order_reason_is_exactly_its_first_sentence(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-first-sentence-order"
    _evidence(
        db,
        run,
        "portfolio_manager",
        "proposed_order",
        {"action": "SELL", "symbol": "CRM", "allocation_pct": 100, "reasoning": REASON},
        symbol="CRM",
    )

    line = _line_with(_morning(db, run), "SELL CRM")

    assert line.endswith(f" — {FIRST}")
    assert "…" not in line
    assert SECOND not in line


def test_pm_view_is_exactly_its_first_sentence(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-first-sentence-view"
    _evidence(db, run, "portfolio_manager", "reasoning", {"portfolio_view": REASON})

    line = _line_with(_morning(db, run), "PM view (this check)")

    assert line.endswith(f": {FIRST}")
    assert "…" not in line
    assert SECOND not in line


def test_risk_reasoning_is_exactly_its_first_sentence(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    run = "run-first-sentence-risk"
    _evidence(db, run, "risk_manager", "verdict", {"approved": True, "reasoning": REASON})

    msg = _morning(db, run)

    assert f"   {FIRST}\n" in msg + "\n"
    assert SECOND not in msg
    assert "…" not in msg


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One short sentence.", "One short sentence."),
        ("No full stop at all", "No full stop at all"),
        ("Is it close? Yes. Sell!", "Is it close?"),
        ("Sell now! The stop is near.", "Sell now!"),
        ("Cut U.S. exposure today. Then wait.", "Cut U.S. exposure today."),
        ("Sell e.g. the weakest name. Then wait.", "Sell e.g. the weakest name."),
        ("It fell 1.5% on volume. More later.", "It fell 1.5% on volume."),
        ("  Spaced \n out.  Next one.", "Spaced out."),
        ("", ""),
        (None, ""),
    ],
)
def test_first_sentence_rules(text, expected):
    assert _first_sentence(text) == expected
