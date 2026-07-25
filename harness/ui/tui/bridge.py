"""Thread-safe bridge between agent_loop worker and Textual UI."""

from __future__ import annotations

from dataclasses import replace
import threading
import uuid
from typing import TYPE_CHECKING, Any, Callable

from harness.ui.tui.events import (
    BackgroundEvent,
    PermissionRequest,
    PermissionResponse,
    RuntimeMetrics,
    ToolEvent,
)

if TYPE_CHECKING:
    from textual.app import App


class TuiBridge:
    """Push UI events from worker threads; wait for same-page interactions."""

    def __init__(self) -> None:
        self.app: App | None = None
        self._lock = threading.Lock()
        self._metrics = RuntimeMetrics()

    def bind(self, app: App) -> None:
        self.app = app

    def unbind(self) -> None:
        self.app = None

    def _call(self, method: str, *args: Any) -> None:
        app = self.app
        if app is None:
            return
        fn = getattr(app, method, None)
        if fn is None:
            return
        try:
            app.call_from_thread(fn, *args)
        except Exception:
            pass

    def reset_turn(self, user_query: str = "", model: str = "") -> None:
        self._call("tui_reset_turn", user_query, model)

    def push_step(self, line: str) -> None:
        if not (line or "").strip():
            return
        self._call("tui_append_step", line)

    def push_final(self, text: str) -> None:
        if not (text or "").strip():
            return
        self._call("tui_set_answer", text)

    def push_error(self, text: str) -> None:
        """Surface an API / turn failure as a dedicated error panel + chat bubble."""
        if not (text or "").strip():
            return
        self._call("tui_set_error", text)

    def push_status(self, text: str) -> None:
        self._call("tui_set_status", text)

    def push_warn(self, text: str) -> None:
        self._call("tui_append_step", f"⚠ {text}")

    def set_busy(self, busy: bool) -> None:
        self._call("tui_set_busy", busy)

    def refresh_usage(self) -> None:
        self._call("refresh_usage_bar")

    def push_tool(self, event: ToolEvent) -> None:
        self._call("tui_tool_event", event)

    def push_background(self, event: BackgroundEvent) -> None:
        self._call("tui_background_event", event)

    def push_metrics(self, metrics: RuntimeMetrics) -> None:
        self._metrics = metrics
        self._call("tui_runtime_metrics", metrics)

    def push_cache_usage(self, hit_tokens: int, miss_tokens: int) -> None:
        self.push_metrics(
            replace(
                self._metrics,
                cache_hit_tokens=int(hit_tokens),
                cache_miss_tokens=int(miss_tokens),
            )
        )

    def push_context_usage(self, tokens: int, window: int) -> None:
        self.push_metrics(
            replace(
                self._metrics,
                context_tokens=int(tokens),
                context_window=int(window),
            )
        )

    def trim_turn_bubbles(self) -> None:
        """U1: remove in-flight turn widgets from Chat."""
        self._call("tui_trim_turn_bubbles")

    def seal_turn_bubbles(self) -> None:
        """Mark live turn bubbles as permanent history."""
        self._call("tui_seal_turn_bubbles")

    def ask_permission(
        self,
        detail: str,
        *,
        title: str = "Allow destructive command?",
        editable: bool = False,
    ) -> PermissionResponse:
        """Block the worker while an inline permission card is active."""
        app = self.app
        request = PermissionRequest(
            request_id=f"permission-{uuid.uuid4().hex}",
            title=title,
            detail=detail,
            editable=editable,
            placeholder="Edit command before allowing" if editable else "",
        )
        if app is None:
            return PermissionResponse(request.request_id, "deny", detail)

        done = threading.Event()
        box: dict[str, PermissionResponse] = {
            "value": PermissionResponse(request.request_id, "cancel", detail)
        }

        def _on_dismiss(value: PermissionResponse) -> None:
            box["value"] = value
            done.set()

        def _show() -> None:
            show = getattr(app, "tui_request_permission", None)
            if show is None:
                _on_dismiss(PermissionResponse(request.request_id, "deny", detail))
                return
            show(request, _on_dismiss)

        try:
            app.call_from_thread(_show)
        except Exception:
            return PermissionResponse(request.request_id, "deny", detail)
        done.wait(timeout=600)
        return box["value"]

    def ask_allow(self, detail: str, *, title: str = "Allow destructive command?") -> bool | None:
        """Compatibility wrapper for callers that only need y/n/cancel."""
        response = self.ask_permission(detail, title=title)
        if response.decision == "cancel":
            return None
        return response.allowed

    def ask_doc_multi_select(self, rows: list[dict]) -> list[str] | None:
        """Block the worker while the inline multi-select doc picker is open.

        Returns selected source names, empty list (= search all), or None if cancelled.
        """
        app = self.app
        if app is None:
            return None

        done = threading.Event()
        box: dict[str, list[str] | None] = {"value": None}

        def _on_done(selected: list[str] | None) -> None:
            box["value"] = selected
            done.set()

        def _show() -> None:
            show = getattr(app, "tui_open_doc_multi_picker", None)
            if show is None:
                _on_done(None)
                return
            show(rows, _on_done)

        try:
            app.call_from_thread(_show)
        except Exception:
            return None
        done.wait(timeout=600)
        return box["value"]


BRIDGE = TuiBridge()
