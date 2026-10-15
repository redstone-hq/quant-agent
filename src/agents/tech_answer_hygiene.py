"""Answer-shape hygiene telemetry for the technical seat.

`_record_answer_hygiene` counts fenced-markdown and extra-key hits against the
`TechAnalystAnswer` schema, tagged by the provider that produced the answer.
Moved verbatim from src/agents/tech_analyst.py; the agent calls it once per
parsed chunk.
"""

from __future__ import annotations

import re

from src.models import TechAnalystAnswerItem, parse_telemetry

_TECH_ANSWER_ITEM_FIELDS = frozenset(TechAnalystAnswerItem.model_fields)
_FENCED_MARKDOWN_RE = re.compile(r"```")


def _record_answer_hygiene(raw_text: str, rows: list, provider: str) -> None:
    """Item 157's runtime check (2026-09-23): a strict `json_schema`
    response format is supposed to make fenced markdown around the JSON and
    undeclared keys on a row impossible. Whether it actually does, on either
    route, was meant to be confirmed by a live pytest call — but no
    deployed process ever holds a real `GOOGLE_API_KEY` for a pytest run to
    use (see docs/WORK.md item 157, tests/test_tech_schema_live.py), so
    that plan can never execute. This runs instead, on every real call,
    where the key actually is. It never changes what gets parsed or used —
    a hit here is evidence for a human deciding item 157, not a gate.

    `provider` is `AgentResult.actual_provider` for the call this answer
    came from. Only "openrouter" and "google" are ever given a
    response_format (see `_openai_wire_call` in src/agents/base.py), and
    since 2026-10-14 a backup rung on any other provider is skipped for this
    seat (`route_takes_schema`). Only a primary configured on another
    provider could still answer unschema'd; the tag keeps that case from
    being read as a schema failure (adversary review, 2026-09-23).
    """
    model_name = f"TechAnalystAnswer[{provider or 'unknown'}]"
    parse_telemetry.record_hygiene_observation(model_name)
    if _FENCED_MARKDOWN_RE.search(raw_text):
        parse_telemetry.record_hygiene_violation(model_name, "fenced_markdown")
    for row in rows:
        if isinstance(row, dict) and (set(row) - _TECH_ANSWER_ITEM_FIELDS):
            parse_telemetry.record_hygiene_violation(model_name, "extra_keys")
