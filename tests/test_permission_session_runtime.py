"""Regression tests for session-scoped permission modes."""

from __future__ import annotations

from unittest import mock

from harness.cli import _handle_permission_command, handle_permission_command
from harness.hooks import permission_hook
from harness.permission_session import (
    PermissionSession,
    get_permission_session,
    reset_permission_session,
)


def setup_function() -> None:
    reset_permission_session()


def teardown_function() -> None:
    reset_permission_session()


def test_session_instances_are_default_and_isolated() -> None:
    first = PermissionSession()
    second = PermissionSession()
    assert first.mode == "default"
    assert second.current_mode == "default"
    first.set_mode("full-access")
    assert first.get_mode() == "full-access"
    assert second.get_mode() == "default"


def test_invalid_mode_does_not_change_state() -> None:
    state = PermissionSession()
    try:
        state.set_mode("banana")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid permission mode should fail")
    assert state.mode == "default"


def test_permission_command_reports_and_switches_active_session() -> None:
    state = PermissionSession()
    assert "default" in _handle_permission_command("/permission", state)
    assert "auto-review" in _handle_permission_command("/permission", state)
    assert "auto-review" in _handle_permission_command("/permission auto-review", state)
    assert state.mode == "auto-review"
    assert "full-access" in handle_permission_command("/permission full-access", state)
    assert state.mode == "full-access"
    invalid = handle_permission_command("/permission banana", state)
    assert "Usage" in invalid
    assert state.mode == "full-access"


def test_classic_permission_picker_applies_selected_mode() -> None:
    from harness.ui import permission_picker

    with mock.patch.object(permission_picker, "is_interactive_tty", return_value=True), mock.patch.object(
        permission_picker, "select_from_list", return_value=2
    ):
        result = permission_picker.run_permission_picker()
    assert "full-access" in result
    assert get_permission_session().mode == "full-access"


def test_hook_mode_matrix_and_blocked_red_line() -> None:
    state = get_permission_session()
    block_read = {"name": "read_file", "input": {"path": "README.md"}}
    block_write = {"name": "write_file", "input": {"path": "notes.txt", "content": "x"}}
    block_unknown = {"name": "unknown_tool", "input": {}}

    with mock.patch("harness.hooks.ask_permission") as ask, mock.patch(
        "harness.hooks.audit_permission"
    ):
        ask.return_value = mock.Mock(
            decision="deny", allowed=False, remember_session=False,
            remember_always=False, value="",
        )
        assert permission_hook(block_read) is None
        assert ask.call_count == 0
        assert permission_hook(block_write) is not None
        assert ask.call_count == 1

        state.set_mode("auto-review")
        assert permission_hook(block_write) is None
        assert ask.call_count == 1

        state.set_mode("default")
        assert permission_hook(block_unknown) is not None
        assert ask.call_count == 2

        state.set_mode("full-access")
        # Unknown tools remain interactive even in full-access; risk=high is
        # not sufficient evidence to authorize an unregistered handler.
        assert permission_hook(block_unknown) is not None
        assert ask.call_count == 3

        # A policy-classified hard block cannot be widened by full-access.
        blocked = {"name": "bash", "input": {"command": "sudo rm -rf /"}}
        assert "Permission denied" in permission_hook(blocked)
        assert ask.call_count == 3


def test_goal_noninteractive_bypasses_session_overlay() -> None:
    get_permission_session().set_mode("full-access")
    block = {"name": "bash", "input": {"command": "echo hi"}}
    with mock.patch("harness.hooks.evaluate_permission") as evaluate, mock.patch(
        "harness.goal.runner.is_goal_noninteractive", return_value=True
    ), mock.patch("harness.goal.runner.mark_goal_permission_pending") as pending, mock.patch(
        "harness.hooks.ask_permission"
    ) as ask, mock.patch("harness.hooks.audit_permission"):
        evaluate.return_value = mock.Mock(
            effect="ask", resource="echo hi", reason="ask", source="config",
            save_tool="bash", save_resource="echo hi", external_resource=None,
        )
        result = permission_hook(block)
    assert "Permission deferred" in result
    pending.assert_called_once()
    ask.assert_not_called()
