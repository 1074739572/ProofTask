"""Per-category context composition: measured at request build, scaled to
the provider-reported prompt total, and reported over the event stream."""

from types import SimpleNamespace
from unittest.mock import patch

from harness.usage.context import (
    record_context_breakdown,
    scaled_context_breakdown,
)


def test_breakdown_scales_to_provider_total_and_sums_exactly():
    record_context_breakdown(
        "s1", system_tokens=100, tools_tokens=50, messages_tokens=250
    )
    parts = scaled_context_breakdown("s1", 1000)
    # Ratios 100:50:250 -> 25% / 12.5% / 62.5% of the API total.
    assert parts["system"] == 250
    assert parts["tools"] == 125
    assert parts["messages"] == 625
    assert sum(parts.values()) == 1000


def test_breakdown_without_measurement_attributes_everything_to_messages():
    parts = scaled_context_breakdown("never-called", 4321)
    assert parts == {"system": 0, "tools": 0, "messages": 4321}


def test_breakdown_rounding_remainder_lands_in_messages():
    record_context_breakdown("s2", system_tokens=1, tools_tokens=1, messages_tokens=1)
    parts = scaled_context_breakdown("s2", 10)
    assert sum(parts.values()) == 10
    assert parts["system"] == 3
    assert parts["tools"] == 3
    assert parts["messages"] == 4


def test_empty_session_id_is_ignored():
    record_context_breakdown("", system_tokens=1, tools_tokens=1, messages_tokens=1)
    assert scaled_context_breakdown("", 100) == {"system": 0, "tools": 0, "messages": 100}


def _response(*, prompt: int = 400):
    usage = SimpleNamespace(
        prompt_cache_hit_tokens=prompt // 2,
        prompt_cache_miss_tokens=prompt - prompt // 2,
        output_tokens=3,
    )
    return SimpleNamespace(usage=usage)


def test_usage_update_carries_scaled_breakdown_for_primary_turn():
    import io
    import json

    from harness.llm import _log_cache_usage
    from harness.ui import events

    record_context_breakdown("sess", system_tokens=100, tools_tokens=100, messages_tokens=200)
    sink = io.StringIO()
    events.enable_event_stream(sink)
    try:
        with patch("harness.llm.record_usage"):
            _log_cache_usage(
                _response(prompt=800),
                model_id="qwen-max",
                usage_context={"session_id": "sess"},
            )
        lines = [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()]
        payload = [line for line in lines if line["type"] == "usage_update"][-1]
        assert payload["context_tokens"] == 800
        assert payload["context_system"] == 200
        assert payload["context_tools"] == 200
        assert payload["context_messages"] == 400
    finally:
        events.disable_event_stream()


def test_usage_update_omits_breakdown_for_subagent_turn():
    import io
    import json

    from harness.llm import _log_cache_usage
    from harness.ui import events

    sink = io.StringIO()
    events.enable_event_stream(sink)
    try:
        with patch("harness.llm.record_usage"):
            _log_cache_usage(
                _response(),
                model_id="qwen-max",
                usage_context={"session_id": "sess", "agent_type": "goal_test_writer"},
            )
        lines = [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()]
        payload = [line for line in lines if line["type"] == "usage_update"][-1]
        assert payload["context_tokens"] is None
        assert payload["context_system"] is None
        assert payload["context_tools"] is None
        assert payload["context_messages"] is None
    finally:
        events.disable_event_stream()


def test_create_message_measures_request_composition_for_primary_turn():
    from harness import llm

    with (
        patch("harness.llm.create_provider_message", return_value=_response()),
        patch("harness.llm.record_usage"),
        patch("harness.llm.get_model_profile") as profile,
    ):
        profile.return_value = SimpleNamespace(
            id="m", provider="p", api_model="m", thinking=False
        )
        llm.create_message(
            messages=[{"role": "user", "content": "x" * 400}],
            max_tokens=100,
            system="y" * 200,
            tools=[{"name": "t", "description": "z" * 100}],
            usage_context={"session_id": "measure"},
        )
    parts = scaled_context_breakdown("measure", 1000)
    # system 200 chars -> 50, tools ~117 chars -> ~29, messages ~414 chars -> ~103;
    # only the ratios matter: system > tools and messages largest.
    assert parts["system"] > parts["tools"] > 0
    assert parts["messages"] > parts["system"]
    assert sum(parts.values()) == 1000


def test_create_message_skips_measurement_for_subagent_turn():
    from harness import llm

    with (
        patch("harness.llm.create_provider_message", return_value=_response()),
        patch("harness.llm.record_usage"),
        patch("harness.llm.get_model_profile") as profile,
    ):
        profile.return_value = SimpleNamespace(
            id="m", provider="p", api_model="m", thinking=False
        )
        llm.create_message(
            messages=[{"role": "user", "content": "x" * 400}],
            max_tokens=100,
            system="y" * 200,
            usage_context={"session_id": "sub", "agent_type": "goal_test_writer"},
        )
    # No measurement recorded: fallback attributes everything to messages.
    assert scaled_context_breakdown("sub", 500) == {
        "system": 0,
        "tools": 0,
        "messages": 500,
    }
