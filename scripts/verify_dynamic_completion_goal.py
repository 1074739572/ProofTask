# -*- coding: utf-8 -*-
"""Goal verification for non-modal @ file and / command autocomplete.

This is deliberately one repository script so /goal can call it without shell
operators (the verification policy rejects &&, pipes and redirection).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_TUI = ROOT / "node_tui"


def print_output(output: str) -> None:
    """Print captured tool output even when the Windows console uses GBK."""
    encoding = sys.stdout.encoding or "utf-8"
    safe = output.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, flush=True)


def run(stage: str, command: list[str], cwd: Path) -> int:
    print(f"\n=== {stage} ===", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[{stage}] FAILED: {exc}", flush=True)
        return 1
    print_output(((result.stdout or "") + (result.stderr or ""))[-5000:])
    if result.returncode:
        print(f"[{stage}] FAILED (exit={result.returncode})", flush=True)
        return 1
    print(f"[{stage}] OK", flush=True)
    return 0


def main() -> int:
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    checks = [
        ("node_tui typecheck", [npm, "run", "typecheck"], NODE_TUI),
        (
            "backend completion protocol",
            [sys.executable, "-m", "pytest", "tests/test_completion_protocol.py", "-q"],
            ROOT,
        ),
        (
            "frontend autocomplete state",
            ["node", "--import", "tsx", "--test", "test/autocomplete.test.ts"],
            NODE_TUI,
        ),
        (
            "existing mention helpers",
            ["node", "--import", "tsx", "--test", "test/mentions.test.ts"],
            NODE_TUI,
        ),
    ]
    for stage, command, cwd in checks:
        if run(stage, command, cwd):
            print(f"\nVERIFICATION FAILED at stage: {stage}", flush=True)
            return 1
    print("\nALL DYNAMIC-COMPLETION VERIFICATION PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
