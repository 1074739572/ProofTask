# -*- coding: utf-8 -*-
"""Goal verification for the @-mention feature.

Default checks (the @-mention feature contract):
  1. node_tui typecheck (tsc, both tsconfigs)
  2. backend mentions pytest module
  3. frontend mentions test (node --test with tsx)

Pass ``--full`` to additionally run the entire backend pytest suite. The
feature-specific default is intentional: a goal's declared verification must
not become retroactively blocked by later, unrelated RED tests added elsewhere
in the workspace.

Exits 0 only when ALL selected checks pass. Any failure prints the failing
stage and exits 1, so the goal runner's machine verification is meaningful.
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


def run(stage: str, cmd: list[str], cwd: Path) -> int:
    print(f"\n=== {stage} ===", flush=True)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print(f"[{stage}] TIMEOUT", flush=True)
        return 1
    out = (proc.stdout or "") + (proc.stderr or "")
    print_output(out[-4000:])
    if proc.returncode != 0:
        print(f"[{stage}] FAILED (exit={proc.returncode})", flush=True)
        return 1
    print(f"[{stage}] OK", flush=True)
    return 0


def _npm_cmd() -> str:
    """Windows needs npm.cmd; POSIX uses npm."""
    if sys.platform == "win32":
        return "npm.cmd"
    return "npm"


def main() -> int:
    npm = _npm_cmd()
    full_suite = "--full" in sys.argv[1:]
    checks = [
        (
            "node_tui typecheck",
            [npm, "run", "typecheck"],
            NODE_TUI,
        ),
        (
            "backend mentions test",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_mentions.py",
                "-q",
                "--continue-on-collection-errors",
            ],
            ROOT,
        ),
        (
            "frontend mentions test",
            ["node", "--import", "tsx", "--test", "test/mentions.test.ts"],
            NODE_TUI,
        ),
    ]
    if full_suite:
        checks.insert(
            2,
            (
                "backend pytest (full suite)",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                    "--continue-on-collection-errors",
                ],
                ROOT,
            ),
        )
    for stage, cmd, cwd in checks:
        if run(stage, cmd, cwd) != 0:
            print(f"\nVERIFICATION FAILED at stage: {stage}", flush=True)
            return 1
    print("\nALL VERIFICATION PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
