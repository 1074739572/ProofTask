"""In-memory session todos persisted under the active session directory.

Path::

    .project/sessions/<id>/todos.json

Legacy ``.project/todos.json`` is migrated by ``session_registry`` into a
session folder; this module never writes the flat path again.

**Per-process binding** — ``todos_path()`` requires an explicit ``session_id``
or ``SessionBinding`` so todo writes never leak into another window's session.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from harness.project.session_registry import SessionBinding, read_active_session_id, session_binding

_CURRENT: list[dict[str, str]] = []
rounds_since_todo_update: int = 0

# Cached binding — set by load_todos_from_disk / explicit init.
_current_binding: SessionBinding | None = None


def todos_path(
    *,
    session_id: str | None = None,
    binding: SessionBinding | None = None,
) -> Path:
    """Return todos.json for a specific session (or the process-bound session)."""
    if binding is not None:
        return binding.todos_json
    if session_id is not None:
        return session_binding(session_id).todos_json
    if _current_binding is not None:
        return _current_binding.todos_json
    sid = read_active_session_id()
    if not sid:
        from harness.project.session_registry import ensure_active_session
        ensure_active_session(fresh=False)
        sid = read_active_session_id()
    return session_binding(sid or "?").todos_json


# Back-compat: ``from harness.todos.state import TODOS_PATH`` resolves via __getattr__.
def __getattr__(name: str):
    if name == "TODOS_PATH":
        return todos_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_todos() -> list[dict[str, str]]:
    return list(_CURRENT)


def set_binding(binding: SessionBinding) -> None:
    """Pin the process-wide todo binding (called once at bootstrap)."""
    global _current_binding
    _current_binding = binding


def _derive_active_form(content: str, status: str) -> str:
    text = content.strip().rstrip(".")
    if status == "in_progress":
        if text.endswith("…") or text.endswith("..."):
            return text
        words = text.split()
        if words:
            first = words[0]
            if re.match(
                r"^(run|fix|add|update|write|read|test|check|implement|create|refactor)\b",
                first,
                re.I,
            ):
                return f"{first[0].upper()}{first[1:]}…" if len(first) > 1 else f"{first}…"
        return f"{text}…"
    return text


def normalize_todos(raw: Any) -> tuple[list[dict[str, str]] | None, str | None]:
    todos = raw
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"

    normalized: list[dict[str, str]] = []
    for index, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{index}] must be an object"
        content = todo.get("content")
        status = todo.get("status")
        if not content or not status:
            return None, f"Error: todos[{index}] missing 'content' or 'status'"
        if status not in ("pending", "in_progress", "completed"):
            return (
                None,
                f"Error: todos[{index}] has invalid status '{status}' "
                "(use pending, in_progress, or completed)",
            )
        active_form = (todo.get("activeForm") or todo.get("active_form") or "").strip()
        if not active_form:
            active_form = _derive_active_form(str(content), status)
        normalized.append(
            {
                "content": str(content).strip(),
                "activeForm": active_form,
                "status": status,
            }
        )

    in_progress = [item for item in normalized if item["status"] == "in_progress"]
    if len(in_progress) > 1:
        titles = ", ".join(f'"{item["content"][:40]}"' for item in in_progress)
        return (
            None,
            f"Error: only one todo may be in_progress at a time (found {len(in_progress)}: {titles})",
        )
    return normalized, None


def set_todos(todos: list[dict[str, str]]) -> None:
    global _CURRENT, rounds_since_todo_update
    _CURRENT = todos
    rounds_since_todo_update = 0
    _persist()


def write_todos(raw: Any) -> tuple[list[dict[str, str]] | None, str | None]:
    todos, error = normalize_todos(raw)
    if error:
        return None, error
    set_todos(todos)
    return todos, None


def clear_todos(*, delete_file: bool = True) -> None:
    """Clear in-memory todos; optionally delete the active session's todos.json.

    When ending a session (``/clear``), pass ``delete_file=False`` so the old
    ``sessions/<id>/todos.json`` remains with that archived conversation.
    """
    global _CURRENT, rounds_since_todo_update
    _CURRENT = []
    rounds_since_todo_update = 0
    if not delete_file:
        return
    try:
        path = todos_path()
    except Exception:
        return
    if path.exists():
        path.unlink()


def load_todos_from_disk(
    *,
    session_id: str | None = None,
    binding: SessionBinding | None = None,
) -> list[dict[str, str]]:
    global _CURRENT, _current_binding
    if binding is not None:
        _current_binding = binding
    try:
        path = todos_path(session_id=session_id, binding=binding)
    except Exception:
        _CURRENT = []
        return []
    if not path.exists():
        _CURRENT = []
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        _CURRENT = []
        return []
    todos, error = normalize_todos(raw)
    if error or todos is None:
        _CURRENT = []
        return []
    _CURRENT = todos
    return list(_CURRENT)


def note_llm_round_without_todo_update() -> None:
    global rounds_since_todo_update
    rounds_since_todo_update += 1


def _persist() -> None:
    path = todos_path()
    if not _CURRENT:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_CURRENT, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
