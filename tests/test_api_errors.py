"""Provider/API error formatting for TUI + CLI."""

from __future__ import annotations

import unittest


class FormatApiErrorTests(unittest.TestCase):
    def test_strips_html_gateway_page(self):
        from harness.providers.errors import format_api_error

        exc = Exception("<html><title>502 Bad Gateway</title><body>nginx</body></html>")
        text = format_api_error(exc)
        self.assertIn("502 Bad Gateway", text)
        self.assertNotIn("<html", text.lower())

    def test_extracts_openai_style_message(self):
        from harness.providers.errors import format_api_error

        class AuthError(Exception):
            status_code = 401
            message = (
                "Error code: 401 - {'error': {'message': 'Incorrect API key provided',"
                " 'type': 'invalid_request_error'}}"
            )

        text = format_api_error(AuthError("ignored"))
        self.assertIn("HTTP 401", text)
        self.assertIn("Incorrect API key", text)
        self.assertIn("API Key", text)

    def test_error_assistant_marker(self):
        from harness.providers.errors import is_error_assistant_text

        self.assertTrue(is_error_assistant_text("[Error] boom"))
        self.assertFalse(is_error_assistant_text("normal answer"))


if __name__ == "__main__":
    unittest.main()
