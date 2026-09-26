"""Execution-boundary tests: opaque scripts and action-time deletion.

Pins the P1 decision matrix:
- opaque interpreter/script execution never inherits broad config or saved
  approvals; only an exact command-level authorization satisfies it, and no
  session mode (including full-access) auto-approves it;
- irreversible local deletion (del/erase/rd/rmdir/Remove-Item) prompts on
  every occurrence, ignoring saved approvals and session modes;
- ordinary commands keep the existing rule + risk two-layer behavior.
"""

from __future__ import annotations

from unittest import mock

from harness.hooks import permission_hook
from harness.permission_session import get_permission_session, reset_permission_session
from harness.permissions.engine import evaluate_permission
from harness.permissions.execution import classify_bash_execution
from harness.permissions.state import add_session_rule, clear_session_rules


def setup_function() -> None:
    reset_permission_session()
    clear_session_rules()


def teardown_function() -> None:
    reset_permission_session()
    clear_session_rules()


# --- classifier unit checks ---


def test_classifier_marks_windows_deletes_action_time() -> None:
    assert classify_bash_execution("del notes.txt") == "action_time"
    assert classify_bash_execution("erase /f tmp.log") == "action_time"
    assert classify_bash_execution("rmdir /s build") == "action_time"
    assert classify_bash_execution("Remove-Item .\\dist -Recurse") == "action_time"


def test_classifier_marks_interpreter_invocations_opaque() -> None:
    assert classify_bash_execution("python deploy.py") == "opaque"
    assert classify_bash_execution("py -3 tools\\build.py") == "opaque"
    assert classify_bash_execution('python -c "import os"') == "opaque"
    assert classify_bash_execution("node scripts/migrate.mjs") == "opaque"
    assert classify_bash_execution("node -e \"console.log(1)\"") == "opaque"
    assert classify_bash_execution("powershell -File setup.ps1") == "opaque"
    assert classify_bash_execution("pwsh -Command Get-ChildItem") == "opaque"
    assert classify_bash_execution(".\\scripts\\release.bat") == "opaque"
    assert classify_bash_execution("bash tools/check.sh") == "opaque"


def test_classifier_keeps_ordinary_commands_normal() -> None:
    assert classify_bash_execution("git status") == "normal"
    assert classify_bash_execution("npm test") == "normal"
    assert classify_bash_execution("python -m pytest -q") == "normal"
    assert classify_bash_execution("python -m pytest tests/test_permissions.py") == "normal"
    assert classify_bash_execution("dir /b") == "normal"
    assert classify_bash_execution("") == "normal"


# --- engine matrix: opaque ---


def test_opaque_script_asks_despite_wildcard_config_allow() -> None:
    decision = evaluate_permission(
        "bash", {"command": "python deploy.py"}, rules={"bash": {"*": "allow"}}
    )
    assert decision.effect == "ask"
    assert decision.approval == "scoped"
    assert decision.source == "safety"


def test_opaque_script_allows_with_exact_config_rule() -> None:
    decision = evaluate_permission(
        "bash",
        {"command": "python deploy.py"},
        rules={"bash": {"*": "ask", "python deploy.py": "allow"}},
    )
    assert decision.effect == "allow"
    assert decision.source == "config"


def test_opaque_script_ignores_wildcard_saved_rule() -> None:
    add_session_rule("bash", "python *", "allow")
    add_session_rule("bash", "*", "allow")
    decision = evaluate_permission(
        "bash", {"command": "python deploy.py"}, rules={"bash": {"*": "ask"}}
    )
    assert decision.effect == "ask"
    assert decision.approval == "scoped"


def test_opaque_script_honors_exact_saved_rule() -> None:
    add_session_rule("bash", "python deploy.py", "allow")
    decision = evaluate_permission(
        "bash", {"command": "python deploy.py"}, rules={"bash": {"*": "ask"}}
    )
    assert decision.effect == "allow"
    assert decision.source == "session"


# --- engine matrix: action_time ---


