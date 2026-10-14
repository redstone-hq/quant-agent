"""Response-format schema strictness.

Moved VERBATIM out of src/agents/base.py; base.py re-exports every name.
"""

import json
import logging

logger = logging.getLogger(__name__)


# Models whose schema could not be made strict-json-schema-compatible — this
# is logged exactly ONCE per model class (not once per call) so a chatty
# result model doesn't spam the log every single request.
_STRICT_SCHEMA_FALLBACK_LOGGED: set[str] = set()
_RESPONSE_FORMAT_CACHE: dict[str, dict] = {}

# The only providers `_openai_wire_call` (src/agents/base.py) sends a
# `response_format` to. Anthropic, OpenAI and DeepSeek calls carry no schema.
SCHEMA_PROVIDERS = frozenset({"openrouter", "google"})


def route_takes_schema(provider: str, result_model: type | None, structured_output: bool, agent_name: str) -> bool:
    """False when a seat with an answer schema would land on a route that
    cannot be sent it. The ladder in `BaseAgent._execute` then skips that
    backup rung as an ABSENCE of an attempt (no exception recorded, the same
    as a demoted rung), so the seat fails over or refuses exactly as an
    exhausted chain does — it is never answered by unschema'd free text.
    """
    if not structured_output or result_model is None or provider in SCHEMA_PROVIDERS:
        return True
    logger.error(
        "Agent %s: skipping backup route %s — it cannot take the %s answer schema.",
        agent_name,
        provider,
        result_model.__name__,
    )
    return False


def _strictify_schema(node: object) -> None:
    """Recursively force every JSON-Schema object node to
    additionalProperties=false with every property required, in place.

    OpenRouter/OpenAI "strict" structured outputs require this shape: a
    field that is logically optional must still be listed in `required` and
    instead allow `null` in its own type/anyOf (pydantic already emits that
    for `Optional[...]` fields — this function does not need to add it,
    only to stop treating "has a default" as "may be omitted").
    """
    if isinstance(node, dict):
        # OpenAI strict mode rejects sibling keywords next to `$ref`
        # (measured 2026-09-14: "$ref cannot have keywords {'default'}" for
        # EarningsAnalysis.strategic_direction). Keep the bare reference.
        if "$ref" in node:
            for key in [k for k in node if k != "$ref"]:
                del node[key]
            return
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties")
            if isinstance(props, dict):
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
        for value in node.values():
            _strictify_schema(value)
    elif isinstance(node, list):
        for item in node:
            _strictify_schema(item)


def _has_free_form_map(node: object) -> bool:
    if isinstance(node, dict):
        if isinstance(node.get("additionalProperties"), dict):
            return True
        return any(_has_free_form_map(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_free_form_map(v) for v in node)
    return False


def _response_format_for(model_cls: type) -> dict:
    """Build the OpenRouter `response_format` extra for `model_cls`.

    Tries strict mode first (see
    https://openrouter.ai/docs/features/structured-outputs). If the
    schema can't be made strict-compatible for any reason, falls back to
    strict:false with the model's plain schema and logs it once — never
    silently drops response_format altogether.
    """
    name = model_cls.__name__
    cached = _RESPONSE_FORMAT_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        schema = model_cls.model_json_schema()
        strict_schema = json.loads(json.dumps(schema))
        # Strict mode cannot express free-form maps (dict[str, X] ->
        # additionalProperties: {schema}); OpenAI rejected
        # NewsIntelligenceReport for it 2026-09-14. Such a model goes
        # strict=false for EVERY candidate model alike.
        if _has_free_form_map(strict_schema):
            raise ValueError("free-form map not strict-compatible")
        _strictify_schema(strict_schema)
        result = {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": strict_schema},
        }
    except Exception:
        if name not in _STRICT_SCHEMA_FALLBACK_LOGGED:
            logger.warning(
                "response_format: %s schema could not be made strict-compatible; sending strict=false instead",
                name,
            )
            _STRICT_SCHEMA_FALLBACK_LOGGED.add(name)
        result = {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "strict": False,
                "schema": model_cls.model_json_schema(),
            },
        }
    _RESPONSE_FORMAT_CACHE[name] = result
    return result


# Model prefixes that route to OpenAI
