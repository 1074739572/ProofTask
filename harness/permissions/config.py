"""Load declarative tool permission rules."""

from __future__ import annotations

import json
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
    if not isinstance(section, dict):
        return dict(DEFAULT_PERMISSIONS)

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
    path = get_permissions_config_path()
    if not path.exists():
        return dict(DEFAULT_PERMISSIONS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_PERMISSIONS)
    return _normalize_rules(data)
