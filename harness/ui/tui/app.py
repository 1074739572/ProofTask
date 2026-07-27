"""Textual App: Usage | Chat | Meta | Composer (Send/Stop)."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from harness.agent.cancel import request_cancel
from harness.ui.tui.bridge import BRIDGE
from harness.ui.tui.events import (
    BackgroundEvent,
    PermissionRequest,
    PermissionResponse,
    RuntimeMetrics,
    ToolEvent,
)
from harness.ui.tui.mode import begin_tui_shutdown, set_tui_active
from harness.ui.tui.widgets import ExecutionTraceCard, MetaChip, ToolCard

_CSS_PATH = Path(__file__).with_name("theme.tcss")
_TURN_LIVE = "turn-live"
# Terminal mouse reporting can leak into TextArea as literal text when a host
# enables xterm/SGR tracking but fails to consume an event. Match only complete
# mouse-report sequences, never ordinary escape sequences or user text.
_TERMINAL_MOUSE_REPORT_RE = re.compile(
    r"(?:\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[M.{3})"
)
_COMPOSER_PLACEHOLDER = "❯ 输入任务…  Enter 发送 · Shift+Enter 换行 · Esc 中断"


def _textarea_bindings_without_copy() -> list[Binding]:
    """Drop TextArea's default Ctrl+C so our OS-clipboard copy wins."""
    out: list[Binding] = []
    for binding in TextArea.BINDINGS:
        keys = {part.strip() for part in (binding.key or "").split(",")}
        if keys & {"ctrl+c", "super+c", "ctrl+insert"}:
            continue
        out.append(binding)
    return out


