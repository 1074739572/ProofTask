"""Unified terminal rendering (Rich) with plain-text fallback.

When the Textual TUI is active (O1), all output goes exclusively to the TUI
bridge — no Rich dual-write.
"""

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
from harness.ui.tui.events import ToolEvent

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    _RICH = True
except ImportError:
    _RICH = False

_console = Console(highlight=False, legacy_windows=False) if _RICH else None


def _tui_bridge():
    from harness.ui.tui.mode import is_tui_active, is_tui_shutdown

    if is_tui_active():
        from harness.ui.tui.bridge import BRIDGE

        return BRIDGE
    # Quitting TUI while worker still runs: never fall through to Rich console.
    if is_tui_shutdown():
        return _SHUTDOWN_SINK
    return None


class _ShutdownSink:
    """No-op sink used while TUI is shutting down (X1)."""

    def push_step(self, line: str) -> None:
        return None

    def push_final(self, text: str) -> None:
        return None

    def push_error(self, text: str) -> None:
        return None

    def push_warn(self, text: str) -> None:
        return None

    def push_status(self, text: str) -> None:
        return None

    def set_busy(self, busy: bool) -> None:
        return None

    def push_tool(self, event: ToolEvent) -> None:
        return None

    def reset_turn(self, user_query: str = "", model: str = "") -> None:
        return None


_SHUTDOWN_SINK = _ShutdownSink()


class Renderer:
    """Single entry for CLI/TUI output; keeps loop/llm free of ad-hoc prints."""

    def _write(self, text: str, *, style: str | None = None, end: str = "\n") -> None:
        bridge = _tui_bridge()
        if bridge is not None:
            bridge.push_step(str(text).rstrip("\n") if end == "\n" else str(text))
            return
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
        bridge = _tui_bridge()
        if bridge is not None:
            bridge.push_warn(message)
            return
        self._write(message, style=theme.WARN)

    def error(self, message: str) -> None:
        bridge = _tui_bridge()
        if bridge is not None:
            bridge.push_error(message)
            return
        self._write(message, style=theme.ERROR)

    def hook(self, label: str, detail: str = "") -> None:
        if not hooks_verbose():
            return
        suffix = f"  {detail}" if detail else ""
        self._write(f"[hook] {label}{suffix}", style=theme.HOOK)

    def user(self, text: str) -> None:
        if _tui_bridge() is not None:
            # TUI already shows the query in Steps via reset_turn.
            return
        if _RICH:
            self._write("")
            self._write(f"{theme.PROMPT}{text}", style=theme.USER)
        else:
            self._write(f"\n{theme.PROMPT}{text}")

    def assistant(self, text: str) -> None:
        if not text:
            return
        bridge = _tui_bridge()
        if bridge is not None:
            from harness.providers.errors import is_error_assistant_text

            if is_error_assistant_text(text):
                # Strip the history marker so the dock shows a clean error card.
                clean = text.lstrip()
                if clean.startswith("[Error]"):
                    clean = clean[len("[Error]") :].lstrip(" :")
                bridge.push_error(clean or text)
                return
            bridge.push_final(text)
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
        bridge = _tui_bridge()
        if bridge is not None:
            bridge.push_tool(
                ToolEvent(tool_use_id or f"{name}-current", name, summary, "running")
            )
            return
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
        bridge = _tui_bridge()
        if bridge is not None:
            bridge.push_tool(
                ToolEvent(
                    tool_use_id or f"{name}-repeat-{streak}",
                    name,
                    summary,
                    "blocked" if blocked else "repeat",
                    streak=streak,
                )
            )
            return
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
        bridge = _tui_bridge()
        if bridge is not None:
            failed = is_failure_tool_output(preview)
            low = preview.lower()
            phase = (
                "blocked"
                if failed and ("blocked" in low or "permission denied" in low)
                else ("failed" if failed else "ok")
            )
            summary = summarize_tool_input(name or "tool", tool_input)
            result_preview = (
                summarize_failure_output(preview)
                if failed
                else " ".join(str(preview).split())[:limit]
            )
            bridge.push_tool(
                ToolEvent(
                    tool_use_id or f"{name or 'tool'}-current",
                    name or "tool",
                    summary,
                    phase,
                    preview=result_preview,
                )
            )
            return
        # Success results stay silent in the terminal (still go to the model).
        if not is_failure_tool_output(preview):
            return
        summary = summarize_failure_output(preview)
        self._write(f"  → {summary}", style=theme.WARN)

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
        bridge = _tui_bridge()
        if bridge is not None:
            from harness.ui.todos import _plain_checklist

            bridge.push_step(_plain_checklist(todos))
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
        bridge = _tui_bridge()
        if bridge is not None:
            bridge.push_status(label)
            try:
                yield
            finally:
                bridge.push_status("Running… (Esc to stop)")
            return
        if _RICH and _console is not None:
            with _console.status(f"[{theme.ACCENT}]{label}[/{theme.ACCENT}]", spinner="dots"):
                yield
        else:
            self.muted(f"[llm] {model_tag}")
            yield


renderer = Renderer()
