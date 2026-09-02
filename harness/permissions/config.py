"""Load declarative tool permission rules."""

from __future__ import annotations

import copy
import json
import os
from typing import Literal, TypeAlias

from harness.settings import get_permissions_config_path

PermissionEffect: TypeAlias = Literal["allow", "ask", "deny"]
PermissionRule: TypeAlias = PermissionEffect | dict[str, PermissionEffect]

_VALID_EFFECTS = {"allow", "ask", "deny"}

DEFAULT_PERMISSIONS: dict[str, PermissionRule] = {
    "*": "ask",
    "external_directory": "ask",
    "read_file": {
        "*": "allow",
        "*.env": "deny",
        "*.env.*": "deny",
        "*.env.example": "allow",
    },
    "inspect_file": {
        "*": "allow",
        "*.env": "deny",
        "*.env.*": "deny",
        "*.env.example": "allow",
    },
    "search_text": "allow",
    "git_status": "allow",
    "git_diff": "allow",
    "glob": "allow",
    "todo_write": "allow",
    "load_skill": "allow",
    "compact": "allow",
    "list_tasks": "allow",
    "get_task": "allow",
    "check_inbox": "allow",
    "list_crons": "allow",
    "rag_search": "allow",
    "rag_status": "allow",
    "project_status": "allow",
    "web_search": "allow",
    "mcp__fetch__fetch": "allow",
    "mcp__playwright__browser_navigate": "allow",
    "mcp__playwright__browser_navigate_back": "allow",
    "mcp__playwright__browser_snapshot": "allow",
    "mcp__playwright__browser_take_screenshot": "allow",
    "mcp__playwright__browser_console_messages": "allow",
    "mcp__playwright__browser_network_requests": "allow",
    "mcp__playwright__browser_wait_for": "allow",
    "mcp__playwright__browser_resize": "allow",
    "mcp__playwright__browser_tabs": "ask",
    "mcp__*": "ask",
    "write_file": "ask",
    "edit_file": "ask",
    "patch_file": "ask",
    "bash": {
        "*": "ask",
        "dir *": "allow",
        "type *": "allow",
        "where *": "allow",
        "git status*": "allow",
        "git diff*": "allow",
        "git log*": "allow",
        "python -c *": "ask",
        "rm *": "deny",
        "del *": "ask",
        "sudo *": "deny",
        "shutdown*": "deny",
        "reboot*": "deny",
    },
}

