"""Tests for @file mention expansion in user messages (feature under development).

The feature: when a user message contains ``@file:<path>`` (or a bare
``@<path>`` mention produced by the TUI picker), the backend resolves the
referenced file and injects its content into the agent context before the
message reaches the model.

These tests are written against the planned public API:

    harness.mentions.parse_mentions(text) -> list[Mention]
    harness.mentions.expand_mentions(text, base_dir) -> (expanded_text, notes)

They are intentionally RED until the feature is implemented — that is the
point: the goal verification command must fail until the @ feature exists.
"""

from __future__ import annotations

import pytest

try:
    from harness.mentions import Mention, expand_mentions, parse_mentions
except ModuleNotFoundError:
    # The feature is not implemented yet. This must FAIL (not skip) so the goal
    # runner's verification stays red until the @-mention feature exists.
    pytest.fail(
        "harness.mentions is not implemented — @-mention feature is missing. "
        "Goal verification must stay RED until this module exists.",
        pytrace=False,
    )


# ---------------------------------------------------------------------------
# parse_mentions: extract @file references from raw text
# ---------------------------------------------------------------------------

class TestParseMentions:
    def test_parses_simple_file_mention(self):
        assert parse_mentions("请看看 @file:src/main.py 这个文件") == [
            Mention(path="src/main.py", line_start=None, line_end=None),
        ]

    def test_parses_mention_with_line_range(self):
        assert parse_mentions("第 @file:src/main.py:10-20 行有问题") == [
            Mention(path="src/main.py", line_start=10, line_end=20),
        ]

    def test_parses_multiple_mentions(self):
        assert parse_mentions("@file:a.py 和 @file:b.py 都要改") == [
            Mention(path="a.py", line_start=None, line_end=None),
            Mention(path="b.py", line_start=None, line_end=None),
        ]

    def test_bare_at_path_mention(self):
        # The TUI inserts bare "@path" mentions; backend must accept both.
        assert parse_mentions("看下 @src/main.py 的实现") == [
            Mention(path="src/main.py", line_start=None, line_end=None),
        ]

    def test_ignore_at_in_email_or_social(self):
        assert parse_mentions("联系 a@b.com 或 @everyone") == []


# ---------------------------------------------------------------------------
# expand_mentions: resolve files and inject content
# ---------------------------------------------------------------------------

class TestExpandMentions:
    def test_expand_injects_file_content(self, tmp_path):
        f = tmp_path / "main.py"
        f.write_text("def hello():\n    return 1\n", encoding="utf-8")
        text = f"请修改 @file:{f.name}"
        expanded, notes = expand_mentions(text, base_dir=tmp_path)
        assert "def hello():" in expanded
        assert notes[0].path == f.name
        assert notes[0].ok is True

    def test_expand_honors_line_range(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_text("\n".join(f"line{i}" for i in range(30)), encoding="utf-8")
        text = f"看 @file:big.py:5-8"
        expanded, notes = expand_mentions(text, base_dir=tmp_path)
        assert "line5" in expanded
        assert "line8" in expanded
        assert "line1" not in expanded

    def test_missing_file_produces_error_note_not_crash(self, tmp_path):
        text = "@file:does_not_exist.py"
        expanded, notes = expand_mentions(text, base_dir=tmp_path)
        assert notes[0].ok is False
        # The mention is preserved so the agent can still see what was asked.
        assert "does_not_exist.py" in expanded

    def test_mention_outside_base_dir_is_rejected(self, tmp_path):
        outside = tmp_path / ".." / "secret.py"
        outside.write_text("top secret", encoding="utf-8")
        text = f"@file:{outside}"
        expanded, notes = expand_mentions(text, base_dir=tmp_path)
        assert notes[0].ok is False  # path traversal blocked
