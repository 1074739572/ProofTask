from __future__ import annotations

import subprocess
from pathlib import Path

from harness.change_session import ChangeIntegration, ChangeSession, ChangeSessionError
from harness.goal.models import GoalPhase, GoalState
from harness.goal.runner import GoalRunner


def git(cwd, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_goal_change_session_preserves_baseline_but_attributes_only_worker_changes(tmp_path):
    root = tmp_path / "repo"
    project = root / "node_tui"
    project.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (project / "app.ts").write_text("base\n", encoding="utf-8")
    (root / "preexisting.txt").write_text("before\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")

    (root / "preexisting.txt").write_text("user-change\n", encoding="utf-8")
    session = ChangeSession.create(root, session_id="goal-test")
    try:
        assert (session.execution_worktree / "preexisting.txt").read_text(encoding="utf-8") == "user-change\n"
        (session.execution_worktree / "node_tui" / "app.ts").write_text("agent-change\n", encoding="utf-8")
        (root / "external.txt").write_text("other-process\n", encoding="utf-8")
        assert session.changed_files() == {"node_tui/app.ts"}
    finally:
        session.remove()


def _isolated_goal_state(root, session) -> GoalState:
    state = GoalState.new(target="change tracked file", verification="pytest -q", workspace=str(root))
    state.phase = GoalPhase.FULL_VERIFY.value
    state.execution_workspace = str(session.execution_path())
    state.change_mode = "worktree"
    state.change_session_id = session.session_id
    state.change_worktree = str(session.execution_worktree)
    state.change_base_commit = session.base_commit
    state.change_baseline_commit = session.worker_base_commit
    state.change_repository_root = str(session.repository_root)
    state.change_execution_relpath = session.execution_relative
    return state


def _verification_result(passed: bool):
    return type("Result", (), {"passed": passed, "error": None, "exit_code": 0 if passed else 1})()


def _patch_final_verification(monkeypatch, passed: tuple[bool, bool]) -> None:
    import harness.goal.runner as runner_module

    results = iter(_verification_result(value) for value in passed)
    monkeypatch.setattr(runner_module, "save_goal", lambda _state: None)
    monkeypatch.setattr(runner_module, "_emit_goal", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_module, "run_verification", lambda *args, **kwargs: next(results))
    monkeypatch.setattr(
        runner_module,
        "evidence_from_result",
        lambda *args, **kwargs: type("Evidence", (), {"to_dict": lambda self: {"exit_code": 0}})(),
    )


def test_goal_full_verify_publishes_only_after_the_integrated_worktree_passes(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    session = ChangeSession.create(root, session_id="verify-publish")
    try:
        (session.execution_worktree / "tracked.txt").write_text("goal\n", encoding="utf-8")
        state = _isolated_goal_state(root, session)
        _patch_final_verification(monkeypatch, (True, True))

        GoalRunner(state=state, history=[], context={}, binding=None)._full_verify(state)

        assert state.phase == GoalPhase.DONE.value
        assert state.change_merge_state == "published"
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "goal\n"
        assert not session.execution_worktree.exists()
    finally:
        if session.execution_worktree.exists():
            session.remove()


def test_goal_full_verify_does_not_publish_when_integrated_worktree_fails(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    session = ChangeSession.create(root, session_id="verify-no-publish")
    try:
        (session.execution_worktree / "tracked.txt").write_text("goal\n", encoding="utf-8")
        state = _isolated_goal_state(root, session)
        _patch_final_verification(monkeypatch, (True, False))

        GoalRunner(state=state, history=[], context={}, binding=None)._full_verify(state)

        assert state.phase == GoalPhase.PAUSED.value
        assert state.stop_reason == "merge_verification_failed"
        assert state.change_merge_state == "verification_failed"
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "base\n"
        assert session.execution_worktree.is_dir()
    finally:
        session.remove()


def test_goal_full_verify_pauses_when_verified_publish_raises(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    session = ChangeSession.create(root, session_id="verify-publish-error")
    try:
        (session.execution_worktree / "tracked.txt").write_text("goal\n", encoding="utf-8")
        state = _isolated_goal_state(root, session)
        _patch_final_verification(monkeypatch, (True, True))
        monkeypatch.setattr(
            ChangeIntegration,
            "publish",
            lambda _integration: (_ for _ in ()).throw(ChangeSessionError("main index changed")),
        )

        GoalRunner(state=state, history=[], context={}, binding=None)._full_verify(state)

        assert state.phase == GoalPhase.PAUSED.value
        assert state.stop_reason == "merge_conflict"
        assert state.change_merge_state == "publish_unavailable"
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "base\n"
        assert session.execution_worktree.is_dir()
    finally:
        session.remove()


def test_terminal_goal_archives_its_isolated_patch_before_removing_the_worktree(tmp_path):
    import harness.goal.runner as runner_module

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    session = ChangeSession.create(root, session_id="terminal-archive")
    (session.execution_worktree / "tracked.txt").write_text("goal\n", encoding="utf-8")
    state = _isolated_goal_state(root, session)

    runner_module._finalize_terminal_change_session(state)

    archive = root / ".project" / "goal-history" / f"{state.id}.patch"
    assert archive.is_file()
    assert "goal" in archive.read_text(encoding="utf-8")
    assert state.change_archive_path == str(archive)
    assert state.change_worktree == ""
    assert not session.execution_worktree.exists()


def test_terminal_status_recovers_a_crashed_goal_worktree_cleanup(tmp_path, monkeypatch):
    import harness.goal.runner as runner_module

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    session = ChangeSession.create(root, session_id="crashed-terminal-cleanup")
    (session.execution_worktree / "tracked.txt").write_text("goal\n", encoding="utf-8")
    state = _isolated_goal_state(root, session)
    state.phase = GoalPhase.FAILED.value
    state.status = "failed"
    monkeypatch.setattr(runner_module, "load_goal", lambda: state)
    monkeypatch.setattr(runner_module, "_emit_goal", lambda *args, **kwargs: None)

    runner_module.emit_current_goal_status(include_terminal=True)

    assert state.change_worktree == ""
    assert Path(state.change_archive_path).is_file()
    assert not session.execution_worktree.exists()
