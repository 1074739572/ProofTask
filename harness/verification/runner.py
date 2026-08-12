"""Controlled verification execution (L3).

Runs a verification command as a structured argv (never through a shell),
under the same hard controls as the bash tool (closed stdin, timeout with
escalation + process-tree kill, UTF-8 decode), returning a structured result
so callers can build evidence and update feature state.

Security gates before any subprocess spawns:

1. :func:`harness.verification.policy.check_verification_command`
   — allowlist / shell-metacharacter / destructive-token rejection;
2. the existing permission engine on the dedicated ``verify_command`` domain
   — an explicit ``allow`` is required; ``ask`` and ``deny`` both refuse;
3. script-path validation for ``python <script>`` — the script must resolve
   inside the target workspace;
4. post-run workspace change detection — if the verification command modified
   the (git) workspace, the run is reported as failed, because a verification
   command must be read-only.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from harness.permissions.engine import evaluate_single_permission
from harness.settings import get_workdir
from harness.tools.filesystem import (
    _assign_windows_job,
    _kill_process_tree,
    _wait_with_escalation,
)
from harness.verification.policy import check_verification_command, split_verification_command
from harness.verification.snapshot import capture_code_snapshot

#: Default hard timeout for a single verification command (seconds).
DEFAULT_VERIFY_TIMEOUT_S = 120.0
#: Output kept in the run result (evidence trims further).
MAX_VERIFY_OUTPUT_CHARS = 50_000

#: Permission domain used for verification commands (dedicated tool name so
#: the config can grant/deny verification independently of the bash tool).
VERIFY_PERMISSION_TOOL = "verify_command"


@dataclass
class VerificationRunResult:
    """Structured outcome of one verification execution."""

    command: str
    exit_code: int | None  # None when timed out / not executed
    stdout: str
    timed_out: bool
    duration_ms: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        """True only when the command really succeeded: no error (policy /
        permission / workspace-mutation / timeout) AND exit code 0."""
        return self.error is None and not self.timed_out and self.exit_code == 0


def _permission_allows(command: str) -> str | None:
    """Require an explicit allow on the verify_command domain. Returns reason or None."""
    decision = evaluate_single_permission(
        VERIFY_PERMISSION_TOOL, command, include_saved=False
    )
    if decision.effect != "allow":
        return (
            f"permission engine does not allow verification command "
            f"(effect={decision.effect}): {decision.reason}"
        )
    return None


def _validate_script_path(tokens: list[str], workspace: Path) -> str | None:
    """For `python <script>` / `py <script>` / `node <script>`: the script must
    be a relative path resolving inside the workspace. Returns error or None."""
    prog = tokens[0].lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]
    if prog not in ("python", "py", "node"):
        return None
    if len(tokens) < 2:
        return f"{prog} requires a repository script path"
    script = tokens[1]
    if script.startswith(("-", "/", "\\")) or ":" in script[:2]:
        return f"{prog} script must be a relative repository path, got {script!r}"
    try:
        resolved = (workspace / script).resolve()
    except OSError as exc:
        return f"invalid script path {script!r}: {exc}"
    if not resolved.is_relative_to(workspace.resolve()):
        return f"script {script!r} escapes the workspace"
    if not resolved.is_file():
        return f"script {script!r} does not exist in the workspace"
    return None


def run_verification(
    command: str,
    *,
    workspace: str | Path | None = None,
    timeout_s: float | None = None,
) -> VerificationRunResult:
    """Validate + run one verification command under controlled execution.

    Never raises for a failing command or timeout — it returns a structured
    result so the caller can turn it into feature evidence.
    """
    started = time.monotonic()

    def _reject(error: str) -> VerificationRunResult:
        return VerificationRunResult(
            command=command,
            exit_code=None,
            stdout="",
            timed_out=False,
            duration_ms=(time.monotonic() - started) * 1000.0,
            error=error,
        )

    policy = check_verification_command(command)
    if not policy.allowed:
        return _reject(f"policy rejected: {policy.reason}")

    denied = _permission_allows(command)
    if denied is not None:
        return _reject(denied)

    cwd = Path(workspace).expanduser().resolve() if workspace else get_workdir()
    try:
        argv = split_verification_command(command)
    except ValueError as exc:
        return _reject(f"unparseable command: {exc}")

    script_error = _validate_script_path(argv, cwd)
    if script_error is not None:
        return _reject(f"policy rejected: {script_error}")

    snapshot_before = capture_code_snapshot(cwd)
    timeout = timeout_s if timeout_s is not None else DEFAULT_VERIFY_TIMEOUT_S
    timeout = max(1.0, min(float(timeout), 3600.0))

    try:
        # Structured argv, shell=False — no shell interpretation at all.
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,  # Windows python-hang bug: never inherit stdin
            **({"start_new_session": True} if sys.platform != "win32" else {}),
        )
    except OSError as exc:
        return _reject(f"failed to start: {exc}")

    job = _assign_windows_job(proc) if sys.platform == "win32" else None
    collected: list[str] = []
    lock = threading.Lock()

    def pump(stream) -> None:
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\n")
            if not line:
                continue
            with lock:
                collected.append(line)

    reader_out = threading.Thread(target=pump, args=(proc.stdout,), daemon=True)
    reader_err = threading.Thread(target=pump, args=(proc.stderr,), daemon=True)
    reader_out.start()
    reader_err.start()

    info = _wait_with_escalation(proc, timeout, collected, lock)
    if info["timed_out"]:
        if job is not None:
            ctypes.windll.kernel32.TerminateJobObject(job, 1)
            ctypes.windll.kernel32.CloseHandle(job)
            job = None
        else:
            _kill_process_tree(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
    reader_out.join(timeout=2)
    reader_err.join(timeout=2)
    if job is not None:
        ctypes.windll.kernel32.CloseHandle(job)

    with lock:
        output = "\n".join(collected).strip()
    output = output[:MAX_VERIFY_OUTPUT_CHARS] if output else "(no output)"

    if info["timed_out"]:
        return VerificationRunResult(
            command=command,
            exit_code=None,
            stdout=output,
            timed_out=True,
            duration_ms=(time.monotonic() - started) * 1000.0,
            error=f"timed out after {timeout:g}s",
        )

    # Read-only guarantee: a verification command must not modify the workspace.
    if snapshot_before:
        snapshot_after = capture_code_snapshot(cwd)
        if snapshot_after != snapshot_before:
            return VerificationRunResult(
                command=command,
                exit_code=info["exit_code"],
                stdout=output,
                timed_out=False,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error="verification command modified the workspace "
                "(verification must be read-only)",
            )

    return VerificationRunResult(
        command=command,
        exit_code=info["exit_code"],
        stdout=output,
        timed_out=False,
        duration_ms=(time.monotonic() - started) * 1000.0,
        error=None,  # exit code is carried in exit_code; error = couldn't run/abnormal
    )
