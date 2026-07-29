"""Persist and restore CLI conversation history.

``history.json`` is a legacy compatibility mirror.  After the session-binding
refactor it is **never written** by checkpoints — ``session.jsonl`` is the sole
authoritative record.  ``history.json`` is only used as a one-time migration
source for sessions created before the binding refactor.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.messages.sanitize import block_to_dict, sanitize_messages_for_api
from harness.prompts.ephemeral import is_ephemeral_session_message
from harness.settings import PROJECT_DIR

HISTORY_PATH = PROJECT_DIR / "history.json"


def _block_to_dict(block) -> dict:
    return block_to_dict(block)


def serialize_messages(messages: list) -> list[dict]:
    serialized = []
    for message in messages:
        if is_ephemeral_session_message(message):
            continue
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            serialized.append({"role": role, "content": content})
            continue
        if isinstance(content, list):
            serialized.append(
                {
                    "role": role,
                    "content": [_block_to_dict(block) for block in content],
                }
            )
            continue
        serialized.append({"role": role, "content": str(content)})
    return serialized


def messages_for_api(messages: list) -> list[dict]:
    """Serialize and sanitize for LLM provider requests."""
    return sanitize_messages_for_api(serialize_messages(messages))


def deserialize_messages(data: list[dict]) -> list:
    messages = []
    for message in data:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if isinstance(content, list):
            messages.append(
                {
                    "role": role,
                    "content": [
                        dict(item) if isinstance(item, dict) else _block_to_dict(item)
                        for item in content
                    ],
                }
            )
            continue
        messages.append({"role": role, "content": content})
    return messages


def save_history(messages: list) -> None:
    """Legacy compat — no longer writes history.json.

    ``session.jsonl`` is the authoritative record.  This function exists only
    so tests and old call sites don't break; it is a no-op.
    """
    # Intentionally empty — session.jsonl is the single source of truth.
    return


def load_history() -> list | None:
    from harness.project.session_store import bootstrap_session

    messages, _binding, _source = bootstrap_session()
    return messages if messages else None


def clear_history() -> None:
    from harness.project.session_store import clear_session
    from harness.project.session_registry import session_binding, read_active_session_id

    sid = read_active_session_id()
    if sid:
        clear_session(binding=session_binding(sid), archive=True)
