"""Tests for session-scoped todos + registry (A) vs workflow state.json (B)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestSessionScopedTodos(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.project = self.root / ".project"
        self.project.mkdir()
        self._patches = [
            mock.patch("harness.settings.PROJECT_DIR", self.project),
            mock.patch("harness.project.session_registry.PROJECT_DIR", self.project),
            mock.patch(
                "harness.project.session_registry.SESSIONS_DIR",
                self.project / "sessions",
            ),
            mock.patch(
                "harness.project.session_registry.ACTIVE_SESSION_PATH",
                self.project / "active_session.json",
            ),
            mock.patch(
                "harness.project.session_registry.LEGACY_SESSION_PATH",
                self.project / "session.jsonl",
            ),
            mock.patch(
                "harness.project.session_registry.LEGACY_SESSION_META_PATH",
                self.project / "session.meta.json",
            ),
            mock.patch(
                "harness.project.session_registry.LEGACY_TODOS_PATH",
                self.project / "todos.json",
            ),
            mock.patch("harness.project.session_store.PROJECT_DIR", self.project),
            mock.patch(
                "harness.project.session_store.HISTORY_PATH",
                self.project / "history.json",
            ),
            mock.patch(
                "harness.project.session.HISTORY_PATH",
                self.project / "history.json",
            ),
        ]
        for p in self._patches:
            p.start()

        # Reset in-memory todos
        from harness.todos import state as todos_state

        todos_state._CURRENT = []
        todos_state.rounds_since_todo_update = 0

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def test_fresh_session_empty_todos(self) -> None:
        from harness.project.session_store import bootstrap_session
        from harness.todos.state import get_todos, load_todos_from_disk, set_todos

        with mock.patch.dict(os.environ, {"HARNESS_CONTINUE_SESSION": "0"}, clear=False):
            messages, binding, source = bootstrap_session()
        self.assertEqual(messages, [])
        self.assertIsNotNone(binding)
        self.assertIsNone(source)
        load_todos_from_disk(binding=binding)
        self.assertEqual(get_todos(), [])

        from harness.todos.state import set_binding
        set_binding(binding)
        set_todos(
            [{"content": "only in this session", "activeForm": "Doing…", "status": "in_progress"}]
        )

        self.assertTrue(binding.todos_json.exists())

    def test_new_session_does_not_load_old_todos(self) -> None:
        from harness.project.session_registry import create_session, session_paths
        from harness.todos.state import get_todos, load_todos_from_disk, set_binding, set_todos

        first = create_session(title="first")
        set_binding(first)
        set_todos(
            [{"content": "old work", "activeForm": "Working…", "status": "in_progress"}]
        )
        self.assertTrue(first.todos_json.exists())

        second = create_session(title="second")
        load_todos_from_disk(binding=second)
        self.assertEqual(get_todos(), [])
        self.assertFalse(second.todos_json.exists())
        # Old session still has its todos on disk
        self.assertTrue(first.todos_json.exists())
        self.assertNotEqual(first.session_id, second.session_id)

    def test_migrate_legacy_flat_files(self) -> None:
        from harness.project.session_registry import migrate_legacy_flat_session

        legacy_session = self.project / "session.jsonl"
        legacy_todos = self.project / "todos.json"
        legacy_session.write_text(
            json.dumps({"type": "message", "role": "user", "content": "hi"}) + "\n",
            encoding="utf-8",
        )
        legacy_todos.write_text(
            json.dumps(
                [{"content": "legacy", "activeForm": "…", "status": "pending"}]
            ),
            encoding="utf-8",
        )
        paths = migrate_legacy_flat_session()
        self.assertIsNotNone(paths)
        assert paths is not None
        self.assertTrue(paths.session_jsonl.exists())
        self.assertTrue(paths.todos_json.exists())
        self.assertFalse(legacy_session.exists())
        self.assertFalse(legacy_todos.exists())

    def test_clear_keeps_old_session_todos_on_disk(self) -> None:
        from harness.project.session_registry import create_session
        from harness.project.tools import run_project_clear
        from harness.todos.state import set_binding, set_todos

        old = create_session(title="with-todos")
        set_binding(old)
        set_todos(
            [{"content": "keep me", "activeForm": "Keeping…", "status": "pending"}]
        )
        old_todos = old.todos_json
        self.assertTrue(old_todos.exists())

        with mock.patch("harness.project.state.STATE_PATH", self.project / "state.json"):
            msg = run_project_clear(clear_project=False, binding=old)

        self.assertIn("保留", msg)
        self.assertTrue(old_todos.exists())
        data = json.loads(old_todos.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["content"], "keep me")

    def test_resume_by_index(self) -> None:
        from harness.project.session_registry import (
            create_session,
            resolve_session_selector,
            write_session_meta,
            read_session_meta,
        )
        from harness.project.session_store import append_checkpoint
        from harness.project.resume import switch_to_session

        a = create_session(title="first chat")
        meta = read_session_meta(a)
        meta["title"] = "first chat"
        write_session_meta(meta, a)
        append_checkpoint([{"role": "user", "content": "hello from first"}], binding=a)

        b = create_session(title="second chat")
        meta_b = read_session_meta(b)
        meta_b["title"] = "second chat"
        write_session_meta(meta_b, b)
        append_checkpoint([{"role": "user", "content": "hello from second"}], binding=b)

        # Prefer title resolve (order can tie on same-second timestamps)
        row, err = resolve_session_selector("first chat")
        self.assertIsNone(err)
        assert row is not None
        self.assertEqual(row["title"], "first chat")

        messages: list = [{"role": "user", "content": "stale"}]
        new_binding, note = switch_to_session(row["id"], messages, binding=b)
        self.assertIn("first chat", note)
        self.assertIn("最近一条（用户）", note)
        self.assertIn("hello from first", note)
        self.assertEqual(len(messages), 1)

        # Index resolve: find which N is first chat in visible list
        from harness.project.session_registry import visible_session_summaries

        visible = visible_session_summaries()
        index = next(i for i, r in enumerate(visible, 1) if r["title"] == "first chat")
        row2, err2 = resolve_session_selector(str(index))
        self.assertIsNone(err2)
        assert row2 is not None
        self.assertEqual(row2["id"], row["id"])

    def test_delete_session_by_index(self) -> None:
        from harness.project.session_registry import (
            create_session,
            resolve_session_selector,
            visible_session_summaries,
            write_session_meta,
            read_session_meta,
        )
        from harness.project.session_store import append_checkpoint
        from harness.project.resume import delete_session_entry

        a = create_session(title="keep me")
        meta = read_session_meta(a)
        meta["title"] = "keep me"
        write_session_meta(meta, a)
        append_checkpoint([{"role": "user", "content": "stay"}], binding=a)

        b = create_session(title="delete me")
        meta_b = read_session_meta(b)
        meta_b["title"] = "delete me"
        write_session_meta(meta_b, b)

        visible = visible_session_summaries()
        index = next(i for i, r in enumerate(visible, 1) if r["title"] == "delete me")
        row, err = resolve_session_selector(str(index))
        self.assertIsNone(err)
        assert row is not None

        note, _new_binding = delete_session_entry(row, messages=[], binding=b)
        self.assertIn("delete me", note)
        self.assertTrue(note.startswith("已删除"))
        self.assertFalse(b.paths.root.exists())
        self.assertTrue(a.paths.root.exists())

    def test_delete_active_session_starts_fresh(self) -> None:
        from harness.project.session_registry import create_session, read_active_session_id
        from harness.project.session_registry import resolve_session_selector
        from harness.project.resume import delete_session_entry

        current = create_session(title="current one")
        sid = read_active_session_id()
        assert sid
        row, _ = resolve_session_selector("1")
        assert row is not None
        messages = [{"role": "user", "content": "old"}]
        note, new_binding = delete_session_entry(row, messages, binding=current)
        self.assertIn("已删除当前会话", note)
        self.assertEqual(messages, [])
        self.assertNotEqual(read_active_session_id(), sid)

    def test_delete_workflow_state(self) -> None:
        from harness.project.state import ProjectState, save_state, STATE_PATH
        from harness.project.resume import delete_workflow_state

        with mock.patch("harness.project.state.STATE_PATH", self.project / "state.json"):
            with mock.patch("harness.project.state.PROJECT_DIR", self.project):
                save_state(ProjectState(title="研制报告改写"))
                note = delete_workflow_state()
        self.assertIn("已删除长任务", note)
        self.assertFalse((self.project / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
