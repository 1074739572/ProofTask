"""Isolated Git worktree sessions for attributable agent changes."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class ChangeSessionError(RuntimeError):
    """Raised when an isolated change session cannot be created or inspected."""


@dataclass
class ChangeSession:
    repository_root: Path
    execution_worktree: Path
    base_commit: str
    session_id: str
    worker_base_commit: str = ""
    initial_file_hashes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        repository_root: str | Path,
        *,
        session_id: str | None = None,
        worktree_root: str | Path | None = None,
        preserve_main_changes: bool = True,
    ) -> "ChangeSession":
        root = Path(repository_root).expanduser().resolve()
        sid = session_id or f"change-{uuid.uuid4().hex[:12]}"
        cls._run(root, "rev-parse", "--show-toplevel")
        base = cls._run(root, "rev-parse", "HEAD").strip()
        if not base:
            raise ChangeSessionError("repository has no HEAD commit")
        parent = Path(worktree_root).expanduser().resolve() if worktree_root else root / ".project" / "worktrees"
        worktree = (parent / sid).resolve()
        if worktree.exists():
            raise ChangeSessionError(f"worktree path already exists: {worktree}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        try:
            cls._run(root, "worktree", "add", "--detach", str(worktree), base)
            initial = cls._copy_main_changes(root, worktree) if preserve_main_changes else {}
            worker_base = cls._commit_baseline(worktree, sid) if initial else base
        except Exception:
            if worktree.exists():
                try:
                    cls._run(root, "worktree", "remove", "--force", str(worktree))
                except ChangeSessionError:
                    pass
            raise
        return cls(root, worktree, base, sid, worker_base, initial)

    @classmethod
    def attach(
        cls,
        repository_root: str | Path,
        execution_worktree: str | Path,
        *,
        base_commit: str,
        session_id: str,
        worker_base_commit: str = "",
    ) -> "ChangeSession":
        root = Path(repository_root).expanduser().resolve()
        worktree = Path(execution_worktree).expanduser().resolve()
        if not worktree.is_dir():
            raise ChangeSessionError(f"change worktree is missing: {worktree}")
        return cls(root, worktree, base_commit, session_id, worker_base_commit or base_commit)

    @classmethod
    def _copy_main_changes(cls, root: Path, worktree: Path) -> dict[str, str]:
        output = cls._run(root, "status", "--porcelain=v1", "-z")
        entries = output.split("\0")
        hashes: dict[str, str] = {}
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            status = entry[:2]
            path = entry[3:] if len(entry) > 3 else ""
            if status and status[0] in "RC" and status[1] in "RC" and index < len(entries):
                path = entries[index]
                index += 1
            normalized = path.replace("\\", "/")
            if not normalized or normalized.split("/", 1)[0] in {".project", ".tasks", ".features", ".worktrees"}:
                continue
            source = (root / path).resolve()
            target = (worktree / path).resolve()
            if not source.is_relative_to(root) or not target.is_relative_to(worktree):
                raise ChangeSessionError(f"dirty path escapes repository: {path}")
            if "D" in status:
                if target.exists():
                    target.unlink() if target.is_file() else shutil.rmtree(target)
                hashes[normalized] = "<missing>"
                continue
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                hashes[normalized] = hashlib.sha256(source.read_bytes()).hexdigest()
        return hashes

    @classmethod
    def _commit_baseline(cls, worktree: Path, session_id: str) -> str:
        cls._run(worktree, "add", "-A")
        cls._run(
            worktree,
            "-c", "user.name=Harness ChangeSession",
            "-c", "user.email=harness@localhost",
            "commit", "--no-verify", "-m", f"harness baseline {session_id}",
        )
        return cls._run(worktree, "rev-parse", "HEAD").strip()

    @staticmethod
    def _run(cwd: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=str(cwd), check=False, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChangeSessionError(f"git command unavailable: git {' '.join(args)}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ChangeSessionError(f"git {' '.join(args)} failed: {detail[:500]}")
        return result.stdout

    def changed_files(self) -> set[str]:
        output = self._run(self.execution_worktree, "status", "--porcelain=v1", "-z")
        changed: set[str] = set()
        for entry in output.split("\0"):
            if entry:
                path = entry[3:] if len(entry) > 3 else ""
                if path:
                    changed.add(path.replace("\\", "/"))
        return changed

    def diff(self) -> str:
        """Return the complete tracked and untracked worker patch."""
        base = self.worker_base_commit or self.base_commit
        self._run(self.execution_worktree, "add", "-A")
        return self._run(self.execution_worktree, "diff", "--cached", "--no-ext-diff", "--binary", base)

    def merge_to_main(self) -> str:
        """Apply only worker changes to the main checkout.

        Returns ``merged`` or ``conflict``. A conflict leaves the worktree and
        main checkout available for explicit recovery; it never force-overwrites.
        """
        patch = self.diff()
        if not patch.strip():
            return "merged"
        try:
            result = subprocess.run(
                ["git", "apply", "--3way", "--binary", "-"],
                cwd=str(self.repository_root),
                input=patch,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "conflict"
        return "merged" if result.returncode == 0 else "conflict"

    def remove(self, *, force: bool = True) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(self.execution_worktree))
        self._run(self.repository_root, *args)

    def status(self) -> str:
        return self._run(self.execution_worktree, "status", "--short")
