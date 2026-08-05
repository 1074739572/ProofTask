"""M001: bash tool must not hang when parent stdin is a pipe (TUI repro).

Real TUI scenario: Node spawns the Python backend with stdio all pipes, so
the backend's stdin is a LIVE pipe. Any bash child that READS stdin (bare
interactive `python`, `cat`, `findstr`) will either hang forever or steal
the frontend's JSON commands.

Repro: spawn a child whose stdin is a LIVE pipe (Popen stdin=PIPE, never
closed, never fed) — exactly what the TUI does. Inside that child, call the
REAL run_bash with a bare interactive `python`. On the buggy code the inner
python blocks reading the pipe => child hangs => M001 FAILS. After the fix
(stdin=DEVNULL) the REPL gets EOF and exits => PASS.

IMPORTANT: the main process must NOT be started with < NUL (that makes its
stdin DEVNULL and the repro silently passes). Run this eval normally.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent

# Child mode: probe run_bash with a bare interactive python. Must not import
# the package before sys.path is set.
if "--child" in sys.argv:
    sys.path.insert(0, str(_REPO))
    from harness.tools.filesystem import run_bash

    out = run_bash("python", timeout=6)
    # Buggy: hangs (timeout) => nonzero. Fixed: EOF => exits, "timeout" absent.
    raise SystemExit(0 if "timeout" not in out.lower() else 1)

from evals.harness_mechanisms.types import MResult


def run_m001(rounds: int = 1) -> MResult:
    started = time.perf_counter()
    attempts: list[bool] = []
    details: list[str] = []
    for i in range(rounds):
        # Child stdin is a LIVE pipe we keep open and never feed — the child
        # must NOT block on it. We spawn via Popen directly (not shell), so
        # this is independent of the outer process's stdin.
        proc = subprocess.Popen(
            [sys.executable, str(_HERE / "m001_bash_stdin.py"), "--child"],
            stdin=subprocess.PIPE,  # live pipe, kept open
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(_REPO),
        )
        try:
            # Do NOT write to stdin and do NOT close it; just wait. The child
            # must complete on its own (i.e. run_bash must not block on the
            # inherited pipe).
            stdout, stderr = proc.communicate(timeout=30)
            ok = proc.returncode == 0
            detail = (stdout + stderr).strip()[:160]
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            ok = False
            detail = "child hung on pipe stdin (timeout)"
        attempts.append(ok)
        details.append(f"run{i+1}: {detail}")

    passed = sum(attempts) >= max(1, rounds * 2 // 3)
    return MResult(
        id="M001",
        name="bash tool does not hang on piped stdin (bare interactive child)",
        passed=passed,
        detail=" | ".join(details),
        duration_ms=(time.perf_counter() - started) * 1000,
        runs=rounds,
        attempts=attempts,
    )


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(0)  # handled at module top; unreachable
    raise SystemExit(main())


def main() -> int:
    from evals.harness_mechanisms.__main__ import _run_single

    return _run_single(run_m001)
