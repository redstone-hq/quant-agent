"""Per-row reading of a list-shaped model answer — standalone.

The answer is read with plain ``json.loads`` and never patched: a seat with an
answer schema is served only by routes that enforce it (see
``route_takes_schema`` in src/agents/llm_schema.py). What survives here is the
desk-specific record docs/OUTCOME.md requires — every row the seat asked for
and could not use is named with its symbol and a reason, never silently lost.
Nothing here knows about ``AgentResult``, ``BaseAgent``, a provider, a seat or
a transport.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class MalformedRow:
    """One row the seat asked for that the answer did not deliver as JSON.

    `key` is the row's identifying field (e.g. the symbol), or None when it is
    not known. `reason` is the decoder's own message, short enough for one log
    line.
    """

    key: str | None
    reason: str


@dataclass
class RowSalvage:
    """Result of `parse_json_rows_text`: every row the answer carried, plus a
    `MalformedRow` per expected key when the answer was not JSON at all. A row
    in neither list was genuinely not returned by the model."""

    rows: list
    malformed: list[MalformedRow]


def parse_json_rows_text(
    raw_text: str,
    list_field: str | None = None,
    expected_keys: Iterable[str] = (),
) -> RowSalvage | None:
    """Rows of an answer that is a LIST of objects.

    `list_field`, when given, names the wrapper key a strict provider-side
    schema roots the list under (e.g. `{"results": [...]}` — see
    `TechAnalystAnswer` in src/models). A bare list is accepted as-is, and any
    other object is one row, which the seat's own per-row validation then
    accepts or drops with its own reason.

    When the answer is not JSON, nothing is guessed out of the text: every
    `expected_keys` entry is returned as a `MalformedRow` carrying the
    decoder's message, so the seat records a per-symbol refusal. Returns None
    only when the answer is not JSON and no keys were expected.
    """
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        reason = f"answer is not JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        malformed = [MalformedRow(key, reason) for key in expected_keys]
        return RowSalvage(rows=[], malformed=malformed) if malformed else None
    if list_field is not None and isinstance(parsed, dict) and isinstance(parsed.get(list_field), list):
        return RowSalvage(rows=parsed[list_field], malformed=[])
    return RowSalvage(rows=parsed if isinstance(parsed, list) else [parsed], malformed=[])
