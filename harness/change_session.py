"""Isolated Git worktree sessions for attributable Goal changes.

The main checkout is never used as a Goal worker directory. A synthetic
baseline commit captures its content through a temporary index, so staged
changes, untracked files, renames, and deletions all have one durable tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from harness.workspace_lock import WorkspaceMutationLock


class ChangeSessionError(RuntimeError):
    """Raised when an isolated change session cannot be created or inspected."""


_HARNESS_METADATA_DIRS = (
    ".project", ".tasks", ".features", ".worktrees", ".mailboxes", ".memory",
    ".transcripts", ".task_outputs",
)


@dataclass
class ChangeIntegration:
    """A disposable merge candidate built from main-at-merge-time + Goal work."""

    session: "ChangeSession"
    worktree: Path
    main_commit: str
    result_commit: str
    error: str = ""

    @property
    def execution_workspace(self) -> Path:
        return self.session.execution_path(self.worktree)

    def publish(self) -> str:
        """Publish the verified candidate without changing the main index."""
        lock = WorkspaceMutationLock(self.session.repository_root, purpose="goal_publish")
        if not lock.acquire(timeout_s=3.0):
            return "main_locked"
        try:
            # The snapshot must be taken inside the shared writer lock. This
            # prevents another harness agent from changing main between check
            # and git apply.
            current_tree = self.session.capture_tree(self.session.repository_root)
            if current_tree != self.session.commit_tree(self.main_commit):
                return "main_changed"
            patch = self.session._run_bytes(
                self.session.repository_root,
                "diff", "--binary", "--full-index", self.main_commit, self.result_commit,
            )
            if not patch:
                return "published"
            before_index = self.session._run_bytes(
                self.session.repository_root, "diff", "--cached", "--binary", "HEAD"
            )
            if not self.session._apply_patch(patch, check_only=True):
                return "publish_conflict"
            if not self.session._apply_patch(patch):
                return "publish_conflict"
            after_index = self.session._run_bytes(
                self.session.repository_root, "diff", "--cached", "--binary", "HEAD"
            )
            if before_index != after_index:
                raise ChangeSessionError("publishing a Goal patch modified the main Git index")
            if self.session.capture_tree(self.session.repository_root) != self.session.commit_tree(self.result_commit):
                return "main_changed"
            return "published"
        finally:
            lock.release()

    def remove(self) -> None:
        self.session._remove_worktree(self.worktree)


@dataclass
class ChangeSession:
    repository_root: Path
    execution_worktree: Path
    base_commit: str
    session_id: str
    worker_base_commit: str
    execution_relative: str = "."

    @classmethod
    def create(
        cls,
        repository_root: str | Path,
        *,
        execution_workspace: str | Path | None = None,
        session_id: str | None = None,
        worktree_root: str | Path | None = None,
        preserve_main_changes: bool = True,
    ) -> "ChangeSession":
        requested_root = Path(repository_root).expanduser().resolve()
        top_level = cls._run(requested_root, "rev-parse", "--show-toplevel").strip()
        root = Path(top_level).resolve()
        base = cls._run(root, "rev-parse", "HEAD").strip()
        if not base:
            raise ChangeSessionError("repository has no HEAD commit")
        execution = Path(execution_workspace or requested_root).expanduser().resolve()
        try:
            relative = execution.relative_to(root).as_posix() or "."
        except ValueError as exc:
            raise ChangeSessionError("Goal execution workspace must be inside the Git repository") from exc
        sid = session_id or f"change-{uuid.uuid4().hex[:12]}"
        parent = (
            Path(worktree_root).expanduser().resolve()
            if worktree_root
            else requested_root / ".project" / "worktrees"
        )
        worktree = (parent / sid).resolve()
        if worktree.exists():
            raise ChangeSessionError(f"worktree path already exists: {worktree}")
        parent.mkdir(parents=True, exist_ok=True)
        baseline = base
        try:
            if preserve_main_changes:
                baseline = cls.snapshot_commit(root, parent_commit=base, label=f"baseline {sid}")
            cls._run(root, "worktree", "add", "--detach", str(worktree), baseline)
            session = cls(root, worktree, base, sid, baseline, relative)
            if not session.execution_path().is_dir():
                raise ChangeSessionError(f"Goal execution project is missing in isolated worktree: {relative}")
            session._provision_runtime_dependencies(execution, session.execution_path())
            return session
        except Exception:
            if worktree.exists():
                try:
                    cls._remove_worktree_from(root, worktree)
                except ChangeSessionError:
                    pass
            raise

    @classmethod
    def attach(
        cls,
        repository_root: str | Path,
        execution_worktree: str | Path,
        *,
        base_commit: str,
        session_id: str,
        worker_base_commit: str,
        execution_relative: str = ".",
    ) -> "ChangeSession":
        requested_root = Path(repository_root).expanduser().resolve()
        top_level = cls._run(requested_root, "rev-parse", "--show-toplevel").strip()
        root = Path(top_level).resolve()
        worktree = Path(execution_worktree).expanduser().resolve()
        if not worktree.is_dir():
            raise ChangeSessionError(f"change worktree is missing: {worktree}")
        if not worker_base_commit:
            raise ChangeSessionError("change session baseline is missing")
        cls._run(root, "cat-file", "-e", f"{worker_base_commit}^{{commit}}")
        session = cls(root, worktree, base_commit, session_id, worker_base_commit, execution_relative or ".")
        if not session.execution_path().is_dir():
            raise ChangeSessionError("change session execution workspace is missing")
        return session

    @classmethod
    def snapshot_commit(cls, source: Path, *, parent_commit: str, label: str) -> str:
        tree = cls.capture_tree(source)
        return cls._run(
            source,
            "-c", "user.name=Harness ChangeSession",
            "-c", "user.email=harness@localhost",
            "-c", "commit.gpgsign=false",
            "commit-tree", tree, "-p", parent_commit, "-m", f"harness {label}",
        ).strip()

    @classmethod
    def capture_tree(cls, source: Path) -> str:
        """Return a tree object for the current filesystem without touching its index."""
        source = source.expanduser().resolve()
        head = cls._run(source, "rev-parse", "HEAD").strip()
        fd, index_name = tempfile.mkstemp(prefix="harness-change-index-", suffix=".idx")
        os.close(fd)
        try:
            os.unlink(index_name)
            env = {"GIT_INDEX_FILE": index_name}
            cls._run(source, "read-tree", head, env=env)
            exclusions = [f":(glob,exclude)**/{name}/**" for name in _HARNESS_METADATA_DIRS]
            cls._run(source, "add", "-A", "--", ".", *exclusions, env=env)
            return cls._run(source, "write-tree", env=env).strip()
        finally:
            try:
                os.unlink(index_name)
            except OSError:
                pass

    def execution_path(self, worktree: Path | None = None) -> Path:
        root = (worktree or self.execution_worktree).resolve()
        candidate = (root / self.execution_relative).resolve()
        if not candidate.is_relative_to(root):
            raise ChangeSessionError("change session execution path escapes its worktree")
        return candidate

    def changed_files(self) -> set[str]:
        result_commit = self._result_commit("changed files")
        output = self._run(
            self.execution_worktree,
            "diff", "--name-only", "-z", self.worker_base_commit, result_commit,
        )
        return {path.replace("\\", "/") for path in output.split("\0") if path}

    def diff(self) -> str:
        """Return the complete worker patch relative to its durable baseline."""
        result_commit = self._result_commit("diff")
        return self._run(
            self.execution_worktree,
            "diff", "--no-ext-diff", "--binary", "--full-index",
            self.worker_base_commit, result_commit,
        )

    def export_goal_patch(self) -> str:
        """Return the Goal delta while excluding harness runtime metadata."""
        result_commit = self._result_commit("terminal result")
        return self._run(
            self.execution_worktree,
            "diff", "--no-ext-diff", "--binary", "--full-index",
            self.worker_base_commit, result_commit,
        )

    def _result_commit(self, label: str) -> str:
        return self.snapshot_commit(
            self.execution_worktree,
            parent_commit=self.worker_base_commit,
            label=f"{label} {self.session_id}",
        )

    def prepare_integration(self, *, source_execution_workspace: str | Path) -> ChangeIntegration:
        """Create a clean three-way merge candidate without touching main."""
        source_execution = Path(source_execution_workspace).expanduser().resolve()
        goal_commit = self.snapshot_commit(
            self.execution_worktree,
            parent_commit=self.worker_base_commit,
            label=f"result {self.session_id}",
        )
        main_commit = self.snapshot_commit(
            self.repository_root,
            parent_commit=self.worker_base_commit,
            label=f"main {self.session_id}",
        )
        integration_path = self.execution_worktree.parent / f"{self.session_id}-merge-{uuid.uuid4().hex[:8]}"
        try:
            self._run(self.repository_root, "worktree", "add", "--detach", str(integration_path), main_commit)
            integration = ChangeIntegration(self, integration_path.resolve(), main_commit, "")
            self._provision_runtime_dependencies(source_execution, integration.execution_workspace)
            result = subprocess.run(
                [
                    "git", "-c", "user.name=Harness ChangeSession",
                    "-c", "user.email=harness@localhost",
                    "-c", "commit.gpgsign=false",
                    "merge", "--no-commit", "--no-ff", goal_commit,
                ],
                cwd=str(integration.worktree), check=False, capture_output=True,
                text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=30,
            )
            if result.returncode != 0:
                files = self._run(integration.worktree, "diff", "--name-only", "--diff-filter=U")
                detail = (result.stderr or result.stdout).strip()
                integration.remove()
                raise ChangeSessionError(
                    "change session merge conflict"
                    + (f" in {', '.join(files.splitlines()[:12])}" if files.strip() else "")
                    + (f": {detail[:500]}" if detail else "")
                )
            self._run(integration.worktree, "add", "-A")
            tree = self._run(integration.worktree, "write-tree").strip()
            integration.result_commit = self._run(
                integration.worktree,
                "-c", "user.name=Harness ChangeSession",
                "-c", "user.email=harness@localhost",
                "-c", "commit.gpgsign=false",
                "commit-tree", tree, "-p", main_commit, "-p", goal_commit,
                "-m", f"harness integration {self.session_id}",
            ).strip()
            return integration
        except Exception:
            if integration_path.exists():
                try:
                    self._remove_worktree(integration_path)
                except ChangeSessionError:
                    pass
            raise

    def merge_to_main(self) -> str:
        """Compatibility helper for callers that do not need integration tests."""
        try:
            integration = self.prepare_integration(source_execution_workspace=self.execution_path())
        except ChangeSessionError:
            return "conflict"
        try:
            return "merged" if integration.publish() == "published" else "conflict"
        finally:
            integration.remove()

    def commit_tree(self, commit: str) -> str:
        return self._run(self.repository_root, "rev-parse", f"{commit}^{{tree}}").strip()

    def _apply_patch(self, patch: bytes, *, check_only: bool = False) -> bool:
        args = ["git", "apply", "--binary"]
        if check_only:
            args.append("--check")
        args.append("-")
        try:
            result = subprocess.run(
                args, cwd=str(self.repository_root), input=patch,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    @classmethod
    def _provision_runtime_dependencies(cls, source: Path, target: Path) -> None:
        """Copy local runtimes so verification cannot mutate shared caches."""
        for name in ("node_modules", ".venv", "venv"):
            dependency = source / name
            destination = target / name
            if not dependency.is_dir() or destination.exists():
                continue
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", "--", f"{name}/.harness-runtime-probe"],
                cwd=str(target), check=False, capture_output=True, stdin=subprocess.DEVNULL,
            )
            if ignored.returncode != 0:
                raise ChangeSessionError(f"cannot isolate {name} because it is not ignored by Git")
            try:
                shutil.copytree(dependency, destination, symlinks=False)
            except OSError as exc:
                try:
                    shutil.rmtree(destination)
                except OSError:
                    pass
                raise ChangeSessionError(f"failed to copy {name} for isolated Goal") from exc

    @staticmethod
    def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=str(cwd), check=False, capture_output=True,
                text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                timeout=30, env={**os.environ, **(env or {})},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChangeSessionError(f"git command unavailable: git {' '.join(args)}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ChangeSessionError(f"git {' '.join(args)} failed: {detail[:500]}")
        return result.stdout

    @staticmethod
    def _run_bytes(cwd: Path, *args: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", *args], cwd=str(cwd), check=False, capture_output=True,
                stdin=subprocess.DEVNULL, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChangeSessionError(f"git command unavailable: git {' '.join(args)}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise ChangeSessionError(f"git {' '.join(args)} failed: {detail[:500]}")
        return result.stdout

    @classmethod
    def _remove_worktree_from(cls, repository_root: Path, worktree: Path) -> None:
        cls._run(repository_root, "worktree", "remove", "--force", str(worktree))

    def _remove_worktree(self, worktree: Path) -> None:
        self._remove_worktree_from(self.repository_root, worktree)

    def remove(self, *, force: bool = True) -> None:
        if force:
            self._remove_worktree(self.execution_worktree)
            return
        self._run(self.repository_root, "worktree", "remove", str(self.execution_worktree))

    def status(self) -> str:
        return self._run(self.execution_worktree, "status", "--short")
