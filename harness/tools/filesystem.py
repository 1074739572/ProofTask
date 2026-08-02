"""Filesystem and shell tools."""

from __future__ import annotations

import difflib
import itertools
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from harness.settings import WORKDIR, get_workdir

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes


def safe_path(path: str, cwd: Path | None = None) -> Path:
    base = cwd or get_workdir()
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


DEFAULT_BASH_TIMEOUT_MS = 120_000
MAX_BASH_TIMEOUT_MS = 3_600_000
_GRACE_SECONDS = 3.0


def _timeout_seconds(timeout: int | None) -> float:
    """Model-supplied timeout is in milliseconds; clamp to [1s, 60min]."""
    raw = timeout if timeout is not None else DEFAULT_BASH_TIMEOUT_MS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_BASH_TIMEOUT_MS
    return max(1.0, min(MAX_BASH_TIMEOUT_MS, value) / 1000.0)


def _assign_windows_job(process: subprocess.Popen):
    """Windows: place the child in a kill-on-close Job Object.

    `taskkill /T` is unreliable for grandchildren (cmd -> python -> child),
    which is exactly the orphan-process problem observed in practice. A Job
    Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE guarantees the whole tree
    dies with the shell. Returns the job handle, or None if unavailable
    (e.g. nested-job restrictions), in which case callers fall back to
    taskkill."""
    if sys.platform != "win32":
        return None

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    kernel32 = ctypes.windll.kernel32
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        try:
            kernel32.CloseHandle(job)
        except Exception:
            pass
        return None


