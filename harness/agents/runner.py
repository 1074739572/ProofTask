"""Run typed subagents with isolated context and bound models."""

from __future__ import annotations

import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

from harness.agent.cancel import is_cancelled
from harness.agent.recovery import RecoveryState, with_retry
from harness.agents.registry import get_agent_profile, validate_agent_model
from harness.hooks import trigger_hooks
from harness.llm import create_message
from harness.messages.blocks import block_field, is_tool_use
from harness.project.session import serialize_messages
from harness.settings import MAX_RETRIES, get_workdir
from harness.skills_loader import load_skill
from harness.tools.dispatch import call_tool_handler, extract_text, has_tool_use
from harness.tools.filesystem import (
    run_bash,
    run_edit,
    run_glob,
    run_patch_file,
    run_read,
    run_search_text,
    run_write,
)
from harness.ui.renderer import renderer
from harness.usage.parse import parse_cache_usage

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
    "search_text": {
        "name": "search_text",
        "description": "Search text in workspace files without running shell commands.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
                "case_sensitive": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
    },
    "patch_file": {
        "name": "patch_file",
        "description": "Apply multiple exact file replacements atomically with an optional content hash.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "expected_sha256": {"type": "string"},
                "hunks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "occurrence": {"type": "integer"},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
            },
            "required": ["path", "hunks"],
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
    "search_text": run_search_text,
    "patch_file": run_patch_file,
    "load_skill": load_skill,
}


@dataclass
class AgentTaskStats:
    """Bounded subagent execution facts for workflow orchestrators."""

    llm_rounds: int = 0
    interrupted: bool = False
    stop_reason: str = "completed"  # completed | cancelled | deadline | max_rounds | max_tokens
    tool_count: int = 0
    tool_names: list[str] = field(default_factory=list)
    read_paths: list[str] = field(default_factory=list)
    write_paths: list[str] = field(default_factory=list)
    # Short, content-free acknowledgements from successful write tools.  Goal
    # handoffs need these to distinguish an approved request from a write that
    # actually reached the filesystem.
    write_outcomes: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    write_audits: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    # Provider-reported token usage accumulated across this bounded slice.
    # These remain zero when a provider omits usage metadata.
    input_tokens: int = 0
    output_tokens: int = 0

    def record_tool(self, name: str, tool_input: Any, output: object) -> None:
        self.tool_count += 1
        self.tool_names.append(str(name))
        if isinstance(tool_input, dict) and tool_input.get("path"):
            path = str(tool_input["path"]).replace("\\", "/")
            if name == "read_file":
                self.read_paths.append(path)
            elif name in {"write_file", "edit_file", "patch_file"}:
                self.write_paths.append(path)
        detail = str(output)
        if name in {"write_file", "edit_file", "patch_file"}:
            audit = re.search(r"sha256\s+([0-9a-f]{12,64})\s*->\s*([0-9a-f]{12,64})", detail, re.IGNORECASE)
            if audit:
                self.write_audits.append(audit.group(0))
            if detail.startswith(("Wrote ", "Edited ", "Patched ")):
                self.write_outcomes.append(f"{name} {path}: {detail[:300]}")
        if detail.lower().startswith((
            "error",
            "write blocked",
            "read blocked",
            "tool error",
            "permission denied",
            "permission deferred",
        )):
            self.tool_errors.append(f"{name}: {detail[:300]}")


