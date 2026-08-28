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


def test_status_uses_provider_prompt_tokens_when_available():
    from types import SimpleNamespace

    from harness.event_stream import _status_payload
    from harness.usage.context import record_prompt_tokens

    binding = SimpleNamespace(session_id="context-session")
    record_prompt_tokens(binding.session_id, 123_456)

    payload = _status_payload({}, binding, [])

    assert payload["ctx_tokens"] == 123_456


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
    assert payload["candidates"][0] == "src" + os.sep


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


# ---------- instant slash commands (model/mode/effort switches) ----------

def test_instant_model_switch_emits_session_status(monkeypatch):
    """TUI: /model switch must push a session_status carrying the new model.

    Regression: instant slash commands only emitted a log note, so the TUI's
    bottom model/mode label (fed by session_status) stayed stale until the
    next real user turn.
    """
    from harness import models as models_mod
    from harness.event_stream import _run_instant_slash_command

    catalog = models_mod.list_models()
    assert len(catalog) >= 2, "need >=2 configured models for this test"
    first, second = catalog[0]["id"], catalog[1]["id"]

    # No real API keys in tests — skip the key gate.
    monkeypatch.setattr(models_mod, "resolve_api_key", lambda provider: True)
    models_mod.set_model(first)

    emitted: list[tuple] = []

    def fake_emit(event_type: str, **payload):
        emitted.append((event_type, payload))

    monkeypatch.setattr("harness.event_stream.emit", fake_emit)
    _run_instant_slash_command(f"/model {second}", {}, [], None, running=False)

    statuses = [(t, p) for (t, p) in emitted if t == "session_status"]
    assert statuses, "expected a session_status event after /model switch"
    assert statuses[-1][1]["model"] == second, (
        f"session_status model should be {second}, got {statuses[-1][1]['model']!r}"
    )
    assert models_mod.get_model() == second


def test_instant_model_switch_preserves_running_flag(monkeypatch):
    """session_status pushed by an instant switch must keep the running flag."""
    from harness import models as models_mod
    from harness.event_stream import _run_instant_slash_command

    catalog = models_mod.list_models()
    first = catalog[0]["id"]

    monkeypatch.setattr(models_mod, "resolve_api_key", lambda provider: True)
    models_mod.set_model(first)

    emitted: list[tuple] = []

    def fake_emit(event_type: str, **payload):
        emitted.append((event_type, payload))

    monkeypatch.setattr("harness.event_stream.emit", fake_emit)
    _run_instant_slash_command(f"/model {first}", {}, [], None, running=True)

    statuses = [(t, p) for (t, p) in emitted if t == "session_status"]
    assert statuses, "expected a session_status event after /model switch"
    assert statuses[-1][1]["running"] is True, "busy agent must not look idle"


def test_completion_request_empty_text_no_crash(fake_workdir):
    payload = _handle_completion_request({"text": "", "request_id": "r5"})
    assert payload["candidates"] == []


def test_draft_answer_requires_an_actual_unanswered_question(tmp_path, monkeypatch):
    from harness.event_stream import (
        _active_goal_draft_stage,
        _is_goal_background_command,
        _is_goal_draft_answer,
    )
    from harness.goal.draft import GoalDraft

    draft = GoalDraft(
        id="draft-1", target="improve input", verification="pytest -q",
        verification_source="test", status="clarifying", stage="intake",
    )
    monkeypatch.setattr("harness.goal.draft.load_draft", lambda *_args, **_kwargs: draft)

    assert not _is_goal_draft_answer("ordinary chat")
    assert _active_goal_draft_stage() == "intake"

    draft.questions = ["Which scope?"]
    assert not _is_goal_draft_answer("per user")
    assert not _is_goal_draft_answer("per user", goal_context=False)
    assert _is_goal_draft_answer("per user", goal_context=True)
    assert _is_goal_background_command("per user", goal_context=True)
    assert _is_goal_background_command("/goal answer per user")
    draft.status = "discovering"
    draft.stage = "discovering"
    assert not _is_goal_draft_answer("per user", goal_context=True)
    assert _active_goal_draft_stage() == "discovering"


def test_user_turn_only_consumes_draft_answer_with_goal_context(monkeypatch):
    from harness.event_stream import _run_user_turn
    from harness.goal.draft import GoalDraft

    draft = GoalDraft(
        id="draft-context",
        target="improve input",
        verification="pytest -q",
        verification_source="test",
        status="clarifying",
        stage="intake",
        questions=["Which scope?"],
    )
    emitted = []
    answers = []
    monkeypatch.setattr("harness.goal.draft.load_draft", lambda *_args, **_kwargs: draft)
    monkeypatch.setattr("harness.goal.runner.is_goal_running", lambda: False)
    monkeypatch.setattr("harness.rag.file_mode.is_file_mode", lambda: True)
    monkeypatch.setattr("harness.rag.file_mode.handle_file_mode_turn", lambda text: f"chat:{text}")
    monkeypatch.setattr(
        "harness.goal.commands.handle_goal_draft_answer",
        lambda text: answers.append(text) or f"draft:{text}",
    )
    monkeypatch.setattr(
        "harness.event_stream.emit",
        lambda event_type, **payload: emitted.append((event_type, payload)),
    )

    _run_user_turn("ordinary chat", [], {}, None, goal_context=False)
    assert answers == []
    assert ("assistant_message", {"text": "chat:ordinary chat"}) in emitted

    emitted.clear()
    _run_user_turn("only src", [], {}, None, goal_context=True)
    assert answers == ["only src"]
    assert ("assistant_message", {"text": "draft:only src"}) in emitted
