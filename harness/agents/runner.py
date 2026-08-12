"""Run typed subagents with isolated context and bound models."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

from harness.agent.cancel import is_cancelled
from harness.agents.registry import get_agent_profile, validate_agent_model
from harness.hooks import trigger_hooks
from harness.llm import create_message
from harness.messages.blocks import block_field, is_tool_use
from harness.project.session import serialize_messages
from harness.settings import get_workdir
from harness.tools.dispatch import call_tool_handler, extract_text, has_tool_use
from harness.tools.filesystem import run_bash, run_edit, run_glob, run_read, run_write
from harness.ui.renderer import renderer

_BASE_TOOL_DEFS = {
    "bash": {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    "read_file": {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    "edit_file": {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    "glob": {
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    "rag_search": {
        "name": "rag_search",
        "description": "Search the local RAG index.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "source": {"type": "string"},
                "chapter": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    "rag_status": {
        "name": "rag_status",
        "description": "Show local RAG index status.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
}

_BASE_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


@dataclass
class AgentTaskStats:
    """Bounded subagent execution facts for workflow orchestrators."""

    llm_rounds: int = 0
    interrupted: bool = False
    stop_reason: str = "completed"  # completed | cancelled | deadline | max_rounds


def _tools_for_agent(
    allowed: list[str], cwd: Path | None = None
) -> tuple[list[dict], dict]:
    unknown = sorted(set(allowed) - set(_BASE_TOOL_DEFS))
    if unknown:
        raise ValueError(f"agent profile declares unknown tools: {', '.join(unknown)}")
    bound_cwd = (cwd or get_workdir()).resolve()
    tools = [_BASE_TOOL_DEFS[name] for name in allowed]
    handlers = {
        "bash": partial(run_bash, cwd=bound_cwd),
        "read_file": partial(run_read, cwd=bound_cwd),
        "write_file": partial(run_write, cwd=bound_cwd),
        "edit_file": partial(run_edit, cwd=bound_cwd),
        "glob": partial(run_glob, cwd=bound_cwd),
    }
    if "rag_search" in allowed:
        from harness.rag.tools import run_rag_search

        handlers["rag_search"] = run_rag_search
    if "rag_status" in allowed:
        from harness.rag.tools import run_rag_status

        handlers["rag_status"] = run_rag_status
    handlers = {name: handlers[name] for name in allowed}
    return tools, handlers


def run_agent_task(
    description: str,
    prompt: str,
    agent_type: str,
    *,
    cwd: str | Path | None = None,
    max_rounds: int = 30,
    cancel_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
    stats: AgentTaskStats | None = None,
) -> str:
    error = validate_agent_model(agent_type)
    if error:
        return f"Error: {error}"

    profile = get_agent_profile(agent_type)
    assert profile is not None

    base_workdir = get_workdir().resolve()
    agent_cwd = Path(cwd).resolve() if cwd else base_workdir
    if not agent_cwd.is_relative_to(base_workdir):
        return f"Error: agent working directory escapes workspace: {agent_cwd}"
    try:
        tools, handlers = _tools_for_agent(profile.tools, agent_cwd)
    except ValueError as exc:
        return f"Error: {exc}"
    workdir = str(agent_cwd)
    system = f"{profile.system}\n\nWorking directory: {workdir}"
    messages = [{"role": "user", "content": prompt}]

    # Every subagent run gets its own id so the TUI can group all nested
    # round/tool events into one scoped block instead of leaking them into the
    # main timeline as flat logs.
    run_id = uuid.uuid4().hex[:8]
    renderer.subagent_start(run_id, agent_type, description, profile.model_id)
    started = time.time()
    tool_count = 0
    round_num = 0
    final_text: str | None = None

    def interrupted() -> str | None:
        if cancel_check is not None and cancel_check():
            return "cancelled"
        if is_cancelled():
            return "cancelled"
        if deadline is not None and time.monotonic() >= deadline:
            return "deadline"
        return None

    for _ in range(max(1, max_rounds)):
        reason = interrupted()
        if reason:
            if stats is not None:
                stats.interrupted = True
                stats.stop_reason = reason
            renderer.subagent_end(run_id, f"stopped: {reason}", tool_count, time.time() - started, max_len=50)
            return f"[{agent_type}] stopped: {reason}"
        round_num += 1
        response = create_message(
            model_id=profile.model_id,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=8000,
        )
        if stats is not None:
            stats.llm_rounds += 1

        # Extract thinking text for this round (truncated to 50 chars)
        thinking_text = extract_text(response.content) or ""
        renderer.subagent_round(run_id, round_num, thinking_text, max_len=50)

        messages.append(
            serialize_messages([{"role": "assistant", "content": response.content}])[0]
        )
        if not has_tool_use(response.content):
            # Final round — no more tools
            final_text = thinking_text
            break

        results = []
        for block in response.content:
            reason = interrupted()
            if reason:
                if stats is not None:
                    stats.interrupted = True
                    stats.stop_reason = reason
                renderer.subagent_end(run_id, f"stopped: {reason}", tool_count, time.time() - started, max_len=50)
                return f"[{agent_type}] stopped: {reason}"
            if not is_tool_use(block):
                continue
            tool_count += 1
            tool_use_id = block_field(block, "id", "")
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = str(blocked)
            else:
                name = block_field(block, "name", "")
                tool_input = block_field(block, "input", {}) or {}
                handler = handlers.get(name)
                renderer.subagent_tool(run_id, name, tool_input, tool_use_id=tool_use_id)
                output = call_tool_handler(handler, tool_input, name)
                trigger_hooks("PostToolUse", block, output)
                # Nested collapsed tool line: ● tool  args → result
                renderer.subagent_tool(run_id, name, tool_input, str(output), tool_use_id=tool_use_id)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": str(output),
                }
            )
        messages.append({"role": "user", "content": results})

    elapsed = time.time() - started

    if stats is not None and final_text is None:
        stats.stop_reason = "max_rounds"

    # If we didn't capture a final_text (loop ran 30 rounds), extract it now
    if final_text is None:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                t = extract_text(msg["content"])
                if t:
                    final_text = t
                    break

    if final_text:
        renderer.subagent_end(run_id, final_text, tool_count, elapsed, max_len=50)
        return (
            f"[{agent_type} / {profile.model_id}] {description} "
            f"({tool_count} tools, {elapsed:.1f}s)\n\n{final_text}"
        )
    renderer.subagent_end(run_id, "finished without summary", tool_count, elapsed, max_len=50)
    return f"[{agent_type}] finished without summary ({tool_count} tools, {elapsed:.1f}s)"


def spawn_subagent(description: str) -> str:
    """Legacy entry: treat as explore task with description as prompt."""
    return run_agent_task(description, description, "explore")
