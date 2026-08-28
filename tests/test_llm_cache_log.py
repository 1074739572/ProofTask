"""Cache usage logging stays silent unless HARNESS_VERBOSE=1."""

from types import SimpleNamespace
from unittest.mock import patch

from harness.llm import _log_cache_usage


def _response(*, hit: int = 8, miss: int = 2, out: int = 3):
    usage = SimpleNamespace(
        prompt_cache_hit_tokens=hit,
        prompt_cache_miss_tokens=miss,
        output_tokens=out,
    )
    return SimpleNamespace(usage=usage)


def test_cache_usage_silenced_by_default(monkeypatch):
    monkeypatch.delenv("HARNESS_VERBOSE", raising=False)
    with (
        patch("harness.llm.record_usage") as record,
        patch("harness.llm.renderer") as renderer,
    ):
        _log_cache_usage(_response(), model_id="qwen-max")
        record.assert_called_once()
        renderer.muted.assert_not_called()


def test_cache_usage_prints_when_verbose(monkeypatch):
    monkeypatch.setenv("HARNESS_VERBOSE", "1")
    with (
        patch("harness.llm.record_usage"),
        patch("harness.llm.renderer") as renderer,
    ):
        _log_cache_usage(_response(), model_id="qwen-max")
        renderer.muted.assert_called_once()
        assert "hit=" in renderer.muted.call_args.args[0]


def test_usage_update_emitted_when_event_stream_enabled():
    import io
    import json
    from harness.ui import events

    sink = io.StringIO()
    events.enable_event_stream(sink)
    try:
        with patch("harness.llm.record_usage"):
            _log_cache_usage(_response(), model_id="qwen-max")
        lines = [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()]
        usage = [line for line in lines if line["type"] == "usage_update"]
        assert usage, "usage_update should be emitted"
        payload = usage[-1]
        assert payload["input_tokens"] == 10
        assert payload["output_tokens"] == 3
        assert payload["cache_read_tokens"] == 8
        assert payload["context_tokens"] is None
    finally:
        events.disable_event_stream()


def test_usage_update_keeps_agent_context_and_unknown_output_visible():
    import io
    import json
    from harness.ui import events

    response = SimpleNamespace(usage=SimpleNamespace(
        prompt_cache_hit_tokens=8,
        prompt_cache_miss_tokens=2,
    ))
    sink = io.StringIO()
    events.enable_event_stream(sink)
    try:
        with patch("harness.llm.record_usage") as record:
            _log_cache_usage(
                response,
                model_id="deepseek-v4-pro",
                usage_context={"agent_type": "goal_test_writer", "agent_run_id": "run-1"},
            )
        record.assert_called_once()
        assert record.call_args.kwargs["context"]["agent_type"] == "goal_test_writer"
        payload = [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()][-1]
        assert payload["agent_run_id"] == "run-1"
        assert payload["output_tokens"] is None
        assert payload["output_tokens_known"] is False
        assert payload["context_tokens"] is None
    finally:
        events.disable_event_stream()


def test_primary_session_usage_reports_authoritative_context_tokens():
    import io
    import json
    from harness.ui import events
    from harness.usage.context import current_context_tokens

    sink = io.StringIO()
    events.enable_event_stream(sink)
    try:
        with patch("harness.llm.record_usage"):
            _log_cache_usage(
                _response(hit=900, miss=100),
                model_id="qwen-max",
                usage_context={"session_id": "session-1"},
            )
        payload = [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()][-1]
        assert payload["context_tokens"] == 1000
        assert current_context_tokens("session-1", 25) == 1000
    finally:
        events.disable_event_stream()


def test_no_usage_update_when_usage_missing():
    import io
    from harness.ui import events

    sink = io.StringIO()
    events.enable_event_stream(sink)
    try:
        with patch("harness.llm.record_usage"):
            _log_cache_usage(SimpleNamespace(usage=None), model_id="qwen-max")
        assert sink.getvalue().strip() == ""
    finally:
        events.disable_event_stream()
