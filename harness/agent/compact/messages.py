"""Message shape helpers for compaction (tool pairs, latest user focus)."""

from __future__ import annotations

from harness.messages.blocks import block_type
from harness.messages.injections import is_harness_injection_text

LATEST_USER_FOCUS_MARKER = "[Current user request]"

def message_has_tool_use(message: dict) -> bool:
    if message.get("role") != "assistant":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(block_type(block) == "tool_use" for block in content)


def is_tool_result_message(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def collect_tool_results(messages: list):
    found = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                found.append((message_index, block_index, block))
    return found


def _user_text_content(message: dict) -> str | None:
    if message.get("role") != "user":
        return None
    if is_tool_result_message(message):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        text = "\n".join(part for part in parts if part).strip()
        return text or None
    return None


def _is_harness_system_user_text(text: str) -> bool:
    return text.startswith(LATEST_USER_FOCUS_MARKER) or is_harness_injection_text(text)


def find_latest_user_text(messages: list) -> str | None:
    """Latest real user instruction (skips tool results and harness injects)."""
    for message in reversed(messages):
        text = _user_text_content(message)
        if text and not _is_harness_system_user_text(text):
            return text
    return None


def focus_latest_user_message(text: str) -> dict:
    """User message that must outrank any conflicting compact summary goals."""
    body = text.strip()
    return {
        "role": "user",
        "content": (
            f"{LATEST_USER_FOCUS_MARKER}\n"
            "This is the latest user instruction. It overrides any conflicting "
            "goals, remaining work, or older tasks in a [Compacted] summary above.\n\n"
            f"{body}"
        ),
    }


def ensure_latest_user_focus(compacted: list, original_messages: list) -> list:
    latest = find_latest_user_text(original_messages)
    if not latest:
        return compacted
    focused = focus_latest_user_message(latest)
    # Drop a trailing duplicate plain copy of the same user text (keep one focused).
    if compacted:
        trailing = _user_text_content(compacted[-1])
        if trailing == latest or (
            trailing
            and trailing.startswith(LATEST_USER_FOCUS_MARKER)
            and latest in trailing
        ):
            return [*compacted[:-1], focused]
    return [*compacted, focused]
