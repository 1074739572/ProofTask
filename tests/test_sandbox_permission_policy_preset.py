"""Regression tests for the named ``sandbox`` permission policy preset.

Pins the acceptance contract for selecting the sandbox policy in the
permission config: ``load_permission_rules()`` resolves the named preset,
read-only tools evaluate to allow, file writes ask except for protected
workspace paths, bash is limited to safe read-only prefixes, and unknown
policy names fall back to the default rules without raising.
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


def test_sandbox_policy_write_file_asks_and_read_file_allows(tmp_path, monkeypatch):
    # AC1: a config selecting the sandbox policy resolves to the named preset,
    # where write_file evaluates to ask and read_file evaluates to allow.
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    # The named preset must actually resolve rather than silently falling back
    # to the default rules.
    assert rules != DEFAULT_PERMISSIONS

    assert _effect(rules, "write_file", {"path": "notes.txt"}) == "ask"
    assert _effect(rules, "read_file", {"path": "notes.txt"}) == "allow"


def test_sandbox_policy_allows_all_read_only_tools(tmp_path, monkeypatch):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert all(_effect(rules, tool) == "allow" for tool in SANDBOX_READ_ONLY_TOOLS)


@pytest.mark.parametrize(
    "command",
    (
        "rm -rf .",  # AC2
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
        "git status",  # AC3
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
    # AC4: an arbitrary command such as 'python run.py' asks, not allows.
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    decision = evaluate_permission(
        "bash",
        {"command": "python run.py"},
        rules=rules,
        include_saved=False,
    )
    assert decision.effect == "ask"
    assert decision.effect != "allow"


@pytest.mark.parametrize(
    ("tool", "path"),
    (
        ("write_file", ".features/f001.json"),  # AC5
        ("edit_file", ".features/f001.json"),
        ("write_file", ".project/goal.md"),
        ("edit_file", ".project/goal.json"),
    ),
)
def test_sandbox_policy_denies_protected_write_paths(tmp_path, monkeypatch, tool, path):
    rules = _load_named_policy(tmp_path, monkeypatch, "sandbox")

    assert _effect(rules, tool, {"path": path}) == "deny"


def test_unknown_policy_name_falls_back_to_default_rules(tmp_path, monkeypatch):
    # AC6: an unknown policy name falls back to the default rules without raising.
    rules = _load_named_policy(tmp_path, monkeypatch, "not-a-real-policy")

    assert rules == DEFAULT_PERMISSIONS
