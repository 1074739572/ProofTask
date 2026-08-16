"""Tests for dynamic session context assembly."""

from __future__ import annotations

import unittest
import unittest.mock

from harness.prompts import assemble_system_prompt
from harness.prompts.dynamic import build_session_context, default_time_granularity


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

    def test_runtime_context_is_wrapped_in_the_system_prompt(self) -> None:
        with (
            unittest.mock.patch(
                "harness.prompts.assemble_static_system_prompt",
                return_value="static system",
            ),
            unittest.mock.patch(
                "harness.prompts.build_session_context",
                return_value="Execution mode: GOAL",
            ),
        ):
            text = assemble_system_prompt({})
        self.assertEqual(
            text,
            "static system\n\n<runtime-context>\nExecution mode: GOAL\n</runtime-context>",
        )


if __name__ == "__main__":
    unittest.main()
