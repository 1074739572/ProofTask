"""Smoke tests for Textual TUI (merged chat + usage/meta)."""

from __future__ import annotations

import os
import asyncio
import unittest
from unittest import mock


class PreferTuiTests(unittest.TestCase):
    def test_prefer_tui_default_on(self):
        from harness.ui.tui.mode import prefer_tui

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_TUI", None)
            self.assertTrue(prefer_tui())

    def test_prefer_tui_off_via_env(self):
        from harness.ui.tui.mode import prefer_tui

        for raw in ("0", "false", "off", "classic", "NO"):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"HARNESS_TUI": raw}):
                    self.assertFalse(prefer_tui())


class ChatHistoryTests(unittest.TestCase):
    def test_iter_history_user_tools_assistant(self):
        from harness.ui.tui.chat_history import iter_history_events

        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will read it"},
                    {
                        "type": "tool_use",
                        "id": "1",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "1", "content": "ok"},
                ],
            },
            {"role": "assistant", "content": "## Done\n\nAll good."},
        ]
        events = list(iter_history_events(messages))
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], "user")
        self.assertIn("step", kinds)
        self.assertEqual(kinds[-1], "assistant")
        self.assertTrue(any("read_file" in t for k, t in events if k == "step"))
        self.assertTrue(any("Done" in t for k, t in events if k == "assistant"))


