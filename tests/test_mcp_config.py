"""Tests for MCP config env expansion."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from harness.mcp.config import _expand_env_value, resolve_server_env


class TestMcpEnvExpand(unittest.TestCase):
    def test_expand_placeholder(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test"}):
            self.assertEqual(
                _expand_env_value("${GITHUB_PERSONAL_ACCESS_TOKEN}"),
                "ghp_test",
            )

    def test_resolve_merges_os_environ(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/bin", "GITHUB_PERSONAL_ACCESS_TOKEN": "tok"}):
            merged = resolve_server_env(
                {"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"}}
            )
            assert merged is not None
            self.assertEqual(merged["GITHUB_PERSONAL_ACCESS_TOKEN"], "tok")
            self.assertEqual(merged["PATH"], "/bin")

    def test_no_env_means_inherit(self) -> None:
        self.assertIsNone(resolve_server_env({}))

    def test_allowlist_does_not_copy_unlisted_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"PATH": "/bin", "OPENAI_API_KEY": "secret", "CUSTOM": "value"},
            clear=True,
        ):
            env = resolve_server_env({"env_allowlist": ["PATH", "CUSTOM"]})
        assert env == {"PATH": "/bin", "CUSTOM": "value"}


if __name__ == "__main__":
    unittest.main()
