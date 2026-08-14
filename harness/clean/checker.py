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
from collections.abc import Collection
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
    # Machine-readable failures let orchestration route a failed clean gate to
    # the feature that actually needs attention instead of guessing from text.
    failures: list[dict[str, str]] = field(default_factory=list)


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


def _check_feature_consistency(
    workspace: Path,
    feature_ids: Collection[str] | None = None,
) -> CleanCheck:
    from harness.features import corrupt_feature_files, feature_is_stale, list_features

    # ``None`` retains the diagnostic's historical workspace-wide behavior.
    # A supplied collection scopes only feature-state consistency; temp-file
    # and git-worktree checks remain workspace-wide.
    scope = None if feature_ids is None else {str(feature_id) for feature_id in feature_ids}
    bad: list[str] = []
    failures: list[dict[str, str]] = []
    features = list_features(workspace=workspace)
    if scope is not None:
        features = [feature for feature in features if feature.id in scope]

    for feature in features:
        if feature.state == "passing" and not feature.evidence:
            bad.append(f"{feature.id} ({feature.name}): passing without evidence")
            failures.append({"feature_id": feature.id, "reason": "missing_evidence"})
        elif feature.state == "passing" and feature_is_stale(feature):
            bad.append(f"{feature.id} ({feature.name}): passing but code changed (stale)")
            failures.append({"feature_id": feature.id, "reason": "stale"})

    corrupt = corrupt_feature_files(workspace)
    if scope is not None:
        corrupt = [filename for filename in corrupt if Path(filename).stem in scope]
    if corrupt:
        bad.append("corrupt feature files: " + ", ".join(corrupt))
        failures.extend(
            {
                "feature_id": Path(filename).stem,
                "path": filename,
                "reason": "corrupt_feature_file",
            }
            for filename in corrupt
        )

    if scope is not None:
        known_ids = {feature.id for feature in features}
        corrupt_ids = {Path(filename).stem for filename in corrupt}
        for feature_id in sorted(scope - known_ids - corrupt_ids):
            bad.append(f"{feature_id}: feature file is missing")
            failures.append({"feature_id": feature_id, "reason": "missing_feature"})

    detail = "; ".join(bad) if bad else ""
    return CleanCheck(
        "feature_state_consistency",
        (
            "passing features carry fresh evidence; no corrupt files"
            if scope is None
            else "selected passing features carry fresh evidence; selected files are valid"
        ),
        ok=not bad,
        detail=detail,
        failures=failures,
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
    feature_ids: Collection[str] | None = None,
    include_feature_consistency: bool = True,
) -> CleanReport:
    """Run clean-state checks for a workspace (default: active workspace).

    ``feature_ids=None`` checks every feature in the workspace, which is the
    right default for diagnostics. Supplying ids scopes only the feature-state
    consistency gate to the owning task or goal, so stale historical evidence
    cannot block unrelated work from completing.
    """
    mode = mode or clean_mode()
    if mode == "off":
        return CleanReport(mode="off", checks=[])

    workspace = workspace or get_workspace_paths().root
    checks = [_check_temp_artifacts(workspace)]
    if include_feature_consistency:
        checks.append(_check_feature_consistency(workspace, feature_ids=feature_ids))
    checks.append(_check_uncommitted(workspace))
    return CleanReport(mode=mode, checks=checks)
