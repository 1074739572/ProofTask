"""W-series: clean-state & handover checks (L4).

Zero-LLM assertions on ``harness.clean`` + the ``complete_task`` gate:

- W001: off mode runs no checks
- W002: temp artifacts are detected
- W003: a clean workspace passes all hard checks
- W004: enforce mode BLOCKS task completion on hard failure
- W005: warn mode reports but still completes
- W006: passing-feature-without-evidence inconsistency is detected
- W007: git worktree with uncommitted changes is soft-info only (never blocks)

All tests run in temp dirs; the real workspace is untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from evals.types import EvalCase

ROOT = Path(__file__).resolve().parent.parent.parent


def _tmp_workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    ws = Path(tmp.name) / "proj"
    ws.mkdir()
    return tmp, ws


def _fake_paths(ws: Path):
    from harness.settings import _derive_paths

    return _derive_paths(ws)


# --- W001 / W002 / W003 / W006 / W007: checker behavior -----------------------

def case_w001_off_runs_no_checks() -> None:
    from harness.clean import run_clean_check

    tmp, ws = _tmp_workspace()
    try:
        (ws / "junk.tmp").write_text("x", encoding="utf-8")
        report = run_clean_check(workspace=ws, mode="off")
        assert report.mode == "off"
        assert report.checks == []
        assert report.ok
    finally:
        tmp.cleanup()


def case_w002_temp_artifacts_detected() -> None:
    from harness.clean import run_clean_check

    tmp, ws = _tmp_workspace()
    try:
        (ws / "app.py").write_text("print(1)", encoding="utf-8")
        (ws / "backup.tmp").write_text("x", encoding="utf-8")
        report = run_clean_check(workspace=ws, mode="warn")
        temp_check = next(c for c in report.checks if c.id == "temp_artifacts")
        assert not temp_check.ok
        assert "backup.tmp" in temp_check.detail
        assert not report.ok  # hard check failed
    finally:
        tmp.cleanup()


def case_w003_clean_workspace_passes() -> None:
    from harness.clean import run_clean_check

    tmp, ws = _tmp_workspace()
    try:
        (ws / "app.py").write_text("print(1)", encoding="utf-8")
        (ws / "tests").mkdir()
        (ws / "tests" / "test_app.py").write_text("def test_x(): pass", encoding="utf-8")
        report = run_clean_check(workspace=ws, mode="enforce")
        assert report.ok, report.summary()
        for check in report.checks:
            assert check.ok, f"{check.id} should pass: {check.detail}"
    finally:
        tmp.cleanup()


def case_w006_feature_consistency_detected() -> None:
    """A passing feature without evidence is a data bug — detected."""
    from harness.clean import run_clean_check

    tmp, ws = _tmp_workspace()
    try:
        # Write the corrupt state directly (the API can't produce it by design).
        feat_dir = ws / ".features"
        feat_dir.mkdir()
        (feat_dir / "feat_bad.json").write_text(
            json.dumps(
                {
                    "id": "feat_bad",
                    "name": "corrupt",
                    "behavior": "x",
                    "verification": "pytest -q",
                    "state": "passing",
                    "workspace": str(ws),
                    "evidence": [],
                    "attempts": 0,
                    "last_error": None,
                    "created_at": 0,
                    "updated_at": 0,
                    "completed_at": 0,
                    "task_id": None,
                }
            ),
            encoding="utf-8",
        )
        report = run_clean_check(workspace=ws, mode="warn")
        check = next(c for c in report.checks if c.id == "feature_state_consistency")
        assert not check.ok
        assert "feat_bad" in check.detail
        assert not report.ok
    finally:
        tmp.cleanup()


def case_w007_uncommitted_is_soft() -> None:
    """Uncommitted git changes are informational — never a hard blocker."""
    from harness.clean import run_clean_check

    tmp, ws = _tmp_workspace()
    try:
        subprocess.run(
            ["git", "init", "-q", str(ws)],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        (ws / "app.py").write_text("print(1)", encoding="utf-8")
        report = run_clean_check(workspace=ws, mode="enforce")
        uncommitted = next(c for c in report.checks if c.id == "uncommitted_changes")
        assert not uncommitted.ok  # worktree is dirty
        assert report.ok  # ...but not a hard failure: completion not blocked
    finally:
        tmp.cleanup()


# --- W004 / W005: complete_task gate -----------------------------------------

def _setup_task(tmp_root: Path) -> str:
    import harness.tasks as tasks_mod

    with mock.patch.object(tasks_mod, "TASKS_DIR", tmp_root / "tasks"):
        tasks_mod.create_task("subject", "desc")
        task = tasks_mod.list_tasks()[0]
        tasks_mod.claim_task(task.id, owner="test")
        return task.id


def case_w004_enforce_blocks_completion() -> None:
    import harness.tasks as tasks_mod

    tmp, ws = _tmp_workspace()
    try:
        task_id = _setup_task(Path(tmp.name))
        (ws / "junk.tmp").write_text("x", encoding="utf-8")

        with (
            mock.patch.object(tasks_mod, "TASKS_DIR", Path(tmp.name) / "tasks"),
            mock.patch(
                "harness.clean.checker.get_workspace_paths",
                return_value=_fake_paths(ws),
            ),
            mock.patch.dict(os.environ, {"HARNESS_CLEAN_MODE": "enforce"}),
        ):
            msg = tasks_mod.complete_task(task_id)
            # Task must still be in_progress and NOT archived.
            assert tasks_mod.load_task(task_id).status == "in_progress"
            archive_dir = Path(tmp.name) / "tasks" / "archive"
            assert not archive_dir.exists() or not list(archive_dir.glob("*.json"))

        assert "Cannot complete" in msg
    finally:
        tmp.cleanup()


def case_w005_warn_reports_but_completes() -> None:
    import harness.tasks as tasks_mod

    tmp, ws = _tmp_workspace()
    try:
        task_id = _setup_task(Path(tmp.name))
        (ws / "junk.tmp").write_text("x", encoding="utf-8")

        with (
            mock.patch.object(tasks_mod, "TASKS_DIR", Path(tmp.name) / "tasks"),
            mock.patch(
                "harness.clean.checker.get_workspace_paths",
                return_value=_fake_paths(ws),
            ),
            mock.patch.dict(os.environ, {"HARNESS_CLEAN_MODE": "warn"}),
        ):
            msg = tasks_mod.complete_task(task_id)
            assert "Completed" in msg
            assert tasks_mod.load_task(task_id).status == "completed"
    finally:
        tmp.cleanup()


# --- W008/W009: completion gate on linked features ---------------------------

def case_w008_blocks_unverified_task() -> None:
    """A Task with a binding cannot complete before its own proof passes."""
    import harness.tasks as tasks_mod

    tmp, ws = _tmp_workspace()
    try:
        task_id = _setup_task(Path(tmp.name))

        with (
            mock.patch.object(tasks_mod, "TASKS_DIR", Path(tmp.name) / "tasks"),
            mock.patch(
                "harness.clean.checker.get_workspace_paths",
                return_value=_fake_paths(ws),
            ),
            mock.patch(
                "harness.settings.get_workspace_paths",
                return_value=_fake_paths(ws),
            ),
            mock.patch.dict(os.environ, {"HARNESS_CLEAN_MODE": "enforce"}),
        ):
            task = tasks_mod.load_task(task_id)
            task.verification_spec = {"source": "generated", "command": "pytest -q", "selectors": ["tests/test_x.py::test_x"], "collected_count": 1}
            tasks_mod.save_task(task)
            msg = tasks_mod.complete_task(task_id)
            assert "Cannot complete" in msg
            assert "has not passed" in msg
            assert tasks_mod.load_task(task_id).status == "in_progress"
    finally:
        tmp.cleanup()


def case_w009_verified_task_completes() -> None:
    """A Task with zero-exit evidence completes normally."""
    import harness.tasks as tasks_mod

    tmp, ws = _tmp_workspace()
    try:
        task_id = _setup_task(Path(tmp.name))

        with (
            mock.patch.object(tasks_mod, "TASKS_DIR", Path(tmp.name) / "tasks"),
            mock.patch(
                "harness.clean.checker.get_workspace_paths",
                return_value=_fake_paths(ws),
            ),
            mock.patch(
                "harness.settings.get_workspace_paths",
                return_value=_fake_paths(ws),
            ),
            mock.patch.dict(os.environ, {"HARNESS_CLEAN_MODE": "enforce"}),
        ):
            task = tasks_mod.load_task(task_id)
            task.verification_spec = {"source": "generated", "command": "pytest -q", "selectors": ["tests/test_x.py::test_x"], "collected_count": 1}
            tasks_mod.save_task(task)
            tasks_mod.set_task_verification_result(task_id, passed=True, evidence={"command": "pytest -q", "exit_code": 0})
            msg = tasks_mod.complete_task(task_id)
            assert "Completed" in msg
            assert tasks_mod.load_task(task_id).status == "completed"
    finally:
        tmp.cleanup()


def case_w010_corrupt_feature_blocks_enforce() -> None:
    """A corrupt feature file is a hard clean failure in enforce mode."""
    from harness.clean import run_clean_check

    tmp, ws = _tmp_workspace()
    try:
        (ws / ".features").mkdir()
        (ws / ".features" / "feat_bad.json").write_text("{broken", encoding="utf-8")
        report = run_clean_check(ws, mode="enforce")
        assert not report.ok
        check = next(c for c in report.checks if c.id == "feature_state_consistency")
        assert not check.ok
        assert "feat_bad.json" in check.detail
    finally:
        tmp.cleanup()


CASES = [
    EvalCase(
        "w001.off_no_checks",
        "W001: clean checks disabled in off mode",
        "clean",
        case_w001_off_runs_no_checks,
    ),
    EvalCase(
        "w002.temp_artifacts_detected",
        "W002: leftover temp artifacts are detected",
        "clean",
        case_w002_temp_artifacts_detected,
    ),
    EvalCase(
        "w003.clean_passes",
        "W003: clean workspace passes all hard checks",
        "clean",
        case_w003_clean_workspace_passes,
    ),
    EvalCase(
        "w004.enforce_blocks_completion",
        "W004: enforce mode blocks task completion on hard failure",
        "clean",
        case_w004_enforce_blocks_completion,
    ),
    EvalCase(
        "w005.warn_reports_but_completes",
        "W005: warn mode reports but still completes",
        "clean",
        case_w005_warn_reports_but_completes,
    ),
    EvalCase(
        "w006.feature_consistency_detected",
        "W006: passing-feature-without-evidence inconsistency detected",
        "clean",
        case_w006_feature_consistency_detected,
    ),
    EvalCase(
        "w007.uncommitted_is_soft",
        "W007: uncommitted git changes are soft info, never block",
        "clean",
        case_w007_uncommitted_is_soft,
    ),
    EvalCase(
        "w008.blocks_unverified_task",
        "W008: Task completion requires its own passing proof",
        "clean",
        case_w008_blocks_unverified_task,
    ),
    EvalCase(
        "w009.verified_task_completes",
        "W009: verified Task completion allowed",
        "clean",
        case_w009_verified_task_completes,
    ),
    EvalCase(
        "w010.corrupt_feature_blocks_enforce",
        "W010: corrupt feature files are a hard clean failure",
        "clean",
        case_w010_corrupt_feature_blocks_enforce,
    ),
]
