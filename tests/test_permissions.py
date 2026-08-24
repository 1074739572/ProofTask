"""Declarative permission engine tests."""

from __future__ import annotations

from unittest import mock

from harness.hooks import permission_hook
from harness.permissions.config import SANDBOX_PERMISSIONS, _normalize_rules
from harness.permissions.engine import evaluate_permission, evaluate_single_permission
from harness.permissions.state import (
    add_session_rule,
    clear_session_rules,
    session_rules,
)


def test_bash_last_matching_rule_wins():
    rules = {
        "*": "ask",
        "bash": {
            "*": "ask",
            "git *": "allow",
            "git push*": "deny",
        },
    }
    assert evaluate_permission("bash", {"command": "git status"}, rules=rules).effect == "allow"
    assert evaluate_permission("bash", {"command": "git push origin main"}, rules=rules).effect == "deny"


def test_specific_mcp_rule_beats_wildcard_rule_regardless_of_order():
    rules = {
        "mcp__playwright__browser_tabs": "ask",
        "mcp__*": "allow",
    }
    assert evaluate_permission("mcp__playwright__browser_tabs", {}, rules=rules).effect == "ask"


def test_compound_bash_command_never_inherits_prefix_allow_rule():
    rules = {"bash": {"dir *": "allow", "*": "ask"}}
    decision = evaluate_permission("bash", {"command": "dir . & del important.txt"}, rules=rules)
    assert decision.effect == "ask"
    assert decision.source == "safety"


def test_windows_file_paths_use_the_same_protected_path_rules():
    rules = {"write_file": {"*": "allow", ".features/*": "deny"}}
    assert evaluate_permission(
        "write_file", {"path": ".features\\f001.json"}, rules=rules
    ).effect == "deny"


def test_patch_file_uses_file_path_permission_rules():
    rules = {"patch_file": {"*": "allow", ".project/goal.json": "deny"}}
    assert evaluate_permission(
        "patch_file", {"path": ".project\\goal.json", "hunks": []}, rules=rules
    ).effect == "deny"


def test_inspect_and_git_tools_are_read_only_by_default():
    assert evaluate_permission("inspect_file", {"path": "src/app.py"}).effect == "allow"
    assert evaluate_permission("inspect_file", {"path": ".env"}).effect == "deny"
    assert evaluate_permission("search_text", {"pattern": "needle"}).effect == "allow"
    assert evaluate_permission("git_status", {}).effect == "allow"
    assert evaluate_permission("git_diff", {}).effect == "allow"


def test_sandbox_protects_absolute_goal_paths_and_keeps_verification_available(tmp_path, monkeypatch):
    import harness.permissions.engine as engine

    monkeypatch.setattr(engine, "get_workdir", lambda: tmp_path)
    goal_path = str((tmp_path / ".project" / "goal.json").resolve())
    assert evaluate_permission(
        "write_file", {"path": goal_path}, rules=SANDBOX_PERMISSIONS, include_saved=False
    ).effect == "deny"
    assert evaluate_permission(
        "read_file", {"path": ".env"}, rules=SANDBOX_PERMISSIONS, include_saved=False
    ).effect == "deny"
    assert evaluate_single_permission(
        "verify_command", "python -m pytest -q", rules=SANDBOX_PERMISSIONS, include_saved=False
    ).effect == "allow"


def test_file_resource_rules_can_deny_env_but_allow_example():
    rules = {
        "read_file": {
            "*": "allow",
            "*.env": "deny",
            "*.env.*": "deny",
            "*.env.example": "allow",
        }
    }
    assert evaluate_permission("read_file", {"path": ".env"}, rules=rules).effect == "deny"
    assert evaluate_permission("read_file", {"path": ".env.local"}, rules=rules).effect == "deny"
    assert evaluate_permission("read_file", {"path": ".env.example"}, rules=rules).effect == "allow"


def test_tool_name_wildcard_matches_mcp_tools():
    rules = {
        "*": "allow",
        "mcp__*": "ask",
        "mcp__github__delete_branch": "deny",
    }
    assert evaluate_permission("mcp__fetch__fetch", {"url": "https://example.com"}, rules=rules).effect == "ask"
    assert evaluate_permission("mcp__github__delete_branch", {"branch": "main"}, rules=rules).effect == "deny"


def test_mcp_annotations_are_fallback_only():
    assert (
        evaluate_permission(
            "mcp__docs__search",
            {"query": "permissions"},
            mcp_meta={"readOnly": True},
            rules={},
        ).effect
        == "allow"
    )
    assert (
        evaluate_permission(
            "mcp__unknown__mutate",
            {"id": "1"},
            mcp_meta={},
            rules={},
        ).effect
        == "ask"
    )


