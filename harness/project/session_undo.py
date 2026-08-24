"""Undo the last user turn (question + agent reply) in the active session."""

from __future__ import annotations

from harness.messages.repair import repair_tool_pairing, strip_trailing_incomplete_tool_turns
from harness.messages.injections import is_harness_injection_text
from harness.project.session_registry import SessionBinding
from harness.project.session_store import replace_session


def is_user_turn(message: dict) -> bool:
    """True for a real user prompt from the CLI (not tool results or injections)."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    text = content.strip()
    if not text:
        return False
    return not is_harness_injection_text(text)


def find_last_turn_index(messages: list) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if is_user_turn(messages[index]):
            return index
    return None


def preview_last_turn(messages: list) -> str | None:
    index = find_last_turn_index(messages)
    if index is None:
        return None
    content = messages[index].get("content", "")
    if not isinstance(content, str):
        return None
    preview = content.replace("\n", " ").strip()
    if len(preview) > 72:
        preview = preview[:72] + "…"
    removed = len(messages) - index
    return f'"{preview}" ({removed} message(s))'


def undo_last_turn(messages: list, *, binding: SessionBinding) -> tuple[bool, str]:
    index = find_last_turn_index(messages)
    if index is None:
        return False, "Nothing to undo (no previous user prompt in this session)."

    preview = preview_last_turn(messages)
    removed_count = len(messages) - index
    del messages[index:]
    replace_session(messages, binding=binding)
    return True, f"Undid last turn — removed {removed_count} message(s): {preview}"


def truncate_turn(messages: list, turn_start: int, *, keep_user: bool = True) -> int:
    """
    Remove in-progress assistant/tool messages for the turn that began at turn_start.
    If keep_user is False, remove the user prompt as well (/undo style).
    Returns number of messages removed.
    """
    if keep_user:
        end = turn_start + 1
    else:
        end = turn_start
    if end >= len(messages):
        return 0
    removed = len(messages) - end
    del messages[end:]
    return removed


def _user_query_at(messages: list, turn_start: int) -> str | None:
    if turn_start >= len(messages):
        return None
    msg = messages[turn_start]
    if not is_user_turn(msg):
        return None
    content = msg.get("content")
    return content if isinstance(content, str) else None


def resolve_turn_start(messages: list, turn_start: int | None) -> int | None:
    """Map CLI turn index to a valid user message after compact/rewrites."""
    if turn_start is not None and turn_start < len(messages):
        if is_user_turn(messages[turn_start]):
            return turn_start
    return find_last_turn_index(messages)


def abort_inflight_turn(
    messages: list,
    turn_start: int | None,
    *,
    binding: SessionBinding,
    keep_user: bool = False,
) -> tuple[str, str | None]:
    """
    Interrupt handler: trim the in-flight turn and persist.

    Default (keep_user=False): remove the user prompt and partial agent output
    from session, return the prompt text so the CLI can offer it again for edit.
    """
    strip_trailing_incomplete_tool_turns(messages)
    repair_tool_pairing(messages)

    resolved = resolve_turn_start(messages, turn_start)
    if resolved is None:
        removed = strip_trailing_incomplete_tool_turns(messages)
        replace_session(messages, binding=binding)
        if removed:
            return f"Interrupted — cleaned {removed} orphaned tool message(s).", None
        return "Interrupted — session already clean.", None

    user_query = _user_query_at(messages, resolved)
    removed = truncate_turn(messages, resolved, keep_user=keep_user)
    strip_trailing_incomplete_tool_turns(messages)
    repair_tool_pairing(messages)
    replace_session(messages, binding=binding)

    if keep_user:
        if removed:
            msg = (
                f"Interrupted — removed {removed} partial message(s). "
                "Your prompt is kept in session; use /undo to drop it."
            )
        else:
            msg = "Interrupted — no partial output yet. Your prompt is kept in session."
        return msg, None

    total = removed + (1 if user_query else 0)
    if user_query:
        preview = user_query.replace("\n", " ").strip()
        if len(preview) > 60:
            preview = preview[:60] + "…"
        msg = (
            f"Interrupted — rolled back {total} message(s). "
            f"Edit and resend: \"{preview}\""
        )
    elif removed:
        msg = f"Interrupted — rolled back {removed} partial message(s)."
    else:
        msg = "Interrupted — nothing to roll back."
    return msg, user_query

