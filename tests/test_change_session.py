from __future__ import annotations

import subprocess

from harness.change_session import ChangeSession


def git(cwd, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout


def init_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def test_change_session_uses_isolated_worktree_and_ignores_main_workspace_changes(tmp_path):
    root = init_repo(tmp_path)
    session = ChangeSession.create(root, session_id="session-test")
    try:
        assert session.execution_worktree.is_dir()
        assert session.execution_worktree != root
        assert session.base_commit == git(root, "rev-parse", "HEAD").strip()

        (root / "external.txt").write_text("outside\n", encoding="utf-8")
        (session.execution_worktree / "agent.txt").write_text("inside\n", encoding="utf-8")

        assert session.changed_files() == {"agent.txt"}
        assert "external.txt" not in session.changed_files()
    finally:
        session.remove()


def test_change_session_exports_only_its_worktree_patch(tmp_path):
    root = init_repo(tmp_path)
    session = ChangeSession.create(root, session_id="patch-test")
    try:
        (root / "external.txt").write_text("outside\n", encoding="utf-8")
        (session.execution_worktree / "tracked.txt").write_text("agent\n", encoding="utf-8")

        patch = session.diff()

        assert "tracked.txt" in patch
        assert "agent" in patch
        assert "external.txt" not in patch
    finally:
        session.remove()


def test_change_session_merges_worker_changes_without_overwriting_external_changes(tmp_path):
    root = init_repo(tmp_path)
    session = ChangeSession.create(root, session_id="merge-test")
    try:
        (session.execution_worktree / "agent.txt").write_text("agent\n", encoding="utf-8")
        (root / "external.txt").write_text("external\n", encoding="utf-8")

        assert session.merge_to_main() == "merged"
        assert (root / "agent.txt").read_text(encoding="utf-8") == "agent\n"
        assert (root / "external.txt").read_text(encoding="utf-8") == "external\n"
    finally:
        session.remove()


def test_change_session_reports_conflict_and_keeps_main_content(tmp_path):
    root = init_repo(tmp_path)
    session = ChangeSession.create(root, session_id="conflict-test")
    try:
        (session.execution_worktree / "tracked.txt").write_text("agent\n", encoding="utf-8")
        (root / "tracked.txt").write_text("external\n", encoding="utf-8")

        assert session.merge_to_main() == "conflict"
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "external\n"
        assert session.execution_worktree.is_dir()
    finally:
        session.remove()


def test_change_session_attach_missing_worktree_raises(tmp_path):
    root = init_repo(tmp_path)
    try:
        ChangeSession.attach(root, root / ".project" / "worktrees" / "missing", base_commit="x", session_id="s")
        raise AssertionError("attach must reject a missing worktree")
    except Exception as exc:
        assert "missing" in str(exc)