# Named permission policy presets.  A config may select one with
# ``{"permission": "<preset name>"}`` and ``load_permission_rules()`` resolves
# it to a deep copy of the corresponding rule table.
SANDBOX_PERMISSIONS: dict[str, PermissionRule] = {
    "*": "ask",
    "external_directory": "ask",
    "read_file": {
        "*": "allow",
        "*.env": "deny",
        "*.env.*": "deny",
        "*.env.example": "allow",
    },
    "inspect_file": {
        "*": "allow",
        "*.env": "deny",
        "*.env.*": "deny",
        "*.env.example": "allow",
    },
    "search_text": "allow",
    "git_status": "allow",
    "git_diff": "allow",
    "glob": "allow",
    "load_skill": "allow",
    "rag_search": "allow",
    "rag_status": "allow",
    "project_status": "allow",
    "list_tasks": "allow",
    "get_task": "allow",
    "web_search": "allow",
    "todo_write": "allow",
    "compact": "allow",
    "list_features": "allow",
    "check_inbox": "allow",
    "list_crons": "allow",
    # MCP browse tools are read-only with respect to the workspace, matching
    # DEFAULT_PERMISSIONS; unknown MCP tools still ask.
    "mcp__fetch__fetch": "allow",
    "mcp__playwright__browser_navigate": "allow",
    "mcp__playwright__browser_navigate_back": "allow",
    "mcp__playwright__browser_snapshot": "allow",
    "mcp__playwright__browser_take_screenshot": "allow",
    "mcp__playwright__browser_console_messages": "allow",
    "mcp__playwright__browser_network_requests": "allow",
    "mcp__playwright__browser_wait_for": "allow",
    "mcp__playwright__browser_resize": "allow",
    "mcp__playwright__browser_tabs": "ask",
    "mcp__*": "ask",
    # Verification has its own structural policy and runs without a model
    # permission prompt. Keep it explicit so the sandbox cannot deadlock Goal.
    "verify_command": "allow",
    "write_file": {
        "*": "ask",
        ".features": "deny",
        ".features/*": "deny",
        "**/.features": "deny",
        "**/.features/*": "deny",
        ".project/goal.json": "deny",
        ".project/goal*": "deny",
        ".project/goal-history/*": "deny",
        "**/.project/goal.json": "deny",
        "**/.project/goal*": "deny",
        "**/.project/goal-history/*": "deny",
    },
    "edit_file": {
        "*": "ask",
        ".features": "deny",
        ".features/*": "deny",
        "**/.features": "deny",
        "**/.features/*": "deny",
        ".project/goal.json": "deny",
        ".project/goal*": "deny",
        ".project/goal-history/*": "deny",
        "**/.project/goal.json": "deny",
        "**/.project/goal*": "deny",
        "**/.project/goal-history/*": "deny",
    },
    "patch_file": {
        "*": "ask",
        ".features": "deny",
        ".features/*": "deny",
        "**/.features": "deny",
        "**/.features/*": "deny",
        ".project/goal.json": "deny",
        ".project/goal*": "deny",
        ".project/goal-history/*": "deny",
        "**/.project/goal.json": "deny",
        "**/.project/goal*": "deny",
        "**/.project/goal-history/*": "deny",
    },
    "bash": {
        "*": "ask",
        "dir *": "allow",
        "type *": "allow",
        "where *": "allow",
        "git status*": "allow",
        "git diff*": "allow",
        "git log*": "allow",
        "*.features*": "deny",
        "*.project/goal.json*": "deny",
        "*.project/goal-history*": "deny",
        "rm *": "deny",
        "sudo *": "deny",
        "shutdown*": "deny",
        "reboot*": "deny",
    },
}

# A lower-friction interactive policy for normal development work.  It keeps
# the same workspace and sensitive-path boundaries as ``sandbox`` but removes
# the prompt for routine edits and explicitly allow-listed test/build commands.
# Commands outside this small list (including compound shell commands) still
# require approval through the normal engine safety checks.
LOW_FRICTION_PERMISSIONS: dict[str, PermissionRule] = copy.deepcopy(SANDBOX_PERMISSIONS)
LOW_FRICTION_PERMISSIONS.update(
    {
        "write_file": {
            "*": "allow",
            ".features": "deny",
            ".features/*": "deny",
            "**/.features": "deny",
            "**/.features/*": "deny",
            ".project/goal.json": "deny",
            ".project/goal*": "deny",
            ".project/goal-history/*": "deny",
            "**/.project/goal.json": "deny",
            "**/.project/goal*": "deny",
            "**/.project/goal-history/*": "deny",
        },
        "edit_file": {
            "*": "allow",
            ".features": "deny",
            ".features/*": "deny",
            "**/.features": "deny",
            "**/.features/*": "deny",
            ".project/goal.json": "deny",
            ".project/goal*": "deny",
            ".project/goal-history/*": "deny",
            "**/.project/goal.json": "deny",
            "**/.project/goal*": "deny",
            "**/.project/goal-history/*": "deny",
        },
        "patch_file": {
            "*": "allow",
            ".features": "deny",
            ".features/*": "deny",
            "**/.features": "deny",
            "**/.features/*": "deny",
            ".project/goal.json": "deny",
            ".project/goal*": "deny",
            ".project/goal-history/*": "deny",
            "**/.project/goal.json": "deny",
            "**/.project/goal*": "deny",
            "**/.project/goal-history/*": "deny",
        },
        "bash": {
            "*": "ask",
            "dir *": "allow",
            "type *": "allow",
            "where *": "allow",
            "git status*": "allow",
            "git diff*": "allow",
            "git log*": "allow",
            "npm test*": "allow",
            "npm run test*": "allow",
            "npm run typecheck*": "allow",
            "npm run build*": "allow",
            "pytest*": "allow",
            "python -m pytest*": "allow",
            "python -m unittest*": "allow",
            "*.features*": "deny",
            "*.project/goal.json*": "deny",
            "*.project/goal-history*": "deny",
            "rm *": "deny",
            "sudo *": "deny",
            "shutdown*": "deny",
            "reboot*": "deny",
        },
    }
)

