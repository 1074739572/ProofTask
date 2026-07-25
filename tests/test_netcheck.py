"""Proxy / network health checks."""

from __future__ import annotations

import unittest
from unittest import mock


class NetcheckTests(unittest.TestCase):
    def test_proxy_host_port(self):
        from harness.providers.netcheck import proxy_host_port

        self.assertEqual(proxy_host_port("http://127.0.0.1:7890"), ("127.0.0.1", 7890))
        self.assertEqual(proxy_host_port("127.0.0.1:7890"), ("127.0.0.1", 7890))

    def test_dead_local_proxy_warns(self):
        from harness.providers import netcheck

        with mock.patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://127.0.0.1:17990", "HTTP_PROXY": "", "ALL_PROXY": ""},
            clear=False,
        ):
            with mock.patch.object(netcheck, "is_tcp_open", return_value=False):
                warn = netcheck.proxy_health_warning()
        self.assertIsNotNone(warn)
        self.assertIn("17990", warn or "")

    def test_live_local_proxy_silent(self):
        from harness.providers import netcheck

        with mock.patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://127.0.0.1:7890"},
            clear=False,
        ):
            with mock.patch.object(netcheck, "is_tcp_open", return_value=True):
                self.assertIsNone(netcheck.proxy_health_warning())

    def test_format_api_error_mentions_proxy(self):
        from harness.providers.errors import format_api_error

        with mock.patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://127.0.0.1:7890"},
            clear=False,
        ):
            text = format_api_error(ConnectionError("Connection refused"))
        self.assertIn("7890", text)
        self.assertIn("代理", text)


if __name__ == "__main__":
    unittest.main()
