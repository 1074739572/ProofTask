"""CLI command routing tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

from harness.cli import (
    _help_text,
    _match_cli_command,
    _resolve_open_directory,
)


class TestCliCommands(unittest.TestCase):
    def test_model_does_not_match_mode(self) -> None:
        self.assertTrue(_match_cli_command("/model", "/model"))
        self.assertFalse(_match_cli_command("/model", "/mode"))
        self.assertTrue(_match_cli_command("/model qwen-max", "/model"))

    def test_mode(self) -> None:
        self.assertTrue(_match_cli_command("/mode", "/mode"))
        self.assertTrue(_match_cli_command("/mode direct", "/mode"))
        self.assertFalse(_match_cli_command("/model", "/mode"))

    def test_open_resolves_directory_with_spaces(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="中文 workspace ") as directory:
            path, error = _resolve_open_directory(f"/open {directory}")
        self.assertEqual(path, Path(directory).resolve())
        self.assertEqual(error, "")

    def test_open_rejects_missing_and_file_paths(self) -> None:
        from tempfile import NamedTemporaryFile

        path, error = _resolve_open_directory("/open")
        self.assertIsNone(path)
        self.assertIn("Usage", error)
        with NamedTemporaryFile() as file:
            path, error = _resolve_open_directory(f"/open {file.name}")
        self.assertIsNone(path)
        self.assertIn("not a directory", error)

    def test_help_mentions_open(self) -> None:
        self.assertIn("/open <directory>", _help_text())

    def test_workspace_switch_precedes_settings_import(self) -> None:
        """Regression: WORKDIR must resolve to the -C target, not the pre-switch cwd.

        Importing the harness package eagerly freezes harness.settings.WORKDIR
        from Path.cwd(); main.py must chdir (via -C) before that first import.
        Runs in a subprocess so a warmed sys.modules cannot mask the ordering.
        """
        import subprocess
        from pathlib import Path
        from tempfile import TemporaryDirectory

        root = Path(__file__).resolve().parent.parent
        with TemporaryDirectory(prefix="ws ordering ") as directory:
            code = (
                "import sys\n"
                f"sys.path.insert(0, {str(root)!r})\n"
                f"sys.argv = ['main.py', '-C', {str(directory)!r}]\n"
                "import main\n"
                "args = main._parse_args()\n"
                "main._change_workspace(args.workspace)\n"
                "import harness.settings as s\n"
                "print(s.WORKDIR)\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(Path(directory).resolve()))


if __name__ == "__main__":
    unittest.main()
