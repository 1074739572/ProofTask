"""Regression tests for Goal workspace resilience hardening.

Covers three related fixes:

1. ``switch_workspace`` refuses harness metadata directories (``.project``
   etc.) so a stray ``/open repo/.project`` can no longer create a nested
   ``.project/.project`` and trip the Goal drift guard.
2. The ``_drive`` drift guard *pauses* the Goal instead of failing it, so the
   user can switch back to the Goal workspace and ``/goal resume``.
3. ``_rebuild_missing_change_worktree`` re-materializes a deleted isolated
   checkout at its recorded baseline commit — but only for a Goal that never
   made progress.
"""
from pathlib import Path

import pytest

from harness import settings as settings_mod
from harness.goal.models import GoalPhase, GoalStatus, StopReason
from harness.goal.runner import (
    GoalRunner,
    _rebuild_missing_change_worktree,
)
from harness.goal.store import load_goal


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """Point the harness at a throwaway workspace and restore afterwards."""
    original = settings_mod.get_workdir()
    monkeypatch.setattr(settings_mod, "_workspace", original)
    monkeypatch.setattr(settings_mod, "_workspace_generation", 0)
    yield
    settings_mod._workspace = original


def _state(target: str, generation: int):
    from harness.goal.models import GoalState

    state = GoalState.new(target="keep the goal alive", verification="pytest -q", workspace=target)
    state.workspace_generation = generation
    return state


# --- 1. switch_workspace reserved-name guard -----------------------------

def test_switch_workspace_rejects_metadata_directories(tmp_path):
    for name in (".project", ".tasks", ".worktrees", ".mailboxes", ".features"):
        with pytest.raises(ValueError, match="metadata directory"):
            settings_mod.switch_workspace(tmp_path / name)


def test_switch_workspace_still_allows_normal_directories(tmp_path):
    generation = settings_mod.switch_workspace(tmp_path / "regular-project")
    assert generation == 1
    assert settings_mod.get_workdir() == (tmp_path / "regular-project").resolve()


# --- 2. drift guard pauses instead of failing ----------------------------

def test_drive_pauses_instead_of_failing_on_workspace_drift(tmp_path, monkeypatch):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    settings_mod._workspace = other.resolve()

    state = _state(str(workspace.resolve()), generation=1)
    state.status = GoalStatus.RUNNING.value
    state.phase = GoalPhase.PREPARE_TESTS.value
    # The process re-bound another directory after this Goal was resumed.
    settings_mod._workspace_generation = 9

    runner = GoalRunner(state=state, history=[], context={}, binding=None, lease_token=None)
    monkeypatch.setattr(
        GoalRunner, "_step_once",
        lambda self, _state: pytest.fail("_step_once must not run after drift"),
    )

    runner._drive()

    assert state.status == GoalStatus.PAUSED.value
    assert state.phase == GoalPhase.PAUSED.value
    assert state.stop_reason == StopReason.workspace_changed.value
    assert state.last_error == "workspace switched while goal was active"
    assert state.resume_phase == GoalPhase.PREPARE_TESTS.value

    # The checkpoint is durable in the Goal's own workspace and resumable.
    persisted = load_goal(workspace)
    assert persisted is not None
    assert persisted.status == GoalStatus.PAUSED.value
    assert persisted.stop_reason == StopReason.workspace_changed.value


# --- 3. missing change worktree rebuild ----------------------------------

def _worktree_state(tmp_path, worktree: Path):
    state = _state(str(tmp_path.resolve()), generation=1)
    state.change_mode = "worktree"
    state.change_worktree = str(worktree)
    state.change_repository_root = str(tmp_path.resolve())
    state.change_base_commit = "0" * 40
    state.change_baseline_commit = "1" * 40
    state.change_execution_relpath = "."
    state.task_ids = []
    return state


def test_rebuild_noop_when_worktree_present(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state = _worktree_state(tmp_path, worktree)
    assert _rebuild_missing_change_worktree(state) is True


def test_rebuild_refuses_non_worktree_goal(tmp_path):
    state = _state(str(tmp_path.resolve()), generation=1)
    assert _rebuild_missing_change_worktree(state) is False


def test_rebuild_refuses_when_goal_made_progress(tmp_path, monkeypatch):
    state = _worktree_state(tmp_path, tmp_path / "wt")
    state.attempts = 1
    called = []
    monkeypatch.setattr(
        "harness.goal.runner.ChangeSession._run",
        lambda *a, **k: called.append(a) or "",
    )
    assert _rebuild_missing_change_worktree(state) is False
    assert called == [], "no git command may run once the Goal has real progress"


def test_rebuild_recreates_missing_worktree_at_baseline(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    state = _worktree_state(tmp_path, worktree)
    calls = []

    def fake_run(cwd, *args, **kwargs):
        calls.append(args)
        if args[:2] == ("worktree", "add"):
            worktree.mkdir(parents=True)
        return ""

    monkeypatch.setattr("harness.goal.runner.ChangeSession._run", fake_run)

    assert _rebuild_missing_change_worktree(state) is True
    assert worktree.is_dir()
    assert calls[0] == ("worktree", "prune"), "stale registration must be pruned first"
    assert calls[1] == ("worktree", "add", "--detach", str(worktree.resolve()), state.change_baseline_commit)
    events = [entry.get("event") for entry in state.execution_trace]
    assert "change_worktree_rebuilt" in events


def test_rebuild_reports_failure_when_git_refuses(tmp_path, monkeypatch):
    from harness.change_session import ChangeSessionError

    state = _worktree_state(tmp_path, tmp_path / "wt")

    def fake_run(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            raise ChangeSessionError("boom")
        return ""

    monkeypatch.setattr("harness.goal.runner.ChangeSession._run", fake_run)
    assert _rebuild_missing_change_worktree(state) is False
    assert state.execution_trace == [], "failed rebuild must not claim recovery"
