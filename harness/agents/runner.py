"""Run typed subagents with isolated context and bound models."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

from harness.agent.cancel import is_cancelled
from harness.agent.recovery import RecoveryState, with_retry
from harness.agents.registry import get_agent_profile, validate_agent_model
from harness.hooks import trigger_hooks
from harness.llm import create_message
from harness.messages.blocks import block_field, is_tool_use
from harness.project.session import serialize_messages
from harness.settings import get_workdir
from harness.skills_loader import load_skill
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
    "load_skill": {
        "name": "load_skill",
        "description": "Load a named workflow skill. This read-only tool does not grant new tools or permissions.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
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


def _provider_error_detail(exc: Exception) -> str:
    """Preserve the transport cause hidden by SDK wrapper exceptions."""

    detail = f"{type(exc).__name__}: {exc}"
    cause = exc.__cause__ or exc.__context__
    if cause is None:
        return detail
    cause_detail = f"{type(cause).__name__}: {cause}"
    return detail if cause_detail == detail else f"{detail} (cause: {cause_detail})"

_BASE_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "load_skill": load_skill,
}


@dataclass
class AgentTaskStats:
    """Bounded subagent execution facts for workflow orchestrators."""

    llm_rounds: int = 0
    interrupted: bool = False
    stop_reason: str = "completed"  # completed | cancelled | deadline | max_rounds


def _tools_for_agent(
    allowed: list[str], cwd: Path | None = None, write_roots: tuple[str, ...] | None = None,
    read_roots: tuple[str, ...] | None = None, read_paths: tuple[str, ...] | None = None,
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
        "load_skill": load_skill,
    }
    if write_roots:
        roots = tuple((bound_cwd / root).resolve() for root in write_roots)

        def guarded_write(*, path: str, content: str) -> str:
            try:
                candidate = (bound_cwd / path).resolve()
            except OSError as exc:
                return f"Write blocked: invalid path {path!r}: {exc}"
            if not any(candidate.is_relative_to(root) for root in roots):
                return "Write blocked: this agent may modify test files only."
            return run_write(path=path, content=content, cwd=bound_cwd)

        def guarded_edit(*, path: str, old_text: str, new_text: str, occurrence: int = 1) -> str:
            try:
                candidate = (bound_cwd / path).resolve()
            except OSError as exc:
                return f"Edit blocked: invalid path {path!r}: {exc}"
            if not any(candidate.is_relative_to(root) for root in roots):
                return "Edit blocked: this agent may modify test files only."
            return run_edit(path=path, old_text=old_text, new_text=new_text, occurrence=occurrence, cwd=bound_cwd)

        if "write_file" in handlers:
            handlers["write_file"] = guarded_write
        if "edit_file" in handlers:
            handlers["edit_file"] = guarded_edit
    if read_roots is not None or read_paths is not None:
        roots = tuple((bound_cwd / root).resolve() for root in (read_roots or ()))
        paths = tuple((bound_cwd / path).resolve() for path in (read_paths or ()))

        def guarded_read(*, path: str, limit: int | None = None, offset: int | None = None) -> str:
            try:
                candidate = (bound_cwd / path).resolve()
            except OSError as exc:
                return f"Read blocked: invalid path {path!r}: {exc}"
            allowed_path = candidate in paths or any(candidate.is_relative_to(root) for root in roots)
            if not allowed_path:
                return "Read blocked: this agent may only read its assigned discovery paths."
            return run_read(path=path, limit=limit, offset=offset, cwd=bound_cwd)

        if "read_file" in handlers:
            handlers["read_file"] = guarded_read
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
    write_roots: tuple[str, ...] | None = None,
    tools_override: tuple[str, ...] | None = None,
    read_roots: tuple[str, ...] | None = None,
    read_paths: tuple[str, ...] | None = None,
    reasoning_effort_override: str | None = None,
) -> str:
    error = validate_agent_model(agent_type, reasoning_effort=reasoning_effort_override)
    if error:
        if stats is not None:
            stats.stop_reason = "configuration_error"
        return f"Error: {error}"

    profile = get_agent_profile(agent_type)
    assert profile is not None
    selected_effort = reasoning_effort_override if reasoning_effort_override is not None else profile.reasoning_effort

    base_workdir = get_workdir().resolve()
    agent_cwd = Path(cwd).resolve() if cwd else base_workdir
    if not agent_cwd.is_relative_to(base_workdir):
        if stats is not None:
            stats.stop_reason = "configuration_error"
        return f"Error: agent working directory escapes workspace: {agent_cwd}"
    try:
        allowed_tools = profile.tools if tools_override is None else tools_override
        tools, handlers = _tools_for_agent(
            allowed_tools, agent_cwd, write_roots=write_roots,
            read_roots=read_roots, read_paths=read_paths,
        )
    except ValueError as exc:
        if stats is not None:
            stats.stop_reason = "configuration_error"
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
        # The provider SDK call is synchronous and may wait on the network for
        # a long time. Run it in a disposable daemon so Goal pause/cancel and
        # operation deadlines can take effect while the request is pending.
        response_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def request_model_response() -> None:
            try:
                recovery = RecoveryState()
                response_queue.put(
                    (
                        "response",
                        with_retry(
                            lambda: create_message(
                                model_id=profile.model_id,
                                reasoning_effort=selected_effort,
                                inherit_interactive_effort=False,
                                system=system,
                                messages=messages,
                                tools=tools,
                                max_tokens=8000,
                            ),
                            recovery,
                        ),
                    )
                )
            except Exception as exc:
                response_queue.put(("error", exc))

        threading.Thread(target=request_model_response, daemon=True).start()
        while True:
            try:
                outcome, value = response_queue.get(timeout=0.1)
                break
            except queue.Empty:
                reason = interrupted()
                if reason:
                    if stats is not None:
                        stats.interrupted = True
                        stats.stop_reason = reason
                    renderer.subagent_end(
                        run_id,
                        f"stopped while waiting for model: {reason}",
                        tool_count,
                        time.time() - started,
                        max_len=50,
                    )
                    return f"[{agent_type}] stopped while waiting for model: {reason}"
        if outcome == "error":
            exc = value
            detail = _provider_error_detail(exc)
            if stats is not None:
                stats.stop_reason = "provider_error"
            renderer.subagent_end(run_id, f"failed: {detail}", tool_count, time.time() - started, max_len=120)
            return f"[{agent_type}] failed: {detail}"
        response = value
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
                # A permission overlay can unblock after the caller has
                # requested pause/cancel. Re-check before executing the tool
                # so an old approval never runs after cancellation.
                reason = interrupted()
                if reason:
                    if stats is not None:
                        stats.interrupted = True
                        stats.stop_reason = reason
                    renderer.subagent_end(run_id, f"stopped: {reason}", tool_count, time.time() - started, max_len=50)
                    return f"[{agent_type}] stopped: {reason}"
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
        # An assistant turn with neither visible text nor tool calls is not a
        # successful completion.  Returning a generic "finished" marker lets
        # structured Goal stages accidentally continue after an empty reply.
        stats.stop_reason = "empty_response"

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
    if stats is not None:
        stats.stop_reason = "empty_response"
    renderer.subagent_end(run_id, "failed: empty response", tool_count, elapsed, max_len=50)
    return f"[{agent_type}] failed: empty response ({tool_count} tools, {elapsed:.1f}s)"


def spawn_subagent(description: str) -> str:
    """Legacy entry: treat as explore task with description as prompt."""
    return run_agent_task(description, description, "explore")
