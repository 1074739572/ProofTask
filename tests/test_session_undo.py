"""Tests for session undo and interrupt rollback."""

from __future__ import annotations

import unittest
from unittest import mock

from harness.project.session_undo import (
    abort_inflight_turn,
    is_user_turn,
    truncate_turn,
    undo_last_turn,
)


def _mock_binding() -> mock.Mock:
    """Return a minimal mock SessionBinding for tests that do not hit disk."""
    return mock.Mock(spec=["session_id", "session_jsonl"])


class TestSessionUndo(unittest.TestCase):
    def test_abort_rolls_back_user_and_partial_assistant(self) -> None:
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "why is ch07 wrong?"},
            {"role": "assistant", "content": [{"type": "tool_use", "name": "read_file"}]},
        ]
        turn_start = 2

        with mock.patch("harness.project.session_undo.replace_session"):
            message, rolled = abort_inflight_turn(messages, turn_start, binding=_mock_binding())

        self.assertEqual(len(messages), 2)
        self.assertEqual(rolled, "why is ch07 wrong?")
        self.assertIn("rolled back", message)
        self.assertIn("why is ch07 wrong", message)

    def test_abort_partial_only_when_no_user_at_turn_start(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "partial…"},
        ]
        with mock.patch("harness.project.session_undo.replace_session"):
            message, rolled = abort_inflight_turn(messages, turn_start=99, binding=_mock_binding())
        self.assertEqual(rolled, "hello")
        self.assertEqual(len(messages), 0)
        self.assertIn("rolled back", message)

    def test_truncate_keep_user(self) -> None:
        messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        removed = truncate_turn(messages, 0, keep_user=True)
        self.assertEqual(removed, 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "q")

    def test_is_user_turn_skips_injections(self) -> None:
        self.assertFalse(is_user_turn({"role": "user", "content": "[Compacted] summary"}))
        self.assertTrue(is_user_turn({"role": "user", "content": "real question"}))

    def test_undo_last_turn(self) -> None:
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "done"},
        ]
        with mock.patch("harness.project.session_undo.replace_session"):
            ok, msg = undo_last_turn(messages, binding=_mock_binding())
        self.assertTrue(ok)
        self.assertEqual(len(messages), 2)
        self.assertIn("second", msg)