class ComposerTextArea(TextArea):
    """Chat composer: Enter sends; Shift+Enter inserts a newline."""

    # Keep undo/nav bindings; replace copy/paste with OS clipboard paths.
    BINDINGS = [
        *_textarea_bindings_without_copy(),
        Binding("enter", "composer_submit", "Send", show=False, priority=True),
        Binding("shift+enter", "composer_newline", "Newline", show=False, priority=True),
        # Selection-only: no selection → SkipAction → App.copy_selection (last answer).
        Binding(
            "ctrl+c,ctrl+insert,super+c",
            "composer_copy",
            "Copy",
            show=False,
            priority=True,
        ),
        Binding("ctrl+v,ctrl+shift+v,super+v", "composer_paste", "Paste", show=False, priority=True),
        Binding("ctrl+up", "composer_history_previous", "Previous", show=False),
        Binding("ctrl+down", "composer_history_next", "Next", show=False),
        # Plain up/down: at text boundaries, navigate history instead of cursor.
        Binding("up", "composer_up_or_history", "Up", show=False, priority=True),
        Binding("down", "composer_down_or_history", "Down", show=False, priority=True),
    ]

    def action_composer_submit(self) -> None:
        app = self.app
        if hasattr(app, "action_submit_or_stop"):
            app.action_submit_or_stop()  # type: ignore[attr-defined]

    def action_composer_newline(self) -> None:
        self.insert("\n")

    def action_composer_up_or_history(self) -> None:
        """Up: cursor at start of text → previous history; otherwise cursor up."""
        if self.cursor_at_start_of_text:
            app = self.app
            if hasattr(app, "composer_history_previous"):
                app.composer_history_previous()
        else:
            self.action_cursor_up()

    def action_composer_down_or_history(self) -> None:
        """Down: cursor at end of text → next history; otherwise cursor down."""
        if self.cursor_at_end_of_text:
            app = self.app
            if hasattr(app, "composer_history_next"):
                app.composer_history_next()
        else:
            self.action_cursor_down()

    def action_composer_paste(self) -> None:
        """Paste OS clipboard when Textual's in-app clipboard is empty (Windows)."""
        self.action_paste()

    def action_composer_copy(self) -> None:
        self.action_copy()

    def action_copy(self) -> None:
        """Copy selected composer text to in-app clipboard; don't write OS clipboard."""
        from textual.actions import SkipAction

        selected = (self.selected_text or "").replace("\x00", "")
        if not selected:
            raise SkipAction()
        try:
            self.app._clipboard = selected  # noqa: SLF001
        except Exception:
            pass
        if hasattr(self.app, "tui_set_status"):
            self.app.tui_set_status("已复制选区")  # type: ignore[attr-defined]

    def _clipboard_text(self) -> str:
        from harness.ui.tui.clipboard import read_os_clipboard

        text = self.app.clipboard or ""
        if not text:
            text = read_os_clipboard()
        return (text or "").replace("\x00", "")

    @staticmethod
    def _strip_terminal_mouse_reports(text: str) -> str:
        """Discard leaked xterm mouse reports without touching normal input."""
        return _TERMINAL_MOUSE_REPORT_RE.sub("", text or "")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Remove mouse protocol bytes that some terminals misroute as text."""
        if event.text_area is not self:
            return
        clean = self._strip_terminal_mouse_reports(self.text)
        if clean == self.text:
            return
        # Replacing the document is safe here: these bytes are transport noise,
        # not user content. Keep focus so a stray event cannot steal it.
        self.text = clean
        self.focus()

    def _insert_paste_text(self, text: str) -> None:
        text = self._strip_terminal_mouse_reports(text)
        if not text:
            return
        # Prefer the selection-aware path; fall back to plain insert.
        try:
            result = self._replace_via_keyboard(text, *self.selection)
        except Exception:
            result = None
        if result is not None:
            self.move_cursor(result.end_location)
        else:
            try:
                self.insert(text)
            except Exception:
                # Last resort: replace whole document.
                self.text = (self.text or "") + text
        self.focus()

    def action_paste(self) -> None:
        if self.read_only:
            return
        text = self._clipboard_text()
        if not text:
            return
        # Do NOT call copy_to_clipboard here — OSC 52 can confuse Windows Terminal
        # and re-enter paste handling. Keep OS clipboard as the source of truth.
        self._insert_paste_text(text)

    async def _on_paste(self, event) -> None:
        """Bracketed paste from the terminal (right-click / WT paste when focused)."""
        if self.read_only:
            return
        pasted = event.text or ""
        if not pasted:
            # Some hosts fire Paste with empty payload — fall back to OS clipboard.
            pasted = self._clipboard_text()
        self._insert_paste_text(pasted)
        event.stop()
        event.prevent_default()

    def action_composer_history_previous(self) -> None:
        app = self.app
        if hasattr(app, "composer_history_previous"):
            app.composer_history_previous()  # type: ignore[attr-defined]

    def action_composer_history_next(self) -> None:
        app = self.app
        if hasattr(app, "composer_history_next"):
            app.composer_history_next()  # type: ignore[attr-defined]


class HarnessApp(App[None]):
    """Merged chat TUI with Send/Stop toggle and clean quit (X1/B2/U1)."""

    TITLE = "improved_harness"
    CSS_PATH = _CSS_PATH
    BINDINGS = [
        Binding("escape", "interrupt", "Stop", show=True, priority=True),
        # Ctrl+C: copy selection / last answer to OS clipboard (never quit).
        Binding("ctrl+c,ctrl+insert", "copy_selection", "Copy", show=False, priority=True),
        # Ctrl+Shift+C / Ctrl+Shift+Y: always copy the latest assistant answer.
        # (WT may steal Ctrl+Shift+C when it has a selection — Y is a reliable fallback.)
        Binding(
            "ctrl+shift+c,ctrl+shift+y",
            "copy_last_answer",
            "Copy answer",
            show=True,
            priority=True,
        ),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
        # Also allow Ctrl+Enter as send (same as Enter).
        Binding("ctrl+enter", "submit_or_stop", "Send", show=True, priority=True),
        # App-level paste: works even when Chat scroll has focus, not the composer.
        Binding("ctrl+v,ctrl+shift+v", "paste_to_composer", "Paste", show=False, priority=True),
    ]

    def __init__(self, history: list, context: dict, *, model_name: str = "") -> None:
        super().__init__()
        self.history = history
        self.context = context
        self._model_name = model_name
        self._busy = False
        self._worker_lock = threading.Lock()
        self._pick_ids: list[str] = []
        self._pick_callback: Callable[[str | None], None] | None = None
        self._picking = False
        self._doc_multi_picking = False
        self._doc_multi_callback: Callable[[list[str] | None], None] | None = None
        self._doc_multi_sources: list[str] = []
        self._doc_multi_checked: dict[str, bool] = {}
        self._exit_when_idle = False
        self._live_turn = False
        self._permission_request: PermissionRequest | None = None
        self._permission_callback: Callable[[PermissionResponse], None] | None = None
        self._tool_cards: dict[str, ToolCard] = {}
        self._active_trace: ExecutionTraceCard | None = None
        self._last_tool_signature: tuple[str, str] | None = None
        self._last_tool_card: ToolCard | None = None
        self._background_views: dict[str, BackgroundEvent] = {}
        self._runtime_metrics = RuntimeMetrics()
        self._tool_health = "tool —"
        self._network_health = "net —"
        self._last_step_text = ""
        self._last_step_widget: Static | None = None
        self._last_step_count = 0
        self._last_assistant_text = ""
        self._input_history: list[str] = []
        self._input_history_index = 0

    def compose(self) -> ComposeResult:
        yield Label("", id="usage-bar")
        with VerticalScroll(id="chat-pane"):
            yield Vertical(id="chat-stream")
        with Vertical(id="footer-stack"):
            with Horizontal(id="meta-bar"):
                yield MetaChip("", id="meta-model", chip="model")
                yield MetaChip("", id="meta-mode", chip="mode")
                yield Static("", id="meta-runtime")
                yield Static("✅ Ready", id="meta-status")
            with Vertical(id="background-tray"):
                yield Label("后台任务", id="background-title")
                yield Static("", id="background-list", markup=True)
            with Vertical(id="pick-panel"):
                yield Label("", id="pick-title")
                yield OptionList(id="pick-list")
                yield Label("↑↓ · Space 勾选 · Enter 确认 · Esc 取消", id="pick-hint")
            with Vertical(id="interaction-panel"):
                yield Label("", id="interaction-title")
                yield Static("", id="interaction-detail", markup=False)
                yield Input("", id="interaction-input")
                with Horizontal(id="interaction-actions"):
                    yield Button("允许", id="interaction-allow", variant="success")
                    yield Button("拒绝", id="interaction-deny", variant="error")
                    yield Button("取消", id="interaction-cancel")
            yield Static("", id="command-hints", markup=False)
            yield ComposerTextArea(
                "",
                id="user-input",
                soft_wrap=True,
                show_line_numbers=False,
                compact=True,
                tab_behavior="indent",
                placeholder="❯ 输入任务…",
            )

    def on_mount(self) -> None:
        set_tui_active(True)
        BRIDGE.bind(self)
        chat = self.query_one("#chat-pane", VerticalScroll)
        chat.border_title = "Chat"
        self.close_inline_picker(notify=False)
        self._hide_permission_panel(notify=False)
        self.query_one("#background-tray", Vertical).display = False
        self.query_one("#command-hints", Static).display = False
        self.refresh_usage_bar()
        self.refresh_meta_bar()
        self._sync_send_stop_button()
        self.hydrate_history()
        self.set_interval(30.0, self.refresh_usage_bar)
        self._focus_composer()
        md_status = (self.context.get("project_instructions_status") or "").strip()
        if md_status:
            self.tui_set_status(md_status)
        try:
            from harness.providers.netcheck import proxy_health_warning

            warn = proxy_health_warning()
            if warn:
                BRIDGE.push_warn(warn)
                self.tui_set_status("代理未启动 — API 会失败")
                self._network_health = "net ⚠"
                self._refresh_runtime_chips()
        except Exception:
            pass

    def on_unmount(self) -> None:
        begin_tui_shutdown()
        request_cancel()
        # Brief wait so worker can notice cancel before console sink opens.
        got = self._worker_lock.acquire(timeout=2.0)
        if got:
            self._worker_lock.release()
        BRIDGE.unbind()
        set_tui_active(False)

    def _composer(self) -> ComposerTextArea:
        return self.query_one("#user-input", ComposerTextArea)

    def _focus_composer(self) -> None:
        try:
            self._composer().focus()
        except Exception:
            pass

    def _get_composer_text(self) -> str:
        try:
            return self._composer().text
        except Exception:
            return ""

    def _set_composer_text(self, text: str) -> None:
        try:
            self._composer().text = text
        except Exception:
            pass

    def _clear_composer(self) -> None:
        self._set_composer_text("")

    # --- Usage + meta ---

    def refresh_usage_bar(self) -> None:
        from harness.ui.tui.usage_bar import format_usage_bar

        try:
            self.query_one("#usage-bar", Label).update(format_usage_bar())
        except Exception:
            pass

    def refresh_meta_bar(self) -> None:
        from harness.models import model_label
        from harness.modes import get_mode

        self._model_name = model_label()
        mode = get_mode()
        try:
            self.query_one("#meta-model", MetaChip).update(f"🤖 {self._model_name}")
            self.query_one("#meta-mode", MetaChip).update(f"🧭 {mode}")
        except Exception:
            pass

    def refresh_model_header(self) -> None:
        self.refresh_meta_bar()
        self.refresh_usage_bar()

    def tui_set_status(self, text: str) -> None:
        raw = (text or "").strip()
        if not raw:
            return
        low = raw.lower()
        if "run" in low or "think" in low or "stop" in low or "work" in low:
            icon = "⚡"
        elif "interrupt" in low or "error" in low or "roll" in low:
            icon = "⚠"
        else:
            icon = "✅"
        try:
            self.query_one("#meta-status", Static).update(f"{icon} {raw}")
        except Exception:
            pass

    def tui_runtime_metrics(self, metrics: RuntimeMetrics) -> None:
        self._runtime_metrics = metrics
        self._refresh_runtime_chips()

    def _refresh_runtime_chips(self) -> None:
        metrics = self._runtime_metrics
        cache_total = metrics.cache_hit_tokens + metrics.cache_miss_tokens
        cache = (
            f"缓存命中 {100 * metrics.cache_hit_rate:.0f}%"
            if cache_total
            else "缓存 —"
        )
        context = (
            f"上下文 {100 * metrics.context_rate:.0f}%"
            if metrics.context_window
            else "上下文 —"
        )
        try:
            self.query_one("#meta-runtime", Static).update(
                f"{context} · {cache} · {self._tool_health} · {self._network_health}"
            )
        except Exception:
            pass

    def _sync_send_stop_button(self) -> None:
        # Button removed; reflect busy state in status only.
        pass

    def tui_set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._sync_send_stop_button()
        # Keep composer enabled so user can edit while running / after stop prefill.
        if self._busy:
            self.tui_set_status("Running… (Esc / Stop)")
        else:
            if not self._exit_when_idle:
                self.tui_set_status("Ready")
            self.refresh_usage_bar()
            self.refresh_meta_bar()
            if not self._picking:
                self._focus_composer()

    def action_pick_model(self) -> None:
        if self._busy:
            return
        from harness.ui.tui.commands import _handle_model

        _handle_model(self, "/model")

    def action_pick_mode(self) -> None:
        if self._busy:
            return
        from harness.ui.tui.commands import _handle_mode

        _handle_mode(self, "/mode")

    # --- Chat stream ---

    def _chat_stream(self) -> Vertical:
        return self.query_one("#chat-stream", Vertical)

    def _is_chat_at_bottom(self) -> bool:
        """Only follow new output when the user was already viewing the bottom."""
        try:
            pane = self.query_one("#chat-pane", VerticalScroll)
            max_y = float(getattr(pane, "max_scroll_y", 0) or 0)
            current_y = float(getattr(pane, "scroll_y", 0) or 0)
            return max_y <= 0 or current_y >= max_y - 2
        except Exception:
            return True

    def _scroll_chat_end(self, *, follow: bool = True) -> None:
        if not follow:
            return
        try:
            self.query_one("#chat-pane", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def _ensure_active_trace(self, initial_tool: ToolCard | None = None) -> ExecutionTraceCard:
        """Create one per live turn so operational detail does not flood Chat."""
        trace = self._active_trace
        if trace is None:
            trace = ExecutionTraceCard(
                live=self._live_turn,
                initial_tools=[initial_tool] if initial_tool is not None else None,
            )
            self._chat_stream().mount(trace)
            self._active_trace = trace
        elif initial_tool is not None:
            trace.add_tool(initial_tool)
        return trace

    def chat_append(self, kind: str, text: str, *, live: bool | None = None) -> None:
        body = (text or "").rstrip()
        if not body:
            return
        use_live = self._live_turn if live is None else live
        follow_scroll = self._is_chat_at_bottom()
        extra = f" {_TURN_LIVE}" if use_live else ""
        stream = self._chat_stream()
        if kind != "step":
            self._last_step_text = ""
            self._last_step_widget = None
            self._last_step_count = 0
        if kind == "assistant":
            self._last_assistant_text = body
            stream.mount(Markdown(body, classes=f"chat-assistant{extra}"))
        elif kind == "error":
            stream.mount(
                Static(f"✗ {body}", classes=f"chat-error{extra}", markup=False)
            )
        elif kind == "user":
            stream.mount(Static(f"❯ {body}", classes=f"chat-user{extra}", markup=False))
        elif kind == "system":
            stream.mount(Static(body, classes=f"chat-system{extra}", markup=False))
        else:
            stream.mount(Static(body, classes=f"chat-step{extra}", markup=False))
        self._scroll_chat_end(follow=follow_scroll)

    def tui_trim_turn_bubbles(self) -> None:
        """U1: remove widgets tagged for the in-flight turn."""
        stream = self._chat_stream()
        for child in list(stream.children):
            try:
                if _TURN_LIVE in child.classes:
                    child.remove()
            except Exception:
                pass
        self._live_turn = False
        if self._active_trace is not None:
            try:
                self._active_trace.finish()
            except Exception:
                pass
        self._active_trace = None
        self._tool_cards.clear()
        self._last_tool_card = None
        self._last_tool_signature = None

    def tui_seal_turn_bubbles(self) -> None:
        """Keep bubbles but drop live tag so a later interrupt won't tear them."""
        stream = self._chat_stream()
        for child in list(stream.children):
            try:
                if _TURN_LIVE in child.classes:
                    child.remove_class(_TURN_LIVE)
            except Exception:
                pass
        self._live_turn = False
        if self._active_trace is not None:
            try:
                self._active_trace.finish()
            except Exception:
                pass
        self._active_trace = None

    def mount_welcome(self) -> None:
        from harness.ui.tui.welcome_panel import build_welcome_parts

        wide = self.size.width >= 80
        parts = build_welcome_parts(wide=wide)
        stream = self._chat_stream()
        welcome = Vertical(classes="welcome-screen")
        stream.mount(welcome)

        brand_widgets: list = []
        if parts.wide:
            # Stack: face above text
            face = Static(parts.smiley, classes="welcome-smiley", markup=False)
            title = Static(parts.hello_title, classes="welcome-hello", markup=False)
            tag = Static(parts.tagline, classes="welcome-tagline", markup=False)
            welcome.mount(face)
            welcome.mount(title)
            welcome.mount(tag)
            brand_widgets = [face, title, tag]
        else:
            narrow = Static(parts.narrow, classes="welcome-narrow", markup=False)
            welcome.mount(narrow)
            brand_widgets = [narrow]

        # Divider
        sep = Static("───", classes="welcome-sep", markup=False)
        welcome.mount(sep)
        brand_widgets.append(sep)

        # Quote card
        card = Vertical(classes="welcome-quote-card")
        welcome.mount(card)
        q_body = Static(parts.quote_body, classes="welcome-quote-body", markup=False)
        card.mount(q_body)
        quote_widgets = [card, q_body]
        if parts.quote_source:
            q_src = Static(parts.quote_source, classes="welcome-quote-source", markup=False)
            card.mount(q_src)
            quote_widgets.append(q_src)

        self._play_welcome_entrance(brand_widgets, quote_widgets)

    def _play_welcome_entrance(self, brand_widgets: list, quote_widgets: list) -> None:
        targets = [*brand_widgets, *quote_widgets]
        for widget in targets:
            try:
                widget.styles.opacity = 0.0
            except Exception:
                pass

        def _fade(widget: Static, delay: float) -> None:
            def _run() -> None:
                try:
                    widget.styles.animate("opacity", value=1.0, duration=0.15)
                except Exception:
                    try:
                        widget.styles.opacity = 1.0
                    except Exception:
                        pass

            self.set_timer(delay, _run)

        for i, w in enumerate(brand_widgets):
            _fade(w, 0.02 + i * 0.08)
        brand_end = 0.02 + len(brand_widgets) * 0.08
        for i, w in enumerate(quote_widgets):
            _fade(w, brand_end + 0.08 + i * 0.05)

    def hydrate_history(self) -> None:
        from harness.ui.tui.chat_history import iter_history_items

        # Markdown and Collapsible widgets are relatively expensive to lay out.
        # Keep startup bounded; the complete session remains persisted on disk.
        _MAX_HYDRATE_EVENTS = 120

        stream = self._chat_stream()
        for child in list(stream.children):
            child.remove()
        try:
            self.mount_welcome()
        except Exception as exc:
            stream.mount(
                Static(
                    f"(welcome failed: {type(exc).__name__}: {exc})",
                    classes="chat-system",
                    markup=False,
                )
            )
        try:
            events = list(iter_history_items(self.history))
        except Exception as exc:
            stream.mount(
                Static(
                    f"(history hydrate failed: {type(exc).__name__}: {exc})",
                    classes="chat-system",
                    markup=False,
                )
            )
            return
        if not events:
            stream.mount(
                Static(
                    "No messages yet — type below to start.",
                    classes="chat-system",
                    markup=False,
                )
            )
            return
        omitted = 0
        if len(events) > _MAX_HYDRATE_EVENTS:
            omitted = len(events) - _MAX_HYDRATE_EVENTS
            events = events[-_MAX_HYDRATE_EVENTS:]
            stream.mount(
                Static(
                    f"(showing last {_MAX_HYDRATE_EVENTS} of "
                    f"{_MAX_HYDRATE_EVENTS + omitted} history items)",
                    classes="chat-system",
                    markup=False,
                )
            )
        for item in events:
            try:
                if isinstance(item, ToolEvent):
                    stream.mount(ToolCard(item, live=False))
                else:
                    kind, text = item
                    self.chat_append(kind, text, live=False)
            except Exception:
                continue

    def reload_session_view(self) -> None:
        """Re-hydrate Chat after /resume or /clear mutates history/session."""
        self._live_turn = False
        self.hydrate_history()
        self.refresh_usage_bar()
        self.refresh_meta_bar()

    # --- Bridge targets ---

    def tui_reset_turn(self, user_query: str = "", model: str = "") -> None:
        self.close_inline_picker(notify=False)
        self._hide_permission_panel(notify=False)
        self.tui_seal_turn_bubbles()
        self._live_turn = True
        self._tool_cards.clear()
        self._last_tool_card = None
        self._last_tool_signature = None
        self._last_step_text = ""
        self._last_step_widget = None
        self._last_step_count = 0
        self._tool_health = "tool —"
        self._network_health = "net —"
        self._refresh_runtime_chips()
        if model:
            self._model_name = model
            self.refresh_meta_bar()
        if user_query.strip():
            self.chat_append("user", user_query.strip(), live=True)
        self.tui_set_status("Running… (Esc / Stop)")

    def tui_append_step(self, line: str) -> None:
        follow_scroll = self._is_chat_at_bottom()
        chunk = (line or "").rstrip("\n")
        if not chunk:
            return
        for part in chunk.splitlines():
            if part.strip():
                clean = part.rstrip()
                trace = self._ensure_active_trace()
                if clean == self._last_step_text and self._last_step_widget is not None:
                    self._last_step_count += 1
                    trace.repeat_last_step(clean, self._last_step_count)
                    continue
                self._last_step_widget = trace.add_step(clean)
                self._last_step_text = clean
                self._last_step_count = 1
                self._scroll_chat_end(follow=follow_scroll)

    def tui_set_answer(self, text: str) -> None:
        """Append the model answer into Chat history (no separate dock)."""
        if self._active_trace is not None:
            self._active_trace.finish()
        self.chat_append("assistant", text)

    def tui_set_error(self, text: str) -> None:
        """Show an API/turn failure inline in Chat (same scroll timeline as answers)."""
        body = (text or "").strip()
        if not body:
            return
        if self._active_trace is not None:
            self._active_trace.finish(failed=True)
        self.chat_append("error", f"API 错误\n{body}")
        try:
            self.tui_set_status(f"Error: {body.splitlines()[0][:120]}")
            self._network_health = "net ⚠"
            self._refresh_runtime_chips()
        except Exception:
            pass

    def tui_append_assistant(self, text: str) -> None:
        self.tui_set_answer(text)

    def tui_tool_event(self, event: ToolEvent) -> None:
        follow_scroll = self._is_chat_at_bottom()
        signature = (event.name, event.summary)
        card = self._tool_cards.get(event.tool_use_id)
        if (
            card is None
            and event.phase == "repeat"
            and self._last_tool_card is not None
            and self._last_tool_signature == signature
        ):
            card = self._last_tool_card
            self._tool_cards[event.tool_use_id] = card
        if card is None:
            card = ToolCard(event, live=self._live_turn)
            self._tool_cards[event.tool_use_id] = card
            self._ensure_active_trace(card)
        else:
            card.update_event(event)
        self._ensure_active_trace().note_tool_phase(event.phase)
        self._last_tool_card = card
        self._last_tool_signature = signature
        if event.phase in ("failed", "blocked"):
            self._tool_health = "tool ⚠"
        elif event.phase == "ok":
            self._tool_health = "tool ✓"
        if any(word in event.name.lower() for word in ("fetch", "search", "browser", "http")):
            if event.phase in ("failed", "blocked"):
                self._network_health = "net ⚠"
            elif event.phase == "ok":
                self._network_health = "net ✓"
        self._refresh_runtime_chips()
        self._scroll_chat_end()
        self._scroll_chat_end()

    def tui_background_event(self, event: BackgroundEvent) -> None:
        self._background_views[event.task_id] = event
        lines = []
        icons = {"running": "●", "completed": "✓", "failed": "✗"}
        colors = {"running": "#4CA6FF", "completed": "#61D095", "failed": "#FF7B72"}
        for task in self._background_views.values():
            command = " ".join(task.command.split())
            if len(command) > 72:
                command = command[:71] + "…"
            phase = task.phase
            icon = icons.get(phase, "●")
            color = colors.get(phase, "#9FB3C8")
            # Rich markup makes state scanable without losing the compact list.
            lines.append(f"[{color}]{icon} {task.task_id}[/{color}]  {command}")
        try:
            tray = self.query_one("#background-tray", Vertical)
            tray.display = bool(lines)
            self.query_one("#background-title", Label).update(
                f"后台任务 ({sum(1 for item in self._background_views.values() if item.phase == 'running')} 运行中)"
            )
            self.query_one("#background-list", Static).update("\n".join(lines))
        except Exception:
            pass

    # --- Inline permission / editable tool request ---

    def tui_request_permission(
        self,
        request: PermissionRequest,
        callback: Callable[[PermissionResponse], None],
    ) -> None:
        """Show a blocking worker request without leaving the current screen."""
        if self._permission_request is not None:
            callback(PermissionResponse(request.request_id, "deny", request.detail))
            return
        self.close_inline_picker(notify=False)
        self._permission_request = request
        self._permission_callback = callback
        panel = self.query_one("#interaction-panel", Vertical)
        panel.display = True
        self.query_one("#interaction-title", Label).update(f"⚠ {request.title}")
        self.query_one("#interaction-detail", Static).update(request.detail)
        editor = self.query_one("#interaction-input", Input)
        editor.value = request.detail if request.editable else ""
        editor.placeholder = request.placeholder
        editor.display = request.editable
        self.tui_set_status("Waiting for tool permission")
        if request.editable:
            editor.focus()
        else:
            self.query_one("#interaction-allow", Button).focus()

    def _resolve_permission(self, decision: str) -> None:
        request = self._permission_request
        callback = self._permission_callback
        if request is None:
            return
        value = request.detail
        if request.editable:
            value = self.query_one("#interaction-input", Input).value
        response = PermissionResponse(request.request_id, decision, value)
        self._hide_permission_panel(notify=False)
        if callback is not None:
            callback(response)

    def _hide_permission_panel(self, *, notify: bool = True) -> None:
        request = self._permission_request
        callback = self._permission_callback
        self._permission_request = None
        self._permission_callback = None
        try:
            panel = self.query_one("#interaction-panel", Vertical)
            panel.display = False
            self.query_one("#interaction-input", Input).value = ""
        except Exception:
            pass
        if notify and request is not None and callback is not None:
            callback(PermissionResponse(request.request_id, "cancel", request.detail))
        if not self._picking:
            self._focus_composer()

    # --- Inline picker ---

    def open_inline_picker(
        self,
        title: str,
        labels: list[str],
        item_ids: list[str],
        *,
        initial_index: int = 0,
        on_pick: Callable[[str | None], None] | None = None,
    ) -> None:
        if len(labels) != len(item_ids):
            raise ValueError("labels and item_ids must be the same length")
        self._doc_multi_picking = False
        self._doc_multi_callback = None
        self._pick_ids = list(item_ids)
        self._pick_callback = on_pick
        self._picking = True

        panel = self.query_one("#pick-panel", Vertical)
        panel.display = True
        self.query_one("#pick-title", Label).update(title)
        try:
            self.query_one("#pick-hint", Label).update("↑↓ move · Enter confirm · Esc close")
        except Exception:
            pass

        ol = self.query_one("#pick-list", OptionList)
        ol.clear_options()
        for label, item_id in zip(labels, item_ids):
            ol.add_option(Option(label, id=item_id))
        if item_ids:
            ol.highlighted = max(0, min(initial_index, len(item_ids) - 1))
        ol.focus()
        self.tui_set_status(f"{title} — ↑↓ Enter · Esc")

    def tui_open_doc_multi_picker(
        self,
        rows: list[dict],
        on_done: Callable[[list[str] | None], None] | None = None,
    ) -> None:
        """Multi-select indexed docs for /rag pick (Space toggle, Enter save)."""
        sources = [str(row.get("source") or "") for row in rows if row.get("source")]
        if not sources:
            if on_done is not None:
                on_done(None)
            return
        self._pick_callback = None
        self._pick_ids = []
        self._doc_multi_picking = True
        self._doc_multi_callback = on_done
        self._doc_multi_sources = sources
        self._doc_multi_checked = {
            str(row["source"]): bool(row.get("selected"))
            for row in rows
            if row.get("source")
        }
        self._picking = True

        panel = self.query_one("#pick-panel", Vertical)
        panel.display = True
        self.query_one("#pick-title", Label).update("选择检索文档（可多选）")
        try:
            self.query_one("#pick-hint", Label).update(
                "↑↓ 移动 · Space 勾选/取消 · Enter 保存 · Esc 取消"
            )
        except Exception:
            pass
        self._refresh_doc_multi_options(highlight=0)
        self.tui_set_status("文档多选 — Space 勾选 · Enter 保存 · Esc 取消")

    def _refresh_doc_multi_options(self, *, highlight: int | None = None) -> None:
        ol = self.query_one("#pick-list", OptionList)
        current = ol.highlighted if highlight is None else highlight
        ol.clear_options()
        for source in self._doc_multi_sources:
            mark = "[x]" if self._doc_multi_checked.get(source) else "[ ]"
            ol.add_option(Option(f"{mark} {source}", id=source))
        if self._doc_multi_sources:
            ol.highlighted = max(0, min(current or 0, len(self._doc_multi_sources) - 1))
        ol.focus()

    def _close_doc_multi_picker(self, selected: list[str] | None) -> None:
        was = self._doc_multi_picking
        cb = self._doc_multi_callback
        self._doc_multi_picking = False
        self._doc_multi_callback = None
        self._doc_multi_sources = []
        self._doc_multi_checked = {}
        self._picking = False
        try:
            panel = self.query_one("#pick-panel", Vertical)
            panel.display = False
            ol = self.query_one("#pick-list", OptionList)
            ol.clear_options()
        except Exception:
            pass
        if was and cb is not None:
            cb(selected)
        self._focus_composer()

    def close_inline_picker(self, *, notify: bool = True, selected: str | None = None) -> None:
        if self._doc_multi_picking:
            # Esc / interrupt while multi-selecting → cancel.
            self._close_doc_multi_picker(None)
            return
        was = self._picking
        cb = self._pick_callback
        self._picking = False
        self._pick_callback = None
        self._pick_ids = []
        try:
            panel = self.query_one("#pick-panel", Vertical)
            panel.display = False
            ol = self.query_one("#pick-list", OptionList)
            ol.clear_options()
        except Exception:
            pass
        if was and notify and cb is not None:
            cb(selected)
        self._focus_composer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if not self._picking or event.option_list.id != "pick-list":
            return
        event.stop()
        if self._doc_multi_picking:
            chosen = [
                source
                for source in self._doc_multi_sources
                if self._doc_multi_checked.get(source)
            ]
            self._close_doc_multi_picker(chosen)
            self.tui_set_status(
                f"已选择 {len(chosen)} 个文档" if chosen else "已设为全部文档"
            )
            return
        option_id = event.option.id
        if option_id is None:
            idx = event.option_index
            picked = self._pick_ids[idx] if 0 <= idx < len(self._pick_ids) else None
        else:
            picked = str(option_id)
        self.close_inline_picker(notify=True, selected=picked)

    def on_key(self, event) -> None:
        """Space toggles checked state while the RAG multi-select panel is open."""
        if not self._doc_multi_picking:
            return
        if event.key != "space":
            return
        try:
            ol = self.query_one("#pick-list", OptionList)
        except Exception:
            return
        idx = ol.highlighted
        if idx is None or not (0 <= idx < len(self._doc_multi_sources)):
            return
        source = self._doc_multi_sources[idx]
        self._doc_multi_checked[source] = not self._doc_multi_checked.get(source, False)
        self._refresh_doc_multi_options(highlight=idx)
        event.stop()
        event.prevent_default()

    # --- Send / Stop / Quit ---

    def action_quit_app(self) -> None:
        """X1: cancel in-flight turn before leaving so output cannot leak to terminal."""
        begin_tui_shutdown()
        if self._busy:
            request_cancel()
            self._exit_when_idle = True
            self.tui_set_status("Stopping before exit…")
            return
        self.exit()

    def action_copy_selection(self) -> None:
        """Ctrl+C copies only an explicit composer or chat selection; shows text in status."""
        focused = self.focused
        if isinstance(focused, ComposerTextArea):
            selected = focused.selected_text or ""
            if selected:
                self.tui_set_status(f"已复制选区")
                return
        try:
            selected = self.screen.get_selected_text() or ""
        except Exception:
            selected = ""
        if selected:
            try:
                self.screen.clear_selection()
            except Exception:
                pass
            self.tui_set_status(f"已复制选区")
            return
        self.tui_set_status("请先选择文字 · Ctrl+Shift+Y 复制完整回答")

    def action_copy_last_answer(self) -> None:
        """Ctrl+Shift+C/Y: show latest assistant text in status."""
        text = self._latest_assistant_text()
        if not text:
            self.tui_set_status("还没有可复制的回答")
            return
        preview = text[:80].replace("\n", " ")
        self.tui_set_status(f"已复制: {preview}…")

    def _latest_assistant_text(self) -> str:
        """Prefer cached bubble text; then live Chat widget; then session history."""
        cached = (self._last_assistant_text or "").strip()
        if cached:
            return self._last_assistant_text
        try:
            stream = self._chat_stream()
            for child in reversed(list(stream.children)):
                classes = getattr(child, "classes", ()) or ()
                if "chat-assistant" not in classes:
                    continue
                # Markdown widgets expose .source in recent Textual versions.
                source = getattr(child, "source", None)
                if isinstance(source, str) and source.strip():
                    self._last_assistant_text = source
                    return source
                # Static-like fallbacks.
                for attr in ("_renderable", "renderable"):
                    value = getattr(child, attr, None)
                    if isinstance(value, str) and value.strip():
                        self._last_assistant_text = value
                        return value
        except Exception:
            pass
        try:
            from harness.ui.final_answer import assistant_text_blocks

            for msg in reversed(self.history or []):
                if msg.get("role") != "assistant":
                    continue
                texts = assistant_text_blocks(msg.get("content"))
                joined = "\n\n".join(t for t in texts if (t or "").strip())
                if joined.strip():
                    self._last_assistant_text = joined
                    return joined
        except Exception:
            pass
        return ""

    def action_copy_selection(self) -> None:
        """Ctrl+C copies only an explicit composer or chat selection; shows text in status."""
        focused = self.focused
        if isinstance(focused, ComposerTextArea):
            selected = focused.selected_text or ""
            if selected:
                self.tui_set_status("已复制选区")
                return
        try:
            selected = self.screen.get_selected_text() or ""
        except Exception:
            selected = ""
        if selected:
            try:
                self.screen.clear_selection()
            except Exception:
                pass
            self.tui_set_status("已复制选区")
            return
        self.tui_set_status("请先选择文字 · Ctrl+Shift+Y 复制完整回答")

    def action_copy_last_answer(self) -> None:
        """Ctrl+Shift+C/Y: show latest assistant text in status."""
        text = self._latest_assistant_text()
        if not text:
            self.tui_set_status("还没有可复制的回答")
            return
        preview = text[:80].replace("\n", " ")
        self.tui_set_status(f"已复制: {preview}…")

    def action_paste_to_composer(self) -> None:
        """App-level Ctrl+V — focus composer and paste OS clipboard."""
        if self._picking or self._permission_request is not None:
            return
        try:
            composer = self._composer()
            composer.focus()
            composer.action_paste()
        except Exception:
            pass

    async def on_paste(self, event) -> None:
        """If Chat/scroll has focus, still route terminal paste into the composer."""
        if self._picking or self._permission_request is not None:
            return
        focused = self.focused
        if isinstance(focused, ComposerTextArea):
            return  # ComposerTextArea._on_paste handles it
        try:
            composer = self._composer()
            composer.focus()
            text = event.text or ""
            if not text:
                text = composer._clipboard_text()
            composer._insert_paste_text(text)
            event.stop()
            event.prevent_default()
        except Exception:
            pass

    def action_submit_or_stop(self) -> None:
        """Enter / Ctrl+Enter — send when idle, stop when busy."""
        if self._busy:
            self.action_interrupt()
            return
        self._submit_composer()

    def action_interrupt(self) -> None:
        if self._permission_request is not None:
            self._resolve_permission("cancel")
            self.tui_set_status("Permission cancelled")
            return
        if self._picking:
            self.close_inline_picker(notify=True, selected=None)
            self.tui_set_status("Picker closed")
            return
        if self._busy:
            request_cancel()
            self.tui_set_status("Stopping…")
            return
        if self._get_composer_text().strip():
            self._clear_composer()
        else:
            self.tui_set_status("Press Ctrl+Q to quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "interaction-allow":
            self._resolve_permission("allow")
            return
        if event.button.id == "interaction-deny":
            self._resolve_permission("deny")
            return
        if event.button.id == "interaction-cancel":
            self._resolve_permission("cancel")
            return

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """H1: grow composer height with line count (3–8 rows)."""
        if event.text_area.id != "user-input":
            return
        try:
            lines = max(1, event.text_area.document.line_count)
        except Exception:
            text = event.text_area.text or ""
            lines = max(1, text.count("\n") + 1)
        height = max(3, min(8, lines + (1 if lines < 8 else 0)))
        try:
            event.text_area.styles.height = height
        except Exception:
            pass
        self._refresh_command_hints(event.text_area.text or "")

    def _refresh_command_hints(self, text: str) -> None:
        commands = (
            "/model",
            "/mode",
            "/rag",
            "/resume",
            "/clear",
            "/skill",
            "/usage",
            "/help",
        )
        token = text.strip()
        matches = [command for command in commands if command.startswith(token)]
        show = token.startswith("/") and " " not in token and bool(matches)
        try:
            hint = self.query_one("#command-hints", Static)
            hint.display = show
            hint.update("  ".join(matches[:6]) if show else "")
        except Exception:
            pass

    def composer_history_previous(self) -> None:
        self._rebuild_input_history()
        if not self._input_history:
            return
        self._input_history_index = max(0, self._input_history_index - 1)
        self._set_composer_text(self._input_history[self._input_history_index])

    def composer_history_next(self) -> None:
        self._rebuild_input_history()
        if not self._input_history:
            return
        self._input_history_index = min(
            len(self._input_history), self._input_history_index + 1
        )
        text = (
            self._input_history[self._input_history_index]
            if self._input_history_index < len(self._input_history)
            else ""
        )
        self._set_composer_text(text)

    def _rebuild_input_history(self) -> list[str]:
        """Extract user queries from session message history."""
        queries: list[str] = []
        for msg in self.history or []:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                queries.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            queries.append(text)
                            break
        self._input_history = queries[-100:]
        self._input_history_index = len(self._input_history)
        return self._input_history

    def _submit_composer(self) -> None:
        query = (self._get_composer_text() or "").strip()
        if not query:
            return
        if self._busy:
            return
        if self._picking:
            self.close_inline_picker(notify=False)
        # Update index position so the next ↑ will skip the just-submitted query.
        self._rebuild_input_history()
        self._clear_composer()
        try:
            self._composer().styles.height = 3
        except Exception:
            pass
        from harness.ui.tui.commands import dispatch_slash

        if dispatch_slash(self, query):
            return
        self._run_turn(query)

    @work(thread=True, exclusive=True, group="agent")
    def _run_turn(self, query: str) -> None:
        if not self._worker_lock.acquire(blocking=False):
            BRIDGE.push_warn("Another turn is still running")
            return
        try:
            from harness.ui.tui.session import run_agent_turn

            result = run_agent_turn(self.history, self.context, query)
            self.context = result.get("context") or self.context
            redo = result.get("redo_query")
            if redo and isinstance(redo, str) and redo.strip():

                def _prefill() -> None:
                    try:
                        self._set_composer_text(redo)
                        lines = max(1, redo.count("\n") + 1)
                        self._composer().styles.height = max(3, min(8, lines + 1))
                        self._focus_composer()
                    except Exception:
                        pass

                self.call_from_thread(_prefill)
        except Exception as exc:
            from harness.providers.errors import format_api_error

            BRIDGE.trim_turn_bubbles()
            BRIDGE.push_error(format_api_error(exc))
            BRIDGE.set_busy(False)
        finally:
            try:
                self.call_from_thread(self.refresh_usage_bar)
            except Exception:
                pass
            self._worker_lock.release()
            if self._exit_when_idle:
                try:
                    self.call_from_thread(self.exit)
                except Exception:
                    pass

    @work(thread=True, exclusive=True, group="agent")
    def _run_rag_command(self, query: str) -> None:
        """Run potentially expensive /rag commands without freezing Textual."""
        if not self._worker_lock.acquire(blocking=False):
            BRIDGE.push_warn("Another turn is still running")
            return
        from harness.models import get_model, model_label

        BRIDGE.reset_turn(user_query=query, model=model_label(get_model()))
        BRIDGE.set_busy(True)
        BRIDGE.push_status("Running RAG command…")
        try:
            from harness.rag.commands import run_rag_cli_command

            result = run_rag_cli_command(query)
            BRIDGE.push_final(result)
            BRIDGE.seal_turn_bubbles()
            BRIDGE.push_status("Ready")
        except Exception as exc:
            from harness.providers.errors import format_api_error

            BRIDGE.push_error(format_api_error(exc))
        finally:
            BRIDGE.set_busy(False)
            self._worker_lock.release()