class TuiImportTests(unittest.TestCase):
    def test_import_textual_app(self):
        from harness.ui.tui.app import HarnessApp
        from harness.ui.tui.bridge import BRIDGE, TuiBridge
        from harness.ui.tui.screens import AllowModal
        from harness.ui.tui.usage_bar import format_usage_bar

        self.assertTrue(issubclass(HarnessApp, object))
        self.assertIsInstance(BRIDGE, TuiBridge)
        self.assertTrue(issubclass(AllowModal, object))
        self.assertIn("今日", format_usage_bar())

    def test_renderer_routes_to_bridge_when_tui_active(self):
        from harness.ui.renderer import renderer
        from harness.ui.tui.bridge import BRIDGE
        from harness.ui.tui.events import ToolEvent
        from harness.ui.tui.mode import set_tui_active

        tools: list[ToolEvent] = []
        finals: list[str] = []
        errors: list[str] = []

        class FakeApp:
            def call_from_thread(self, fn, *args):
                fn(*args)

            def tui_tool_event(self, event: ToolEvent) -> None:
                tools.append(event)

            def tui_set_answer(self, text: str) -> None:
                finals.append(text)

            def tui_set_error(self, text: str) -> None:
                errors.append(text)

            def tui_set_status(self, text: str) -> None:
                pass

            def tui_set_busy(self, busy: bool) -> None:
                pass

            def tui_reset_turn(self, user_query: str = "", model: str = "") -> None:
                pass

            def refresh_usage_bar(self) -> None:
                pass

            def push_screen(self, screen, callback=None):
                if callback:
                    callback(False)

        set_tui_active(True)
        BRIDGE.bind(FakeApp())
        try:
            renderer.tool_start("bash", {"command": "echo hi"})
            renderer.assistant("# Hello\n\nworld")
            renderer.assistant("[Error] AuthenticationError: bad key")
            renderer.error("Connection failed")
            self.assertTrue(any(event.name == "bash" for event in tools))
            self.assertTrue(any("Hello" in f for f in finals))
            self.assertTrue(any("bad key" in e for e in errors))
            self.assertTrue(any("Connection failed" in e for e in errors))
            self.assertFalse(any("[Error]" in f for f in finals))
        finally:
            BRIDGE.unbind()
            set_tui_active(False)

    def test_composer_keeps_paste_binding(self):
        from textual.widgets import TextArea

        from harness.ui.tui.app import ComposerTextArea

        actions = {b.action for b in ComposerTextArea.BINDINGS}
        self.assertIn("composer_paste", actions)
        # Parent paste/copy still present via splat.
        self.assertTrue(any(b.action == "paste" for b in TextArea.BINDINGS))

    def test_slash_rag_routes_to_background_command(self):
        from harness.ui.tui.commands import dispatch_slash

        calls: list[str] = []

        class FakeApp:
            _busy = False

            def _run_rag_command(self, query: str) -> None:
                calls.append(query)

        self.assertTrue(dispatch_slash(FakeApp(), "/rag status"))
        self.assertEqual(calls, ["/rag status"])

    def test_test_directory_name_does_not_force_background(self):
        from harness.agent.background import is_slow_operation

        self.assertFalse(
            is_slow_operation(
                "bash",
                {"command": r"python scripts\read_sheet.py test\gaia_dataset.xlsx"},
            )
        )
        self.assertTrue(is_slow_operation("bash", {"command": "python -m pytest -q"}))

    def test_ask_allow_uses_bridge_in_tui(self):
        from harness.ui.permission_prompt import ask_allow
        from harness.ui.tui.bridge import BRIDGE
        from harness.ui.tui.events import PermissionResponse
        from harness.ui.tui.mode import set_tui_active

        class FakeApp:
            def call_from_thread(self, fn, *args):
                fn(*args)

            def tui_request_permission(self, request, callback):
                callback(PermissionResponse(request.request_id, "allow", request.detail))

        set_tui_active(True)
        BRIDGE.bind(FakeApp())
        try:
            self.assertTrue(ask_allow(detail="rm -rf tmp", title="Allow?"))
        finally:
            BRIDGE.unbind()
            set_tui_active(False)

    def test_slash_model_by_id(self):
        from harness.ui.tui.commands import dispatch_slash

        calls: dict = {"status": "", "meta": 0, "chat": []}

        class FakeApp:
            def chat_append(self, kind: str, text: str) -> None:
                calls["chat"].append((kind, text))

            def tui_set_status(self, text: str) -> None:
                calls["status"] = text

            def refresh_meta_bar(self) -> None:
                calls["meta"] += 1

            def refresh_model_header(self) -> None:
                calls["meta"] += 1

            def exit(self) -> None:
                pass

            def open_inline_picker(self, *a, **k):
                raise AssertionError("by-id should not open picker")

        with mock.patch("harness.models.set_model", return_value="Switched to x") as sm:
            with mock.patch("harness.models.model_label", return_value="x"):
                self.assertTrue(dispatch_slash(FakeApp(), "/model x"))
                sm.assert_called_once_with("x")
        self.assertIn("Switched", calls["status"])
        self.assertGreaterEqual(calls["meta"], 1)

    def test_slash_model_opens_inline_picker(self):
        from harness.ui.tui.commands import dispatch_slash

        opened: dict = {}

        class FakeApp:
            def tui_set_status(self, text: str) -> None:
                pass

            def chat_append(self, kind: str, text: str) -> None:
                pass

            def open_inline_picker(self, title, labels, item_ids, *, initial_index=0, on_pick=None):
                opened["title"] = title
                opened["ids"] = item_ids
                opened["on_pick"] = on_pick

        with mock.patch(
            "harness.ui.model_picker.menu_entries",
            return_value=(["A", "B"], ["a", "b"], 1),
        ):
            self.assertTrue(dispatch_slash(FakeApp(), "/model"))
        self.assertEqual(opened["title"], "Select model")
        self.assertEqual(opened["ids"], ["a", "b"])


