"""Regression tests for explicit new chats and TUI session replay."""

from __future__ import annotations

import json
from pathlib import Path


def _patch_session_paths(monkeypatch, tmp_path: Path) -> Path:
    project = tmp_path / ".project"
    project.mkdir()
    monkeypatch.setattr("harness.settings.PROJECT_DIR", project)
    monkeypatch.setattr("harness.project.session_registry.PROJECT_DIR", project)
    monkeypatch.setattr("harness.project.session_registry.SESSIONS_DIR", project / "sessions")
    monkeypatch.setattr("harness.project.session_registry.ACTIVE_SESSION_PATH", project / "active_session.json")
    monkeypatch.setattr("harness.project.session_registry.LEGACY_SESSION_PATH", project / "session.jsonl")
    monkeypatch.setattr("harness.project.session_registry.LEGACY_SESSION_META_PATH", project / "session.meta.json")
    monkeypatch.setattr("harness.project.session_registry.LEGACY_TODOS_PATH", project / "todos.json")
    monkeypatch.setattr("harness.project.session_store.PROJECT_DIR", project)
    monkeypatch.setattr("harness.project.session_store.HISTORY_PATH", project / "history.json")
    monkeypatch.setattr("harness.project.session.HISTORY_PATH", project / "history.json")
    return project


def test_new_alias_starts_clean_session_and_keeps_old_history(monkeypatch, tmp_path):
    project = _patch_session_paths(monkeypatch, tmp_path)
    from harness.project.resume import is_new_session_command, new_session_title, start_new_session
    from harness.project.session_registry import create_session
    from harness.project.session_store import append_checkpoint, load_session_messages

    old = create_session(title="old chat")
    append_checkpoint(
        [{"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"}],
        binding=old,
    )
    messages = load_session_messages(binding=old) or []
    new_binding, note = start_new_session(
        messages,
        binding=old,
        title=new_session_title("new fresh topic"),
    )

    assert is_new_session_command("new")
    assert is_new_session_command("/new fresh topic")
    assert not is_new_session_command("renew this")
    assert new_binding.session_id != old.session_id
    assert messages == []
    assert "fresh topic" in note
    assert load_session_messages(binding=old) == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    assert json.loads((project / "active_session.json").read_text(encoding="utf-8"))["id"] == new_binding.session_id


def test_history_replay_replace_emits_empty_transcript(monkeypatch):
    from harness import event_stream

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_stream, "emit", lambda kind, **payload: emitted.append((kind, payload)))
    event_stream._emit_history_replay([], replace=True, new_session=True, session_id="sid-new")

    assert emitted == [
        (
            "history_replay",
            {
                "messages": [],
                "truncated": False,
                "replace": True,
                "new_session": True,
                "session_id": "sid-new",
            },
        )
    ]


def test_tui_new_command_is_routed_without_an_llm_turn(monkeypatch, tmp_path):
    project = _patch_session_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("harness.goal.runner.is_goal_running", lambda: False)
    from harness import event_stream
    from harness.project.session_registry import create_session

    binding = create_session(title="old")
    history = [{"role": "user", "content": "before"}]
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_stream, "emit", lambda kind, **payload: emitted.append((kind, payload)))
    _note, new_binding = event_stream._handle_slash_command("new", history, binding)

    assert history == []
    assert new_binding.session_id != binding.session_id
    replay = [payload for kind, payload in emitted if kind == "history_replay"]
    assert replay and replay[-1]["replace"] is True and replay[-1]["new_session"] is True
    assert replay[-1]["messages"] == []
