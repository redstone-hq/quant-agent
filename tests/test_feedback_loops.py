"""RC4 feedback-loop repairs (2026-07-16 forensics).

The self-grading loop was structurally self-exculpatory:
  - `value_entry_missed` (evening's actionable "we identified the entry and
    skipped it" category — SNDK flagged 16×) was code-filtered OUT of the
    recurring-miss digest, so PM was shown "(no recurring missed themes)"
    every run.
  - Misses were grouped by `theme_if_any` — LLM free text that never repeats
    verbatim — so symbol-level recurrence could not surface.
  - Sell grades were scored at t+1..t+3 by the LLM (97% "correct" while the
    tape showed 53% of exits ≥5% higher within 20 days). The deterministic
    post-exit reality block that answered it was deleted 2026-10-16: it fed
    the desk's own past exits back as an instruction (rule 3.14).
"""

import pytest
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from tests.pipeline_factory import build_pipeline


@pytest.fixture(autouse=True)
def _window_open_for_mechanics(monkeypatch):
    """These tests exercise grouping mechanics with clock-relative rows; the
    clean-record window itself is pinned in test_memory_windows_clean_record."""
    for mod in ("blocked", "exits"):
        m = f"src.prompt_facts.review.{mod}"
        monkeypatch.setattr(f"{m}.CLEAN_RECORD_DATE", "2000-01-01", raising=False)
        monkeypatch.setattr(f"{m}.CLEAN_RECORD_START_UTC", "2000-01-01T00:00:00", raising=False)


def _mk_pipeline():
    pipeline = build_pipeline(db=MagicMock(), broker=MagicMock())
    return pipeline


# ---------- deterministic post-exit reality ----------


def _trade(symbol: str, action: str, price: float, days_ago: int, fill_status: str = "filled") -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "symbol": symbol,
        "action": action,
        "price": price,
        "fill_price": price,
        "fill_status": fill_status,
        "timestamp": ts,
    }


def test_trade_grade_summary_has_no_post_exit_reality_block():
    pipeline = _mk_pipeline()
    pipeline.db.get_recent_insights.return_value = []
    pipeline.db.get_trades.return_value = [_trade("ZZZA", "SELL", 1000.0, days_ago=5)]
    pipeline.broker.get_latest_price.return_value = 1100.0
    assert "post_exit_reality" not in pipeline._build_trade_grade_summary()
    assert not hasattr(pipeline, "_build_post_exit_reality")


# ---------- reviewer prompt no longer carries the post-exit reality section ----------


def test_reviewer_prompt_has_no_post_exit_reality_section():
    from src.agents.position_reviewer import PositionReviewerAgent
    from unittest.mock import patch

    with patch("anthropic.Anthropic"):
        agent = PositionReviewerAgent(
            api_key="k",
            model="claude-opus-4-7",
            max_tokens=1024,
        )
    msg = agent.build_user_message(
        positions=[],
        macro_summary={},
        cash_balance=1000.0,
        total_value=100_000.0,
        session_type="midday",
        trade_grade_summary={
            "n_sells": 0,
            "n_buys": 0,
            "sell_counts": {},
            "buy_counts": {},
            "repeat_premature_symbols": [],
            "repeat_wrong_symbols": [],
            "post_exit_reality": {
                "n": 4,
                "n_higher_5pct": 3,
                "avg_move_pct": 8.5,
                "worst": [{"symbol": "LLY", "date": "2026-06-18", "move_pct": 12.4}],
            },
        },
    )
    # Deleted 2026-10-16: feeding the desk's own past exit outcomes back as an
    # instruction is fitting to its record (REBUILD_SPECIFICATION rule 3.14).
    assert "Post-exit reality" not in msg
    assert "tape says your exits fire too early" not in msg
    assert "must name a hard trigger" not in msg
