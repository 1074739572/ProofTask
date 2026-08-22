"""Tests for usage cache parsing, ledger, and reports."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness.usage import parse_cache_usage
from harness.usage.parse import CacheUsage
from harness.usage.report import (
    bar,
    format_tokens,
    format_usage_report,
    format_usage_welcome_line,
    parse_usage_arg,
    sparkline,
)
from harness.usage import store as usage_store


class TestParseCacheUsage(unittest.TestCase):
    def test_deepseek_cache_read(self) -> None:
        usage = SimpleNamespace(
            input_tokens=76,
            output_tokens=5,
            cache_read_input_tokens=3200,
            cache_creation_input_tokens=0,
        )
        parsed = parse_cache_usage(usage)
        assert parsed is not None
        self.assertEqual(parsed.hit_tokens, 3200)
        self.assertEqual(parsed.miss_tokens, 76)

    def test_prompt_cache_hit_fields(self) -> None:
        usage = SimpleNamespace(
            prompt_cache_hit_tokens=1000,
            prompt_cache_miss_tokens=200,
            output_tokens=10,
        )
        parsed = parse_cache_usage(usage)
        assert parsed is not None
        self.assertEqual(parsed.hit_tokens, 1000)
        self.assertEqual(parsed.miss_tokens, 200)

    def test_cached_openai_usage_reads_completion_tokens(self) -> None:
        usage = SimpleNamespace(
            prompt_cache_hit_tokens=1000,
            prompt_cache_miss_tokens=200,
            completion_tokens=37,
        )

        parsed = parse_cache_usage(usage)

        assert parsed is not None
        self.assertEqual(parsed.output_tokens, 37)


class TestUsageStoreAndReport(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.usage_dir = Path(self._tmp.name) / "usage"
        self.patches = [
            mock.patch.object(usage_store, "USAGE_DIR", self.usage_dir),
            mock.patch.object(usage_store, "PROJECT_DIR", Path(self._tmp.name)),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_record_and_day_report(self) -> None:
        cache = CacheUsage(hit_tokens=9000, miss_tokens=1000, output_tokens=200, source="t")
        when = datetime(2026, 7, 11, 15, 0, 0)
        usage_store.record_usage(model="qwen-max", cache=cache, when=when)
        usage_store.record_usage(
            model="deepseek-v4-flash",
            cache=CacheUsage(hit_tokens=500, miss_tokens=500, output_tokens=50, source="t"),
            when=when,
        )

        report = format_usage_report("2026-07-11")
        self.assertIn("2026-07-11", report)
        self.assertIn("qwen-max", report)
        self.assertIn("hit", report.lower())
        self.assertIn("10k", report)  # 10000 input formatted
        welcome = format_usage_welcome_line(date(2026, 7, 11))
        assert welcome is not None
        self.assertIn("hit", welcome)

    def test_usage_event_keeps_agent_metadata_and_unknown_output(self) -> None:
        when = datetime(2026, 7, 11, 15, 0, 0)
        usage_store.record_usage(
            model="deepseek-v4-pro",
            cache=CacheUsage(hit_tokens=20, miss_tokens=5, output_tokens=None, source="cache"),
            context={
                "agent_type": "goal_test_writer",
                "agent_run_id": "run-1",
                "goal_id": "goal-1",
                "task_id": "task-1",
                "goal_phase": "prepare_tests",
            },
            when=when,
        )

        events = usage_store.load_day_events(when.date())
        totals = usage_store.totals_for_day(when.date())

        self.assertIsNone(events[0].out)
        self.assertEqual(events[0].agent_type, "goal_test_writer")
        self.assertEqual(events[0].goal_phase, "prepare_tests")
        self.assertEqual(totals.out, 0)
        self.assertEqual(totals.unknown_output_calls, 1)

    def test_week_and_month_aggregation(self) -> None:
        for day_n, hit, miss in ((9, 100, 100), (10, 800, 200), (11, 9000, 1000)):
            usage_store.record_usage(
                model="qwen-max",
                cache=CacheUsage(hit_tokens=hit, miss_tokens=miss, output_tokens=10, source="t"),
                when=datetime(2026, 7, day_n, 12, 0, 0),
            )

        week = format_usage_report("week")
        # week uses today(); patch today via format_week with explicit end through parse
        from harness.usage.report import format_week_report, format_month_report

        week = format_week_report(date(2026, 7, 11))
        self.assertIn("last 7 days", week)
        self.assertIn("07/11", week)
        self.assertIn("Spark", week)

        month = format_month_report(2026, 7)
        self.assertIn("2026-07", month)
        self.assertIn("Days with usage: 3", month)

    def test_parse_usage_arg(self) -> None:
        self.assertEqual(parse_usage_arg("week")[0], "week")
        self.assertEqual(parse_usage_arg("2026-07")[0], "month")
        self.assertEqual(parse_usage_arg("2026")[0], "year")
        with self.assertRaises(ValueError):
            parse_usage_arg("nope")

    def test_bar_and_sparkline(self) -> None:
        self.assertEqual(len(bar(0.5, width=10)), 10)
        self.assertEqual(format_tokens(1_250_000), "1.2M")
        self.assertEqual(format_tokens(8200), "8.2k")
        self.assertTrue(sparkline([1, 2, 3, 4]))


if __name__ == "__main__":
    unittest.main()
