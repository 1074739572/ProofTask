"""Regression tests for the Goal ``workspace_changed`` guard.

A TUI re-binds the workspace it already shows (startup, ``/open`` on the same
path, a second window).  ``workspace_generation`` advances anyway, so a resumed
Goal used to fail ~20 ms into its first round with ``workspace_changed`` even
though its project root never moved.
"""
from pathlib import Path

import pytest

from harness import settings as settings_mod
from harness.goal.models import GoalPhase, GoalStatus, StopReason
from harness.goal.runner import goal_workspace_drift, resume_goal
from harness.goal.store import load_goal, save_goal


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


def test_same_root_rebind_is_not_workspace_drift(tmp_path, monkeypatch):
    """Re-opening the identical project must not be reported as a switch."""
    settings_mod._workspace = tmp_path.resolve()
    settings_mod._workspace_generation = 1
    state = _state(str(tmp_path.resolve()), 1)

    settings_mod._workspace_generation = 2  # a benign re-bind of the same path

    assert goal_workspace_drift(state) is None


def test_real_workspace_change_is_still_reported(tmp_path):
    """A genuine switch to another project must keep failing the Goal."""
    other = tmp_path / "other"
    other.mkdir()
    settings_mod._workspace = other.resolve()
    state = _state(str(tmp_path.resolve()), 1)

    assert goal_workspace_drift(state) == "workspace switched while goal was active"


def test_relative_workspace_matches_its_absolute_root(tmp_path, monkeypatch):
    """Stored and active roots are compared after resolution, not as strings."""
    settings_mod._workspace = tmp_path.resolve()
    state = _state(str(tmp_path), 0)

    assert goal_workspace_drift(state) is None


def test_resume_rebinds_the_generation_it_was_paused_on(tmp_path, monkeypatch):
    """``resume_goal`` must not fail on its first round after a re-bind."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    settings_mod._workspace = workspace.resolve()
    settings_mod._workspace_generation = 1

    state = _state(str(workspace.resolve()), 1)
    state.status = GoalStatus.PAUSED.value
    state.phase = GoalPhase.PAUSED.value
    state.resume_phase = GoalPhase.PREPARE_TESTS.value
    state.execution_approved = True
    # ``_resume_target`` re-enters INITIALIZE whenever the durable
    # initialization checkpoint is missing, which is unrelated to the
    # generation guard under test.
    state.initialization_complete = True
    save_goal(state)

    # The user re-opens the same directory before resuming.
    settings_mod._workspace_generation = 7

    monkeypatch.setattr("harness.goal.runner.GoalRunner.start", lambda self: None)
    monkeypatch.setattr("harness.goal.runner._reap_runner", lambda: None)
    monkeypatch.setattr("harness.goal.runner.acquire_goal_lease", lambda state: "token")
    monkeypatch.setattr("harness.goal.runner._create_goal_change_session", lambda *a, **k: (None, a[1]))

    try:
        resumed = resume_goal(history=[], context={}, binding=None)
    finally:
        import harness.goal.runner as runner_module

        runner_module._runner = None

    assert resumed.phase == GoalPhase.PREPARE_TESTS.value
    assert resumed.status == GoalStatus.RUNNING.value
    assert resumed.workspace_generation == 7, "resume must adopt the live generation"
    assert goal_workspace_drift(resumed) is None

    persisted = load_goal(workspace)
    assert persisted is not None
    assert persisted.workspace_generation == 7
    assert persisted.stop_reason != StopReason.workspace_changed.value
