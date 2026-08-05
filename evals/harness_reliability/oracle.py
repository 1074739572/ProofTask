"""Independent verifier (oracle) for reliability eval runs.

The agent never sees these checks. A run passes only if the oracle passes;
the agent's own "I'm done" claim is recorded separately and compared later.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from evals.harness_reliability.types import OracleCheck, OracleResult, ReliabilityTask

CHECKS_DIR = Path(__file__).resolve().parent / "checks"


def _run_command(command: list[str], cwd: Path, timeout: int) -> tuple[int, str, float]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Windows gotcha: an inherited open stdin pipe makes python (and
            # child processes) sit in "waiting for input" and appear hung.
            # Always close stdin for eval subprocesses.
            stdin=subprocess.DEVNULL,
        )
        detail = (proc.stdout + proc.stderr).strip()[-2000:]
        return proc.returncode, detail, (time.perf_counter() - started) * 1000
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s", (time.perf_counter() - started) * 1000
    except OSError as exc:
        return -2, f"failed to run: {exc}", (time.perf_counter() - started) * 1000


def git_changed_files(workspace: Path) -> list[str]:
    """Return paths changed vs the fixture baseline commit (modified + untracked).

    Harness runtime artifacts (`.project/`, `.transcripts/`, etc.) are created
    by the harness itself when it binds a session to the workspace; they are
    NOT agent edits and must be excluded from the scope check.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    runtime_dirs = {".project", ".transcripts", ".tasks", ".mailboxes",
                    ".worktrees", ".memory", ".rag", ".scheduled_tasks.json"}
    files: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        top = path.split("/", 1)[0].split("\\", 1)[0]
        if top in runtime_dirs:
            continue
        if path and path not in files:
            files.append(path)
    return files


def run_oracle(task: ReliabilityTask, workspace: Path) -> OracleResult:
    """Execute the task's acceptance checks against the workspace."""
    checks: list[OracleCheck] = []

    # 1) Behavior: the full test suite must pass.
    code, detail, ms = _run_command(
        ["python", "-m", "pytest", "tests", "-q"], workspace, task.oracle_timeout_s
    )
    checks.append(
        OracleCheck(
            id="pytest_tests",
            command="python -m pytest tests -q",
            passed=code == 0,
            exit_code=code,
            duration_ms=ms,
            detail=detail[:300],
        )
    )

    # 2) Independent behavior check (hidden from the agent).
    script_ok = True
    if task.check_script:
        script_path = CHECKS_DIR / task.check_script
        if not script_path.exists():
            script_ok = False
            checks.append(
                OracleCheck(
                    id="script_check",
                    command=f"python {task.check_script} <workspace>",
                    passed=False,
                    detail=f"check script missing: {script_path.name}",
                )
            )
        else:
            code, detail, ms = _run_command(
                ["python", str(script_path), str(workspace)],
                workspace.parent,
                task.oracle_timeout_s,
            )
            checks.append(
                OracleCheck(
                    id="script_check",
                    command=f"python {task.check_script} <workspace>",
                    passed=code == 0,
                    exit_code=code,
                    duration_ms=ms,
                    detail=detail[:300],
                )
            )
            script_ok = code == 0

    # 3) Scope: the agent must not modify files outside the allowed set.
    #    Allowed entries may be exact file paths or directory prefixes
    #    ("tests/" matches "tests/test_app.py").
    changed = git_changed_files(workspace)
    unexpected = []
    for path in changed:
        allowed = any(
            path == entry or path.startswith(entry.rstrip("/") + "/")
            for entry in task.allowed_paths
        )
        if not allowed:
            unexpected.append(path)

    passed = checks[0].passed and script_ok
    reasons = [f"check {c.id} failed (exit {c.exit_code})" for c in checks if not c.passed]
    if unexpected:
        reasons.append(f"modified files outside allowed set: {unexpected}")

    return OracleResult(
        passed=passed,
        checks=checks,
        overreach=bool(unexpected),
        unexpected_files=unexpected,
        failure_reasons=reasons,
    )
