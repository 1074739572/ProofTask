"""Unified classic terminal rendering (Rich with plain-text fallback)."""

from __future__ import annotations

import threading
from contextlib import contextmanager

from harness import terminal_state
from harness.ui import theme
from harness.ui.tool_display import (
    hooks_verbose,
    is_failure_tool_output,
    summarize_failure_output,
    summarize_tool_input,
)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    _RICH = True
except ImportError:
    _RICH = False

_console = Console(highlight=False, legacy_windows=False) if _RICH else None


class Renderer:
    """Single entry for classic CLI output; keeps loop/llm free of ad-hoc prints."""

    def _write(self, text: str, *, style: str | None = None, end: str = "\n") -> None:
        if not _RICH or _console is None:
            print(text, end=end, flush=True)
            return
        if threading.current_thread() is threading.main_thread() or not terminal_state.CLI_ACTIVE:
            _console.print(text, style=style, end=end)
            return
        line = ""
        if terminal_state.READLINE_AVAILABLE:
            try:
                import readline

                line = readline.get_line_buffer()
            except Exception:
                line = ""
        plain = Text(str(text), style=style or "")
        with _console.capture() as capture:
            _console.print(plain, end=end)
        rendered = capture.get()
        print(f"\r\033[K{rendered}", end="")
        print(theme.PROMPT + line, end="", flush=True)

    def info(self, message: str) -> None:
        self._write(message, style=theme.INFO)

    def muted(self, message: str) -> None:
        self._write(message, style=theme.MUTED)

    def warn(self, message: str) -> None:
        self._write(message, style=theme.WARN)

    def error(self, message: str) -> None:
        self._write(message, style=theme.ERROR)

    def hook(self, label: str, detail: str = "") -> None:
        if not hooks_verbose():
            return
        suffix = f"  {detail}" if detail else ""
        self._write(f"[hook] {label}{suffix}", style=theme.HOOK)

    def user(self, text: str) -> None:
        if _RICH:
            self._write("")
            self._write(f"{theme.PROMPT}{text}", style=theme.USER)
        else:
            self._write(f"\n{theme.PROMPT}{text}")

    def assistant(self, text: str) -> None:
        if not text:
            return
        if _RICH and _console is not None:
            self._write("")
            _console.print(
                Panel(text, title="Assistant", border_style=theme.ACCENT, padding=(0, 1))
            )
        else:
            self._write(f"\n{text}")

    def tool_intent(self, text: str) -> None:
        """Show model's short rationale before a tool call (not a full answer panel)."""
        if not (text or "").strip():
            return
        lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
        preview = " ".join(lines)
        if len(preview) > 220:
            preview = preview[:219] + "…"
        self._write(f"› {preview}", style=theme.MUTED)

    def tool_start(
        self,
        name: str,
        tool_input: dict | None = None,
        *,
        tool_use_id: str = "",
    ) -> None:
        summary = summarize_tool_input(name, tool_input)
        detail = f"  {summary}" if summary else ""
        self._write(f"● {name}{detail}", style=theme.TOOL)

    def tool_repeat(
        self,
        name: str,
        tool_input: dict | None,
        *,
        streak: int,
        blocked: bool = False,
        tool_use_id: str = "",
    ) -> None:
        """Collapse identical consecutive calls instead of reprinting full lines."""
        summary = summarize_tool_input(name, tool_input)
        detail = f"  {summary}" if summary else ""
        if blocked:
            self._write(
                f"⊘ {name}{detail}  (×{streak} identical — blocked)",
                style=theme.WARN,
            )
        else:
            self._write(
                f"↻ {name}{detail}  (×{streak} identical)",
                style=theme.MUTED,
            )

    def tool_result(
        self,
        preview: str,
        limit: int = 280,
        *,
        name: str | None = None,
        tool_input: dict | None = None,
        tool_use_id: str = "",
    ) -> None:
        # Success results stay silent in the terminal (still go to the model).
        if not is_failure_tool_output(preview):
            return
        summary = summarize_failure_output(preview)
        self._write(f"  → {summary}", style=theme.WARN)

    def round_header(self, prefix: str, round_num: int, thinking_text: str, max_len: int = 50) -> None:
        """Show round start with truncated thinking text."""
        text = (thinking_text or "").strip()
        if len(text) > max_len:
            text = text[:max_len] + "..."
        if text:
            line = f"  ◆ Round {round_num} · \"{text}\""
        else:
            line = f"  ◆ Round {round_num}"
        self._write(line, style=theme.MUTED)

    def round_tool(self, name: str, tool_input: dict | None, result_preview: str, limit: int = 60) -> None:
        """Show collapsed tool call within a round: ● tool  args → result."""
        summary = summarize_tool_input(name, tool_input)
        result_text = str(result_preview).strip()
        if len(result_text) > limit:
            result_text = result_text[:limit] + "…"
        if result_text:
            line = f"    ● {name}  {summary} → {result_text}"
        else:
            line = f"    ● {name}  {summary}"
        self._write(line, style=theme.TOOL)

    def round_final(self, prefix: str, text: str, tool_count: int, elapsed: float, max_len: int = 50) -> None:
        """Show final answer round with stats."""
        answer = (text or "").strip()
        if len(answer) > max_len:
            answer = answer[:max_len] + "..."
        line = f"  ◆ Final · \"{answer}\""
        self._write(line, style=theme.MUTED)
        stats_line = f"    └─ {tool_count} tools · {elapsed:.1f}s"
        self._write(stats_line, style=theme.MUTED)

    def files_changed(self, paths: list[str]) -> None:
        """End-of-turn summary of files write_file/edit_file touched."""
        if not paths:
            return
        self._write("Changed files:", style=theme.MUTED)
        for path in paths:
            self._write(f"  · {path}", style=theme.TOOL)

    def plain(self, message: str) -> None:
        self._write(message)

    def todo_checklist(self, todos: list[dict[str, str]]) -> None:
        if not todos:
            return
        if _RICH and _console is not None:
            from harness.ui.todos import render_todo_checklist

            render_todo_checklist(todos, console=_console)
            return
        from harness.ui.todos import _plain_checklist

        self._write(_plain_checklist(todos))

    @contextmanager
    def llm_busy(self, model_tag: str):
        label = f"Thinking  {model_tag}"
        if _RICH and _console is not None:
            with _console.status(f"[{theme.ACCENT}]{label}[/{theme.ACCENT}]", spinner="dots"):
                yield
        else:
            self.muted(f"[llm] {model_tag}")
            yield


renderer = Renderer()
