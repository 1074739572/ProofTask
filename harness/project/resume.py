"""Resume: Claude Code-style session continue + opt-in project context.

**Per-process binding** — ``checkpoint_history`` and ``switch_to_session`` use
the window-pinned ``SessionBinding``.  ``/resume`` atomically checkpoints the
old binding, then swaps the process-wide binding to the new session.
"""

from __future__ import annotations

import os

from harness.project.session_registry import (
    SessionBinding,
    read_session_meta,
    session_binding,
    write_active_session_id,
    write_session_meta,
)
from harness.project.session_store import (
    append_checkpoint,
    format_session_line,
    load_session_messages,
    session_stats,
)
from harness.project.state import (
    load_state,
    sync_chapters_from_disk,
)
from harness.settings import WORKDIR

RESUME_CONTEXT_PREFIX = "[Resume context]"
LEGACY_SESSION_PREFIX = "[Session resumed]"


def auto_resume_mode() -> str:
    """
    off (default): reload session.jsonl only — no project/thesis injection.
    project:       also inject thesis chapter context on startup (legacy behavior).
    """
    return os.getenv("HARNESS_AUTO_RESUME", "off").strip().lower()


def should_auto_inject_project_on_startup() -> bool:
    return auto_resume_mode() in ("project", "thesis", "all", "1", "true", "yes")


def show_project_in_welcome() -> bool:
    if not load_state():
        return False
    flag = os.getenv("HARNESS_WELCOME_PROJECT", "0").strip().lower()
    return flag in ("1", "true", "yes", "on") or should_auto_inject_project_on_startup()