def test_normalize_accepts_top_level_string_policy():
    assert _normalize_rules({"permission": "allow"}) == {"*": "allow"}


def test_session_rule_overrides_config():
    clear_session_rules()
    try:
        add_session_rule("bash", "npm test*", "allow")
        decision = evaluate_permission(
            "bash",
            {"command": "npm test -- --watch=false"},
            rules={"bash": {"*": "ask"}},
        )
        assert decision.effect == "allow"
        assert decision.source == "session"
    finally:
        clear_session_rules()


def test_external_directory_gate_runs_before_tool_permission():
    rules = {
        "read_file": "allow",
        "external_directory": "ask",
    }
    decision = evaluate_permission(
        "read_file",
        {"path": "C:/outside/project/notes.txt"},
        rules=rules,
        include_saved=False,
    )
    assert decision.effect == "ask"
    assert decision.save_tool == "external_directory"
    assert decision.external_resource is not None


def test_external_directory_can_be_allowed_then_tool_rule_applies():
    rules = {
        "external_directory": {"C:/outside/project*": "allow"},
        "read_file": "allow",
    }
    decision = evaluate_permission(
        "read_file",
        {"path": "C:/outside/project/notes.txt"},
        rules=rules,
        include_saved=False,
    )
    assert decision.effect == "allow"
    assert decision.tool == "read_file"


def test_permission_hook_remembers_session_approval():
    clear_session_rules()
    response = mock.Mock(decision="session", allowed=True, remember_session=True, remember_always=False, value="")
    block = {"name": "bash", "input": {"command": "npm test"}}
    try:
        with mock.patch("harness.hooks.evaluate_permission") as evaluate, mock.patch(
            "harness.hooks.ask_permission", return_value=response
        ), mock.patch("harness.hooks.audit_permission"):
            evaluate.return_value = mock.Mock(
                effect="ask",
                resource="npm test",
                reason="matched bash:*",
                source="config",
                save_tool="bash",
                save_resource="npm test*",
                external_resource=None,
            )
            assert permission_hook(block) is None
        assert any(rule.tool == "bash" and rule.resource == "npm test*" for rule in session_rules())
    finally:
        clear_session_rules()


def _ask_decision(resource: str):
    return mock.Mock(
        effect="ask",
        resource=resource,
        reason="matched wildcard ask rule",
        source="config",
        save_tool="",
        save_resource="",
        external_resource=None,
    )


def test_goal_write_inside_task_scope_is_auto_approved(tmp_path):
    from harness.goal.authority import goal_authority

    block = {"name": "write_file", "input": {"path": "src/app.py", "content": "x"}}
    with goal_authority(
        goal_id="goal-1",
        task_id="task-1",
        phase="act",
        workspace=tmp_path,
        write_roots=("src",),
    ), mock.patch("harness.hooks.evaluate_permission", return_value=_ask_decision("src/app.py")), mock.patch(
        "harness.goal.runner.is_goal_noninteractive", return_value=True
    ), mock.patch("harness.goal.runner.mark_goal_permission_pending") as pending, mock.patch(
        "harness.hooks.ask_permission"
    ) as ask, mock.patch("harness.hooks.audit_permission"):
        assert permission_hook(block) is None

    pending.assert_not_called()
    ask.assert_not_called()


def test_goal_authority_is_the_shared_boundary_for_the_write_tool(tmp_path):
    from harness.agents.runner import _tools_for_agent
    from harness.goal.authority import goal_authority

    (tmp_path / "src").mkdir()
    with goal_authority(
        goal_id="goal-1",
        task_id="task-1",
        phase="act",
        workspace=tmp_path,
        write_roots=("src/app.py",),
    ):
        _tools, handlers = _tools_for_agent(
            ["write_file"],
            cwd=tmp_path,
            # Deliberately different from GoalAuthority. The Goal path must
            # be evaluated by the shared authority, not this stale projection.
            write_roots=("other",),
        )
        result = handlers["write_file"](path="src/app.py", content="value = 1\n")
        blocked = handlers["write_file"](path="outside.py", content="blocked\n")

    assert result == "Wrote 10 bytes to src/app.py"
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert blocked == "Write blocked: path is outside the current agent write scope."
    assert not (tmp_path / "outside.py").exists()


