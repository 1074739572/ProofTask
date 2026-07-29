"""Agent tools for project resume and progress tracking."""

from __future__ import annotations

from harness.project.session import clear_history, load_history
from harness.project.session_store import format_session_line
from harness.project.transcript import format_transcript_hint, import_transcript, list_transcripts
from harness.project.state import (
    Chapter,
    format_status,
    get_or_create_state,
    init_state,
    save_state,
    sync_chapters_from_disk,
)


def run_project_status() -> str:
    state = sync_chapters_from_disk(get_or_create_state())
    history = load_history()
    if history:
        user_turns = sum(1 for m in history if m.get("role") == "user")
        extra = (
            f"\n{format_session_line()}"
            f"\nSnapshot: {len(history)} entries ({user_turns} user prompts)"
        )
    else:
        extra = f"\n{format_session_line()}"
    hint = format_transcript_hint()
    if hint:
        extra += f"\n\n{hint}"
    return format_status(state) + extra


def run_project_import_transcript(path: str = "", mode: str = "summary", merge: bool = False) -> str:
    return import_transcript(path=path or None, mode=mode, merge=merge)


def run_project_list_transcripts() -> str:
    transcripts = list_transcripts()
    if not transcripts:
        return "No transcripts under .transcripts/"
    lines = ["Transcript backups (.transcripts/):"]
    for path in transcripts:
        lines.append(f"  {path.name}  ({path.stat().st_size // 1024} KB)")
    lines.append("\nRestore: /import-transcript [filename]  or  /import-transcript full")
    return "\n".join(lines)


def run_project_init(
    title: str = "研制报告改写",
    source_doc: str = "files/基于深度学习的对流云识别与外推算法研究_终稿_v1.docx",
    reset: bool = False,
) -> str:
    if reset:
        clear_history()
    state = init_state(title=title, source_doc=source_doc)
    return f"Initialized project.\n\n{format_status(state)}"


def run_project_set_chapter(
    chapter_id: str,
    status: str = "in_progress",
    path: str = "",
    notes: str = "",
) -> str:
    state = get_or_create_state()
    found = None
    for chapter in state.chapters:
        if chapter.id == chapter_id:
            found = chapter
            break
    if not found:
        ids = ", ".join(c.id for c in state.chapters)
        return f"Unknown chapter_id '{chapter_id}'. Available: {ids}"

    if status in ("pending", "in_progress", "done"):
        found.status = status
    if path:
        found.path = path
    if notes:
        found.notes = notes
    state.current_chapter = chapter_id
    save_state(state)
    return f"Updated {chapter_id} → {status}\n\n{format_status(state)}"


def run_project_note(notes: str) -> str:
    state = get_or_create_state()
    state.notes = notes.strip()
    save_state(state)
    return f"Notes saved.\n\n{format_status(state)}"


def run_project_clear(*, clear_project: bool = True, binding=None) -> str:
    """OpenCode-style /clear: by default wipes session + todos + project state.

    Pass ``clear_project=False`` for the lighter "/clear session" variant that
    keeps ``.project/state.json`` (thesis chapter table) on disk.
    """
    from harness.project.session_registry import ensure_active_session, read_active_session_id, session_binding
    from harness.project.session_store import clear_session
    from harness.project.state import STATE_PATH
    from harness.todos.state import clear_todos

    if binding is None:
        sid = read_active_session_id()
        binding = session_binding(sid) if sid else ensure_active_session(fresh=False)

    clear_todos(delete_file=False)
    archived = clear_session(binding=binding, archive=True)
    parts = []
    if archived:
        parts.append(f"已结束会话（目录保留，含其 todos）：{archived}")
    else:
        parts.append("已开启新会话（此前无 active session）。")

    if clear_project:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
            parts.append("已删除 .project/state.json（论文/长任务章节表，单槽）。")
        else:
            parts.append("未找到 .project/state.json，无需删除。")
        parts.append(
            "已全新起步（OpenCode）。"
            "todos 跟新 session；续论文：/resume project 或 project_init。"
        )
    else:
        parts.append(
            "论文章节表已保留（.project/state.json）——这是 /clear session 轻量版。"
            "当前会话已换新 id，旧 session 的 todos 仍在其目录下。"
        )
    return "\n".join(parts)


def run_project_reset() -> str:
    clear_history()
    return (
        "Cleared saved conversation (session archived). "
        "Project chapter state kept — use project_init reset=true to fully reset."
    )