class TuiResumeClearTests(unittest.TestCase):
    def test_resume_list_opens_picker(self):
        from harness.ui.tui.commands import dispatch_slash

        notes: list[str] = []
        opened: dict = {}

        class FakeApp:
            _busy = False
            history: list = []
            context: dict = {}

            def tui_set_status(self, text: str) -> None:
                pass

            def chat_append(self, kind: str, text: str) -> None:
                notes.append(text)

            def reload_session_view(self) -> None:
                raise AssertionError("list should not reload")

            def open_inline_picker(self, title, labels, item_ids, *, initial_index=0, on_pick=None):
                opened["title"] = title
                opened["ids"] = item_ids
                opened["on_pick"] = on_pick

        rows = [
            {
                "id": "s1",
                "title": "Alpha",
                "created_at": 1_700_000_000,
                "updated_at": 1_700_000_000,
                "active": True,
            },
            {
                "id": "s2",
                "title": "Beta",
                "created_at": 1_700_000_100,
                "updated_at": 1_700_000_100,
                "active": False,
            },
        ]
        with mock.patch(
            "harness.project.resume.format_resume_status",
            return_value="会话\n  1. Alpha",
        ):
            with mock.patch(
                "harness.project.session_registry.visible_session_summaries",
                return_value=rows,
            ):
                self.assertTrue(dispatch_slash(FakeApp(), "/resume"))
        self.assertTrue(any("Alpha" in n for n in notes))
        self.assertEqual(opened["title"], "Select session")
        self.assertEqual(opened["ids"], ["s1", "s2"])

    def test_resume_switch_reloads_chat(self):
        from harness.ui.tui.commands import dispatch_slash

        reloads = {"n": 0}
        notes: list[str] = []

        class FakeApp:
            _busy = False
            history = [{"role": "user", "content": "old"}]
            context: dict = {}

            def tui_set_status(self, text: str) -> None:
                pass

            def chat_append(self, kind: str, text: str) -> None:
                notes.append(text)

            def reload_session_view(self) -> None:
                reloads["n"] += 1

        with mock.patch(
            "harness.project.resume.run_resume_command",
            return_value="已切换到会话：Beta（2 条消息）",
        ) as rr:
            with mock.patch("harness.messages.repair.repair_tool_pairing"):
                with mock.patch(
                    "harness.context.update_context",
                    side_effect=lambda ctx, hist: {**ctx, "ok": True},
                ):
                    self.assertTrue(dispatch_slash(FakeApp(), "/resume 2"))
                    rr.assert_called_once()
                    self.assertEqual(rr.call_args.args[0], "2")
        self.assertEqual(reloads["n"], 1)
        self.assertTrue(any("已切换" in n for n in notes))

    def test_resume_blocked_while_busy(self):
        from harness.ui.tui.commands import dispatch_slash

        statuses: list[str] = []

        class FakeApp:
            _busy = True

            def tui_set_status(self, text: str) -> None:
                statuses.append(text)

            def chat_append(self, kind: str, text: str) -> None:
                raise AssertionError("should not append")

        self.assertTrue(dispatch_slash(FakeApp(), "/resume"))
        self.assertTrue(any("Stop" in s for s in statuses))

    def test_clear_reloads_empty_history(self):
        from harness.ui.tui.commands import dispatch_slash

        class FakeApp:
            _busy = False
            history = [{"role": "user", "content": "x"}]
            context: dict = {}
            notes: list[str] = []
            reloads = 0

            def tui_set_status(self, text: str) -> None:
                pass

            def chat_append(self, kind: str, text: str) -> None:
                self.notes.append(text)

            def reload_session_view(self) -> None:
                self.reloads += 1

        app = FakeApp()
        with mock.patch(
            "harness.project.tools.run_project_clear",
            return_value="已全新起步",
        ):
            with mock.patch("harness.messages.repair.repair_tool_pairing"):
                with mock.patch(
                    "harness.context.update_context",
                    side_effect=lambda ctx, hist: dict(ctx),
                ):
                    self.assertTrue(dispatch_slash(app, "/clear"))
        self.assertEqual(app.history, [])
        self.assertEqual(app.reloads, 1)
        self.assertTrue(any("全新" in n for n in app.notes))

    def test_resume_context_renders_as_system(self):
        from harness.ui.tui.chat_history import iter_history_events

        events = list(
            iter_history_events(
                [
                    {
                        "role": "user",
                        "content": "[Resume context]\n项目：demo",
                    }
                ]
            )
        )
        self.assertEqual(events, [("system", "[Resume context]\n项目：demo")])


