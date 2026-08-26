from __future__ import annotations

import subprocess

from harness.change_session import ChangeSession
from harness.workspace_lock import WorkspaceMutationLock


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


def test_change_session_diff_and_changed_files_do_not_modify_worker_index(tmp_path):
    root = init_repo(tmp_path)
    session = ChangeSession.create(root, session_id="index-readonly")
    try:
        (session.execution_worktree / "tracked.txt").write_text("agent\n", encoding="utf-8")

        assert git(session.execution_worktree, "diff", "--cached", "--name-only") == ""
        assert session.changed_files() == {"tracked.txt"}
        assert "agent" in session.diff()
        assert git(session.execution_worktree, "diff", "--cached", "--name-only") == ""
    finally:
        session.remove()


def test_change_session_preserves_untracked_directories_and_renames(tmp_path):
    root = init_repo(tmp_path)
    (root / "newdir").mkdir()
    (root / "newdir" / "nested.txt").write_text("untracked\n", encoding="utf-8")
    git(root, "mv", "tracked.txt", "renamed.txt")

    session = ChangeSession.create(root, session_id="snapshot-test")
    try:
        assert (session.execution_worktree / "newdir" / "nested.txt").read_text(encoding="utf-8") == "untracked\n"
        assert (session.execution_worktree / "renamed.txt").read_text(encoding="utf-8") == "base\n"
        assert not (session.execution_worktree / "tracked.txt").exists()
    finally:
        session.remove()


def test_attached_session_keeps_the_durable_dirty_baseline_out_of_its_patch(tmp_path):
    root = init_repo(tmp_path)
    (root / "tracked.txt").write_text("user baseline\n", encoding="utf-8")
    session = ChangeSession.create(root, session_id="attached-baseline")
    try:
        (session.execution_worktree / "agent.txt").write_text("goal\n", encoding="utf-8")
        attached = ChangeSession.attach(
            root,
            session.execution_worktree,
            base_commit=session.base_commit,
            worker_base_commit=session.worker_base_commit,
            session_id=session.session_id,
        )
        assert "user baseline" not in attached.diff()
        assert attached.merge_to_main() == "merged"
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "user baseline\n"
        assert (root / "agent.txt").read_text(encoding="utf-8") == "goal\n"
    finally:
        session.remove()


def test_change_session_merges_a_goal_continuation_of_an_initially_dirty_file(tmp_path):
    root = init_repo(tmp_path)
    (root / "tracked.txt").write_text("user baseline\n", encoding="utf-8")
    session = ChangeSession.create(root, session_id="dirty-continuation")
    try:
        (session.execution_worktree / "tracked.txt").write_text("goal continuation\n", encoding="utf-8")
        assert session.merge_to_main() == "merged"
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "goal continuation\n"
        assert git(root, "diff", "--cached", "--name-only") == ""
    finally:
        session.remove()


def test_integration_merges_non_overlapping_concurrent_edits_without_staging(tmp_path):
    root = init_repo(tmp_path)
    (root / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git(root, "add", "tracked.txt")
    git(root, "commit", "-m", "three lines")
    session = ChangeSession.create(root, session_id="integration-merge")
    try:
        (session.execution_worktree / "tracked.txt").write_text("goal one\ntwo\nthree\n", encoding="utf-8")
        (root / "tracked.txt").write_text("one\ntwo\nmain three\n", encoding="utf-8")
        integration = session.prepare_integration(source_execution_workspace=root)
        try:
            assert integration.execution_workspace == integration.worktree
            assert (integration.worktree / "tracked.txt").read_text(encoding="utf-8") == "goal one\ntwo\nmain three\n"
            assert integration.publish() == "published"
        finally:
            integration.remove()
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "goal one\ntwo\nmain three\n"
        assert git(root, "diff", "--cached", "--name-only") == ""
    finally:
        session.remove()


def test_integration_conflict_never_writes_the_main_worktree(tmp_path):
    root = init_repo(tmp_path)
    session = ChangeSession.create(root, session_id="integration-conflict")
    try:
        (session.execution_worktree / "tracked.txt").write_text("goal\n", encoding="utf-8")
        (root / "tracked.txt").write_text("main\n", encoding="utf-8")
        try:
            session.prepare_integration(source_execution_workspace=root)
            raise AssertionError("expected merge conflict")
        except Exception as exc:
            assert "conflict" in str(exc)
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "main\n"
        assert git(root, "diff", "--cached", "--name-only") == ""
    finally:
        session.remove()


def test_integration_publish_waits_for_the_shared_main_workspace_lock(tmp_path):
    root = init_repo(tmp_path)
    session = ChangeSession.create(root, session_id="publish-lock")
    try:
        (session.execution_worktree / "tracked.txt").write_text("agent\n", encoding="utf-8")
        integration = session.prepare_integration(source_execution_workspace=root)
        try:
            lock = WorkspaceMutationLock(root, purpose="test")
            assert lock.acquire()
            try:
                assert integration.publish() == "main_locked"
                assert (root / "tracked.txt").read_text(encoding="utf-8") == "base\n"
            finally:
                lock.release()
            assert integration.publish() == "published"
        finally:
            integration.remove()
    finally:
        session.remove()


def test_change_session_copies_ignored_node_dependencies_without_sharing_them(tmp_path):
    root = init_repo(tmp_path)
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "ignore runtime dependencies")
    dependency = root / "node_modules" / "package" / "index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("module.exports = 'main';\n", encoding="utf-8")

    session = ChangeSession.create(root, session_id="copied-dependencies")
    try:
        copied = session.execution_worktree / "node_modules" / "package" / "index.js"
        assert copied.read_text(encoding="utf-8") == "module.exports = 'main';\n"
        copied.write_text("module.exports = 'worker';\n", encoding="utf-8")
        assert dependency.read_text(encoding="utf-8") == "module.exports = 'main';\n"
    finally:
        session.remove()


def test_change_session_maps_a_nested_workspace_relative_to_the_git_root(tmp_path):
    root = init_repo(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    (nested / "app.py").write_text("app\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "nested project")

    session = ChangeSession.create(nested, execution_workspace=nested, session_id="nested-workspace")
    try:
        assert session.execution_relative == "nested"
        assert session.execution_path() == session.execution_worktree / "nested"
        assert (session.execution_path() / "app.py").read_text(encoding="utf-8") == "app\n"
    finally:
        session.remove()


def test_change_session_attach_missing_worktree_raises(tmp_path):
    root = init_repo(tmp_path)
    try:
        ChangeSession.attach(
            root,
            root / ".project" / "worktrees" / "missing",
            base_commit="x",
            worker_base_commit="x",
            session_id="s",
        )
        raise AssertionError("attach must reject a missing worktree")
    except Exception as exc:
        assert "missing" in str(exc)