def test_delete_always_asks_even_with_config_and_saved_allow() -> None:
    add_session_rule("bash", "del notes.txt", "allow")
    decision = evaluate_permission(
        "bash",
        {"command": "del notes.txt"},
        rules={"bash": {"*": "ask", "del *": "allow"}},
    )
    assert decision.effect == "ask"
    assert decision.approval == "action_time"
    assert decision.source == "safety"


def test_compound_command_with_delete_segment_asks_action_time() -> None:
    decision = evaluate_permission(
        "bash",
        {"command": "dir & del notes.txt"},
        rules={"bash": {"dir *": "allow", "*": "ask"}},
    )
    assert decision.effect == "ask"
    assert decision.source == "safety"
    assert decision.approval == "action_time"


def test_hard_deny_still_wins_over_execution_classes() -> None:
    decision = evaluate_permission(
        "bash", {"command": "rm -rf build"}, rules={"bash": {"rm *": "deny", "*": "allow"}}
    )
    assert decision.effect == "deny"


def test_normal_command_still_uses_wildcard_saved_rule() -> None:
    add_session_rule("bash", "npm *", "allow")
    decision = evaluate_permission(
        "bash", {"command": "npm test"}, rules={"bash": {"*": "ask"}}
    )
    assert decision.effect == "allow"
    assert decision.source == "session"
    assert decision.approval == "normal"


# --- hook matrix: session modes cannot widen the new boundaries ---


def _prompt_allowed():
    return mock.Mock(
        decision="allow",
        allowed=True,
        remember_session=False,
        remember_always=False,
        value="",
    )


def test_full_access_does_not_auto_approve_opaque_script() -> None:
    get_permission_session().set_mode("full-access")
    block = {"name": "bash", "input": {"command": "python deploy.py"}}
    with mock.patch("harness.hooks.ask_permission", return_value=_prompt_allowed()) as ask, mock.patch(
        "harness.hooks.audit_permission"
    ):
        assert permission_hook(block) is None
        ask.assert_called_once()


def test_full_access_does_not_auto_approve_delete() -> None:
    get_permission_session().set_mode("full-access")
    block = {"name": "bash", "input": {"command": "del notes.txt"}}
    with mock.patch("harness.hooks.ask_permission", return_value=_prompt_allowed()) as ask, mock.patch(
        "harness.hooks.audit_permission"
    ):
        assert permission_hook(block) is None
        ask.assert_called_once()


def test_saved_bash_wildcard_does_not_cover_delete_or_opaque() -> None:
    get_permission_session().set_mode("full-access")
    add_session_rule("bash", "*", "allow")
    with mock.patch("harness.hooks.ask_permission", return_value=_prompt_allowed()) as ask, mock.patch(
        "harness.hooks.audit_permission"
    ):
        assert permission_hook({"name": "bash", "input": {"command": "del a.txt"}}) is None
        assert permission_hook({"name": "bash", "input": {"command": "node migrate.js"}}) is None
        assert ask.call_count == 2


def test_delete_prompt_does_not_offer_remember() -> None:
    block = {"name": "bash", "input": {"command": "del notes.txt"}}
    with mock.patch("harness.hooks.ask_permission", return_value=_prompt_allowed()) as ask, mock.patch(
        "harness.hooks.audit_permission"
    ):
        assert permission_hook(block) is None
        assert ask.call_args.kwargs["remember"] is False


def test_default_mode_keeps_read_only_commands_silent() -> None:
    block = {"name": "bash", "input": {"command": "ls -la"}}
    with mock.patch("harness.hooks.ask_permission") as ask, mock.patch(
        "harness.hooks.audit_permission"
    ):
        assert permission_hook(block) is None
        ask.assert_not_called()


def test_default_mode_prompts_for_unknown_command() -> None:
    block = {"name": "bash", "input": {"command": "frobnicate --all-the-things"}}
    with mock.patch("harness.hooks.ask_permission", return_value=_prompt_allowed()) as ask, mock.patch(
        "harness.hooks.audit_permission"
    ):
        assert permission_hook(block) is None
        ask.assert_called_once()