@dataclass
class AgentTaskConversation:
    """In-memory message history shared by restartable agent slices."""

    messages: list[dict[str, Any]] = field(default_factory=list)

    def continue_with(self, prompt: str) -> list[dict[str, Any]]:
        if not self.messages:
            self.messages.append({"role": "user", "content": prompt})
            return self.messages

        instruction = {"type": "text", "text": prompt}
        last = self.messages[-1]
        if last.get("role") != "user":
            self.messages.append({"role": "user", "content": prompt})
            return self.messages

        content = last.get("content")
        if isinstance(content, list):
            content.append(instruction)
        elif isinstance(content, str):
            last["content"] = f"{content}\n\n{prompt}"
        else:
            last["content"] = prompt
        return self.messages


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
        "search_text": partial(run_search_text, cwd=bound_cwd),
        "patch_file": partial(run_patch_file, cwd=bound_cwd),
        "load_skill": load_skill,
    }
    if write_roots:
        roots = tuple((bound_cwd / root).resolve() for root in write_roots)

        def write_is_permitted(tool_name: str, path: str, candidate: Path) -> bool:
            """Use the active Goal authority as the sole Goal write boundary.

            Goal permission_hook already evaluates this authority.  Rechecking
            a separately reconstructed ``write_roots`` tuple here allowed the
            two guards to disagree on the same path.
            """
            try:
                from harness.goal.authority import current_goal_authority, evaluate_goal_authority

                if current_goal_authority() is not None:
                    return evaluate_goal_authority(tool_name, {"path": path}).allowed
            except ImportError:
                pass
            return any(candidate.is_relative_to(root) for root in roots)

        def guarded_write(*, path: str, content: str) -> str:
            try:
                candidate = (bound_cwd / path).resolve()
            except OSError as exc:
                return f"Write blocked: invalid path {path!r}: {exc}"
            if not write_is_permitted("write_file", path, candidate):
                _record_goal_scope_request("write_file", path, candidate, bound_cwd)
                return "Write blocked: path is outside the current agent write scope."
            return run_write(path=path, content=content, cwd=bound_cwd)

        def guarded_edit(*, path: str, old_text: str, new_text: str, occurrence: int = 1) -> str:
            try:
                candidate = (bound_cwd / path).resolve()
            except OSError as exc:
                return f"Edit blocked: invalid path {path!r}: {exc}"
            if not write_is_permitted("edit_file", path, candidate):
                _record_goal_scope_request("edit_file", path, candidate, bound_cwd)
                return "Edit blocked: path is outside the current agent write scope."
            return run_edit(path=path, old_text=old_text, new_text=new_text, occurrence=occurrence, cwd=bound_cwd)

        if "write_file" in handlers:
            handlers["write_file"] = guarded_write
        if "edit_file" in handlers:
            handlers["edit_file"] = guarded_edit
        if "patch_file" in handlers:
            base_patch = handlers["patch_file"]

            def guarded_patch(*, path: str, hunks: list[dict], expected_sha256: str = "") -> str:
                try:
                    candidate = (bound_cwd / path).resolve()
                except OSError as exc:
                    return f"Patch blocked: invalid path {path!r}: {exc}"
                if not write_is_permitted("patch_file", path, candidate):
                    _record_goal_scope_request("patch_file", path, candidate, bound_cwd)
                    return "Patch blocked: path is outside the current agent write scope."
                return base_patch(path=path, hunks=hunks, expected_sha256=expected_sha256)

            handlers["patch_file"] = guarded_patch
    if read_roots is not None or read_paths is not None:
        roots = tuple((bound_cwd / root).resolve() for root in (read_roots or ()))
        paths = tuple((bound_cwd / path).resolve() for path in (read_paths or ()))

        def permitted(candidate: Path) -> bool:
            return candidate in paths or any(candidate.is_relative_to(root) for root in roots)

        def guarded_read(*, path: str, limit: int | None = None, offset: int | None = None) -> str:
            try:
                candidate = (bound_cwd / path).resolve()
            except OSError as exc:
                return f"Read blocked: invalid path {path!r}: {exc}"
            if not permitted(candidate):
                return "Read blocked: this agent may only read its assigned discovery paths."
            return run_read(path=path, limit=limit, offset=offset, cwd=bound_cwd)

        if "read_file" in handlers:
            handlers["read_file"] = guarded_read
        if "search_text" in handlers:
            base_search = handlers["search_text"]

            def guarded_search(
                *, pattern: str, path: str = ".", max_results: int = 100, case_sensitive: bool = True,
            ) -> str:
                try:
                    candidate = (bound_cwd / path).resolve()
                except OSError as exc:
                    return f"Search blocked: invalid path {path!r}: {exc}"
                if not permitted(candidate):
                    return "Search blocked: this agent may only search its assigned discovery paths."
                return base_search(
                    pattern=pattern,
                    path=path,
                    max_results=max_results,
                    case_sensitive=case_sensitive,
                )

            handlers["search_text"] = guarded_search
        if "glob" in handlers:
            base_glob = handlers["glob"]

            def guarded_glob(*, pattern: str) -> str:
                # The non-wildcard prefix is the directory a glob has to walk.
                # Requiring that prefix to be readable prevents a constrained
                # agent from turning ``**/*`` into a dependency-tree dump.
                raw_pattern = str(pattern or "").replace("\\", "/").lstrip("./")
                prefix: list[str] = []
                for part in raw_pattern.split("/"):
                    if not part or any(token in part for token in ("*", "?", "[")):
                        break
                    prefix.append(part)
                try:
                    candidate = (bound_cwd.joinpath(*prefix)).resolve()
                except OSError as exc:
                    return f"Glob blocked: invalid pattern {pattern!r}: {exc}"
                if not permitted(candidate):
                    return "Glob blocked: this agent may only glob its assigned discovery paths."
                return base_glob(pattern=pattern)

            handlers["glob"] = guarded_glob
    if "rag_search" in allowed:
        from harness.rag.tools import run_rag_search

        handlers["rag_search"] = run_rag_search
    if "rag_status" in allowed:
        from harness.rag.tools import run_rag_status

        handlers["rag_status"] = run_rag_status
    handlers = {name: handlers[name] for name in allowed}
    return tools, handlers


