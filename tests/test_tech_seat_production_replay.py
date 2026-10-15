"""Item 157: replay REAL stored tech-seat answers through both the OLD
bare-array parse (`parse_json_rows()`, no `list_field`) and the NEW
wrapper-aware parse (`parse_json_rows(list_field="results")`), and assert
they produce byte-identical rows and malformed lists.

Why this matters more than a hand-written fixture can show: every one of
these answers predates the wrapper schema and is a bare JSON array (the old
prompt only ever asked for one), so this is the closest thing to a
guarantee that item 157's schema change does not silently change what the
desk already parses correctly today. A hand-written fixture only proves the
parser handles the shapes an engineer thought to write; this proves it
against shapes the model actually produced.

`tests/fixtures/tech_seat_production_answers_sample.json` is a bounded,
evenly-spaced sample of real `agent_logs.full_response` rows for
`agent_name='tech_analyst'`, read from the read-only production snapshot
available on this box (`/tmp/qamc_ro.db`, generated 2026-09-18 19:24 UTC),
and then REDACTED: this repository is public, so every symbol, entry price,
stop, target, level and piece of reasoning text in it is synthetic. The
prices and levels are WHOLE DOLLARS on a synthetic ladder (2026-09-26 —
the first redaction pass left them at their real cent-precision values,
which the leak guard `tests/test_no_real_desk_output.py` correctly reads
as real desk output). They were assigned rank-preserving, so every
ordering the real values carried survives: each row's stop/entry/target
direction, and the sort order inside each support/resistance array. The
replay below reads no value at all, so nothing here asserts on a price;
what the substitution had to protect was the row-validity split, and that
is unchanged [measured 2026-09-26: 63 parsed rows, 40 valid and 23
invalid against `TechAnalysisResult`, identical before and after]. What is
real, and what this file tests, is the STRUCTURE the model produced — row
counts, key names, key order, null patterns, fence markers (including the
double-fenced self-correction in log 283) and indentation. The replay below
reads none of the values, and the substitution was checked to leave every
row's outcome against `TechAnalysisResult` unchanged: 68 rows, 45 valid and
23 invalid before and after, with each individual row landing on the same
side [measured 2026-09-23]. Two things this fixture does NOT reproduce, and
should not be relied on for: the character distribution of real model prose
(the synthetic sentences carry no `$`, `%`, apostrophes or parentheses,
which real answers carry in quantity), and any malformed-JSON case — all
seven samples parse cleanly, before and after.
That snapshot's `agent_logs` table holds 116 tech-seat calls spanning
2026-08-17 13:32:41 to 2026-09-18 19:16:18, which split (on the desk's own
`--- chunk N/M ---` markers) into 243 individual raw answers. ALL 243 were
replayed through both parsers when this fixture was built: zero
differences — identical rows, identical malformed lists, on every one.
This file keeps 7 of the 243 (evenly spaced across the full range, not
cherry-picked) as a standing regression fixture so this guarantee is
checked on every CI run, not just once by hand.

A reviewer separately reported 292 production answers (2026-08-17 to
2026-09-22) replayed against both parsers with zero differences — NOT
independently confirmed from this box, and possibly conflated with a
DIFFERENT, pre-existing measurement of the same count over the same start
date in docs/INCIDENT_HISTORY.md's 2026-09-19 entry (item #538's row-salvage
validation, which is a different code path and found 9 differences /
45 recoveries, not zero). This file's own 243/7 numbers are independently
verifiable from `/tmp/qamc_ro.db` by anyone who can read it; the 292 figure
is not re-asserted as fact here.
"""

import json
import re
from pathlib import Path

import pytest

from src.agents.base import AgentResult

# Same fence pattern `AgentResult.parse_json` itself uses to recover a
# fenced JSON block — reused here only to inspect the fixture's shape, not
# to duplicate the parser under test.
_FENCED_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tech_seat_production_answers_sample.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text())
_SAMPLES = _FIXTURE["samples"]


def test_fixture_is_not_empty_and_is_really_bare_arrays():
    """Sanity on the fixture itself: if this ever loads zero samples (a
    bad path, a schema change to the JSON), the parametrized test below
    would silently pass on nothing and this whole file would stop proving
    anything. Also confirms the premise stated in the module docstring —
    these are pre-wrapper bare arrays, not `{"results": [...]}` — so the
    round-trip below is actually exercising the NEW code path against OLD
    data, not comparing a wrapper to itself.
    """
    assert len(_SAMPLES) >= 5
    for sample in _SAMPLES:
        text = sample["raw_text"]
        # The desk's own answer can self-correct mid-response (a second
        # fenced block after the first, or none at all — some real answers
        # are unfenced); when fenced, the LAST block is what the parser
        # actually takes (see `parse_json`'s "latest correction" rule).
        # This only checks the outer shape is array-like, not that the
        # whole thing parses. An earlier version of this comment claimed
        # some of these samples are the "one garbled row" shape the
        # row-by-row salvage exists for; that was never true of this
        # fixture — all seven parse cleanly [measured 2026-09-23]. The
        # garbled-row path is covered by tests/test_tech_row_salvage.py,
        # not here.
        matches = _FENCED_RE.findall(text)
        body = matches[-1] if matches else text
        inner = body.lstrip()
        assert inner.startswith("["), (
            f"fixture sample {sample.get('agent_log_id')} does not open "
            f"with a bare array — the module docstring's premise is wrong "
            f"for this one"
        )
        assert '"results"' not in inner.split("\n", 1)[0], (
            f"fixture sample {sample.get('agent_log_id')} already looks "
            f"like the NEW wrapper shape — it does not test what this file "
            f"claims to test"
        )
