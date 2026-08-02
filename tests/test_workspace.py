"""In-process workspace switching: /open semantics and state isolation."""

import os
from pathlib import Path

import pytest

from harness.workspace import switch_workspace
from harness import workspace as workspace_mod


@pytest.fixture
def isolated_recent(tmp_path, monkeypatch):
    """Redirect recent-projects storage to a temp file for each test."""
    store = tmp_path / "recent_projects.json"
    monkeypatch.setattr(workspace_mod, "RECENT_PROJECTS_PATH", store)
    return store


def test_switch_workspace_rejects_missing(tmp_path):
    ok, note, binding = switch_workspace(tmp_path / "nope")
    assert not ok
    assert "Cannot open" in note
    assert binding is None


def test_switch_workspace_rejects_file(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("x", encoding="utf-8")
    ok, note, binding = switch_workspace(file)
    assert not ok
    assert "not a directory" in note
    assert binding is None


def test_switch_workspace_flips_workdir_and_generation(tmp_path):
    from harness.settings import get_workdir, workspace_generation

    before = workspace_generation()
    target = tmp_path / "proj"
    target.mkdir()
    ok, note, binding = switch_workspace(target)
    assert ok
    assert "Switched workspace" in note
    assert get_workdir() == target.resolve()
    assert workspace_generation() == before + 1
    assert binding is not None


def test_switch_workspace_creates_dot_dirs(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    switch_workspace(target)
    for name in (".tasks", ".project", ".mailboxes", ".worktrees"):
        assert (target / name).is_dir(), name


def test_switch_workspace_creates_session_in_target(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    ok, note, binding = switch_workspace(target)
    assert ok
    # session store lives under the new workspace's .project/sessions
    session_root = target / ".project" / "sessions" / binding.session_id
    assert session_root.is_dir()
    assert binding.session_jsonl.parent == session_root


def test_switch_workspace_refreshes_rag_paths(tmp_path):
    from harness.rag import config as rag_config

    target = tmp_path / "proj"
    target.mkdir()
    switch_workspace(target)
    assert rag_config.INDEX_DIR == (target / ".rag" / "index").resolve() or str(rag_config.INDEX_DIR).endswith(
        os.sep + ".rag" + os.sep + "index"
    )


# ---------- recent projects ----------

def test_record_recent_project_newest_first(isolated_recent, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    workspace_mod.record_recent_project(a)
    workspace_mod.record_recent_project(b)
    projects = workspace_mod.list_recent_projects()
    assert [p["path"] for p in projects] == [str(b.resolve()), str(a.resolve())]


def test_record_recent_project_dedupes(isolated_recent, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    workspace_mod.record_recent_project(a)
    workspace_mod.record_recent_project(a)
    assert len(workspace_mod.list_recent_projects()) == 1


def test_list_recent_projects_marks_current(isolated_recent, tmp_path, monkeypatch):
    from harness import settings as settings_mod

    a = tmp_path / "a"
    a.mkdir()
    workspace_mod.record_recent_project(a)
    monkeypatch.setattr(settings_mod, "_workspace", a.resolve())
    rows = workspace_mod.list_recent_projects()
    assert len(rows) == 1
    assert rows[0]["current"] is True


def test_record_recent_project_caps_limit(isolated_recent, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_mod, "RECENT_PROJECTS_LIMIT", 3)
    for i in range(5):
        d = tmp_path / f"p{i}"
        d.mkdir()
        workspace_mod.record_recent_project(d)
    assert len(workspace_mod.list_recent_projects()) == 3


def test_record_recent_project_persists_across_reads(isolated_recent, tmp_path):
    a = tmp_path / "persisted"
    a.mkdir()
    workspace_mod.record_recent_project(a)
    # simulate a fresh call reading from disk
    assert isolated_recent.exists()
    rows = workspace_mod.list_recent_projects()
    assert rows and rows[0]["path"] == str(a.resolve())


# ---------- prompt advertises the active workspace ----------

def test_system_prompt_advertises_new_workspace_after_switch(tmp_path, monkeypatch):
    """Regression: after /open, the agent's system prompt must point at the NEW
    directory, not the startup one — otherwise the model reports the old cwd."""
    from harness.prompts import assemble_static_system_prompt

    target = tmp_path / "newproj"
    target.mkdir()
    old = str(Path.cwd())

    ok, note, _ = switch_workspace(target)
    assert ok

    static = assemble_static_system_prompt()
    assert f"Working directory: {target.resolve()}" in static
    assert old not in static


def test_bash_cwd_follows_switched_workspace(tmp_path):
    from harness.tools.filesystem import run_bash

    target = tmp_path / "bashproj"
    target.mkdir()
    (target / "sentinel.txt").write_text("x", encoding="utf-8")
    ok, note, _ = switch_workspace(target)
    assert ok
    # pwd must report the switched workspace (Windows: cd prints the cwd)
    code = "cd" if os.name == "nt" else "pwd"
    output = run_bash(code)
    assert str(target.resolve()) in output


def test_permission_engine_follows_switched_workspace(tmp_path):
    """External-resource detection must use the active workspace, not the
    startup directory, or file tools would mis-classify paths after /open."""
    from harness.permissions.engine import _external_resource_for_path

    target = tmp_path / "permproj"
    target.mkdir()
    ok, note, _ = switch_workspace(target)
    assert ok

    inside = target / "inner.txt"
    inside.write_text("x", encoding="utf-8")
    assert _external_resource_for_path(str(inside)) is None  # inside workspace

    outside = tmp_path / "outer.txt"
    outside.write_text("x", encoding="utf-8")
    assert _external_resource_for_path(str(outside)) is not None  # outside
