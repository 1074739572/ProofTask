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


def capture_code_snapshot(workspace: str | Path | None = None) -> str:
    """Return a short fingerprint of the workspace code state, or '' if the
    workspace is not a git repository (staleness check unavailable)."""
    ws = Path(workspace).expanduser().resolve() if workspace else None
    if ws is None:
        from harness.settings import get_workdir

        ws = get_workdir()
    head = _git("rev-parse", "HEAD", cwd=ws)
    if head is None:
        return ""
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=ws) or ""
    digest = hashlib.sha1(status.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{head.strip()[:12]}:{digest}"


def workspace_has_changes_since(workspace: str | Path | None, snapshot: str) -> bool:
    """True when the workspace is a git repo, the snapshot is a git snapshot,
    and the current state differs from it. Non-git -> False (cannot check)."""
    if not snapshot or ":" not in snapshot:
        return False
    current = capture_code_snapshot(workspace)
    if not current:
        return False
    return current != snapshot