class TuiShutdownSinkTests(unittest.TestCase):
    def test_shutdown_swallows_renderer_not_console(self):
        from harness.ui.renderer import renderer
        from harness.ui.tui.mode import begin_tui_shutdown, clear_tui_shutdown, set_tui_active

        set_tui_active(False)
        begin_tui_shutdown()
        try:
            with mock.patch("harness.ui.renderer._console") as console:
                renderer.tool_start("bash", {"command": "echo hi"})
                renderer.assistant("should not print")
                console.print.assert_not_called()
        finally:
            clear_tui_shutdown()

    def test_trim_helpers_exist_on_app(self):
        from harness.ui.tui.app import HarnessApp

        self.assertTrue(hasattr(HarnessApp, "tui_trim_turn_bubbles"))
        self.assertTrue(hasattr(HarnessApp, "tui_seal_turn_bubbles"))
        self.assertTrue(hasattr(HarnessApp, "action_quit_app"))
        self.assertTrue(hasattr(HarnessApp, "reload_session_view"))
        self.assertTrue(hasattr(HarnessApp, "action_copy_selection"))
        self.assertTrue(hasattr(HarnessApp, "action_copy_last_answer"))
        self.assertTrue(hasattr(HarnessApp, "action_submit_or_stop"))

    def test_bindings_copy_ctrl_c_and_ctrl_enter(self):
        from harness.ui.tui.app import ComposerTextArea, HarnessApp

        def _actions_for(bindings, needle: str) -> set[str]:
            found: set[str] = set()
            for binding in bindings:
                keys = {part.strip() for part in (binding.key or "").split(",")}
                if needle in keys:
                    found.add(binding.action)
            return found

        self.assertIn("copy_selection", _actions_for(HarnessApp.BINDINGS, "ctrl+c"))
        self.assertIn("copy_last_answer", _actions_for(HarnessApp.BINDINGS, "ctrl+shift+c"))
        self.assertIn("copy_last_answer", _actions_for(HarnessApp.BINDINGS, "ctrl+shift+y"))
        self.assertIn("submit_or_stop", _actions_for(HarnessApp.BINDINGS, "ctrl+enter"))
        self.assertIn("interrupt", _actions_for(HarnessApp.BINDINGS, "escape"))
        self.assertNotIn("interrupt", _actions_for(HarnessApp.BINDINGS, "ctrl+c"))
        self.assertNotIn("swallow_ctrl_c", _actions_for(HarnessApp.BINDINGS, "ctrl+c"))

        self.assertIn("composer_copy", _actions_for(ComposerTextArea.BINDINGS, "ctrl+c"))
        self.assertIn("composer_submit", _actions_for(ComposerTextArea.BINDINGS, "enter"))
        self.assertIn("composer_newline", _actions_for(ComposerTextArea.BINDINGS, "shift+enter"))
        # Default TextArea copy binding must be stripped (OS clipboard path only).
        self.assertNotIn("copy", _actions_for(ComposerTextArea.BINDINGS, "ctrl+c"))

    def test_composer_strips_leaked_terminal_mouse_reports(self):
        from harness.ui.tui.app import ComposerTextArea

        clean = ComposerTextArea._strip_terminal_mouse_reports
        self.assertEqual(clean("hello\x1b[<64;12;8Mworld"), "helloworld")
        self.assertEqual(clean("hello\x1b[Mabcworld"), "helloworld")
        self.assertEqual(clean("普通 [64;12;8M 文本"), "普通 [64;12;8M 文本")

    def test_copy_last_answer_shows_status(self):
        from harness.ui.tui.app import HarnessApp

        app = HarnessApp([], {})
        app._last_assistant_text = "HARNESS_TUI_COPY_STATUS_555"
        statuses: list[str] = []
        app.tui_set_status = statuses.append  # type: ignore[method-assign]
        app.action_copy_last_answer()
        self.assertTrue(any("已复制" in s for s in statuses))

    def test_copy_selection_uses_chat_selection(self):
        from harness.ui.tui.app import HarnessApp

        app = HarnessApp([], {})
        screen = mock.Mock()
        screen.get_selected_text.return_value = "selected fragment"
        statuses: list[str] = []
        app.tui_set_status = statuses.append  # type: ignore[method-assign]
        with mock.patch.object(type(app), "focused", new_callable=mock.PropertyMock, return_value=None), mock.patch.object(
            type(app), "screen", new_callable=mock.PropertyMock, return_value=screen
        ):
            app.action_copy_selection()
        self.assertTrue(any("已复制" in s for s in statuses))
        screen.clear_selection.assert_called_once_with()

    def test_copy_selection_without_selection_does_not_copy_answer(self):
        from harness.ui.tui.app import HarnessApp

        app = HarnessApp([], {})
        app._last_assistant_text = "whole answer must not be copied"
        screen = mock.Mock()
        screen.get_selected_text.return_value = None
        statuses: list[str] = []
        app.tui_set_status = statuses.append  # type: ignore[method-assign]
        with mock.patch.object(type(app), "focused", new_callable=mock.PropertyMock, return_value=None), mock.patch.object(
            type(app), "screen", new_callable=mock.PropertyMock, return_value=screen
        ):
            app.action_copy_selection()
        self.assertFalse(any("已复制" in s for s in statuses))
        self.assertTrue(any("选择文字" in status for status in statuses))

    def test_execution_trace_reuses_one_bounded_step_widget(self):
        from harness.ui.tui.widgets import ExecutionTraceCard

        trace = ExecutionTraceCard()
        mounted = []
        trace._mount_detail = mounted.append  # type: ignore[method-assign]
        widgets = [trace.add_step(f"step {index}") for index in range(100)]
        self.assertTrue(all(widget is widgets[0] for widget in widgets))
        self.assertEqual(mounted, [])
        self.assertEqual(len(trace._step_lines), 80)
        self.assertEqual(trace._step_lines[0], "step 20")


