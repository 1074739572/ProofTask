"""Windows clipboard reader must not truncate 64-bit handles."""

from __future__ import annotations

import subprocess
import sys
import unittest


class WindowsClipboardTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_win32_reads_unicode_clipboard(self):
        from harness.ui.tui.clipboard import _windows_clipboard, read_os_clipboard

        marker = "HARNESS_CLIP_REGRESSION_789"
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Set-Clipboard -Value '{marker}'",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        win = _windows_clipboard()
        self.assertEqual(win, marker)
        self.assertEqual(read_os_clipboard(), marker)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_win32_write_roundtrip(self):
        from harness.ui.tui.clipboard import read_os_clipboard, write_os_clipboard

        marker = "HARNESS_CLIP_WRITE_321"
        self.assertTrue(write_os_clipboard(marker))
        self.assertEqual(read_os_clipboard(), marker)


if __name__ == "__main__":
    unittest.main()
