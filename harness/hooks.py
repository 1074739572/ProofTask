"""Hook registry and default permission pipeline."""

from __future__ import annotations

import re

from harness.mcp.pool import mcp_tool_meta
from harness.messages.blocks import block_field
from harness.permissions.engine import evaluate_permission
from harness.permissions.state import (
    add_persistent_rule,
    add_session_rule,
    audit_permission,
)
from harness.settings import WORKDIR, get_workdir
from harness.tools.filesystem import safe_path
from harness.ui import events
from harness.ui.permission_prompt import PermissionResponse, ask_permission

HOOKS: dict[str, list] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

# Spawning a nested interactive agent / new console hijacks the session.
_NESTED_AGENT_RE = re.compile(
    r"(?:^|[\s;&|])(?:python|py)\s+(?:-m\s+)?(?:main\.py|harness\.cli)\b|"
    r"\brun_cli\s*\(|"
    r"(?:^|[\s;&|])start\s+cmd\b|"
    r"os\.system\s*\(.*(?:start\s+cmd|run_cli)",
    re.IGNORECASE,
)


def register_hook(event: str, callback) -> None:
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def _hook_print(message: str, *, warn: bool = False) -> None:
    """Route hook notices to classic stdout; stay silent in JSONL event-stream mode."""
    from harness.ui import events

    if events.is_enabled():
        return
    if warn:
        print(f"\n\033[33m{message}\033[0m")
    else:
        print(f"\033[90m{message}\033[0m" if message.startswith("[HOOK]") else message)


def permission_hook(block):
    name = block_field(block, "name", "")
    tool_input = block_field(block, "input", {}) or {}

    if name == "bash":
        if "cwd" in tool_input:
            return "Permission denied: bash working directory is execution-owned and cannot be set by the model"
        command = tool_input.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                return f"Permission denied: '{pattern}' is on the deny list"
        if _NESTED_AGENT_RE.search(command):
            return (
                "Permission denied: do not spawn a nested interactive agent "
                "(python main.py / run_cli / start cmd). Run the user's target "
                "script or service in-process with a finite command; if it needs "
                "a separate terminal, tell the user the exact command to run."
            )

    if name in ("write_file", "edit_file"):
        path = tool_input.get("path", "")
        try:
            safe_path(path)
        except Exception:
            return f"Permission denied: path escapes workspace: {path}"

    decision = evaluate_permission(
        name,
        tool_input if isinstance(tool_input, dict) else {},
        mcp_meta=mcp_tool_meta.get(name),
    )
    audit_permission(
        {
            "event": "decision",
            "tool": name,
            "resource": decision.resource,
            "effect": decision.effect,
            "reason": decision.reason,
            "source": decision.source,
            "save_tool": decision.save_tool,
            "save_resource": decision.save_resource,
        }
    )
    if decision.effect == "allow":
        return None
    if decision.effect == "deny":
        audit_permission(
            {
                "event": "blocked",
                "tool": name,
                "resource": decision.resource,
                "reason": decision.reason,
            }
        )
        return (
            f"Permission denied: {name} on {decision.resource!r} "
            f"({decision.reason})"
        )

    _hook_print(f"[permission] {name} requires approval", warn=True)
    if decision.resource and decision.resource != "*":
        _hook_print(f"  {decision.resource}")
    if decision.external_resource:
        _hook_print(f"  external: {decision.external_resource}")
    # /goal ACTs run non-interactively: an `ask` is returned as a rejection
    # (never blocks on stdin / the TUI permission flow) and the runner pauses
    # with stop_reason=permission_wait. Thread-local, so normal foreground
    # turns keep the interactive ask behavior.
    from harness.goal.runner import is_goal_noninteractive, mark_goal_permission_pending

    if is_goal_noninteractive():
        mark_goal_permission_pending()
        audit_permission(
            {
                "event": "goal_noninteractive_deny",
                "tool": name,
                "resource": decision.resource,
                "reason": "goal ACT is non-interactive; human approval required",
            }
        )
        return (
            f"Permission denied: {name} needs human approval, but the goal "
            "runner is non-interactive. The goal will pause (permission_wait); "
            "approve it in config and /goal resume."
        )
    if events.is_enabled():
        from harness.ui.permission_events import request_permission
        choice = request_permission(name, decision.resource or name, f"Allow {name}?")
        response = PermissionResponse("jsonl-permission", choice, decision.resource or name)
    else:
        response = ask_permission(
            "  Allow? [y/N] ",
            detail=decision.resource or name,
            title=f"Allow {name}?",
            editable=name == "bash",
            remember=True,
        )
    audit_permission(
        {
            "event": "reply",
            "tool": name,
            "resource": decision.resource,
            "reply": response.decision,
            "save_tool": decision.save_tool,
            "save_resource": decision.save_resource,
        }
    )
    if response.decision == "cancel":
        return "Permission denied: cancelled by user"
    if not response.allowed:
        return "Permission denied by user"
    save_tool = decision.save_tool or name
    save_resource = decision.save_resource or decision.resource or "*"
    if response.remember_session:
        add_session_rule(save_tool, save_resource, "allow")
    if response.remember_always:
        add_persistent_rule(save_tool, save_resource, "allow")
    if name == "bash":
        edited = response.value.strip()
        command = tool_input.get("command", "")
        if edited and edited != command:
            tool_input["command"] = edited
    return None


def log_hook(block):
    from harness.ui.tool_display import hooks_verbose

    if not hooks_verbose():
        return None
    _hook_print(f"[HOOK] {block_field(block, 'name', '')}")
    return None


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        _hook_print(
            f"[HOOK] large output from {block_field(block, 'name', '')}: "
            f"{len(str(output))} chars",
            warn=True,
        )
    return None


def user_prompt_hook(query: str):
    from harness.ui.tool_display import hooks_verbose

    # User history must remain verbatim. Per-turn routing constraints are
    # assembled into ephemeral context by the CLI/event-stream entrypoints.
    if not hooks_verbose():
        return None
    _hook_print(f"[HOOK] UserPromptSubmit: {get_workdir()}")
    return None


def stop_hook(messages: list):
    from harness.ui.tool_display import hooks_verbose

    if not hooks_verbose():
        return None
    tool_count = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            tool_count += sum(
                1
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_result"
            )
    _hook_print(f"[HOOK] Stop: {tool_count} tool result(s) in session")
    return None


def project_write_hook(block, output):
    name = block_field(block, "name", "")
    tool_input = block_field(block, "input", {}) or {}
    if name in ("write_file", "edit_file"):
        path = tool_input.get("path", "")
        if path:
            try:
                from harness.project.resume import on_write_file

                on_write_file(path)
            except Exception:
                pass
    return None


register_hook("UserPromptSubmit", user_prompt_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("PostToolUse", project_write_hook)
register_hook("Stop", stop_hook)