PERMISSION_PRESETS: dict[str, dict[str, PermissionRule]] = {
    "sandbox": SANDBOX_PERMISSIONS,
    "low-friction": LOW_FRICTION_PERMISSIONS,
    "low_friction": LOW_FRICTION_PERMISSIONS,
}


def _valid_effect(value: object) -> bool:
    return isinstance(value, str) and value in _VALID_EFFECTS


def _normalize_rules(raw: object) -> dict[str, PermissionRule]:
    if isinstance(raw, str) and raw in _VALID_EFFECTS:
        return {"*": raw}
    if not isinstance(raw, dict):
        return dict(DEFAULT_PERMISSIONS)
    section = raw.get("permission", raw)
    if isinstance(section, str) and section in _VALID_EFFECTS:
        return {"*": section}
    if isinstance(section, str):
        preset = PERMISSION_PRESETS.get(section.strip().lower())
        if preset is not None:
            return copy.deepcopy(preset)
    if not isinstance(section, dict):
        return dict(DEFAULT_PERMISSIONS)
    # Shipped permissions.json uses the declarative v1 envelope with tool
    # severities under ``tools`` plus mode auto-approval metadata.  Resolve
    # that envelope into the engine's flat effect table; low/medium/high are
    # risk labels, so deterministic read-only tools become allow while
    # mutating/high-risk tools remain ask unless an explicit rule says deny.
    if isinstance(section.get("tools"), dict):
        flat: dict[str, PermissionRule] = {}
        for tool_pattern, risk in section["tools"].items():
            if not isinstance(tool_pattern, str):
                continue
            # Detailed top-level rule blocks (notably bash deny patterns) are
            # more authoritative than the coarse risk label in ``tools``.
            if tool_pattern in section and isinstance(section.get(tool_pattern), dict):
                continue
            level = str(risk or "").lower()
            effect: PermissionRule = "allow" if level == "low" else "ask"
            # Keep path-level safety rules from the built-in policy when the
            # coarse v1 envelope marks a file-inspection tool as low risk.
            # Otherwise ``inspect_file: low`` would replace the nested
            # ``*.env: deny`` guard with an unrestricted allow rule.
            existing = DEFAULT_PERMISSIONS.get(tool_pattern)
            if isinstance(existing, dict) and effect == "allow":
                continue
            flat[tool_pattern] = effect
        # Preserve structural safety defaults from the built-in policy for
        # commands not represented by the envelope.
        merged = dict(DEFAULT_PERMISSIONS)
        merged.update(flat)
        return merged

    rules: dict[str, PermissionRule] = {}
    for tool_pattern, rule in section.items():
        if not isinstance(tool_pattern, str):
            continue
        if _valid_effect(rule):
            rules[tool_pattern] = rule  # type: ignore[assignment]
            continue
        if isinstance(rule, dict):
            nested: dict[str, PermissionEffect] = {}
            for resource_pattern, effect in rule.items():
                if isinstance(resource_pattern, str) and _valid_effect(effect):
                    nested[resource_pattern] = effect  # type: ignore[assignment]
            if nested:
                rules[tool_pattern] = nested
    return rules or dict(DEFAULT_PERMISSIONS)


def load_permission_rules() -> dict[str, PermissionRule]:
    # An explicit environment override is useful for local sessions and does
    # not modify the checked-in policy file.  Unknown values are ignored so a
    # typo cannot accidentally broaden permissions.
    env_preset = os.getenv("HARNESS_PERMISSION_PRESET", "").strip().lower()
    if env_preset:
        preset = PERMISSION_PRESETS.get(env_preset)
        if preset is not None:
            return copy.deepcopy(preset)
    path = get_permissions_config_path()
    if not path.exists():
        return dict(DEFAULT_PERMISSIONS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_PERMISSIONS)
    return _normalize_rules(data)
