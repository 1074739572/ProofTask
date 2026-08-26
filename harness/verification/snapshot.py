"""Code snapshot for verification evidence (L3).

A passing verdict is only meaningful while the code it verified is the code
that still exists. ``capture_code_snapshot`` fingerprints a workspace at a
point in time (git HEAD + working-tree state); evidence stores it, and the
clean/gate layers compare it with the current snapshot to detect stale
"passing" states after the code changed.

Non-git workspaces return ``""`` — staleness cannot be checked there
(document limitation; the feature store still enforces evidence presence).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_bytes(*args: str, cwd: Path) -> bytes | None:
    """Binary-safe git output for workspace content fingerprints."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


_HARNESS_METADATA_ROOTS = frozenset(
    {
        ".project",
        ".tasks",
        ".features",
        ".transcripts",
        ".task_outputs",
        ".mailboxes",
        ".memory",
        ".worktrees",
    }
)


def _is_harness_metadata(path: Path) -> bool:
    return bool(path.parts and path.parts[0] in _HARNESS_METADATA_ROOTS)


def _workspace_git_path(raw_path: bytes, workspace: Path, repository: Path) -> tuple[Path, str] | None:
    """Resolve a Git path relative to a possibly nested execution workspace."""
    value = raw_path.decode("utf-8", errors="surrogateescape")
    relative = Path(value)
    workspace = workspace.resolve()
    repository = repository.resolve()
    workspace_candidate = (workspace / relative).resolve()
    repository_candidate = (repository / relative).resolve()
    candidates = (
        (workspace_candidate, repository_candidate)
        if workspace_candidate.is_file() or not repository_candidate.is_file()
        else (repository_candidate, workspace_candidate)
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return candidate, candidate.relative_to(workspace).as_posix()
        except ValueError:
            continue
    # Keep a deleted path under the workspace visible to the dirty-file gate.
    try:
        return repository_candidate, repository_candidate.relative_to(workspace).as_posix()
    except ValueError:
        return None


def capture_code_snapshot(workspace: str | Path | None = None) -> str:
    """Return a content-aware workspace fingerprint, or ``''`` outside git.

    ``git status`` alone only identifies a dirty path. Editing that same dirty
    path again leaves its porcelain output unchanged, which made verification
    mutation checks, stale evidence, and goal no-progress detection incorrect.
    The fingerprint therefore hashes the full tracked diff plus untracked file
    names and bytes, excluding harness runtime metadata.
    """
    ws = Path(workspace).expanduser().resolve() if workspace else None
    if ws is None:
        from harness.settings import get_workdir

        ws = get_workdir()
    head = _git("rev-parse", "HEAD", cwd=ws)
    if head is None:
        return ""
    tracked_diff = _git_bytes("diff", "--no-ext-diff", "--binary", "HEAD", cwd=ws)
    untracked = _git_bytes("ls-files", "--others", "--exclude-standard", "-z", cwd=ws)
    if tracked_diff is None or untracked is None:
        return ""

    digest = hashlib.sha256()
    digest.update(tracked_diff)
    for raw_path in untracked.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        if _is_harness_metadata(relative):
            continue
        candidate = ws / relative
        if not candidate.is_file():
            continue
        digest.update(b"\0path:")
        digest.update(raw_path)
        try:
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            # A concurrent edit/deletion is still a code-state change. Make
            # the snapshot conservative instead of accepting stale evidence.
            digest.update(b"\0unreadable")
    return f"{head.strip()[:12]}:{digest.hexdigest()[:16]}"


def capture_dirty_file_hashes(workspace: str | Path | None = None) -> dict[str, str]:
    """Hash files already changed at Task start for later scope filtering."""
    ws = Path(workspace).expanduser().resolve() if workspace else None
    if ws is None:
        from harness.settings import get_workdir

        ws = get_workdir()
    changed = _git_bytes("diff", "--name-only", "-z", "HEAD", cwd=ws)
    untracked = _git_bytes("ls-files", "--others", "--exclude-standard", "-z", cwd=ws)
    if changed is None or untracked is None:
        return {}
    root = _git("rev-parse", "--show-toplevel", cwd=ws)
    repository = Path(root.strip()).resolve() if root else ws
    result: dict[str, str] = {}
    for raw_path in (*changed.split(b"\0"), *untracked.split(b"\0")):
        if not raw_path:
            continue
        resolved = _workspace_git_path(raw_path, ws, repository)
        if resolved is None:
            continue
        path, key = resolved
        if _is_harness_metadata(Path(key)):
            continue
        if not path.is_file():
            result[key] = "<missing>"
            continue
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            result[key] = "<unreadable>"
        else:
            result[key] = digest.hexdigest()
    return result


def capture_dirty_file_contents(workspace: str | Path | None = None) -> dict[str, str]:
    """Capture pre-Task dirty file text for task-local diff attribution.

    Hashes tell us *which* files were already dirty, but cannot distinguish an
    earlier recovered change from a later Task edit to the same file.  Keep a
    bounded text baseline so an evaluator can review only the latter.  Binary,
    unreadable, and harness-metadata files are intentionally omitted.
    """
    ws = Path(workspace).expanduser().resolve() if workspace else None
    if ws is None:
        from harness.settings import get_workdir

        ws = get_workdir()
    changed = _git_bytes("diff", "--name-only", "-z", "HEAD", cwd=ws)
    untracked = _git_bytes("ls-files", "--others", "--exclude-standard", "-z", cwd=ws)
    if changed is None or untracked is None:
        return {}
    root = _git("rev-parse", "--show-toplevel", cwd=ws)
    repository = Path(root.strip()).resolve() if root else ws
    result: dict[str, str] = {}
    for raw_path in (*changed.split(b"\0"), *untracked.split(b"\0")):
        if not raw_path:
            continue
        resolved = _workspace_git_path(raw_path, ws, repository)
        if resolved is None:
            continue
        path, key = resolved
        if _is_harness_metadata(Path(key)) or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Task records are durable metadata.  A bounded baseline prevents a
        # large pre-existing generated file from bloating every Goal checkpoint.
        if len(content) <= 200_000:
            result[key] = content
    return result


def workspace_has_changes_since(workspace: str | Path | None, snapshot: str) -> bool:
    """True when the workspace is a git repo, the snapshot is a git snapshot,
    and the current state differs from it. Non-git -> False (cannot check)."""
    if not snapshot or ":" not in snapshot:
        return False
    current = capture_code_snapshot(workspace)
    if not current:
        return False
    return current != snapshot
