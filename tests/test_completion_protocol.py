"""Behavioral tests for dynamic completion results from the event-stream backend.

``completion_request`` must serve both:
- ``@path``: file and directory candidates (existing behavior);
- ``/prefix``: slash-command candidates (new behavior).

These assertions intentionally fail until the backend command-completion branch
is implemented.  The public contract remains one JSONL command/event pair:
``completion_request`` -> ``completion_result``.
"""

from __future__ import annotations

from harness.event_stream import _handle_completion_request


def _candidates(text: str, cursor: int | None = None) -> list[str]:
    result = _handle_completion_request(
        {
            "text": text,
            "cursor": len(text) if cursor is None else cursor,
            "request_id": "test-completion",
        }
    )
    assert result["request_id"] == "test-completion"
    assert isinstance(result["candidates"], list)
    return result["candidates"]


def test_slash_prefix_returns_matching_command_candidates():
    candidates = _candidates("/mo")

    assert "/model" in candidates
    assert "/mode" in candidates
    assert all(item.startswith("/mo") for item in candidates)


def test_goal_prefix_is_available_to_dynamic_command_completion():
    candidates = _candidates("/go")

    assert "/goal" in candidates


def test_slash_completion_uses_token_before_cursor_not_text_after_cursor():
    candidates = _candidates("/mo keep-this", cursor=3)

    assert "/model" in candidates
    assert "/mode" in candidates


def test_non_command_text_does_not_return_slash_candidates():
    assert _candidates("please /mo") == []