def test_goal_write_outside_task_scope_becomes_supervisor_boundary(tmp_path):
    from harness.goal.authority import goal_authority

    block = {"name": "edit_file", "input": {"path": "src/shared.py", "old_text": "a", "new_text": "b"}}
    with goal_authority(
        goal_id="goal-1",
        task_id="task-1",
        phase="act",
        workspace=tmp_path,
        write_roots=("src/app.py",),
    ), mock.patch("harness.hooks.evaluate_permission", return_value=_ask_decision("src/shared.py")), mock.patch(
        "harness.goal.runner.is_goal_noninteractive", return_value=True
    ), mock.patch("harness.goal.runner.mark_goal_permission_pending") as pending, mock.patch(
        "harness.hooks.ask_permission"
    ) as ask, mock.patch("harness.hooks.audit_permission"):
        result = permission_hook(block)

    assert "Permission deferred" in result
    assert pending.call_args.args[0]["path"] == "src/shared.py"
    ask.assert_not_called()


def test_goal_task_scope_is_enforced_before_a_generic_allow_rule(tmp_path):
    from harness.goal.authority import goal_authority

    decision = _ask_decision("src/shared.py")
    decision.effect = "allow"
    block = {"name": "write_file", "input": {"path": "src/shared.py", "content": "x"}}
    with goal_authority(
        goal_id="goal-1",
        task_id="task-1",
        phase="act",
        workspace=tmp_path,
        write_roots=("src/app.py",),
    ), mock.patch("harness.hooks.evaluate_permission", return_value=decision), mock.patch(
        "harness.goal.runner.is_goal_noninteractive", return_value=True
    ), mock.patch("harness.goal.runner.mark_goal_permission_pending") as pending, mock.patch(
        "harness.hooks.audit_permission"
    ):
        result = permission_hook(block)

    assert "Permission deferred" in result
    assert pending.call_args.args[0]["path"] == "src/shared.py"


def test_goal_hard_deny_cannot_be_overridden_by_scope(tmp_path):
    from harness.goal.authority import goal_authority

    decision = _ask_decision("src/app.py")
    decision.effect = "deny"
    decision.reason = "protected by policy"
    block = {"name": "write_file", "input": {"path": "src/app.py", "content": "x"}}
    with goal_authority(
        goal_id="goal-1",
        task_id="task-1",
        phase="act",
        workspace=tmp_path,
        write_roots=("src",),
    ), mock.patch("harness.hooks.evaluate_permission", return_value=decision), mock.patch(
        "harness.goal.runner.is_goal_noninteractive", return_value=True
    ), mock.patch("harness.goal.runner.mark_goal_permission_pending") as pending, mock.patch(
        "harness.hooks.audit_permission"
    ):
        result = permission_hook(block)

    assert "Permission denied" in result
    pending.assert_not_called()


def test_goal_bash_request_is_deferred_and_never_scope_approved():
    block = {"name": "bash", "input": {"command": "git status"}}
    with mock.patch("harness.hooks.evaluate_permission", return_value=_ask_decision("git status")), mock.patch(
        "harness.goal.runner.is_goal_noninteractive", return_value=True
    ), mock.patch("harness.goal.runner.mark_goal_permission_pending") as pending, mock.patch(
        "harness.ui.permission_events.request_permission"
    ) as request, mock.patch("harness.hooks.audit_permission"):
        result = permission_hook(block)

    assert "Permission deferred" in result
    assert pending.call_args.args[0]["tool"] == "bash"
    assert pending.call_args.args[0]["command"] == "git status"
    request.assert_not_called()


def test_permission_hook_persists_always_approval():
    response = mock.Mock(decision="always", allowed=True, remember_session=False, remember_always=True, value="")
    block = {"name": "bash", "input": {"command": "npm test"}}
    with mock.patch("harness.hooks.evaluate_permission") as evaluate, mock.patch(
        "harness.hooks.ask_permission", return_value=response
    ), mock.patch("harness.hooks.audit_permission"), mock.patch(
        "harness.hooks.add_persistent_rule"
    ) as add_persistent:
        evaluate.return_value = mock.Mock(
            effect="ask",
            resource="npm test",
            reason="matched bash:*",
            source="config",
            save_tool="bash",
            save_resource="npm test*",
            external_resource=None,
        )
        assert permission_hook(block) is None
    add_persistent.assert_called_once_with("bash", "npm test*", "allow")


def test_permission_hook_rejects_model_controlled_bash_cwd():
    block = {"name": "bash", "input": {"command": "dir", "cwd": "C:/Users"}}
    assert "execution-owned" in permission_hook(block)
