"""Interactive multi-select picker for indexed RAG documents."""

from __future__ import annotations

import time

from harness.rag.selection import load_selection, set_selection
from harness.rag.sources import format_docs_list, list_indexed_sources
from harness.ui.terminal_menu import drain_stdin, is_interactive_tty, read_menu_key

try:
    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    _RICH = True
except ImportError:
    _RICH = False


def _labels_and_sources(rows: list[dict], selected_names: set[str]) -> tuple[list[str], list[str], list[bool]]:
    labels: list[str] = []
    sources: list[str] = []
    checked: list[bool] = []
    for row in rows:
        sources.append(row["source"])
        is_on = row["source"] in selected_names
        checked.append(is_on)
        box = "[x]" if is_on else "[ ]"
        labels.append(
            f"{box} {row['source']} "
            f"({row.get('child_chunks', 0)} child, {row.get('chars', 0)} chars)"
        )
    return labels, sources, checked


def _render_multi(
    labels: list[str],
    *,
    index: int,
    checked: list[bool],
    title: str,
    hint: str,
) -> Text:
    body = Text()
    body.append(f"{title}\n", style="bold")
    for row, label in enumerate(labels):
        selected_row = row == index
        prefix = "› " if selected_row else "  "
        style = "bold cyan" if selected_row else "dim"
        mark = "[x]" if checked[row] else "[ ]"
        text = label[4:] if label.startswith("[x] ") or label.startswith("[ ] ") else label
        body.append(prefix, style=style)
        body.append(f"{mark} {text}", style=style)
        body.append("\n")
    body.append(f"\n{hint}", style="dim")
    return body


def _read_key_with_space() -> str:
    if __import__("sys").platform == "win32":
        import msvcrt

        while True:
            if not msvcrt.kbhit():
                time.sleep(0.02)
                continue
            ch = msvcrt.getch()
            if ch == b" ":
                return "space"
            if ch in (b"\x00", b"\xe0") and msvcrt.kbhit():
                code = msvcrt.getch()
                if code == b"H":
                    return "up"
                if code == b"P":
                    return "down"
                continue
            if ch in (b"\r", b"\n"):
                return "enter"
            if ch == b"\x03":
                raise KeyboardInterrupt
            if ch == b"\x1b":
                return "esc"
    key = read_menu_key()
    return key


def _run_tui_doc_picker() -> str:
    """Block on the Textual multi-select panel (worker-thread safe)."""
    from harness.ui.tui.bridge import BRIDGE

    rows = list_indexed_sources()
    if not rows:
        return (
            "尚无已索引文档。\n"
            "先 /rag files 查看本地上传，再 /rag index files。"
        )
    chosen = BRIDGE.ask_doc_multi_select(rows)
    if chosen is None:
        return "Selection unchanged."
    set_selection(chosen)
    if not chosen:
        return "Selection cleared — 将搜索全部已索引文档。"
    lines = ["Saved selection:"]
    lines.extend(f"  - {name}" for name in chosen)
    return "\n".join(lines)


def run_doc_picker() -> str:
    rows = list_indexed_sources()
    if not rows:
        return (
            "尚无已索引文档。\n"
            "先 /rag files 查看本地上传，再 /rag index files。"
        )

    try:
        from harness.ui.tui.mode import is_tui_active

        if is_tui_active():
            return _run_tui_doc_picker()
    except Exception:
        pass

    if not is_interactive_tty():
        return (
            "当前无法打开交互多选。\n"
            f"{format_docs_list()}\n\n"
            "请用: /rag select 1,3"
        )

    selected_names = set(load_selection())
    labels, sources, checked = _labels_and_sources(rows, selected_names)
    index = 0
    hint = "↑↓ move · Space toggle · Enter save · Esc cancel"
    title = "Select documents for file-mode /rag ask"

    drain_stdin()

    if not _RICH:
        print(title)
        print(hint)
        for label in labels:
            print(f"  {label}")
        return "Use /rag select 1,3 in non-rich mode."

    console = Console(highlight=False, legacy_windows=False)
    with Live(
        _render_multi(labels, index=index, checked=checked, title=title, hint=hint),
        console=console,
        refresh_per_second=12,
        transient=True,
    ) as live:
        while True:
            key = _read_key_with_space()
            if key == "up":
                index = (index - 1) % len(labels)
            elif key == "down":
                index = (index + 1) % len(labels)
            elif key == "space":
                checked[index] = not checked[index]
            elif key == "enter":
                chosen = [sources[i] for i, on in enumerate(checked) if on]
                set_selection(chosen)
                if not chosen:
                    return "Selection cleared — /rag ask will search all indexed documents."
                lines = ["Saved selection:"]
                lines.extend(f"  - {name}" for name in chosen)
                return "\n".join(lines)
            elif key == "esc":
                return "Selection unchanged."
            live.update(
                _render_multi(labels, index=index, checked=checked, title=title, hint=hint)
            )
