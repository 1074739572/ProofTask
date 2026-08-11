"""Regression tests for stateless per-request dynamic context."""

from __future__ import annotations

from unittest.mock import patch

from harness.prompts.ephemeral import messages_with_ephemeral_context


def test_ephemeral_context_is_present_on_every_api_request() -> None:
    messages = [{"role": "user", "content": "implement this"}]
    with patch("harness.prompts.ephemeral.build_session_context", return_value="Mode: plan"):
        first = messages_with_ephemeral_context(messages, {})
        second = messages_with_ephemeral_context(messages, {})
    assert first[-1]["content"].startswith("<session-context>")
    assert second[-1]["content"].startswith("<session-context>")


def test_ephemeral_context_never_mutates_persisted_messages() -> None:
    messages = [{"role": "user", "content": "original"}]
    with patch("harness.prompts.ephemeral.build_session_context", return_value="Mode: direct"):
        api_messages = messages_with_ephemeral_context(messages, {})
    assert len(messages) == 1
    assert len(api_messages) == 2
