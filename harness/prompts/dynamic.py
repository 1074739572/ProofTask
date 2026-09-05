"""Per-turn session context (time, model, mode, todos, …) — not part of the static system prefix."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Literal

from harness.models import get_model, get_model_profile, model_label
from harness.modes import (
    mode_builtin_skills_section,
    mode_enables_task,
    mode_lead_model_hint,
    mode_prompt_section,
)
from harness.prompts.project_md import format_project_instructions_block
from harness.providers.config import get_provider
from harness.settings import WORKDIR
from harness.todos.format import format_todos_for_prompt
from harness.todos.state import get_todos

TimeGranularity = Literal["seconds", "minute"]


def _format_platform() -> str:
    """OS-aware shell guidance injected every turn so the agent picks the
    right command family up front (avoids ls→dir / grep→findstr churn).

    The ``bash`` tool runs via ``subprocess.Popen(shell=True)``, which on
    Windows means cmd.exe — Unix commands fail with confusing errors.
    """
    import platform as _platform

    system = _platform.system()  # "Windows" | "Linux" | "Darwin"
    if system == "Windows":
        return (
            "System environment: Windows. The `bash` tool executes via "
            "cmd.exe (shell=True), so use Windows command syntax:\n"
            "- `dir` not `ls`; `findstr` not `grep`; `type` not `cat`\n"
            "- `del` not `rm`; `copy`/`move` not `cp`/`mv`; `where` not `which`\n"
            "- no `tail`: use `findstr` or Python; no `touch`: use `type nul > file`\n"
            "- prefer the `glob` tool over `find`/`dir /s` for searching files\n"
            "- do NOT use multi-line `python -c` (cmd breaks newlines); "
            "write a temporary .py file and run it\n"
            "- path separator: backslash `\\` (quoted) or forward slash `/` both work"
        )
    if system == "Linux":
        return (
            "System environment: Linux. The `bash` tool executes via a POSIX "
            "shell; standard Unix commands (ls/grep/tail/find/cat) work."
        )
    if system == "Darwin":
        return (
            "System environment: macOS. The `bash` tool executes via a POSIX "
            "shell; standard Unix commands (ls/grep/tail/find/cat) work."
        )
    return f"System environment: {system}."


def _format_time(*, granularity: TimeGranularity) -> str:
    now = datetime.now()
    if granularity == "minute":
        now = now.replace(second=0, microsecond=0)
    return now.isoformat(timespec="seconds")


def default_time_granularity() -> TimeGranularity:
    """Minute by default to avoid needless timestamp churn."""
    raw = os.getenv("HARNESS_TIME_GRANULARITY", "minute").strip().lower()
    if raw in ("seconds", "second", "s"):
        return "seconds"
    return "minute"


def build_session_context(
    context: dict,
    *,
    include_time: bool = True,
    time_granularity: TimeGranularity | None = None,
    include_model: bool = True,
    include_mode: bool = True,
    include_memories: bool = True,
    include_mcp: bool = True,
    include_teammates: bool = True,
    include_todos: bool = True,
    include_project_instructions: bool = True,
    include_platform: bool = True,
) -> str:
    """Dynamic harness state for the per-request system context."""
    sections: list[str] = []
    granularity = time_granularity or default_time_granularity()

    if include_time:
        sections.append(f"Current time: {_format_time(granularity=granularity)}")

    if include_platform:
        sections.append(_format_platform())

    if include_model:
        current = get_model()
        profile = get_model_profile(current)
        label = model_label(current)
        try:
            provider_label = get_provider(profile.provider).label
        except KeyError:
            provider_label = profile.provider
        model_line = f"Current model: {current} [{provider_label}]"
        if profile.api_model != current:
            model_line += f" (API: {profile.api_model})"
        if profile.thinking:
            model_line += " [thinking]"
        if label != current:
            model_line += f" — {label}"
        sections.append(model_line)

    if include_mode:
        sections.append(mode_prompt_section())
        builtin = mode_builtin_skills_section()
        if builtin:
            sections.append(builtin)
        hint = mode_lead_model_hint()
        current = get_model()
        if hint and current != hint:
            if mode_enables_task():
                sections.append(
                    f"Mode lead binding: this mode runs the lead with {hint} "
                    f"(direct model selection: {current})."
                )
            else:
                sections.append(
                    f"Mode lead hint: {hint} is recommended for this mode "
                    f"(current: {current}). Switch with /model if needed."
                )

    if include_project_instructions:
        project_block = format_project_instructions_block(context)
        if project_block:
            sections.append(project_block)

    if include_memories and context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")

    if include_mcp:
        mcp_names = context.get("connected_mcp") or []
        if mcp_names:
            sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")

    if include_teammates and context.get("active_teammates"):
        sections.append(
            "Active teammates: " + ", ".join(context["active_teammates"])
        )

    if include_todos:
        todos_block = format_todos_for_prompt(get_todos())
        if todos_block:
            sections.append(todos_block)

    pending_question = context.get("pending_question")
    if isinstance(pending_question, dict):
        question = str(pending_question.get("text") or "").strip()
        if question:
            sections.append(
                "Pending clarification from the previous turn:\n"
                f"{question}\n"
                "Treat a short next user reply as the answer to this question "
                "unless the user explicitly starts a different request."
            )

    turn_constraints = (context.get("turn_constraints") or "").strip()
    if turn_constraints:
        sections.append(f"Turn constraints (internal, not a user message):\n{turn_constraints}")

    rag_boot = (context.get("rag_bootstrap") or "").strip()
    if rag_boot:
        sections.append(rag_boot)

    if context.get("writing_mode"):
        sections.append(
            "Writing mode active: use rag_search on indexed files/ before "
            "write_file to output/*.md; do not read_file whole reference docx."
        )

    return "\n\n".join(sections)


def build_stable_session_context(context: dict) -> str:
    """Session/task-scoped instructions suitable for the provider system prompt.

    Omit volatile operational data. Time comes from a tool when needed; tool
    schemas already describe MCP availability; todo updates and background
    results are durable conversation events. This shape changes only when a
    user intentionally changes task/mode-level state.
    """
    return build_session_context(
        context,
        include_time=False,
        include_model=False,
        include_mcp=False,
        include_teammates=False,
        include_todos=False,
        include_project_instructions=False,
    )