def _kill_process_tree(process: subprocess.Popen, *, graceful: float = _GRACE_SECONDS) -> None:
    """Terminate the whole process tree with a graceful window first.

    This mirrors Cline's terminateProcessTree / OpenCode's forceKillAfter:
    POSIX sends SIGTERM to the process group, waits `graceful` seconds for a
    clean shutdown, then escalates to SIGKILL. On Windows the preferred path
    is the Job Object (see _assign_windows_job); taskkill /T /F is the
    fallback and is force-only (same documented limitation as Cline's
    tree-kill)."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=10,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        return
    try:
        process.wait(timeout=graceful)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def run_bash(
    command: str,
    cwd: Path | None = None,
    run_in_background: bool = False,
    timeout: int | None = None,
) -> str:
    # Always decode as UTF-8 with replacement: Windows locale (GBK) otherwise
    # crashes the stdout reader thread on non-GBK bytes (UnicodeDecodeError).
    timeout_s = _timeout_seconds(timeout)
    kwargs: dict = {
        "shell": True,
        "cwd": cwd or get_workdir(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **kwargs)
    job = _assign_windows_job(proc) if sys.platform == "win32" else None
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if job is not None:
            ctypes.windll.kernel32.TerminateJobObject(job, 1)
            ctypes.windll.kernel32.CloseHandle(job)
            job = None
        else:
            _kill_process_tree(proc)
        try:
            proc.communicate()  # drain pipes after the tree is gone
        except Exception:
            pass
        return (
            f"Error: Timeout ({int(timeout_s)}s). The command did not finish "
            f"within the timeout. If it is expected to take longer, retry with "
            f"a larger `timeout` (in milliseconds, up to {MAX_BASH_TIMEOUT_MS}) "
            f"or pass `run_in_background: true`."
        )
    finally:
        if job is not None:
            ctypes.windll.kernel32.CloseHandle(job)
    output = (out + err).strip()
    return output[:50000] if output else "(no output)"


def run_bash_streaming(
    command: str,
    cwd: Path | None = None,
    timeout: int | None = None,
    tool_use_id: str = "",
) -> str:
    """Run a bash command and stream each output line as a `tool_output` event.

    Mirrors Claude Code's live tool output: every stdout/stderr line is emitted
    as ``events.emit("tool_output", id=tool_use_id, line=...)`` while the command
    runs, so the TUI can show the output streaming in real time instead of a
    black box that resolves at the end. Returns the full combined output with
    the same semantics as :func:`run_bash`.
    """
    from harness.ui import events

    timeout_s = _timeout_seconds(timeout)
    kwargs: dict = {
        "shell": True,
        "cwd": cwd or get_workdir(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **kwargs)
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
            try:
                events.emit("tool_output", id=tool_use_id, line=line)
            except Exception:
                pass

    reader_out = threading.Thread(target=pump, args=(proc.stdout,), daemon=True)
    reader_err = threading.Thread(target=pump, args=(proc.stderr,), daemon=True)
    reader_out.start()
    reader_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        if job is not None:
            ctypes.windll.kernel32.TerminateJobObject(job, 1)
            ctypes.windll.kernel32.CloseHandle(job)
            job = None
        else:
            _kill_process_tree(proc)
        proc.wait(timeout=10)
    finally:
        if job is not None:
            ctypes.windll.kernel32.CloseHandle(job)
    reader_out.join(timeout=2)
    reader_err.join(timeout=2)

    with lock:
        output = "\n".join(collected).strip()
    if timed_out:
        return (
            f"Error: Timeout ({int(timeout_s)}s). The command did not finish "
            f"within the timeout. If it is expected to take longer, retry with "
            f"a larger `timeout` (in milliseconds, up to {MAX_BASH_TIMEOUT_MS}) "
            f"or pass `run_in_background: true`."
        )
    return output[:50000] if output else "(no output)"


MAX_READ_CHARS = 400_000  # hard cap for full-file reads; page with limit/offset
MAX_WRITE_CHARS = 2_000_000  # write_file content cap (chars)
_BINARY_SNIFF_BYTES = 8192
_ENCODING_PROBE_BYTES = 65_536


def _count_lines_bytes(target: Path) -> int:
    """Streaming newline count: O(size) time, O(1) memory."""
    count = 0
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            count += chunk.count(b"\n")
    return count


def _pick_encoding(target: Path) -> str:
    """UTF-8 preferred; fall back to GBK (Windows locale) on decode failure."""
    try:
        with target.open("rb") as fh:
            probe = fh.read(_ENCODING_PROBE_BYTES)
        probe.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gbk"


def run_read(
    path: str,
    limit: int | None = None,
    offset: int = 0,
    cwd: Path | None = None,
) -> str:
    try:
        target = safe_path(path, cwd)
        if target.is_dir():
            return f"Error: {path} is a directory; use glob to list its contents."
        with target.open("rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
        if b"\x00" in head:
            return f"Error: {path} appears to be a binary file; inspect it with bash instead."
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        if limit is not None and limit <= 0:
            limit = None

        total = _count_lines_bytes(target)
        if limit is not None and offset >= total:
            return f"{total} lines total (offset {offset} is past the end of {path})"
        encoding = _pick_encoding(target)
        with target.open("r", encoding=encoding, errors="replace") as fh:
            if limit is None:
                raw_lines = fh.read().splitlines()
            else:
                raw_lines = [
                    ln.rstrip("\r\n")
                    for ln in itertools.islice(fh, offset, offset + limit)
                ]

        truncated = False
        if limit is None:
            kept, size = [], 0
            for line in raw_lines:
                size += len(line) + 1
                if size > MAX_READ_CHARS:
                    truncated = True
                    break
                kept.append(line)
            raw_lines = kept

        shown = len(raw_lines)
        end = offset + shown
        out = [f"{total} lines total" + (f", showing {offset + 1}-{end}" if limit is not None else "")]
        for i, line in enumerate(raw_lines, start=offset + 1):
            out.append(f"{i:>6} | {line}")
        if limit is not None and end < total:
            out.append(f"... ({total - end} more lines)")
        elif truncated:
            out.append(f"... (truncated after {MAX_READ_CHARS} chars; use limit/offset to page)")
        return "\n".join(out)
    except Exception as exc:
        return f"Error: {exc}"


def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    try:
        if len(content) > MAX_WRITE_CHARS:
            return (
                f"Error: content too large ({len(content)} chars > {MAX_WRITE_CHARS}); "
                f"write it in smaller chunks or generate it with bash."
            )
        target = safe_path(path, cwd)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def _replace_nth(text: str, old: str, new: str, n: int) -> str | None:
    idx = -1
    for _ in range(n):
        idx = text.find(old, idx + 1)
        if idx == -1:
            return None
    return text[:idx] + new + text[idx + len(old):]


def _closest_line_hint(old_text: str, text: str) -> str | None:
    """Help the model recover from a failed match by pointing at the nearest line."""
    first = old_text.splitlines()[0].strip()
    if not first:
        return None
    lines = text.splitlines()
    matches = difflib.get_close_matches(first, [ln.strip() for ln in lines], n=1, cutoff=0.55)
    if not matches:
        return None
    line_no = next(i for i, ln in enumerate(lines) if ln.strip() == matches[0]) + 1
    return f"Closest match: line {line_no}: {lines[line_no - 1].strip()[:80]}"


def run_edit(
    path: str,
    old_text: str,
    new_text: str,
    occurrence: int = 1,
    cwd: Path | None = None,
) -> str:
    try:
        target = safe_path(path, cwd)
        text = target.read_text(encoding="utf-8")
        if old_text not in text:
            hint = _closest_line_hint(old_text, text)
            msg = f"Error: text not found in {path}"
            return f"{msg}. {hint}" if hint else f"{msg}."
        count = text.count(old_text)
        try:
            n = int(occurrence or 1)
        except (TypeError, ValueError):
            n = 1
        if n < 1 or n > count:
            return (
                f"Error: occurrence {n} out of range; '{old_text[:40]}' appears "
                f"{count} time(s) in {path}."
            )
        replaced = _replace_nth(text, old_text, new_text, n)
        if replaced is None:
            return f"Error: occurrence {n} not found in {path}"
        target.write_text(replaced, encoding="utf-8")
        if count > 1:
            return f"Edited {path} (replaced occurrence {n} of {count})"
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_glob(pattern: str, cwd: Path | None = None) -> str:
    import glob as globlib

    try:
        base = cwd or get_workdir()
        results = []
        for match in globlib.glob(pattern, root_dir=base, recursive=True):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(sorted(results)) if results else "(no matches)"
    except Exception as exc:
        return f"Error: {exc}"
