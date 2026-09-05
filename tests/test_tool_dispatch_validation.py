from harness.tools.dispatch import call_tool_handler, validate_tool_input


def test_validate_tool_input_enforces_required_types_and_bounds():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 5},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    assert "missing required" in (validate_tool_input(schema, {}) or "")
    assert "must be an integer" in (validate_tool_input(schema, {"query": "ok", "top_k": "2"}) or "")
    assert "maxLength" in (validate_tool_input(schema, {"query": "toolong"}) or "")
    assert "unknown field" in (validate_tool_input(schema, {"query": "ok", "extra": 1}) or "")


def test_call_tool_handler_returns_structured_input_error_without_calling_handler():
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return "ok"

    result = call_tool_handler(
        handler,
        {"value": "bad"},
        "demo",
        {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
    )
    assert result.startswith("Tool input error (demo):")
    assert calls == []
