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
    """Return an API-only copy with session context before conversation history.

    The session block is deliberately present on every stateless request. Putting
    it before the first user turn keeps the block and the original request in a
    stable prefix through a tool loop, which lets providers reuse that prefix in
    their prompt cache without making the model forget runtime constraints.
    """
    body = build_session_context(context).strip()
    if not body:
        return list(messages)
    ephemeral = build_ephemeral_user_message(context)
    if not ephemeral:
        return list(messages)
    if messages and messages[0].get("role") == "user" and isinstance(messages[0].get("content"), str):
        first = dict(messages[0])
        first["content"] = f"{ephemeral}\n\n{first['content']}"
        return [first, *messages[1:]]
    return [{"role": "user", "content": ephemeral}, *messages]