def _record_goal_scope_request(
    tool_name: str,
    raw_path: str,
    candidate: Path,
    cwd: Path,
) -> None:
    """Expose a Goal capability miss without weakening the write guard."""
    try:
        from harness.goal.authority import current_goal_authority
        from harness.goal.runner import is_goal_noninteractive, mark_goal_permission_pending

        authority = current_goal_authority()
        if authority is None or not is_goal_noninteractive():
            return
        relative = candidate.relative_to(authority.workspace).as_posix()
        mark_goal_permission_pending({
            "tool": tool_name,
            "resource": str(raw_path),
            "path": relative,
            "reason": "requested path is outside the current Task write scope",
            "policy_reason": "agent write capability boundary",
            "source": "goal_write_scope",
            "external_resource": "",
            "cwd": str(cwd),
        })
    except (ImportError, OSError, ValueError):
        return


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
    max_tokens: int = 8_000,
    stream_response: bool = False,
    request_read_timeout_seconds: float | None = None,
    max_request_attempts: int | None = None,
    conversation: AgentTaskConversation | None = None,
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
    messages = (
        conversation.continue_with(prompt)
        if conversation is not None
        else [{"role": "user", "content": prompt}]
    )

    # Every subagent run gets its own id so the TUI can group all nested
    # round/tool events into one scoped block instead of leaking them into the
    # main timeline as flat logs.
    run_id = uuid.uuid4().hex[:8]
    renderer.subagent_start(run_id, agent_type, description, profile.model_id)
    usage_context: dict[str, str] = {
        "agent_type": agent_type,
        "agent_run_id": run_id,
    }
    try:
        # Capture Goal authority on this thread before the model request moves
        # to its cancellable daemon. Thread-local authority does not cross
        # that boundary by itself.
        from harness.goal.authority import current_goal_authority

        authority = current_goal_authority()
        if authority is not None:
            usage_context.update({
                "goal_id": authority.goal_id,
                "task_id": authority.task_id,
                "goal_phase": authority.phase,
            })
    except ImportError:
        pass
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
                                max_tokens=max(1, int(max_tokens)),
                                usage_context=usage_context,
                                force_stream=stream_response,
                                read_timeout_seconds=request_read_timeout_seconds,
                            ),
                            recovery,
                            max_attempts=(
                                max_request_attempts
                                if max_request_attempts is not None
                                else MAX_RETRIES
                            ),
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
            usage = parse_cache_usage(getattr(response, "usage", None))
            if usage is not None:
                stats.input_tokens += max(0, int(usage.input_tokens or 0))
                stats.output_tokens += max(0, int(usage.output_tokens or 0))
            if getattr(response, "stop_reason", None) == "max_tokens":
                stats.stop_reason = "max_tokens"

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
            name = block_field(block, "name", "")
            tool_input = block_field(block, "input", {}) or {}
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                output = str(blocked)
                renderer.subagent_tool(run_id, name, tool_input, tool_use_id=tool_use_id)
                if stats is not None:
                    stats.record_tool(name, tool_input, output)
                renderer.subagent_tool(
                    run_id,
                    name,
                    tool_input,
                    output,
                    tool_use_id=tool_use_id,
                )
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
                handler = handlers.get(name)
                renderer.subagent_tool(run_id, name, tool_input, tool_use_id=tool_use_id)
                output = call_tool_handler(handler, tool_input, name)
                if stats is not None:
                    stats.record_tool(name, tool_input, output)
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
    if stats is not None:
        stats.elapsed_seconds = elapsed

    exhausted_rounds = final_text is None
    if exhausted_rounds and stats is not None and stats.stop_reason == "completed":
        # The last turn requested tools, so this is not an empty provider
        # reply. Callers that support continuation need to distinguish the
        # per-call round budget from a malformed or missing response.
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
    if stats is not None and stats.stop_reason == "max_rounds":
        summary = "round slice exhausted; continuation required"
        renderer.subagent_end(run_id, summary, tool_count, elapsed, max_len=50)
        return f"[{agent_type}] {summary} ({tool_count} tools, {elapsed:.1f}s)"
    if stats is not None and stats.stop_reason == "completed":
        stats.stop_reason = "empty_response"
    renderer.subagent_end(run_id, "failed: empty response", tool_count, elapsed, max_len=50)
    return f"[{agent_type}] failed: empty response ({tool_count} tools, {elapsed:.1f}s)"


def spawn_subagent(description: str) -> str:
    """Legacy entry: treat as explore task with description as prompt."""
    return run_agent_task(description, description, "explore")
