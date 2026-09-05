"""Hook registry and default permission pipeline."""

from __future__ import annotations

import re

from harness.mcp.pool import mcp_tool_meta
from harness.messages.blocks import block_field
from harness.permission_policy import (
    PermissionPolicyConfigError,
    auto_approve_levels,
    classify_tool,
)
from harness.permission_session import get_permission_mode, get_permission_session
from harness.permissions.engine import evaluate_permission
from harness.permissions.state import (
    add_persistent_rule,
    add_session_rule,
    audit_permission,
)
from harness.settings import WORKDIR, get_workdir
from harness.tools.filesystem import safe_path
from harness.ui import events
from harness.ui.permission_prompt import (
    PermissionResponse,
    ask_allow as _DEFAULT_ASK_ALLOW,
    ask_permission,
)

# Keep the legacy boolean seam available for embedders/tests that used the
# earlier ``ask_allow`` prompt API.  Normal production flow below continues to
# use structured ``ask_permission`` so session/always remembering remains
# available.
ask_allow = _DEFAULT_ASK_ALLOW

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


def _safe_audit(event: dict) -> None:
    """Best-effort audit helper; permission decisions must never crash a turn."""
    try:
        audit_permission(event)
    except Exception:
        pass


def permission_hook(block, session=None):
    name = block_field(block, "name", "")
    if not isinstance(name, str):
        name = str(name or "")
    tool_input = block_field(block, "input", {}) or {}
    if not isinstance(tool_input, dict):
        # Malformed provider blocks are denied/handled conservatively rather
        # than crashing the main loop while trying to inspect ``.get``.
        tool_input = {}

    def hard_deny(message: str) -> str:
        """Record and return a non-overridable process/safety denial."""
        _safe_audit(
            {
                "event": "blocked",
                "tool": name,
                "resource": "",
                "reason": message,
                "source": "safety",
            }
        )
        return message

    if name == "bash":
        if "cwd" in tool_input:
            return hard_deny(
                "Permission denied: bash working directory is execution-owned and cannot be set by the model"
            )
        command = str(tool_input.get("command", "") or "")
        for pattern in DENY_LIST:
            if pattern in command:
                return hard_deny(f"Permission denied: '{pattern}' is on the deny list")
        if _NESTED_AGENT_RE.search(command):
            return hard_deny(
                "Permission denied: do not spawn a nested interactive agent "
                "(python main.py / run_cli / start cmd). Run the user's target "
                "script or service in-process with a finite command; if it needs "
                "a separate terminal, tell the user the exact command to run."
            )

    if name in ("write_file", "edit_file", "patch_file"):
        path = tool_input.get("path", "")
        try:
            safe_path(path)
        except Exception:
            return hard_deny(f"Permission denied: path escapes workspace: {path}")

    # Goal workers have their own thread-local authority and supervisor
    # boundary.  Resolve this before applying the interactive mode overlay so
    # a user's /permission choice can never widen a Goal contract.
    from harness.goal.runner import is_goal_noninteractive, mark_goal_permission_pending

    # Test harnesses/embedded callers may carry the Goal marker directly on a
    # serialized block.  The runner's thread-local flag remains authoritative
    # for production execution paths.
    explicit_goal = any(
        block_field(block, key, False) is True
        for key in ("goal_context", "goal_noninteractive", "supervisor_boundary")
    )
    if isinstance(tool_input, dict):
        explicit_goal = explicit_goal or any(
            tool_input.get(key) is True
            for key in ("goal_context", "goal_noninteractive", "supervisor_boundary", "_goal_context")
        )
    goal_noninteractive = is_goal_noninteractive() or explicit_goal

    # The static risk classifier is deliberately separate from the allow/ask/
    # deny engine.  ``blocked`` is a hard red line and is handled before any
    # mode can auto-approve it.  A malformed policy is fail-closed.
    risk: str | None = None
    session_mode = None
    if not goal_noninteractive:
        try:
            # Read the live holder for every ordinary request (including
            # already-allowed low-risk tools) so a mode change between tool
            # calls is observed deterministically.
            if session is None:
                session_mode = get_permission_mode()
            else:
                session_mode = (
                    session.get_mode() if hasattr(session, "get_mode") else session.mode
                )
            risk = classify_tool(
                name,
                tool_input if isinstance(tool_input, dict) else {},
            )
        except (PermissionPolicyConfigError, OSError, ValueError) as exc:
            return hard_deny(f"Permission denied: permission policy unavailable ({exc})")
        except Exception as exc:
            return hard_deny(f"Permission denied: permission policy unavailable ({exc})")
        if risk == "blocked":
            return hard_deny(
                f"Permission denied: {name} is classified as blocked by the safety policy"
            )
        if risk not in {"low", "medium", "high"}:
            # Third-party classifiers must not accidentally introduce a new
            # auto-approvable level.  Unknown values are treated conservatively.
            risk = "high"

    try:
        decision = evaluate_permission(
            name,
            tool_input if isinstance(tool_input, dict) else {},
            mcp_meta=mcp_tool_meta.get(name),
        )
    except Exception as exc:
        return hard_deny(f"Permission denied: permission engine unavailable ({exc})")
    _safe_audit(
        {
            "event": "decision",
            "tool": name,
            "resource": decision.resource,
            "effect": decision.effect,
            "reason": decision.reason,
            "source": decision.source,
            "save_tool": decision.save_tool,
            "save_resource": decision.save_resource,
            "risk": risk or "goal",
            "mode": session_mode or "goal",
        }
    )
    if decision.effect == "deny":
        _safe_audit(
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

    # Goal file capabilities are narrower than ordinary permission rules, so
    # enforce them even when the generic policy says ``allow``. Other allowed
    # tools keep their normal policy behavior; unresolved ``ask`` decisions
    # become supervisor boundaries instead of interactive prompts.
    if goal_noninteractive and (
        name in {"write_file", "edit_file", "patch_file"} or decision.effect != "allow"
    ):
        from harness.goal.authority import evaluate_goal_authority

        scoped = evaluate_goal_authority(
            name,
            tool_input if isinstance(tool_input, dict) else {},
        )
        if scoped.allowed:
            _safe_audit(
                {
                    "event": "goal_scope_allow",
                    "tool": name,
                    "resource": decision.resource,
                    "path": scoped.path,
                    "reason": scoped.reason,
                }
            )
            return None
        request = {
            "tool": name,
            "resource": str(decision.resource or ""),
            "path": scoped.path,
            "reason": scoped.reason or decision.reason,
            "policy_reason": decision.reason,
            "source": decision.source,
            "external_resource": str(decision.external_resource or ""),
        }
        if name == "bash":
            request["command"] = str(tool_input.get("command") or "")[:2_000]
        mark_goal_permission_pending(request)
        _safe_audit({"event": "goal_supervisor_boundary", **request})
        return (
            f"Permission deferred: {name} on {decision.resource!r} is outside the current Goal capability. "
            "The global supervisor will analyze this request at the next safe checkpoint."
        )

    if decision.effect == "allow":
        return None

    # Apply the selected session mode only to ordinary interactive ``ask``
    # decisions.  Explicit safety/external-directory asks remain prompts even
    # in full-access mode; those gates protect boundaries that a convenience
    # mode must not silently remove.  Saved explicit approvals still arrive as
    # ``allow`` above and therefore retain their existing semantics.
    if not goal_noninteractive and decision.effect == "ask" and risk is not None:
        try:
            mode = session_mode
            if mode is None:
                mode = get_permission_mode()
            auto_levels = set(auto_approve_levels(mode))
        except (PermissionPolicyConfigError, OSError, ValueError) as exc:
            return hard_deny(f"Permission denied: permission policy unavailable ({exc})")
        except Exception as exc:
            return hard_deny(f"Permission denied: permission policy unavailable ({exc})")
        boundary_ask = (
            decision.source == "safety"
            or decision.save_tool == "external_directory"
            or bool(decision.external_resource)
        )
        # Never auto-approve an unknown tool.  Unknown tools are classified as
        # ``high`` for conservative risk accounting, but treating that level as
        # equivalent to a known high-risk tool would let full-access silently
        # execute newly introduced or misspelled handlers.  MCP fallback
        # decisions are also kept interactive unless an explicit config rule
        # classified the tool.
        # Config files commonly contain a catch-all ``*`` rule.  Its source is
        # still ``config`` even for a name that has no registered handler, so
        # inspect the canonical registry as well as the decision source.
        try:
            from harness.tools.registry import BUILTIN_HANDLERS

            known_builtin = name in BUILTIN_HANDLERS
        except Exception:
            known_builtin = False
        known_mcp = name in mcp_tool_meta
        unknown_or_fallback = (
            decision.source in {"default", "mcp"}
            or (not known_builtin and not known_mcp)
        )
        if risk in auto_levels and not boundary_ask and not unknown_or_fallback:
            _safe_audit(
                {
                    "event": "auto_approved",
                    "tool": name,
                    "resource": decision.resource,
                    "effect": "allow",
                    "risk": risk,
                    "mode": mode,
                    "reason": f"{mode} auto-approves {risk}",
                    "source": "session_mode",
                }
            )
            return None

    _hook_print(f"[permission] {name} requires approval", warn=True)
    if decision.resource and decision.resource != "*":
        _hook_print(f"  {decision.resource}")
    if decision.external_resource:
        _hook_print(f"  external: {decision.external_resource}")
    if events.is_enabled():
        from harness.agent.cancel import is_cancelled
        from harness.ui.permission_events import request_permission
        choice = request_permission(
            name,
            decision.resource or name,
            f"Allow {name}?",
            cancel_check=is_cancelled,
        )
        response = PermissionResponse("jsonl-permission", choice, decision.resource or name)
    else:
        # ``ask_allow`` is retained as a compatibility seam.  It is only used
        # when a caller explicitly replaces that legacy symbol; otherwise use
        # the structured prompt with session/always choices.
        if ask_allow is not _DEFAULT_ASK_ALLOW:
            try:
                choice = ask_allow(
                    "  Allow? [y/N] ",
                    detail=decision.resource or name,
                    title=f"Allow {name}?",
                )
            except TypeError:
                # A minimal test/embedding callback may accept no keyword
                # arguments; keep the compatibility path forgiving.
                choice = ask_allow("  Allow? [y/N] ")
            if isinstance(choice, PermissionResponse):
                response = choice
            else:
                response = PermissionResponse(
                    "classic-permission",
                    "cancel" if choice is None else ("allow" if bool(choice) else "deny"),
                    decision.resource or name,
                )
        else:
            response = ask_permission(
                "  Allow? [y/N] ",
                detail=decision.resource or name,
                title=f"Allow {name}?",
                editable=name == "bash",
                remember=True,
            )
    _safe_audit(
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
    if name in ("write_file", "edit_file", "patch_file"):
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
