"""Tests for compact: safe input budget + prompt-too-long detection."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from harness.agent.compact import (
    COMPACT_TAIL_COUNT,
    LATEST_USER_FOCUS_MARKER,
    _safe_input_budget,
    _structured_summary_instruction,
    autocompact_threshold_tokens,
    compact_history,
    estimate_tokens,
    find_latest_user_text,
    micro_compact,
    prepare_context,
    model_context_window,
    reactive_compact,
    should_autocompact,
    stabilize_tool_output,
    stabilize_tool_results,
    summarize_history,
)
from harness.agent.recovery import is_prompt_too_long_error


class TestSafeInputBudget(unittest.TestCase):
    def test_budget_under_deepseek_limit(self) -> None:
        budget = _safe_input_budget("deepseek-v4-flash")
        # 1M context -> ~250K chars, well above the old 12K floor
        self.assertGreater(budget, 100_000)

    def test_qwen_max_small_context(self) -> None:
        budget = _safe_input_budget("qwen-max")
        # 32K context, 12K reserve -> 20K tokens -> 80K chars
        self.assertGreater(budget, 50_000)
        self.assertLess(budget, 100_000)

    def test_unknown_model_falls_back(self) -> None:
        budget = _safe_input_budget("unknown-model-xyz")
        self.assertGreaterEqual(budget, 12_000)


class TestAutocompactThreshold(unittest.TestCase):
    """Claude Code–style: tokens ≳ 0.835 × context_window."""

    def tearDown(self) -> None:
        for key in (
            "HARNESS_CONTEXT_LIMIT",
            "HARNESS_CONTEXT_WINDOW",
            "HARNESS_AUTOCOMPACT_PCT",
        ):
            unittest.mock.patch.dict("os.environ", {key: ""})  # no-op safety
            import os

            os.environ.pop(key, None)

    def test_default_pct_of_window(self) -> None:
        import os

        os.environ.pop("HARNESS_CONTEXT_LIMIT", None)
        os.environ.pop("HARNESS_AUTOCOMPACT_PCT", None)
        os.environ["HARNESS_CONTEXT_WINDOW"] = "200000"
        try:
            threshold = autocompact_threshold_tokens("any")
            self.assertEqual(threshold, int(200_000 * 0.835))
        finally:
            os.environ.pop("HARNESS_CONTEXT_WINDOW", None)

    def test_pct_override_as_percent(self) -> None:
        import os

        os.environ.pop("HARNESS_CONTEXT_LIMIT", None)
        os.environ["HARNESS_CONTEXT_WINDOW"] = "100000"
        os.environ["HARNESS_AUTOCOMPACT_PCT"] = "75"
        try:
            self.assertEqual(autocompact_threshold_tokens("any"), 75_000)
        finally:
            os.environ.pop("HARNESS_CONTEXT_WINDOW", None)
            os.environ.pop("HARNESS_AUTOCOMPACT_PCT", None)

    def test_absolute_limit_overrides_pct(self) -> None:
        import os

        os.environ["HARNESS_CONTEXT_WINDOW"] = "1000000"
        os.environ["HARNESS_CONTEXT_LIMIT"] = "12000"
        try:
            self.assertEqual(autocompact_threshold_tokens("any"), 12_000)
        finally:
            os.environ.pop("HARNESS_CONTEXT_WINDOW", None)
            os.environ.pop("HARNESS_CONTEXT_LIMIT", None)

    def test_should_autocompact_uses_tokens_not_old_50k_chars(self) -> None:
        import os

        # ~60KB of content would have triggered the old 50KB char limit.
        # With 1M × 0.835 it must NOT compact yet.
        os.environ.pop("HARNESS_CONTEXT_LIMIT", None)
        os.environ["HARNESS_CONTEXT_WINDOW"] = "1000000"
        try:
            blob = "x" * 60_000
            messages = [{"role": "user", "content": blob}]
            self.assertGreater(estimate_tokens(messages), 10_000)
            self.assertLess(estimate_tokens(messages), autocompact_threshold_tokens())
            self.assertFalse(should_autocompact(messages))
        finally:
            os.environ.pop("HARNESS_CONTEXT_WINDOW", None)

    def test_model_window_qwen_max(self) -> None:
        import os

        os.environ.pop("HARNESS_CONTEXT_WINDOW", None)
        self.assertEqual(model_context_window("qwen-max"), 32_000)



class TestSummarizeHistoryFallback(unittest.TestCase):
    def test_returns_fallback_on_api_error(self) -> None:
        import harness.agent.compact.summarize as summarize_mod

        class Boom(Exception):
            pass

        def boom(**kwargs):
            raise Boom("Range of input length should be [1, 30720]")

        with unittest.mock.patch.object(summarize_mod, "create_message", side_effect=boom):
            with unittest.mock.patch.object(summarize_mod, "get_model", return_value="m1"):
                result = summarize_history([{"role": "user", "content": "hi"}])
        self.assertIn("Compact summary unavailable", result)

    def test_structured_summary_prompt_sections(self) -> None:
        text = _structured_summary_instruction()
        for heading in (
            "## Goal",
            "## User Constraints",
            "## Changed Files",
            "## Key Findings",
            "## Sources Tried",
            "## Facts Gathered",
            "## Remaining Work",
            "## Do NOT Forget",
        ):
            self.assertIn(heading, text)
        self.assertIn("latest real user instruction", text)
        self.assertIn("latest user message wins", text)


class TestCompactHistoryTail(unittest.TestCase):
    def test_compact_history_keeps_recent_tail(self) -> None:
        import harness.agent.compact.pipeline as pipeline

        messages = [{"role": "user", "content": f"msg-{index}"} for index in range(12)]
        messages.append({"role": "assistant", "content": "latest assistant"})
        messages.append({"role": "user", "content": "latest user"})

        with unittest.mock.patch.object(pipeline, "write_transcript", return_value=Path("t.jsonl")):
            with unittest.mock.patch.object(pipeline, "summarize_history", return_value="SUMMARY"):
                with unittest.mock.patch(
                    "harness.project.session_store.record_compact_boundary"
                ):
                    result = compact_history(messages)

        self.assertEqual(result[0]["content"], "[Compacted]\n\nSUMMARY")
        self.assertEqual(len(result), 1 + COMPACT_TAIL_COUNT)
        self.assertIn(LATEST_USER_FOCUS_MARKER, result[-1]["content"])
        self.assertIn("latest user", result[-1]["content"])
        self.assertIn("overrides any conflicting", result[-1]["content"])
        self.assertEqual(result[-2]["content"], "latest assistant")

    def test_compact_focus_outranks_stale_summary_goal(self) -> None:
        import harness.agent.compact.pipeline as pipeline

        messages = [
            {"role": "user", "content": "verify bug 001 and 002"},
            {"role": "assistant", "content": "checking chapters"},
            {"role": "user", "content": "写 CWRF 实施方案，不要验 ch07"},
        ]
        stale = "## Goal\nVerify bug 001/002 wiring\n"

        with unittest.mock.patch.object(pipeline, "write_transcript", return_value=Path("t.jsonl")):
            with unittest.mock.patch.object(pipeline, "summarize_history", return_value=stale):
                with unittest.mock.patch(
                    "harness.project.session_store.record_compact_boundary"
                ):
                    result = compact_history(messages)

        self.assertIn("Verify bug 001/002", result[0]["content"])
        self.assertEqual(result[-1]["role"], "user")
        self.assertIn(LATEST_USER_FOCUS_MARKER, result[-1]["content"])
        self.assertIn("写 CWRF 实施方案", result[-1]["content"])
        self.assertNotIn(LATEST_USER_FOCUS_MARKER, result[0]["content"])

    def test_reactive_compact_also_focuses_latest_user(self) -> None:
        import harness.agent.compact.pipeline as pipeline

        messages = [
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "new task only"},
        ]
        with unittest.mock.patch.object(pipeline, "write_transcript", return_value=Path("t.jsonl")):
            with unittest.mock.patch.object(pipeline, "summarize_history", return_value="OLD GOAL"):
                with unittest.mock.patch(
                    "harness.project.session_store.record_compact_boundary"
                ):
                    result = reactive_compact(messages)
        self.assertTrue(result[0]["content"].startswith("[Reactive compact]"))
        self.assertIn("new task only", result[-1]["content"])
        self.assertIn(LATEST_USER_FOCUS_MARKER, result[-1]["content"])

    def test_find_latest_user_skips_tool_and_compact_markers(self) -> None:
        messages = [
            {"role": "user", "content": "real ask"},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}],
            },
            {"role": "user", "content": "[Compacted]\n\nold summary"},
        ]
        self.assertEqual(find_latest_user_text(messages), "real ask")

    def test_find_latest_user_skips_reminder_and_harness_injections(self) -> None:
        messages = [
            {"role": "user", "content": "real ask"},
            {"role": "user", "content": "<reminder>\nupdate todos\n</reminder>"},
            {"role": "user", "content": "[Harness] continue the current goal"},
        ]
        self.assertEqual(find_latest_user_text(messages), "real ask")


class TestMicroCompactPersist(unittest.TestCase):
    def test_micro_compact_persists_instead_of_hard_delete(self) -> None:
        import harness.agent.compact.persist as persist_mod

        long_text = "x" * 500
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-old", "content": long_text},
                    {"type": "tool_result", "tool_use_id": "tool-mid", "content": long_text},
                    {"type": "tool_result", "tool_use_id": "tool-new", "content": long_text},
                    {"type": "tool_result", "tool_use_id": "tool-latest", "content": long_text},
                ],
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tool_dir = Path(tmp) / "tool_results"
            with unittest.mock.patch.object(persist_mod, "TOOL_RESULTS_DIR", tool_dir):
                result = micro_compact(messages)

            old_block = result[0]["content"][0]
            latest_block = result[0]["content"][-1]
            self.assertIn("<persisted-output compacted>", old_block["content"])
            self.assertIn("tool-old.txt", old_block["content"])
            self.assertEqual(latest_block["content"], long_text)
            self.assertTrue((tool_dir / "tool-old.txt").exists())
            self.assertFalse((tool_dir / "tool-latest.txt").exists())

    def test_micro_compact_skips_already_persisted(self) -> None:
        import harness.agent.compact.persist as persist_mod

        wrapped = (
            "<persisted-output compacted>\nFull output: /tmp/x.txt\n"
            "Preview:\nhello\n</persisted-output>"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t-old", "content": wrapped},
                    {"type": "tool_result", "tool_use_id": "t-mid", "content": "y" * 500},
                    {"type": "tool_result", "tool_use_id": "t-new", "content": "z" * 500},
                    {"type": "tool_result", "tool_use_id": "t-latest", "content": "w" * 500},
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tool_dir = Path(tmp) / "tool_results"
            with unittest.mock.patch.object(persist_mod, "TOOL_RESULTS_DIR", tool_dir):
                result = micro_compact(messages)
        old_block = result[0]["content"][0]["content"]
        self.assertEqual(old_block, wrapped)
        self.assertNotIn("<persisted-output compacted>\nFull output:", old_block.replace(wrapped, ""))


class TestStableToolResultIngress(unittest.TestCase):
    def test_shared_user_content_builder_applies_ingress_policy(self) -> None:
        import harness.agent.background as background_mod
        import harness.agent.compact.persist as persist_mod

        results = [
            {
                "type": "tool_result",
                "tool_use_id": "tool-shared-ingress",
                "content": "x" * 2_000,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tool_dir = Path(tmp) / "tool_results"
            with unittest.mock.patch.object(persist_mod, "TOOL_RESULTS_DIR", tool_dir):
                with unittest.mock.patch.object(
                    background_mod, "collect_background_results", return_value=[]
                ):
                    with unittest.mock.patch.dict(
                        os.environ, {"HARNESS_TOOL_RESULT_MAX_CHARS": "1000"}
                    ):
                        content = background_mod.build_user_content(results)

            self.assertIn("<persisted-output truncated", content[0]["content"])
            self.assertTrue((tool_dir / "tool-shared-ingress.txt").exists())
            self.assertEqual(results[0]["content"], "x" * 2_000)

    def test_large_output_is_persisted_before_history_ingress(self) -> None:
        import harness.agent.compact.persist as persist_mod

        output = "HEAD:" + ("x" * 4_000) + ":TAIL"
        with tempfile.TemporaryDirectory() as tmp:
            tool_dir = Path(tmp) / "tool_results"
            with unittest.mock.patch.object(persist_mod, "TOOL_RESULTS_DIR", tool_dir):
                normalized = stabilize_tool_output(
                    "tool-large",
                    output,
                    tool_name="read_file",
                    max_chars=1_000,
                )

            self.assertLessEqual(len(normalized), 1_000)
            self.assertIn("HEAD:", normalized)
            self.assertIn(":TAIL", normalized)
            self.assertIn("use read_file for full output", normalized)
            self.assertEqual((tool_dir / "tool-large.txt").read_text("utf-8"), output)

    def test_parallel_round_shares_a_total_budget(self) -> None:
        import harness.agent.compact.persist as persist_mod

        results = [
            {
                "type": "tool_result",
                "tool_use_id": f"tool-{index}",
                "content": str(index) * 5_000,
            }
            for index in range(3)
        ]
        env = {
            "HARNESS_TOOL_RESULT_MAX_CHARS": "10000",
            "HARNESS_TOOL_ROUND_MAX_CHARS": "3000",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                persist_mod, "TOOL_RESULTS_DIR", Path(tmp) / "tool_results"
            ):
                with unittest.mock.patch.dict(os.environ, env):
                    normalized = stabilize_tool_results(results)

        lengths = [len(block["content"]) for block in normalized]
        self.assertTrue(all(length <= 1_000 for length in lengths))
        self.assertLessEqual(sum(lengths), 3_000)
        self.assertEqual(results[0]["content"], "0" * 5_000)

    def test_unsafe_tool_id_cannot_escape_output_directory(self) -> None:
        import harness.agent.compact.persist as persist_mod

        with tempfile.TemporaryDirectory() as tmp:
            tool_dir = Path(tmp) / "tool_results"
            with unittest.mock.patch.object(persist_mod, "TOOL_RESULTS_DIR", tool_dir):
                normalized = stabilize_tool_output(
                    "../outside",
                    "x" * 2_000,
                    max_chars=1_000,
                )
            files = list(tool_dir.glob("*.txt"))

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, tool_dir)
        self.assertIn(str(files[0]), normalized)


class TestAppendOnlyContextPreparation(unittest.TestCase):
    def _without_compaction(self):
        return unittest.mock.patch.dict(
            os.environ,
            {
                "HARNESS_CONTEXT_WINDOW": "1000000",
                "HARNESS_AUTOCOMPACT_PCT": "99",
                "HARNESS_CONTEXT_LIMIT": "",
            },
        )

    def test_prepare_context_does_not_rewrite_sent_history(self) -> None:
        messages = [
            {"role": "user", "content": "inspect the repository"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 5_000}
                ],
            },
        ]
        before = copy.deepcopy(messages)
        with self._without_compaction():
            prepared = prepare_context(messages)

        self.assertIs(prepared, messages)
        self.assertEqual(messages, before)

    def test_appending_round_preserves_previous_serialized_prefix(self) -> None:
        messages = [{"role": "user", "content": "start"}]
        with self._without_compaction():
            prepare_context(messages)
            previous = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            previous_snapshot = copy.deepcopy(messages)
            messages.extend(
                [
                    {"role": "assistant", "content": "next"},
                    {"role": "user", "content": "continue"},
                ]
            )
            prepare_context(messages)

        self.assertEqual(messages[: len(previous_snapshot)], previous_snapshot)
        current_prefix = json.dumps(
            messages[: len(previous_snapshot)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertEqual(current_prefix, previous)


class TestPromptTooLong(unittest.TestCase):
    def test_deepseek_range_error_detected(self) -> None:
        exc = Exception(
            "BadRequestError: 400 - Range of input length should be [1, 30720]"
        )
        self.assertTrue(is_prompt_too_long_error(exc))

    def test_anthropic_context_length_detected(self) -> None:
        self.assertTrue(is_prompt_too_long_error(Exception("context_length_exceeded")))
        self.assertTrue(is_prompt_too_long_error(Exception("prompt is too long")))

    def test_unrelated_error_not_matched(self) -> None:
        self.assertFalse(is_prompt_too_long_error(Exception("network timeout")))


class TestSummaryThinkingFallback(unittest.TestCase):
    def test_extract_falls_back_to_thinking_block(self) -> None:
        from harness.agent.compact.summarize import extract_summary_text

        content = [
            {"type": "thinking", "thinking": "## Goal\nFind paper X\n"},
            {"type": "text", "text": ""},
        ]
        self.assertIn("## Goal", extract_summary_text(content))

    def test_text_block_preferred_over_thinking(self) -> None:
        from harness.agent.compact.summarize import extract_summary_text

        content = [
            {"type": "thinking", "thinking": "internal only"},
            {"type": "text", "text": "## Goal\nVisible summary"},
        ]
        self.assertEqual(extract_summary_text(content), "## Goal\nVisible summary")

    def test_empty_thinking_only_marks_unusable(self) -> None:
        import harness.agent.compact.summarize as summarize_mod
        from harness.agent.compact.summarize import SUMMARY_UNUSABLE

        class FakeResp:
            content = [{"type": "thinking", "thinking": ""}]

        with unittest.mock.patch.object(
            summarize_mod, "create_message", return_value=FakeResp()
        ):
            with unittest.mock.patch.object(summarize_mod, "get_model", return_value="m1"):
                result = summarize_history([{"role": "user", "content": "hi"}])
        self.assertEqual(result, SUMMARY_UNUSABLE)

    def test_compact_history_degrades_on_empty_summary(self) -> None:
        import harness.agent.compact.pipeline as pipeline
        from harness.agent.compact.summarize import SUMMARY_UNUSABLE

        messages = [{"role": "user", "content": f"msg-{i}"} for i in range(12)]
        messages.append({"role": "user", "content": "latest user question"})

        with unittest.mock.patch.object(pipeline, "write_transcript", return_value=Path("t.jsonl")):
            with unittest.mock.patch.object(
                pipeline, "summarize_history", return_value=SUMMARY_UNUSABLE
            ):
                with unittest.mock.patch(
                    "harness.project.session_store.record_compact_boundary"
                ):
                    result = compact_history(messages)

        head = result[0]["content"]
        self.assertIn("summary unavailable", head.lower())
        self.assertNotIn("(empty summary)", head)
        self.assertIn(LATEST_USER_FOCUS_MARKER, result[-1]["content"])


if __name__ == "__main__":
    unittest.main()