class TuiInlineInteractionTests(unittest.TestCase):
    def test_permission_tool_card_and_answer_stay_on_same_screen(self):
        from textual.containers import Vertical

        from harness.agent.cancel import clear_cancel
        from harness.ui.tui.app import HarnessApp
        from harness.ui.tui.events import PermissionRequest, ToolEvent
        from harness.ui.tui.widgets import ToolCard

        async def scenario():
            app = HarnessApp([], {})
            responses = []
            async with app.run_test(size=(120, 40)) as pilot:
                request = PermissionRequest(
                    "p1",
                    "Allow destructive command?",
                    "rm old.txt",
                    editable=True,
                )
                app.tui_request_permission(request, responses.append)
                await pilot.pause()
                self.assertTrue(app.query_one("#interaction-panel", Vertical).display)
                app.query_one("#interaction-input").value = "rm safe.txt"
                await pilot.click("#interaction-allow")
                await pilot.pause()
                self.assertEqual(responses[0].decision, "allow")
                self.assertEqual(responses[0].value, "rm safe.txt")
                self.assertFalse(app.query_one("#interaction-panel", Vertical).display)

                app.tui_tool_event(
                    ToolEvent("tool-1", "read_file", "a.py", "running")
                )
                app.tui_tool_event(
                    ToolEvent("tool-1", "read_file", "a.py", "ok", "Completed")
                )
                await pilot.pause()
                card = app.query_one(ToolCard)
                self.assertIn("✓", str(card.title))

                app.tui_set_answer("Done")
                await pilot.pause()
                stream = app.query_one("#chat-stream", Vertical)
                assistant_bubbles = [
                    child
                    for child in stream.children
                    if "chat-assistant" in getattr(child, "classes", [])
                ]
                self.assertTrue(assistant_bubbles)
                # Separate sticky answer dock is gone — Chat is the only answer surface.
                from textual.css.query import NoMatches

                with self.assertRaises(NoMatches):
                    app.query_one("#answer-dock")

        try:
            asyncio.run(scenario())
        finally:
            clear_cancel()


if __name__ == "__main__":
    unittest.main()
