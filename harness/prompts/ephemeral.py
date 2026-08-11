"""Attach per-turn session context to API messages without persisting to session history."""

from __future__ import annotations

from harness.prompts.dynamic import build_session_context

EPHEMERAL_MARKER = "<session-context>"
EphemeralPolicy = str  # "always" | "if_unchanged"

def ephemeral_policy() -> str:
    """Compatibility accessor for callers that inspect the old setting.

    Dynamic state is part of every stateless API request. Omitting it after a
    matching previous body made tool-loop and recovery calls forget the mode,
    project rules, and todos.
    """
    return "always"


def reset_ephemeral_cache() -> None:
    """Compatibility no-op; ephemeral context is intentionally uncached."""


def is_ephemeral_session_message(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, str) and content.startswith(EPHEMERAL_MARKER)


def build_ephemeral_user_message(context: dict) -> str | None:
    body = build_session_context(context).strip()
    if not body:
        return None
    return (
        f"{EPHEMERAL_MARKER}\n"
        "The following is current harness session state (not a new user request). "
        "Use it together with the conversation above.\n\n"
        f"{body}"
    )


def messages_with_ephemeral_context(
    messages: list,
    context: dict,
    *,
    policy: str | None = None,
) -> list:
    """Shallow copy of messages with session context appended for a single API call."""
    body = build_session_context(context).strip()
    if not body:
        return list(messages)
    ephemeral = build_ephemeral_user_message(context)
    if not ephemeral:
        return list(messages)
    return [*messages, {"role": "user", "content": ephemeral}]
