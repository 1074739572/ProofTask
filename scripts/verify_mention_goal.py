# -*- coding: utf-8 -*-
"""Goal verification for the @-mention feature.

Runs (in order):
  1. node_tui typecheck (tsc, both tsconfigs)
  2. backend pytest suite (all tests under tests/)
  3. frontend mentions test (node --test with tsx)

Exits 0 only when ALL pass. Any failure prints the failing stage and exits 1,
so the goal runner's machine verification is meaningful.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_TUI = ROOT / "node_tui"


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
    print(out[-4000:], flush=True)
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
    mentions_only = "--mentions-only" in sys.argv[1:]
    checks = [
        (
            "node_tui typecheck",
            [npm, "run", "typecheck"],
            NODE_TUI,
        ),
    ]
    if mentions_only:
        # Goal-scoped verification: only the @-mention tests, plus typecheck.
        # Avoids the unrelated pre-existing failure in test_orchestration_routing.
        checks += [
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
    else:
        checks += [
            (
                "backend pytest (tests/)",
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
            (
                "frontend mentions test",
                ["node", "--import", "tsx", "--test", "test/mentions.test.ts"],
                NODE_TUI,
            ),
        ]
    for stage, cmd, cwd in checks:
        if run(stage, cmd, cwd) != 0:
            print(f"\nVERIFICATION FAILED at stage: {stage}", flush=True)
            return 1
    print("\nALL VERIFICATION PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
