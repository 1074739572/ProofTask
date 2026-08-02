"""Path completion and readline integration tests."""

from __future__ import annotations

import builtins
import os

from harness.path_completion import ReadlinePathCompleter, complete_paths


def test_at_path_completion_supports_chinese_and_spaces(tmp_path):
    (tmp_path / "中文 目录").mkdir()

    assert complete_paths("请查看 @中文", cwd=tmp_path) == [
        "请查看 @中文 目录" + os.sep
    ]


def test_open_completion_returns_directories_only(tmp_path):
    (tmp_path / "project folder").mkdir()
    (tmp_path / "project.txt").write_text("x", encoding="utf-8")

    assert complete_paths("/open project", cwd=tmp_path) == [
        "/open project folder" + os.sep
    ]


def test_completion_preserves_windows_separator(tmp_path):
    parent = tmp_path / "parent"
    (parent / "child").mkdir(parents=True)

    assert complete_paths(r"@parent\ch", cwd=tmp_path) == [
        "@parent\\child\\"
    ]


def test_completion_clamps_cursor_out_of_bounds(tmp_path):
    (tmp_path / "alpha").mkdir()

    assert complete_paths("@al", 999, cwd=tmp_path) == ["@alpha" + os.sep]
    assert complete_paths("@al", -10, cwd=tmp_path) == []


class FakeReadline:
    def __init__(self, line: str) -> None:
        self.line = line

    def get_line_buffer(self) -> str:
        return self.line

    def get_endidx(self) -> int:
        return len(self.line)


def test_readline_completer_returns_none_out_of_range(tmp_path, monkeypatch):
    (tmp_path / "alpha").mkdir()
    monkeypatch.chdir(tmp_path)
    completer = ReadlinePathCompleter(FakeReadline("@al"))

    assert completer("", 0) == "@alpha" + os.sep
    assert completer("", 1) is None
    assert completer("", -1) is None


def test_prompt_input_restores_original_completer(monkeypatch):
    import readline
    from harness.ui import prompt_input

    original = object()
    state = {"completer": original, "delims": " old ", "hook": None}
    monkeypatch.setattr(prompt_input, "READLINE_AVAILABLE", True)
    monkeypatch.setattr(readline, "get_completer", lambda: state["completer"])
    monkeypatch.setattr(readline, "set_completer", lambda value: state.__setitem__("completer", value))
    monkeypatch.setattr(readline, "get_completer_delims", lambda: state["delims"])
    monkeypatch.setattr(readline, "set_completer_delims", lambda value: state.__setitem__("delims", value))
    monkeypatch.setattr(readline, "parse_and_bind", lambda _value: None)
    monkeypatch.setattr(readline, "set_startup_hook", lambda value=None: state.__setitem__("hook", value))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "/open demo")

    assert prompt_input.read_cli_query(prompt="> ") == "/open demo"
    assert state["completer"] is original
    assert state["delims"] == " old "
