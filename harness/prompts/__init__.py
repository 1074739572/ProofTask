"""System prompt assembly for the stable, cacheable request prefix."""

from harness.prompts.dynamic import build_stable_session_context
from harness.prompts.ephemeral import (
    EPHEMERAL_MARKER,
    build_ephemeral_user_message,
    is_ephemeral_session_message,
    messages_with_ephemeral_context,
)
from harness.prompts.project_md import (
    apply_project_instructions,
    find_project_md,
    format_project_instructions_block,
)
from harness.prompts.sections import PROMPT_SECTIONS
from harness.prompts.static import assemble_static_system_prompt


def assemble_system_prompt(context: dict, *, base_system: str | None = None) -> str:
    """Return the stable provider system prompt for this session.

    The only variable sections are session/task-scoped instructions. Volatile
    operational state is represented by tools and durable conversation events,
    not by a per-request pseudo-user message.
    """
    static = base_system or assemble_static_system_prompt()
    # Project instructions and memory are loaded as session snapshots. Mode or
    # task constraints change only at an explicit user/task boundary.
    project_block = format_project_instructions_block(context).strip()
    session_block = build_stable_session_context(context).strip()
    blocks = [static]
    if project_block:
        blocks.append(project_block)
    if session_block:
        blocks.append(session_block)
    return "\n\n".join(blocks)


__all__ = [
    "EPHEMERAL_MARKER",
    "PROMPT_SECTIONS",
    "apply_project_instructions",
    "assemble_static_system_prompt",
    "assemble_system_prompt",
    "build_ephemeral_user_message",
    "build_stable_session_context",
    "find_project_md",
    "format_project_instructions_block",
    "is_ephemeral_session_message",
    "messages_with_ephemeral_context",
]
