"""Regression tests for the named ``sandbox`` permission policy preset.

Pins the acceptance contract for selecting the sandbox policy in the
permission config module: ``load_permission_rules()`` resolves the named
preset, read-only tools evaluate to allow, file writes ask except for
protected workspace paths, bash is limited to safe read-only prefixes while
destructive commands are denied, and unknown policy names fall back to the
default rules without raising.
"""

from __future__ import annotations

import json

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

DESTRUCTIVE_COMMANDS = (
    "rm -rf .",
    "rm important.txt",
    "sudo apt update",
    "shutdown -h now",
    "reboot",
)

SAFE_READ_ONLY_COMMANDS = (
    "dir .",
    "type README.md",
    "where python",
    "git status",
    "git diff --stat",
    "git log -3",
)


def _load_named_policy(tmp_path, monkeypatch, policy_name: str) -> dict:
    config_path = tmp_path / "permissions.json"
    config_path.write_text(json.dumps({"permission": policy_name}), encoding="utf-8")
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


def test_ac1_named_policy_write_file_asks_and_read_file_allows(
    tmp_path, monkeypatch
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    # The named preset must actually resolve rather than silently fall back
    # to the default rules.
    assert rules != DEFAULT_PERMISSIONS

    assert _effect(rules, "write_file", {"path": "notes.txt"}) == "ask"
    assert _effect(rules, "read_file", {"path": "notes.txt"}) == "allow"


def test_sandbox_policy_allows_every_listed_read_only_tool(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert all(_effect(rules, tool) == "allow" for tool in SANDBOX_READ_ONLY_TOOLS)


def test_ac2_bash_rm_rf_dot_is_denied(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "bash", {"command": "rm -rf ."}) == "deny"


def test_sandbox_policy_denies_destructive_bash_commands(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    for command in DESTRUCTIVE_COMMANDS:
        assert _effect(rules, "bash", {"command": command}) == "deny"


def test_ac3_bash_git_status_is_allowed(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "bash", {"command": "git status"}) == "allow"


def test_sandbox_policy_allows_safe_read_only_bash_prefixes(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    for command in SAFE_READ_ONLY_COMMANDS:
        assert _effect(rules, "bash", {"command": command}) == "allow"


def test_ac4_arbitrary_bash_command_asks_not_allows(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    decision = evaluate_permission(
        "bash",
        {"command": "python run.py"},
        rules=rules,
        include_saved=False,
    )
    assert decision.effect == "ask"
    assert decision.effect != "allow"


def test_ac5_write_file_protected_path_is_denied(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert (
        _effect(rules, "write_file", {"path": ".features/f001.json"}) == "deny"
    )


def test_sandbox_policy_denies_protected_write_paths(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    for tool, path in (
        ("write_file", ".features/f001.json"),
        ("edit_file", ".features/f001.json"),
        ("write_file", ".project/goal.md"),
        ("edit_file", ".project/goal.json"),
    ):
        assert _effect(rules, tool, {"path": path}) == "deny"


def test_sandbox_policy_asks_for_external_directory_and_unknown_tools(
    tmp_path, monkeypatch
):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, "external_directory") == "ask"
    assert _effect(rules, "some_unrecognized_tool") == "ask"


def test_ac6_unknown_policy_name_falls_back_to_default_rules(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "not-a-real-policy")

    assert rules == DEFAULT_PERMISSIONS
