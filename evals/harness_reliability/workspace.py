"""Copy a fixture into an isolated workspace and prepare variant files.

Each run gets its own copy under the run directory; nothing in the real
repo or in the fixtures directory is ever modified.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from evals.harness_reliability.types import HarnessVariant, ReliabilityTask

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def fixture_path(task: ReliabilityTask) -> Path:
    return FIXTURES_DIR / task.fixture


def _force_rmtree(path: Path) -> None:
    """Remove a tree even if it contains read-only files (git objects on Windows)."""
    if not path.exists():
        return
    for root, _dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.chmod(Path(root) / name, stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,  # see oracle.py: open stdin pipe hangs python
    )


def prepare_workspace(task: ReliabilityTask, variant: HarnessVariant, run_dir: Path) -> Path:
    """Copy fixture to ``run_dir/workspace`` and shape it for the variant.

    - baseline:      remove HARNESS.md (no project instructions at all)
    - instructions:  keep HARNESS.md (project instructions only)
    - structured:    keep HARNESS.md + add progress.md and feature_list.json
    """
    workspace = run_dir / "workspace"
    _force_rmtree(workspace)
    shutil.copytree(fixture_path(task), workspace)

    harness_md = workspace / "HARNESS.md"
    if variant.id == "baseline" and harness_md.exists():
        harness_md.unlink()

    # Keep git status clean of runtime artifacts (pytest __pycache__ etc.)
    # so the oracle's scope check only sees real agent edits.
    (workspace / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8"
    )

    if variant.progress_state:
        (workspace / "progress.md").write_text(
            "# Progress\n\n"
            "## Current state\n"
            "- No work started yet.\n"
            "## Next step\n"
            "1. Implement the requested feature(s); see feature_list.json.\n",
            encoding="utf-8",
        )
        (workspace / "feature_list.json").write_text(
            '[\n'
            '  {\n'
            '    "id": "F001",\n'
            '    "behavior": "All requested functionality works end-to-end",\n'
            '    "verification": "python -m pytest tests -q",\n'
            '    "state": "failing"\n'
            '  }\n'
            ']\n',
            encoding="utf-8",
        )

    # Turn the copy into a tiny git repo so we can diff what the agent changed.
    _git("init", "-q", cwd=workspace)
    _git("add", "-A", cwd=workspace)
    _git("commit", "-q", "-m", "fixture baseline", cwd=workspace)
    return workspace
