"""Clean-state checks (L4) — deterministic, zero-LLM, zero side effects.

Checks (all report-only; nothing is auto-deleted in this layer — see the
reliability plan: only clean what the agent itself produced, never touch user
files, and prefer reporting + letting the caller decide):

- ``temp_artifacts``          (HARD) leftover temp/editor files in the workspace
- ``feature_state_consistency`` (HARD) a passing feature without evidence is a
                              data-inconsistency bug (normal flow can't produce it)
- ``uncommitted_changes``     (SOFT) git worktree has uncommitted changes —
                              informational for handover, not a blocker

Modes (``HARNESS_CLEAN_MODE`` env, default ``warn``):

- ``off``     — no checks at all
- ``warn``    — run checks, report, never block
- ``enforce`` — run checks; hard failures block task completion
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from harness.settings import get_workspace_paths

CLEAN_MODES = ("off", "warn", "enforce")
DEFAULT_CLEAN_MODE = "warn"
ENV_CLEAN_MODE = "HARNESS_CLEAN_MODE"

#: Checks whose failure blocks completion in enforce mode.
HARD_CHECKS = frozenset({"temp_artifacts", "feature_state_consistency"})

#: Harness runtime dirs / normal dependency dirs — never treated as garbage.
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".project",
        ".features",
        ".tasks",
        ".worktrees",
        ".mailboxes",
        ".memory",
        ".transcripts",
        ".task_outputs",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        ".local",
    }
)

#: File-name patterns treated as leftover artifacts.
_TEMP_PATTERNS = (
    "*.tmp",
    "*.bak",
    "*.orig",
    "*.swp",
    "*.swo",
    "*~",
    ".DS_Store",
    "Thumbs.db",
)


def clean_mode() -> str:
    """Active clean-state mode from env (validated, falls back to default)."""
    value = os.environ.get(ENV_CLEAN_MODE, DEFAULT_CLEAN_MODE).strip().lower()
    return value if value in CLEAN_MODES else DEFAULT_CLEAN_MODE


@dataclass
class CleanCheck:
    id: str
    label: str
    ok: bool
    detail: str = ""


@dataclass
class CleanReport:
    mode: str
    checks: list[CleanCheck] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[CleanCheck]:
        return [c for c in self.checks if not c.ok and c.id in HARD_CHECKS]

    @property
    def ok(self) -> bool:
        """True when no HARD check failed (soft issues don't block)."""
        return not self.hard_failures

    def summary(self) -> str:
        lines = [f"[clean:{self.mode}]"]
        if not self.checks:
            lines.append("  (no checks)")
        for check in self.checks:
            mark = "PASS" if check.ok else "FAIL"
            lines.append(f"  {mark} {check.id}: {check.label}")
            if check.detail:
                lines.append(f"       {check.detail}")
        return "\n".join(lines)


# --- individual checks -------------------------------------------------------

def _iter_workspace_files(workspace: Path):
    """Yield non-ignored files under the workspace (bounded depth walk)."""
    for root, dirs, files in os.walk(workspace):
        root_path = Path(root)
        rel = root_path.relative_to(workspace)
        # Prune ignored dirs at any depth (os.walk already descended past
        # pruned ancestors, so only current name + ancestors matter).
        dirs[:] = [
            d
            for d in dirs
            if d not in _IGNORED_DIRS
            and not any(part in _IGNORED_DIRS for part in rel.parts)
        ]
        for name in files:
            yield root_path / name


def _check_temp_artifacts(workspace: Path) -> CleanCheck:
    import fnmatch

    found: list[str] = []
    for path in _iter_workspace_files(workspace):
        name = path.name
        if any(fnmatch.fnmatch(name, pat) for pat in _TEMP_PATTERNS):
            found.append(str(path.relative_to(workspace)))
    found = sorted(found)[:20]
    detail = ", ".join(found) if found else ""
    return CleanCheck(
        "temp_artifacts",
        "no leftover temp/editor files",
        ok=not found,
        detail=detail,
    )


def _check_feature_consistency(workspace: Path) -> CleanCheck:
    from harness.features import corrupt_feature_files, feature_is_stale, list_features

    bad: list[str] = []
    for feature in list_features(workspace=workspace):
        if feature.state == "passing" and not feature.evidence:
            bad.append(f"{feature.id} ({feature.name}): passing without evidence")
        elif feature.state == "passing" and feature_is_stale(feature):
            bad.append(f"{feature.id} ({feature.name}): passing but code changed (stale)")
    corrupt = corrupt_feature_files(workspace)
    if corrupt:
        bad.append("corrupt feature files: " + ", ".join(corrupt))
    detail = "; ".join(bad) if bad else ""
    return CleanCheck(
        "feature_state_consistency",
        "passing features carry fresh evidence; no corrupt files",
        ok=not bad,
        detail=detail,
    )


def _git_porcelain(workspace: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []  # not a git repo
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _check_uncommitted(workspace: Path) -> CleanCheck:
    lines = _git_porcelain(workspace)
    if not lines:
        return CleanCheck("uncommitted_changes", "git worktree clean (or not a repo)", True)
    detail = f"{len(lines)} change(s), e.g. {lines[0][:80]}"
    return CleanCheck(
        "uncommitted_changes",
        "git worktree clean",
        ok=False,
        detail=detail,
    )


# --- entry point -------------------------------------------------------------

def run_clean_check(
    workspace: Path | None = None,
    *,
    mode: str | None = None,
) -> CleanReport:
    """Run clean-state checks for a workspace (default: active workspace)."""
    mode = mode or clean_mode()
    if mode == "off":
        return CleanReport(mode="off", checks=[])

    workspace = workspace or get_workspace_paths().root
    checks = [
        _check_temp_artifacts(workspace),
        _check_feature_consistency(workspace),
        _check_uncommitted(workspace),
    ]
    return CleanReport(mode=mode, checks=checks)
