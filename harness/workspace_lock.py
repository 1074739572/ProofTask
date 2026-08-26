"""Cross-process coordination for short main-workspace mutations."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


class WorkspaceMutationLock:
    """An advisory lock shared by harness writers and Goal publication."""

    def __init__(self, workspace: str | Path, *, purpose: str = "mutation") -> None:
        self.workspace = self._workspace_root(Path(workspace).expanduser().resolve())
        self.path = self.workspace / ".project" / "workspace-mutation.lock"
        self.purpose = purpose
        self.token = uuid.uuid4().hex
        self.acquired = False

    @staticmethod
    def _workspace_root(start: Path) -> Path:
        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                return candidate
        return start

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, SystemError):
            if os.name != "nt":
                return False
            try:
                import ctypes
                from ctypes import wintypes

                process_query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
                handle = ctypes.windll.kernel32.OpenProcess(process_query, False, pid)
                if not handle:
                    return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
                try:
                    exit_code = wintypes.DWORD()
                    if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return True
                    return exit_code.value == 259  # STILL_ACTIVE
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
            except (AttributeError, OSError, SystemError):
                # A false busy result is safer than allowing concurrent edits.
                return True
        return True

    def acquire(self, timeout_s: float = 0.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.path.mkdir()
            except FileExistsError:
                self._recover_stale_owner()
                if not self.path.exists():
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.03)
                continue
            try:
                (self.path / "owner.json").write_text(
                    json.dumps({
                        "pid": os.getpid(), "token": self.token,
                        "purpose": self.purpose, "created_at": time.time(),
                    }),
                    encoding="utf-8",
                )
            except OSError:
                try:
                    self.path.rmdir()
                except OSError:
                    pass
                raise
            self.acquired = True
            return True

    def _recover_stale_owner(self) -> None:
        owner = self.path / "owner.json"
        try:
            payload = json.loads(owner.read_text(encoding="utf-8", errors="replace"))
            pid = int(payload.get("pid") or 0) if isinstance(payload, dict) else 0
            created_at = float(payload.get("created_at") or 0) if isinstance(payload, dict) else 0
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return
        if self._pid_is_alive(pid) or time.time() - created_at < 2.0:
            return
        try:
            owner.unlink()
            self.path.rmdir()
        except OSError:
            pass

    def release(self) -> None:
        if not self.acquired:
            return
        owner = self.path / "owner.json"
        try:
            payload = json.loads(owner.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("token") == self.token:
            try:
                owner.unlink()
                self.path.rmdir()
            except OSError:
                pass
        self.acquired = False
