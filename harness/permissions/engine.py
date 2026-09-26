"""OpenCode-style permission engine for tool calls."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

from harness.permissions.config import PermissionEffect, PermissionRule, load_permission_rules
from harness.permissions.execution import classify_bash_execution, has_wildcard
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
    # ``normal``: ordinary decision; modes and scoped saved rules apply.
    # ``scoped``: opaque execution; only an exact command-level authorization
    # satisfies it, and session modes never auto-approve it.
    # ``action_time``: irreversible action; every occurrence prompts, saved
    # approvals are never consulted.
    approval: str = "normal"


@dataclass(frozen=True)
class ToolPermissionContext:
    tool: str
    resource: str
    external_resource: str | None = None


_SENSITIVE_NAMES = frozenset({".env", ".env.local", ".env.production", ".env.development"})


def _is_sensitive_path(resource: str) -> bool:
    """Return whether a workspace-relative resource is secret-like.

    This check is intentionally independent of the user-editable permission
    file so adding a broad ``search_text: allow`` rule cannot reopen secrets.
    Example templates remain readable.
    """
    path = resource.replace("\\", "/").strip().lower()
    while path.startswith("./"):
        path = path[2:]
    if not path:
        return False
    name = path.rsplit("/", 1)[-1]
    if name in _SENSITIVE_NAMES or (name.startswith(".env.") and not name.endswith(".example")):
        return True
    return name.endswith((".pem", ".key", ".p12", ".pfx"))


# Separators that make a whole-command prefix allow-list unsound: whatever
# follows them is a separate command that must clear the policy on its own.
_SHELL_SEPARATORS = ("&", "|", ">", "<", ";", "\n", "\r")
_SHELL_SEPARATOR_RE = re.compile(r"[&|><;]+|\r?\n")


def _is_compound_shell_command(command: str) -> bool:
    """Return whether a shell command chains or redirects several commands."""
    return any(token in command for token in _SHELL_SEPARATORS)


def _split_shell_segments(command: str) -> list[str]:
    """Split a compound shell command into individually checkable segments.

    A full shell parse is intentionally out of scope.  This is a conservative
    split on the separators that make a prefix allow-list unsound (``&``,
    ``|``, ``>``, ``<``, ``;`` and newlines), followed by stripping whitespace.
    Bare file-descriptor numbers produced by redirections (the ``2`` and ``1``
    of ``2>&1``) are dropped because they are not commands; '' segments are
    dropped as well.  Erring toward more segments only ever makes the decision
    stricter, never more permissive.
    """
    segments: list[str] = []
    for raw in _SHELL_SEPARATOR_RE.split(command):
        segment = raw.strip()
        if segment and not segment.isdigit():
            segments.append(segment)
    return segments


def _match(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())


def _json_preview(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True)[:500]
    except TypeError:
        return str(data)[:500]


def _path_value(tool_name: str, data: dict) -> str:
    if tool_name in (
        "read_file", "write_file", "edit_file", "patch_file", "inspect_file",
        "git_diff", "search_text", "rag_index",
    ):
        return str(data.get("path") or "")
    return ""


def _normalize_resource(tool_name: str, resource: str) -> str:
    """Normalize filesystem separators before matching policy rules.

    Rules are stored with forward slashes, while Windows callers commonly use
    backslashes. Matching the raw input let the same protected file have two
    different permission outcomes.
    """
    if tool_name in (
        "read_file", "write_file", "edit_file", "patch_file", "inspect_file",
        "git_diff", "search_text", "rag_index",
    ):
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
    if tool_name in (
        "read_file", "write_file", "edit_file", "patch_file", "inspect_file",
        "git_diff", "search_text", "rag_index",
    ):
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

    exec_class = classify_bash_execution(resource) if tool_name == "bash" else "normal"

    if exec_class == "action_time":
        # Irreversible local deletion: every occurrence prompts. Saved
        # approvals, broad config allows, and session modes (including
        # full-access) never substitute for action-time confirmation.
        return PermissionDecision(
            effect="ask",
            tool=tool_name,
            resource=resource,
            reason="irreversible local deletion requires action-time confirmation",
            save_tool=tool_name,
            save_resource=resource,
            source="safety",
            external_resource=external_resource,
            approval="action_time",
        )

    if exec_class == "opaque":
        # Opaque script/interpreter execution: a wildcard approval (`bash *`,
        # `python *`) says nothing about what the script does, so only an
        # exact command-level authorization can satisfy it.
        if include_saved:
            precise_rules = [
                rule
                for rule in [*session_rules(), *load_persistent_rules()]
                if not has_wildcard(rule.tool) and not has_wildcard(rule.resource)
            ]
            saved_effect, saved_matched, saved_source = _evaluate_saved_rules(
                tool_name, resource, precise_rules
            )
            if saved_effect == "allow":
                return PermissionDecision(
                    effect="allow",
                    tool=tool_name,
                    resource=resource,
                    reason=f"matched exact authorization {saved_matched}",
                    save_tool=tool_name,
                    save_resource=resource,
                    source=saved_source or "saved",
                    external_resource=external_resource,
                )
        if (
            config_effect == "allow"
            and ":" in config_matched
            and not has_wildcard(config_matched)
        ):
            return PermissionDecision(
                effect="allow",
                tool=tool_name,
                resource=resource,
                reason=f"matched exact config rule ({config_matched})",
                save_tool=tool_name,
                save_resource=resource,
                source="config",
                external_resource=external_resource,
            )
        return PermissionDecision(
            effect="ask",
            tool=tool_name,
            resource=resource,
            reason="opaque execution requires exact command authorization",
            save_tool=tool_name,
            save_resource=resource,
            source="safety",
            external_resource=external_resource,
            approval="scoped",
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
    if tool_name in {
        "read_file", "inspect_file", "git_diff", "search_text", "rag_index",
    } and _is_sensitive_path(ctx.resource):
        return PermissionDecision(
            effect="deny",
            tool=tool_name,
            resource=ctx.resource,
            reason="sensitive resource is protected",
            save_tool=tool_name,
            save_resource=ctx.resource,
            source="safety",
            external_resource=ctx.external_resource,
        )
    # MCP annotations are safety metadata, not merely descriptive fields.
    # A destructive tool must remain interactive even when a broad config
    # wildcard (for example ``mcp__fetch__*``) would otherwise allow it.
    if tool_name.startswith("mcp__") and isinstance(mcp_meta, dict):
        if bool(mcp_meta.get("destructive")) or mcp_meta.get("readOnly") is False:
            return PermissionDecision(
                effect="ask",
                tool=tool_name,
                resource=ctx.resource,
                reason="destructive MCP tool requires explicit approval",
                save_tool=tool_name,
                save_resource=ctx.resource,
                source="safety",
                external_resource=ctx.external_resource,
            )
    if tool_name == "bash" and _is_compound_shell_command(ctx.resource):
        # A prefix allow-list cannot safely authorize a compound shell command
        # as a whole: inheriting the ``dir *`` allow rule would also authorize
        # ``dir . & del important.txt``.  Instead of asking unconditionally,
        # split the command on the separators and evaluate every segment
        # independently, then keep the most restrictive outcome.
        #
        # The command is auto-allowed only when *every* segment is allowed on
        # its own; a single deny or ask segment keeps the decision interactive.
        # A blocking verdict is still tagged ``source="safety"`` so a session
        # convenience mode (auto-review / full-access) cannot silently approve
        # a compound command whose parts the policy did not all clear.
        segments = _split_shell_segments(ctx.resource)
        if segments:
            segment_decisions = [
                evaluate_single_permission(
                    tool_name,
                    segment,
                    rules=rules,
                    include_saved=include_saved,
                    source_tool=tool_name,
                )
                for segment in segments
            ]
            worst_effect: PermissionEffect = "allow"
            worst_reason = ""
            worst_approval = "normal"
            approval_rank = {"normal": 0, "scoped": 1, "action_time": 2}
            for segment_decision in segment_decisions:
                if approval_rank.get(segment_decision.approval, 0) > approval_rank.get(worst_approval, 0):
                    worst_approval = segment_decision.approval
                if segment_decision.effect == "deny":
                    worst_effect, worst_reason = "deny", segment_decision.reason
                    break
                if segment_decision.effect == "ask" and worst_effect == "allow":
                    worst_effect, worst_reason = "ask", segment_decision.reason
            if worst_effect == "allow":
                return PermissionDecision(
                    effect="allow",
                    tool=tool_name,
                    resource=ctx.resource,
                    reason="compound shell command: every segment is allowed",
                    save_tool=tool_name,
                    save_resource=ctx.resource,
                    source="config",
                    external_resource=ctx.external_resource,
                )
            return PermissionDecision(
                effect=worst_effect,
                tool=tool_name,
                resource=ctx.resource,
                reason=f"compound shell command: {worst_reason}",
                save_tool=tool_name,
                save_resource=ctx.resource,
                source="safety",
                external_resource=ctx.external_resource,
                approval=worst_approval,
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
