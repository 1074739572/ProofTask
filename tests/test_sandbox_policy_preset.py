"""Regression coverage for the named sandbox permission policy preset."""

from __future__ import annotations

import json

import pytest

from harness.permissions.config import DEFAULT_PERMISSIONS, load_permission_rules
from harness.permissions.engine import evaluate_permission


def _load_named_policy(tmp_path, monkeypatch, policy_name: str):
    config_path = tmp_path / "permissions.json"
    config_path.write_text(
        json.dumps({"permission": policy_name}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "harness.permissions.config.get_permissions_config_path",
        lambda: config_path,
    )
    return load_permission_rules()


def _effect(tool: str, tool_input: dict, rules) -> str:
    return evaluate_permission(
        tool,
        tool_input,
        rules=rules,
        include_saved=False,
    ).effect


def test_sandbox_policy_allows_read_only_tools_and_asks_for_other_tools(
    tmp_path, monkeypatch
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    read_only_tools = (
        "read_file",
        "glob",
        "load_skill",
        "rag_search",
        "rag_status",
        "project_status",
        "list_tasks",
        "get_task",
        "web_search",
        "todo_write",
        "compact",
        "list_features",
        "check_inbox",
        "list_crons",
    )
    assert {_effect(tool, {}, rules) for tool in read_only_tools} == {"allow"}
    assert _effect("write_file", {"path": "notes.txt"}, rules) == "ask"
    assert _effect("edit_file", {"path": "notes.txt"}, rules) == "ask"
    assert _effect("external_directory", {}, rules) == "ask"
    assert _effect("unrecognized_tool", {}, rules) == "ask"


@pytest.mark.parametrize(
    ("tool", "path"),
    (
        ("write_file", ".features/f001.json"),
        ("edit_file", ".features/f001.json"),
        ("write_file", ".project/goal.md"),
        ("edit_file", ".project/goal.md"),
    ),
)
def test_sandbox_policy_denies_protected_write_paths(
    tmp_path, monkeypatch, tool, path
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(tool, {"path": path}, rules) == "deny"


@pytest.mark.parametrize(
    "command",
    (
        "rm -rf .",
        "sudo apt update",
        "shutdown now",
        "reboot",
    ),
)
def test_sandbox_policy_denies_destructive_bash_commands(
    tmp_path, monkeypatch, command
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect("bash", {"command": command}, rules) == "deny"


@pytest.mark.parametrize(
    "command",
    (
        "dir .",
        "type README.md",
        "where python",
        "git status",
        "git diff --stat",
        "git log -1",
    ),
)
def test_sandbox_policy_allows_safe_read_only_bash_prefixes(
    tmp_path, monkeypatch, command
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect("bash", {"command": command}, rules) == "allow"


def test_sandbox_policy_asks_for_arbitrary_bash_commands(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect("bash", {"command": "python run.py"}, rules) == "ask"


def test_unknown_policy_name_falls_back_to_default_rules(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "not-a-real-policy")

    assert rules == DEFAULT_PERMISSIONS
