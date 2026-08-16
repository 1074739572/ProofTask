"""Regression tests for the named sandbox permission policy preset."""

from __future__ import annotations

import json

from harness.permissions.config import DEFAULT_PERMISSIONS, load_permission_rules
from harness.permissions.engine import evaluate_permission


def _load_policy(tmp_path, monkeypatch, policy: str):
    config_path = tmp_path / "permissions.json"
    config_path.write_text(json.dumps({"permission": policy}), encoding="utf-8")
    monkeypatch.setattr(
        "harness.permissions.config.get_permissions_config_path",
        lambda: config_path,
    )
    return load_permission_rules()


def test_sandbox_policy_resolves_expected_workspace_permissions(tmp_path, monkeypatch):
    rules = _load_policy(tmp_path, monkeypatch, "sandbox")

    expected = {
        "read_file": "allow",
        "glob": "allow",
        "load_skill": "allow",
        "rag_search": "allow",
        "rag_status": "allow",
        "project_status": "allow",
        "list_tasks": "allow",
        "get_task": "allow",
        "web_search": "allow",
        "todo_write": "allow",
        "compact": "allow",
        "list_features": "allow",
        "check_inbox": "allow",
        "list_crons": "allow",
        "write_file": "ask",
        "edit_file": "ask",
        "external_directory": "ask",
        "unrecognized_tool": "ask",
    }
    actual = {
        tool: evaluate_permission(tool, {}, rules=rules, include_saved=False).effect
        for tool in expected
    }

    assert actual == expected


def test_sandbox_policy_enforces_bash_allow_ask_and_deny_rules(tmp_path, monkeypatch):
    rules = _load_policy(tmp_path, monkeypatch, "sandbox")

    effects = {
        command: evaluate_permission(
            "bash",
            {"command": command},
            rules=rules,
            include_saved=False,
        ).effect
        for command in ("rm -rf .", "git status", "python run.py")
    }

    assert effects == {
        "rm -rf .": "deny",
        "git status": "allow",
        "python run.py": "ask",
    }


def test_sandbox_policy_denies_protected_write_paths(tmp_path, monkeypatch):
    rules = _load_policy(tmp_path, monkeypatch, "sandbox")

    assert evaluate_permission(
        "write_file",
        {"path": ".features/f001.json"},
        rules=rules,
        include_saved=False,
    ).effect == "deny"


def test_unknown_policy_falls_back_to_default_rules(tmp_path, monkeypatch):
    rules = _load_policy(tmp_path, monkeypatch, "not-a-real-policy")

    assert rules == DEFAULT_PERMISSIONS
