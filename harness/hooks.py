"""Hook registry and default permission pipeline."""

from __future__ import annotations

import re

from harness.mcp.pool import mcp_tool_meta
from harness.messages.blocks import block_field
from harness.settings import WORKDIR
from harness.tools.filesystem import safe_path
from harness.ui.permission_prompt import ask_permission

HOOKS: dict[str, list] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]

# Word-boundary destructive tokens (avoid substring false positives like "from").
_DESTRUCTIVE_RE = re.compile(
    r"(?:^|[\s;&|])(?:rm|chmod)\s|"
    r"(?:^|[\s;&|])>\s*/etc/",
    re.IGNORECASE,
)

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
    """Route hook notices to TUI Steps when active; else classic stdout."""
    from harness.ui.tui.mode import is_tui_active

    if is_tui_active():
        from harness.ui.tui.bridge import BRIDGE

        if warn:
            BRIDGE.push_warn(message)
        else:
            BRIDGE.push_step(message)
        return
    if warn:
        print(f"\n\033[33m{message}\033[0m")
    else:
        print(f"\033[90m{message}\033[0m" if message.startswith("[HOOK]") else message)


# Playwright MCP tools that only browse / observe — not true destructive ops.
# (Playwright marks many as destructiveHint; prompting on every navigate breaks search.)
_MCP_BROWSE_SOFT_ALLOW = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_tabs",
        "browser_wait_for",
        "browser_resize",
        "browser_handle_dialog",
    }
)


def _mcp_tool_short_name(full_name: str) -> str:
    # mcp__playwright__browser_navigate → browser_navigate
    parts = full_name.split("__")
    return parts[-1] if len(parts) >= 3 else full_name


def permission_hook(block):
    name = block_field(block, "name", "")
    tool_input = block_field(block, "input", {}) or {}
    if name == "bash":
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
        if _DESTRUCTIVE_RE.search(command):
            _hook_print("[permission] destructive command", warn=True)
            _hook_print(f"  {command}")
            response = ask_permission(
                "  Allow? [y/N] ",
                detail=command,
                title="Allow destructive command?",
                editable=True,
            )
            if response.decision == "cancel":
                return "Permission denied: cancelled by user"
            if not response.allowed:
                return "Permission denied by user"
            edited = response.value.strip()
            if edited and edited != command:
                tool_input["command"] = edited
    if name in ("write_file", "edit_file"):
        path = tool_input.get("path", "")
        try:
            safe_path(path)
        except Exception:
            return f"Permission denied: path escapes workspace: {path}"
    if name.startswith("mcp__"):
        meta = mcp_tool_meta.get(name, {})
        # readOnly wins over a noisy destructiveHint (browse tools).
        if meta.get("readOnly"):
            return None
        short = _mcp_tool_short_name(name)
        if short in _MCP_BROWSE_SOFT_ALLOW:
            return None
        if meta.get("destructive"):
            _hook_print(f"[permission] MCP destructive tool: {name}", warn=True)
            response = ask_permission(
                "  Allow? [y/N] ",
                detail=name,
                title="Allow MCP destructive tool?",
            )
            if response.decision == "cancel":
                return "Permission denied: cancelled by user"
            if not response.allowed:
                return "Permission denied by user"
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
    from harness.prompts.goal_stickiness import augment_if_needed
    from harness.prompts.lookup import augment_query as augment_lookup
    from harness.prompts.lookup import is_lookup_active
    from harness.prompts.writing import augment_query as augment_writing
    from harness.prompts.writing import is_writing_query

    sticky = augment_if_needed(query)
    if sticky is not None:
        # Sticky augment can stack with lookup/writing when both apply.
        base = sticky
    else:
        base = None

    # Activate lookup when keywords trigger OR the user is in /mode lookup.
    if is_lookup_active(query):
        # Silent for the user: constraint is for the model only.
        looked = augment_lookup(query if base is None else base)
        return looked if looked is not None else base

    if is_writing_query(query):
        # Silent for the user: workflow hint is for the model only.
        written = augment_writing(query if base is None else base)
        return written if written is not None else base

    if base is not None:
        return base

    if not hooks_verbose():
        return None
    _hook_print(f"[HOOK] UserPromptSubmit: {WORKDIR}")
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
