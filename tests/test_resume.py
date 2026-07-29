"""Tests for OpenCode-style opt-in resume."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from harness.project.resume import (
    auto_resume_mode,
    inject_project_context,
    is_resume_injection,
    project_context_message,
    should_auto_inject_project_on_startup,
)
from harness.project.session_store import (
    bootstrap_from_transcript_enabled,
    bootstrap_session,
    continue_session_on_startup,
)
from harness.project.tools import run_project_clear


class TestResume(unittest.TestCase):
    def test_default_no_auto_project(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_AUTO_RESUME": "off"}, clear=False):
            self.assertFalse(should_auto_inject_project_on_startup())

    def test_legacy_auto_project_env(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_AUTO_RESUME": "project"}, clear=False):
            self.assertTrue(should_auto_inject_project_on_startup())

    def test_resume_injection_marker(self) -> None:
        self.assertTrue(
            is_resume_injection({"role": "user", "content": "[Resume context]\nfoo"})
        )
        self.assertFalse(is_resume_injection({"role": "user", "content": "hello"}))

    def test_inject_project_context(self) -> None:
        messages: list = []
        with mock.patch("harness.project.resume.load_state") as load_state:
            load_state.return_value = mock.Mock(
                title="Test",
                project_id="p1",
                chapters=[],
                current_chapter="01",
                output_dir="output",
                source_doc="files/x.docx",
            )
            with mock.patch("harness.project.resume.sync_chapters_from_disk", side_effect=lambda s: s):
                with mock.patch("harness.project.resume.append_checkpoint"):
                    ok, _ = inject_project_context(messages, binding=mock.Mock(), checkpoint=False)
        self.assertTrue(ok)
        self.assertEqual(len(messages), 1)
        self.assertIn("[Resume context]", messages[0]["content"])

    def test_project_message_none_without_state(self) -> None:
        with mock.patch("harness.project.resume.load_state", return_value=None):
            self.assertIsNone(project_context_message())


class TestOpenCodeBootstrap(unittest.TestCase):
    def test_default_no_continue_session(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_CONTINUE_SESSION": "0"}, clear=False):
            self.assertFalse(continue_session_on_startup())

    def test_continue_session_opt_in(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_CONTINUE_SESSION": "1"}, clear=False):
            self.assertTrue(continue_session_on_startup())

    def test_default_no_transcript_bootstrap(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_BOOTSTRAP_TRANSCRIPT": "0"}, clear=False):
            self.assertFalse(bootstrap_from_transcript_enabled())

    def test_bootstrap_session_default_empty(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HARNESS_CONTINUE_SESSION": "0", "HARNESS_BOOTSTRAP_TRANSCRIPT": "0"},
            clear=False,
        ):
            messages, binding, source = bootstrap_session()
        self.assertEqual(messages, [])
        self.assertIsNotNone(binding)
        self.assertIsNone(source)


class TestClear(unittest.TestCase):
    def test_clear_default_wipes_project(self) -> None:
        with mock.patch("harness.project.session_store.clear_session", return_value="archived.jsonl"):
            with mock.patch("harness.todos.state.clear_todos"):
                with mock.patch("harness.project.state.STATE_PATH") as path:
                    path.exists.return_value = True
                    msg = run_project_clear()
        self.assertIn("state.json", msg)
        self.assertIn("OpenCode", msg)

    def test_clear_session_keeps_project(self) -> None:
        with mock.patch("harness.project.session_store.clear_session", return_value="archived.jsonl"):
            with mock.patch("harness.todos.state.clear_todos"):
                with mock.patch("harness.project.state.STATE_PATH") as path:
                    path.exists.return_value = True
                    msg = run_project_clear(clear_project=False)
        self.assertIn("保留", msg)


if __name__ == "__main__":
    unittest.main()
