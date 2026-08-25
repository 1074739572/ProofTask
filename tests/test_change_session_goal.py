from __future__ import annotations

import subprocess

from harness.change_session import ChangeSession


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
