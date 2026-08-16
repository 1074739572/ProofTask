"""Regression tests for the shipped ``config/permissions.json`` sandbox default.

Pins the acceptance contract for a fresh harness run: the permission loader
resolves the real shipped config path (with no injected rules or project
override) and that file selects or contains the sandbox policy. File writes
ask, destructive bash commands deny, safe read-only bash and reads allow,
absolute paths outside the workspace hit the ``external_directory`` gate
before the tool rule applies, and unknown MCP tools ask. The shipped config
must not contain blanket allow rules for write_file, edit_file, bash, or
external_directory.
"""

from __future__ import annotations

import pytest

from harness.permissions.config import get_permissions_config_path, load_permission_rules
from harness.permissions.engine import evaluate_permission
from harness.settings import BUILTIN_CONFIG_DIR

SHIPPED_PERMISSIONS_PATH = BUILTIN_CONFIG_DIR / "permissions.json"


@pytest.fixture
def shipped_workspace(tmp_path, monkeypatch):
    """Point the active workspace at an empty temp dir with no project config.

    ``get_permissions_config_path()`` then resolves the built-in shipped
    ``config/permissions.json`` instead of any project-level override, and the
    engine's workspace boundary is known for the external-directory gate.
    """
    import harness.settings as settings

    monkeypatch.setattr(settings, "_workspace", tmp_path.resolve())
    return tmp_path


def _decision(tool: str, tool_input: dict | None = None):
    """Evaluate through the engine, letting it load the real shipped config."""
    return evaluate_permission(tool, tool_input or {}, include_saved=False)


def _blanket_effect(rule) -> str | None:
    """Return the catch-all effect of a rule, or None when there is none."""
    if rule == "allow":
        return "allow"
    if isinstance(rule, dict):
        return rule.get("*")
    return None


def test_permission_loader_resolves_the_shipped_config_path(shipped_workspace):
    # A fresh harness run with no project-level override must load the shipped
    # config file from the real config path, not fall back to injected rules.
    path = get_permissions_config_path()

    assert path.resolve() == SHIPPED_PERMISSIONS_PATH.resolve()
    assert path.exists()


def test_shipped_config_has_no_blanket_allows_for_write_edit_bash_external(
    shipped_workspace,
):
    rules = load_permission_rules()

    # The sandbox default must not carry permissive catch-all rules for the
    # mutating tools; they may only be allowed per-resource/prefix.
    for tool in ("write_file", "edit_file", "bash", "external_directory"):
        assert _blanket_effect(rules.get(tool)) != "allow", tool
    assert rules.get("*") != "allow"


def test_ac1_write_file_on_workspace_path_is_ask(shipped_workspace):
    decision = _decision("write_file", {"path": "notes.txt"})

    assert decision.effect == "ask"


def test_ac2_bash_rm_rf_dot_is_denied(shipped_workspace):
    decision = _decision("bash", {"command": "rm -rf ."})

    assert decision.effect == "deny"


def test_ac3_bash_git_status_is_allowed(shipped_workspace):
    decision = _decision("bash", {"command": "git status"})

    assert decision.effect == "allow"


def test_ac4_read_file_readme_is_allowed(shipped_workspace):
    decision = _decision("read_file", {"path": "README.md"})

    assert decision.effect == "allow"


def test_ac5_absolute_path_outside_workspace_hits_external_directory_gate(
    shipped_workspace,
):
    outside = shipped_workspace.parent / "outside_workspace" / "notes.txt"

    decision = _decision("read_file", {"path": str(outside)})

    # read_file alone would allow; the external_directory gate must return a
    # non-allow effect before the tool rule applies.
    assert decision.effect != "allow"
    assert decision.save_tool == "external_directory"
    assert decision.external_resource is not None


def test_ac6_unknown_mcp_tool_is_ask(shipped_workspace):
    decision = _decision("mcp__unknown__do_thing", {"id": "1"})

    assert decision.effect == "ask"


def test_sandbox_config_arbitrary_bash_command_asks(shipped_workspace):
    # No blanket bash allow: an unlisted command must ask, not allow.
    decision = _decision("bash", {"command": "python run.py"})

    assert decision.effect == "ask"
    assert decision.effect != "allow"


def test_sandbox_config_edit_file_asks(shipped_workspace):
    # No blanket edit_file allow: a normal workspace edit must ask.
    decision = _decision("edit_file", {"path": "notes.txt"})

    assert decision.effect == "ask"
