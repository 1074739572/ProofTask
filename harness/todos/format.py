"""Format session todos for prompts, tool results, and reminders."""

from __future__ import annotations

from harness.todos import state


def _status_icon(status: str) -> str:
    if status == "completed":
        return "[x]"
    if status == "in_progress":
        return "[>]"
    return "[ ]"


def format_todos_for_cli(todos: list[dict[str, str]] | None = None) -> str:
    """Human-readable todo list for /resume and similar CLI output."""
    todos = todos if todos is not None else state.get_todos()
    if not todos:
        return ""
    status_zh = {"completed": "已完成", "in_progress": "进行中", "pending": "待办"}
    lines = ["当前会话任务（todo_write 维护，每次提交完整列表）："]
    for index, todo in enumerate(todos, 1):
        status = todo["status"]
        label = todo["activeForm"] if status == "in_progress" else todo["content"]
        zh = status_zh.get(status, status)
        lines.append(f"{index}. {_status_icon(status)} {label}（{zh}）")
    lines.append("规则：同时只能有一项「进行中」；完成后立即标为已完成。")
    return "\n".join(lines)


def format_todos_for_prompt(todos: list[dict[str, str]] | None = None) -> str:
    todos = todos if todos is not None else state.get_todos()
    if not todos:
        return ""
    lines = [
        "Current session todos (single source of truth — update via todo_write with the FULL list):"
    ]
    for index, todo in enumerate(todos, 1):
        status = todo["status"]
        label = todo["activeForm"] if status == "in_progress" else todo["content"]
        lines.append(f"{index}. {_status_icon(status)} {label} ({status})")
    lines.append(
        "Discipline: exactly one in_progress; mark completed immediately when done."
    )
    return "\n".join(lines)


def format_todos_tool_result(todos: list[dict[str, str]]) -> str:
    lines = [f"Updated {len(todos)} todo(s). Current list:"]
    for index, todo in enumerate(todos, 1):
        status = todo["status"]
        if status == "completed":
            lines.append(f"{index}. [x] {todo['content']}")
        elif status == "in_progress":
            lines.append(f"{index}. [>] {todo['activeForm']} (in progress)")
        else:
            lines.append(f"{index}. [ ] {todo['content']} (pending)")
    lines.append("")
    lines.append(
        "Remember: pass the FULL list on every todo_write call; "
        "exactly one in_progress; mark completed immediately when done."
    )
    return "\n".join(lines)


def format_todo_reminder() -> str:
    todos = state.get_todos()
    body = format_todos_for_prompt(todos) if todos else "(no todos yet — create the list with todo_write)"
    return (
        "<reminder>\n"
        "Update your session todos with todo_write. Pass the complete list every time.\n"
        f"Rounds since last todo_write: {state.rounds_since_todo_update}\n\n"
        f"{body}\n"
        "</reminder>"
    )


def format_todos_welcome_line(todos: list[dict[str, str]] | None = None) -> str | None:
    todos = todos if todos is not None else state.get_todos()
    if not todos:
        return None
    done = sum(1 for item in todos if item["status"] == "completed")
    active = next((item for item in todos if item["status"] == "in_progress"), None)
    parts = [f"{done}/{len(todos)} 已完成"]
    if active:
        parts.append(f"当前：{active['activeForm']}")
    return "  ·  ".join(parts)
