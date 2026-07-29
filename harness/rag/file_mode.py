"""File-mode RAG Q&A: always retrieve, scope = selected docs or all."""

from __future__ import annotations

from harness.rag.doc_picker import run_doc_picker
from harness.rag.ingest import load_manifest
from harness.rag.qa import answer_question
from harness.rag.selection import (
    SCOPE_ALL,
    SCOPE_SELECTED,
    SCOPE_UNSET,
    format_selection_summary,
    get_scope,
    load_selection,
    set_scope,
)
from harness.rag.sources import list_indexed_sources
from harness.ui.terminal_menu import is_interactive_tty, select_from_list

_SCOPE_ALL_HINTS = (
    "搜全部",
    "全部文档",
    "所有文档",
    "search all",
    "all docs",
    "all documents",
)
_SCOPE_PICK_HINTS = (
    "指定文档",
    "选文档",
    "换文档",
    "重新选",
    "pick docs",
    "select docs",
)
_LIST_DOCS_HINTS = (
    "有什么文档",
    "有哪些文档",
    "文档列表",
    "列出文档",
    "现在有什么文档",
    "有什么文件",
    "有哪些文件",
    "本地文件",
    "上传了什么",
    "list docs",
    "list files",
    "what documents",
    "what files",
)


def is_file_mode() -> bool:
    from harness.modes import get_mode

    return get_mode() == "file"


def _has_index() -> bool:
    manifest = load_manifest()
    return bool(manifest.get("sources"))


def _index_ready() -> tuple[bool, str]:
    """File mode does not auto-rebuild index after /rag reset — user must /rag index."""
    from harness.rag.corpus import format_files_list

    if _has_index():
        from harness.rag.bootstrap import index_refresh_reason

        reason = index_refresh_reason()
        if reason:
            return False, (
                f"索引已过期：{reason}。\n"
                "请先 /rag index files，再提问。\n\n"
                f"{format_files_list()}"
            )
        return True, ""
    return False, (
        "索引为空（可能刚执行过 /rag reset，或还没建过索引）。\n"
        "本地上传的文件还在 files/ 里 —— 先确认列表，再构建索引：\n"
        "  /rag files\n"
        "  /rag index files\n\n"
        f"{format_files_list()}"
    )


def on_enter_file_mode() -> str:
    """Enter file mode: ensure index, default scope=all — no forced picker."""
    ok, note = _index_ready()
    if not ok:
        return note
    rows = list_indexed_sources()
    if not rows:
        return "尚无已索引文档。请先 /rag index files，再 /mode file。"

    # After /rag reset or first enter: default to ALL without interactive menu.
    # User can later say「指定文档」or /rag pick when they want a subset.
    if get_scope() == SCOPE_UNSET or (
        get_scope() == SCOPE_SELECTED and not load_selection()
    ):
        set_scope(SCOPE_ALL)
    return format_file_mode_banner()


def clarify_scope(*, force: bool = False) -> str:
    """Optional interactive scope choice (call explicitly if needed)."""
    ok, note = _index_ready()
    if not ok:
        return note

    rows = list_indexed_sources()
    if not rows:
        return "尚无已索引文档。请 /rag index files 后再试。"

    if not force and get_scope() in (SCOPE_ALL, SCOPE_SELECTED):
        if get_scope() == SCOPE_SELECTED and not load_selection():
            pass
        else:
            return format_file_mode_banner()

    if not is_interactive_tty():
        set_scope(SCOPE_ALL)
        return format_file_mode_banner()

    labels = [
        "搜全部已索引文档",
        "指定文档（多选）",
    ]
    choice = select_from_list(
        labels,
        title="文件模式 · 检索范围",
        initial_index=0,
        hint="↑↓ · Enter · Esc=全部",
    )
    if choice is None or choice == 0:
        set_scope(SCOPE_ALL)
    else:
        run_doc_picker()
        if load_selection():
            set_scope(SCOPE_SELECTED)
        else:
            set_scope(SCOPE_ALL)
    return format_file_mode_banner()


def format_file_mode_banner() -> str:
    return (
        f"文件模式 · {format_selection_summary()}\n"
        "直接提问即可。改范围：指定文档 / 搜全部 · 退出：/mode direct"
    )


def _maybe_handle_scope_intent(query: str) -> str | None:
    low = query.strip().lower()
    compact = query.strip().replace(" ", "")
    if any(h in compact or h in low for h in _LIST_DOCS_HINTS):
        from harness.rag.corpus import format_files_list

        return format_files_list()
    if any(h in compact or h in low for h in _SCOPE_ALL_HINTS):
        set_scope(SCOPE_ALL)
        return format_file_mode_banner()
    if any(h in compact or h in low for h in _SCOPE_PICK_HINTS):
        run_doc_picker()
        if load_selection():
            set_scope(SCOPE_SELECTED)
        return format_file_mode_banner()
    if compact in ("范围", "当前范围", "scope"):
        return format_file_mode_banner()
    return None


def handle_file_mode_turn(query: str) -> str:
    """Answer a user turn while in file mode (always RAG)."""
    text = query.strip()
    if not text:
        return format_file_mode_banner()

    scope_msg = _maybe_handle_scope_intent(text)
    if scope_msg is not None:
        return scope_msg

    ok, note = _index_ready()
    if not ok:
        return note

    if get_scope() == SCOPE_SELECTED and not load_selection():
        return "指定文档列表为空。请说「指定文档」或「搜全部」。"

    # Visible retrieval cue before LLM answer.
    from harness.ui.renderer import renderer

    renderer.muted(f"检索中 · {format_selection_summary()}")
    return answer_question(text)
