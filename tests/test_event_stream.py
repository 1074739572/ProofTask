"""Event-stream protocol: completion_request and /open workspace switch."""

import os
import sys
from pathlib import Path

import pytest

from harness.event_stream import _handle_completion_request, _handle_open_workspace
from harness import settings


@pytest.fixture
def fake_workdir(tmp_path, monkeypatch):
    """Point the active workspace (get_workdir) at tmp_path for completion tests."""
    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "switch_workspace", lambda _p: 0)
    monkeypatch.setattr(settings, "get_workdir", lambda: root)
    monkeypatch.setattr(settings, "get_workspace_paths", lambda: settings._derive_paths(root))
    monkeypatch.chdir(root)
    return root


# ---------- /open workspace switch ----------

def test_open_resolves_directory(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    note, cwd, list_mode = _handle_open_workspace(f"/open {target}")
    assert cwd == str(target.resolve())
    assert "Switching workspace" in note
    assert not list_mode


def test_open_resolves_directory_with_spaces(tmp_path):
    target = tmp_path / "中文 project"
    target.mkdir()
    note, cwd, list_mode = _handle_open_workspace(f"/open {target}")
    assert cwd == str(target.resolve())
    assert note
    assert not list_mode


def test_open_missing_directory_returns_error(tmp_path):
    note, cwd, list_mode = _handle_open_workspace(f"/open {tmp_path / 'nope'}")
    assert cwd is None
    assert "Cannot open" in note
    assert not list_mode


def test_open_file_path_rejected(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("x", encoding="utf-8")
    note, cwd, list_mode = _handle_open_workspace(f"/open {file}")
    assert cwd is None
    assert "not a directory" in note
    assert not list_mode


def test_open_without_argument_returns_usage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    note, cwd, list_mode = _handle_open_workspace("/open")
    assert cwd is None
    assert note is None
    assert list_mode


def test_open_numeric_selects_recent_project(tmp_path, monkeypatch):
    from harness import workspace as workspace_mod

    target = tmp_path / "recent"
    target.mkdir()
    projects = [str(target.resolve())]
    monkeypatch.setattr(workspace_mod, "_read_recent_projects", lambda: projects)
    note, cwd, list_mode = _handle_open_workspace("/open 1")
    assert cwd == str(target.resolve())
    assert "Switching workspace" in note
    assert not list_mode


def test_open_numeric_out_of_range(tmp_path, monkeypatch):
    from harness import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "_read_recent_projects", lambda: [])
    note, cwd, list_mode = _handle_open_workspace("/open 3")
    assert cwd is None
    assert "序号超出范围" in note
    assert not list_mode


# ---------- completion_request ----------

def test_completion_request_at_path(fake_workdir):
    (fake_workdir / "src").mkdir()
    (fake_workdir / "src" / "main.py").write_text("", encoding="utf-8")
    payload = _handle_completion_request({"text": "看 @src", "request_id": "r1"})
    assert payload["request_id"] == "r1"
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0] == "看 @src" + os.sep


def test_completion_request_open_directories_only(fake_workdir):
    (fake_workdir / "project folder").mkdir()
    (fake_workdir / "project.txt").write_text("", encoding="utf-8")
    payload = _handle_completion_request({"text": "/open project", "request_id": "r2"})
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0].endswith("project folder" + os.sep)


def test_completion_request_no_match(fake_workdir):
    payload = _handle_completion_request({"text": "@zzz", "cursor": 4, "request_id": "r3"})
    assert payload["candidates"] == []


def test_completion_request_bad_cursor_falls_back(fake_workdir):
    (fake_workdir / "alpha").mkdir()
    payload = _handle_completion_request({"text": "@al", "cursor": "bogus", "request_id": "r4"})
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0].endswith("alpha" + os.sep)


def test_completion_request_empty_text_no_crash(fake_workdir):
    payload = _handle_completion_request({"text": "", "request_id": "r5"})
    assert payload["candidates"] == []
