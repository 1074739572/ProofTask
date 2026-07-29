"""Tests for manual /skill injection."""

from __future__ import annotations

import unittest
from unittest import mock


class SkillInjectTests(unittest.TestCase):
    def test_inject_skill_appends_marked_user_message(self) -> None:
        from harness.skills_loader import (
            SKILL_LOADED_PREFIX,
            inject_skill,
            skill_loaded_notice,
        )

        messages: list = []
        with mock.patch(
            "harness.skills_loader.SKILL_REGISTRY",
            {
                "demo": {
                    "name": "demo",
                    "description": "Demo skill",
                    "content": "---\nname: demo\n---\nDo the demo.",
                }
            },
        ):
            with mock.patch("harness.skills_loader.scan_skills"):
                with mock.patch(
                    "harness.project.resume.checkpoint_history"
                ) as cp:
                    ok, note = inject_skill("demo", messages, checkpoint=True)
        self.assertTrue(ok)
        self.assertEqual(note, skill_loaded_notice("demo"))
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["content"].startswith(f"{SKILL_LOADED_PREFIX} demo]"))
        self.assertIn("Do the demo.", messages[0]["content"])
        cp.assert_called_once_with(messages, binding=None)

    def test_inject_unknown_skill(self) -> None:
        from harness.skills_loader import inject_skill

        messages: list = []
        with mock.patch("harness.skills_loader.SKILL_REGISTRY", {}):
            with mock.patch("harness.skills_loader.scan_skills"):
                ok, note = inject_skill("nope", messages, checkpoint=False)
        self.assertFalse(ok)
        self.assertIn("not found", note.lower())
        self.assertEqual(messages, [])

    def test_run_skill_list_without_messages(self) -> None:
        from harness.skills_loader import run_skill_command

        with mock.patch(
            "harness.skills_loader.format_skill_command_status",
            return_value="Skills\n  - a",
        ):
            self.assertIn("Skills", run_skill_command(""))

    def test_undo_skips_skill_injection(self) -> None:
        from harness.project.session_undo import is_user_turn

        self.assertFalse(
            is_user_turn(
                {
                    "role": "user",
                    "content": "[Skill loaded: demo]\nbody",
                }
            )
        )
        self.assertTrue(is_user_turn({"role": "user", "content": "real question"}))


if __name__ == "__main__":
    unittest.main()
