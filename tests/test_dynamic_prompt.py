"""Tests for dynamic session context assembly."""

from __future__ import annotations

import unittest
import unittest.mock

from harness.prompts import assemble_system_prompt
from harness.prompts.dynamic import (
    build_session_context,
    build_stable_session_context,
    default_time_granularity,
)


class TestDynamicPrompt(unittest.TestCase):
    def test_default_time_granularity_is_minute(self) -> None:
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(default_time_granularity(), "minute")

    def test_time_granularity_env_seconds(self) -> None:
        with unittest.mock.patch.dict("os.environ", {"HARNESS_TIME_GRANULARITY": "seconds"}):
            self.assertEqual(default_time_granularity(), "seconds")

    def test_latest_user_query_is_not_repeated_in_runtime_context(self) -> None:
        text = build_session_context(
            {"latest_user_query": "write an implementation plan"},
            include_time=False,
            include_model=False,
            include_mode=False,
            include_memories=False,
            include_mcp=False,
            include_teammates=False,
            include_todos=False,
            include_project_instructions=False,
            include_platform=False,
        )
        self.assertNotIn("write an implementation plan", text)

    def test_stable_system_context_excludes_per_round_operational_state(self) -> None:
        with unittest.mock.patch(
            "harness.prompts.dynamic.build_session_context",
            return_value="stable",
        ) as build:
            self.assertEqual(build_stable_session_context({}), "stable")
        kwargs = build.call_args.kwargs
        self.assertFalse(kwargs["include_time"])
        self.assertFalse(kwargs["include_model"])
        self.assertFalse(kwargs["include_mcp"])
        self.assertFalse(kwargs["include_teammates"])
        self.assertFalse(kwargs["include_todos"])

    def test_system_prompt_excludes_volatile_runtime_context(self) -> None:
        with (
            unittest.mock.patch(
                "harness.prompts.assemble_static_system_prompt",
                return_value="static system",
            ),
            unittest.mock.patch(
                "harness.prompts.build_stable_session_context",
                return_value="",
            ),
        ):
            text = assemble_system_prompt({"latest_user_query": "unrelated"})
        self.assertEqual(text, "static system")

    def test_project_instruction_snapshot_is_part_of_the_stable_system(self) -> None:
        context = {
            "project_instructions": "Run focused tests.",
            "project_instructions_source": "AGENTS.md",
        }
        with unittest.mock.patch(
            "harness.prompts.assemble_static_system_prompt",
            return_value="static system",
        ), unittest.mock.patch(
            "harness.prompts.build_stable_session_context",
            return_value="System environment: Windows.",
        ):
            text = assemble_system_prompt(context)
        self.assertIn("static system", text)
        self.assertIn("Run focused tests.", text)
        self.assertIn("System environment: Windows.", text)


if __name__ == "__main__":
    unittest.main()
