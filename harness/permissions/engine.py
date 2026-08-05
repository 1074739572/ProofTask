"""OpenCode-style permission engine for tool calls."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from harness.permissions.config import PermissionEffect, PermissionRule, load_permission_rules
from harness.permissions.state import SavedPermissionRule, load_persistent_rules, session_rules
from harness.settings import WORKDIR, get_workdir


@dataclass(frozen=True)
class PermissionDecision:
    effect: PermissionEffect
    tool: str
    resource: str
    reason: str = ""
    save_tool: str = ""
    save_resource: str = ""
    source: str = "config"
    external_resource: str | None = None


@dataclass(frozen=True)
class ToolPermissionContext:
    tool: str
    resource: str
    external_resource: str | None = None


def _match(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())


def _json_preview(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True)[:500]
    except TypeError:
        return str(data)[:500]


def _path_value(tool_name: str, data: dict) -> str:
    if tool_name in ("read_file", "write_file", "edit_file"):
        return str(data.get("path") or "")
    return ""


def _external_resource_for_path(path_text: str) -> str | None:
    if not path_text:
        return None
    try:
        path = Path(path_text)
        if not path.is_absolute():
            return None
        resolved = path.resolve()
        base = get_workdir().resolve()
        if resolved.is_relative_to(base):
            return None
        if resolved.is_file():
            resolved = resolved.parent
        return str(resolved).replace("\\", "/")
    except Exception:
        return None


def context_for_tool(tool_name: str, tool_input: dict | None) -> ToolPermissionContext:
    data = tool_input or {}
    external = _external_resource_for_path(_path_value(tool_name, data))
    if tool_name == "bash":
        return ToolPermissionContext(tool_name, str(data.get("command") or ""))
    if tool_name in ("read_file", "write_file", "edit_file"):
        return ToolPermissionContext(tool_name, str(data.get("path") or ""), external)
    if tool_name == "glob":
        return ToolPermissionContext(tool_name, str(data.get("pattern") or ""))
    if tool_name == "web_search":
        return ToolPermissionContext(tool_name, str(data.get("query") or ""))
    if tool_name == "rag_search":
        return ToolPermissionContext(tool_name, str(data.get("query") or ""))
    if tool_name.startswith("mcp__fetch__"):
        return ToolPermissionContext(tool_name, str(data.get("url") or ""))
    if tool_name.startswith("mcp__playwright__"):
        for key in ("url", "target", "element", "text", "regex", "filename"):
            value = data.get(key)
            if value:
                return ToolPermissionContext(tool_name, str(value))
    if data:
        return ToolPermissionContext(tool_name, _json_preview(data))
    return ToolPermissionContext(tool_name, "*")


def _fallback_from_mcp_meta(tool_name: str, meta: dict | None) -> PermissionEffect | None:
    if not tool_name.startswith("mcp__"):
        return None
    meta = meta or {}
    if meta.get("readOnly"):
        return "allow"
    # Unknown MCP tools should ask; destructiveHint also asks.
    return "ask"


def _evaluate_config_rules(
    tool_name: str,
    resource: str,
    rules: dict[str, PermissionRule],
) -> tuple[PermissionEffect | None, str]:
    effect: PermissionEffect | None = None
    matched = ""
    for tool_pattern, rule in rules.items():
        if not _match(tool_pattern, tool_name):
            continue
        matched = tool_pattern
        if isinstance(rule, str):
            effect = rule
            continue
        nested_effect: PermissionEffect | None = None
        nested_pattern = ""
        for resource_pattern, candidate in rule.items():
            if _match(resource_pattern, resource):
                nested_effect = candidate
                nested_pattern = resource_pattern
        if nested_effect is not None:
            effect = nested_effect
            matched = f"{tool_pattern}:{nested_pattern}"
    return effect, matched


def _evaluate_saved_rules(
    tool_name: str,
    resource: str,
    rules: list[SavedPermissionRule],
) -> tuple[PermissionEffect | None, str, str]:
    effect: PermissionEffect | None = None
    matched = ""
    source = ""
    for rule in rules:
        if _match(rule.tool, tool_name) and _match(rule.resource, resource):
            effect = rule.effect
            matched = f"{rule.tool}:{rule.resource}"
            source = rule.scope
    return effect, matched, source


def evaluate_single_permission(
    tool_name: str,
    resource: str,
    *,
    mcp_meta: dict | None = None,
    rules: dict[str, PermissionRule] | None = None,
    include_saved: bool = True,
    fallback_to_mcp: bool = True,
    source_tool: str | None = None,
    external_resource: str | None = None,
) -> PermissionDecision:
    loaded = rules if rules is not None else load_permission_rules()
    config_effect, config_matched = _evaluate_config_rules(tool_name, resource, loaded)

    # Safety red line: a config `deny` (e.g. `rm *`, `sudo *`) must NEVER be
    # overridden by a saved "always allow" rule. A user's single "always"
    # click on `bash *` must not silently disable the deny-list.
    if config_effect == "deny":
        return PermissionDecision(
            effect="deny",
            tool=tool_name,
            resource=resource,
            reason=f"config deny ({config_matched}) overrides saved rules",
            save_tool=tool_name,
            save_resource=resource,
            source="config",
            external_resource=external_resource,
        )

    if include_saved:
        effect, matched, source = _evaluate_saved_rules(
            tool_name,
            resource,
            [*session_rules(), *load_persistent_rules()],
        )
        if effect is not None:
            return PermissionDecision(
                effect=effect,
                tool=tool_name,
                resource=resource,
                reason=f"matched {matched}",
                save_tool=tool_name,
                save_resource=resource,
                source=source or "saved",
                external_resource=external_resource,
            )

    effect = config_effect
    matched = config_matched
    source = "config"
    if effect is None and fallback_to_mcp:
        effect = _fallback_from_mcp_meta(source_tool or tool_name, mcp_meta)
        if effect is not None:
            matched = "mcp annotations"
            source = "mcp"
    if effect is None:
        effect = "ask"
        matched = "default"
        source = "default"

    return PermissionDecision(
        effect=effect,
        tool=tool_name,
        resource=resource,
        reason=f"matched {matched}" if matched else "default",
        save_tool=tool_name,
        save_resource=resource,
        source=source,
        external_resource=external_resource,
    )


def evaluate_permission(
    tool_name: str,
    tool_input: dict | None = None,
    *,
    mcp_meta: dict | None = None,
    rules: dict[str, PermissionRule] | None = None,
    include_saved: bool = True,
) -> PermissionDecision:
    """Return the effective allow/ask/deny decision for a tool call.

    Tool rules use last-match-wins over tool name patterns. Nested resource
    rules also use last-match-wins against an extracted resource string.
    External absolute file paths are evaluated through an additional
    ``external_directory`` gate before the tool's own permission.
    """
    ctx = context_for_tool(tool_name, tool_input)
    if ctx.external_resource:
        external_decision = evaluate_single_permission(
            "external_directory",
            ctx.external_resource,
            rules=rules,
            include_saved=include_saved,
            fallback_to_mcp=False,
            source_tool=tool_name,
            external_resource=ctx.external_resource,
        )
        if external_decision.effect != "allow":
            return PermissionDecision(
                effect=external_decision.effect,
                tool=tool_name,
                resource=ctx.resource,
                reason=f"external_directory {external_decision.reason}",
                save_tool="external_directory",
                save_resource=ctx.external_resource,
                source=external_decision.source,
                external_resource=ctx.external_resource,
            )

    return evaluate_single_permission(
        tool_name,
        ctx.resource,
        mcp_meta=mcp_meta,
        rules=rules,
        include_saved=include_saved,
        source_tool=tool_name,
        external_resource=ctx.external_resource,
    )
