"""Regression tests for stateless per-request dynamic context."""

from __future__ import annotations

from unittest.mock import patch

from harness.prompts.ephemeral import ephemeral_policy, messages_with_ephemeral_context


def test_runtime_ephemeral_policy_is_always_for_stateless_requests() -> None:
    assert ephemeral_policy() == "always"


def test_ephemeral_context_is_present_on_every_api_request() -> None:
    messages = [{"role": "user", "content": "implement this"}]
    with patch("harness.prompts.ephemeral.build_session_context", return_value="Mode: plan"):
        first = messages_with_ephemeral_context(messages, {})
        second = messages_with_ephemeral_context(messages, {})
    assert first[0]["content"].startswith("<session-context>")
    assert second[0]["content"].startswith("<session-context>")
    assert first[0]["content"].endswith("implement this")


def test_ephemeral_context_never_mutates_persisted_messages() -> None:
    messages = [{"role": "user", "content": "original"}]
    with patch("harness.prompts.ephemeral.build_session_context", return_value="Mode: direct"):
        api_messages = messages_with_ephemeral_context(messages, {})
    assert len(messages) == 1
    assert len(api_messages) == 1
    assert api_messages[0]["content"].endswith("original")
