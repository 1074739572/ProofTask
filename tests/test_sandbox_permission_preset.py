"""Regression tests for the sandbox permission policy preset.

Pins the acceptance contract of the named ``sandbox`` policy in
``harness.permissions.config``: read-only tools allow, file writes ask with
protected paths denied, bash is limited to safe read-only prefixes, and
unknown policy names fall back to the default rules without raising.
"""

from __future__ import annotations

import json

import pytest

from harness.permissions.config import DEFAULT_PERMISSIONS, load_permission_rules
from harness.permissions.engine import evaluate_permission

SANDBOX_READ_ONLY_TOOLS = (
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


def _load_named_policy(tmp_path, monkeypatch, name: str) -> dict:
    config_path = tmp_path / "permissions.json"
    config_path.write_text(json.dumps({"permission": name}), encoding="utf-8")
    monkeypatch.setattr(
        "harness.permissions.config.get_permissions_config_path",
        lambda: config_path,
    )
    return load_permission_rules()


def _effect(rules: dict, tool: str, tool_input: dict | None = None) -> str:
    return evaluate_permission(
        tool,
        tool_input or {},
        rules=rules,
        include_saved=False,
    ).effect


def test_sandbox_policy_resolves_read_only_tools_to_allow(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert all(_effect(rules, tool) == "allow" for tool in SANDBOX_READ_ONLY_TOOLS)


def test_sandbox_policy_resolves_write_file_and_edit_file_to_ask(
    tmp_path, monkeypatch
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "write_file", {"path": "notes.txt"}) == "ask"
    assert _effect(rules, "edit_file", {"path": "notes.txt"}) == "ask"


def test_sandbox_policy_asks_for_external_directory_and_unknown_tools(
    tmp_path, monkeypatch
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "external_directory") == "ask"
    assert _effect(rules, "some_unknown_tool") == "ask"


@pytest.mark.parametrize(
    "command",
    (
        "rm -rf .",
        "rm important.txt",
        "sudo apt update",
        "shutdown -h now",
        "reboot",
    ),
)
def test_sandbox_policy_denies_destructive_bash_commands(
    tmp_path, monkeypatch, command
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "bash", {"command": command}) == "deny"


@pytest.mark.parametrize(
    "command",
    (
        "dir .",
        "type README.md",
        "where python",
        "git status",
        "git diff --stat",
        "git log -3",
    ),
)
def test_sandbox_policy_allows_safe_read_only_bash_prefixes(
    tmp_path, monkeypatch, command
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "bash", {"command": command}) == "allow"


def test_sandbox_policy_asks_for_arbitrary_bash_command(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    decision = evaluate_permission(
        "bash",
        {"command": "python run.py"},
        rules=rules,
        include_saved=False,
    )
    assert decision.effect == "ask"


@pytest.mark.parametrize(
    ("tool", "path"),
    (
        ("write_file", ".features/f001.json"),
        ("edit_file", ".features/f001.json"),
        ("write_file", ".project/goal.md"),
        ("edit_file", ".project/goal.json"),
    ),
)
def test_sandbox_policy_denies_protected_write_paths(tmp_path, monkeypatch, tool, path):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, tool, {"path": path}) == "deny"


def test_unknown_policy_name_falls_back_to_default_rules(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "not-a-real-policy")

    assert rules == DEFAULT_PERMISSIONS
