"""Background execution for slow bash operations."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from harness.agent.compact.persist import stabilize_tool_results
from harness.agent.cancel import is_cancelled
from harness.hooks import trigger_hooks
from harness.messages.blocks import block_field
from harness.tools.dispatch import call_tool_handler

_bg_counter = 0


@dataclass(frozen=True)
class BackgroundEvent:
    task_id: str
    command: str
    phase: str
    preview: str = ""

background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()
_SLOW_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)("
    r"(?:python\s+-m\s+)?pytest\b|"
    r"(?:pip|python\s+-m\s+pip)\s+install\b|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:install|add|build|test)\b|"
    r"(?:cargo|go)\s+(?:build|test)\b|"
    r"(?:uv|poetry)\s+(?:sync|install|add|build)\b|"
    r"git\s+clone\b|"
    r"docker\s+(?:build|compose\s+up|pull)\b|"
    r"make(?:\s|$)|"
    r"(?:gradle|mvn)\s+.*(?:build|test|package)\b"
    r")",
    re.IGNORECASE,
)


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    command = str(tool_input.get("command", ""))
    return bool(_SLOW_COMMAND_RE.search(command))


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    if tool_name != "bash":
        return False
    return bool(tool_input.get("run_in_background")) or is_slow_operation(
        tool_name, tool_input
    )


def start_background_task(block, handlers: dict) -> str:
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    name = block_field(block, "name", "")
    tool_input = block_field(block, "input", {}) or {}
    command = tool_input.get("command", name)

    def worker():
        # A queued background operation must not start after the turn was
        # cancelled.  The foreground loop may have returned while this daemon
        # thread was still waiting for a scheduler slot.
        if is_cancelled():
            with background_lock:
                background_tasks[bg_id]["status"] = "cancelled"
                background_results[bg_id] = "[cancelled before start]"
            return
        handler = handlers.get(name)
        result = call_tool_handler(handler, tool_input, name)
        trigger_hooks("PostToolUse", block, result)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = str(result)
        _push_background_event(
            BackgroundEvent(
                task_id=bg_id,
                command=str(command),
                phase="completed",
                preview=str(result)[:240],
            )
        )

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block_field(block, "id", ""),
            "command": command,
            "status": "running",
        }
    threading.Thread(target=worker, daemon=True).start()
    event = BackgroundEvent(
        task_id=bg_id,
        command=str(command),
        phase="running",
    )
    if not _push_background_event(event):
        print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id


def _push_background_event(event) -> bool:
    return False


def collect_background_results() -> list[str]:
    with background_lock:
        ready = [
            bg_id
            for bg_id, task in background_tasks.items()
            if task["status"] in {"completed", "cancelled"}
        ]
    notifications = []
    for bg_id in ready:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:2000] if len(output) > 2000 else output
        match = re.search(r"\[exit_code=(-?\d+)\]", str(output))
        if match:
            exit_code = int(match.group(1))
        else:
            exit_code = 0 if not str(output).startswith("Error:") else 1
        status = task.get("status", "completed")
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>{status}</status>\n"
            f"  <exit_code>{exit_code}</exit_code>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"  <output_chars>{len(output)}</output_chars>\n"
            f"</task_notification>"
        )
    return notifications


def build_user_content(results: list[dict]) -> list[dict]:
    # Tool output becomes immutable once appended to message history. Persist and
    # bound it here, at the single ingress shared by normal and cancelled rounds.
    content = stabilize_tool_results(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list) -> None:
    notes = collect_background_results()
    if notes:
        messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": note} for note in notes],
            }
        )
