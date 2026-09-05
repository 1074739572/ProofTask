"""Shared tool dispatch helpers."""

from __future__ import annotations

from harness.messages.blocks import block_text, is_text, is_tool_use


def validate_tool_input(schema: dict | None, args: dict | None) -> str | None:
    """Validate the small JSON-schema subset used by tool definitions.

    This intentionally avoids a heavyweight dependency while enforcing the
    constraints that protect handlers from malformed provider arguments:
    object shape, required fields, primitive types, and numeric bounds.
    """
    if not schema:
        return None
    data = args or {}
    if schema.get("type") == "object" and not isinstance(data, dict):
        return "tool input must be an object"
    required = schema.get("required") or []
    missing = [key for key in required if key not in data]
    if missing:
        return f"missing required field(s): {', '.join(map(str, missing))}"
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unknown = [key for key in data if key not in properties]
        if unknown:
            return f"unknown field(s): {', '.join(map(str, unknown))}"
    for key, rule in properties.items():
        if key not in data or not isinstance(rule, dict):
            continue
        value = data[key]
        expected = rule.get("type")
        if expected == "string" and not isinstance(value, str):
            return f"field {key!r} must be a string"
        if expected == "boolean" and not isinstance(value, bool):
            return f"field {key!r} must be a boolean"
        if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"field {key!r} must be an integer"
        if expected == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return f"field {key!r} must be a number"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if rule.get("minimum") is not None and value < rule["minimum"]:
                return f"field {key!r} is below minimum {rule['minimum']}"
            if rule.get("maximum") is not None and value > rule["maximum"]:
                return f"field {key!r} exceeds maximum {rule['maximum']}"
        if isinstance(value, str) and rule.get("maxLength") is not None and len(value) > rule["maxLength"]:
            return f"field {key!r} exceeds maxLength {rule['maxLength']}"
    return None


def call_tool_handler(handler, args: dict, name: str, schema: dict | None = None) -> str:
    if not handler:
        return f"Unknown: {name}"
    validation_error = validate_tool_input(schema, args)
    if validation_error:
        return f"Tool input error ({name}): {validation_error}"
    try:
        return handler(**(args or {}))
    except TypeError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        block_text(block) for block in content if is_text(block)
    ).strip()


def has_tool_use(content) -> bool:
    if not isinstance(content, list):
        return False
    return any(is_tool_use(block) for block in content)
