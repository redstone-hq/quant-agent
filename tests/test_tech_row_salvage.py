"""One garbled row in the technical seat's answer must cost that row only.

The two fixtures are the seat's real answers from production, copied verbatim
out of `agent_logs.full_response` (row 456, run `intra_check-26f52bf2`,
2026-09-17 14:31), split at the desk's own `--- retry ---` marker:

  - first answer: five rows; SQQQ carries a bare `n/a` line;
  - retry answer: four rows; SQQQ carries `s"thesis_invalid_if"`.

The shared fragment parser kept ONE row of each (ZS, then MTZ), so ORCL and
ETN were lost from that decision although both answers carried them
well-formed, and the log called all of them "missing-from-response".
"""

import json
import logging
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import log_health as L
from src.agents.base import AgentResult
from src.agents.tech_analyst import TechAnalystAgent
from src.models import OHLCV, TechnicalIndicators, parse_telemetry

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = (FIXTURES / "tech_answer_20260917_intra_check_26f52bf2_first.txt").read_text()
RETRY = (FIXTURES / "tech_answer_20260917_intra_check_26f52bf2_retry.txt").read_text()
SUBMITTED = ["ORCL", "SQQQ", "ETN", "MTZ", "ZS"]


def _result(text: str) -> AgentResult:
    return AgentResult(raw_text=text, tokens_used=0, model="m")


def _symbols_data(symbols):
    bars = [OHLCV(date=date(2026, 9, 16), open=1.0, high=2.0, low=0.5, close=1.5, volume=1_000)]
    return [{"symbol": s, "bars": bars, "indicators": TechnicalIndicators(symbol=s, atr_14=1.0)} for s in symbols]


def _replay(answers, asked):
    """Anthropic-client stub answering each call with the next text, and
    recording which submitted symbols each prompt asked about."""
    queue = list(answers)

    def _respond(**kw):
        content = kw["messages"][0]["content"]
        asked.append([s for s in SUBMITTED if f"### {s}" in content])
        resp = MagicMock()
        resp.content = [MagicMock(text=queue.pop(0))]
        resp.usage.input_tokens = 1
        resp.usage.output_tokens = 1
        return resp

    client = MagicMock()
    client.messages.create.side_effect = _respond
    return client


# --- the parser --------------------------------------------------------------


def test_a_clean_answer_parses_exactly_as_before():
    rows = [{"symbol": "A"}, {"symbol": "B"}]
    salvage = _result(json.dumps(rows)).parse_json_rows()
    assert salvage.rows == rows and salvage.malformed == []


# --- the seat, end to end ------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Tech answer carried 1 malformed row(s) — dropped individually, the 4 "
        "well-formed row(s) beside them kept: SQQQ: Expecting property name",
        "Tech batch incomplete: submitted=5, parsed=4, validation-failed=[], "
        "malformed-in-response=['SQQQ'], missing-from-response=[] — retrying",
    ],
)
def test_the_health_report_treats_a_recoverable_broken_row_as_handled(line):
    """The loss, if any, is the later `unresolved after retry` line."""
    family = L.classify(line, "WARNING")
    assert family is not None and family.reason is None