def is_resume_injection(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    text = content.strip()
    return text.startswith(RESUME_CONTEXT_PREFIX) or text.startswith(LEGACY_SESSION_PREFIX)


def project_context_message() -> str | None:
    """Opt-in thesis/project snapshot for the agent (not a user question)."""
    state = load_state()
    if state is None:
        return None
    state = sync_chapters_from_disk(state)
    done = sum(1 for c in state.chapters if c.status == "done")
    total = len(state.chapters)
    current = next((c for c in state.chapters if c.id == state.current_chapter), None)
    current_title = current.title if current else "(unset)"
    return (
        f"{RESUME_CONTEXT_PREFIX}\n"
        f"用户已选择继续以下论文/报告项目。"
        f"除非用户另有说明，否则不要当作无关新任务。\n\n"
        f"项目：{state.title}（{state.project_id}）\n"
        f"进度：{done}/{total} 章已完成\n"
        f"当前章节：{state.current_chapter} {current_title}\n"
        f"输出：{WORKDIR}/{state.output_dir}/\n"
        f"源文档：{state.source_doc}\n\n"
        f"写章节时可 load_skill(thesis-writing)。"
        f"完整章节表请调用 project_status。"
    )


def inject_project_context(messages: list, *, binding: SessionBinding, checkpoint: bool = True) -> tuple[bool, str]:
    """Append opt-in project context to the active session."""
    msg = project_context_message()
    if not msg:
        return False, "未找到 .project/state.json，无法恢复论文项目。"

    if messages and is_resume_injection(messages[-1]):
        return False, "论文恢复上下文已是最后一条消息，无需重复注入。"

    messages.append({"role": "user", "content": msg})
    if checkpoint:
        append_checkpoint(messages, binding=binding)
    return True, "已注入论文项目上下文。请继续输入你的问题。"


def resume_banner(*, binding: SessionBinding) -> str | None:
    """Optional startup banner (informational only — never auto-injects)."""
    stats = session_stats(binding=binding)
    state = load_state()
    if not stats["exists"] and state is None:
        return None

    lines = ["--- 会话 ---", format_session_line(binding=binding)]
    if state and show_project_in_welcome():
        state = sync_chapters_from_disk(state)
        done = sum(1 for c in state.chapters if c.status == "done")
        total = len(state.chapters)
        lines.append(
            f"磁盘上的论文项目：{state.title}（{done}/{total} 章）"
            f" — 输入 /resume project 继续"
        )
    lines.append("---")
    return "\n".join(lines)


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif block.get("type") == "tool_result" and block.get("content"):
                parts.append(str(block["content"]))
        elif getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(str(block.text))
    return "\n".join(parts)


def _last_message_preview(messages: list, *, max_len: int = 220) -> str | None:
    """One-line snippet of the latest user/assistant turn for switch confirmation."""
    skip_prefixes = (
        "[Resume context]",
        "[Session resumed]",
        "[Scheduled]",
        "[Skill loaded:",
    )
    for msg in reversed(messages):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(msg.get("content")).strip()
        if not text:
            continue
        if role == "user" and any(text.startswith(p) for p in skip_prefixes):
            continue
        one_line = " ".join(text.split())
        if len(one_line) > max_len:
            one_line = one_line[: max_len - 1] + "…"
        label = "用户" if role == "user" else "助手"
        return f"最近一条（{label}）：{one_line}"
    return None


def switch_to_session(session_id: str, messages: list, *, binding: SessionBinding) -> tuple[SessionBinding, str]:
    """Checkpoint *binding*, then switch *messages* to *session_id*. Returns (new_binding, note)."""
    from harness.todos.state import load_todos_from_disk

    # 1. Checkpoint current binding before we leave it.
    if messages:
        append_checkpoint(messages, binding=binding)

    target_binding = session_binding(session_id)
    if not target_binding.paths.root.is_dir():
        return binding, f"会话不存在：{session_id}"

    write_active_session_id(session_id)
    loaded = load_session_messages(binding=target_binding) or []
    messages.clear()
    messages.extend(loaded)

    meta = read_session_meta(binding=target_binding)
    meta["active_persisted"] = len(loaded)
    write_session_meta(meta, binding=target_binding)
    load_todos_from_disk(binding=target_binding)

    title = meta.get("title") or "(untitled)"
    lines = [f"已切换到会话：{title}（{len(loaded)} 条消息）"]
    preview = _last_message_preview(loaded)
    if preview:
        lines.append(preview)
    return target_binding, "\n".join(lines)


def format_resume_status(*, include_project: bool = True) -> str:
    """Minimal /resume: numbered sessions + created time (+ workflow one-liner)."""
    from harness.project.session_registry import format_session_list_block

    lines = [format_session_list_block(limit=20)]

    if include_project:
        state = load_state()
        if state:
            created = state.updated_at or "—"
            lines.extend(
                [
                    "",
                    "长任务",
                    f"  {state.title}  ·  {created}",
                    "（续：/resume project  ·  删：/resume delete project）",
                ]
            )

    return "\n".join(lines)


def delete_workflow_state() -> str:
    """Delete single-slot ``state.json`` (long thesis/workflow)."""
    from harness.project.state import STATE_PATH

    state = load_state()
    if state is None or not STATE_PATH.exists():
        return "无长任务档案可删。"
    title = state.title
    STATE_PATH.unlink()
    return f"已删除长任务：{title}"


def delete_session_entry(row: dict, messages: list | None, *, binding: SessionBinding) -> tuple[str, SessionBinding]:
    """Delete one session directory; if it was active, start a fresh session."""
    from harness.project.session_registry import (
        create_session,
        delete_session_by_id,
    )
    from harness.todos.state import clear_todos, load_todos_from_disk

    was_active = row.get("active")
    ok, title_or_err = delete_session_by_id(row["id"])
    if not ok:
        return title_or_err, binding

    if was_active and messages is not None:
        messages.clear()
        new_binding = create_session()
        clear_todos(delete_file=False)
        load_todos_from_disk(binding=new_binding)
        return f"已删除当前会话：{title_or_err}。已开启新会话。", new_binding

    return f"已删除会话：{title_or_err}", binding


def run_resume_command(
    args: str = "",
    *,
    messages: list | None = None,
    binding: SessionBinding | None = None,
) -> tuple[str, SessionBinding | None]:
    """Handle /resume [N|name|project|status]. Returns (note, new_binding_or_None)."""
    from harness.project.session_registry import resolve_session_selector

    raw = (args or "").strip()
    sub = raw.lower()

    if sub.startswith("delete "):
        target = raw[7:].strip()
        if not target:
            return (
                "用法：\n"
                "  /resume delete <序号|名称>   删除列表中的会话\n"
                "  /resume delete project       删除长任务 state.json",
                binding,
            )
        if target.lower() in ("project", "workflow", "thesis", "长任务"):
            return delete_workflow_state(), binding
        row, err = resolve_session_selector(target)
        if err:
            return err, binding
        assert row is not None
        if messages is None:
            return "请在 CLI 中执行 /resume delete <序号>", binding
        if binding is None:
            return "内部错误：缺少会话绑定", binding
        note, new_binding = delete_session_entry(row, messages, binding=binding)
        return note, new_binding

    if sub in ("", "status", "list", "session"):
        return format_resume_status(include_project=True), binding

    if sub in ("project", "thesis", "chapters"):
        if messages is None:
            return "请在 CLI 中执行 /resume project，将论文上下文注入当前会话。", binding
        if binding is None:
            return "内部错误：缺少会话绑定", binding
        _ok, message = inject_project_context(messages, binding=binding, checkpoint=True)
        return message, binding

    # /resume 2  or  /resume <name>
    row, err = resolve_session_selector(raw)
    if err:
        return err, binding
    assert row is not None
    if messages is None:
        return "请在 CLI 中执行 /resume <序号> 以切换会话。", binding
    if binding is None:
        return "内部错误：缺少会话绑定", binding
    if row["active"]:
        preview = _last_message_preview(messages or [])
        base = f"已在当前会话：{row['title']}"
        return (f"{base}\n{preview}" if preview else base), binding
    new_binding, note = switch_to_session(row["id"], messages, binding=binding)
    return note, new_binding


# Backward-compatible alias used in tests/docs
def resume_context_message() -> str | None:
    if not should_auto_inject_project_on_startup():
        return None
    return project_context_message()


def on_write_file(path: str) -> None:
    """Update project state when agent writes under output/."""
    from harness.project.state import get_or_create_state, save_state

    state = get_or_create_state()
    rel = str(path).replace("\\", "/")
    try:
        rel_path = str((WORKDIR / path).resolve().relative_to(WORKDIR)).replace("\\", "/")
    except Exception:
        rel_path = rel

    if not rel_path.startswith(state.output_dir.rstrip("/") + "/") and rel_path != state.output_dir:
        if not rel_path.startswith("output/"):
            return

    for chapter in state.chapters:
        if chapter.path == rel_path or rel_path.startswith(f"{state.output_dir}/{chapter.id}"):
            chapter.path = rel_path
            chapter.status = "done"
            state.current_chapter = chapter.id
            save_state(state)
            return

    if state.current_chapter:
        for chapter in state.chapters:
            if chapter.id == state.current_chapter:
                chapter.path = rel_path
                chapter.status = "done"
                save_state(state)
                return


def checkpoint_history(messages: list, *, binding: SessionBinding) -> None:
    if messages:
        append_checkpoint(messages, binding=binding)
