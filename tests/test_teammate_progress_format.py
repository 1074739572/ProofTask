from __future__ import annotations

import unittest
from unittest import mock


class TestTeammateProgressFormat(unittest.TestCase):
    def test_summarized_tool_progress_shape(self):
        from harness.ui.tool_display import summarize_tool_input

        summary = summarize_tool_input("read_file", {"path": "harness/ui/renderer.py", "limit": 20})
        self.assertIn("renderer.py", summary)
        self.assertNotIn("{'path'", summary)

    def test_new_progress_prefixes_are_structured(self):
        prefixes = ["started:", "thinking:", "reading:", "running:", "failed:", "done:"]
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertTrue(prefix.endswith(":"))


if __name__ == "__main__":
    unittest.main()
