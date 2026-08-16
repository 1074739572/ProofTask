"""Regression tests for the named sandbox permission policy."""

from __future__ import annotations

import json

from harness.permissions.config import DEFAULT_PERMISSIONS, load_permission_rules
from harness.permissions.engine import evaluate_permission


_READ_ONLY_TOOLS = (
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


def _load_named_policy(tmp_path, monkeypatch, name: str):
    config_path = tmp_path / "permissions.json"
    config_path.write_text(json.dumps({"permission": name}), encoding="utf-8")
    monkeypatch.setattr(
        "harness.permissions.config.get_permissions_config_path",
        lambda: config_path,
    )
    return load_permission_rules()


def _effect(rules, tool: str, tool_input: dict | None = None) -> str:
    return evaluate_permission(
        tool,
        tool_input or {},
        rules=rules,
        include_saved=False,
    ).effect


def test_sandbox_policy_allows_read_only_tools_and_asks_for_writes(
    tmp_path, monkeypatch
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert {_effect(rules, tool) for tool in _READ_ONLY_TOOLS} == {"allow"}
    assert _effect(rules, "write_file", {"path": "notes.txt"}) == "ask"
    assert _effect(rules, "edit_file", {"path": "notes.txt"}) == "ask"
    assert _effect(rules, "external_directory") == "ask"
    assert _effect(rules, "unknown_tool") == "ask"


def test_sandbox_policy_denies_destructive_bash_commands(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    for command in ("rm -rf .", "sudo apt update", "shutdown now", "reboot"):
        assert _effect(rules, "bash", {"command": command}) == "deny"


def test_sandbox_policy_allows_safe_read_only_bash_prefixes(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    for command in (
        "dir .",
        "type README.md",
        "where python",
        "git status",
        "git diff --stat",
        "git log -1",
    ):
        assert _effect(rules, "bash", {"command": command}) == "allow"


def test_sandbox_policy_asks_for_arbitrary_bash_command(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "bash", {"command": "python run.py"}) == "ask"


def test_sandbox_policy_denies_protected_write_paths(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    for tool in ("write_file", "edit_file"):
        assert _effect(rules, tool, {"path": ".features/f001.json"}) == "deny"
        assert _effect(rules, tool, {"path": ".project/goal.json"}) == "deny"


def test_unknown_policy_name_falls_back_to_default_rules(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "does-not-exist")

    assert rules == DEFAULT_PERMISSIONS
