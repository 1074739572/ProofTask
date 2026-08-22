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
    if tool_name in ("read_file", "write_file", "edit_file", "patch_file", "inspect_file", "git_diff"):
        return str(data.get("path") or "")
    return ""


def _normalize_resource(tool_name: str, resource: str) -> str:
    """Normalize filesystem separators before matching policy rules.

    Rules are stored with forward slashes, while Windows callers commonly use
    backslashes. Matching the raw input let the same protected file have two
    different permission outcomes.
    """
    if tool_name in ("read_file", "write_file", "edit_file", "patch_file", "inspect_file", "git_diff"):
        normalized = resource.replace("\\", "/")
        # Policies are workspace-relative. Without canonicalizing an absolute
        # path inside the workspace, `.project/goal.json` can evade a deny rule
        # simply by spelling the same file as `C:/.../.project/goal.json`.
        try:
            base = get_workdir().resolve()
            candidate = Path(normalized)
            resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
            if resolved.is_relative_to(base):
                return resolved.relative_to(base).as_posix()
        except (OSError, ValueError):
            pass
        return normalized
    if tool_name in ("glob", "bash"):
        return resource.replace("\\", "/")
    return resource


def _pattern_specificity(pattern: str) -> int:
    """Prefer an explicit rule over a broad wildcard rule."""
    return len(pattern.replace("*", "").replace("?", ""))


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
        return ToolPermissionContext(
            tool_name, _normalize_resource(tool_name, str(data.get("command") or ""))
        )
    if tool_name in ("read_file", "write_file", "edit_file", "patch_file", "inspect_file", "git_diff"):
        return ToolPermissionContext(
            tool_name,
            _normalize_resource(tool_name, str(data.get("path") or "")),
            external,
        )
    if tool_name == "glob":
        return ToolPermissionContext(
            tool_name, _normalize_resource(tool_name, str(data.get("pattern") or ""))
        )
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
    best_tool_score = -1
    best_resource_score = -1
    for tool_pattern, rule in rules.items():
        if not _match(tool_pattern, tool_name):
            continue
        if isinstance(rule, str):
            score = _pattern_specificity(tool_pattern)
            if score > best_tool_score or (score == best_tool_score and rule == "deny"):
                effect = rule
                matched = tool_pattern
                best_tool_score = score
                best_resource_score = 0
            continue
        nested_effect: PermissionEffect | None = None
        nested_pattern = ""
        nested_score = -1
        for resource_pattern, candidate in rule.items():
            if _match(resource_pattern, resource):
                score = _pattern_specificity(resource_pattern)
                if score > nested_score or (score == nested_score and candidate == "deny"):
                    nested_effect = candidate
                    nested_pattern = resource_pattern
                    nested_score = score
        if nested_effect is not None:
            tool_score = _pattern_specificity(tool_pattern)
            if (
                tool_score > best_tool_score
                or (tool_score == best_tool_score and nested_score > best_resource_score)
                or (
                    tool_score == best_tool_score
                    and nested_score == best_resource_score
                    and nested_effect == "deny"
                )
            ):
                effect = nested_effect
                matched = f"{tool_pattern}:{nested_pattern}"
                best_tool_score = tool_score
                best_resource_score = nested_score
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
    if tool_name == "bash" and any(token in ctx.resource for token in ("&", "|", ">", "<", "\n", "\r")):
        # A prefix allow-list cannot safely authorize a compound shell command.
        # It may contain an unrelated destructive command after the separator.
        return PermissionDecision(
            effect="ask",
            tool=tool_name,
            resource=ctx.resource,
            reason="compound shell command requires explicit approval",
            save_tool=tool_name,
            save_resource=ctx.resource,
            source="safety",
        )
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
