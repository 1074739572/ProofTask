"""Autonomous teammate threads with plan approval and idle polling."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from harness.llm import create_message
from harness.models import get_model
from harness.settings import (
    IDLE_POLL_INTERVAL,
    IDLE_TIMEOUT,
    TEAMMATE_MAX_RUNTIME,
    WORKTREES_DIR,
)
from harness.tasks import claim_task, complete_task, list_tasks, load_task, scan_unclaimed_tasks
from harness.teams.bus import BUS, active_teammates
from harness.teams.protocol import teammate_submit_plan
from harness.tools.dispatch import call_tool_handler, has_tool_use
from harness.tools.filesystem import run_bash, run_read, run_write


def idle_poll(
    agent_name: str,
    messages: list,
    name: str,
    role: str,
    worktree_context: dict | None = None,
) -> str:
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(
                        name,
                        "lead",
                        "Shutting down.",
                        "shutdown_response",
                        {"request_id": req_id, "approve": True},
                    )
                    return "shutdown"
            messages.append(
                {"role": "user", "content": "<inbox>" + json.dumps(inbox) + "</inbox>"}
            )
            return "work"
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                wt_info = ""
                if task_data.get("worktree"):
                    wt_path = WORKTREES_DIR / task_data["worktree"]
                    wt_info = f"\nWork directory: {wt_path}"
                    if worktree_context is not None:
                        worktree_context["path"] = str(wt_path)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"<auto-claimed>Task {task_data['id']}: "
                            f"{task_data['subject']}{wt_info}</auto-claimed>"
                        ),
                    }
                )
                return "work"
    return "timeout"


def _spawn_legacy_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    protocol_ctx = {"waiting_plan": None}
    started_at = time.monotonic()
    system = (
        f"You are '{name}', a {role}. "
        f"Use tools to complete tasks. "
        f"If a task has a worktree, work in that directory."
    )

    def handle_inbox_message(teammate_name: str, msg: dict, messages: list) -> bool:
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if msg_type == "shutdown_request":
            BUS.send(
                teammate_name,
                "lead",
                "Shutting down.",
                "shutdown_response",
                {"request_id": req_id, "approve": True},
            )
            return True
        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if req_id == protocol_ctx["waiting_plan"]:
                protocol_ctx["waiting_plan"] = None
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "[Plan approved]" if approve else f"[Plan rejected] {msg['content']}"
                    ),
                }
            )
        return False

    def run() -> None:
        outcome_type = "result"
        outcome_detail = ""
        tool_calls = 0
        wt_ctx: dict[str, str | None] = {"path": None}
        BUS.send(name, "lead", f"started: {role} — {prompt[:90]}", "progress")

        def _wt_cwd() -> Path | None:
            path = wt_ctx["path"]
            return Path(path) if path else None

        def _run_claim_task(task_id: str) -> str:
            result = claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = load_task(task_id)
                wt_ctx["path"] = (
                    str(WORKTREES_DIR / task.worktree) if task.worktree else None
                )
            return result

        sub_tools = [
            {
                "name": "bash",
                "description": "Run a shell command.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            {
                "name": "read_file",
                "description": "Read file with line numbers and total-line header; use limit/offset to page.",
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
            {
                "name": "write_file",
                "description": "Write file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "send_message",
                "description": "Send message to another agent.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["to", "content"],
                },
            },
            {
                "name": "submit_plan",
                "description": "Submit a plan for Lead approval.",
                "input_schema": {
                    "type": "object",
                    "properties": {"plan": {"type": "string"}},
                    "required": ["plan"],
                },
            },
            {
                "name": "list_tasks",
                "description": "List active tasks (completed are archived).",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "claim_task",
                "description": "Claim a pending task.",
                "input_schema": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
            {
                "name": "complete_task",
                "description": "Mark an in-progress task as completed.",
                "input_schema": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
        ]

        sub_handlers = {
            "bash": lambda command: run_bash(command, cwd=_wt_cwd()),
            "read_file": lambda path, limit=None, offset=0: run_read(
                path, limit=limit, offset=offset, cwd=_wt_cwd()
            ),
            "write_file": lambda path, content: run_write(path, content, cwd=_wt_cwd()),
            "send_message": lambda to, content: (BUS.send(name, to, content), "Sent")[1],
            "list_tasks": lambda: "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in list_tasks()
            )
            or "No tasks.",
            "claim_task": _run_claim_task,
            "complete_task": lambda task_id: (
                complete_task(task_id),
                wt_ctx.update({"path": None}),
            )[0],
        }

        messages = [{"role": "user", "content": prompt}]
        while True:
            if time.monotonic() - started_at >= TEAMMATE_MAX_RUNTIME:
                outcome_type = "timeout"
                outcome_detail = (
                    f"Worker runtime exceeded {TEAMMATE_MAX_RUNTIME}s before completion."
                )
                break
            if len(messages) <= 3:
                messages.insert(
                    0,
                    {
                        "role": "user",
                        "content": (
                            f"<identity>You are '{name}', role: {role}. "
                            f"Continue your work.</identity>"
                        ),
                    },
                )
            should_shutdown = False
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if handle_inbox_message(name, msg, messages):
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if protocol_ctx["waiting_plan"]:
                    time.sleep(IDLE_POLL_INTERVAL)
                    continue
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox if m.get("type") == "message"]
                    if non_protocol:
                        messages.append(
                            {
                                "role": "user",
                                "content": "<inbox>" + json.dumps(non_protocol) + "</inbox>",
                            }
                        )
                if time.monotonic() - started_at >= TEAMMATE_MAX_RUNTIME:
                    outcome_type = "timeout"
                    outcome_detail = (
                        f"Worker runtime exceeded {TEAMMATE_MAX_RUNTIME}s before an LLM call."
                    )
                    should_shutdown = True
                    break
                BUS.send(name, "lead", "thinking: requesting model response", "progress")
                try:
                    response = create_message(
                        model_id=get_model(),
                        system=system,
                        messages=messages[-20:],
                        tools=sub_tools,
                        max_tokens=8000,
                    )
                except Exception as exc:
                    outcome_type = "error"
                    outcome_detail = f"failed: LLM call failed: {type(exc).__name__}: {exc}"
                    should_shutdown = True
                    break
                messages.append({"role": "assistant", "content": response.content})
                if not has_tool_use(response.content):
                    outcome_detail = "done: completed assigned prompt"
                    should_shutdown = True
                    break
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    tool_calls += 1
                    if block.name == "submit_plan":
                        output = teammate_submit_plan(name, block.input.get("plan", ""))
                        match = re.search(r"\((req_\d+)\)", output)
                        protocol_ctx["waiting_plan"] = (
                            match.group(1) if match else output
                        )
                    else:
                        handler = sub_handlers.get(block.name)
                        from harness.ui.tool_display import summarize_tool_input
                        summary = summarize_tool_input(block.name, block.input)
                        phase = "reading" if block.name == "read_file" else "running"
                        BUS.send(
                            name,
                            "lead",
                            f"{phase}: {block.name}" + (f" {summary}" if summary else ""),
                            "progress",
                        )
                        output = call_tool_handler(handler, block.input, block.name)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        }
                    )
                    if protocol_ctx["waiting_plan"]:
                        break
                messages.append({"role": "user", "content": results})
                if protocol_ctx["waiting_plan"]:
                    break
            if should_shutdown:
                break
            if protocol_ctx["waiting_plan"]:
                continue
            idle_result = idle_poll(name, messages, name, role, wt_ctx)
            if idle_result in ("shutdown", "timeout"):
                break

        summary = ""
        for msg in reversed(messages):
            if msg["role"] != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if getattr(block, "type", None) == "text" and block.text.strip():
                    summary = block.text.strip()
                    break
            if summary:
                break
        if outcome_type != "result":
            prior_summary = summary
            summary = outcome_detail or f"Worker ended with {outcome_type}."
            if prior_summary:
                summary += f"\nLast worker text: {prior_summary}"
        elif not summary:
            summary = outcome_detail or (
                f"Worker ended without a text summary after {tool_calls} tool call(s)."
            )
        BUS.send(
            name,
            "lead",
            summary,
            outcome_type,
            {
                "elapsed_seconds": round(time.monotonic() - started_at, 1),
                "tool_calls": tool_calls,
            },
        )
        active_teammates.pop(name, None)

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    return f"Teammate '{name}' spawned as {role}"


def spawn_teammate_thread(
    name: str,
    role: str,
    prompt: str,
    *,
    agent_type: str | None = None,
) -> str:
    """Spawn a role-bound teammate through the typed subagent executor.

    The former teammate loop maintained a second, unrestricted tool registry.
    Keeping one executor makes model binding, tool allow-lists, and
    ``PreToolUse`` permission hooks apply to every worker path.
    """
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    resolved_type = agent_type or role.strip().split(maxsplit=1)[0]
    from harness.agents.registry import get_agent_profile
    from harness.agents.runner import run_agent_task

    if get_agent_profile(resolved_type) is None:
        return f"Error: unknown teammate role '{role}'; use a configured agent type"

    def run() -> None:
        started_at = time.monotonic()
        BUS.send(name, "lead", f"started: {resolved_type} worker", "progress")
        try:
            BUS.send(name, "lead", "thinking: requesting model response", "progress")
            summary = run_agent_task(role, prompt, resolved_type)
            outcome_type = "error" if summary.startswith("Error:") else "result"
        except Exception as exc:
            summary = f"Worker failed: {type(exc).__name__}: {exc}"
            outcome_type = "error"
        BUS.send(
            name,
            "lead",
            summary,
            outcome_type,
            {"elapsed_seconds": round(time.monotonic() - started_at, 1)},
        )
        active_teammates.pop(name, None)

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    return f"Teammate '{name}' spawned as {resolved_type}"
