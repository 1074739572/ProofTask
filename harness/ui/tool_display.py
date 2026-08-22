"""Human-facing one-line summaries for tool *actions* (full payloads still go to the model)."""

from __future__ import annotations

import json
import re
from typing import Any

_FAILURE_PREFIXES = (
    "error:",
    "permission denied",
    "[repeatguard]",
    "[lookupguard]",
    "[groundingguard]",
    "[writingguard]",
)


def hooks_verbose() -> bool:
    import os

    return os.getenv("HARNESS_VERBOSE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def show_tool_lines() -> bool:
    """Whether per-tool intent/start lines are printed to the terminal.

    Default OFF: every tool request otherwise prints two lines
    (``› <rationale>`` and ``● <tool> <summary>``), which many users find
    noisy. Set ``HARNESS_SHOW_TOOL_LINES=1`` to bring them back. Events are
    still emitted either way (TUI / JSONL unaffected).
    """
    import os

    return os.getenv("HARNESS_SHOW_TOOL_LINES", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_failure_tool_output(output: Any) -> bool:
    text = str(output if output is not None else "").strip()
    if not text:
        return False
    low = text.lower()
    return any(low.startswith(prefix) for prefix in _FAILURE_PREFIXES)


def _short(text: str, limit: int = 72) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def summarize_tool_input(name: str, tool_input: dict | None) -> str:
    """One-line human summary of what the tool is about to do."""
    args = tool_input or {}
    if name == "bash":
        return _short(str(args.get("command", "")), 90)
    if name in {"read_file", "write_file", "edit_file", "patch_file", "inspect_file"}:
        path = str(args.get("path", ""))
        extra = ""
        if name == "read_file" and args.get("offset") is not None:
            extra = f" @{args.get('offset')}"
            if args.get("limit") is not None:
                extra += f"+{args.get('limit')}"
        if name == "patch_file":
            extra = f" ({len(args.get('hunks') or [])} hunks)"
        return _short(path + extra, 90)
    if name in {"git_status", "git_diff"}:
        path = str(args.get("path", ""))
        return _short(name + (f" {path}" if path else ""), 90)
    if name == "glob":
        return _short(str(args.get("pattern", "")), 90)
    if name == "todo_write":
        todos = args.get("todos") or []
        return f"{len(todos)} item(s)" if isinstance(todos, list) else "update"
    if name.startswith("mcp__"):
        keys = [k for k in ("url", "path", "selector", "query", "command") if k in args]
        if keys:
            return _short(f"{keys[0]}={args.get(keys[0])}", 90)
        return "…"
    if name == "github_request":
        return _short(f"{args.get('method', 'GET')} {args.get('path', '')}", 90)
    for key in ("path", "query", "command", "url", "name", "prompt"):
        if key in args and args[key] not in (None, ""):
            return _short(f"{key}={args[key]}", 90)
    if not args:
        return ""
    try:
        return _short(json.dumps(args, ensure_ascii=False), 90)
    except TypeError:
        return _short(str(args), 90)


def summarize_failure_output(output: Any) -> str:
    """Short → line for errors / guard blocks only."""
    text = str(output if output is not None else "").strip()
    if text.startswith("[RepeatGuard]"):
        return "blocked duplicate call"
    return _short(text, 100)
