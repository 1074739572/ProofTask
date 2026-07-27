"""Clickable meta chips under the chat pane."""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Collapsible, Static

from harness.ui.tui.events import ToolEvent


_TOOL_ICONS = {
    "running": "●",
    "ok": "✓",
    "failed": "✗",
    "blocked": "⊘",
    "repeat": "↻",
}


class ToolCard(Collapsible):
    """A compact tool call whose status can be updated in place."""

    def __init__(self, event: ToolEvent, *, live: bool = True) -> None:
        self.tool_use_id = event.tool_use_id
        self.signature = (event.name, event.summary)
        self._detail = Static("", classes="tool-card-detail", markup=False)
        classes = "tool-card"
        if live:
            classes += " turn-live"
        super().__init__(
            self._detail,
            title=self._title_for(event),
            collapsed=True,
            classes=classes,
        )
        self.update_event(event)

    @staticmethod
    def _title_for(event: ToolEvent) -> str:
        icon = _TOOL_ICONS.get(event.phase, "●")
        summary = f"  {event.summary}" if event.summary else ""
        repeat = f"  ×{event.streak}" if event.streak > 1 else ""
        return f"{icon} {event.name}{summary}{repeat}"

    def update_event(self, event: ToolEvent) -> None:
        self.title = self._title_for(event)
        for phase in _TOOL_ICONS:
            self.remove_class(f"tool-{phase}")
        self.add_class(f"tool-{event.phase}")
        detail = event.preview.strip()
        if not detail:
            if event.phase == "running":
                detail = "Running…"
            elif event.phase == "ok":
                detail = "Completed"
            elif event.phase == "blocked":
                detail = "Blocked by guard or permission policy"
        self._detail.update(detail)


class ExecutionTraceCard(Collapsible):
    """One turn's inspectable reasoning and tool activity.

    The card stays open while work is happening so progress remains visible, then
    folds after the final answer to keep the chat timeline focused on outcomes.
    """

    def __init__(self, *, live: bool = True, initial_tools: list[ToolCard] | None = None) -> None:
        tools = initial_tools or []
        self._steps = Static("", classes="trace-step", markup=False)
        self._content = Vertical(self._steps, *tools, classes="execution-trace-content")
        self._pending_details: list = []
        self._detail_flush_scheduled = False
        self._step_lines: list[str] = []
        self._step_count = 0
        self._tool_count = len(tools)
        self._failed = False
        self._live = live
        classes = "execution-trace"
        if live:
            classes += " turn-live"
        super().__init__(
            self._content,
            title="◌ 执行轨迹 · 准备中",
            collapsed=False,
            classes=classes,
        )

    def _mount_detail(self, widget) -> None:
        """Mount details once the trace is attached, preserving event order."""
        if self._content.is_attached:
            self._content.mount(widget)
            return
        self._pending_details.append(widget)
        if self._detail_flush_scheduled:
            return
        self._detail_flush_scheduled = True

        def _flush() -> None:
            self._detail_flush_scheduled = False
            if not self._content.is_attached:
                self.call_after_refresh(_flush)
                self._detail_flush_scheduled = True
                return
            pending, self._pending_details = self._pending_details, []
            if pending:
                self._content.mount(*pending)

        self.call_after_refresh(_flush)

    def add_step(self, text: str) -> Static:
        self._step_count += 1
        self._step_lines.append(text)
        self._step_lines = self._step_lines[-80:]
        self._steps.update("\n".join(self._step_lines))
        self._refresh_title()
        return self._steps

    def repeat_last_step(self, text: str, count: int) -> None:
        """Update the last step without mounting another layout node."""
        if not self._step_lines:
            self.add_step(text)
            return
        self._step_lines[-1] = f"{text}  ×{count}"
        self._steps.update("\n".join(self._step_lines))

    def add_tool(self, card: ToolCard) -> None:
        self._tool_count += 1
        self._mount_detail(card)
        self._refresh_title()

    def note_tool_phase(self, phase: str) -> None:
        if phase in ("failed", "blocked"):
            self._failed = True
        self._refresh_title()

    def finish(self, *, failed: bool = False) -> None:
        self._failed = self._failed or failed
        self._live = False
        self._refresh_title()
        # History keeps the evidence one click away without burying the answer.
        self.collapsed = True

    def _refresh_title(self) -> None:
        if self._live:
            icon, label = "●", "执行中"
        elif self._failed:
            icon, label = "⚠", "已结束，存在警告"
        else:
            icon, label = "✓", "已完成"
        parts = [f"{icon} 执行轨迹 · {label}"]
        if self._step_count:
            parts.append(f"{self._step_count} 步")
        if self._tool_count:
            parts.append(f"{self._tool_count} 个工具")
        self.title = " · ".join(parts)


class MetaChip(Static):
    """Clickable status chip (model / mode)."""

    DEFAULT_CSS = """
    MetaChip {
        width: auto;
        height: 1;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    MetaChip:hover {
        text-style: bold underline;
    }
    """

    def __init__(self, renderable: str = "", *, chip: str = "", **kwargs) -> None:
        super().__init__(renderable, **kwargs)
        self.chip = chip

    def on_click(self) -> None:
        app = self.app
        if self.chip == "model" and hasattr(app, "action_pick_model"):
            app.action_pick_model()
        elif self.chip == "mode" and hasattr(app, "action_pick_mode"):
            app.action_pick_mode()
